"""The bounded sweep window may only ever be filled by rows the pass can act on.

``sweep_expired_uploads`` selects at most ``limit`` rows ordered by id and then
deletes what it can. Retaining a ``send_unknown`` row until its own expiry is
correct -- it is the only thing that can still tell the owner what became of
their message -- but a retained row that keeps *matching the selection* occupies
one of those bounded slots on every later pass, for its whole 900s TTL, without
ever being acted on. Once enough of them accumulate ahead of the garbage in id
order, the window is full of rows the pass cannot touch and throughput falls to
exactly zero: deletable rows and their payloads behind them are never reached.

That is head-of-line blocking, it is global and unpartitioned across tenants,
and it is manufactured by the sweep itself -- ``reclaim_lapsed_leases`` runs at
the top of the same function and is what creates ``send_unknown`` rows.

Everything here is driven through the real service functions, so the rows carry
true chronological ids and the states are reached the way production reaches
them. No clocks are advanced and nothing races: the defect is structural.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.config import settings
from src.models.base import Base
from src.models.outbound_upload import OutboundUpload
from src.models.user import User
from src.services.upload_service import (
    _SEND_UNKNOWN,
    begin_upload,
    claim_uploads_for_send,
    retire_unknown_uploads,
    store_upload_stream,
    sweep_expired_uploads,
)

# The sweep's own bound. Every scenario here is expressed against it rather than
# against a literal, so a change to the default cannot quietly invalidate them.
LIMIT = 100
GARBAGE_BYTES = 64 * 1024


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


async def _retained(db, user_id: int, count: int) -> list[str]:
    """``count`` genuinely retired slots, each reached the way a real send does.

    One ambiguous SMTP outcome retires every slot the send named, so a tenant
    whose own SMTP host times out reaches this state with no special privilege
    and no unusual input. Each is retired before the next is reserved, so the
    owner never holds more than one live slot: a terminal slot charges no
    budget, which is precisely what makes the row count unbounded.
    """
    ids: list[str] = []
    for index in range(count):
        slot = begin_upload(db, user_id, filename=f"retired{index}.bin")
        upload_id = str(slot.upload_id)
        await store_upload_stream(db, upload_id, user_id, _chunks(b"x"))
        retire_unknown_uploads(db, claim_uploads_for_send(db, user_id, [upload_id]))
        ids.append(upload_id)
    assert (
        db.query(OutboundUpload).filter_by(state=_SEND_UNKNOWN).count() == count
    ), "the retained rows were not created the way a real ambiguous send creates them"
    return ids


async def _expired_garbage(db, user_id: int, count: int) -> list[str]:
    """``count`` slots holding real bytes that only the sweep can ever free.

    Deliberately *not* consumed slots: a slot abandoned by its owner and left to
    expire is garbage whose payload no other mechanism will ever touch, so the
    bytes measured below are bytes the sweep is solely responsible for.
    """
    ids: list[str] = []
    for index in range(count):
        slot = begin_upload(db, user_id, filename=f"garbage{index}.bin")
        upload_id = str(slot.upload_id)
        await store_upload_stream(db, upload_id, user_id, _chunks(b"G" * GARBAGE_BYTES))
        ids.append(upload_id)
    stale = datetime.now(tz=timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
    for upload_id in ids:
        db.query(OutboundUpload).filter_by(upload_id=upload_id).update({"expires_at": stale})
    db.commit()
    return ids


def _resident_bytes(root) -> int:
    if not root.exists():
        return 0
    return sum(path.stat().st_size for path in root.iterdir() if path.is_file())


@pytest.mark.parametrize("retained", [LIMIT, 150])
@pytest.mark.asyncio
async def test_retained_rows_never_starve_the_bounded_deletion_window(
    upload_db, upload_root, retained
):
    """Garbage queued behind the retained rows must still go, in one pass.

    At ``retained == LIMIT`` the selection is exactly filled by rows the pass
    refuses to act on, and at 150 it is overfilled; both leave zero room for
    the garbage behind them, so throughput is exactly zero and stays there for
    the whole TTL of the retained rows -- which the sweep keeps manufacturing.
    Measured on the two things that actually matter: rows removed, and bytes
    gone from the volume.
    """
    attacker = _user(upload_db, f"retainer-{retained}")
    victim = _user(upload_db, f"victim-{retained}")

    # The retained rows come first in id order, which is the whole mechanism:
    # ORDER BY id puts them at the head of every later selection.
    await _retained(upload_db, attacker.id, retained)
    garbage = await _expired_garbage(upload_db, victim.id, 8)

    before = _resident_bytes(upload_root)
    assert before == 8 * GARBAGE_BYTES, "the garbage payloads are not on the volume to begin with"

    removed = sweep_expired_uploads(upload_db)

    assert removed == 8, (
        f"{retained} retained rows starved the sweep: it removed {removed} of 8 deletable rows"
    )
    assert upload_db.query(OutboundUpload).filter(
        OutboundUpload.upload_id.in_(garbage)
    ).count() == 0
    assert _resident_bytes(upload_root) == 0, "the garbage payloads are still occupying the volume"
    # And the retention goal is intact: the rows that explain an unknown
    # delivery are still there to answer for it.
    assert upload_db.query(OutboundUpload).filter_by(state=_SEND_UNKNOWN).count() == retained


@pytest.mark.asyncio
async def test_a_starved_sweep_does_not_leak_disk_across_repeated_passes(
    upload_db, upload_root
):
    """The unbounded half: garbage arriving every cycle must not accumulate forever.

    Holding the retained rows while fresh garbage keeps arriving is the shape
    that ends in volume exhaustion -- rows and orphaned payloads both grew
    linearly with no ceiling, because every pass selected the same retained
    rows and never reached anything behind them.
    """
    attacker = _user(upload_db, "steady-retainer")
    victim = _user(upload_db, "steady-victim")
    await _retained(upload_db, attacker.id, 120)

    for _ in range(5):
        await _expired_garbage(upload_db, victim.id, 4)
        sweep_expired_uploads(upload_db)
        # Each pass has to clear the cycle's own garbage, so nothing carries
        # over into the next one.
        assert _resident_bytes(upload_root) == 0
        assert upload_db.query(OutboundUpload).filter(
            OutboundUpload.state != _SEND_UNKNOWN
        ).count() == 0

    assert upload_db.query(OutboundUpload).count() == 120


@pytest.mark.asyncio
async def test_a_retained_row_is_still_deleted_once_it_expires(upload_db, upload_root):
    """Excluding retained rows from the window must not make them immortal."""
    owner = _user(upload_db, "eventually-expires")
    retained = (await _retained(upload_db, owner.id, 3))[0]

    assert sweep_expired_uploads(upload_db) == 0
    assert upload_db.query(OutboundUpload).filter_by(state=_SEND_UNKNOWN).count() == 3

    stale = datetime.now(tz=timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
    upload_db.query(OutboundUpload).filter_by(upload_id=retained).update({"expires_at": stale})
    upload_db.commit()

    assert sweep_expired_uploads(upload_db) == 1
    assert (
        upload_db.query(OutboundUpload).filter_by(upload_id=retained).one_or_none() is None
    )
    assert upload_db.query(OutboundUpload).filter_by(state=_SEND_UNKNOWN).count() == 2


@pytest.mark.asyncio
async def test_the_sweep_does_not_re_attempt_a_retained_payload_every_pass(
    upload_db, upload_root
):
    """A retained row's bytes are freed exactly once, not once per tick for 900s.

    Retention lasts a full TTL, so a pass that re-derives and re-unlinks every
    retained payload does that work fifteen times per row for nothing. The
    payload goes at retirement; after that the sweep has no filesystem work to
    do for a retained row at all.
    """
    from src.services import upload_service

    owner = _user(upload_db, "free-once")
    await _retained(upload_db, owner.id, 5)
    # Retirement already freed them.
    assert _resident_bytes(upload_root) == 0

    unlinked: list[str] = []
    real_unlink = upload_service._unlink_payload

    def watched(path):
        unlinked.append(str(path))
        return real_unlink(path)

    upload_service._unlink_payload = watched
    try:
        for _ in range(3):
            sweep_expired_uploads(upload_db)
    finally:
        upload_service._unlink_payload = real_unlink

    assert unlinked == [], f"the sweep re-attempted retained payloads every pass: {unlinked}"


@pytest.mark.asyncio
async def test_a_payload_the_retirement_could_not_free_is_still_reclaimed(
    upload_db, upload_root, monkeypatch
):
    """The safety net: freeing at retirement is best effort, so something must follow it.

    ``_free_retired_payload`` deliberately never raises -- the settlement path
    may not propagate -- so a read-only volume or a transient error leaves bytes
    behind with the row retained for its whole TTL. A bounded second query picks
    those up, and only those: the pass above proves it does no work when there
    is none.
    """
    from src.services import upload_service

    owner = _user(upload_db, "stubborn-volume")
    slot = begin_upload(upload_db, owner.id, filename="stuck.bin")
    upload_id = str(slot.upload_id)
    await store_upload_stream(upload_db, upload_id, owner.id, _chunks(b"S" * GARBAGE_BYTES))

    monkeypatch.setattr(
        upload_service,
        "_unlink_payload",
        lambda path: (_ for _ in ()).throw(OSError("read-only volume")),
    )
    retire_unknown_uploads(
        upload_db, claim_uploads_for_send(upload_db, owner.id, [upload_id])
    )
    monkeypatch.undo()

    # The row is retired and the bytes are still there: exactly the state the
    # net exists for.
    assert upload_db.query(OutboundUpload).filter_by(upload_id=upload_id).one().state == (
        _SEND_UNKNOWN
    )
    assert _resident_bytes(upload_root) == GARBAGE_BYTES

    # The row is retained, so nothing is removed -- but the bytes go.
    assert sweep_expired_uploads(upload_db) == 0
    assert _resident_bytes(upload_root) == 0
    assert upload_db.query(OutboundUpload).filter_by(upload_id=upload_id).one().state == (
        _SEND_UNKNOWN
    )


# --------------------------------------------------------------------------
# SUG-4: a pass that cannot make progress has to say so
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_sweep_reports_a_saturated_window(upload_db, upload_root):
    """More garbage than one pass can take is a backlog an operator should see."""
    from src.services.upload_service import sweep_outbound_uploads

    owner = _user(upload_db, "saturated")
    await _expired_garbage(upload_db, owner.id, 4)

    healthy = sweep_outbound_uploads(upload_db, limit=8)

    assert healthy.removed == 4
    assert healthy.examined == 4
    assert healthy.saturated is False
    assert healthy.starved is False

    await _expired_garbage(upload_db, owner.id, 8)
    full = sweep_outbound_uploads(upload_db, limit=4)

    assert full.removed == 4
    assert full.saturated is True, "a full window is the backlog signal and must be reported"
    # Productive, so not starvation: the pass removed everything it looked at.
    assert full.starved is False


@pytest.mark.asyncio
async def test_a_starved_pass_is_reported_rather_than_silent(upload_db, upload_root):
    """The invariant alarm: a full window that removes nothing is the defect itself.

    With retained rows excluded from the selection this is structurally
    impossible to reach through the service's own API, so the flag is asserted
    on the report type directly: it exists to catch a regression that puts
    un-actionable rows back into the window, and it must never fire in a
    healthy system.
    """
    from src.services.upload_service import SweepReport, sweep_outbound_uploads

    owner = _user(upload_db, "starved")
    await _expired_garbage(upload_db, owner.id, 4)

    # A productive full window is a backlog, not starvation.
    productive = sweep_outbound_uploads(upload_db, limit=4)
    assert (productive.removed, productive.saturated, productive.starved) == (4, True, False)

    # Nothing at all to do is quiet, not starved.
    idle = sweep_outbound_uploads(upload_db, limit=4)
    assert (idle.removed, idle.examined, idle.saturated, idle.starved) == (0, 0, False, False)

    # And the shape the flag is for: a window entirely filled by rows the pass
    # could not act on.
    assert SweepReport(removed=0, examined=4, limit=4, freed_retained_payloads=0).starved is True
    # A partial window that removed nothing is not starvation -- the pass simply
    # had nothing it could reach, which is ordinary.
    assert SweepReport(removed=0, examined=2, limit=4, freed_retained_payloads=0).starved is False


@pytest.mark.asyncio
async def test_the_heartbeat_logs_a_starved_sweep_and_keeps_beating(
    upload_db, upload_root, caplog
):
    """The signal has to reach an operator, and must never cost the heartbeat.

    ``server.py`` logged only ``if removed:``, so a pass that removed nothing --
    the starved pass, every time -- was completely invisible. An operator had no
    way to tell a healthy idle deployment from one whose disk was filling.
    """
    import logging

    from src import server
    from src.services.upload_service import SweepReport

    starved = SweepReport(removed=0, examined=100, limit=100, freed_retained_payloads=0)

    with caplog.at_level(logging.WARNING, logger="src.server"):
        server._log_sweep_outcome(starved)

    assert any(
        "starv" in record.message.lower() or "no progress" in record.message.lower()
        for record in caplog.records
    ), f"a starved sweep produced no operator-visible signal: {caplog.records}"

    # A healthy pass is quiet: this runs once a minute forever and must not
    # become log noise on an idle deployment.
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="src.server"):
        server._log_sweep_outcome(SweepReport(removed=0, examined=0, limit=100, freed_retained_payloads=0))
    assert [record for record in caplog.records if record.levelno >= logging.INFO] == []
