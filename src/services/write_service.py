"""Mutating a mailbox on the owner's behalf.

Only flags for now. A flag is the one write whose worst failure is a wrong
boolean that the next reconcile corrects; a move or a delete relocates mail, and
neither belongs here until this path has proven itself.

Three things every write must do, in this order: prove the caller owns the
message, prove the message has an address upstream, and hold the mailbox lease
so that no census is reading sequence numbers while the write lands.
"""

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from src.config import settings
from src.email.draft_builder import build_draft
from src.email.email_processor import (
    acquire_mailbox_lease,
    refresh_mailbox_lease,
    release_mailbox_lease,
    upsert_placement,
)
from src.email.gmail_labels import (
    ARCHIVE,
    LOCATIONS,
    labels_for_move,
    location_for,
    parse_labels,
)
from src.email.imap_writer import (
    ImapWriteError,
    append_message,
    child_folders,
    create_folder,
    delete_folder,
    folder_message_count,
    list_folders,
    move_messages,
    rename_folder,
    store_flags,
)
from src.email.message_flags import apply_flag_state
from src.email.smtp_client import SMTPClient
from src.models.email import EmailLog
from src.models.placement import MessagePlacement
from src.models.smtp_config import SMTPConfig
from src.models.sync_cursor import MailSyncCursor
from src.models.user import User
from src.services.mail_service import (
    is_excluded_folder,
    owned_account,
    owned_email,
    owned_email_query,
    select_message_ids,
)
from src.services.upload_service import (
    claim_uploads_for_send,
    load_uploads,
    measure_uploads,
    outbound_attachments,
    plan_upload_dispositions,
    settle_uploads,
)

logger = logging.getLogger(__name__)

MARKS = {
    "read": ([r"\Seen"], []),
    "unread": ([], [r"\Seen"]),
    "flagged": ([r"\Flagged"], []),
    "unflagged": ([], [r"\Flagged"]),
}

_recent_writes: dict[int, list[datetime]] = {}


def _rate_limit(user_id: int) -> None:
    """Per owner, mirroring send_mail. A global counter lets one tenant starve the rest.

    Counted per call rather than per message: one call moves a whole batch over one
    connection, so charging per message would make triaging a mailbox impossible
    while doing nothing about the actual cost, which is the round trip.
    """
    now = datetime.now(tz=timezone.utc)
    window = now - timedelta(minutes=1)
    recent = [stamp for stamp in _recent_writes.get(user_id, []) if stamp > window]
    if len(recent) >= settings.max_writes_per_minute:
        raise HTTPException(status_code=429, detail="Mailbox write rate limit exceeded")
    recent.append(now)
    _recent_writes[user_id] = recent


def current_validities(db: Session, account_id: int) -> dict[str, int | None]:
    """The UIDVALIDITY each folder last reported, per the sync cursors."""
    return {
        folder: validity
        for folder, validity in db.query(
            MailSyncCursor.folder, MailSyncCursor.uid_validity
        ).filter(MailSyncCursor.smtp_config_id == account_id)
    }


def writable_placement(
    db: Session,
    message: EmailLog,
    *,
    requires_uid: bool = True,
    validities: dict[str, int | None] | None = None,
) -> MessagePlacement:
    """The copy of a message a write should address.

    Prefer one that is addressable and outside Trash, so that marking a message
    read does not act on the deleted copy while the live one keeps its old state.

    On IMAP, addressable means having a UID *in the numbering space the folder is
    currently using*. A UIDVALIDITY change discards every UID, so writing to a
    placement that still carries the old one would address whatever now happens
    to hold that number -- a different message, or none. Those are ranked last
    and refused rather than guessed at.

    Gmail messages have no UID at all: they are addressed by provider id, and the
    placement only records which location the labels project to.
    """
    placements = (
        db.query(MessagePlacement).filter(MessagePlacement.email_log_id == message.id).all()
    )
    if not placements:
        raise HTTPException(
            status_code=409,
            detail=(
                "No known folder, so this message cannot be addressed upstream. It was "
                "either stored before folders were tracked, or it has since moved "
                "somewhere this account does not sync -- on Gmail, All Mail excludes "
                "Bin and Spam, so mail deleted there leaves the indexed folder."
            ),
        )
    suffixes = settings.excluded_folder_suffixes
    known = validities or {}

    def stale(placement: MessagePlacement) -> bool:
        # A folder with no cursor has never been synced, so nothing is known
        # about its numbering; that is not evidence of staleness.
        if placement.folder not in known or placement.uid_validity is None:
            return False
        return placement.uid_validity != known[placement.folder]

    ranked = sorted(
        placements,
        key=lambda placement: (
            requires_uid and (placement.uid is None or stale(placement)),
            is_excluded_folder(placement.folder, suffixes),
            placement.folder or "",
        ),
    )
    best = ranked[0]
    if requires_uid and best.uid is None:
        raise HTTPException(status_code=409, detail="This message has no upstream UID to address")
    if requires_uid and stale(best):
        raise HTTPException(
            status_code=409,
            detail=(
                f"The only known location for this message is {best.folder!r} under a "
                "UIDVALIDITY the server has since discarded, so its UID now names a "
                "different message or none. Re-run scripts/repair_placements.py to "
                "locate it again."
            ),
        )
    return best


def _assert_writable(account: SMTPConfig) -> None:
    if not account.credential_ciphertext:
        raise HTTPException(status_code=400, detail="Mailbox password is not configured")




@dataclass
class Selection:
    """The messages one bulk operation will act on."""

    account: SMTPConfig
    targets: list[tuple[EmailLog, MessagePlacement]]
    matched: int
    skipped: list[dict]

    @property
    def by_folder(self) -> dict[str, list[tuple[EmailLog, MessagePlacement]]]:
        grouped: dict[str, list[tuple[EmailLog, MessagePlacement]]] = {}
        for message, placement in self.targets:
            grouped.setdefault(placement.folder, []).append((message, placement))
        return grouped


def _select(db: Session, user: User, email_ids: list[int] | None, filters: dict, limit: int | None) -> Selection:
    """Resolve either an explicit id list or a search into addressable messages.

    limit=None means everything that matched, capped by max_write_batch. The cap
    is a backstop against a runaway selection, not a page size: commands are
    chunked and the lease is refreshed, so a large batch is still one connection.
    """
    limit = settings.max_write_batch if limit is None else max(1, min(limit, settings.max_write_batch))
    if email_ids:
        ids, matched = list(dict.fromkeys(email_ids))[:limit], len(set(email_ids))
    else:
        if not any(value not in (None, "", [], False) for value in filters.values()):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Supply email_ids, or filters describing which messages to act on. "
                    "Refusing to act on an entire mailbox by default."
                ),
            )
        ids, matched = select_message_ids(db, user, limit=limit, **filters)

    # deleted_at matters even for an explicit id list: a tombstoned message is one
    # the provider no longer has, so there is nothing upstream to write to.
    messages = (
        owned_email_query(db, user.id)
        .filter(EmailLog.id.in_(ids), EmailLog.deleted_at.is_(None))
        .all()
        if ids
        else []
    )
    found = {message.id for message in messages}
    skipped = [{"email_id": missing, "reason": "not found"} for missing in set(ids) - found]

    accounts = {message.smtp_config_id for message in messages}
    if len(accounts) > 1:
        raise HTTPException(
            status_code=400,
            detail=(
                f"These messages span {len(accounts)} mailboxes. Bulk operations run "
                "against one mailbox at a time; pass account_id, or split the call."
            ),
        )
    if not messages:
        raise HTTPException(status_code=404, detail="No messages matched")

    account = db.query(SMTPConfig).filter(SMTPConfig.id == messages[0].smtp_config_id).one()
    _assert_writable(account)

    requires_uid = not _is_gmail_api(account)
    validities = current_validities(db, account.id) if requires_uid else {}
    targets = []
    for message in messages:
        try:
            targets.append((
                message,
                writable_placement(
                    db, message, requires_uid=requires_uid, validities=validities
                ),
            ))
        except HTTPException as exc:
            skipped.append({"email_id": message.id, "reason": exc.detail})
    if not targets:
        raise HTTPException(status_code=409, detail="No matched message could be addressed upstream")
    return Selection(account=account, targets=targets, matched=matched, skipped=skipped)


def _summarise_skipped(skipped: list[dict]) -> list[dict]:
    """One entry per reason, not one per message.

    A batch of 2,600 can skip hundreds for the same cause, and repeating the same
    sentence hundreds of times buries the result it is attached to.
    """
    grouped: dict[str, list[int]] = {}
    for entry in skipped:
        grouped.setdefault(entry["reason"], []).append(entry["email_id"])
    return [
        {
            "reason": reason,
            "count": len(ids),
            "email_ids": sorted(ids)[:20],
            "email_ids_truncated": len(ids) > 20,
        }
        for reason, ids in sorted(grouped.items(), key=lambda item: -len(item[1]))
    ]


def _outcome(selection: Selection, limit: int | None, **extra) -> dict:
    result = {
        "success": True,
        "account_id": selection.account.id,
        "matched": selection.matched,
        "affected": len(selection.targets),
        "skipped": len(selection.skipped),
        "skipped_reasons": _summarise_skipped(selection.skipped),
        **extra,
    }
    if selection.matched > len(selection.targets) + len(selection.skipped):
        result["truncated"] = True
        result["note"] = (
            f"{selection.matched} messages matched but this call handled "
            f"{len(selection.targets) + len(selection.skipped)}. Repeat with the same "
            "filters to continue."
        )
    return result


async def _with_mailbox(db: Session, account: SMTPConfig, operation):
    """Run one operation against a mailbox while holding its lease."""
    deadline = datetime.now(tz=timezone.utc) + timedelta(
        seconds=settings.mail_write_lease_wait_seconds
    )
    while True:
        token = acquire_mailbox_lease(db, account, seconds=settings.sync_lease_seconds)
        if token or datetime.now(tz=timezone.utc) >= deadline:
            break
        await asyncio.sleep(1)
    if not token:
        raise HTTPException(
            status_code=409,
            detail="This mailbox has been synchronizing longer than this write would wait; retry shortly",
        )
    # Moving thousands of messages outruns a 120 second lease, and an expired
    # lease lets a folder census start while UIDs are still being renumbered.
    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(max(1, settings.sync_lease_seconds // 3))
            if not refresh_mailbox_lease(token, seconds=settings.sync_lease_seconds):
                logger.warning("Lost the mailbox lease during a write on account %s", account.id)
                return

    keepalive = asyncio.create_task(heartbeat())
    client = SMTPClient(SMTPConfig.create_detached(account))
    try:
        if not await client.connect():
            raise HTTPException(status_code=502, detail="Could not connect to the mailbox")
        try:
            return await operation(client)
        except ImapWriteError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        keepalive.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keepalive
        # A cancelled aioimaplib command leaves pending_sync_command set, and every
        # later command on that connection -- including logout -- waits on it forever.
        try:
            await asyncio.wait_for(client.disconnect(), timeout=10)
        except Exception as exc:
            logger.warning("Discarding wedged IMAP session after write: %s", exc)
        release_mailbox_lease(db, token)


async def mark_mail(
    db: Session,
    user: User,
    *,
    mark: str,
    email_ids: list[int] | None = None,
    limit: int | None = None,
    **filters,
) -> dict:
    """Set or clear a flag on one message or on everything matching a search."""
    if mark not in MARKS:
        raise HTTPException(status_code=400, detail=f"Unknown mark: {mark}")
    _rate_limit(user.id)
    selection = _select(db, user, email_ids, filters, limit)
    add, remove = MARKS[mark]

    if _is_gmail_api(selection.account):
        # Gmail states the negative: a read message simply lacks UNREAD.
        gmail = {
            "read": ([], ["UNREAD"]),
            "unread": (["UNREAD"], []),
            "flagged": (["STARRED"], []),
            "unflagged": ([], ["STARRED"]),
        }[mark]
        changed = await _gmail_relabel(
            db, selection, add_for=lambda _labels: gmail[0], remove_for=lambda _labels: gmail[1]
        )
        return _outcome(selection, limit, mark=mark, confirmed=changed)

    async def operation(client):
        confirmed: dict[str, dict[int, str]] = {}
        for folder, group in selection.by_folder.items():
            uids = [placement.uid for _message, placement in group]
            confirmed[folder] = await store_flags(client, folder, uids, add=add, remove=remove)
        return confirmed

    confirmed = await _with_mailbox(db, selection.account, operation)

    changed = 0
    for folder, group in selection.by_folder.items():
        for message, placement in group:
            raw = confirmed.get(folder, {}).get(placement.uid)
            # Nothing is recorded locally that the server did not confirm.
            if raw is None:
                selection.skipped.append({"email_id": message.id, "reason": "server did not confirm"})
                continue
            apply_flag_state(message, raw)
            changed += 1
    db.commit()
    return _outcome(selection, limit, mark=mark, confirmed=changed)


async def move_mail(
    db: Session,
    user: User,
    *,
    folder: str,
    email_ids: list[int] | None = None,
    limit: int | None = None,
    **filters,
) -> dict:
    """Move one message or a whole search result into another folder."""
    _rate_limit(user.id)
    selection = _select(db, user, email_ids, filters, limit)

    if _is_gmail_api(selection.account):
        destination = folder.upper()
        if destination not in (*LOCATIONS, ARCHIVE):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{folder!r} is not a Gmail location. Gmail has labels, not folders: "
                    f"use one of {', '.join((*LOCATIONS, ARCHIVE))}. ARCHIVE means "
                    "removing the message from the Inbox without deleting it."
                ),
            )
        changed = await _gmail_relabel(
            db,
            selection,
            add_for=lambda labels: labels_for_move(labels, destination)[0],
            remove_for=lambda labels: labels_for_move(labels, destination)[1],
        )
        return _outcome(selection, limit, to=destination, moved=changed, already_there=0)

    async def operation(client):
        names = {item["name"] for item in await list_folders(client)}
        if folder not in names:
            raise HTTPException(
                status_code=404,
                detail=f"No folder named {folder!r} in this mailbox. Call list_mail_folders first.",
            )
        landed: dict[str, dict[int, int]] = {}
        for source, group in selection.by_folder.items():
            if source == folder:
                continue
            uids = [placement.uid for _message, placement in group]
            landed[source] = await move_messages(client, source, uids, folder)
        return landed

    landed = await _with_mailbox(db, selection.account, operation)
    return _outcome(selection, limit, to=folder, **_reflect_move(db, selection, folder, landed))


def _reflect_move(db: Session, selection: Selection, destination: str, landed: dict) -> dict:
    """Record a completed move locally, once the server has confirmed it."""
    moved = unchanged = unconfirmed = 0
    for source, group in selection.by_folder.items():
        if source == destination:
            unchanged += len(group)
            continue
        for message, placement in group:
            confirmation = landed.get(source, {}).get(placement.uid)
            if confirmation is None:
                # The server did not say where it went, so the old location is
                # still the best thing known. Deleting it would leave the message
                # unplaced, and nothing re-places a message in a folder this
                # account does not census -- Gmail's Bin, for one. The next census
                # of the source folder retires it if the move did happen.
                unconfirmed += 1
                continue
            new_uid, new_validity = confirmation
            db.delete(placement)
            # The validity from COPYUID belongs to the destination folder. Reusing
            # the source folder's would stamp a UID from one numbering space with
            # another's identifier, which reads as a renumbered folder for ever.
            upsert_placement(db, message.id, destination, new_uid, new_validity)
            moved += 1
    db.commit()
    result = {"moved": moved, "already_there": unchanged}
    if unconfirmed:
        result["unconfirmed"] = unconfirmed
        result["note_unconfirmed"] = (
            f"{unconfirmed} message(s) were moved but the server did not report the "
            "new location, so the index still shows the old one until the next sync."
        )
    return result


async def delete_mail(
    db: Session,
    user: User,
    *,
    permanent: bool = False,
    email_ids: list[int] | None = None,
    limit: int | None = None,
    **filters,
) -> dict:
    """Move to Trash, or destroy messages already in Trash.

    The rule a mail client uses: deleting from anywhere means Trash, and only
    deleting from Trash removes anything.
    """
    _rate_limit(user.id)
    selection = _select(db, user, email_ids, filters, limit)
    suffixes = settings.excluded_folder_suffixes

    if _is_gmail_api(selection.account):
        if not permanent:
            changed = await _gmail_relabel(
                db,
                selection,
                add_for=lambda labels: labels_for_move(labels, "TRASH")[0],
                remove_for=lambda labels: labels_for_move(labels, "TRASH")[1],
            )
            return _outcome(selection, limit, permanent=False, to="TRASH", moved=changed)

        outside = [
            message.id
            for message, placement in selection.targets
            if not is_excluded_folder(placement.folder, suffixes)
        ]
        if outside:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{len(outside)} of these messages are not in Gmail's Bin yet. Send "
                    "them there first; only a message already in Trash can be removed "
                    "permanently."
                ),
            )

        async def destroy(client):
            semaphore = asyncio.Semaphore(max(1, settings.gmail_request_concurrency))

            async def one(message):
                async with semaphore:
                    await client.delete_message_permanently(message.provider_message_id)

            await asyncio.gather(*(one(message) for message, _p in selection.targets))

        await _gmail(db, selection.account, destroy)
        now = datetime.now(tz=timezone.utc)
        for message, placement in selection.targets:
            db.delete(placement)
            message.deleted_at = now
        db.commit()
        return _outcome(selection, limit, permanent=True)

    if permanent:
        outside = [
            message.id
            for message, placement in selection.targets
            if not is_excluded_folder(placement.folder, suffixes)
        ]
        if outside:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{len(outside)} of these messages are not in Trash yet. Send them "
                    "there first; only a message already in Trash can be removed "
                    "permanently."
                ),
            )

        async def destroy(client):
            for folder, group in selection.by_folder.items():
                uids = [placement.uid for _message, placement in group]
                await store_flags(client, folder, uids, add=["\\Deleted"], remove=[])
                capabilities = {
                    item.upper()
                    for item in getattr(client.client.protocol, "capabilities", set())
                }
                if "UIDPLUS" in capabilities:
                    await client.client.uid("expunge", ",".join(str(uid) for uid in uids))
            return None

        await _with_mailbox(db, selection.account, destroy)
        now = datetime.now(tz=timezone.utc)
        for message, placement in selection.targets:
            db.delete(placement)
            # Tombstone rather than drop the row: the grace period is what makes a
            # mistaken deletion recoverable from the index.
            message.deleted_at = now
        db.commit()
        return _outcome(selection, limit, permanent=True)

    async def to_trash(client):
        folders = await list_folders(client)
        trash = _role_folder(folders, "trash", suffixes)
        if not trash:
            raise HTTPException(
                status_code=409,
                detail="This mailbox has no Trash folder; move the messages explicitly instead",
            )
        landed: dict[str, dict[int, int]] = {}
        for source, group in selection.by_folder.items():
            if is_excluded_folder(source, suffixes):
                continue
            uids = [placement.uid for _message, placement in group]
            landed[source] = await move_messages(client, source, uids, trash)
        return trash, landed

    trash, landed = await _with_mailbox(db, selection.account, to_trash)
    reflected = _reflect_move(db, selection, trash, landed)
    return _outcome(selection, limit, permanent=False, to=trash, **reflected)


def _assert_folders_supported(account: SMTPConfig) -> None:
    """Folder management is an IMAP idea.

    Gmail has labels, and its locations here are projected rather than stored, so
    there is nothing for create, rename or delete to act on. Refusing with the
    reason is more useful than letting an IMAP connection fail against an account
    that never had one.
    """
    if _is_gmail_api(account):
        raise HTTPException(
            status_code=501,
            detail=(
                "This is a Gmail account, which has labels rather than folders. Its "
                "locations are fixed: INBOX, ARCHIVE, SENT, DRAFT, SPAM, TRASH. Use "
                "move_mail with one of those."
            ),
        )


async def create_mail_folder(db: Session, user: User, *, account_id: int, name: str) -> dict:
    """Create a folder, so an agent can file mail somewhere that does not exist yet."""
    account = owned_account(db, user.id, account_id)
    _assert_writable(account)
    _assert_folders_supported(account)
    cleaned = name.strip().strip('"')
    if not cleaned or any(ch in cleaned for ch in '"\r\n'):
        raise HTTPException(status_code=400, detail="Invalid folder name")
    _rate_limit(user.id)
    created = await _with_mailbox(db, account, lambda client: create_folder(client, cleaned))
    return {"success": True, "account_id": account_id, "folder": cleaned, "created": created}


async def list_mail_folders(db: Session, user: User, *, account_id: int) -> dict:
    """Every folder in one mailbox, so a move has real names to target."""
    account = owned_account(db, user.id, account_id)
    _assert_writable(account)
    if _is_gmail_api(account):
        # Gmail's locations are projected, not listed: reporting its user labels
        # here would offer move_mail destinations that move_mail cannot honour.
        folders = [
            {"name": name, "special_use": _GMAIL_ROLES.get(name)}
            for name in (*LOCATIONS, ARCHIVE)
        ]
    else:
        folders = await _with_mailbox(db, account, lambda client: list_folders(client))
    counts = dict(
        db.query(MessagePlacement.folder, func.count())
        .join(EmailLog, EmailLog.id == MessagePlacement.email_log_id)
        .filter(EmailLog.smtp_config_id == account_id, EmailLog.deleted_at.is_(None))
        .group_by(MessagePlacement.folder)
        .all()
    )
    for folder in folders:
        folder["indexed_message_count"] = int(counts.get(folder["name"], 0))
    return {"account_id": account_id, "folders": folders}


def _role_folder(folders: list[dict], role: str, suffixes: list[str]) -> str | None:
    """The folder serving a role, by its own declaration first and its name second."""
    declared = next((f["name"] for f in folders if f["special_use"] == role), None)
    if declared:
        return declared
    return next((f["name"] for f in folders if is_excluded_folder(f["name"], suffixes)), None)


async def save_draft(
    db: Session,
    user: User,
    *,
    account_id: int,
    to_addresses: list[str],
    subject: str,
    body_text: str = "",
    body_html: str = "",
    cc_addresses: list[str] | None = None,
    reply_to_email_id: int | None = None,
    upload_ids: list[str] | None = None,
    inline_upload_ids: list[str] | None = None,
) -> dict:
    """Store a draft in the mailbox, where any mail client will pick it up."""
    account = owned_account(db, user.id, account_id)
    _assert_writable(account)
    _rate_limit(user.id)

    # One message, one cap, across both lists: an image embedded in the body is
    # as much a part of the draft as a file appended to it. Refused before
    # anything is measured, let alone claimed, so an ambiguous request costs a
    # slot nothing.
    _ordinary, inline_uploads, selected_uploads = plan_upload_dispositions(
        upload_ids, inline_upload_ids
    )
    if len(selected_uploads) > settings.max_outbound_attachments:
        raise HTTPException(status_code=400, detail="Too many outbound attachments")

    headers: dict[str, str] = {}
    if reply_to_email_id is not None:
        parent = owned_email(db, user.id, reply_to_email_id)
        if parent.smtp_config_id != account_id:
            raise HTTPException(
                status_code=400,
                detail="A reply draft must use the mailbox that owns the original message",
            )
        if parent.message_id:
            headers["In-Reply-To"] = parent.message_id
            references = (parent.references or "").split()
            if parent.message_id not in references:
                references.append(parent.message_id)
            headers["References"] = " ".join(references[-100:])

    # Sized before anything is claimed, exactly as send_mail does it. The rows
    # already record what arrived, so a draft that cannot fit is refused without
    # touching a single slot -- where measuring after the claim cost two write
    # transactions per named slot (one to take it, one to hand it back) for a
    # request that was always going to be refused. measure_uploads only reads
    # the recorded sizes; it never opens a payload.
    if measure_uploads(db, user.id, selected_uploads) > settings.max_outbound_attachment_bytes:
        raise HTTPException(status_code=400, detail="Outbound attachments exceed the configured limit")

    # Claimed before any payload is read, and before the mailbox round trip. The
    # APPEND in the middle is a long await, so a second draft naming the same
    # slot would otherwise pass the same 'stored' check and file the same bytes
    # a second time; one conditional UPDATE decides between them instead.
    claimed = claim_uploads_for_send(db, user.id, selected_uploads) if selected_uploads else []
    try:
        uploads = load_uploads(db, claimed)

        raw = build_draft(
            sender=account.account_name or account.username,
            to_addresses=to_addresses,
            cc_addresses=cc_addresses or [],
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            headers=headers,
            attachments=outbound_attachments(uploads, inline_uploads),
        )

        if _is_gmail_api(account):
            thread_id = None
            if reply_to_email_id is not None:
                thread_id = owned_email(db, user.id, reply_to_email_id).provider_thread_id
            created = await _gmail(
                db, account, lambda client: client.create_draft(raw, thread_id=thread_id)
            )
            # Consumed only once the draft is actually in the mailbox; a failure
            # above releases the slots so the same bytes can be filed on a retry.
            # Defensively: the draft exists by now, so a bookkeeping failure
            # that propagated would strand the slot in 'sending' and report a
            # filed draft as a failure.
            settle_uploads(db, claimed, outcome="sent")
            return {
                "success": True,
                "folder": "DRAFT",
                "draft_id": (created or {}).get("id"),
                "bytes": len(raw),
            }

        async def operation(client):
            folders = await list_folders(client)
            drafts = next((f["name"] for f in folders if f["special_use"] == "drafts"), None)
            if not drafts:
                drafts = next((f["name"] for f in folders if f["name"].lower().endswith("drafts")), None)
            if not drafts:
                raise HTTPException(status_code=409, detail="This mailbox has no Drafts folder")
            return drafts, await append_message(client, drafts, raw, flags=["\\Draft", "\\Seen"])

        drafts, uid = await _with_mailbox(db, account, operation)
    except BaseException:
        # 'failed', and unlike the send path that is not a judgement call: a
        # draft is filed, never delivered. A failed APPEND or create_draft
        # leaves nothing in anybody's mailbox and no message on any wire, so
        # the slots are honestly re-usable and an honest retry must not have to
        # upload the bytes again. (A draft that was filed but whose response
        # was lost costs at worst a duplicate draft in the owner's own Drafts
        # folder -- not a message delivered twice.)
        settle_uploads(db, claimed, outcome="failed")
        raise
    settle_uploads(db, claimed, outcome="sent")
    return {"success": True, "folder": drafts, "uid": uid, "bytes": len(raw)}


# Folders the mailbox itself relies on. INBOX is required by RFC 3501, and a
# folder the server tagged with a special use is where it files sent mail,
# drafts, spam or deletions.
def _protected_reason(folder: dict) -> str | None:
    if folder["name"].upper() == "INBOX":
        return "INBOX cannot be removed"
    if folder["special_use"]:
        return f"this is the mailbox's {folder['special_use']} folder"
    return None


async def delete_mail_folder(
    db: Session, user: User, *, account_id: int, name: str, force: bool = False
) -> dict:
    """Remove a folder, sending any mail still in it to Trash first.

    Deleting a folder on an IMAP server destroys the messages inside it. So this
    empties it into Trash first and only then removes it -- and refuses outright
    if the server still reports messages afterwards, rather than taking the
    server's word for what the index thinks it knows.
    """
    account = owned_account(db, user.id, account_id)
    _assert_writable(account)
    _assert_folders_supported(account)
    _rate_limit(user.id)

    folders = await _with_mailbox(db, account, lambda client: list_folders(client))
    target = next((folder for folder in folders if folder["name"] == name), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"No folder named {name!r} in this mailbox")
    protected = _protected_reason(target)
    if protected:
        raise HTTPException(status_code=409, detail=f"Refusing to delete {name!r}: {protected}")
    children = child_folders(folders, name)
    if children:
        raise HTTPException(
            status_code=409,
            detail=f"{name!r} still contains the folder(s) {children}. Remove those first.",
        )

    # Empty it into Trash through the ordinary path, so placements and read state
    # are updated the same way any other move updates them.
    emptied = 0
    if not is_excluded_folder(name, settings.excluded_folder_suffixes):
        with contextlib.suppress(HTTPException):
            moved = await delete_mail(db, user, account_id=account_id, folders=[name])
            emptied = moved.get("moved", 0)

    async def operation(client):
        remaining = await folder_message_count(client, name)
        if remaining and not force:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{name!r} still holds {remaining} message(s) the index does not know "
                    "about; deleting the folder would destroy them. Move them out, or "
                    "pass force=true."
                ),
            )
        await delete_folder(client, name)
        return remaining

    remaining = await _with_mailbox(db, account, operation)
    # The folder is gone, so its placements name nowhere. Anything that lost its
    # last placement stays unplaced, which is never treated as deleted.
    db.query(MessagePlacement).filter(
        MessagePlacement.folder == name,
        MessagePlacement.email_log_id.in_(
            db.query(EmailLog.id).filter(EmailLog.smtp_config_id == account_id)
        ),
    ).delete(synchronize_session=False)
    db.commit()
    return {
        "success": True,
        "account_id": account_id,
        "deleted": name,
        "moved_to_trash": emptied,
        "destroyed": remaining,
    }


async def rename_mail_folder(
    db: Session, user: User, *, account_id: int, name: str, new_name: str
) -> dict:
    """Rename a folder, keeping the mail and any nested folders with it."""
    account = owned_account(db, user.id, account_id)
    _assert_writable(account)
    _assert_folders_supported(account)
    cleaned = new_name.strip().strip('"')
    if not cleaned or any(character in cleaned for character in '"\r\n'):
        raise HTTPException(status_code=400, detail="Invalid folder name")
    _rate_limit(user.id)

    folders = await _with_mailbox(db, account, lambda client: list_folders(client))
    target = next((folder for folder in folders if folder["name"] == name), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"No folder named {name!r} in this mailbox")
    protected = _protected_reason(target)
    if protected:
        raise HTTPException(status_code=409, detail=f"Refusing to rename {name!r}: {protected}")
    if any(folder["name"] == cleaned for folder in folders):
        raise HTTPException(status_code=409, detail=f"A folder named {cleaned!r} already exists")

    await _with_mailbox(db, account, lambda client: rename_folder(client, name, cleaned))

    # RENAME carries nested folders too, so every placement beneath the old name
    # has to follow, not just the ones directly in it.
    owned = db.query(EmailLog.id).filter(EmailLog.smtp_config_id == account_id)
    renamed = 0
    for placement in (
        db.query(MessagePlacement)
        .filter(MessagePlacement.email_log_id.in_(owned))
        .filter(
            or_(
                MessagePlacement.folder == name,
                MessagePlacement.folder.startswith(f"{name}/"),
                MessagePlacement.folder.startswith(f"{name}."),
            )
        )
        .all()
    ):
        placement.folder = cleaned + placement.folder[len(name) :]
        renamed += 1
    db.commit()
    return {
        "success": True,
        "account_id": account_id,
        "renamed_from": name,
        "renamed_to": cleaned,
        "placements_updated": renamed,
    }


_GMAIL_ROLES = {"TRASH": "trash", "SPAM": "junk", "DRAFT": "drafts", "SENT": "sent", "ARCHIVE": "archive"}


def _is_gmail_api(account: SMTPConfig) -> bool:
    return account.provider == "gmail" and account.auth_type == "oauth2"


async def _gmail(db: Session, account: SMTPConfig, operation):
    """Run an operation against the Gmail API under the mailbox lease.

    The lease matters less here than on IMAP -- there are no sequence numbers to
    renumber -- but the Gmail sync reconciles by history id, and a relabel landing
    mid-pass is still a change the pass did not see.
    """
    from src.email.gmail_api_client import GmailApiClient

    deadline = datetime.now(tz=timezone.utc) + timedelta(
        seconds=settings.mail_write_lease_wait_seconds
    )
    while True:
        token = acquire_mailbox_lease(db, account, seconds=settings.sync_lease_seconds)
        if token or datetime.now(tz=timezone.utc) >= deadline:
            break
        await asyncio.sleep(1)
    if not token:
        raise HTTPException(
            status_code=409,
            detail="This mailbox has been synchronizing longer than this write would wait; retry shortly",
        )

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(max(1, settings.sync_lease_seconds // 3))
            if not refresh_mailbox_lease(token, seconds=settings.sync_lease_seconds):
                logger.warning("Lost the mailbox lease during a write on account %s", account.id)
                return

    keepalive = asyncio.create_task(heartbeat())
    client = GmailApiClient(SMTPConfig.create_detached(account))
    try:
        return await operation(client)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Gmail refused the change: {exc.response.status_code} {exc.response.text[:200]}",
        ) from exc
    finally:
        keepalive.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keepalive
        with contextlib.suppress(Exception):
            await client.close()
        release_mailbox_lease(db, token)


def _record_gmail_labels(db: Session, message: EmailLog, labels: list[str]) -> None:
    """Write back what Gmail says the message now is, never what was asked for."""
    apply_flag_state(message, json.dumps(sorted(labels)))
    upsert_placement(db, message.id, location_for(labels), None, None, exclusive=True)


async def _gmail_relabel(
    db: Session,
    selection: Selection,
    *,
    add_for,
    remove_for,
) -> int:
    """Apply per-message label changes, bounded by the account's concurrency."""
    semaphore = asyncio.Semaphore(max(1, settings.gmail_request_concurrency))

    async def one(client, message: EmailLog) -> tuple[EmailLog, list[str] | None]:
        labels = parse_labels(message.flags)
        add, remove = add_for(labels), remove_for(labels)
        if not add and not remove:
            return message, labels
        async with semaphore:
            return message, await client.modify_labels(
                message.provider_message_id, add=add, remove=remove
            )

    async def operation(client):
        return await asyncio.gather(
            *(one(client, message) for message, _placement in selection.targets)
        )

    results = await _gmail(db, selection.account, operation)
    changed = 0
    for message, labels in results:
        if labels is None:
            selection.skipped.append({"email_id": message.id, "reason": "Gmail did not confirm"})
            continue
        _record_gmail_labels(db, message, labels)
        changed += 1
    db.commit()
    return changed
