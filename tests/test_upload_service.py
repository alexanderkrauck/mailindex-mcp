"""Outbound upload slots: bytes reach the volume without crossing the conversation."""

import hashlib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.config import settings
from src.models.base import Base
from src.models.outbound_upload import OutboundUpload
from src.models.user import User
from src.services.upload_service import (
    _SEND_UNKNOWN,
    _expired,
    _live_pending,
    begin_upload,
    claim_uploads_for_send,
    load_uploads,
    mark_consumed,
    retire_unknown_uploads,
    store_upload_stream,
    sweep_expired_uploads,
)


@pytest.fixture
def upload_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def upload_root(tmp_path, monkeypatch):
    """Point the data volume at a scratch directory for the duration of a test."""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    return tmp_path / "outbound-uploads"


def _user(db, sub: str) -> User:
    user = User(google_sub=sub, email=f"{sub}@example.com")
    db.add(user)
    db.flush()
    return user


async def _chunks(*parts: bytes):
    for part in parts:
        yield part


def _take(db, user_id: int, upload_ids: list[str]) -> list[dict]:
    """Claim the named slots and read them, as a send does."""
    return load_uploads(db, claim_uploads_for_send(db, user_id, upload_ids))


def _consume(db, user_id: int, upload_ids: list[str]) -> None:
    """Retire slots the way a successful send does: claim, then consume."""
    mark_consumed(db, claim_uploads_for_send(db, user_id, upload_ids))


@pytest.mark.asyncio
async def test_begin_store_load_and_consume_round_trip(upload_db, upload_root):
    owner = _user(upload_db, "upload-owner")
    payload = b"%PDF-1.7 invoice bytes"

    slot = begin_upload(upload_db, owner.id, filename="invoice.pdf")

    assert slot.state == "pending"
    assert slot.upload_id
    assert len(slot.upload_id) >= 32
    assert slot.size is None

    receipt = await store_upload_stream(
        upload_db, slot.upload_id, owner.id, _chunks(payload[:8], payload[8:])
    )

    # The receipt is built from what the attempt had in hand rather than by
    # re-reading the row, so a TTL lapsing right after the promote cannot turn a
    # successful upload into a 404.
    assert receipt["state"] == "stored"
    assert receipt["size"] == len(payload)
    assert receipt["sha256"] == hashlib.sha256(payload).hexdigest()
    assert receipt["content_type"] == "application/pdf"
    assert receipt["filename"] == "invoice.pdf"
    # And it describes the row that was actually written.
    stored = upload_db.query(OutboundUpload).filter_by(upload_id=slot.upload_id).one()
    assert (stored.state, stored.size, stored.sha256) == (
        receipt["state"],
        receipt["size"],
        receipt["sha256"],
    )
    assert stored.detected_content_type == "application/pdf"
    on_disk = upload_root / slot.upload_id
    assert on_disk.read_bytes() == payload
    assert stored.storage_path == str(on_disk)
    # The payload stays on the volume, never in a column a pg_dump would carry.
    assert not any(
        isinstance(getattr(stored, column.name), bytes) for column in OutboundUpload.__table__.columns
    )
    assert oct(upload_root.stat().st_mode)[-3:] == "700"

    loaded = _take(upload_db, owner.id, [slot.upload_id])

    assert [item["data"] for item in loaded] == [payload]
    assert [item["filename"] for item in loaded] == ["invoice.pdf"]
    assert loaded[0]["upload"].id == slot.id

    mark_consumed(upload_db, [item["upload"] for item in loaded])

    assert upload_db.query(OutboundUpload).one().state == "consumed"
    assert upload_db.query(OutboundUpload).one().consumed_at is not None
    # Consumption is what retires a slot, and the bytes go with it: a spent slot
    # can never be sent again, so nothing is waiting on them and there is no
    # reason for them to occupy the volume until the next sweep tick.
    assert not on_disk.exists()


@pytest.mark.asyncio
async def test_load_preserves_caller_order_and_deduplicates(upload_db, upload_root):
    owner = _user(upload_db, "order-owner")
    first = begin_upload(upload_db, owner.id, filename="first.txt")
    second = begin_upload(upload_db, owner.id, filename="second.txt")
    await store_upload_stream(upload_db, first.upload_id, owner.id, _chunks(b"one"))
    await store_upload_stream(upload_db, second.upload_id, owner.id, _chunks(b"two"))

    loaded = _take(upload_db, owner.id, [second.upload_id, first.upload_id, second.upload_id])

    assert [item["data"] for item in loaded] == [b"two", b"one"]
    assert [item["filename"] for item in loaded] == ["second.txt", "first.txt"]


@pytest.mark.asyncio
async def test_sweep_leaves_a_live_slot_and_its_bytes_alone(upload_db, upload_root):
    owner = _user(upload_db, "sweep-live-owner")
    pending = begin_upload(upload_db, owner.id, filename="pending.txt")
    live = begin_upload(upload_db, owner.id, filename="live.txt")
    await store_upload_stream(upload_db, live.upload_id, owner.id, _chunks(b"still needed"))

    assert sweep_expired_uploads(upload_db) == 0
    assert upload_db.query(OutboundUpload).filter_by(id=pending.id).one_or_none() is not None
    assert upload_db.query(OutboundUpload).filter_by(id=live.id).one_or_none() is not None
    assert (upload_root / live.upload_id).read_bytes() == b"still needed"


@pytest.mark.asyncio
async def test_stream_past_the_size_limit_aborts_and_leaves_no_file(
    upload_db, upload_root, monkeypatch
):
    monkeypatch.setattr(settings, "max_outbound_attachment_bytes", 16)
    owner = _user(upload_db, "oversize-owner")
    slot = begin_upload(upload_db, owner.id, filename="big.bin")
    delivered: list[int] = []

    async def greedy():
        # A body that keeps coming must be cut off, not buffered to completion.
        for index in range(1000):
            delivered.append(index)
            yield b"x" * 8

    with pytest.raises(HTTPException) as error:
        await store_upload_stream(upload_db, slot.upload_id, owner.id, greedy())

    assert error.value.status_code == 413
    assert len(delivered) < 10, "the stream must be abandoned, not drained"
    assert not (upload_root / slot.upload_id).exists()
    assert list(upload_root.iterdir()) == []
    upload_db.rollback()
    assert upload_db.query(OutboundUpload).one().state == "pending"


@pytest.mark.asyncio
async def test_declared_sha256_mismatch_is_rejected_and_the_file_removed(upload_db, upload_root):
    owner = _user(upload_db, "digest-owner")
    slot = begin_upload(
        upload_db,
        owner.id,
        filename="claimed.txt",
        declared_sha256=hashlib.sha256(b"promised").hexdigest(),
    )

    with pytest.raises(HTTPException) as error:
        await store_upload_stream(upload_db, slot.upload_id, owner.id, _chunks(b"delivered"))

    assert error.value.status_code == 409
    assert not (upload_root / slot.upload_id).exists()
    upload_db.rollback()
    assert upload_db.query(OutboundUpload).one().state == "pending"


@pytest.mark.asyncio
async def test_declared_size_mismatch_is_rejected_and_the_file_removed(upload_db, upload_root):
    owner = _user(upload_db, "size-owner")
    slot = begin_upload(upload_db, owner.id, filename="claimed.txt", declared_size=99)

    with pytest.raises(HTTPException) as error:
        await store_upload_stream(upload_db, slot.upload_id, owner.id, _chunks(b"short"))

    assert error.value.status_code == 409
    assert not (upload_root / slot.upload_id).exists()


@pytest.mark.asyncio
async def test_a_slot_accepts_bytes_only_once(upload_db, upload_root):
    owner = _user(upload_db, "single-shot-owner")
    slot = begin_upload(upload_db, owner.id, filename="once.txt")
    await store_upload_stream(upload_db, slot.upload_id, owner.id, _chunks(b"first write"))

    with pytest.raises(HTTPException) as error:
        await store_upload_stream(upload_db, slot.upload_id, owner.id, _chunks(b"overwrite"))

    assert error.value.status_code == 409
    # The original payload must survive an attempt to replace it.
    assert (upload_root / slot.upload_id).read_bytes() == b"first write"


@pytest.mark.asyncio
async def test_a_consumed_slot_cannot_be_written_again(upload_db, upload_root):
    owner = _user(upload_db, "reuse-owner")
    slot = begin_upload(upload_db, owner.id, filename="spent.txt")
    await store_upload_stream(upload_db, slot.upload_id, owner.id, _chunks(b"sent already"))
    _consume(upload_db, owner.id, [slot.upload_id])

    with pytest.raises(HTTPException) as error:
        await store_upload_stream(upload_db, slot.upload_id, owner.id, _chunks(b"again"))

    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_another_users_upload_id_is_invisible(upload_db, upload_root):
    owner = _user(upload_db, "victim-owner")
    intruder = _user(upload_db, "intruder")
    slot = begin_upload(upload_db, owner.id, filename="private.txt")
    await store_upload_stream(upload_db, slot.upload_id, owner.id, _chunks(b"private bytes"))

    with pytest.raises(HTTPException) as write_error:
        await store_upload_stream(upload_db, slot.upload_id, intruder.id, _chunks(b"stolen"))
    with pytest.raises(HTTPException) as read_error:
        _take(upload_db, intruder.id, [slot.upload_id])

    # 404 both ways: a foreign id must not even be confirmed to exist.
    assert write_error.value.status_code == 404
    assert read_error.value.status_code == 404
    assert (upload_root / slot.upload_id).read_bytes() == b"private bytes"


@pytest.mark.asyncio
async def test_an_expired_slot_is_refused_for_write_and_read(upload_db, upload_root):
    owner = _user(upload_db, "expiry-owner")
    stored = begin_upload(upload_db, owner.id, filename="late.txt")
    await store_upload_stream(upload_db, stored.upload_id, owner.id, _chunks(b"in time"))
    unwritten = begin_upload(upload_db, owner.id, filename="never.txt")
    stale = datetime.now(tz=timezone.utc) - timedelta(seconds=1)
    stored.expires_at = stale
    unwritten.expires_at = stale
    upload_db.commit()

    with pytest.raises(HTTPException) as write_error:
        await store_upload_stream(upload_db, unwritten.upload_id, owner.id, _chunks(b"too late"))
    with pytest.raises(HTTPException) as read_error:
        _take(upload_db, owner.id, [stored.upload_id])

    assert write_error.value.status_code == 404
    assert read_error.value.status_code == 404


@pytest.mark.asyncio
async def test_a_consumed_upload_cannot_be_loaded_again(upload_db, upload_root):
    owner = _user(upload_db, "replay-owner")
    slot = begin_upload(upload_db, owner.id, filename="attached.txt")
    await store_upload_stream(upload_db, slot.upload_id, owner.id, _chunks(b"attached once"))
    _consume(upload_db, owner.id, [slot.upload_id])

    with pytest.raises(HTTPException) as error:
        _take(upload_db, owner.id, [slot.upload_id])

    assert error.value.status_code == 409


def test_an_unstored_or_unknown_id_cannot_be_loaded(upload_db, upload_root):
    owner = _user(upload_db, "empty-owner")
    reserved = begin_upload(upload_db, owner.id, filename="reserved.txt")

    with pytest.raises(HTTPException) as pending_error:
        _take(upload_db, owner.id, [reserved.upload_id])
    with pytest.raises(HTTPException) as unknown_error:
        _take(upload_db, owner.id, ["not-a-real-upload-id"])

    assert pending_error.value.status_code == 409
    assert unknown_error.value.status_code == 404


def test_pending_slot_count_is_capped_per_user(upload_db, upload_root, monkeypatch):
    monkeypatch.setattr(settings, "max_pending_uploads_per_user", 2)
    owner = _user(upload_db, "count-budget-owner")
    other = _user(upload_db, "count-budget-other")
    begin_upload(upload_db, owner.id, filename="one.txt")
    begin_upload(upload_db, owner.id, filename="two.txt")

    with pytest.raises(HTTPException) as error:
        begin_upload(upload_db, owner.id, filename="three.txt")

    assert error.value.status_code == 429
    # The budget is per owner, so one tenant cannot lock another out.
    assert begin_upload(upload_db, other.id, filename="mine.txt").state == "pending"


@pytest.mark.asyncio
async def test_pending_byte_budget_counts_declared_and_stored_bytes(
    upload_db, upload_root, monkeypatch
):
    monkeypatch.setattr(settings, "max_pending_upload_bytes_per_user", 100)
    # An unsized slot is charged this much, so keep it small enough to reason about.
    monkeypatch.setattr(settings, "max_outbound_attachment_bytes", 50)
    owner = _user(upload_db, "byte-budget-owner")
    # 40 bytes promised but not yet delivered still occupies the budget.
    begin_upload(upload_db, owner.id, filename="promised.bin", declared_size=40)
    stored = begin_upload(upload_db, owner.id, filename="delivered.bin")
    await store_upload_stream(upload_db, stored.upload_id, owner.id, _chunks(b"y" * 50))

    with pytest.raises(HTTPException) as error:
        begin_upload(upload_db, owner.id, filename="too-much.bin", declared_size=20)

    assert error.value.status_code == 429

    # Consuming the stored slot frees its share of the budget.
    _consume(upload_db, owner.id, [stored.upload_id])
    assert begin_upload(upload_db, owner.id, filename="now-fits.bin", declared_size=20).state == "pending"


def test_a_slot_with_no_declared_size_is_charged_what_it_could_still_take(
    upload_db, upload_root, monkeypatch
):
    """size_bytes is optional; omitting it must not make the reservation free."""
    monkeypatch.setattr(settings, "max_pending_upload_bytes_per_user", 100)
    monkeypatch.setattr(settings, "max_outbound_attachment_bytes", 60)
    owner = _user(upload_db, "unsized-budget-owner")

    # One unsized slot can still be filled to 60, so that is what it costs.
    begin_upload(upload_db, owner.id, filename="unsized.bin")

    with pytest.raises(HTTPException) as error:
        begin_upload(upload_db, owner.id, filename="also-unsized.bin")

    assert error.value.status_code == 429
    assert "budget" in str(error.value.detail)


def test_a_traversal_filename_cannot_escape_the_upload_directory(upload_db, upload_root, monkeypatch):
    monkeypatch.setattr(settings, "max_pending_uploads_per_user", 100)
    owner = _user(upload_db, "traversal-owner")
    hostile = [
        "../../etc/passwd",
        "..\\..\\windows\\system32\\config\\sam",
        "/etc/shadow",
        "....//....//root/.ssh/authorized_keys",
    ]

    slots = [begin_upload(upload_db, owner.id, filename=name) for name in hostile]

    for slot in slots:
        # The column is sanitized for MIME use...
        assert "/" not in slot.filename
        assert "\\" not in slot.filename
        assert ".." not in slot.filename
        # ...and the path is built from the server-generated id alone.
        resolved = (upload_root / slot.upload_id).resolve()
        assert resolved.parent == upload_root.resolve()
        assert slot.upload_id not in ("", ".", "..")


@pytest.mark.asyncio
async def test_the_filename_never_reaches_the_storage_path(upload_db, upload_root):
    owner = _user(upload_db, "path-owner")
    slot = begin_upload(upload_db, owner.id, filename="../../etc/passwd")

    await store_upload_stream(upload_db, slot.upload_id, owner.id, _chunks(b"root:x:0:0"))

    stored = upload_db.query(OutboundUpload).filter_by(upload_id=slot.upload_id).one()
    written = Path(stored.storage_path)
    assert written.resolve().parent == upload_root.resolve()
    assert written.name == slot.upload_id
    assert "passwd" not in stored.storage_path
    assert [entry.name for entry in upload_root.iterdir()] == [slot.upload_id]


def test_the_filename_is_sanitized_on_the_way_in(upload_db, upload_root):
    owner = _user(upload_db, "sanitize-owner")

    slot = begin_upload(upload_db, owner.id, filename="my report<>:|?.pdf")

    assert slot.filename == "my_report.pdf"


@pytest.mark.asyncio
async def test_sweep_removes_expired_and_consumed_rows_with_their_files(upload_db, upload_root):
    owner = _user(upload_db, "sweep-owner")
    expired_pending = begin_upload(upload_db, owner.id, filename="abandoned.txt")
    expired_stored = begin_upload(upload_db, owner.id, filename="stale.txt")
    consumed = begin_upload(upload_db, owner.id, filename="sent.txt")
    live = begin_upload(upload_db, owner.id, filename="current.txt")
    await store_upload_stream(upload_db, expired_stored.upload_id, owner.id, _chunks(b"stale"))
    await store_upload_stream(upload_db, consumed.upload_id, owner.id, _chunks(b"sent"))
    await store_upload_stream(upload_db, live.upload_id, owner.id, _chunks(b"current"))
    _consume(upload_db, owner.id, [consumed.upload_id])
    stale = datetime.now(tz=timezone.utc) - timedelta(seconds=1)
    expired_pending.expires_at = stale
    expired_stored.expires_at = stale
    upload_db.commit()
    stale_file = upload_root / expired_stored.upload_id
    consumed_file = upload_root / consumed.upload_id
    live_file = upload_root / live.upload_id
    # The expired-but-unspent payload is still resident -- nothing has retired
    # it, so only the sweep can take it. The consumed one's bytes went with its
    # consumption; it is the *row* the sweep is being measured on here.
    assert stale_file.exists()
    assert not consumed_file.exists()

    swept = sweep_expired_uploads(upload_db)

    assert swept == 3
    remaining = upload_db.query(OutboundUpload).all()
    assert [row.id for row in remaining] == [live.id]
    assert not stale_file.exists()
    assert not consumed_file.exists()
    assert live_file.read_bytes() == b"current"


@pytest.mark.asyncio
async def test_sweep_tolerates_a_payload_that_is_already_gone(upload_db, upload_root):
    owner = _user(upload_db, "missing-file-owner")
    slot = begin_upload(upload_db, owner.id, filename="vanished.txt")
    await store_upload_stream(upload_db, slot.upload_id, owner.id, _chunks(b"gone soon"))
    _consume(upload_db, owner.id, [slot.upload_id])
    # Consumption already freed it; removing it again is the double-unlink the
    # sweep has to tolerate whatever took the file first.
    (upload_root / slot.upload_id).unlink(missing_ok=True)

    swept = sweep_expired_uploads(upload_db)

    assert swept == 1
    assert upload_db.query(OutboundUpload).count() == 0


@pytest.mark.asyncio
async def test_sweep_honours_its_limit(upload_db, upload_root):
    owner = _user(upload_db, "sweep-limit-owner")
    for index in range(5):
        slot = begin_upload(upload_db, owner.id, filename=f"batch{index}.txt")
        await store_upload_stream(upload_db, slot.upload_id, owner.id, _chunks(b"spent"))
        _consume(upload_db, owner.id, [slot.upload_id])

    assert sweep_expired_uploads(upload_db, limit=2) == 2
    assert upload_db.query(OutboundUpload).count() == 3


def test_sweep_reclaims_an_orphan_payload_from_a_crashed_upload(upload_db, upload_root):
    """A process killed mid-PUT leaves bytes with no storage_path recorded."""
    owner = _user(upload_db, "crash-owner")
    slot = begin_upload(upload_db, owner.id, filename="interrupted.bin")
    upload_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    orphan = upload_root / slot.upload_id
    orphan.write_bytes(b"half a payload")
    slot.expires_at = datetime.now(tz=timezone.utc) - timedelta(seconds=1)
    upload_db.commit()
    assert slot.storage_path is None

    assert sweep_expired_uploads(upload_db) == 1
    assert not orphan.exists()


# --------------------------------------------------------------------------
# SUG-3: a retired slot's bytes go with the retirement, not with the next tick
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retiring_a_slot_frees_its_payload_without_waiting_for_the_sweep(
    upload_db, upload_root
):
    """A terminal slot must never be a terminal slot that still occupies the volume.

    Both retirements only moved the row and left the bytes for the sweep, up to
    a whole ``UPLOAD_SWEEP_INTERVAL_SECONDS`` later. On its own that is bounded
    and self-correcting, but it is also the window in which the owner may
    reserve -- and fill -- a completely fresh allowance, because a terminal slot
    stops charging their budget the instant it gets there. Freeing the bytes at
    the moment the row stops being spendable is what keeps "terminal" and "not
    on the volume" the same event.
    """
    owner = _user(upload_db, "prompt-free-owner")
    sent = begin_upload(upload_db, owner.id, filename="sent.bin")
    retired = begin_upload(upload_db, owner.id, filename="retired.bin")
    await store_upload_stream(upload_db, sent.upload_id, owner.id, _chunks(b"S" * 4096))
    await store_upload_stream(upload_db, retired.upload_id, owner.id, _chunks(b"R" * 4096))
    sent_file = upload_root / sent.upload_id
    retired_file = upload_root / retired.upload_id
    assert sent_file.stat().st_size == 4096
    assert retired_file.stat().st_size == 4096

    # A successful send: claim, read, consume.
    mark_consumed(upload_db, claim_uploads_for_send(upload_db, owner.id, [sent.upload_id]))
    # An ambiguous one: claim, then retire because nobody can say what happened.
    retire_unknown_uploads(
        upload_db, claim_uploads_for_send(upload_db, owner.id, [retired.upload_id])
    )

    # No sweep has run, and both payloads are already gone from the volume.
    assert not sent_file.exists(), "a consumed slot still occupied the volume"
    assert not retired_file.exists(), "a retired slot still occupied the volume"
    assert sum(path.stat().st_size for path in upload_root.iterdir()) == 0

    # The rows are exactly where they should be: consumed is deletable garbage,
    # send_unknown is retained so the owner can still be told what happened.
    states = {
        row.upload_id: row.state
        for row in upload_db.query(OutboundUpload).all()
    }
    assert states == {sent.upload_id: "consumed", retired.upload_id: _SEND_UNKNOWN}

    # And the sweep is untroubled by bytes that are already gone.
    assert sweep_expired_uploads(upload_db) == 1


@pytest.mark.asyncio
async def test_freeing_a_retired_payload_can_never_break_the_retirement(
    upload_db, upload_root, monkeypatch
):
    """Bookkeeping comes first; the unlink is best effort and must stay that way.

    ``settle_uploads`` may never propagate, so nothing it calls may turn a
    filesystem problem into a slot stranded in 'sending' forever.
    """
    from src.services import upload_service

    owner = _user(upload_db, "unlink-explodes-owner")
    slot = begin_upload(upload_db, owner.id, filename="stubborn.bin")
    await store_upload_stream(upload_db, slot.upload_id, owner.id, _chunks(b"X" * 1024))

    def explode(path):
        raise OSError("the volume is read-only")

    monkeypatch.setattr(upload_service, "_unlink_payload", explode)

    upload_service.settle_uploads(
        upload_db,
        claim_uploads_for_send(upload_db, owner.id, [slot.upload_id]),
        outcome="unknown",
    )

    row = upload_db.query(OutboundUpload).filter_by(upload_id=slot.upload_id).one()
    assert row.state == _SEND_UNKNOWN, "a failed unlink stranded the slot in 'sending'"


# --------------------------------------------------------------------------
# Defect B: a negative declared size must never credit the budget
# --------------------------------------------------------------------------


def test_a_negative_declared_size_is_refused_outright(upload_db, upload_root):
    """``size_bytes`` is a claim about how much of the budget a slot will take.

    Only the upper bound was ever checked, so a negative value flowed into the
    charge unexamined and *subtracted* from the owner's live total: one
    reservation for -1GiB and the byte budget stops binding for that tenant
    entirely.
    """
    owner = _user(upload_db, "negative-owner")

    with pytest.raises(HTTPException) as error:
        begin_upload(upload_db, owner.id, filename="credit.bin", declared_size=-1073741824)

    assert error.value.status_code == 400
    assert upload_db.query(OutboundUpload).count() == 0


def test_a_negative_declared_size_cannot_be_smuggled_past_the_budget(upload_db, upload_root, monkeypatch):
    """Defence in depth: even a row carrying one must not pay the owner.

    The guard above is the door; this is the accounting behind it. A row with a
    negative declared_size -- however it got there -- has to cost nothing rather
    than hand its owner free budget.
    """
    from src.services.upload_service import _charged_bytes, _committed_bytes

    monkeypatch.setattr(settings, "max_outbound_attachment_bytes", 4096)
    monkeypatch.setattr(settings, "max_pending_upload_bytes_per_user", 2 * 1024 * 1024)
    owner = _user(upload_db, "smuggler")
    honest = begin_upload(upload_db, owner.id, filename="honest.bin", declared_size=1024)
    hostile = begin_upload(upload_db, owner.id, filename="hostile.bin", declared_size=1024)
    hostile.declared_size = -1073741824
    upload_db.commit()

    assert _charged_bytes(hostile) >= 0
    # The honest slot's own charge still stands; the hostile one simply adds
    # nothing rather than wiping it out.
    assert _committed_bytes(upload_db, owner.id) >= _charged_bytes(honest)


def test_a_malformed_declared_sha256_is_refused_at_reservation(upload_db, upload_root):
    """A digest that cannot match anything only buys a 409 after the bytes land."""
    owner = _user(upload_db, "digest-owner")

    for bad in ("not-a-digest", "abc", "z" * 64, "0" * 63):
        with pytest.raises(HTTPException) as error:
            begin_upload(upload_db, owner.id, filename="x.bin", declared_sha256=bad)
        assert error.value.status_code == 400

    # A real one, in either case, is still accepted.
    upper = hashlib.sha256(b"fine").hexdigest().upper()
    assert begin_upload(upload_db, owner.id, filename="x.bin", declared_sha256=upper)



# --------------------------------------------------------------------------
# Every datetime this module writes has to live in one frame
# --------------------------------------------------------------------------


def _datetime_columns() -> set[str]:
    from sqlalchemy import DateTime

    return {
        column.name
        for column in OutboundUpload.__table__.columns
        if isinstance(column.type, DateTime)
    }


@contextmanager
def _audit_datetime_writes(db):
    """Every datetime value the module binds to a DateTime column, in both shapes.

    The ORM insert in ``begin_upload`` and the Core UPDATE every transition goes
    through. The point is the *frame*, not the value: the columns are naive
    ``DateTime``, ``_column_now()`` reads naive, and psycopg2 adapts an aware
    value as a timestamptz that Postgres then casts through the session
    ``TimeZone``. One of each means the TTL a slot actually gets depends on a
    server setting rather than on the code.
    """
    from sqlalchemy import event

    columns = _datetime_columns()
    seen: list[tuple[str, datetime]] = []

    def _record(name, value):
        if isinstance(value, datetime):
            seen.append((name, value))

    def _on_insert(mapper, connection, target):
        for name in columns:
            _record(name, getattr(target, name, None))

    def _on_execute(conn, clause, multiparams, params, execution_options):
        for key, bound in (getattr(clause, "_values", None) or {}).items():
            name = getattr(key, "name", str(key))
            if name in columns:
                _record(name, getattr(bound, "value", bound))

    bind = db.get_bind()
    event.listen(OutboundUpload, "before_insert", _on_insert)
    event.listen(bind, "before_execute", _on_execute)
    try:
        yield seen
    finally:
        event.remove(OutboundUpload, "before_insert", _on_insert)
        event.remove(bind, "before_execute", _on_execute)


def test_no_column_default_produces_an_aware_datetime():
    """The defaults are writes too, and they go through the same columns."""
    from sqlalchemy import DateTime

    aware = []
    for column in OutboundUpload.__table__.columns:
        default = column.default
        if not isinstance(column.type, DateTime) or default is None:
            continue
        value = default.arg({}) if default.is_callable else default.arg
        if isinstance(value, datetime) and value.tzinfo is not None:
            aware.append((column.name, value))
    assert aware == [], f"aware datetime defaults on naive DateTime columns: {aware}"


@pytest.mark.asyncio
async def test_every_datetime_written_to_a_column_is_naive_like_the_column(
    upload_db, upload_root
):
    """One frame for writes and reads, so the TTL is not a function of TimeZone.

    ``expires_at`` was written aware while ``_column_now()`` reads naive. Under
    ``SET TIME ZONE 'America/New_York'`` that made a freshly reserved slot be
    born already expired -- ``_live_pending`` excluded it, ``_owned_slot``
    answered 404, and the sweep removed it immediately; under ``Asia/Tokyo`` the
    900s TTL silently became 9h15m. UTC being the deployment default is what
    made it latent, not what made it correct.
    """
    from src.services.upload_service import _column_now

    owner = _user(upload_db, "frame-owner")

    with _audit_datetime_writes(upload_db) as seen:
        before = _column_now()
        slot = begin_upload(upload_db, owner.id, filename="framed.bin")
        after = _column_now()
        await store_upload_stream(upload_db, slot.upload_id, owner.id, _chunks(b"framed"))
        _consume(upload_db, owner.id, [slot.upload_id])

    assert seen, "the audit never observed a datetime write"
    aware = [(name, value) for name, value in seen if value.tzinfo is not None]
    assert aware == [], f"aware datetimes written to naive DateTime columns: {aware}"

    # And the value really is the TTL, measured in the frame the column reads.
    row = upload_db.query(OutboundUpload).filter_by(upload_id=slot.upload_id).one()
    ttl = timedelta(seconds=settings.outbound_upload_ttl_seconds)
    assert row.expires_at.tzinfo is None
    assert before + ttl <= row.expires_at <= after + ttl
    # Which is the same thing _expired() and the sweep judge it by, so a slot is
    # live for its whole TTL rather than being born expired.
    assert not _expired(row)
    assert _live_pending(upload_db, owner.id).count() == 0  # consumed, not expired
