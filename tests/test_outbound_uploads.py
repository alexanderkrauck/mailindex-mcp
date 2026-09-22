"""Outbound uploads end to end: slot, signed PUT, send, consumption, sweep.

The bytes of a new attachment must never cross the MCP conversation. The client
asks for a slot, PUTs the payload to a signed URL, then names the slot when
sending. These tests drive that path through the real HTTP route and the real
tool functions, and press on the edges that make it safe.
"""

import asyncio
import contextlib
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from fastmcp.exceptions import ToolError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.config import settings
from src.models.base import Base
from src.models.outbound_upload import OutboundUpload
from src.models.smtp_config import SMTPConfig
from src.models.user import User

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


class _CapturedSMTP:
    """Stands in for the socket, so the real MIME builder still runs."""

    def __init__(self):
        self.messages = []
        self.refuse = False

    def send_message(self, message, to_addrs=None):
        self.messages.append(message)
        return {}


@pytest.fixture
def service(tmp_path, monkeypatch):
    """The app, an isolated database, and a scratch data volume."""
    from src import mcp_tools
    from src.database import connection
    from src.database.connection import get_db
    from src.handlers import email_handler
    from src.server import api_app, final_app

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    engine = create_engine(
        f"sqlite:///{tmp_path}/uploads.db",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(connection, "engine", engine)
    monkeypatch.setattr(connection, "SessionLocal", Session)
    # mcp_tools bound the factory at import time, so the module attribute has to
    # be redirected as well or the tools would talk to the deployment database.
    monkeypatch.setattr(mcp_tools, "SessionLocal", Session)

    def override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    # api_app is mounted inside final_app and resolves its own dependencies, so
    # the override has to be registered there to take effect.
    api_app.dependency_overrides[get_db] = override_db

    sent: list = []

    class _Manager:
        """Routes a send through the real MIME construction, stopping at the wire."""

        result = {"success": True, "delivery_state": "sent"}

        async def send_email_via_config(self, *, smtp_config_id, owner_user_id, **kwargs):
            from src.email.smtp_sender import EmailSender

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
            server = _CapturedSMTP()
            sender._server = server
            outcome = await sender.send_email(**kwargs)
            sent.extend(server.messages)
            return outcome

        async def invalidate(self, smtp_config_id):
            return None

    manager = _Manager()
    monkeypatch.setattr(email_handler, "email_sender_manager", manager)

    with TestClient(final_app) as client:
        yield SimpleNamespace(
            client=client,
            Session=Session,
            engine=engine,
            root=tmp_path / "outbound-uploads",
            sent=sent,
            manager=manager,
        )
    api_app.dependency_overrides.clear()


def _owner(service) -> User:
    """The user both HTTP and MCP resolve to in development auth mode."""
    from src.security.auth import local_owner_user

    with service.Session() as db:
        return local_owner_user(db)


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


async def _tool(name: str):
    from src.server import mcp

    return (await mcp.get_tool(name)).fn


def _error_code(error: ToolError) -> str:
    return json.loads(str(error))["code"]


def _put(service, url: str, body: bytes, headers: dict | None = None):
    """PUT to the URL the tool handed out, exactly as an AI client would."""
    parts = urlsplit(url)
    return service.client.put(
        f"{parts.path}?{parts.query}" if parts.query else parts.path,
        content=body,
        headers=headers,
    )


def _attachment_parts(message) -> list[tuple[str, bytes]]:
    return [
        (part.get_filename(), part.get_payload(decode=True))
        for part in message.walk()
        if part.get_content_disposition() == "attachment"
    ]


# --------------------------------------------------------------------------
# The round trip
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_begin_put_and_send_carries_the_bytes_and_retires_the_slot(service):
    owner = _owner(service)
    account = _account(service, owner.id)
    payload = b"%PDF-1.7 the quarterly invoice"

    begin = await _tool("begin_attachment_upload")
    slot = await begin(filename="my report<>:|?.pdf", size_bytes=len(payload))

    assert slot["method"] == "PUT"
    assert slot["upload_expires_in"] == settings.outbound_upload_ttl_seconds
    assert slot["max_bytes"] == settings.max_outbound_attachment_bytes
    assert slot["upload_id"] in slot["upload_url"]
    # The URL is what the client is told to use; it has to actually resolve.
    assert urlsplit(slot["upload_url"]).path == f"/api/v1/uploads/{slot['upload_id']}"

    stored = _put(service, slot["upload_url"], payload)

    assert stored.status_code == 200
    body = stored.json()
    assert body["upload_id"] == slot["upload_id"]
    assert body["size"] == len(payload)
    assert body["sha256"] == hashlib.sha256(payload).hexdigest()
    assert body["state"] == "stored"
    assert body["filename"] == "my_report.pdf"
    assert body["content_type"] == "application/pdf"

    send = await _tool("send_mail")
    result = await send(
        account_id=account.id,
        to_addresses=["recipient@example.com"],
        subject="Invoice",
        body_text="Attached.",
        upload_ids=[slot["upload_id"]],
        idempotency_key="round-trip",
    )

    assert result["success"] is True
    # The payload reached the wire, under the sanitized name and byte-for-byte.
    assert _attachment_parts(service.sent[0]) == [("my_report.pdf", payload)]

    with service.Session() as db:
        assert db.query(OutboundUpload).one().state == "consumed"


# --------------------------------------------------------------------------
# The signed grant
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_download_token_cannot_be_spent_on_an_upload_slot(service):
    from src.security.download_tokens import issue_download_token

    owner = _owner(service)
    begin = await _tool("begin_attachment_upload")
    slot = await begin(filename="target.txt")
    # A download grant is for reading someone's own attachment; it must not be
    # usable to write bytes onto the volume.
    confused = issue_download_token(owner.id, 1)

    response = _put(
        service,
        f"/api/v1/uploads/{slot['upload_id']}?token={confused}",
        b"smuggled",
    )

    assert response.status_code == 403
    assert not (service.root / slot["upload_id"]).exists()


@pytest.mark.asyncio
async def test_a_token_minted_for_another_slot_is_refused(service):
    owner = _owner(service)
    begin = await _tool("begin_attachment_upload")
    first = await begin(filename="first.txt")
    second = await begin(filename="second.txt")
    borrowed = urlsplit(first["upload_url"]).query

    response = _put(
        service,
        f"/api/v1/uploads/{second['upload_id']}?{borrowed}",
        b"wrong slot",
    )

    assert response.status_code == 403
    assert not (service.root / second["upload_id"]).exists()
    assert owner.id


@pytest.mark.asyncio
async def test_an_absent_or_forged_token_is_refused(service):
    begin = await _tool("begin_attachment_upload")
    slot = await begin(filename="guarded.txt")
    path = f"/api/v1/uploads/{slot['upload_id']}"

    missing = _put(service, path, b"no token")
    garbage = _put(service, f"{path}?token=not-a-token", b"forged")
    empty = _put(service, f"{path}?token=", b"blank")

    assert [missing.status_code, garbage.status_code, empty.status_code] == [403, 403, 403]
    assert not (service.root / slot["upload_id"]).exists()


@pytest.mark.asyncio
async def test_another_owners_upload_id_is_never_writable_or_sendable(service):
    owner = _owner(service)
    account = _account(service, owner.id)
    from src.security.upload_tokens import issue_upload_token
    from src.services.upload_service import begin_upload

    with service.Session() as db:
        stranger = User(google_sub="stranger", email="stranger@example.com")
        db.add(stranger)
        db.commit()
        foreign = begin_upload(db, stranger.id, filename="theirs.txt")
        foreign_id = foreign.upload_id

    # A grant for the signed-in user over someone else's slot resolves nothing.
    response = _put(
        service,
        f"/api/v1/uploads/{foreign_id}?token={issue_upload_token(owner.id, foreign_id)}",
        b"not mine",
    )

    assert response.status_code == 404

    send = await _tool("send_mail")
    with pytest.raises(ToolError) as error:
        await send(
            account_id=account.id,
            to_addresses=["recipient@example.com"],
            subject="Borrowed",
            upload_ids=[foreign_id],
        )

    assert _error_code(error.value) == "NOT_FOUND"
    assert service.sent == []


# --------------------------------------------------------------------------
# Size, before and during the body
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_oversized_content_length_is_refused_before_the_body_is_read(service):
    begin = await _tool("begin_attachment_upload")
    slot = await begin(filename="huge.bin")
    # The body is tiny and would store perfectly well. Only the declared length
    # is impossible, so a 413 here proves the header was judged on its own.
    response = _put(
        service,
        slot["upload_url"],
        b"tiny",
        headers={"Content-Length": str(settings.max_outbound_attachment_bytes + 1)},
    )

    assert response.status_code == 413
    assert not (service.root / slot["upload_id"]).exists()
    with service.Session() as db:
        assert db.query(OutboundUpload).one().state == "pending"


@pytest.mark.asyncio
async def test_a_unicode_digit_content_length_does_not_crash_the_route(service):
    """``str.isdigit()`` is true of characters ``int()`` will not accept.

    '\\u00b2' passes isdigit() and then raises ValueError inside int(), which the
    route never catches, so a header value no HTTP client will even encode turns
    into an unhandled 500 rather than an ordinary refusal. It is delivered here
    through a raw ASGI scope, which is the shape a server hands the app, because
    httpx refuses to put a non-ASCII byte in a header at all.
    """
    from src.server import final_app

    begin = await _tool("begin_attachment_upload")
    slot = await begin(filename="unicode-length.bin")
    parts = urlsplit(slot["upload_url"])
    body = b"small body"
    messages: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    await final_app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "PUT",
            "path": parts.path,
            "raw_path": parts.path.encode(),
            "query_string": parts.query.encode(),
            "root_path": "",
            "scheme": "http",
            "headers": [(b"host", b"testserver"), (b"content-length", "\u00b2".encode("latin-1"))],
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
        },
        receive,
        send,
    )

    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    assert status != 500
    # An unparseable hint is simply not a hint; the body decides.
    assert status == 200
    with service.Session() as db:
        assert db.query(OutboundUpload).one().size == len(body)


@pytest.mark.asyncio
async def test_a_lying_content_length_is_still_caught_by_the_stream(service, monkeypatch):
    monkeypatch.setattr(settings, "max_outbound_attachment_bytes", 64)
    begin = await _tool("begin_attachment_upload")
    slot = await begin(filename="liar.bin")

    response = _put(
        service,
        slot["upload_url"],
        b"z" * 4096,
        headers={"Content-Length": "10"},
    )

    assert response.status_code == 413
    assert not (service.root / slot["upload_id"]).exists()
    # The session was rolled back, so the slot is still usable for an honest retry.
    honest = _put(service, slot["upload_url"], b"z" * 32)
    assert honest.status_code == 200


@pytest.mark.asyncio
async def test_the_route_never_buffers_the_whole_body_into_memory(service, monkeypatch):
    """A 25MB limit is no limit at all if the body is read before it is measured.

    ``await request.body()`` pulls the entire payload into the process before the
    size check can run, so a client sending gigabytes exhausts memory rather than
    receiving a 413. The only defence is never calling it, which is what this
    pins: the route is required to consume ``request.stream()``.
    """
    from starlette.requests import Request as StarletteRequest

    async def forbidden(self):
        raise AssertionError("the upload route buffered the request body")

    monkeypatch.setattr(StarletteRequest, "body", forbidden)
    begin = await _tool("begin_attachment_upload")
    slot = await begin(filename="streamed.bin")

    response = _put(service, slot["upload_url"], b"y" * 4096)

    assert response.status_code == 200
    assert response.json()["size"] == 4096


@pytest.mark.asyncio
async def test_a_rejected_upload_leaves_no_transaction_open_on_the_session(
    tmp_path, monkeypatch
):
    """A refused PUT must hand the session back clean.

    The service raises mid-request with a transaction already open on the slot's
    row. Left that way the connection stays pinned and the next caller to reuse
    the session inherits a transaction it never started, so the route has to roll
    back before re-raising.
    """
    from src.database import connection
    from src.database.connection import get_db
    from src.security.auth import local_owner_user
    from src.security.upload_tokens import issue_upload_token
    from src.server import api_app, final_app
    from src.services.upload_service import begin_upload

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "max_outbound_attachment_bytes", 64)
    engine = create_engine(
        f"sqlite:///{tmp_path}/rollback.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(connection, "engine", engine)
    monkeypatch.setattr(connection, "SessionLocal", Session)
    shared = Session()
    # One long-lived session, so what the route leaves behind is visible instead
    # of being swept away by a per-request close. The override belongs on api_app:
    # a mounted sub-app resolves its own dependencies, so one registered on the
    # outer app would silently never apply and the route would quietly use the
    # deployment database instead.
    api_app.dependency_overrides[get_db] = lambda: (yield shared)
    try:
        owner = local_owner_user(shared)
        slot = begin_upload(shared, owner.id, filename="oversize.bin")
        upload_id = str(slot.upload_id)
        token = issue_upload_token(int(owner.id), upload_id)
        # Start from a clean session, so what the assertion sees afterwards was
        # left by the request rather than by this setup.
        shared.commit()
        shared.expunge_all()
        assert shared.in_transaction() is False

        with TestClient(final_app) as client:
            response = client.put(
                f"/api/v1/uploads/{upload_id}?token={token}",
                # An understated length slips past the header pre-check, so the
                # refusal comes from the streaming limit -- the path that leaves
                # the slot's row modified and uncommitted.
                content=b"z" * 4096,
                headers={"Content-Length": "10"},
            )

        assert response.status_code == 413
        assert shared.in_transaction() is False
        # And the slot survived intact, so an honest retry still has somewhere to go.
        assert shared.query(OutboundUpload).one().state == "pending"
    finally:
        shared.close()
        api_app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_a_slot_accepts_one_payload_only(service):
    begin = await _tool("begin_attachment_upload")
    slot = await begin(filename="once.txt")

    first = _put(service, slot["upload_url"], b"the real payload")
    second = _put(service, slot["upload_url"], b"a substitution")

    assert first.status_code == 200
    assert second.status_code == 409
    assert (service.root / slot["upload_id"]).read_bytes() == b"the real payload"


# --------------------------------------------------------------------------
# Caps, failure and replay on the send path
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_existing_and_new_attachments_share_one_per_message_cap(service, monkeypatch):
    monkeypatch.setattr(settings, "max_outbound_attachments", 2)
    owner = _owner(service)
    account = _account(service, owner.id)
    begin = await _tool("begin_attachment_upload")
    slots = [await begin(filename=f"file{index}.txt") for index in range(2)]
    for slot in slots:
        _put(service, slot["upload_url"], b"bytes")

    send = await _tool("send_mail")
    with pytest.raises(ToolError) as error:
        await send(
            account_id=account.id,
            to_addresses=["recipient@example.com"],
            subject="Too many",
            # One existing attachment plus two uploads is three parts, and the
            # cap counts the whole message rather than either list alone.
            attachment_ids=[999_999],
            upload_ids=[slot["upload_id"] for slot in slots],
        )

    assert _error_code(error.value) == "INVALID_ARGUMENT"
    assert "attachments" in json.loads(str(error.value))["message"].lower()
    with service.Session() as db:
        # The cap is judged before anything is fetched, so nothing was spent.
        assert {row.state for row in db.query(OutboundUpload).all()} == {"stored"}


@pytest.mark.asyncio
async def test_a_failed_send_leaves_the_slot_ready_for_another_attempt(service):
    owner = _owner(service)
    account = _account(service, owner.id)
    begin = await _tool("begin_attachment_upload")
    slot = await begin(filename="retry.txt")
    _put(service, slot["upload_url"], b"payload worth keeping")
    send = await _tool("send_mail")
    service.manager.result = {
        "success": False,
        "delivery_state": "failed",
        "message": "SMTP refused the message",
    }

    with pytest.raises(ToolError) as error:
        await send(
            account_id=account.id,
            to_addresses=["recipient@example.com"],
            subject="First attempt",
            upload_ids=[slot["upload_id"]],
            idempotency_key="failed-attempt",
        )

    assert _error_code(error.value) == "PROVIDER_ERROR"
    with service.Session() as db:
        assert db.query(OutboundUpload).one().state == "stored"

    service.manager.result = {"success": True, "delivery_state": "sent"}
    result = await send(
        account_id=account.id,
        to_addresses=["recipient@example.com"],
        subject="Second attempt",
        upload_ids=[slot["upload_id"]],
        idempotency_key="second-attempt",
    )

    assert result["success"] is True
    assert _attachment_parts(service.sent[0]) == [("retry.txt", b"payload worth keeping")]
    with service.Session() as db:
        assert db.query(OutboundUpload).one().state == "consumed"


@pytest.mark.asyncio
async def test_a_genuine_retry_replays_the_cached_success(service):
    owner = _owner(service)
    account = _account(service, owner.id)
    begin = await _tool("begin_attachment_upload")
    slot = await begin(filename="invoice.pdf")
    _put(service, slot["upload_url"], b"%PDF-1.7 once only")
    send = await _tool("send_mail")
    arguments = {
        "account_id": account.id,
        "to_addresses": ["recipient@example.com"],
        "subject": "Invoice",
        "body_text": "Attached.",
        "upload_ids": [slot["upload_id"]],
        "idempotency_key": "retried-send",
    }

    first = await send(**arguments)
    # A client that lost the response retries the identical call. The slot is
    # spent by now, and the right answer is the original success, not a conflict.
    second = await send(**arguments)

    assert first["success"] is True
    assert second["idempotent_replay"] is True
    assert second["audit_id"] == first["audit_id"]
    # Exactly one message left the building.
    assert len(service.sent) == 1


@pytest.mark.asyncio
async def test_a_retry_that_tidies_a_duplicated_slot_still_replays(service):
    owner = _owner(service)
    account = _account(service, owner.id)
    begin = await _tool("begin_attachment_upload")
    slot = await begin(filename="once.pdf")
    _put(service, slot["upload_url"], b"%PDF-1.7 the same file")

    def post(ids):
        # Straight over HTTP, where the list arrives exactly as the caller wrote
        # it rather than tidied by a tool wrapper.
        return service.client.post(
            "/api/v1/send",
            json={
                "account_id": account.id,
                "to_addresses": ["recipient@example.com"],
                "subject": "Invoice",
                "upload_ids": ids,
                "idempotency_key": "tidied-retry",
            },
        )

    first = post([slot["upload_id"], slot["upload_id"]])
    # The same slot named once is the same request; the hash must agree, or the
    # retry is rejected as a different one instead of replaying.
    second = post([slot["upload_id"]])

    assert first.status_code == 200
    assert second.status_code == 200
    # Named twice, attached once.
    assert len(_attachment_parts(service.sent[0])) == 1
    assert second.json()["idempotent_replay"] is True
    assert second.json()["audit_id"] == first.json()["audit_id"]


@pytest.mark.asyncio
async def test_a_retry_whose_attachments_changed_is_still_rejected(service):
    owner = _owner(service)
    account = _account(service, owner.id)
    begin = await _tool("begin_attachment_upload")
    first_slot = await begin(filename="first.pdf")
    second_slot = await begin(filename="second.pdf")
    _put(service, first_slot["upload_url"], b"first payload")
    _put(service, second_slot["upload_url"], b"second payload")
    send = await _tool("send_mail")
    await send(
        account_id=account.id,
        to_addresses=["recipient@example.com"],
        subject="Invoice",
        upload_ids=[first_slot["upload_id"]],
        idempotency_key="reused-key",
    )

    with pytest.raises(ToolError) as error:
        await send(
            account_id=account.id,
            to_addresses=["recipient@example.com"],
            subject="Invoice",
            upload_ids=[second_slot["upload_id"]],
            idempotency_key="reused-key",
        )

    assert _error_code(error.value) == "CONFLICT"
    assert "different request" in json.loads(str(error.value))["message"]


@pytest.mark.asyncio
async def test_a_spent_slot_cannot_be_sent_again_under_a_new_key(service):
    owner = _owner(service)
    account = _account(service, owner.id)
    begin = await _tool("begin_attachment_upload")
    slot = await begin(filename="single-use.txt")
    _put(service, slot["upload_url"], b"one send only")
    send = await _tool("send_mail")
    await send(
        account_id=account.id,
        to_addresses=["recipient@example.com"],
        subject="First",
        upload_ids=[slot["upload_id"]],
        idempotency_key="first-send",
    )

    with pytest.raises(ToolError) as error:
        await send(
            account_id=account.id,
            to_addresses=["recipient@example.com"],
            subject="Second",
            upload_ids=[slot["upload_id"]],
            idempotency_key="a-different-send",
        )

    # Replay is for the same request; a new request may not spend a used slot.
    assert _error_code(error.value) == "CONFLICT"
    assert len(service.sent) == 1


# --------------------------------------------------------------------------
# Drafts
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_http_send_route_accepts_upload_ids_too(service):
    owner = _owner(service)
    account = _account(service, owner.id)
    begin = await _tool("begin_attachment_upload")
    slot = await begin(filename="contract.pdf")
    _put(service, slot["upload_url"], b"%PDF-1.7 signed contract")

    response = service.client.post(
        "/api/v1/send",
        json={
            "account_id": account.id,
            "to_addresses": ["recipient@example.com"],
            "subject": "Contract",
            "body_text": "Attached.",
            "upload_ids": [slot["upload_id"]],
            "idempotency_key": "http-send",
        },
    )

    assert response.status_code == 200
    assert _attachment_parts(service.sent[0]) == [
        ("contract.pdf", b"%PDF-1.7 signed contract")
    ]
    with service.Session() as db:
        assert db.query(OutboundUpload).one().state == "consumed"


@pytest.mark.asyncio
async def test_save_draft_attaches_the_uploaded_bytes_and_retires_the_slot(
    service, monkeypatch
):
    from src.services import write_service

    owner = _owner(service)
    account = _account(service, owner.id)
    begin = await _tool("begin_attachment_upload")
    slot = await begin(filename="agenda.txt")
    _put(service, slot["upload_url"], b"the agenda bytes")
    appended: dict = {}

    async def fake_with_mailbox(db, config, operation):
        return await operation(object())

    async def fake_list_folders(client):
        return [{"name": "Drafts", "special_use": "drafts"}]

    async def fake_append(client, folder, raw, *, flags):
        appended.update(folder=folder, raw=raw, flags=flags)
        return 7

    monkeypatch.setattr(write_service, "_with_mailbox", fake_with_mailbox)
    monkeypatch.setattr(write_service, "list_folders", fake_list_folders)
    monkeypatch.setattr(write_service, "append_message", fake_append)

    save = await _tool("save_draft")
    result = await save(
        account_id=account.id,
        to_addresses=["recipient@example.com"],
        subject="Agenda",
        body_text="See attached.",
        upload_ids=[slot["upload_id"]],
    )

    assert result["success"] is True
    assert appended["folder"] == "Drafts"
    import email as email_module

    draft = email_module.message_from_bytes(appended["raw"])
    assert _attachment_parts(draft) == [("agenda.txt", b"the agenda bytes")]
    with service.Session() as db:
        assert db.query(OutboundUpload).one().state == "consumed"


# --------------------------------------------------------------------------
# Housekeeping
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_api_heartbeat_sweeps_uploads_and_survives_a_failing_sweep(
    tmp_path, monkeypatch
):
    from src import server
    from src.database import connection
    from src.services.upload_service import begin_upload
    from src.sync_health import read_record, runtime_dir

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "sync_scheduler_interval_seconds", 0.01)
    monkeypatch.setattr(settings, "outbound_upload_ttl_seconds", 0)
    # The sweep runs on its own slow clock in production; drive it every tick here.
    monkeypatch.setattr(server, "UPLOAD_SWEEP_INTERVAL_SECONDS", 0.0)
    monkeypatch.setenv("EMAILSERVER_PROCESS_IDENTITY", "test-api")
    engine = create_engine(
        f"sqlite:///{tmp_path}/sweep.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(connection, "engine", engine)
    monkeypatch.setattr(connection, "SessionLocal", Session)
    monkeypatch.setattr(server, "DATABASE_PREPARED", True)

    with Session() as db:
        user = User(google_sub="sweep-owner", email="sweep@example.com")
        db.add(user)
        db.commit()
        expired = begin_upload(db, user.id, filename="forgotten.txt")
        expired.expires_at = datetime.now(tz=timezone.utc) - timedelta(seconds=1)
        db.commit()

    # The sweep itself is the API process's job, and it works.
    assert server._sweep_outbound_uploads().removed == 1
    with Session() as db:
        assert db.query(OutboundUpload).count() == 0

    calls: list[int] = []

    def exploding_sweep():
        calls.append(1)
        raise RuntimeError("the volume is on fire")

    monkeypatch.setattr(server, "_sweep_outbound_uploads", exploding_sweep)
    task = asyncio.create_task(server._api_heartbeat())
    try:
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(calls) >= 2 and read_record(runtime_dir() / "api.json"):
                break
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # Housekeeping is allowed to fail; liveness is not allowed to stop.
    assert len(calls) >= 2
    assert task.done()
    assert read_record(runtime_dir() / "api.json").get("pid")


# --------------------------------------------------------------------------
# Defect B: a negative size_bytes must not credit the byte budget
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_negative_size_bytes_cannot_disable_the_byte_budget(service, monkeypatch):
    """One MCP call must not buy a tenant unlimited outbound storage.

    ``begin_attachment_upload`` only ever checked the upper bound, so a
    negative ``size_bytes`` was recorded verbatim and charged verbatim: the
    owner's live total went negative and every subsequent reservation, at the
    full per-attachment maximum, was granted against a budget that could no
    longer bind.
    """
    from src.services.upload_service import _committed_bytes

    monkeypatch.setattr(settings, "max_outbound_attachment_bytes", 1024 * 1024)
    monkeypatch.setattr(settings, "max_pending_upload_bytes_per_user", 2 * 1024 * 1024)
    owner = _owner(service)
    begin = await _tool("begin_attachment_upload")

    with pytest.raises(ToolError) as refused:
        await begin(filename="credit.bin", size_bytes=-1073741824)

    assert _error_code(refused.value) == "INVALID_ARGUMENT"
    with service.Session() as db:
        assert _committed_bytes(db, owner.id) >= 0

    # And the budget still binds afterwards: two 1MiB slots fit in 2MiB, a
    # third does not.
    await begin(filename="a.bin", size_bytes=1024 * 1024)
    await begin(filename="b.bin", size_bytes=1024 * 1024)
    with pytest.raises(ToolError) as full:
        await begin(filename="c.bin", size_bytes=1024 * 1024)
    assert _error_code(full.value) == "RATE_LIMITED"


@pytest.mark.asyncio
async def test_the_upload_tool_schema_advertises_a_non_negative_size(service):
    """The bound belongs in the schema an AI client reads, not only in the server."""
    from src.server import mcp

    tool = await mcp.get_tool("begin_attachment_upload")
    size = tool.parameters["properties"]["size_bytes"]
    branches = size.get("anyOf", [size])
    integer = next(branch for branch in branches if branch.get("type") == "integer")

    assert integer.get("minimum") == 0
