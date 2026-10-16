"""Delete a mail account from this server, together with everything indexed from it.

Nothing happens at the provider. No mail is deleted there, and the same mailbox
can be connected again later, when it is indexed from scratch. What goes is this
server's copy: the stored credential and settings, every indexed message with its
extracted attachment text, its folder placements and participants, and the sync
checkpoints. The send audit stays. It records what this server sent on the
owner's behalf, so it outlives the account it was sent from, detached from it.
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from src.models.attachment import EmailAttachment
from src.models.email import EmailLog
from src.models.participant import MailParticipant
from src.models.placement import MessagePlacement
from src.models.send_audit import SendAudit
from src.models.smtp_config import SMTPConfig
from src.models.sync_cursor import MailSyncCursor

logger = logging.getLogger(__name__)

# A sync worker holds the account row locked for the length of each of its
# transactions. Waiting for one to finish is right; waiting behind a stuck one
# indefinitely is not.
LOCK_TIMEOUT_MS = 30_000

# How long a send can still be on its way once claimed. send_mail records the
# claim as a 'pending' audit before it transmits and settles it afterwards, and
# the transport's own timeouts end any attempt well inside this. A 'pending'
# audit older than this belongs to a process that died mid-send; it must not
# block deleting the account for good.
IN_FLIGHT_SEND_WINDOW = timedelta(minutes=30)


# lock_not_available (the lock_timeout above) and deadlock_detected. Anything
# else -- a lost connection, say -- is not "busy" and must not be reported so.
_CONTENDED = {"55P03", "40P01"}


def _contended(exc: OperationalError) -> bool:
    return getattr(exc.orig, "pgcode", None) in _CONTENDED


def _busy(message: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"message": f"{message}; nothing was deleted. Try again shortly.", "retry_after": 30},
    )


def _lock_owned_account(db: Session, user_id: int, account_id: int) -> SMTPConfig:
    """Take the account row before touching anything that hangs off it.

    A sync worker's fence (sync_fence._check) locks this row first in every
    transaction, too, so the two take their locks in the same order. A worker
    transaction already open on the account is waited for; one that begins
    while this lock is held waits for this transaction and then finds the row
    gone, so its fence check fails and it writes nothing. A worker between
    transactions is fenced out at its next one.
    """
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text(f"SET LOCAL lock_timeout = '{int(LOCK_TIMEOUT_MS)}ms'"))
    account = (
        db.query(SMTPConfig)
        .filter(SMTPConfig.id == account_id, SMTPConfig.owner_user_id == user_id)
        .with_for_update()
        .first()
    )
    if account is None:
        raise HTTPException(status_code=404, detail="Mail account not found")
    return account


def delete_mail_account(db: Session, user_id: int, *, account_id: int, confirm_name: str | None) -> dict:
    """Without ``confirm_name``, say what would go. With it, and only if it matches, delete.

    One transaction: either everything below goes, or nothing does.
    """
    try:
        return _delete(db, user_id, account_id=account_id, confirm_name=confirm_name)
    except OperationalError as exc:
        db.rollback()
        if not _contended(exc):
            raise
        raise _busy("This account is held by a synchronization or another change") from exc
    except BaseException:
        db.rollback()
        raise


def _delete(db: Session, user_id: int, *, account_id: int, confirm_name: str | None) -> dict:
    account = _lock_owned_account(db, user_id, account_id)
    summary = {
        "account_id": account.id,
        "name": account.name,
        "address": account.account_name or account.username,
        "provider": account.provider,
    }
    messages = select(EmailLog.id).where(EmailLog.smtp_config_id == account.id)
    counts = {
        "messages": db.scalar(select(func.count()).select_from(EmailLog).where(EmailLog.smtp_config_id == account.id)),
        "attachments": db.scalar(
            select(func.count()).select_from(EmailAttachment).where(EmailAttachment.email_log_id.in_(messages))
        ),
    }
    sends = db.scalar(select(func.count()).select_from(SendAudit).where(SendAudit.smtp_config_id == account.id))
    # A send that has claimed but not settled would find its account gone
    # mid-flight: delivered, yet reported as failed. Let it finish first. A new
    # send cannot start meanwhile: its claim needs this row, which is locked.
    #
    # The bound is timezone-aware on purpose. created_at is the database's own
    # now(), written in the session's TimeZone; PostgreSQL reads a naive column
    # in that same zone when comparing it with an aware value, so this holds
    # whatever TimeZone is set. A naive UTC bound would hold only under UTC.
    in_flight = db.scalar(
        select(func.count())
        .select_from(SendAudit)
        .where(
            SendAudit.smtp_config_id == account.id,
            SendAudit.status == "pending",
            SendAudit.created_at >= datetime.now(timezone.utc) - IN_FLIGHT_SEND_WINDOW,
        )
    )

    if confirm_name is None:
        db.rollback()
        return {
            "deleted": False,
            **summary,
            "would_remove": counts,
            "send_audits_kept": sends,
            "next_step": (
                "Nothing was deleted. To delete this account, call delete_mail_account again "
                "with confirm_name set to the account's name exactly as shown here."
            ),
        }
    if confirm_name != account.name:
        raise HTTPException(
            status_code=400,
            detail="confirm_name does not match this account's name; nothing was deleted",
        )
    if in_flight:
        raise _busy("A message is being sent from this account right now")

    # Dependency order, spelled out rather than left to ON DELETE rules: SQLite
    # enforces none of them unless asked, and email_attachments has no cascade.
    bulk = {"synchronize_session": False}
    db.execute(
        update(SendAudit).where(SendAudit.reply_to_email_id.in_(messages)).values(reply_to_email_id=None),
        execution_options=bulk,
    )
    db.execute(
        update(SendAudit).where(SendAudit.smtp_config_id == account.id).values(smtp_config_id=None),
        execution_options=bulk,
    )
    db.execute(delete(EmailAttachment).where(EmailAttachment.email_log_id.in_(messages)), execution_options=bulk)
    db.execute(delete(MailParticipant).where(MailParticipant.email_log_id.in_(messages)), execution_options=bulk)
    db.execute(delete(MessagePlacement).where(MessagePlacement.email_log_id.in_(messages)), execution_options=bulk)
    db.execute(delete(EmailLog).where(EmailLog.smtp_config_id == account.id), execution_options=bulk)
    db.execute(delete(MailSyncCursor).where(MailSyncCursor.smtp_config_id == account.id), execution_options=bulk)
    db.execute(delete(SMTPConfig).where(SMTPConfig.id == account.id), execution_options=bulk)
    db.commit()
    logger.info(
        "User %s deleted mail account %s with %s indexed messages; %s send audit records kept",
        user_id,
        summary["account_id"],
        counts["messages"],
        sends,
    )
    return {"deleted": True, **summary, "removed": counts, "send_audits_kept": sends}
