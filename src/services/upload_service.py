"""Outbound upload slots: a client PUTs bytes here, then names them when sending.

Bytes never enter the database and never enter the MCP conversation. They are
streamed to the data volume under a server-generated id, hashed on the way past,
and read back only for the owner who created the slot.

A slot moves pending -> receiving -> stored -> sending -> consumed, and every one
of those transitions is a conditional UPDATE rather than a read followed by a
write. Each HTTP request holds its own session, and both the PUT and the send
await the network in the middle of their work, so a check the application repeats
is a check two requests can both pass. One statement the database resolves is
the only thing that makes "one slot, one payload, one send" true.

The in-flight states ('receiving' and 'sending') carry a lease and a holder
token, because neither can end itself. A lapsed 'receiving' returns to 'pending'
-- nothing irreversible happened and its bytes were in a per-attempt .part file.
A lapsed 'sending' does NOT return to 'stored': the message may already be on
the wire and nothing on this side can tell. It retires to the terminal
``send_unknown``, which refuses any further send, stops charging the owner's
budgets, and has its payload freed at the moment of retirement exactly as
'consumed' does. Only the *row* lingers, until its own expiry, so the owner can
still be told what became of their message.

``send_unknown`` is reached in-process as well as by the lease, and for the same
reason: a send whose transport died after handing the bytes over, or whose
recipient set the server only partly accepted, has an outcome nobody can
establish. Only a send that *demonstrably* did not happen returns its slots to
'stored' -- see ``settle_uploads``.
"""

import contextlib
import hashlib
import logging
import os
import re
import secrets
from collections.abc import AsyncIterable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import ObjectDeletedError

from src.config import settings
from src.email import sanitize_filename
from src.email.attachment_handler import AttachmentHandler
from src.models.outbound_upload import OutboundUpload

logger = logging.getLogger(__name__)

UPLOAD_DIRECTORY = "outbound-uploads"
# Enough of the leading bytes for the existing signature sniffer; the rest of the
# body is written straight through and never retained.
_SNIFF_BYTES = 512
# A slot whose delivery outcome can never be established. A process that died
# between putting a message on the wire and recording that it had is the one
# case where nothing on this side knows whether the MTA accepted it, so the slot
# is retired here rather than being recycled into a sendable state by a timer:
# re-sending confidential mail on a guess is worse than telling the owner the
# outcome is unknown. Terminal, like 'consumed', and swept the same way.
_SEND_UNKNOWN = "send_unknown"
_SEND_UNKNOWN_DETAIL = (
    "This upload was already claimed by a send that did not report its outcome, so "
    "whether the message was delivered is unknown; it will not be sent again. "
    "Reserve a new upload slot if you want to send these bytes."
)
# Slots nothing will ever spend again. Their payloads are swept with their rows,
# and they stop charging their owner's byte and slot budgets the moment they get
# here -- an honest "unknown" must not also cost the owner the volume.
_TERMINAL_STATES = ("consumed", _SEND_UNKNOWN)
# A slot holding a claim on the volume, whether or not its bytes have landed yet.
_LIVE_STATES = ("pending", "receiving", "stored", "sending")
# How often the owner's live byte total is re-read while a body is arriving. The
# reservation charge is a promise about one slot; this is what stops several
# slots being filled at once from overrunning the budget between them.
_BUDGET_RECHECK_BYTES = 1024 * 1024
_ATTEMPT_SUFFIX = ".part"
# How long an in-flight claim ('receiving' or 'sending') holds a slot before it
# may be reclaimed. Neither state can end itself: a worker killed between the
# claim and the promotion leaves the slot -- and its share of both the byte and
# the slot budget -- held until the 900s TTL, refusing even the owner's own
# honest retry. A lease turns that into a bounded outage.
#
# 300s is one third of outbound_upload_ttl_seconds, so a strand can be reclaimed
# and retried twice inside a slot's life. It cannot cut a legitimate operation
# short in either state:
#   - 'receiving' renews on every budget recheck, so a client would have to
#     stall a full five minutes without delivering _BUDGET_RECHECK_BYTES -- less
#     than 28 kbit/s sustained -- before its claim lapsed.
#   - 'sending' is one SMTP conversation on a socket with a 10s timeout, or one
#     IMAP APPEND under a 60s command timeout: an order of magnitude inside it.
_LEASE_SECONDS = 300
# Both in-flight states, and what a lapsed one falls back to. Receiving returns
# the slot for re-upload: nothing irreversible has happened and the bytes live
# in the attempt's own .part file. Sending does NOT return to 'stored' -- the
# message may already be on the wire, and a timer that makes it sendable again
# is a timer that delivers confidential mail twice. It retires to a terminal
# state that reports the outcome as unknown instead.
_LEASE_FALLBACK = {"receiving": "pending", "sending": _SEND_UNKNOWN}


def _now() -> datetime:
    """The module's clock. Never written to a column directly -- see below."""
    return datetime.now(tz=timezone.utc)


def _column_now() -> datetime:
    """``_now()`` in the shape the DateTime columns here actually hold.

    Postgres and SQLite both hand back naive datetimes from a DateTime column,
    so every comparison against one belongs in the same frame. psycopg2 will
    adapt an aware datetime into a timestamptz, which Postgres then compares
    against a timestamp by *assuming* the session TimeZone -- right only while
    that happens to be UTC, which is a setting rather than an invariant.

    This is the only thing that should ever produce a value for one of those
    columns, whether it is written or compared: ``expires_at`` was once written
    through ``_now()`` while everything read it through here, and a slot
    reserved under ``America/New_York`` was consequently born already expired.
    """
    return _now().replace(tzinfo=None)


def _lease_deadline() -> datetime:
    return _column_now() + timedelta(seconds=_LEASE_SECONDS)


def _upload_root() -> Path:
    """The private directory outbound payloads live in, created on first use."""
    root = Path(settings.data_dir) / UPLOAD_DIRECTORY
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def _storage_path(upload_id: str) -> Path:
    # The id is generated by secrets.token_urlsafe, so it cannot contain a path
    # separator or a traversal segment. The client filename never appears here.
    return _upload_root() / upload_id


def _attempt_path(upload_id: str, holder: str | None = None) -> Path:
    """A scratch file belonging to one PUT attempt and to nothing else.

    The final destination is shared by every attempt on a slot, so writing
    straight to it lets one attempt overwrite -- or, on its failure path, delete
    -- a payload another attempt already validated. An attempt that owns its own
    file can only ever destroy its own work.

    Named after the claim it belongs to rather than after fresh randomness, and
    that is what makes the reclaim able to clean up after a dispossessed
    attempt. A lease token is 16 random bytes minted per claim and never
    reused, so ``{upload_id}.{holder}.part`` identifies one attempt exactly:
    the reclaim can remove the file of the claim it just took without any
    chance of touching the file of the successor that takes the slot next,
    which necessarily holds a different token. Without that identity the only
    way to find the file was a glob over the slot, which cannot tell the two
    apart -- see ``_reclaim_lapsed``.

    Falls back to fresh randomness when the attempt never learned its own token
    (a row swept between the claim and the read). Such an attempt fails at its
    first renewal anyway; the fallback only keeps its scratch file private in
    the meantime.

    THAT FALLBACK'S NAME IS NOT DERIVABLE FROM THE ROW, so the targeted reclaim
    in ``_reclaim_lapsed`` can never find the file: it looks up the row's
    lease_token, and the file is named after randomness the row never saw.
    Only the sweep's glob over ``{upload_id}.*`` removes it, which means not
    before the row's own TTL. Narrow -- reaching this needs the row to vanish
    inside the two statements between the won claim and the token read, and
    such an attempt cannot write much before its first renewal refuses it --
    but it is a real bounded leak rather than an impossible one.
    """
    return _upload_root() / f"{upload_id}.{holder or secrets.token_hex(8)}{_ATTEMPT_SUFFIX}"


def _discard_attempt_file(path: Path) -> None:
    """Free one attempt's scratch bytes now, even if somebody holds it open.

    Truncate *then* unlink. Unlinking alone only removes the directory entry:
    a stalled attempt still parked in ``async for chunk`` keeps its handle, and
    the inode -- and every block behind it -- stays allocated on the volume for
    as long as that request lives. Truncating first releases the blocks
    immediately, and the dispossessed writer's later writes land in a sparse
    hole, so what it can still occupy is bounded by what it writes between
    waking up and discovering it lost (one budget recheck).

    For ``.part`` files only, and that restriction is load-bearing. The final
    destination may be open in a reader: ``load_uploads`` reads it whole and
    then checks the size and digest, so truncating underneath that reader turns
    a payload into a short one. It would be *caught* -- the verification is
    there for exactly this -- but the right outcome for a payload being swept
    is that the row is going too, not that a live send fails on a partial read.
    Removing the directory entry leaves an open reader's bytes intact, which is
    what the destination wants and what a scratch file specifically must not
    have.
    """
    with contextlib.suppress(OSError):
        # Already gone, or never created. The unlink below is tolerant too.
        os.truncate(path, 0)
    _unlink_payload(path)


def _unlink_payload(path: Path) -> None:
    """Remove a payload, tolerating one that is already gone.

    A half-swept slot must not block the sweep, and the row is the record that
    matters.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        logger.warning("Could not remove outbound upload payload %s: %s", path, error)


def _new_lease_token() -> str:
    """An identity for one in-flight claim, unguessable and never reused."""
    return secrets.token_hex(16)


def _live_pending(db: Session, user_id: int):
    """Slots that still hold a claim on the volume for this owner.

    Anything expired is already garbage the sweep will take, and a terminal slot
    -- sent, or claimed by a send that never reported -- will never be spent
    again, so none of them should count against a new request. An honest
    "delivery unknown" must not also cost the owner their slot allowance.
    """
    return db.query(OutboundUpload).filter(
        OutboundUpload.owner_user_id == user_id,
        OutboundUpload.state.in_(_LIVE_STATES),
        OutboundUpload.expires_at > _column_now(),
    )


def _charged_bytes(upload: OutboundUpload) -> int:
    """What a live slot costs its owner's budget right now.

    A slot that has been written is charged what actually arrived. One still
    receiving is charged the greater of what it promised and what has landed so
    far, because a body in flight is already occupying the volume and a
    declared_size is a hint the client is free to understate. One that has not
    started is charged what it promised -- and a slot that promised nothing is
    charged the most it could still be filled with, because otherwise omitting
    an optional size_bytes is a way to reserve space for free and the per-user
    budget never binds at all.

    Never negative: a declared size is attacker-supplied, and a charge that can
    go below zero is a charge that pays its owner budget instead of spending it.
    """
    if upload.size is not None:
        return max(0, upload.size)
    promised = upload.declared_size if upload.declared_size is not None else None
    arrived = upload.received_bytes or 0
    if upload.state == "receiving":
        # The running total the stream writes as it goes. Without it, N slots
        # streaming at once are mutually invisible and each is charged only its
        # declaration, so the budget binds against promises rather than bytes.
        return max(0, promised if promised is not None else settings.max_outbound_attachment_bytes, arrived)
    if promised is not None:
        return max(0, promised)
    return settings.max_outbound_attachment_bytes


def _committed_bytes(db: Session, user_id: int, *, excluding: str | None = None) -> int:
    """The owner's live byte total, optionally ignoring one slot of their own."""
    query = _live_pending(db, user_id)
    if excluding is not None:
        query = query.filter(OutboundUpload.upload_id != excluding)
    total = sum(_charged_bytes(upload) for upload in query.all())
    # Reading opened a transaction, and this runs between chunks of a body that
    # may still take a while. Nothing of ours is pending, so let the snapshot go
    # rather than hold a read lock across the rest of the upload.
    db.rollback()
    return total


def _enforce_pending_budget(db: Session, user_id: int, declared_size: int | None) -> None:
    """Keep one owner from parking the data volume full of slots nobody sends."""
    outstanding = _live_pending(db, user_id).all()
    if len(outstanding) >= settings.max_pending_uploads_per_user:
        raise HTTPException(
            status_code=429,
            detail=f"At most {settings.max_pending_uploads_per_user} pending uploads are allowed",
        )
    committed = sum(_charged_bytes(upload) for upload in outstanding)
    claim = declared_size if declared_size is not None else settings.max_outbound_attachment_bytes
    if committed + claim > settings.max_pending_upload_bytes_per_user:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Pending uploads would exceed the {settings.max_pending_upload_bytes_per_user} "
                "byte per-user budget"
            ),
        )


_HEX64 = re.compile(r"\A[0-9a-fA-F]{64}\Z")

# The domain half of a generated Content-ID. A fixed constant on purpose: the id
# is handed to the client when it reserves the slot, the client writes it into
# its HTML, and the send re-derives it up to 900 seconds later. Anything that
# can change in between -- a restart onto a different public_base_url, a proxy
# moved to another host -- would turn the caller's cid: reference into a
# dangling one, and the composer's demotion rule then delivers the image as an
# ordinary attachment with nothing anywhere reporting an error. The uniqueness
# that matters is already in the local part, which is 32 bytes of
# secrets.token_urlsafe. .invalid is reserved by RFC 2606 precisely so a name
# not meant to resolve cannot collide with one that does.
#
# Short on purpose too. "Content-ID: <" plus the id plus ">" has to stay inside
# the 78 column limit or the generator folds the value onto a continuation
# line; a longer name here did exactly that. Compliant parsers unfold, but real
# mailers emit this header on one line and matching them is the whole point --
# ``test_the_content_id_header_is_never_folded`` holds the budget.
#
# THE MARGIN IS FOUR CHARACTERS. Measured:
#   "Content-ID: <"  13
#   secrets.token_urlsafe(32)  43
#   "@" + "mailindex.invalid"  18
#   ">"  1
#   = 75 columns, and the generator first folds at 79.
# So four more characters anywhere in this constant -- or in the header name,
# or in the id's own length -- silently reintroduces folding. The break is at
# the identical +4 on BOTH builders, not worse on the draft side.
#
# THE FOLD IS THE WORSE FAILURE, and it is the one four characters buys. From
# 79 columns to about 91 both builders PLAIN-FOLD: the value parses back as
# ``' <id>'`` with a leading space, so the cid: URL cannot resolve against it
# and the image silently demotes. Only around 92+ does ``EmailMessage``
# additionally RFC 2047-encode the header, which no real mailer does to a
# Content-ID -- and which, perversely, still round-trips correctly through a
# compliant parser. Anything that grows this must re-measure rather than assume
# headroom.
_CID_HOST = "mailindex.invalid"


def content_id_for(upload_id: str) -> str:
    """The Content-ID an inline part built from this slot will carry.

    Derived from the slot rather than stored, so the answer is the same every
    time it is asked for: the client is told the id when it reserves the slot,
    writes ``cid:<id>`` into its HTML, and the send re-derives the same id when
    it builds the part. Nothing the client supplies reaches the header, and the
    id inherits the slot's uniqueness -- an upload_id is 32 bytes of
    ``secrets.token_urlsafe``, whose alphabet is already a legal local part, so
    the result always satisfies the composer's validator.
    """
    return f"{upload_id}@{_CID_HOST}"


def plan_upload_dispositions(
    upload_ids: Iterable[str] | None,
    inline_upload_ids: Iterable[str] | None,
) -> tuple[list[str], list[str], list[str]]:
    """Normalise the two lists a caller named, and return them plus their union.

    Each list is deduplicated -- naming one slot twice is the same request as
    naming it once -- but a slot named in *both* is refused rather than folded
    into one disposition. Embedded in the body and appended at the bottom are
    different messages, the bytes can only be spent once, and quietly picking
    one of the two would send something the caller did not ask for.

    The union is what every shared check is then made against, so the count cap,
    the byte cap and the claim all see one message rather than two lists.
    """
    ordinary = list(dict.fromkeys(upload_ids or []))
    inline = list(dict.fromkeys(inline_upload_ids or []))
    both = [upload_id for upload_id in ordinary if upload_id in set(inline)]
    if both:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{len(both)} upload id(s) appear in both upload_ids and "
                "inline_upload_ids. A slot holds one payload and is spent once, so "
                "name it in exactly one: inline_upload_ids to embed it in body_html "
                "via its cid, upload_ids to attach it at the bottom."
            ),
        )
    return ordinary, inline, ordinary + inline


def outbound_attachments(loaded: Iterable[dict], inline_upload_ids: Iterable[str]) -> list[dict]:
    """Turn loaded payloads into the part descriptions both builders accept.

    The one place the send path and the draft path agree on what a message is
    made of, so a draft and the sent copy of it cannot render differently.

    ``content_type`` is what the server sniffed when the bytes arrived, never
    what anyone declared. ``content_id`` is set only for an inline part, and is
    derived from the slot -- the same value the client was handed when it
    reserved it, so the ``cid:`` URL it wrote into the HTML resolves.
    """
    inline = set(inline_upload_ids)
    parts = []
    for item in loaded:
        embedded = item["upload_id"] in inline
        parts.append(
            {
                "data": item["data"],
                "filename": item["filename"],
                "content_type": item["content_type"],
                "disposition": "inline" if embedded else "attachment",
                "content_id": content_id_for(item["upload_id"]) if embedded else None,
            }
        )
    return parts


def begin_upload(
    db: Session,
    user_id: int,
    *,
    filename: str,
    declared_size: int | None = None,
    declared_sha256: str | None = None,
) -> OutboundUpload:
    """Reserve a slot the caller may PUT bytes into exactly once."""
    if declared_size is not None:
        # Both ends of the range. Only the upper bound was ever checked, and a
        # negative claim is not merely nonsense: it is charged verbatim against
        # the owner's live total, so one reservation for -1GiB would pay the
        # tenant enough budget to stop it binding at all.
        if declared_size < 0:
            raise HTTPException(status_code=400, detail="size_bytes cannot be negative")
        if declared_size > settings.max_outbound_attachment_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Upload exceeds the {settings.max_outbound_attachment_bytes} byte outbound attachment limit",
            )
    if declared_sha256 is not None and not _HEX64.match(declared_sha256):
        # A digest that cannot match anything buys nothing but a 409 after the
        # bytes have already been written; say so before they are sent.
        raise HTTPException(status_code=400, detail="sha256 must be 64 hexadecimal characters")
    _enforce_pending_budget(db, user_id, declared_size)
    upload = OutboundUpload(
        upload_id=secrets.token_urlsafe(32),
        owner_user_id=user_id,
        filename=sanitize_filename(filename),
        declared_size=declared_size,
        declared_sha256=declared_sha256,
        state="pending",
        # _column_now(), not _now(): the column is a naive DateTime and every
        # read of it -- _expired(), _live_pending(), the sweep -- happens in
        # that frame. Writing an aware value here made psycopg2 adapt it as a
        # timestamptz that Postgres cast through the session TimeZone, so the
        # TTL a slot actually got was a function of a server setting: under
        # America/New_York the slot was born already expired, under Asia/Tokyo
        # the 900s became 9h15m. UTC being the deployment default is what made
        # that latent, not what made it correct.
        expires_at=_column_now() + timedelta(seconds=settings.outbound_upload_ttl_seconds),
    )
    db.add(upload)
    db.commit()
    db.refresh(upload)
    return upload


def _owned_slot(db: Session, upload_id: str, user_id: int) -> OutboundUpload:
    """Resolve a slot for its owner only.

    A foreign or unknown id is the same 404: an id that is not yours must not be
    confirmed to exist. An expired one is treated the same way, because the grant
    it represented is gone.
    """
    upload = (
        db.query(OutboundUpload)
        .filter(
            OutboundUpload.upload_id == upload_id,
            OutboundUpload.owner_user_id == user_id,
        )
        .first()
    )
    if not upload or _expired(upload):
        raise HTTPException(status_code=404, detail="Upload slot not found")
    return upload


def _expired(upload: OutboundUpload) -> bool:
    """Whether a slot's grant has lapsed, judged in the column's own frame.

    The same comparison ``_live_pending`` and the sweep make in SQL, so the
    three cannot disagree about whether one row is live. A value that somehow
    arrived aware is normalised rather than trusted: comparing an aware and a
    naive datetime raises, and this runs on the read path of every request.
    """
    expires_at = upload.expires_at
    if expires_at is None:
        return False
    # Postgres and SQLite both hand back naive datetimes from a DateTime column.
    if expires_at.tzinfo is not None:
        expires_at = expires_at.astimezone(timezone.utc).replace(tzinfo=None)
    return expires_at <= _column_now()


def _slot_id(upload: OutboundUpload) -> str | None:
    """The slot's id, or nothing if the sweep took the row out from under us.

    A commit expires the instance, so reading an attribute goes back to the
    database. Between a claim and the transition that follows it the row can be
    swept, and a send that already succeeded must not turn into a 500 because a
    housekeeping task removed the record of the slot it used.
    """
    try:
        return str(upload.upload_id)
    except ObjectDeletedError:
        return None


def _claim(
    db: Session,
    upload_id: str,
    expected: str,
    target: str,
    *,
    holder: str | None = None,
    **values,
) -> bool:
    """Move one slot between states, or report that somebody else got there.

    This is the whole concurrency story of the module. ``UPDATE ... WHERE
    state = :expected`` is a single statement, so of two requests racing for the
    same slot exactly one can see a rowcount of 1 -- no matter how many times
    either of them yields to the event loop before or afterwards.

    Entering an in-flight state takes a lease and a fresh holder token; leaving
    one drops both, so a claim nobody ever ends becomes reclaimable rather than
    permanent. ``holder`` narrows the WHERE to one particular claim: a state
    predicate identifies a state and not an owner, so without it an attempt
    whose lease was reclaimed minutes ago still matches ``state = 'receiving'``
    and can act on the claim its successor now holds.
    """
    entering = target in _LEASE_FALLBACK
    values.setdefault("lease_expires_at", _lease_deadline() if entering else None)
    # A new claim gets a new identity; leaving one clears it, so the token can
    # never be replayed against the next holder of the same slot.
    values.setdefault("lease_token", _new_lease_token() if entering else None)
    conditions = [
        OutboundUpload.upload_id == upload_id,
        OutboundUpload.state == expected,
    ]
    if holder is not None:
        conditions.append(OutboundUpload.lease_token == holder)
    changed = db.execute(
        update(OutboundUpload.__table__).where(*conditions).values(state=target, **values)
    ).rowcount
    # Committed immediately: a claim another request cannot see is not a claim,
    # and the whole point is that it is visible before the next await.
    db.commit()
    return bool(changed)


def _free_retired_payload(db: Session, upload_id: str) -> bool:
    """Free the bytes of a slot that has just become terminal, now.

    A terminal slot will never be sent again, so the instant its row stops
    being spendable its payload has no business occupying the volume. Leaving
    that to the sweep meant a retired slot's bytes stayed resident for up to a
    whole sweep interval -- and a terminal slot stops charging its owner's
    budgets the moment it gets there, so within that window the owner can
    reserve and fill a fresh full allowance while the old bytes are still on
    the disk. Retiring and freeing are one event here instead.

    Only ever called after a conditional UPDATE this caller won, so the slot is
    this caller's to free and cannot be a successor's. Unlinked rather than
    truncated, exactly as the sweep does it: a reader that already opened the
    payload keeps its bytes, and ``load_uploads`` verifies what it read either
    way.

    ``storage_path`` is cleared once the file is confirmed gone, and that is
    what the sweep's retained-payload net keys off. The net therefore selects
    nothing at all in a healthy deployment, instead of re-deriving and
    re-unlinking every retained payload on every one of the ~15 passes a 900s
    retention spans.

    Never raises, and reports whether the bytes actually went. This runs on the
    settlement path, which must not propagate: the bytes are already spent, and
    turning a filesystem problem into an exception here would strand the slot
    in 'sending' for the whole TTL and replace the caller's real outcome with a
    bookkeeping error. A payload this fails to remove keeps its
    ``storage_path``, so the sweep picks it up -- which is what makes falling
    through safe.
    """
    try:
        recorded = db.query(OutboundUpload.storage_path).filter_by(upload_id=upload_id).scalar()
        db.rollback()
        # Fall back to the id-derived path: a row can carry bytes it never
        # recorded a path for, and the path is derived from the id anyway.
        path = Path(recorded) if recorded else _storage_path(upload_id)
        _unlink_payload(path)
        if path.exists():
            # _unlink_payload swallowed an OSError. Leave storage_path alone so
            # the sweep's net sees there is still something to collect.
            return False
        if recorded is not None:
            db.execute(
                update(OutboundUpload.__table__)
                .where(OutboundUpload.upload_id == upload_id)
                .values(storage_path=None)
            )
            db.commit()
        return True
    except Exception as error:
        logger.warning(
            "Could not free the payload of retired upload %s (%s: %s); the sweep will take it",
            upload_id,
            type(error).__name__,
            error,
        )
        with contextlib.suppress(Exception):
            db.rollback()
        return False


def _claim_holder(db: Session, upload_id: str) -> str | None:
    """The token of whichever claim currently holds this slot.

    Read straight after the conditional UPDATE that created it. The claim is
    already committed and already exclusive, so this is a read of something only
    one attempt can have written -- it is not a check that decides anything.
    """
    token = db.query(OutboundUpload.lease_token).filter_by(upload_id=upload_id).scalar()
    db.rollback()
    return token


def _renew(db: Session, upload_id: str, holder: str | None, received: int) -> bool:
    """Record how much of a body has landed, and push this holder's lease out.

    Two jobs, one statement, because they share a cadence and a condition. The
    running total is what lets concurrent uploads see each other at all --
    ``size`` is only set by the final promote, so without this a slot still
    receiving is charged its declaration and N of them are mutually invisible.
    The renewal is what keeps an upload that is genuinely progressing from being
    reclaimed underneath itself.

    Fenced on the holder as well as the state, and that is the load-bearing
    part: ``state = 'receiving'`` is satisfied by any receiver, so a dispossessed
    attempt used to renew successfully, overwrite its successor's running total
    with its own -- charging the budget against the wrong body -- and never
    learn it had lost. Matching the token is how it finds out, before it writes
    another byte or promotes over somebody else's slot.
    """
    if holder is None:
        # No identity, no renewal. An attempt that never saw its own token
        # cannot prove the claim is still its own, and ``lease_token == None``
        # would compile to ``IS NULL`` -- which is exactly the shape of a row
        # nobody holds.
        return False
    changed = db.execute(
        update(OutboundUpload.__table__)
        .where(
            OutboundUpload.upload_id == upload_id,
            OutboundUpload.state == "receiving",
            OutboundUpload.lease_token == holder,
        )
        .values(received_bytes=received, lease_expires_at=_lease_deadline())
    ).rowcount
    db.commit()
    return bool(changed)


def _reclaim_lapsed(db: Session, upload_id: str) -> None:
    """Return one slot whose in-flight claim has lapsed, if it has.

    Conditional on the lease as well as on the state, in the same single
    statement as every other transition here: a reclaim that read the deadline
    and then wrote would be exactly the race the claims exist to prevent, and it
    would be able to take a slot from a live uploader about to promote. The
    holder discovers it lost because the token it is fenced on is cleared here,
    so its next renewal and its promotion both fail and it aborts.

    Where the two in-flight states go is not symmetric, and deliberately so.
    'receiving' has done nothing irreversible -- its bytes are in a ``.part``
    file -- so the slot returns to 'pending' and is usable again. 'sending' has
    an irreversible side effect: a process that died between the wire and the
    bookkeeping leaves delivery genuinely *unknown*, and nothing here can tell
    a message that never left from one the MTA already accepted. Recycling it
    into a sendable state is how the same confidential mail reached an MTA
    twice from one user intent, so it goes to a terminal state that says so
    instead.

    A reclaimed 'receiving' also has its bytes removed here, and that is not
    tidiness. Zeroing ``received_bytes`` alone rested on the premise that "the
    attempt that was writing them unlinks its own file", which holds for an
    attempt that died or disconnected and fails for the one the lease actually
    exists to dispossess: a merely *stalled* client is still parked in
    ``async for chunk``, its ``except`` has not run, and it still holds an open
    ``.part``. Its bytes then occupied the volume while the row said the owner
    was using none of them -- a slot refillable from scratch every lease period,
    multiplying the per-user byte budget by however many reclaims fit inside a
    slot's TTL.

    A reclaimed 'sending' has its payload removed too, for the same reason the
    in-process retirement does it: the slot is terminal the moment it lands in
    ``send_unknown`` and stops charging the owner's budgets there, so its bytes
    must not outlive the charge by a sweep interval. Only the row is kept.

    Removing the file is safe precisely because the attempt file is named after
    the claim's lease token: the token read here is the one this statement has
    just cleared, so the file removed belongs to the attempt that just lost and
    can never be the file of a successor, whose claim necessarily carries a
    different token. The order matters too -- the token is read *before* the
    UPDATE, because the UPDATE is what erases it.
    """
    for state, fallback in _LEASE_FALLBACK.items():
        # Read first: the UPDATE below clears the token, and the token is the
        # only thing that names the dispossessed attempt's file. Nothing is
        # decided on this value -- the UPDATE still decides whether there was
        # anything to reclaim -- so reading it outside the statement is not the
        # read-then-write race the claims exist to prevent.
        holder = (
            db.query(OutboundUpload.lease_token)
            .filter(OutboundUpload.upload_id == upload_id, OutboundUpload.state == state)
            .scalar()
        )
        db.rollback()
        changed = db.execute(
            update(OutboundUpload.__table__)
            .where(
                OutboundUpload.upload_id == upload_id,
                OutboundUpload.state == state,
                OutboundUpload.lease_expires_at.isnot(None),
                OutboundUpload.lease_expires_at <= _column_now(),
            )
            .values(
                state=fallback,
                lease_expires_at=None,
                # Clearing the token is what dispossesses the old holder: every
                # write that could extend or end its claim is fenced on it, so a
                # slot back in 'pending' cannot be renewed -- or promoted into --
                # by whoever was holding it before.
                lease_token=None,
                # A slot returning to 'pending' has no bytes to its name; the
                # bytes themselves are removed below, in the same breath, so
                # the two can never disagree.
                **({"received_bytes": 0} if fallback == "pending" else {}),
            )
        ).rowcount
        db.commit()
        if changed and fallback == "pending" and holder:
            # Only this claim's own file, identified by the token the UPDATE
            # just erased. A successor's attempt file carries a different
            # token, so an interleaved new attempt on the same slot is
            # untouched however the two overlap.
            _discard_attempt_file(_attempt_path(upload_id, holder))
        if changed and fallback == _SEND_UNKNOWN:
            # Retired, so the payload goes with it -- the same event the
            # in-process retirement makes of it. The row stays until its own
            # expiry so the owner can still be told what became of the message.
            _free_retired_payload(db, upload_id)


def reclaim_lapsed_leases(db: Session, *, limit: int = 100) -> int:
    """Sweep in-flight claims nobody is holding any more.

    The expiry sweep deliberately will not touch these -- a slot mid-upload has
    not expired -- so without this a strand keeps its share of the owner's byte
    and slot budgets for the whole TTL. Each row is reclaimed by its own
    conditional UPDATE rather than in one bulk statement, so a slot whose holder
    renews between the read and the write keeps it.
    """
    lapsed = (
        db.query(OutboundUpload.upload_id)
        .filter(
            OutboundUpload.state.in_(tuple(_LEASE_FALLBACK)),
            OutboundUpload.lease_expires_at.isnot(None),
            OutboundUpload.lease_expires_at <= _column_now(),
        )
        .order_by(OutboundUpload.id)
        .limit(limit)
        .all()
    )
    db.rollback()
    reclaimed = 0
    for (upload_id,) in lapsed:
        before = db.query(OutboundUpload.state).filter_by(upload_id=upload_id).scalar()
        _reclaim_lapsed(db, upload_id)
        after = db.query(OutboundUpload.state).filter_by(upload_id=upload_id).scalar()
        db.rollback()
        if before != after:
            reclaimed += 1
    return reclaimed


async def store_upload_stream(
    db: Session,
    upload_id: str,
    user_id: int,
    chunks: AsyncIterable[bytes],
) -> dict:
    """Stream a request body onto the volume, hashing and measuring as it lands.

    The body is never held in memory: it is written through as it arrives and the
    digest is computed incrementally, so the size limit is enforced against what
    has actually been received rather than against a buffer.
    """
    upload = _owned_slot(db, upload_id, user_id)
    if upload.state != "pending":
        # An in-flight claim nobody is holding any more is the owner's own slot,
        # stuck: a process killed between the claim and the promotion leaves it
        # there for the whole TTL, refusing this honest retry. Reclaim it before
        # judging the state -- atomically, so a live uploader keeps it.
        _reclaim_lapsed(db, upload_id)
        db.refresh(upload)
    if upload.state == _SEND_UNKNOWN:
        # Terminal, and it got there by being claimed for a send. Refilling it
        # would put fresh bytes behind an id whose delivery outcome is already
        # unknown, so it is retired rather than reusable.
        raise HTTPException(status_code=409, detail=_SEND_UNKNOWN_DETAIL)
    if upload.state != "pending":
        # One slot, one payload: a second PUT must not be able to swap the bytes
        # out from under a send that already validated them.
        raise HTTPException(status_code=409, detail="Upload slot has already received its bytes")
    declared_size = upload.declared_size
    declared_sha256 = upload.declared_sha256
    filename = upload.filename

    # Claimed before the first byte is read. Awaiting the body yields to the loop
    # many times, so the check above is worth nothing on its own: two PUTs
    # carrying the same valid grant both pass it. The conditional UPDATE is what
    # decides between them, and the loser is told the slot is taken.
    if not _claim(db, upload_id, "pending", "receiving", received_bytes=0):
        raise HTTPException(status_code=409, detail="Upload slot has already received its bytes")
    # The identity of the claim this attempt just won. Every later write it makes
    # -- each renewal, and the promotion -- is fenced on it, so if the claim is
    # reclaimed and handed to a successor, this attempt fails rather than acting
    # on somebody else's slot. It also names this attempt's scratch file, which
    # is what lets a reclaim free the bytes of a dispossessed attempt without
    # any risk of touching its successor's.
    holder = _claim_holder(db, upload_id)

    destination = _storage_path(upload_id)
    attempt = _attempt_path(upload_id, holder)
    limit = settings.max_outbound_attachment_bytes
    digest = hashlib.sha256()
    total = 0
    head = b""
    landed = False

    def _budget_allowance() -> int:
        """The most this attempt may still take, against the owner's live total.

        The reservation charge is a promise about one slot; this is what stops a
        set of slots being filled at once from overrunning the budget between
        them, and it is the only byte check that happens at PUT time at all.
        """
        spent_elsewhere = _committed_bytes(db, user_id, excluding=upload_id)
        return max(0, min(limit, settings.max_pending_upload_bytes_per_user - spent_elsewhere))

    def _too_large(ceiling: int) -> HTTPException:
        return HTTPException(
            status_code=413,
            detail=f"Upload exceeds the {ceiling} byte outbound attachment limit",
        )

    try:
        allowance = _budget_allowance()
        next_recheck = _BUDGET_RECHECK_BYTES
        with open(attempt, "wb") as handle:
            async for chunk in chunks:
                if not chunk:
                    continue
                total += len(chunk)
                if total > allowance:
                    raise _too_large(allowance)
                if total >= next_recheck:
                    # Publish before reading. The running total is what makes
                    # this slot visible to its siblings at all -- 'size' is only
                    # set by the promote below -- so a slot that read the shared
                    # total without first writing its own would be asking how
                    # much everyone else is using while pretending to use
                    # nothing, which is exactly how N concurrent uploads used to
                    # overrun the budget by a factor of N. The same statement
                    # renews the lease, on the same cadence.
                    if not _renew(db, upload_id, holder, total):
                        # The claim was reclaimed or swept while this body was
                        # arriving; somebody else may own the slot now. Stop
                        # rather than promote over them.
                        raise HTTPException(status_code=404, detail="Upload slot not found")
                    # Siblings may have grown while this body arrived, so the
                    # live total is re-read rather than trusted from the start.
                    allowance = _budget_allowance()
                    next_recheck = total + _BUDGET_RECHECK_BYTES
                    if total > allowance:
                        raise _too_large(allowance)
                digest.update(chunk)
                if len(head) < _SNIFF_BYTES:
                    head += chunk[: _SNIFF_BYTES - len(head)]
                handle.write(chunk)

        computed = digest.hexdigest()
        content_type = AttachmentHandler._detect_content_type(head, filename)
        if declared_size is not None and declared_size != total:
            raise HTTPException(status_code=409, detail="Upload size does not match the declared size")
        if declared_sha256 and declared_sha256.lower() != computed:
            raise HTTPException(status_code=409, detail="Upload digest does not match the declared sha256")

        # CLAIM FIRST, PROMOTE SECOND, and the order is the whole point.
        #
        # Promoting first put this attempt's bytes at the shared destination
        # before anything had established that this attempt still owned the
        # slot. A stalled upload whose lease had lapsed, whose client had
        # retried, and whose retry had been validated and committed would come
        # back, os.replace its own .part over the *winner's* payload, fail the
        # claim below, and then unlink the destination on its way out -- taking
        # the winner's file with it and leaving a row saying 'stored' with
        # nothing on the volume. That slot is wedged: re-PUT is 409, send is
        # 409, and no lease reclaim rescues it because 'stored' holds no lease.
        #
        # Winning the claim first means the destination is only ever written by
        # the attempt that owns the slot, and a loser touches nothing but its
        # own .part file.
        #
        # A residual window remains between the won claim and the os.replace
        # below: a process killed in it leaves a row saying 'stored' with no
        # file behind it. It is narrow (two syscalls) and fail-safe -- the send
        # path's load_uploads refuses a payload that is not on the volume, so
        # nothing incorrect is ever sent -- but it does NOT self-heal. 'stored'
        # carries no lease, so no reclaim reaches it; a re-PUT is answered 409
        # because the slot is no longer 'pending'; and the owner therefore
        # cannot recover that slot before its TTL expires and the sweep removes
        # it. The cost is one slot and its budget share for the rest of the
        # TTL, not a wrong message, which is why it is accepted rather than
        # closed with a second transactional mechanism.
        if not _claim(
            db,
            upload_id,
            "receiving",
            "stored",
            holder=holder,
            size=total,
            received_bytes=total,
            sha256=computed,
            detected_content_type=content_type,
            storage_path=str(destination),
        ):
            # The slot went away underneath us -- an expiry swept mid-upload, or
            # a lapsed lease handed to a successor. The bytes are still in this
            # attempt's own .part file, which the handler below unlinks; the
            # destination belongs to whoever holds the slot now and is not this
            # attempt's to touch.
            raise HTTPException(status_code=404, detail="Upload slot not found")
        try:
            # Won the claim, so this attempt's bytes are the slot's bytes. They
            # arrive in one move rather than being assembled in place where a
            # reader could catch them half written.
            os.replace(attempt, destination)
        except OSError as error:
            # A won claim with no file behind it is the wedged row all over
            # again, so the row goes back rather than being left describing
            # bytes that are not there. Everything the won claim recorded goes
            # with it, or the slot would keep charging its owner for a payload
            # that is not on the volume.
            _claim(
                db,
                upload_id,
                "stored",
                "pending",
                size=None,
                received_bytes=0,
                sha256=None,
                detected_content_type=None,
                storage_path=None,
            )
            raise HTTPException(status_code=404, detail="Upload slot not found") from error
        landed = True
    except BaseException:
        # Includes the 413 above and a client that disconnects mid-body. Only this
        # attempt's own file is removed: a payload another attempt validated and
        # committed is none of this one's business.
        attempt.unlink(missing_ok=True)
        if not landed:
            # Hand the slot back, so an honest retry has somewhere to go. The
            # running total goes with it: the bytes it counted have just been
            # unlinked, so leaving it would charge the owner for nothing.
            #
            # Fenced on this attempt's own claim. Without that, an attempt that
            # was dispossessed while it streamed would reset the slot its
            # successor is currently filling -- destroying a live upload on its
            # own way out.
            _claim(db, upload_id, "receiving", "pending", holder=holder, received_bytes=0)
        raise

    # Assembled from what this attempt holds, not re-read. The promote above is
    # the last word on the upload; re-reading the row afterwards made the answer
    # depend on the row still being there, and it need not be. The TTL is 900s,
    # a large upload can outlive it, and _owned_slot answers an expired -- or
    # swept -- row with a 404. That told an honest client its successful upload
    # had failed and sent it back to spend a second slot and a second budget
    # charge on bytes that were already safely stored.
    return {
        "upload_id": upload_id,
        "filename": filename,
        "size": total,
        "sha256": computed,
        "content_type": content_type,
        "state": "stored",
    }


def measure_uploads(db: Session, user_id: int, upload_ids: Iterable[str]) -> int:
    """Total recorded bytes of the named slots, without reading one of them.

    The size that arrived is already in the row, so a message that cannot fit can
    be refused while its payloads are still on disk -- rather than after every
    one of them has been pulled into the process to be measured.
    """
    total = 0
    seen: set[str] = set()
    for upload_id in upload_ids:
        if upload_id in seen:
            continue
        seen.add(upload_id)
        total += _owned_slot(db, upload_id, user_id).size or 0
    return total


def claim_uploads_for_send(db: Session, user_id: int, upload_ids: Iterable[str]) -> list[OutboundUpload]:
    """Take exclusive ownership of each named slot, in the caller's order.

    Reading ``state == 'stored'``, sending, and only then consuming leaves a
    whole SMTP conversation between the check and the write, which is ample room
    for a second send to pass the same check and put the same bytes on the wire
    again. Claiming first collapses that window: the slot is moved out of
    ``stored`` before any payload is read, so the second caller finds nothing to
    take and the send is refused instead of duplicated.
    """
    claimed: list[OutboundUpload] = []
    try:
        for upload_id in dict.fromkeys(upload_ids):
            upload = _owned_slot(db, upload_id, user_id)
            if upload.state == "consumed":
                raise HTTPException(status_code=409, detail="Upload has already been sent")
            if upload.state == _SEND_UNKNOWN:
                # A send claimed this slot and never reported what happened. It
                # may be sitting in the recipient's mailbox. Re-sending on a
                # guess is the one failure this whole module cannot walk back,
                # so the owner is told the truth instead.
                raise HTTPException(status_code=409, detail=_SEND_UNKNOWN_DETAIL)
            if upload.state == "sending":
                raise HTTPException(status_code=409, detail="Upload is already being sent")
            if upload.state != "stored" or not upload.storage_path:
                raise HTTPException(status_code=409, detail="Upload slot has not received its bytes yet")
            if not _claim(db, upload_id, "stored", "sending"):
                raise HTTPException(status_code=409, detail="Upload is already being sent")
            claimed.append(upload)
    except BaseException:
        # All or nothing: a send that cannot have every slot releases the ones it
        # did take, or they would be stranded in 'sending' with no sender. Via
        # the defensive settle, because a cleanup that raises would strand
        # exactly the slots it was called to free -- and would replace the real
        # reason the send was refused with a database error.
        #
        # 'failed', unambiguously: this runs entirely *before* the send. No
        # payload has been read and no transport has been opened, so nothing
        # can have reached an MTA and the slots are honestly re-sendable.
        settle_uploads(db, claimed, outcome="failed")
        raise
    return claimed


def load_uploads(db: Session, uploads: Iterable[OutboundUpload]) -> list[dict]:
    """Read claimed payloads back for a send, verifying them against their rows.

    The row is what the client was told about its upload, so the bytes handed to
    the sender have to still match it. Re-hashing here is cheap next to an SMTP
    conversation and it is the only point where the promise made at PUT time is
    checked against what is actually about to be sent.
    """
    results: list[dict] = []
    for upload in uploads:
        path = Path(upload.storage_path) if upload.storage_path else None
        if path is None or not path.is_file():
            raise HTTPException(status_code=409, detail="Upload payload is no longer on the volume")
        data = path.read_bytes()
        if upload.size is not None and len(data) != upload.size:
            raise HTTPException(status_code=409, detail="Upload payload no longer matches its recorded size")
        if upload.sha256 and hashlib.sha256(data).hexdigest() != upload.sha256:
            raise HTTPException(status_code=409, detail="Upload payload no longer matches its recorded sha256")
        results.append(
            {
                "data": data,
                "filename": upload.filename,
                # Read here, while the row is certainly live and attached. A
                # caller that reached for these attributes later would be doing
                # it across the commit that writes its own bookkeeping, which
                # expires the instance and can raise ObjectDeletedError if the
                # sweep took the row in between -- see _slot_id.
                "content_type": upload.detected_content_type,
                "upload_id": upload.upload_id,
                "upload": upload,
            }
        )
    return results


def release_uploads(db: Session, uploads: Iterable[OutboundUpload]) -> None:
    """Return claimed slots to 'stored' after a send that did not happen.

    Only for a send that *demonstrably* did not happen -- the MTA refused it, or
    it was abandoned before a single byte reached the wire. A failed send must
    leave the bytes exactly where they were, so the caller can fix whatever went
    wrong and try again without uploading them a second time.
    """
    for upload in uploads:
        upload_id = _slot_id(upload)
        if upload_id is not None:
            _claim(db, upload_id, "sending", "stored")


def retire_unknown_uploads(db: Session, uploads: Iterable[OutboundUpload]) -> None:
    """Retire claimed slots whose delivery outcome nobody can establish.

    The same destination a lapsed 'sending' lease reaches, for the same reason
    and reached in-process instead of by a timer: the bytes may be on the wire
    and nothing on this side can tell. Terminal, so the slot stops charging the
    owner's budgets and its payload is freed here rather than at the next sweep
    tick -- but never sendable again. The row itself stays until its own
    expiry, because it is the only thing that can still tell the owner what
    became of their message.
    """
    for upload in uploads:
        upload_id = _slot_id(upload)
        if upload_id is not None and _claim(db, upload_id, "sending", _SEND_UNKNOWN):
            # Only on a won claim: a caller that did not retire this slot has no
            # business removing bytes that now belong to whoever holds it.
            _free_retired_payload(db, upload_id)


def mark_consumed(db: Session, uploads: Iterable[OutboundUpload]) -> None:
    """Retire claimed slots after a send actually succeeded.

    The payload goes with the retirement. The bytes were read before the send
    and a consumed slot can never be spent again, so nothing is waiting on them
    and there is no reason for them to sit on the volume until the next sweep.
    """
    now = _column_now()
    for upload in uploads:
        upload_id = _slot_id(upload)
        if upload_id is not None and _claim(db, upload_id, "sending", "consumed", consumed_at=now):
            _free_retired_payload(db, upload_id)


# What each delivery outcome does to the slots the send claimed. The mapping is
# the whole point of taking an outcome rather than a boolean: 'failed' and
# 'unknown' are opposites here, and collapsing them into "not sent" is what
# delivered one message twice.
_SETTLEMENTS = {
    # Reported success: the slot is spent.
    "sent": mark_consumed,
    # Definitively refused, or abandoned before the wire. Nothing was
    # delivered, so the honest retry must work without a second upload.
    "failed": release_uploads,
    # Nobody knows: a transport that died after the bytes were handed over, or
    # a recipient set the server only partly accepted. Terminal.
    "unknown": retire_unknown_uploads,
}

# The only delivery_state that means the message demonstrably did not go
# anywhere. Everything else a sender may report -- 'unknown' for a transport
# that died after the bytes were handed over, 'partial' for a recipient set
# the server only partly accepted -- leaves the message possibly, or in
# 'partial' actually, delivered.
_DEFINITIVE_FAILURE_STATES = frozenset({"failed"})


def delivery_outcome(result: dict) -> str:
    """Turn a sender's result into the outcome ``settle_uploads`` acts on.

    Three answers, and the distinction between the last two is load-bearing:

    - ``sent``    the sender reported success.
    - ``failed``  the send demonstrably did not happen. Every path in this repo
      that reports a failure *without* a delivery_state is a pre-send refusal --
      no SMTP config, a disabled config, a connection that never opened -- so an
      absent state is read as one.
    - ``unknown`` the message may be, or partly is, delivered.

    A state this function has not been taught is read as ambiguous on purpose:
    that is a code change somewhere else, and the failure that costs the owner
    a slot is recoverable while the one that re-sends confidential mail is not.
    """
    if result.get("success"):
        return "sent"
    state = result.get("delivery_state")
    if state is None or state in _DEFINITIVE_FAILURE_STATES:
        return "failed"
    return "unknown"


def settle_uploads(db: Session, uploads: Iterable[OutboundUpload], *, outcome: str) -> None:
    """Move claimed slots out of 'sending', whatever the database does next.

    Takes the *outcome* rather than a boolean, and that is the fix rather than
    a tidy-up. All three of these run after the irreversible part is over, and
    they are not interchangeable:

    - ``sent``     consume the slot.
    - ``failed``   hand it back to 'stored'. Nothing was delivered, so an honest
      retry must not have to upload the bytes again.
    - ``unknown``  retire it to the terminal ``send_unknown``. A transport that
      died after the bytes were handed over, or a partly accepted recipient
      set, leaves delivery genuinely unestablished.

    A boolean could not express the third, so ``unknown`` and ``partial`` were
    settled exactly like a flat refusal: the slot went back to 'stored' and a
    retry re-sent it. The guards that were supposed to catch the duplicate do
    not: ``idempotency_key`` is optional and ``mail_service`` mints a fresh
    uuid when it is absent -- and a fresh key after an opaque transport error is
    the natural thing for a client to do -- while ``load_uploads`` only compares
    a payload against its own row, which still agree perfectly. Two
    byte-identical messages did reach an MTA this way, from one user intent.

    And this never raises, whichever settlement runs. A lost connection or a
    restart at exactly this moment would otherwise leave a row nothing can
    reclaim before the TTL, still charged against the owner's budget and
    answering every retry with 409. What is left holding a slot in 'sending' is
    covered by the lease, whose reclaim retires it to the same
    ``send_unknown`` this settles an ambiguous outcome to.
    """
    settlement = _SETTLEMENTS.get(outcome)
    if settlement is None:
        # An outcome nobody taught this function is not a licence to guess the
        # safe-looking answer: fall back to the one that cannot re-send.
        logger.warning("Unknown outbound send outcome %r; retiring the slots", outcome)
        settlement = retire_unknown_uploads
    try:
        settlement(db, uploads)
    except Exception as error:
        # Never propagated. Raising here would replace the caller's real outcome
        # -- a delivered message, or the failure that actually stopped it -- with
        # a bookkeeping error, and would strand the slot on the way out.
        logger.warning(
            "Could not settle outbound upload slots after a %s send (%s: %s)",
            outcome,
            type(error).__name__,
            error,
        )
        try:
            db.rollback()
        except Exception:
            # A session too broken to roll back is the caller's to discard.
            logger.debug("Rollback after a failed upload settlement also failed")


@dataclass(frozen=True)
class SweepReport:
    """What one sweep pass actually managed to do.

    The pass used to answer with a bare count, which cannot distinguish the
    three states an operator needs to tell apart: nothing to do, more to do
    than one pass fits, and a pass that looked at a full window and could act
    on none of it. The last one is the failure mode, and reporting only a count
    made it indistinguishable from the first.
    """

    # Rows deleted by this pass.
    removed: int
    # Rows the bounded selection actually returned -- how much of the window
    # the pass had to work with.
    examined: int
    # The bound the selection was made under.
    limit: int
    # Retained rows whose payload the retirement failed to free and this pass
    # picked up. Non-zero means the volume was refusing unlinks, not that
    # anything is wrong with the sweep.
    freed_retained_payloads: int

    @property
    def saturated(self) -> bool:
        """The window was full, so there is more garbage than one pass fits.

        Ordinary under a burst -- the next pass takes the next batch -- but
        sustained saturation means the sweep is not keeping up with the arrival
        rate, which is worth an operator's attention before the volume fills.
        """
        return self.examined >= self.limit

    @property
    def starved(self) -> bool:
        """A full window that produced nothing: the pass cannot make progress.

        This is the head-of-line blocking signature. It is structurally
        unreachable now that the selection only returns rows the pass can act
        on, so it exists to catch a regression that puts un-actionable rows
        back into the bounded window -- the exact defect that made deletion
        throughput fall to zero and stay there.
        """
        return self.saturated and self.removed == 0


def sweep_outbound_uploads(db: Session, *, limit: int = 100) -> SweepReport:
    """Delete spent and expired slots, free every payload they hold, and report.

    A pending slot that has not expired is left strictly alone: the client may
    still be uploading into it, or holding it for a send.

    In-flight claims whose lease has lapsed are reclaimed first rather than
    deleted. A slot in 'receiving' or 'sending' has not expired, so nothing here
    would ever take it, and a strand that cannot be reclaimed holds its share of
    the owner's byte and slot budgets for the whole TTL.

    Payloads and rows are released on different schedules, because they answer
    different questions. A terminal slot's *bytes* go at the moment it becomes
    terminal -- ``mark_consumed``, ``retire_unknown_uploads`` and the lease
    reclaim all free them there -- because neither 'consumed' nor
    ``send_unknown`` will ever be sent again and an honest "delivery unknown"
    must not also cost the owner the disk.

    Its *row* is a different matter. ``send_unknown`` is the only state whose
    whole purpose is to answer a later question, and the reclaim that creates
    one runs at the top of this same function: deleting terminal rows in the
    same pass retired a slot and erased it microseconds later, so the owner's
    next call got a bare 404 -- "no such slot" -- rather than the message
    explaining that the delivery outcome is unknown and that they should
    reserve a new slot. A 404 invites precisely the retry the terminal state
    exists to prevent. So a ``send_unknown`` row is kept until its own
    expires_at, by which point it has long outlived any client still holding
    the id. It costs nothing: its payload is already gone and terminal slots
    charge no budget.

    THE SELECTION RETURNS ONLY ROWS THIS PASS CAN ACT ON, and that is what
    keeps the retention from eating the sweep. Retaining a row while still
    letting it match the bounded selection meant it occupied one of the
    ``limit`` slots on every later pass, for its whole 900s TTL, without ever
    being deleted. ORDER BY id puts the oldest -- therefore the retained --
    rows at the head, so once ``limit`` of them accumulated the window was
    entirely full of rows the pass refused to touch and deletion throughput was
    exactly zero: measured 100 rows/pass at 0 retained, 25 at 75, 5 at 95, 1 at
    99, and 0 from 100 onwards, never recovering. Garbage queued behind them in
    id order was never reached, so payloads and rows both grew without a
    ceiling.

    Three things make that unreachable rather than unlikely:

    - ``reclaim_lapsed_leases`` runs at the top of this same function, so the
      sweep manufactures the very rows that blocked its own lower half.
    - One send naming the full 10-slot allowance retires all ten on a single
      ambiguous SMTP outcome -- 'partial' whenever the tenant's own SMTP host
      refuses one recipient, 'unknown' on any socket timeout -- so ten sends
      reach a hundred retained rows in well under a minute at the stock send
      rate.
    - This sweep is global and unpartitioned, so one tenant with a flaky SMTP
      host suppressed payload reclamation for every tenant on the deployment.

    Excluding them from the predicate is preferred over a second bounded query
    for deletable rows because it keeps ONE window with ONE meaning -- every
    row the pass selects is a row it deletes -- which is what makes ``starved``
    a genuine invariant rather than an expected condition. The retained rows
    are not forgotten: their payload is freed at retirement, and the separate
    bounded net below collects any the retirement could not free. That net
    selects on ``storage_path IS NOT NULL``, which retirement clears on
    success, so it returns nothing at all in a healthy deployment instead of
    re-unlinking every retained payload on each of the ~15 passes a retention
    spans.
    """
    reclaim_lapsed_leases(db, limit=limit)
    cutoff = _column_now()
    doomed = (
        db.query(OutboundUpload)
        .filter(
            OutboundUpload.state.in_(_TERMINAL_STATES) | (OutboundUpload.expires_at <= cutoff),
            # The retention, expressed where the LIMIT can see it. A retained
            # row is one this pass will not delete, so it must not consume a
            # slot in the bounded window -- otherwise the rows behind it are
            # never reached at all.
            ~(
                (OutboundUpload.state == _SEND_UNKNOWN)
                & (OutboundUpload.expires_at > cutoff)
            ),
        )
        .order_by(OutboundUpload.id)
        .limit(limit)
        .all()
    )
    removed = 0
    for upload in doomed:
        # Fall back to the id-derived path: a process killed mid-PUT leaves bytes
        # behind before storage_path was ever recorded, and those are the orphans
        # most likely to accumulate on the volume.
        destination = Path(upload.storage_path) if upload.storage_path else _storage_path(upload.upload_id)
        # Unlinked, not truncated: a reader that already opened it keeps its
        # bytes, and load_uploads verifies what it read either way.
        _unlink_payload(destination)
        # Plus any attempt file still being written into this slot: the row is
        # about to go, so nothing else will ever account for them. These *are*
        # truncated, because the process writing one may still be parked with
        # it open and its blocks would otherwise stay allocated.
        for attempt in _upload_root().glob(f"{upload.upload_id}.*{_ATTEMPT_SUFFIX}"):
            _discard_attempt_file(attempt)
        db.delete(upload)
        removed += 1
    if removed:
        db.commit()
    else:
        # The payload unlinks above are outside the transaction, but the query
        # opened one. Let it go rather than holding a read snapshot open.
        db.rollback()
    return SweepReport(
        removed=removed,
        examined=len(doomed),
        limit=limit,
        freed_retained_payloads=_free_stranded_retained_payloads(db, cutoff, limit=limit),
    )


def _free_stranded_retained_payloads(db: Session, cutoff: datetime, *, limit: int) -> int:
    """Collect payloads a retirement could not free, without touching the window.

    ``_free_retired_payload`` never raises -- the settlement path may not
    propagate -- so a read-only volume or a transient error leaves a retained
    row's bytes resident for its whole TTL with nothing else looking at them.
    This is that safety net, and it is deliberately a second, separately
    bounded query rather than part of the deletion selection: mixing rows the
    pass deletes with rows it merely tidies is exactly what produced the
    head-of-line blocking, and keeping them apart is what lets the deletion
    window mean one thing.

    Costs nothing when there is nothing to do. ``storage_path`` is cleared the
    moment a retirement confirms its unlink, so in a healthy deployment this
    matches no rows at all -- rather than re-deriving and re-unlinking every
    retained payload on every pass for the full retention.
    """
    stranded = (
        db.query(OutboundUpload.upload_id)
        .filter(
            OutboundUpload.state == _SEND_UNKNOWN,
            OutboundUpload.expires_at > cutoff,
            OutboundUpload.storage_path.isnot(None),
        )
        .order_by(OutboundUpload.id)
        .limit(limit)
        .all()
    )
    db.rollback()
    return sum(_free_retired_payload(db, upload_id) for (upload_id,) in stranded)


def sweep_expired_uploads(db: Session, *, limit: int = 100) -> int:
    """``sweep_outbound_uploads`` for callers that only want the deleted count."""
    return sweep_outbound_uploads(db, limit=limit).removed
