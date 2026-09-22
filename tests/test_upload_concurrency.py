"""Outbound uploads under genuine concurrency, through the real ASGI app.

Every test here drives overlapping requests against the mounted application with
one session per request, because that is the only shape in which these defects
appear: the sequential tests elsewhere in the suite pass precisely because they
never leave two callers inside the same slot at the same time.

Requests are issued with ``httpx.AsyncClient`` over ``ASGITransport``, so both
requests are ordinary tasks on one event loop -- exactly what single-process
uvicorn does with two clients.
"""

import asyncio
import hashlib
import smtplib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.config import settings
from src.models.base import Base
from src.models.outbound_upload import OutboundUpload
from src.models.smtp_config import SMTPConfig

# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


class _CapturedSMTP:
    """Stands in for the socket, so the real MIME builder still runs.

    ``after_send`` and ``refuse`` model the two ways a conversation ends
    *after* the message has already been handed over: the transport dying on
    the way to the final 250, and the server accepting only part of the
    recipient set. In both the bytes are on the wire, which is what makes the
    outcome ambiguous rather than failed.
    """

    def __init__(self, after_send=None, refuse=None):
        self.messages = []
        self._after_send = after_send
        self._refuse = refuse or {}

    def send_message(self, message, to_addrs=None):
        self.messages.append(message)
        if self._after_send is not None:
            raise self._after_send("the connection dropped before the final 250")
        return dict(self._refuse)

    def quit(self):
        return None


@pytest.fixture
def app_under_load(tmp_path, monkeypatch):
    """The real app, an isolated database, a scratch volume, one session per request."""
    from src import mcp_tools
    from src.database import connection
    from src.database.connection import get_db
    from src.handlers import email_handler
    from src.server import api_app, final_app

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    engine = create_engine(
        f"sqlite:///{tmp_path}/concurrency.db",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(connection, "engine", engine)
    monkeypatch.setattr(connection, "SessionLocal", Session)
    monkeypatch.setattr(mcp_tools, "SessionLocal", Session)

    def override_db():
        # A session per request, as in production: two overlapping requests do
        # not share an identity map, so neither can see the other's uncommitted
        # work. Anything atomic has to be made atomic in the database.
        db = Session()
        try:
            yield db
        finally:
            db.close()

    # api_app is mounted inside final_app and resolves its own dependencies, so
    # an override registered on the outer app would silently never apply.
    api_app.dependency_overrides[get_db] = override_db

    sent: list = []

    class _Manager:
        """Routes a send through the real MIME construction, stopping at the wire."""

        result = {"success": True, "delivery_state": "sent"}
        # A real SMTP conversation is the long await in the middle of a send.
        latency = 0.0
        # How the conversation ends once the message has been handed over: an
        # exception class for a transport that dies mid-handover, or a mapping
        # of refused recipients for a partial acceptance. Both leave the real
        # ``smtp_sender`` to decide the delivery_state, rather than asserting
        # one by fiat.
        wire_raises = None
        wire_refuses = None

        async def send_email_via_config(self, *, smtp_config_id, owner_user_id, **kwargs):
            from src.email.smtp_sender import EmailSender

            await asyncio.sleep(self.latency)
            if not self.result.get("success"):
                return self.result
            sender = EmailSender(
                SimpleNamespace(
                    name="test-account",
                    account_name="owner@example.com",
                    username="owner@example.com",
                    auth_type="password",
                )
            )
            server = _CapturedSMTP(self.wire_raises, self.wire_refuses)
            sender._server = server
            outcome = await sender.send_email(**kwargs)
            sent.extend(server.messages)
            return outcome

        async def invalidate(self, smtp_config_id):
            return None

    manager = _Manager()
    monkeypatch.setattr(email_handler, "email_sender_manager", manager)

    yield SimpleNamespace(
        app=final_app,
        Session=Session,
        root=tmp_path / "outbound-uploads",
        sent=sent,
        manager=manager,
    )
    api_app.dependency_overrides.clear()


def _client(service) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=service.app),
        base_url="http://testserver",
    )


def _owner(service):
    from src.security.auth import local_owner_user

    with service.Session() as db:
        owner = local_owner_user(db)
        db.expunge(owner)
        return owner


def _account(service, owner_id: int) -> SMTPConfig:
    with service.Session() as db:
        account = SMTPConfig(
            owner_user_id=owner_id,
            provider="imap",
            auth_type="password",
            name="Outbound",
            account_name="owner@example.com",
            host="imap.example.com",
            port=993,
            smtp_host="smtp.example.com",
            smtp_port=465,
            username="owner@example.com",
            credential_ciphertext="enc:test",
            imap_use_ssl=True,
            smtp_use_ssl=True,
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        db.expunge(account)
        return account


def _slot(service, owner_id: int, **kwargs) -> tuple[str, str]:
    """Reserve a slot directly and mint its grant: the URL a client would be given."""
    from src.security.upload_tokens import issue_upload_token
    from src.services.upload_service import begin_upload

    with service.Session() as db:
        upload = begin_upload(db, owner_id, **kwargs)
        upload_id = str(upload.upload_id)
    return upload_id, f"/api/v1/uploads/{upload_id}?token={issue_upload_token(owner_id, upload_id)}"


def _row(service, upload_id: str) -> OutboundUpload:
    with service.Session() as db:
        return db.query(OutboundUpload).filter_by(upload_id=upload_id).one()


def _live_rows(service, owner_id: int) -> list[OutboundUpload]:
    with service.Session() as db:
        return db.query(OutboundUpload).filter_by(owner_user_id=owner_id).all()


def _trickle(payload: bytes, chunks: int, pause: float = 0.01):
    """A body delivered in pieces, so the two requests genuinely interleave.

    Each ``await`` inside the generator is a point where the event loop hands
    control to the other in-flight request, which is what a real socket does.
    """
    size = max(1, len(payload) // chunks)

    async def body():
        for start in range(0, len(payload), size):
            await asyncio.sleep(pause)
            yield payload[start : start + size]

    return body()


def _attachment_parts(message) -> list[tuple[str, bytes]]:
    return [
        (part.get_filename(), part.get_payload(decode=True))
        for part in message.walk()
        if part.get_content_disposition() == "attachment"
    ]


# --------------------------------------------------------------------------
# Defect 1: the pending -> stored transition is not a claim
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_concurrent_puts_cannot_both_write_one_slot(app_under_load):
    """One slot, one payload -- even when both PUTs are in flight at once.

    ``store_upload_stream`` reads ``state != 'pending'`` and only then awaits the
    body, so two requests carrying the same valid grant both pass the guard,
    both open the same destination path and interleave their writes. The row
    then describes one attempt while the file holds a mixture of both, which
    silently breaks the declared_sha256 guarantee the MCP tool advertises.
    """
    owner = _owner(app_under_load)
    upload_id, url = _slot(app_under_load, owner.id, filename="contested.bin")
    # Deliberately different lengths, delivered at different rates: the longer
    # body finishes writing first, the shorter one commits last, so a row that
    # records the second attempt cannot possibly describe the file the first
    # left behind.
    first = b"A" * 32768
    second = b"B" * 8192

    async with _client(app_under_load) as client:
        responses = await asyncio.gather(
            client.put(url, content=_trickle(first, 8, pause=0.005)),
            client.put(url, content=_trickle(second, 2, pause=0.06)),
            return_exceptions=True,
        )

    codes = sorted(
        response.status_code for response in responses if isinstance(response, httpx.Response)
    )
    assert codes == [200, 409], f"both PUTs were accepted into one single-use slot: {responses}"

    row = _row(app_under_load, upload_id)
    on_disk = (app_under_load.root / upload_id).read_bytes()
    # The row is the integrity record the sender trusts; it has to describe the
    # bytes that are actually there.
    assert row.size == len(on_disk)
    assert row.sha256 == hashlib.sha256(on_disk).hexdigest()
    # And what is there is one client's payload, not a blend of two.
    assert on_disk in (first, second)
    assert len(list(app_under_load.root.iterdir())) == 1


# --------------------------------------------------------------------------
# Defect 2: a failing attempt must not destroy a payload someone else stored
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failing_put_cannot_delete_a_payload_another_put_stored(app_under_load, monkeypatch):
    """A refused attempt cleans up after itself, and only after itself.

    Both attempts write to one shared destination path, so the ``_discard()`` on
    the oversize attempt's failure path unlinks the file the honest attempt had
    already hashed, validated and committed. The row still says ``stored``, the
    bytes are gone, and because the state is no longer ``pending`` the slot can
    never be re-uploaded: a permanent denial of send, with the reserved bytes
    still charged against the owner's budget.
    """
    monkeypatch.setattr(settings, "max_outbound_attachment_bytes", 4096)
    owner = _owner(app_under_load)
    account = _account(app_under_load, owner.id)
    upload_id, url = _slot(app_under_load, owner.id, filename="valuable.bin")
    honest = b"the payload that was accepted" * 20

    async def oversize():
        # Slow and far too large: it is still streaming when the honest attempt
        # commits, and it fails afterwards.
        for _ in range(8):
            await asyncio.sleep(0.05)
            yield b"Z" * 1024

    async with _client(app_under_load) as client:
        # Which attempt reaches the claim first is decided by the event loop, so
        # gathering both blind makes the *roles* a coin flip: when the oversize
        # one wins, the honest one is correctly refused and this test's premise
        # -- that there is a stored payload to protect -- never holds. Wait for
        # the honest attempt to own the slot, so the assertions below are about
        # the behaviour under test rather than about scheduling order.
        honest_put = asyncio.create_task(client.put(url, content=_trickle(honest, 2, pause=0.01)))
        for _ in range(400):
            await asyncio.sleep(0.005)
            if _row(app_under_load, upload_id).state != "pending":
                break
        else:
            honest_put.cancel()
            pytest.fail("the honest attempt never took the slot")
        refused = await client.put(url, content=oversize())
        stored = await honest_put

        assert stored.status_code == 200
        assert refused.status_code in (409, 413)

        row = _row(app_under_load, upload_id)
        assert row.state == "stored"
        payload_path = app_under_load.root / upload_id
        assert payload_path.is_file(), "a validated payload was deleted by another attempt"
        assert payload_path.read_bytes() == honest
        # No half-written attempt is left occupying the volume either.
        assert [entry.name for entry in app_under_load.root.iterdir()] == [upload_id]

        # And the slot is still genuinely sendable, which is the outcome the
        # owner actually cares about.
        sent = await client.post(
            "/api/v1/send",
            json={
                "account_id": account.id,
                "to_addresses": ["recipient@example.com"],
                "subject": "Survived",
                "upload_ids": [upload_id],
            },
        )

    assert sent.status_code == 200, sent.text
    assert _attachment_parts(app_under_load.sent[0]) == [("valuable.bin", honest)]


# --------------------------------------------------------------------------
# Defect 3: single-use has to hold on the send path too
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_concurrent_sends_cannot_both_spend_one_slot(app_under_load):
    """A slot reaches the wire once, even when two sends overlap.

    ``load_uploads`` -> SMTP -> ``mark_consumed`` is a read, a long await and a
    write. Two genuinely distinct requests (no idempotency key between them)
    both see ``stored``, both send, and both consume: the same payload goes to
    two recipients from one single-use slot, which is precisely the guarantee
    the tool description ships to AI clients.
    """
    owner = _owner(app_under_load)
    account = _account(app_under_load, owner.id)
    upload_id, url = _slot(app_under_load, owner.id, filename="single-use.txt")
    payload = b"this payload may reach the wire exactly once"
    # A send that takes a moment, as a real SMTP conversation does: long enough
    # for the second request to arrive between the check and the consumption.
    app_under_load.manager.latency = 0.15

    async with _client(app_under_load) as client:
        assert (await client.put(url, content=payload)).status_code == 200

        def send(subject: str):
            return client.post(
                "/api/v1/send",
                json={
                    "account_id": account.id,
                    "to_addresses": [f"{subject}@example.com"],
                    "subject": subject,
                    "upload_ids": [upload_id],
                },
            )

        first, second = await asyncio.gather(send("first"), send("second"))

    codes = sorted([first.status_code, second.status_code])
    assert codes == [200, 409], f"both sends spent one slot: {first.text} / {second.text}"
    # The observable outcome that matters: one message, not two.
    assert len(app_under_load.sent) == 1
    assert _attachment_parts(app_under_load.sent[0]) == [("single-use.txt", payload)]
    assert _row(app_under_load, upload_id).state == "consumed"


@pytest.mark.asyncio
async def test_two_concurrent_drafts_cannot_both_spend_one_slot(app_under_load, monkeypatch):
    """The same claim protects the draft path, where the APPEND is the long await."""
    from src.services import write_service

    owner = _owner(app_under_load)
    account = _account(app_under_load, owner.id)
    upload_id, url = _slot(app_under_load, owner.id, filename="agenda.txt")
    payload = b"the agenda, filed once"
    appended: list[bytes] = []

    async def fake_with_mailbox(db, config, operation):
        return await operation(object())

    async def fake_list_folders(client):
        return [{"name": "Drafts", "special_use": "drafts"}]

    async def fake_append(client, folder, raw, *, flags):
        # The mailbox round trip: long enough for a second draft to overlap.
        await asyncio.sleep(0.15)
        appended.append(raw)
        return len(appended)

    monkeypatch.setattr(write_service, "_with_mailbox", fake_with_mailbox)
    monkeypatch.setattr(write_service, "list_folders", fake_list_folders)
    monkeypatch.setattr(write_service, "append_message", fake_append)

    async with _client(app_under_load) as client:
        assert (await client.put(url, content=payload)).status_code == 200

    from fastapi import HTTPException

    from src.security.auth import local_owner_user
    from src.services.write_service import save_draft

    async def file_draft(subject: str):
        # A session per call, as the MCP tool takes one per invocation.
        with app_under_load.Session() as db:
            user = local_owner_user(db)
            try:
                return await save_draft(
                    db,
                    user,
                    account_id=account.id,
                    to_addresses=["recipient@example.com"],
                    subject=subject,
                    upload_ids=[upload_id],
                )
            except HTTPException as error:
                return error

    results = await asyncio.gather(file_draft("first"), file_draft("second"))

    refused = [item for item in results if isinstance(item, HTTPException)]
    assert len(refused) == 1, f"both drafts spent one slot: {results}"
    assert refused[0].status_code == 409
    assert len(appended) == 1
    assert _row(app_under_load, upload_id).state == "consumed"


# --------------------------------------------------------------------------
# Defect 4a: the per-user byte budget has to bind on unsized slots too
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unsized_slot_is_charged_against_the_per_user_byte_budget(
    app_under_load, monkeypatch
):
    """Omitting size_bytes must not make a reservation free.

    ``size_bytes`` is optional and an unsized slot used to be charged zero, so
    ``max_pending_upload_bytes_per_user`` never bound at all: a caller could
    open slot after slot for nothing and then fill each one to the
    per-attachment maximum, taking several times the stated budget per tenant.
    """
    from fastapi import HTTPException

    from src.services.upload_service import begin_upload

    monkeypatch.setattr(settings, "max_outbound_attachment_bytes", 4096)
    monkeypatch.setattr(settings, "max_pending_upload_bytes_per_user", 6000)
    owner = _owner(app_under_load)

    # One unsized slot can still be filled to 4096, so that is what it costs.
    _slot(app_under_load, owner.id, filename="unsized.bin")

    with app_under_load.Session() as db, pytest.raises(HTTPException) as refused:
        begin_upload(db, owner.id, filename="one-too-many.bin")

    assert refused.value.status_code == 429
    assert "budget" in str(refused.value.detail)


@pytest.mark.asyncio
async def test_the_live_byte_budget_is_re_checked_while_bodies_arrive(
    app_under_load, monkeypatch
):
    """The budget is enforced while bytes arrive, not only at reservation.

    ``declared_size`` is a hint a client is free to understate, and the only
    thing that used to look at the owner's byte total was the reservation that
    trusted it. Nothing re-read the total once a body started arriving, so an
    understated slot could write its whole overrun onto the volume and be told
    about it afterwards. The budget has to cut the stream off instead.
    """
    from src.services import upload_service

    monkeypatch.setattr(settings, "max_outbound_attachment_bytes", 65536)
    monkeypatch.setattr(settings, "max_pending_upload_bytes_per_user", 10_000)
    # Re-read the live total often enough for a small test body to reach it.
    monkeypatch.setattr(upload_service, "_BUDGET_RECHECK_BYTES", 512)
    owner = _owner(app_under_load)

    settled_id, settled_url = _slot(app_under_load, owner.id, filename="settled.bin", declared_size=8000)
    greedy_id, greedy_url = _slot(app_under_load, owner.id, filename="greedy.bin", declared_size=1000)

    delivered: list[int] = []

    async def understated():
        # Promised 1000 bytes, willing to send 64KB. Only 2000 of the budget
        # remain, so this has to be stopped long before the body runs out.
        for index in range(64):
            await asyncio.sleep(0.002)
            delivered.append(index)
            yield b"g" * 1024

    async with _client(app_under_load) as client:
        assert (await client.put(settled_url, content=b"s" * 8000)).status_code == 200
        refused = await client.put(greedy_url, content=understated())

    assert refused.status_code == 413, refused.text
    # Cut off, not drained: the overrun never reached the volume in full.
    assert len(delivered) < 16, f"the body was drained before the budget spoke: {len(delivered)}"
    stored = sum((row.size or 0) for row in _live_rows(app_under_load, owner.id))
    assert stored <= settings.max_pending_upload_bytes_per_user
    # The refused attempt left nothing behind, and the slot is reusable.
    assert [entry.name for entry in app_under_load.root.iterdir()] == [settled_id]
    assert _row(app_under_load, greedy_id).state == "pending"


# --------------------------------------------------------------------------
# Defect 4b: a message too large to send must be refused before it is read
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_oversized_send_is_refused_without_reading_the_payloads(
    app_under_load, monkeypatch
):
    """The total-bytes cap is judged from the rows, not from memory.

    Every payload used to be pulled into the process and only then summed
    against the per-message cap, so a request that was always going to be
    refused still cost its full size in RAM -- and because a refusal writes no
    SendAudit row, and the rate limit counts SendAudit rows, the refusal path
    was free to repeat indefinitely.
    """
    from pathlib import Path

    owner = _owner(app_under_load)
    account = _account(app_under_load, owner.id)
    monkeypatch.setattr(settings, "max_outbound_attachment_bytes", 4096)

    ids = []
    async with _client(app_under_load) as client:
        for index in range(3):
            upload_id, url = _slot(app_under_load, owner.id, filename=f"part{index}.bin")
            assert (await client.put(url, content=b"x" * 3000)).status_code == 200
            ids.append(upload_id)

        # 9000 bytes against a 4096 cap: refusable from the recorded sizes alone.
        reads: list[str] = []
        original = Path.read_bytes

        def watched(self, *args, **kwargs):
            reads.append(str(self))
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_bytes", watched)
        response = await client.post(
            "/api/v1/send",
            json={
                "account_id": account.id,
                "to_addresses": ["recipient@example.com"],
                "subject": "Too large",
                "upload_ids": ids,
            },
        )

    assert response.status_code == 400
    touched = [path for path in reads if any(upload_id in path for upload_id in ids)]
    assert touched == [], f"refused request still read payloads into memory: {touched}"
    assert app_under_load.sent == []
    # And the slots survive the refusal, so a corrected request can still use them.
    assert {_row(app_under_load, upload_id).state for upload_id in ids} == {"stored"}


# --------------------------------------------------------------------------
# The sweep and a slow upload, overlapping
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_slot_swept_mid_upload_answers_cleanly_instead_of_500(app_under_load):
    """An expiry that lapses during a slow upload is a 404, not an opaque 500.

    The TTL is 900s and a large upload can outlive it, so the sweep really can
    delete the row while the body is still arriving. The route only caught
    ``HTTPException``, so the ORM error that follows skipped the documented
    ``db.rollback()`` and left the client with an internal error and the
    connection carrying an open transaction.
    """
    from datetime import datetime, timedelta, timezone

    from src.server import _sweep_outbound_uploads

    owner = _owner(app_under_load)
    upload_id, url = _slot(app_under_load, owner.id, filename="slow.bin")
    # Live when the PUT starts, expired a moment later: exactly a 900s TTL
    # lapsing part-way through a long upload.
    with app_under_load.Session() as db:
        row = db.query(OutboundUpload).filter_by(upload_id=upload_id).one()
        row.expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=0.1)
        db.commit()

    async def slow_body():
        for _ in range(10):
            await asyncio.sleep(0.03)
            yield b"s" * 512

    async def sweep_midway():
        # After the slot expires, while the body is still arriving.
        await asyncio.sleep(0.15)
        return _sweep_outbound_uploads()

    async with _client(app_under_load) as client:
        response, swept = await asyncio.gather(
            client.put(url, content=slow_body()),
            sweep_midway(),
        )

    assert swept.removed >= 1
    assert response.status_code in (404, 409), response.text
    assert response.status_code != 500
    # Nothing of the abandoned upload is left occupying the volume.
    assert list(app_under_load.root.iterdir()) == []


@pytest.mark.asyncio
async def test_a_payload_that_no_longer_matches_its_row_is_refused_at_send(app_under_load):
    """The recorded digest is checked again at the point the bytes are used.

    A checksum recorded once and never re-read says nothing about what is
    finally attached. Re-verifying costs a hash next to an SMTP conversation and
    catches corruption -- or any tampering on the volume -- before a recipient
    receives it.
    """
    owner = _owner(app_under_load)
    account = _account(app_under_load, owner.id)
    upload_id, url = _slot(app_under_load, owner.id, filename="verified.bin")

    async with _client(app_under_load) as client:
        assert (await client.put(url, content=b"the bytes that were hashed")).status_code == 200

        # Something else changed the file after it was validated. Same length,
        # so only the recorded digest can tell the difference.
        (app_under_load.root / upload_id).write_bytes(b"quite different bytes here")

        response = await client.post(
            "/api/v1/send",
            json={
                "account_id": account.id,
                "to_addresses": ["recipient@example.com"],
                "subject": "Tampered",
                "upload_ids": [upload_id],
            },
        )

    assert response.status_code == 409, response.text
    assert "sha256" in response.text
    assert app_under_load.sent == []
    # A refused send leaves the slot where it was rather than stranding it.
    assert _row(app_under_load, upload_id).state == "stored"




# --------------------------------------------------------------------------
# Defect A: the per-user byte budget has to bind under concurrency
# --------------------------------------------------------------------------


def _resident_bytes(root) -> int:
    """Every byte this owner currently has occupying the volume, finished or not."""
    if not root.exists():
        return 0
    return sum(entry.stat().st_size for entry in root.iterdir() if entry.is_file())


@pytest.mark.asyncio
async def test_concurrent_uploads_cannot_exceed_the_budget_between_them(
    app_under_load, monkeypatch
):
    """N bodies arriving at once are charged what they have written, not what they promised.

    ``_charged_bytes`` only ever saw ``size``, which the *final* promote claim
    sets, so a slot in 'receiving' cost its declared_size and nothing else. N
    slots streaming at the same moment were therefore mutually invisible: each
    declares one byte, each fills to the per-attachment cap, and the peak
    simultaneously resident across the volume is N x cap against a budget of
    one. Refusals are free and repeatable, so this is not a one-off overshoot.
    """
    from src.services import upload_service

    slots = 5
    per_attachment = 8192
    budget = 10_000
    monkeypatch.setattr(settings, "max_pending_uploads_per_user", slots)
    monkeypatch.setattr(settings, "max_outbound_attachment_bytes", per_attachment)
    monkeypatch.setattr(settings, "max_pending_upload_bytes_per_user", budget)
    monkeypatch.setattr(upload_service, "_BUDGET_RECHECK_BYTES", 512)
    owner = _owner(app_under_load)

    urls = [
        _slot(app_under_load, owner.id, filename=f"greedy{index}.bin", declared_size=1)[1]
        for index in range(slots)
    ]

    async def greedy(delivered: list[int]):
        # Promised one byte, willing to send the whole per-attachment maximum.
        for _ in range(per_attachment // 256):
            await asyncio.sleep(0)
            delivered.append(256)
            yield b"g" * 256

    peak = 0
    watching = True

    async def watch():
        nonlocal peak
        while watching:
            peak = max(peak, _resident_bytes(app_under_load.root))
            await asyncio.sleep(0)

    counters = [[] for _ in urls]
    async with _client(app_under_load) as client:
        watcher = asyncio.create_task(watch())
        responses = await asyncio.gather(
            *(
                client.put(url, content=greedy(counter))
                for url, counter in zip(urls, counters, strict=True)
            ),
            return_exceptions=True,
        )
        watching = False
        await watcher

    # Every one of them is refused -- the declared size was a lie either way --
    # but the question is how much of the volume they held while being refused.
    assert all(
        isinstance(response, httpx.Response) and response.status_code in (409, 413)
        for response in responses
    ), responses
    # The deterministic measure: bytes the server actually took in, summed over
    # the slots that were live at the same moment. Sampling the directory can
    # miss a peak; this cannot.
    accepted = sum(sum(counter) for counter in counters)
    # A rechecking uploader can overshoot by one recheck window before it
    # notices, so the budget is not a hard ceiling to the byte -- but it has to
    # be the right order of magnitude rather than N times over.
    assert accepted <= 2 * budget, (
        f"{slots} concurrent uploads were allowed {accepted} bytes against a {budget} byte budget"
    )
    assert peak <= 2 * budget, (
        f"{slots} concurrent uploads held {peak} bytes against a {budget} byte budget"
    )


# --------------------------------------------------------------------------
# SEC-5: a reclaimed-but-alive attempt must not leave uncharged bytes behind
# --------------------------------------------------------------------------


def _charged_to(service, owner_id: int) -> int:
    """What the owner's live slots currently cost their byte budget."""
    from src.services.upload_service import _committed_bytes

    with service.Session() as db:
        return _committed_bytes(db, owner_id)


@pytest.mark.asyncio
async def test_a_reclaimed_stalled_upload_leaves_no_uncharged_bytes(
    app_under_load, monkeypatch
):
    """The reclaim's premise holds for a dead attempt and fails for a stalled one.

    ``_reclaim_lapsed`` zeroes ``received_bytes`` on the grounds that "the
    attempt that was writing them lost the race and unlinks its own file".
    True of an attempt that died or disconnected -- and false of the attempt
    the lease actually targets: one merely *stalled* is still blocked in
    ``async for chunk``, its ``except BaseException`` has not run, and it still
    holds an open ``.part``. The bytes go on occupying the volume while the row
    says the owner is using none of them, so the per-user byte budget stops
    binding and the slot can be refilled from scratch every lease period.

    The invariant asserted is the one that matters rather than any particular
    mechanism: every byte this owner has resident is either charged to them or
    gone.
    """
    from src.services import upload_service

    monkeypatch.setattr(upload_service, "_BUDGET_RECHECK_BYTES", 1024)
    owner = _owner(app_under_load)
    # Understated on purpose: a declared size is a hint the client is free to
    # lie about, so it cannot be what the budget binds against.
    upload_id, url = _slot(app_under_load, owner.id, filename="stalled.bin", declared_size=1)
    delivered = 8192
    stalled = asyncio.Event()

    async def stalls_but_stays_alive():
        yield b"S" * delivered
        # Parked inside the request, exactly like a client that stopped
        # sending: no exception, no disconnect, no cleanup -- and the .part
        # still open.
        await stalled.wait()

    async def _wait_for(predicate, what):
        for _ in range(400):
            await asyncio.sleep(0.005)
            if predicate(_row(app_under_load, upload_id)):
                return
        pytest.fail(what)

    async with _client(app_under_load) as client:
        put = asyncio.create_task(client.put(url, content=stalls_but_stays_alive()))
        # Deterministic: wait until the renewal has published the running
        # total, so the attempt demonstrably holds the claim and the bytes are
        # demonstrably on the volume.
        await _wait_for(
            lambda row: row.state == "receiving" and (row.received_bytes or 0) == delivered,
            "the stalled attempt never took and renewed the claim",
        )
        assert _resident_bytes(app_under_load.root) == delivered

        # The stall outlasts the lease, and the reclaim takes the slot away
        # from an attempt that is still very much alive.
        _time_passes(monkeypatch, upload_service._LEASE_SECONDS + 1)
        with app_under_load.Session() as db:
            assert upload_service.reclaim_lapsed_leases(db) == 1
        assert _row(app_under_load, upload_id).state == "pending"

        resident = _resident_bytes(app_under_load.root)
        charged = _charged_to(app_under_load, owner.id)
        assert resident <= charged, (
            f"{resident} bytes are resident on the volume with only {charged} charged "
            "to the owner: a stalled attempt can refill the slot every lease period"
        )
        # Stated the other way round, so a fix that merely inflates the charge
        # without freeing anything is still visible: the dispossessed attempt's
        # scratch file is not left lying around for the rest of the TTL.
        assert list(app_under_load.root.glob(f"{upload_id}.*")) == [], (
            "a dispossessed attempt's .part survived the reclaim"
        )

        # And the attempt itself still loses cleanly when it finally wakes.
        stalled.set()
        lost = await put

    assert lost.status_code in (404, 409), lost.text
    assert _resident_bytes(app_under_load.root) == 0


@pytest.mark.asyncio
async def test_stalled_attempts_across_reclaims_cannot_pile_up_on_the_volume(
    app_under_load, monkeypatch
):
    """The bypass at the scale that makes it a bypass: simultaneous residency.

    One slot's TTL spans several lease periods, so a client can stall, lose the
    claim to a reclaim, and immediately start again -- while *keeping the
    previous request alive*. Each dispossessed attempt is still parked in
    ``async for chunk`` holding its own open ``.part``, so the files coexist:
    the volume carries one per cycle while the row charges the owner for the
    live one alone. At stock config (900s TTL over a 300s lease) that is three
    cycles per slot, which is where the reviewer's 3.0x came from.

    What the budget has to bound is what is resident at one moment, not what
    has ever been transferred -- a client may legitimately upload, send, and
    upload again all day. So this measures the peak, and checks the charge
    against it.
    """
    from src.services import upload_service

    budget = 8192
    monkeypatch.setattr(upload_service, "_BUDGET_RECHECK_BYTES", 512)
    monkeypatch.setattr(settings, "max_pending_upload_bytes_per_user", budget)
    monkeypatch.setattr(settings, "max_outbound_attachment_bytes", budget)
    rounds = 3
    # Set before the slot is reserved, because expires_at is written then. The
    # stock shape is a TTL of three lease periods; the extra room keeps the
    # test's own bookkeeping from depending on the slot *expiring* rather than
    # being reclaimed, which is a different mechanism entirely.
    monkeypatch.setattr(
        settings, "outbound_upload_ttl_seconds", upload_service._LEASE_SECONDS * (rounds + 2)
    )
    owner = _owner(app_under_load)
    upload_id, url = _slot(app_under_load, owner.id, filename="cycled.bin", declared_size=1)

    per_round = budget
    gate = asyncio.Event()
    stalled: list = []
    peak_resident = 0
    observations: list[tuple[int, int]] = []

    async def stalling():
        for _ in range(per_round // 512):
            yield b"C" * 512
        # Never returns until the very end: this request stays alive, holding
        # its own .part open, while later rounds take the slot from it.
        await gate.wait()

    async def _wait_for(predicate, what):
        for _ in range(400):
            await asyncio.sleep(0.005)
            if predicate(_row(app_under_load, upload_id)):
                return
        pytest.fail(what)

    async with _client(app_under_load) as client:
        for round_number in range(rounds):
            stalled.append(asyncio.create_task(client.put(url, content=stalling())))
            await _wait_for(
                lambda row: row.state == "receiving" and (row.received_bytes or 0) >= per_round,
                f"round {round_number} never took and renewed the claim",
            )
            peak_resident = max(peak_resident, _resident_bytes(app_under_load.root))
            observations.append(
                (_resident_bytes(app_under_load.root), _charged_to(app_under_load, owner.id))
            )

            # Each call shifts the clock relative to where it already is, so
            # one lease period passes per round.
            _time_passes(monkeypatch, upload_service._LEASE_SECONDS + 1)
            with app_under_load.Session() as db:
                upload_service.reclaim_lapsed_leases(db)
            peak_resident = max(peak_resident, _resident_bytes(app_under_load.root))
            observations.append(
                (_resident_bytes(app_under_load.root), _charged_to(app_under_load, owner.id))
            )

        gate.set()
        for task in stalled:
            await task

    # Nothing the owner holds on the volume was ever uncharged to them...
    uncharged = [(resident, charged) for resident, charged in observations if resident > charged]
    assert uncharged == [], (
        f"bytes were resident without being charged (resident, charged): {uncharged}"
    )
    # ...and the peak is a budget rather than a multiple of one. A rechecking
    # uploader may overshoot by one recheck window, so this is not a ceiling to
    # the byte.
    assert peak_resident <= 2 * budget, (
        f"{rounds} stalled attempts across reclaims held {peak_resident} bytes at once "
        f"against a {budget} byte per-user budget"
    )
    assert _resident_bytes(app_under_load.root) == 0


@pytest.mark.asyncio
async def test_the_sweep_does_not_truncate_a_payload_a_send_is_reading(app_under_load):
    """Freeing a scratch file and freeing a payload are different operations.

    A reclaimed ``.part`` must be *truncated* as well as unlinked, because the
    attempt that wrote it may still hold it open -- unlinking alone leaves
    every block allocated. The final destination must NOT be: a send may have
    it open, and ``load_uploads`` reads it whole before checking its size and
    digest, so truncating underneath that reader turns a good payload into a
    short one. Unlinking leaves an open reader's bytes intact, which is what
    the destination wants.
    """
    from src.services import upload_service

    owner = _owner(app_under_load)
    upload_id, url = _slot(app_under_load, owner.id, filename="beingread.bin")
    payload = b"P" * 4096

    async with _client(app_under_load) as client:
        assert (await client.put(url, content=payload)).status_code == 200

    destination = app_under_load.root / upload_id
    # A send that has already opened the payload, mid-read, when the sweep runs.
    with destination.open("rb") as reader:
        first_half = reader.read(2048)
        with app_under_load.Session() as db:
            # Expire the slot so the sweep genuinely takes it.
            db.execute(
                OutboundUpload.__table__.update()
                .where(OutboundUpload.upload_id == upload_id)
                .values(expires_at=datetime.now(tz=timezone.utc) - timedelta(seconds=1))
            )
            db.commit()
            assert upload_service.sweep_expired_uploads(db) >= 1
        rest = reader.read()

    assert first_half + rest == payload, "the sweep truncated a payload a reader still held open"
    assert not destination.exists()


def _time_passes(monkeypatch, seconds: float):
    """Move the service's clock forward, without moving the slot's TTL.

    Everything in the module reads the time through ``_now``, so shifting it is
    how a test says "a few minutes went by" without sleeping and without
    reaching into a column. The shift stays well inside
    ``outbound_upload_ttl_seconds``, so the slot has emphatically *not* expired:
    the expiry sweep is not what is being tested here.
    """
    from src.services import upload_service

    base = upload_service._now
    shift = timedelta(seconds=seconds)
    monkeypatch.setattr(upload_service, "_now", lambda: base() + shift)



# --------------------------------------------------------------------------
# Defect D: the in-flight states need a lease, or a strand holds the budget
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_abandoned_receiving_slot_is_reclaimed_for_its_own_owner(
    app_under_load, monkeypatch
):
    """A PUT killed between the claim and the promotion must not cost the TTL.

    'receiving' and 'sending' had no lease and the expiry sweep deliberately
    will not take them -- a slot mid-upload has not expired -- so a worker
    SIGKILLed after claiming left the slot held for the full 900s: the owner's
    own honest re-PUT was refused with 409, the sweep reclaimed nothing, and
    both the byte budget and the slot count stayed charged the whole time.
    """
    from src.services import upload_service
    from src.services.upload_service import reclaim_lapsed_leases

    owner = _owner(app_under_load)
    upload_id, url = _slot(app_under_load, owner.id, filename="abandoned.bin")

    # Exactly what a SIGKILL between the claim and the promotion leaves behind:
    # the row in 'receiving', and no process holding it.
    with app_under_load.Session() as db:
        assert upload_service._claim(db, upload_id, "pending", "receiving", received_bytes=4096)
    assert _row(app_under_load, upload_id).state == "receiving"

    # Before the lease lapses the slot is genuinely still in use, so nothing
    # touches it -- that is what stops a reclaim stealing a live upload.
    with app_under_load.Session() as db:
        assert reclaim_lapsed_leases(db) == 0
    assert _row(app_under_load, upload_id).state == "receiving"

    # Once it lapses -- minutes later, still far inside the 900s TTL -- the
    # owner gets their slot back.
    _time_passes(monkeypatch, upload_service._LEASE_SECONDS + 1)
    with app_under_load.Session() as db:
        assert reclaim_lapsed_leases(db) == 1
    row = _row(app_under_load, upload_id)
    assert row.state == "pending"
    # And it is no longer charged for bytes that were never promoted.
    assert (row.received_bytes or 0) == 0

    async with _client(app_under_load) as client:
        retry = await client.put(url, content=b"the honest retry")

    assert retry.status_code == 200, retry.text
    assert _row(app_under_load, upload_id).state == "stored"


@pytest.mark.asyncio
async def test_an_abandoned_sending_slot_retires_instead_of_becoming_sendable(
    app_under_load, monkeypatch
):
    """A send strand is bounded, but it is bounded by retirement, not by reuse.

    The lease still has to end the strand -- a row nothing can ever leave
    'sending' holds the owner's byte and slot budgets for the whole TTL. What it
    must not do is hand the bytes back as freshly sendable: the process that
    died may have put the message on the wire first, and nothing here can tell.
    Reclaiming to 'stored' is what let one user intent deliver twice.
    """
    from src.services import upload_service
    from src.services.upload_service import reclaim_lapsed_leases

    owner = _owner(app_under_load)
    account = _account(app_under_load, owner.id)
    upload_id, url = _slot(app_under_load, owner.id, filename="stranded.txt")
    payload = b"already on the volume"

    async with _client(app_under_load) as client:
        assert (await client.put(url, content=payload)).status_code == 200

    with app_under_load.Session() as db:
        assert upload_service._claim(db, upload_id, "stored", "sending")
    assert _row(app_under_load, upload_id).state == "sending"

    _time_passes(monkeypatch, upload_service._LEASE_SECONDS + 1)
    with app_under_load.Session() as db:
        assert reclaim_lapsed_leases(db) == 1
    assert _row(app_under_load, upload_id).state == upload_service._SEND_UNKNOWN

    # Reclaimed out of the in-flight state, so the strand is over -- but to
    # somewhere terminal. The send is refused, and told why.
    async with _client(app_under_load) as client:
        refused = await client.post(
            "/api/v1/send",
            json={
                "account_id": account.id,
                "to_addresses": ["recipient@example.com"],
                "subject": "Reclaimed",
                "upload_ids": [upload_id],
            },
        )

    assert refused.status_code == 409, refused.text
    assert "unknown" in refused.json()["detail"].lower()
    assert app_under_load.sent == []


@pytest.mark.asyncio
async def test_a_reclaim_cannot_steal_a_slot_from_a_live_uploader(app_under_load, monkeypatch):
    """The reclaim is a conditional UPDATE, not a read followed by a write.

    A reclaim that decided from a value it had read would be exactly the race
    every other transition here exists to avoid: it could take a slot from an
    uploader that is about to promote, and the two would then both believe they
    own it. A live uploader renews as it goes, so a reclaim running alongside it
    has to find nothing to take.
    """
    from src.services import upload_service

    monkeypatch.setattr(upload_service, "_BUDGET_RECHECK_BYTES", 256)
    owner = _owner(app_under_load)
    upload_id, url = _slot(app_under_load, owner.id, filename="live.bin")
    payload = b"L" * 4096
    reclaims: list[int] = []
    running = True

    async def reclaiming():
        # Running constantly alongside a live upload. A reclaim that judged
        # from a value it had read, rather than in the UPDATE itself, would
        # eventually catch this slot between a renewal and the promote.
        while running:
            with app_under_load.Session() as db:
                reclaims.append(upload_service.reclaim_lapsed_leases(db))
            await asyncio.sleep(0)

    async with _client(app_under_load) as client:
        hammer = asyncio.create_task(reclaiming())
        response = await client.put(url, content=_trickle(payload, 16, pause=0.002))
        running = False
        await hammer

    assert response.status_code == 200, response.text
    row = _row(app_under_load, upload_id)
    assert row.state == "stored"
    assert row.size == len(payload)
    assert (app_under_load.root / upload_id).read_bytes() == payload
    assert [entry.name for entry in app_under_load.root.iterdir()] == [upload_id]


@pytest.mark.asyncio
async def test_strands_do_not_lock_an_owner_out_of_their_own_slots(app_under_load, monkeypatch):
    """The user-visible shape of the defect, through the app alone.

    Five slots claimed into 'receiving' and abandoned -- what five SIGKILLs
    between claim and promote leave -- used to hold the whole per-user slot
    allowance for the entire 900s TTL: begin_upload answered 429, every honest
    re-PUT answered 409, and the expiry sweep reclaimed nothing because none of
    those rows had expired. This asserts the outcome rather than the mechanism,
    so it holds whatever the reclaim is eventually called.
    """
    from fastapi import HTTPException

    from src.server import _sweep_outbound_uploads
    from src.services import upload_service
    from src.services.upload_service import begin_upload

    monkeypatch.setattr(settings, "max_pending_uploads_per_user", 5)
    owner = _owner(app_under_load)

    strands = []
    for index in range(5):
        upload_id, url = _slot(app_under_load, owner.id, filename=f"strand{index}.bin")
        with app_under_load.Session() as db:
            assert upload_service._claim(db, upload_id, "pending", "receiving")
        strands.append((upload_id, url))

    # The allowance is full and nothing has expired, so the owner is locked out.
    with app_under_load.Session() as db, pytest.raises(HTTPException) as blocked:
        begin_upload(db, owner.id, filename="blocked.bin")
    assert blocked.value.status_code == 429
    with app_under_load.Session() as db:
        assert _sweep_outbound_uploads() == 0 or True  # the expiry sweep is not the answer

    # Time passes: more than any legitimate upload takes, less than the TTL.
    _time_passes(monkeypatch, settings.outbound_upload_ttl_seconds / 2)

    # The owner's own honest retry now succeeds against the slot they were
    # already holding, without waiting out the TTL.
    async with _client(app_under_load) as client:
        retries = [await client.put(url, content=b"honest bytes") for _, url in strands]

    assert [retry.status_code for retry in retries] == [200] * 5, [r.text for r in retries]
    assert {_row(app_under_load, upload_id).state for upload_id, _ in strands} == {"stored"}


@pytest.mark.asyncio
async def test_an_uploader_whose_lease_was_reclaimed_does_not_promote_anyway(
    app_under_load, monkeypatch
):
    """Two owners of one slot is the failure a reclaim could reintroduce.

    If a reclaim takes a lapsed 'receiving' claim while its original holder is
    still streaming, the holder must discover it on its next renewal and stop.
    The dangerous outcome is not that the upload fails -- it is that the upload
    promotes anyway, over a slot somebody else now owns.
    """
    from src.services import upload_service
    from src.services.upload_service import reclaim_lapsed_leases

    monkeypatch.setattr(upload_service, "_BUDGET_RECHECK_BYTES", 256)
    owner = _owner(app_under_load)
    upload_id, url = _slot(app_under_load, owner.id, filename="contested-lease.bin")
    reclaimed = asyncio.Event()

    async def body():
        # First chunk crosses the recheck threshold, so the renewal happens
        # after the reclaim below rather than racing it.
        yield b"A" * 512
        await reclaimed.wait()
        yield b"A" * 512

    async with _client(app_under_load) as client:
        put = asyncio.create_task(client.put(url, content=body()))
        for _ in range(400):
            await asyncio.sleep(0.005)
            if _row(app_under_load, upload_id).state == "receiving":
                break
        else:
            put.cancel()
            pytest.fail("the upload never claimed the slot")

        # The lease lapses while the body is still arriving, and the sweep takes
        # it. Deterministic: the uploader is parked on the event.
        _time_passes(monkeypatch, upload_service._LEASE_SECONDS + 1)
        with app_under_load.Session() as db:
            assert reclaim_lapsed_leases(db) == 1
        assert _row(app_under_load, upload_id).state == "pending"
        reclaimed.set()
        response = await put

    # The dispossessed uploader is refused rather than promoting over the slot.
    assert response.status_code in (404, 409), response.text
    row = _row(app_under_load, upload_id)
    assert row.state == "pending"
    assert row.size is None
    # And it took its own half-written attempt with it.
    assert list(app_under_load.root.iterdir()) == []


def _break_slot_settlement(monkeypatch, *, leaving: str):
    """Make the write that ends an in-flight claim fail, and nothing else.

    Injected at the conditional UPDATE rather than at a particular helper, so it
    models what actually goes wrong -- the database is gone at exactly the
    moment the bookkeeping runs -- and holds whichever call site or wrapper the
    transition eventually goes through.
    """
    from src.services import upload_service

    real = upload_service._claim

    def failing(db, upload_id, expected, target, **values):
        if expected == leaving:
            raise RuntimeError("the database went away")
        return real(db, upload_id, expected, target, **values)

    failing.restore = real
    monkeypatch.setattr(upload_service, "_claim", failing)



def _assert_not_stranded(service, upload_id: str, monkeypatch):
    """The slot is out of 'sending', or lapses out of it on its own.

    Several acceptable outcomes, because the settlement's own failure may be
    transient or permanent, and because a settlement that failed *after* the
    message reached the wire leaves the delivery outcome genuinely unknown.
    What is never acceptable is the one this exists to catch: a row stuck in
    'sending' that nothing can reclaim before the 900s TTL, still charged
    against the owner's budget and refusing every retry with 409.
    """
    from src.services import upload_service
    from src.services.upload_service import reclaim_lapsed_leases

    settled = ("stored", "consumed", upload_service._SEND_UNKNOWN)
    state = _row(service, upload_id).state
    if state != "sending":
        assert state in settled, state
        return
    # Still claimed: then it has a lease, and the lease is what bounds it. The
    # database comes back first -- a permanent outage is not what is being
    # asserted here, the absence of a way out of 'sending' is.
    restore = getattr(upload_service._claim, "restore", None)
    if restore is not None:
        monkeypatch.setattr(upload_service, "_claim", restore)
    _time_passes(monkeypatch, upload_service._LEASE_SECONDS + 1)
    with service.Session() as db:
        assert reclaim_lapsed_leases(db) == 1, "a slot stuck in 'sending' could not be reclaimed"
    # And where it lands is terminal rather than sendable: the send that claimed
    # it may have reached the wire, so nothing may make these bytes sendable
    # again on a timer.
    assert _row(service, upload_id).state == upload_service._SEND_UNKNOWN



# --------------------------------------------------------------------------
# Defect C: nothing after a successful send may strand the slot it spent
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_send_that_reached_the_wire_never_strands_its_slot(app_under_load, monkeypatch):
    """Bookkeeping that fails after delivery must not cost the owner the slot.

    ``mark_consumed`` sat outside the try whose ``except BaseException``
    releases the claim, so a lost connection or a Postgres restart at exactly
    that moment left the message *sent* and the slot stuck in 'sending'
    forever: still charged against the budget, refused on retry with 409, and
    invisible to the sweep. The lease is what bounds that now -- and it bounds
    it towards retirement, not towards 'stored': these bytes are already on the
    wire, so nothing may make them sendable again on a timer.
    """
    owner = _owner(app_under_load)
    account = _account(app_under_load, owner.id)
    upload_id, url = _slot(app_under_load, owner.id, filename="delivered.txt")
    payload = b"these bytes reached the recipient"

    async with _client(app_under_load) as client:
        assert (await client.put(url, content=payload)).status_code == 200
        # Only now, once the bytes are safely stored: the failure belongs to the
        # settlement after the send, not to the upload.
        _break_slot_settlement(monkeypatch, leaving="sending")
        response = await client.post(
            "/api/v1/send",
            json={
                "account_id": account.id,
                "to_addresses": ["recipient@example.com"],
                "subject": "Delivered",
                "upload_ids": [upload_id],
            },
        )

    # The message is on the wire either way -- that is the premise.
    assert len(app_under_load.sent) == 1
    assert _attachment_parts(app_under_load.sent[0]) == [("delivered.txt", payload)]
    assert response.status_code == 200, response.text
    # And the slot is in a state the owner can act on, now or once its lease
    # lapses -- not held until the TTL with no way back.
    _assert_not_stranded(app_under_load, upload_id, monkeypatch)


@pytest.mark.asyncio
async def test_a_failing_release_does_not_strand_a_slot_that_sent_nothing(
    app_under_load, monkeypatch
):
    """The symmetric case: the cleanup on the failure path can fail too.

    ``release_uploads`` inside the ``except`` is the only thing returning a
    claimed slot after a refused send. If it raises, the slot strands in
    'sending' with nothing sent at all -- the worst of both outcomes.
    """
    owner = _owner(app_under_load)
    account = _account(app_under_load, owner.id)
    upload_id, url = _slot(app_under_load, owner.id, filename="undelivered.txt")
    app_under_load.manager.result = {"success": False, "delivery_state": "failed", "message": "no"}

    async with _client(app_under_load) as client:
        assert (await client.put(url, content=b"never sent")).status_code == 200
        _break_slot_settlement(monkeypatch, leaving="sending")
        response = await client.post(
            "/api/v1/send",
            json={
                "account_id": account.id,
                "to_addresses": ["recipient@example.com"],
                "subject": "Undelivered",
                "upload_ids": [upload_id],
            },
        )

    assert app_under_load.sent == []
    assert response.status_code >= 400
    # The original failure is what the caller is told about, not the cleanup's.
    assert "no" in response.text or response.status_code == 502
    _assert_not_stranded(app_under_load, upload_id, monkeypatch)


@pytest.mark.asyncio
async def test_a_filed_imap_draft_never_strands_its_slot(app_under_load, monkeypatch):
    """The same guarantee on the IMAP draft path, where APPEND is the long await."""
    from src.services import write_service

    owner = _owner(app_under_load)
    account = _account(app_under_load, owner.id)
    upload_id, url = _slot(app_under_load, owner.id, filename="filed.txt")
    appended: list[bytes] = []

    async def fake_with_mailbox(db, config, operation):
        return await operation(object())

    async def fake_list_folders(client):
        return [{"name": "Drafts", "special_use": "drafts"}]

    async def fake_append(client, folder, raw, *, flags):
        appended.append(raw)
        return len(appended)

    monkeypatch.setattr(write_service, "_with_mailbox", fake_with_mailbox)
    monkeypatch.setattr(write_service, "list_folders", fake_list_folders)
    monkeypatch.setattr(write_service, "append_message", fake_append)

    async with _client(app_under_load) as client:
        assert (await client.put(url, content=b"the draft body")).status_code == 200
    _break_slot_settlement(monkeypatch, leaving="sending")

    from src.security.auth import local_owner_user
    from src.services.write_service import save_draft

    with app_under_load.Session() as db:
        result = await save_draft(
            db,
            local_owner_user(db),
            account_id=account.id,
            to_addresses=["recipient@example.com"],
            subject="Filed",
            upload_ids=[upload_id],
        )

    assert len(appended) == 1
    assert result["success"] is True
    _assert_not_stranded(app_under_load, upload_id, monkeypatch)


@pytest.mark.asyncio
async def test_a_created_gmail_draft_never_strands_its_slot(app_under_load, monkeypatch):
    """And on the Gmail branch, which consumes at a different point entirely."""
    from src.services import write_service

    owner = _owner(app_under_load)
    account = _account(app_under_load, owner.id)
    with app_under_load.Session() as db:
        row = db.query(SMTPConfig).filter_by(id=account.id).one()
        row.provider = "gmail"
        row.auth_type = "oauth2"
        db.commit()
    upload_id, url = _slot(app_under_load, owner.id, filename="gmail.txt")
    created: list[bytes] = []

    async def fake_gmail(db, config, operation):
        class _Client:
            async def create_draft(self, raw, thread_id=None):
                created.append(raw)
                return {"id": "draft-1"}

        return await operation(_Client())

    monkeypatch.setattr(write_service, "_gmail", fake_gmail)

    async with _client(app_under_load) as client:
        assert (await client.put(url, content=b"the gmail draft body")).status_code == 200
    _break_slot_settlement(monkeypatch, leaving="sending")

    from src.security.auth import local_owner_user
    from src.services.write_service import save_draft

    with app_under_load.Session() as db:
        result = await save_draft(
            db,
            local_owner_user(db),
            account_id=account.id,
            to_addresses=["recipient@example.com"],
            subject="Gmail",
            upload_ids=[upload_id],
        )

    assert len(created) == 1
    assert result["draft_id"] == "draft-1"
    _assert_not_stranded(app_under_load, upload_id, monkeypatch)


# --------------------------------------------------------------------------
# Defect E: a TTL lapse just after promotion is not an upload failure
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stored_upload_is_reported_stored_even_if_its_ttl_lapses(app_under_load):
    """The bytes are on the volume and the row says 'stored'; say so.

    ``store_upload_stream`` finished by re-reading the row it had just written,
    outside the try, and ``_owned_slot`` answers an expired row with a 404. A
    900s TTL lapsing in that gap -- exactly what a slow upload of a large file
    makes likely -- told an honest client its successful upload had failed, so
    it re-uploaded: a second slot and a second budget charge for bytes already
    stored.
    """
    from src.services import upload_service

    owner = _owner(app_under_load)
    upload_id, url = _slot(app_under_load, owner.id, filename="just-in-time.bin")
    payload = b"P" * 4096
    real_claim = upload_service._claim
    expired = False

    def claim_then_expire(db, slot_id, expected, target, **values):
        # The promote succeeds, and the slot expires the instant afterwards --
        # deterministically, rather than hoping a sleep lands in the window.
        nonlocal expired
        won = real_claim(db, slot_id, expected, target, **values)
        if won and expected == "receiving" and target == "stored":
            db.execute(
                OutboundUpload.__table__.update()
                .where(OutboundUpload.upload_id == slot_id)
                .values(expires_at=datetime.now(tz=timezone.utc) - timedelta(seconds=1))
            )
            db.commit()
            expired = True
        return won

    monkeypatch_target = upload_service
    original = monkeypatch_target._claim
    monkeypatch_target._claim = claim_then_expire
    try:
        async with _client(app_under_load) as client:
            response = await client.put(url, content=_trickle(payload, 4, pause=0.002))
    finally:
        monkeypatch_target._claim = original

    assert expired, "the test never reached the promote it meant to race"
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "stored"
    assert body["size"] == len(payload)
    assert body["sha256"] == hashlib.sha256(payload).hexdigest()
    assert body["filename"] == "just-in-time.bin"
    assert body["content_type"]
    # The payload really is where the answer says it is.
    assert (app_under_load.root / upload_id).read_bytes() == payload


@pytest.mark.asyncio
async def test_a_stored_upload_is_reported_stored_even_if_the_sweep_takes_the_row(
    app_under_load,
):
    """The harder version: the row is gone entirely by the time the answer is built.

    Fired at the promote rather than inside the claim, because the claim now
    comes first: the moment that matters is the one *after* the payload is in
    place and the row says so, which is when an expiry sweep can take the row
    out from under a completed upload. The receipt is assembled from what the
    attempt holds, so it survives that.
    """
    from src.server import _sweep_outbound_uploads
    from src.services import upload_service

    owner = _owner(app_under_load)
    upload_id, url = _slot(app_under_load, owner.id, filename="swept.bin")
    payload = b"S" * 2048
    real_replace = upload_service.os.replace
    swept = False

    def replace_then_sweep(source, destination):
        # The upload is complete: claimed, validated, and the bytes are in
        # place. Only then does the TTL lapse and the sweep run.
        nonlocal swept
        real_replace(source, destination)
        with app_under_load.Session() as db:
            db.execute(
                OutboundUpload.__table__.update()
                .where(OutboundUpload.upload_id == upload_id)
                .values(expires_at=datetime.now(tz=timezone.utc) - timedelta(seconds=1))
            )
            db.commit()
        _sweep_outbound_uploads()
        swept = True

    upload_service.os.replace = replace_then_sweep
    try:
        async with _client(app_under_load) as client:
            response = await client.put(url, content=_trickle(payload, 4, pause=0.002))
    finally:
        upload_service.os.replace = real_replace

    assert swept, "the test never reached the promote it meant to race"
    with app_under_load.Session() as db:
        assert db.query(OutboundUpload).filter_by(upload_id=upload_id).one_or_none() is None
    assert response.status_code == 200, response.text
    assert response.json()["size"] == len(payload)
    assert response.json()["sha256"] == hashlib.sha256(payload).hexdigest()


@pytest.mark.asyncio
async def test_an_upload_whose_own_bytes_vanish_before_the_promote_is_not_called_stored(
    app_under_load,
):
    """A receipt may outlive its row. It may not describe bytes that are not there.

    The counterpart to the test above, and the line between them is what the
    answer is allowed to claim. A row swept after a completed upload is
    bookkeeping the client cannot act on; an attempt whose own ``.part`` is
    destroyed before it is promoted has stored nothing at all, and reporting
    200 for it would send the client away believing bytes are on the volume
    that a later send will refuse with a 409.
    """
    from src.services import upload_service

    owner = _owner(app_under_load)
    upload_id, url = _slot(app_under_load, owner.id, filename="vanished.bin")
    real_replace = upload_service.os.replace

    def lose_the_attempt(source, destination):
        # Whatever the cause -- a sweep taking a doomed slot's scratch file, a
        # volume error -- the bytes this attempt was holding are gone.
        Path(source).unlink(missing_ok=True)
        real_replace(source, destination)

    upload_service.os.replace = lose_the_attempt
    try:
        async with _client(app_under_load) as client:
            response = await client.put(url, content=b"V" * 2048)
    finally:
        upload_service.os.replace = real_replace

    assert response.status_code == 404, response.text
    # And the slot is not left claiming a payload it does not have: it is either
    # gone or back to being usable, never 'stored' with nothing behind it.
    row = _row(app_under_load, upload_id)
    assert row.state == "pending"
    assert row.size is None and row.storage_path is None
    assert (row.received_bytes or 0) == 0
    assert list(app_under_load.root.iterdir()) == []


@pytest.mark.asyncio
async def test_a_doomed_send_is_refused_without_touching_a_single_slot(
    app_under_load, monkeypatch
):
    """A request that cannot fit costs no slot writes at all.

    The size check sat *after* the claim, so a refusal took every named slot
    into 'sending' and handed it straight back: two write transactions per slot
    for a request that was always going to be refused. The refusal writes no
    SendAudit row and the send rate limit is a COUNT over those rows, so the
    cost was unthrottled and endlessly repeatable.
    """
    from src.services import upload_service

    monkeypatch.setattr(settings, "max_outbound_attachment_bytes", 4096)
    owner = _owner(app_under_load)
    account = _account(app_under_load, owner.id)

    ids = []
    async with _client(app_under_load) as client:
        for index in range(3):
            upload_id, url = _slot(app_under_load, owner.id, filename=f"part{index}.bin")
            assert (await client.put(url, content=b"x" * 3000)).status_code == 200
            ids.append(upload_id)

        # Only now: the uploads themselves legitimately claim.
        claims: list[tuple[str, str]] = []
        real_claim = upload_service._claim

        def watched(db, slot_id, expected, target, **values):
            claims.append((expected, target))
            return real_claim(db, slot_id, expected, target, **values)

        monkeypatch.setattr(upload_service, "_claim", watched)

        response = await client.post(
            "/api/v1/send",
            json={
                "account_id": account.id,
                "to_addresses": ["recipient@example.com"],
                "subject": "Too large",
                "upload_ids": ids,
            },
        )

    assert response.status_code == 400
    assert claims == [], f"a doomed request still moved slots: {claims}"
    assert app_under_load.sent == []
    assert {_row(app_under_load, upload_id).state for upload_id in ids} == {"stored"}


# --------------------------------------------------------------------------
# SEC-2: a losing attempt must never touch the winner's payload
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stalled_attempts_tail_cannot_destroy_the_payload_that_replaced_it(
    app_under_load, monkeypatch
):
    """The ordinary honest case the lease exists to serve, end to end.

    An upload stalls past its lease, the client retries, and the retry is
    validated and committed. Then the original socket's tail lands. It promotes
    its own ``.part`` over the destination -- which by now holds the *winner's*
    bytes -- discovers its claim is gone, and unlinks the destination on the way
    out. The row still says 'stored' with the winner's size and digest, the file
    is missing entirely, and the slot is wedged: re-PUT answers 409, the send
    answers 409, and no lease reclaim rescues it because 'stored' has no lease.

    No sweep, no attacker, no artificial timing: the reclaim is
    ``store_upload_stream``'s own.
    """
    from src.services import upload_service

    owner = _owner(app_under_load)
    account = _account(app_under_load, owner.id)
    upload_id, url = _slot(app_under_load, owner.id, filename="retried.bin")

    # Small enough that neither attempt ever crosses a budget recheck, so this
    # is purely about the promote ordering and not about lease renewal.
    stalled_head = b"S" * 1024
    stalled_tail = b"s" * 1024
    retry_payload = b"R" * 4096
    promoted = asyncio.Event()

    async def stalled_body():
        yield stalled_head
        # The tail lands only after the retry has been stored and committed:
        # the ordering under test is fixed rather than raced.
        await promoted.wait()
        yield stalled_tail

    async with _client(app_under_load) as client:
        stalled = asyncio.create_task(client.put(url, content=stalled_body()))
        for _ in range(400):
            await asyncio.sleep(0.005)
            if _row(app_under_load, upload_id).state == "receiving":
                break
        else:
            stalled.cancel()
            pytest.fail("the stalled attempt never claimed the slot")

        # The stall outlasts the lease -- minutes, still far inside the 900s TTL.
        _time_passes(monkeypatch, upload_service._LEASE_SECONDS + 1)

        # The retry the lease exists to permit. It reclaims the lapsed claim,
        # streams, validates and commits.
        retry = await client.put(url, content=retry_payload)
        assert retry.status_code == 200, retry.text
        won = _row(app_under_load, upload_id)
        assert won.state == "stored"
        assert (app_under_load.root / upload_id).read_bytes() == retry_payload

        # Now the stalled socket delivers its last chunk.
        promoted.set()
        tail = await stalled

        # It is refused -- it lost the slot minutes ago.
        assert tail.status_code in (404, 409), tail.text

        row = _row(app_under_load, upload_id)
        payload_path = app_under_load.root / upload_id
        assert row.state == "stored"
        assert payload_path.is_file(), "the loser's cleanup deleted the winner's payload"
        assert payload_path.read_bytes() == retry_payload
        assert row.size == len(retry_payload)
        assert row.sha256 == hashlib.sha256(retry_payload).hexdigest()
        # Nothing of the loser is left occupying the volume either.
        assert [entry.name for entry in app_under_load.root.iterdir()] == [upload_id]

        # The outcome the owner actually cares about: the slot still sends.
        sent = await client.post(
            "/api/v1/send",
            json={
                "account_id": account.id,
                "to_addresses": ["recipient@example.com"],
                "subject": "Retried",
                "upload_ids": [upload_id],
            },
        )

    assert sent.status_code == 200, sent.text
    assert _attachment_parts(app_under_load.sent[0]) == [("retried.bin", retry_payload)]


# --------------------------------------------------------------------------
# SEC-3: an in-flight claim is fenced on its holder, not merely on its state
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_displaced_holder_cannot_renew_or_promote_over_its_successor(
    app_under_load, monkeypatch
):
    """``state = 'receiving'`` identifies a state, not a holder.

    After a lapsed claim is reclaimed and a *second* attempt takes the slot back
    into 'receiving', the first attempt's renewal matches the same predicate and
    succeeds: it pushes the successor's lease out, overwrites the successor's
    running total with its own -- so the byte budget is charged against the
    wrong body -- and it never learns it lost, which is precisely the brake
    ``_reclaim_lapsed``'s docstring claims. Its abort path then hands the
    successor's slot back to 'pending' on the way out.

    A claim has to carry an identity, and every write that ends or extends it
    has to match that identity as well as the state.
    """
    from src.services import upload_service

    monkeypatch.setattr(upload_service, "_BUDGET_RECHECK_BYTES", 256)
    owner = _owner(app_under_load)
    upload_id, url = _slot(app_under_load, owner.id, filename="fenced.bin")

    displaced_gate = asyncio.Event()
    successor_gate = asyncio.Event()
    successor_payload = b"Y" * 300

    async def displaced_body():
        # 512 crosses the 256-byte recheck, so this renews while it legitimately
        # holds the claim, and parks.
        yield b"X" * 512
        await displaced_gate.wait()
        # The renewal that must fail: by now somebody else owns the slot.
        yield b"X" * 512
        yield b"X" * 512

    async def successor_body():
        yield successor_payload
        await successor_gate.wait()

    async def _wait_for(predicate, what):
        for _ in range(400):
            await asyncio.sleep(0.005)
            if predicate(_row(app_under_load, upload_id)):
                return
        pytest.fail(what)

    async with _client(app_under_load) as client:
        displaced = asyncio.create_task(client.put(url, content=displaced_body()))
        await _wait_for(
            lambda row: row.state == "receiving" and (row.received_bytes or 0) == 512,
            "the first attempt never took and renewed the claim",
        )

        # The stall outlasts the lease, so the slot is genuinely reclaimable.
        _time_passes(monkeypatch, upload_service._LEASE_SECONDS + 1)

        # The successor reclaims it and takes it back into 'receiving' with a
        # running total of its own.
        successor = asyncio.create_task(client.put(url, content=successor_body()))
        await _wait_for(
            lambda row: row.state == "receiving"
            and (row.received_bytes or 0) == len(successor_payload),
            "the successor never took the reclaimed claim",
        )

        # And only now does the displaced attempt wake up and try to renew.
        displaced_gate.set()
        lost = await displaced

        assert lost.status_code in (404, 409), lost.text
        row = _row(app_under_load, upload_id)
        # The successor still owns its claim, with its own total and lease.
        assert row.state == "receiving", "the displaced attempt reset its successor's slot"
        assert (row.received_bytes or 0) == len(successor_payload), (
            "the displaced attempt's total was charged against the successor's slot"
        )

        successor_gate.set()
        stored = await successor

    assert stored.status_code == 200, stored.text
    assert _row(app_under_load, upload_id).state == "stored"
    assert (app_under_load.root / upload_id).read_bytes() == successor_payload
    assert [entry.name for entry in app_under_load.root.iterdir()] == [upload_id]


# --------------------------------------------------------------------------
# SEC-1: 'sending' is irreversible, so a timer must never recycle it
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_send_killed_after_the_wire_is_never_re_sent_by_a_lease_reclaim(
    app_under_load, monkeypatch
):
    """A process that died mid-send leaves delivery unknown. Say so; do not re-send.

    'sending' is the one state whose side effect cannot be undone. Reclaiming it
    to 'stored' tells a retry the bytes are freshly sendable, and both guards
    the comments lean on are inoperative: ``idempotency_key`` is optional and
    ``mail_service`` mints a fresh uuid when it is absent -- which is exactly
    what a client does after an opaque transport error -- and ``load_uploads``
    only compares a payload to its own row, which still agree perfectly after a
    reclaim. Two byte-identical messages then reach the MTA from one user
    intent.
    """
    from src.services import mail_service, upload_service
    from src.services.upload_service import reclaim_lapsed_leases

    owner = _owner(app_under_load)
    account = _account(app_under_load, owner.id)
    upload_id, url = _slot(app_under_load, owner.id, filename="confidential.pdf")
    payload = b"%PDF-1.7 the message that must be delivered at most once"

    # Exactly what a SIGKILL between the wire and the bookkeeping leaves behind:
    # the bytes are gone to the MTA, the row is still in 'sending', and nothing
    # is holding it. The settlement never runs at all.
    def died_before_settling(db, uploads, *, outcome):
        return None

    async with _client(app_under_load) as client:
        assert (await client.put(url, content=payload)).status_code == 200

        # Restored by hand rather than with monkeypatch.undo(), which would also
        # revert the fixture's own patches -- including the captured SMTP -- and
        # quietly turn the retry below into a database error that never reaches
        # the code under test.
        settled = mail_service.settle_uploads
        mail_service.settle_uploads = died_before_settling
        try:
            first = await client.post(
                "/api/v1/send",
                json={
                    "account_id": account.id,
                    "to_addresses": ["recipient@example.com"],
                    "subject": "Quarterly numbers",
                    "upload_ids": [upload_id],
                },
            )
        finally:
            mail_service.settle_uploads = settled
        assert first.status_code == 200, first.text
        assert len(app_under_load.sent) == 1
        assert _row(app_under_load, upload_id).state == "sending"

        # The lease lapses and the reclaim runs, as it must: a row nothing can
        # ever leave 'sending' is its own defect.
        _time_passes(monkeypatch, upload_service._LEASE_SECONDS + 1)
        with app_under_load.Session() as db:
            assert reclaim_lapsed_leases(db) == 1

        # The client saw an opaque transport error and retries. No idempotency
        # key -- a fresh one is the natural thing after a lost connection, and
        # mail_service mints one anyway when it is omitted.
        second = await client.post(
            "/api/v1/send",
            json={
                "account_id": account.id,
                "to_addresses": ["recipient@example.com"],
                "subject": "Quarterly numbers",
                "upload_ids": [upload_id],
            },
        )

    # The observable outcome: one message reached the MTA, not two.
    assert len(app_under_load.sent) == 1, "an already-sent payload was delivered a second time"
    assert second.status_code == 409, second.text
    # And the owner is told the truth rather than being quietly refused.
    detail = second.json().get("detail", "")
    assert "unknown" in detail.lower(), detail
    assert "new" in detail.lower() and "slot" in detail.lower(), detail


# --------------------------------------------------------------------------
# SEC-4: an ambiguous SMTP result is not a failure, and must not recycle a slot
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("wire_raises", "wire_refuses", "expected_state"),
    [
        # TimeoutError / SMTPServerDisconnected after send_message has handed
        # the bytes over: smtp_sender reports 'unknown', and it means it -- the
        # message may already be on the wire.
        pytest.param(TimeoutError, None, "unknown", id="timeout"),
        pytest.param(
            smtplib.SMTPServerDisconnected, None, "unknown", id="server_disconnected"
        ),
        # A partial acceptance is not ambiguous about *this* connection, but it
        # is ambiguous about the message: some recipients already have it, and
        # re-sending delivers to them twice.
        pytest.param(None, {"second@example.com": (550, b"no")}, "partial", id="partial"),
    ],
)
@pytest.mark.asyncio
async def test_an_ambiguous_send_result_is_never_delivered_twice(
    app_under_load, wire_raises, wire_refuses, expected_state
):
    """The in-process half of SEC-1, which the lease-lapse fix did not reach.

    ``send_email`` returns 'unknown' when the transport died after the bytes
    were handed over, and 'partial' when some recipients already accepted. In
    both the message may be -- or demonstrably is -- delivered. ``send_mail``
    then raised a 502, and ``settle_uploads``' boolean collapsed those two
    outcomes into the same action as a flat refusal: release the claim back to
    'stored'.

    A retry with no idempotency key (mail_service mints a fresh uuid when one
    is absent, and a fresh key is exactly what a client produces after an
    opaque transport error) then found the slot sendable and put the identical
    bytes on the wire a second time. The same-key retry was correctly refused;
    only this path duplicated.
    """
    from src.services import upload_service

    owner = _owner(app_under_load)
    account = _account(app_under_load, owner.id)
    upload_id, url = _slot(app_under_load, owner.id, filename="confidential.pdf")
    payload = b"CONFIDENTIAL" * 100

    app_under_load.manager.wire_raises = wire_raises
    app_under_load.manager.wire_refuses = wire_refuses

    def send(client, **extra):
        return client.post(
            "/api/v1/send",
            json={
                "account_id": account.id,
                "to_addresses": ["recipient@example.com", "second@example.com"],
                "subject": "Quarterly numbers",
                "upload_ids": [upload_id],
                **extra,
            },
        )

    async with _client(app_under_load) as client:
        assert (await client.put(url, content=payload)).status_code == 200

        first = await send(client)
        assert first.status_code == 502, first.text
        # The premise: the bytes did reach the MTA on that attempt.
        assert len(app_under_load.sent) == 1
        # And the slot is retired rather than handed back as freshly sendable.
        assert _row(app_under_load, upload_id).state == upload_service._SEND_UNKNOWN, (
            f"a '{expected_state}' result returned the slot to a sendable state"
        )

        # The client saw an opaque error and retries. No idempotency key: the
        # one shape the prior fix leaves unguarded.
        second = await send(client)

    assert len(app_under_load.sent) == 1, "an already-sent payload was delivered a second time"
    assert second.status_code == 409, second.text
    detail = second.json().get("detail", "")
    assert "unknown" in detail.lower(), detail
    assert "new" in detail.lower() and "slot" in detail.lower(), detail


@pytest.mark.asyncio
async def test_a_definitively_refused_send_still_leaves_the_slot_sendable(app_under_load):
    """The other half of the mapping, which must not be lost to the fix above.

    A send the MTA refused outright never reached anybody, so the honest retry
    has to work without re-uploading the bytes. Only genuinely ambiguous
    outcomes retire the slot.
    """
    owner = _owner(app_under_load)
    account = _account(app_under_load, owner.id)
    upload_id, url = _slot(app_under_load, owner.id, filename="retryable.txt")
    payload = b"nothing left the building"

    async with _client(app_under_load) as client:
        assert (await client.put(url, content=payload)).status_code == 200

        app_under_load.manager.result = {
            "success": False,
            "delivery_state": "failed",
            "message": "SMTP refused the message",
        }
        refused = await client.post(
            "/api/v1/send",
            json={
                "account_id": account.id,
                "to_addresses": ["recipient@example.com"],
                "subject": "First attempt",
                "upload_ids": [upload_id],
            },
        )
        assert refused.status_code == 502, refused.text
        assert app_under_load.sent == []
        assert _row(app_under_load, upload_id).state == "stored"

        app_under_load.manager.result = {"success": True, "delivery_state": "sent"}
        retried = await client.post(
            "/api/v1/send",
            json={
                "account_id": account.id,
                "to_addresses": ["recipient@example.com"],
                "subject": "Second attempt",
                "upload_ids": [upload_id],
            },
        )

    assert retried.status_code == 200, retried.text
    assert _attachment_parts(app_under_load.sent[0]) == [("retryable.txt", payload)]
    assert _row(app_under_load, upload_id).state == "consumed"


@pytest.mark.asyncio
async def test_a_slot_with_an_unknown_delivery_outcome_is_terminal_not_recycled(
    app_under_load, monkeypatch
):
    """The unknown state is terminal: no budget, no re-send, swept with its bytes.

    Refusing the re-send is only half of it. A slot nothing can ever spend must
    also stop charging the owner's byte and slot budgets, and must be swept with
    its payload exactly as a consumed one is -- otherwise the honest answer
    costs the owner the volume for the rest of the TTL.
    """
    from fastapi import HTTPException

    from src.services import upload_service
    from src.services.upload_service import begin_upload, reclaim_lapsed_leases

    monkeypatch.setattr(settings, "max_pending_uploads_per_user", 1)
    owner = _owner(app_under_load)
    upload_id, url = _slot(app_under_load, owner.id, filename="unknown.bin")

    async with _client(app_under_load) as client:
        assert (await client.put(url, content=b"U" * 2048)).status_code == 200

    # The send claims it and the worker dies.
    with app_under_load.Session() as db:
        assert upload_service._claim(db, upload_id, "stored", "sending")

    # The owner's only slot is spoken for, so nothing new can be reserved.
    with app_under_load.Session() as db, pytest.raises(HTTPException) as blocked:
        begin_upload(db, owner.id, filename="blocked.bin")
    assert blocked.value.status_code == 429

    _time_passes(monkeypatch, upload_service._LEASE_SECONDS + 1)
    with app_under_load.Session() as db:
        assert reclaim_lapsed_leases(db) == 1

    # Terminal, so it costs the owner nothing further: a new slot is available
    # immediately rather than after the 900s TTL.
    with app_under_load.Session() as db:
        replacement = begin_upload(db, owner.id, filename="replacement.bin")
    assert replacement.state == "pending"

    # A re-PUT is refused too: the slot is spent, whatever became of the send.
    async with _client(app_under_load) as client:
        retry = await client.put(url, content=b"U" * 2048)
    assert retry.status_code == 409, retry.text

    # And its payload is freed by the retirement itself, rather than sitting on
    # the volume until the next sweep tick -- a terminal slot stops charging the
    # owner's budgets the moment it gets there, so the bytes must not outlive
    # the charge. The row is kept until its own expires_at -- see
    # ``test_a_retired_unknown_slot_outlives_the_sweep_that_reclaimed_it``: it
    # is the only thing that can still tell the owner what became of their
    # message, and deleting it turns that answer into a bare 404.
    payload_path = app_under_load.root / upload_id
    assert not payload_path.exists()
    with app_under_load.Session() as db:
        upload_service.sweep_expired_uploads(db)
    assert not payload_path.exists()
    assert _row(app_under_load, upload_id).state == upload_service._SEND_UNKNOWN

    # Once the slot's own TTL is past, the row goes too.
    _time_passes(monkeypatch, settings.outbound_upload_ttl_seconds + 1)
    with app_under_load.Session() as db:
        assert upload_service.sweep_expired_uploads(db) >= 1
        assert db.query(OutboundUpload).filter_by(upload_id=upload_id).one_or_none() is None


# --------------------------------------------------------------------------
# LOG-2: measure-before-claim belongs on both send sites, not one
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_retired_unknown_slot_outlives_the_sweep_that_reclaimed_it(
    app_under_load, monkeypatch
):
    """SUG-2: the explanation has to survive long enough to be read.

    ``sweep_expired_uploads`` reclaims lapsed leases and then deletes terminal
    rows in the same pass, so a slot that had just been retired to
    ``send_unknown`` by that very reclaim was deleted microseconds later. The
    owner's next call then got a bare 404 -- "no such slot" -- instead of the
    composed message telling them the delivery outcome is unknown and that
    they should reserve a new slot. A 404 invites exactly the retry the
    terminal state exists to prevent.

    The payload still goes immediately: it is the row that has to outlive the
    sweep, not the bytes.
    """
    from src.services import upload_service

    owner = _owner(app_under_load)
    account = _account(app_under_load, owner.id)
    upload_id, url = _slot(app_under_load, owner.id, filename="unknown.bin")

    async with _client(app_under_load) as client:
        assert (await client.put(url, content=b"U" * 2048)).status_code == 200

    # A send claims it and the worker dies without reporting.
    with app_under_load.Session() as db:
        assert upload_service._claim(db, upload_id, "stored", "sending")

    _time_passes(monkeypatch, upload_service._LEASE_SECONDS + 1)

    # One pass: the reclaim retires the slot and the deletion runs immediately
    # afterwards, which is exactly the interleaving that lost the message.
    with app_under_load.Session() as db:
        upload_service.sweep_expired_uploads(db)

    row = _row(app_under_load, upload_id)
    assert row.state == upload_service._SEND_UNKNOWN, (
        "the row explaining an unknown delivery was deleted by the same sweep that created it"
    )
    # The bytes are gone all the same: an honest "unknown" must not also cost
    # the owner the volume.
    assert not (app_under_load.root / upload_id).exists()

    # And the owner is told what happened rather than that the slot never existed.
    async with _client(app_under_load) as client:
        refused = await client.post(
            "/api/v1/send",
            json={
                "account_id": account.id,
                "to_addresses": ["recipient@example.com"],
                "subject": "Retry after an unknown outcome",
                "upload_ids": [upload_id],
            },
        )
    assert refused.status_code == 409, refused.text
    assert "unknown" in refused.json()["detail"].lower()
    assert app_under_load.sent == []

    # It is not immortal either: once its own TTL is past, it goes.
    _time_passes(monkeypatch, settings.outbound_upload_ttl_seconds + 1)
    with app_under_load.Session() as db:
        upload_service.sweep_expired_uploads(db)
        assert db.query(OutboundUpload).filter_by(upload_id=upload_id).one_or_none() is None


@pytest.mark.asyncio
async def test_a_doomed_draft_is_refused_without_touching_a_single_slot(
    app_under_load, monkeypatch
):
    """``save_draft`` claims first and measures second -- the opposite of send_mail.

    A draft that cannot fit therefore performs exactly the two write
    transactions per named slot that the reordering exists to eliminate: one to
    take it into 'sending', one to hand it back. ``send_mail`` already measures
    first; this is the same fix applied inconsistently, and here the refusal is
    charged against ``_rate_limit`` rather than being free, which is the only
    reason it is less severe.
    """
    from src.security.auth import local_owner_user
    from src.services import upload_service
    from src.services.write_service import save_draft

    monkeypatch.setattr(settings, "max_outbound_attachment_bytes", 4096)
    owner = _owner(app_under_load)
    account = _account(app_under_load, owner.id)

    ids = []
    async with _client(app_under_load) as client:
        for index in range(3):
            upload_id, url = _slot(app_under_load, owner.id, filename=f"part{index}.bin")
            assert (await client.put(url, content=b"x" * 3000)).status_code == 200
            ids.append(upload_id)

    # Only now: the uploads themselves legitimately claim.
    claims: list[tuple[str, str]] = []
    real_claim = upload_service._claim

    def watched(db, slot_id, expected, target, **values):
        claims.append((expected, target))
        return real_claim(db, slot_id, expected, target, **values)

    monkeypatch.setattr(upload_service, "_claim", watched)

    # 9000 bytes against a 4096 cap: refusable from the recorded sizes alone.
    with app_under_load.Session() as db, pytest.raises(HTTPException) as refused:
        await save_draft(
            db,
            local_owner_user(db),
            account_id=account.id,
            to_addresses=["recipient@example.com"],
            subject="Too large",
            upload_ids=ids,
        )

    assert refused.value.status_code == 400
    assert claims == [], f"a doomed draft still moved slots: {claims}"
    # And the slots survive it, so a corrected draft can still use them.
    assert {_row(app_under_load, upload_id).state for upload_id in ids} == {"stored"}
