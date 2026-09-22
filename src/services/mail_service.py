"""Tenant-scoped search, retrieval, and sending."""

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import String, and_, case, cast, func, literal, or_, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from typing_extensions import TypedDict

from src.config import settings
from src.handlers.email_handler_types import SendMailInput
from src.models.attachment import EmailAttachment
from src.models.email import EmailLog
from src.models.participant import MailParticipant
from src.models.placement import MessagePlacement
from src.models.send_audit import SendAudit
from src.models.smtp_config import SMTPConfig
from src.models.user import User
from src.security.search_cursors import issue_search_cursor, verify_search_cursor
from src.services.search_text import attachment_document, match_condition, message_document
from src.services.upload_service import (
    claim_uploads_for_send,
    delivery_outcome,
    load_uploads,
    measure_uploads,
    outbound_attachments,
    plan_upload_dispositions,
    settle_uploads,
)


class MailAccountDetails(TypedDict):
    id: int
    name: str
    address: str
    provider: str
    auth_type: str
    username: str
    imap_host: str
    imap_port: int
    imap_security: str
    smtp_host: str
    smtp_port: int
    smtp_security: str
    credential_configured: bool
    enabled: bool
    sync_state: str
    backfill_complete: bool
    last_attempt: str | None
    last_success: str | None
    last_reconciled: str | None
    last_error_code: str | None
    last_error_message: str | None
    retry_at: str | None
    last_sync: str | None
    message_count: int
    attachment_count: int
    partial_message_count: int


class MailAccountSummary(TypedDict):
    account_count: int
    enabled_account_count: int
    total_message_count: int
    total_attachment_count: int
    total_partial_message_count: int
    count_definition: str
    accounts: list[MailAccountDetails]


class SearchPage(TypedDict):
    items: list[dict[str, Any]]
    total_count: int
    raw_count: int
    returned_count: int
    has_more: bool
    next_cursor: str | None
    match: str
    matched_fields: list[str]
    account_coverage: list[dict[str, Any]]
    warnings: list[dict[str, str]]
    facets: dict[str, list[dict[str, Any]]]


def owned_account(db: Session, user_id: int, account_id: int) -> SMTPConfig:
    account = (
        db.query(SMTPConfig)
        .filter(SMTPConfig.id == account_id, SMTPConfig.owner_user_id == user_id)
        .first()
    )
    if not account:
        raise HTTPException(status_code=404, detail="Mail account not found")
    return account


def owned_email_query(db: Session, user_id: int):
    return db.query(EmailLog).join(SMTPConfig).filter(SMTPConfig.owner_user_id == user_id)


def owned_email(db: Session, user_id: int, email_id: int) -> EmailLog:
    message = owned_email_query(db, user_id).filter(EmailLog.id == email_id).first()
    if not message:
        raise HTTPException(status_code=404, detail="Email not found")
    return message


def mail_account_summary(db: Session, user: User) -> MailAccountSummary:
    """Return exact tenant-scoped mailbox inventory without search pagination."""
    rows = (
        db.query(
            SMTPConfig,
            func.count(EmailLog.id).label("message_count"),
            func.coalesce(func.sum(EmailLog.attachment_count), 0).label(
                "attachment_count"
            ),
            func.coalesce(
                func.sum(
                    case(
                        (EmailLog.content_state != "complete", 1),
                        else_=0,
                    )
                ),
                0,
            ).label("partial_message_count"),
        )
        .outerjoin(
            EmailLog,
            and_(
                EmailLog.smtp_config_id == SMTPConfig.id,
                EmailLog.deleted_at.is_(None),
            ),
        )
        .filter(SMTPConfig.owner_user_id == user.id)
        .group_by(SMTPConfig.id)
        .order_by(SMTPConfig.id)
        .all()
    )
    accounts = [
        {
            "id": account.id,
            "name": account.name,
            "address": account.account_name or account.username,
            "provider": account.provider,
            "auth_type": account.auth_type,
            "username": account.username,
            "imap_host": account.host,
            "imap_port": account.port,
            "imap_security": "ssl" if account.imap_use_ssl else "starttls",
            "smtp_host": account.smtp_host or account.host,
            "smtp_port": account.smtp_port,
            "smtp_security": "ssl" if account.smtp_use_ssl else "starttls",
            "credential_configured": bool(account.credential_ciphertext),
            "enabled": account.enabled,
            "sync_state": "disabled" if not account.enabled else account.sync_state,
            "backfill_complete": account.backfill_complete,
            "last_attempt": account.last_attempt_at.isoformat()
            if account.last_attempt_at
            else None,
            "last_success": account.last_success_at.isoformat()
            if account.last_success_at
            else None,
            "last_reconciled": account.last_reconciled_at.isoformat()
            if account.last_reconciled_at
            else None,
            "last_error_code": account.last_error_code,
            "last_error_message": account.last_error_message,
            "retry_at": account.retry_at.isoformat() if account.retry_at else None,
            "last_sync": account.last_check.isoformat() if account.last_check else None,
            "message_count": int(message_count),
            "attachment_count": int(attachment_count),
            "partial_message_count": int(partial_message_count),
        }
        for account, message_count, attachment_count, partial_message_count in rows
    ]
    return {
        "account_count": len(accounts),
        "enabled_account_count": sum(account["enabled"] for account in accounts),
        "total_message_count": sum(account["message_count"] for account in accounts),
        "total_attachment_count": sum(
            account["attachment_count"] for account in accounts
        ),
        "total_partial_message_count": sum(
            account["partial_message_count"] for account in accounts
        ),
        "count_definition": (
            "Non-deleted messages currently stored by this server. "
            "partial_message_count is indexed from headers only because the "
            "provider message exceeded the configured safe fetch limit."
        ),
        "accounts": accounts,
    }


def serialize_attachment(attachment: EmailAttachment, include_text: bool = False) -> dict[str, Any]:
    result = {
        "id": attachment.id,
        "filename": attachment.filename,
        "content_type": attachment.detected_content_type or attachment.content_type,
        "claimed_content_type": attachment.claimed_content_type,
        "size": attachment.size,
        "sha256": attachment.sha256,
        "extraction_state": attachment.extraction_state,
    }
    if include_text:
        result["text"] = (attachment.text_content or "")[: settings.max_extracted_text_chars]
        result["text_truncated"] = bool(
            attachment.text_content and len(attachment.text_content) > settings.max_extracted_text_chars
        )
    return result


def serialize_email(
    message: EmailLog,
    include_body: bool = False,
    include_attachment_text: bool = False,
    *,
    body_format: str | None = None,
    max_body_chars: int | None = None,
):
    result = {
        "id": message.id,
        "account_id": message.smtp_config_id,
        "sender": message.sender,
        "recipient": message.recipient,
        "subject": message.subject or "",
        "message_id": message.message_id,
        "thread_id": message.provider_thread_id,
        "email_date": message.email_date.isoformat() if message.email_date else None,
        "provider_size": message.provider_size,
        "content_state": message.content_state,
        # Tri-state: null means the provider never reported flags for this
        # message, which is not the same as it having been read.
        "is_unread": message.is_unread,
        "is_flagged": message.is_flagged,
        "is_answered": message.is_answered,
        "attachment_count": message.attachment_count,
        "attachments": [
            serialize_attachment(item, include_text=include_attachment_text) for item in message.attachments
        ],
    }
    if include_body or body_format:
        selected_format = body_format or "both"
        if selected_format not in {"plain", "html", "both", "metadata"}:
            raise HTTPException(status_code=400, detail="Invalid body format")
        body_limit = min(
            max(
                0,
                settings.max_message_body_chars
                if max_body_chars is None
                else max_body_chars,
            ),
            settings.max_message_body_chars,
        )
        plain = message.body_plain or ""
        html = message.body_html or ""
        if selected_format in {"plain", "both"}:
            result["body_plain"] = plain[:body_limit]
        if selected_format in {"html", "both"}:
            result["body_html"] = html[:body_limit]
        result["body_format"] = selected_format
        result["body_truncated"] = (
            (selected_format in {"plain", "both"} and len(plain) > body_limit)
            or (selected_format in {"html", "both"} and len(html) > body_limit)
        )
    return result


def is_excluded_folder(folder: str | None, suffixes: list[str]) -> bool:
    """Match Trash and Junk by suffix: real folders are INBOX.Trash, [Google Mail]/Trash."""
    if not folder:
        return False
    lowered = folder.lower()
    return any(
        lowered == suffix or lowered.endswith(f".{suffix}") or lowered.endswith(f"/{suffix}")
        for suffix in suffixes
    )


def _apply_folder_scope(db, query, folders: list[str] | None, exclude_folders: list[str] | None):
    """Scope a query by folder, via placements.

    The exclusion is "has a placement outside the excluded set", not "has no
    placement inside it": a message can sit in both INBOX and Trash, and the
    stricter reading would hide thousands of live messages. A message with no
    placement at all is kept, because absence of a placement is not evidence of
    being in Trash.
    """
    if folders:
        return query.filter(
            EmailLog.id.in_(
                select(MessagePlacement.email_log_id).where(MessagePlacement.folder.in_(folders))
            )
        )

    suffixes = settings.excluded_folder_suffixes if exclude_folders is None else exclude_folders
    if not suffixes:
        return query
    excluded_names = [
        row[0]
        for row in db.query(MessagePlacement.folder).distinct().all()
        if is_excluded_folder(row[0], suffixes)
    ]
    if not excluded_names:
        return query
    return query.filter(
        ~EmailLog.id.in_(select(MessagePlacement.email_log_id))
        | EmailLog.id.in_(
            select(MessagePlacement.email_log_id).where(MessagePlacement.folder.notin_(excluded_names))
        )
    )


FLAG_COLUMNS = {
    "is_unread": EmailLog.is_unread,
    "is_flagged": EmailLog.is_flagged,
    "is_answered": EmailLog.is_answered,
}


def _apply_flag_filters(query, requested: dict[str, bool | None]):
    """Filter on read state, using SQL two-valued logic on purpose.

    ``is_unread=False`` must not match a message whose flags were never fetched.
    A NULL fails both ``= true`` and ``= false``, which is the honest answer;
    _flag_state_warning then reports how many messages that silently removed.
    """
    for name, value in requested.items():
        if value is not None:
            query = query.filter(FLAG_COLUMNS[name].is_(value))
    return query


def _flag_state_warning(scope_query, requested: dict[str, bool | None]) -> dict[str, str] | None:
    """Report messages excluded from a flag filter only because nothing is known."""
    columns = [FLAG_COLUMNS[name] for name, value in requested.items() if value is not None]
    if not columns:
        return None
    unknown = (
        scope_query.filter(or_(*[column.is_(None) for column in columns]))
        .order_by(None)
        .count()
    )
    if not unknown:
        return None
    return {
        "code": "FLAG_STATE_UNKNOWN",
        "message": (
            f"{unknown} message(s) matching the other filters were excluded because "
            "their read state was never reported by the provider. Messages stored "
            "before flag tracking existed, and messages in accounts that have not "
            "reconciled since, have no flag state to filter on."
        ),
    }


def _folder_scope_warning(db: Session, scope_query, folders, exclude_folders) -> dict[str, str] | None:
    """Report messages a folder filter could not judge either way.

    A message stored before folders were tracked has no placement at all. It is
    deliberately kept by _apply_folder_scope, because absence of a placement is
    not evidence of being in Trash -- but that means a folders= scope silently
    excludes it, and an exclude_folders= scope silently keeps it.
    """
    if not folders and exclude_folders == []:
        return None
    unplaced = (
        scope_query.filter(~EmailLog.id.in_(select(MessagePlacement.email_log_id)))
        .order_by(None)
        .count()
    )
    if not unplaced:
        return None
    if folders:
        effect = "were excluded from this folder scope"
    else:
        effect = "were kept, because there is no evidence they are in an excluded folder"
    return {
        "code": "FOLDER_UNKNOWN",
        "message": (
            f"{unplaced} message(s) matching the other filters have no known folder and {effect}. "
            "They were stored before this server tracked folders."
        ),
    }


def _apply_common_filters(
    query,
    *,
    account_id: int | None,
    date_from: datetime | None,
    date_to: datetime | None,
    participant: str | None,
    has_attachments: bool,
):
    if account_id is not None:
        query = query.filter(EmailLog.smtp_config_id == account_id)
    if date_from:
        query = query.filter(EmailLog.email_date >= date_from)
    if date_to:
        query = query.filter(EmailLog.email_date <= date_to)
    if participant:
        escaped = participant.replace("%", r"\%").replace("_", r"\_")
        query = query.filter(
            or_(
                EmailLog.sender.ilike(f"%{escaped}%", escape="\\"),
                EmailLog.recipient.ilike(f"%{escaped}%", escape="\\"),
            )
        )
    if has_attachments:
        query = query.filter(EmailLog.attachment_count > 0)
    return query


def _participant_filter(terms: list[str]):
    conditions = []
    for raw_term in terms:
        term = raw_term.strip().lower()
        if not term:
            continue
        if term.startswith("@"):
            conditions.append(MailParticipant.domain == term[1:])
        elif "@" in term:
            conditions.append(MailParticipant.email == term)
        elif "." in term and " " not in term:
            conditions.append(MailParticipant.domain == term)
        else:
            escaped = term.replace("%", r"\%").replace("_", r"\_")
            conditions.append(
                or_(
                    MailParticipant.email.ilike(f"%{escaped}%", escape="\\"),
                    MailParticipant.display_name.ilike(
                        f"%{escaped}%",
                        escape="\\",
                    ),
                )
            )
    return or_(*conditions) if conditions else None


def _dedupe_key(mode: str):
    row_key = literal("id:") + cast(EmailLog.id, String)
    message_key = case(
        (
            and_(EmailLog.message_id.is_not(None), EmailLog.message_id != ""),
            literal("message:") + func.lower(EmailLog.message_id),
        ),
        else_=row_key,
    )
    if mode == "exact":
        return message_key
    if mode == "mirror":
        return case(
            (
                EmailLog.content_fingerprint.is_not(None),
                literal("content:") + EmailLog.content_fingerprint,
            ),
            else_=message_key,
        )
    return row_key


def _query_digest(values: dict[str, Any]) -> str:
    canonical = json.dumps(values, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _database_regex_pattern(pattern: str, dialect_name: str) -> str:
    """Translate portable word-boundary escapes to PostgreSQL ARE syntax."""
    if dialect_name != "postgresql":
        return pattern
    translated = []
    index = 0
    while index < len(pattern):
        if pattern[index] == "\\" and index + 1 < len(pattern):
            escaped = pattern[index + 1]
            if escaped == "b":
                translated.append(r"\y")
                index += 2
                continue
            if escaped == "B":
                translated.append(r"\Y")
                index += 2
                continue
            translated.append(pattern[index : index + 2])
            index += 2
            continue
        translated.append(pattern[index])
        index += 1
    return "".join(translated)


def _apply_keyset(query, cursor_date: datetime | None, cursor_id: int):
    if cursor_date is None:
        return query.filter(
            EmailLog.email_date.is_(None),
            EmailLog.id < cursor_id,
        )
    return query.filter(
        or_(
            EmailLog.email_date < cursor_date,
            EmailLog.email_date.is_(None),
            and_(
                EmailLog.email_date == cursor_date,
                EmailLog.id < cursor_id,
            ),
        )
    )


def _account_coverage(
    db: Session,
    user: User,
    account_id: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    query = db.query(SMTPConfig).filter(SMTPConfig.owner_user_id == user.id)
    if account_id is not None:
        query = query.filter(SMTPConfig.id == account_id)
    accounts = query.order_by(SMTPConfig.id).all()
    now = datetime.now(tz=timezone.utc)
    coverage = []
    warnings = []
    for account in accounts:
        last_success = account.last_success_at or account.last_check
        normalized_success = last_success
        if normalized_success and normalized_success.tzinfo is None:
            normalized_success = normalized_success.replace(tzinfo=timezone.utc)
        stale = bool(
            normalized_success
            and (now - normalized_success).total_seconds()
            > settings.sync_stale_after_seconds
        )
        state = "disabled" if not account.enabled else account.sync_state
        entry = {
            "account_id": account.id,
            "name": account.name,
            "address": account.account_name or account.username,
            "enabled": account.enabled,
            "sync_state": state,
            "backfill_complete": account.backfill_complete,
            "last_success": last_success.isoformat() if last_success else None,
            "last_error_code": account.last_error_code,
            "stale": stale,
        }
        coverage.append(entry)
        if not account.enabled:
            warnings.append(
                {
                    "code": "ACCOUNT_DISABLED",
                    "message": f"{account.name} is disabled; results only include previously stored mail.",
                }
            )
        elif not account.backfill_complete:
            warnings.append(
                {
                    "code": "ACCOUNT_BACKFILL_INCOMPLETE",
                    "message": f"{account.name} has not completed its historical backfill.",
                }
            )
        elif state == "error":
            warnings.append(
                {
                    "code": account.last_error_code or "ACCOUNT_SYNC_ERROR",
                    "message": f"{account.name} has a synchronization error.",
                }
            )
        elif stale:
            warnings.append(
                {
                    "code": "ACCOUNT_SYNC_STALE",
                    "message": f"{account.name} has not synchronized recently.",
                }
            )
    return coverage, warnings


def _lexical_matches(
    db: Session,
    message_ids: list[int],
    query_text: str,
    *,
    search_attachments: bool,
    participant_terms: list[str],
    match: str = "stemmed",
) -> tuple[dict[int, list[dict[str, Any]]], set[str]]:
    matches: dict[int, list[dict[str, Any]]] = {
        message_id: [] for message_id in message_ids
    }
    fields: set[str] = set()
    if not message_ids:
        return matches, fields

    if query_text.strip():
        columns = {
            "sender": func.coalesce(EmailLog.sender, ""),
            "recipient": func.coalesce(EmailLog.recipient, ""),
            "subject": func.coalesce(EmailLog.subject, ""),
            "body": func.coalesce(EmailLog.body_plain, ""),
        }
        rows = (
            db.query(
                EmailLog.id,
                *(
                    match_condition(column, query_text, match=match).label(name)
                    for name, column in columns.items()
                ),
            )
            .filter(EmailLog.id.in_(message_ids))
            .all()
        )
        for row in rows:
            for name in columns:
                if getattr(row, name):
                    matches[row.id].append({"field": name})
                    fields.add(name)

        if search_attachments:
            attachment_text = attachment_document()
            attachment_rows = (
                db.query(
                    EmailAttachment.email_log_id,
                    EmailAttachment.id,
                    EmailAttachment.filename,
                    # Highlighting is verbatim whatever the match mode: a stem is
                    # not a substring the reader would recognise in the page.
                    func.ts_headline(
                        "simple",
                        attachment_text,
                        func.websearch_to_tsquery("simple", query_text),
                        "MaxWords=24, MinWords=8, ShortWord=2",
                    ).label("snippet"),
                )
                .filter(
                    EmailAttachment.email_log_id.in_(message_ids),
                    match_condition(attachment_text, query_text, match=match),
                )
                .all()
            )
            for row in attachment_rows:
                matches[row.email_log_id].append(
                    {
                        "field": "attachment",
                        "attachment_id": row.id,
                        "filename": row.filename,
                        "snippet": row.snippet,
                    }
                )
                fields.add("attachment")

    if participant_terms:
        participant_rows = (
            db.query(
                MailParticipant.email_log_id,
                MailParticipant.role,
                MailParticipant.email,
            )
            .filter(
                MailParticipant.email_log_id.in_(message_ids),
                _participant_filter(participant_terms),
            )
            .all()
        )
        for row in participant_rows:
            matches[row.email_log_id].append(
                {
                    "field": "participant",
                    "role": row.role,
                    "email": row.email,
                }
            )
            fields.add("participant")
    return matches, fields


def select_message_ids(
    db: Session,
    user: User,
    *,
    query: str = "",
    account_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    participants: list[str] | None = None,
    has_attachments: bool = False,
    search_attachments: bool = False,
    folders: list[str] | None = None,
    exclude_folders: list[str] | None = None,
    is_unread: bool | None = None,
    is_flagged: bool | None = None,
    is_answered: bool | None = None,
    match: str = "stemmed",
    limit: int = 500,
) -> tuple[list[int], int]:
    """Ids matching the same filters search_mail uses, plus the true total.

    Bulk operations need to act on a search, not on a list of ids: an inbox with
    8,000 newsletters cannot be triaged by naming every message. Returning the
    total separately is what lets a caller see that its limit truncated the work
    rather than silently doing part of it.
    """
    q = owned_email_query(db, user.id).filter(EmailLog.deleted_at.is_(None))
    q = _apply_common_filters(
        q,
        account_id=account_id,
        date_from=date_from,
        date_to=date_to,
        participant=None,
        has_attachments=has_attachments,
    )
    q = _apply_folder_scope(db, q, folders, exclude_folders)
    condition = _participant_filter(
        [term for term in (participants or []) if term and term.strip()]
    )
    if condition is not None:
        q = q.filter(
            EmailLog.id.in_(
                select(db.query(MailParticipant.email_log_id).filter(condition).distinct().subquery())
            )
        )
    if query.strip():
        matched = match_condition(message_document(), query, match=match)
        if search_attachments:
            attachment_ids = (
                db.query(EmailAttachment.email_log_id)
                .filter(match_condition(attachment_document(), query, match=match))
                .subquery()
            )
            matched = or_(matched, EmailLog.id.in_(select(attachment_ids)))
        q = q.filter(matched)
    q = _apply_flag_filters(
        q, {"is_unread": is_unread, "is_flagged": is_flagged, "is_answered": is_answered}
    )
    total = q.order_by(None).count()
    rows = (
        q.with_entities(EmailLog.id)
        .order_by(EmailLog.email_date.desc().nullslast(), EmailLog.id.desc())
        .limit(max(1, limit))
        .all()
    )
    return [row[0] for row in rows], int(total)


def search_mail(
    db: Session,
    user: User,
    *,
    query: str = "",
    account_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    participant: str | None = None,
    participants: list[str] | None = None,
    has_attachments: bool = False,
    search_attachments: bool = False,
    folders: list[str] | None = None,
    exclude_folders: list[str] | None = None,
    is_unread: bool | None = None,
    is_flagged: bool | None = None,
    is_answered: bool | None = None,
    match: str = "stemmed",
    limit: int = 50,
    cursor: str | None = None,
    deduplicate: str = "exact",
) -> SearchPage:
    if deduplicate not in {"none", "exact", "mirror"}:
        raise HTTPException(status_code=400, detail="Invalid deduplication mode")
    if match not in {"stemmed", "exact"}:
        raise HTTPException(status_code=400, detail="Invalid match mode")
    if account_id is not None:
        owned_account(db, user.id, account_id)
    participant_terms = [
        term
        for term in ([participant] if participant else []) + (participants or [])
        if term and term.strip()
    ]
    q = owned_email_query(db, user.id).filter(EmailLog.deleted_at.is_(None))
    q = _apply_common_filters(
        q,
        account_id=account_id,
        date_from=date_from,
        date_to=date_to,
        participant=None,
        has_attachments=has_attachments,
    )
    folder_warning = _folder_scope_warning(db, q, folders, exclude_folders)
    q = _apply_folder_scope(db, q, folders, exclude_folders)
    participant_condition = _participant_filter(participant_terms)
    if participant_condition is not None:
        participant_ids = (
            db.query(MailParticipant.email_log_id)
            .filter(participant_condition)
            .distinct()
            .subquery()
        )
        q = q.filter(EmailLog.id.in_(select(participant_ids)))
    if query.strip():
        condition = match_condition(message_document(), query, match=match)
        if search_attachments:
            attachment_ids = (
                db.query(EmailAttachment.email_log_id)
                .filter(match_condition(attachment_document(), query, match=match))
                .subquery()
            )
            condition = or_(condition, EmailLog.id.in_(select(attachment_ids)))
        q = q.filter(condition)

    flag_filters = {
        "is_unread": is_unread,
        "is_flagged": is_flagged,
        "is_answered": is_answered,
    }
    flag_warning = _flag_state_warning(q, flag_filters)
    q = _apply_flag_filters(q, flag_filters)

    raw_count = q.order_by(None).count()
    key_expression = _dedupe_key(deduplicate)
    if deduplicate == "none":
        result_query = q
        total_count = raw_count
    else:
        base = q.with_entities(
            EmailLog.id.label("message_id"),
            EmailLog.email_date.label("email_date"),
            key_expression.label("logical_key"),
        ).order_by(None).subquery()
        ranked = (
            db.query(
                base.c.message_id,
                base.c.logical_key,
                func.row_number()
                .over(
                    partition_by=base.c.logical_key,
                    order_by=(
                        base.c.email_date.desc().nullslast(),
                        base.c.message_id.desc(),
                    ),
                )
                .label("position"),
            )
            .subquery()
        )
        representative_ids = select(ranked.c.message_id).where(
            ranked.c.position == 1
        )
        total_count = (
            db.query(func.count())
            .select_from(ranked)
            .filter(ranked.c.position == 1)
            .scalar()
            or 0
        )
        result_query = owned_email_query(db, user.id).filter(
            EmailLog.id.in_(representative_ids)
        )

    query_values = {
        "query": query,
        "account_id": account_id,
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
        "participants": sorted(participant_terms),
        "has_attachments": has_attachments,
        "search_attachments": search_attachments,
        "folders": sorted(folders) if folders else None,
        "exclude_folders": sorted(exclude_folders) if exclude_folders is not None else None,
        "is_unread": is_unread,
        "is_flagged": is_flagged,
        "is_answered": is_answered,
        "match": match,
        "deduplicate": deduplicate,
    }
    query_hash = _query_digest(query_values)
    if cursor:
        try:
            cursor_date, cursor_id = verify_search_cursor(
                cursor,
                user.id,
                query_hash,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        result_query = _apply_keyset(result_query, cursor_date, cursor_id)

    page_limit = min(max(1, limit), 100)
    messages = (
        result_query.order_by(
            EmailLog.email_date.desc().nullslast(),
            EmailLog.id.desc(),
        )
        .limit(page_limit + 1)
        .all()
    )
    has_more = len(messages) > page_limit
    messages = messages[:page_limit]
    message_ids = [message.id for message in messages]
    matches, matched_fields = _lexical_matches(
        db,
        message_ids,
        query,
        search_attachments=search_attachments,
        participant_terms=participant_terms,
        match=match,
    )

    logical_keys = {}
    duplicate_sources: dict[str, list[dict[str, int]]] = {}
    if messages:
        logical_keys = dict(
            db.query(EmailLog.id, key_expression)
            .filter(EmailLog.id.in_(message_ids))
            .all()
        )
        if deduplicate != "none":
            selected_keys = set(logical_keys.values())
            source_rows = (
                db.query(
                    EmailLog.id,
                    EmailLog.smtp_config_id,
                    key_expression.label("logical_key"),
                )
                .join(SMTPConfig)
                .filter(
                    SMTPConfig.owner_user_id == user.id,
                    key_expression.in_(selected_keys),
                    EmailLog.deleted_at.is_(None),
                )
                .all()
            )
            for row in source_rows:
                duplicate_sources.setdefault(row.logical_key, []).append(
                    {
                        "email_id": row.id,
                        "account_id": row.smtp_config_id,
                    }
                )

    items = []
    for message in messages:
        item = serialize_email(message)
        key = logical_keys.get(message.id, f"id:{message.id}")
        sources = duplicate_sources.get(
            key,
            [{"email_id": message.id, "account_id": message.smtp_config_id}],
        )
        item["logical_group"] = hashlib.sha256(key.encode()).hexdigest()[:16]
        item["duplicate_count"] = len(sources)
        item["sources"] = sources
        item["matches"] = matches.get(message.id, [])
        items.append(item)

    next_cursor = None
    if has_more and messages:
        last = messages[-1]
        next_cursor = issue_search_cursor(
            user.id,
            query_hash,
            last.email_date,
            last.id,
        )

    account_rows = (
        q.with_entities(
            SMTPConfig.id,
            SMTPConfig.name,
            func.count(EmailLog.id),
        )
        .group_by(SMTPConfig.id, SMTPConfig.name)
        .order_by(SMTPConfig.id)
        .all()
    )
    matching_ids = q.with_entities(EmailLog.id).order_by(None).subquery()
    domain_rows = (
        db.query(
            MailParticipant.domain,
            func.count(func.distinct(MailParticipant.email_log_id)),
        )
        .filter(MailParticipant.email_log_id.in_(select(matching_ids)))
        .group_by(MailParticipant.domain)
        .order_by(func.count(func.distinct(MailParticipant.email_log_id)).desc())
        .limit(25)
        .all()
    )
    coverage, warnings = _account_coverage(db, user, account_id)
    if folder_warning:
        warnings.append(folder_warning)
    if flag_warning:
        warnings.append(flag_warning)
    return {
        "items": items,
        "total_count": int(total_count),
        "raw_count": int(raw_count),
        "returned_count": len(items),
        "has_more": has_more,
        "next_cursor": next_cursor,
        "match": match,
        "matched_fields": sorted(matched_fields),
        "account_coverage": coverage,
        "warnings": warnings,
        "facets": {
            "accounts": [
                {"account_id": row[0], "name": row[1], "count": int(row[2])}
                for row in account_rows
            ],
            "participant_domains": [
                {"domain": row[0], "count": int(row[1])} for row in domain_rows
            ],
        },
    }


def search_mail_regex(
    db: Session,
    user: User,
    *,
    pattern: str,
    field: str | None = None,
    fields: list[str] | None = None,
    account_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    folders: list[str] | None = None,
    exclude_folders: list[str] | None = None,
    is_unread: bool | None = None,
    is_flagged: bool | None = None,
    is_answered: bool | None = None,
    limit: int = 25,
    cursor: str | None = None,
    deduplicate: str = "exact",
) -> dict[str, Any]:
    if not pattern or len(pattern) > settings.max_regex_pattern_length:
        raise HTTPException(status_code=400, detail="Regex pattern is empty or too long")
    try:
        compiled = re.compile(pattern, flags=re.IGNORECASE)
    except re.error as exc:
        raise HTTPException(status_code=400, detail=f"Invalid regex: {exc.msg}") from None
    selected_fields = list(dict.fromkeys((fields or []) + ([field] if field else [])))
    if not selected_fields:
        selected_fields = ["body"]
    valid_fields = {"sender", "recipient", "subject", "body", "attachment"}
    if not set(selected_fields).issubset(valid_fields):
        raise HTTPException(status_code=400, detail="Invalid regex field")
    if account_id is None and date_from is None and date_to is None:
        raise HTTPException(status_code=400, detail="Regex requires an account or date scope")
    if deduplicate not in {"none", "exact", "mirror"}:
        raise HTTPException(status_code=400, detail="Invalid deduplication mode")
    if account_id is not None:
        owned_account(db, user.id, account_id)

    database_pattern = _database_regex_pattern(pattern, db.bind.dialect.name)
    db.execute(text(f"SET LOCAL statement_timeout = '{int(settings.regex_statement_timeout_ms)}ms'"))
    q = owned_email_query(db, user.id).filter(EmailLog.deleted_at.is_(None))
    q = _apply_common_filters(
        q,
        account_id=account_id,
        date_from=date_from,
        date_to=date_to,
        participant=None,
        has_attachments=False,
    )
    folder_warning = _folder_scope_warning(db, q, folders, exclude_folders)
    q = _apply_folder_scope(db, q, folders, exclude_folders)
    columns = {
        "sender": EmailLog.sender,
        "recipient": EmailLog.recipient,
        "subject": EmailLog.subject,
        "body": EmailLog.body_plain,
    }
    conditions = [
        columns[name].op("~*")(database_pattern)
        for name in selected_fields
        if name != "attachment"
    ]
    if "attachment" in selected_fields:
        matching_attachment_ids = (
            q.join(EmailAttachment)
            .filter(EmailAttachment.text_content.op("~*")(database_pattern))
            .with_entities(EmailLog.id)
            .distinct()
            .subquery()
        )
        conditions.append(EmailLog.id.in_(select(matching_attachment_ids)))
    q = q.filter(or_(*conditions))

    flag_filters = {
        "is_unread": is_unread,
        "is_flagged": is_flagged,
        "is_answered": is_answered,
    }
    scope_before_flags = q
    q = _apply_flag_filters(q, flag_filters)

    query_values = {
        "pattern": pattern,
        "fields": sorted(selected_fields),
        "account_id": account_id,
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
        "folders": sorted(folders) if folders else None,
        "exclude_folders": sorted(exclude_folders) if exclude_folders is not None else None,
        "is_unread": is_unread,
        "is_flagged": is_flagged,
        "is_answered": is_answered,
        "deduplicate": deduplicate,
    }
    query_hash = _query_digest(query_values)
    try:
        # Inside the try: this count runs under the same statement timeout.
        flag_warning = _flag_state_warning(scope_before_flags, flag_filters)
        raw_count = q.order_by(None).count()
        key_expression = _dedupe_key(deduplicate)
        if deduplicate == "none":
            result_query = q
            total_count = raw_count
        else:
            base = q.with_entities(
                EmailLog.id.label("message_id"),
                EmailLog.email_date.label("email_date"),
                key_expression.label("logical_key"),
            ).order_by(None).subquery()
            ranked = (
                db.query(
                    base.c.message_id,
                    func.row_number()
                    .over(
                        partition_by=base.c.logical_key,
                        order_by=(
                            base.c.email_date.desc().nullslast(),
                            base.c.message_id.desc(),
                        ),
                    )
                    .label("position"),
                )
                .subquery()
            )
            representative_ids = select(ranked.c.message_id).where(
                ranked.c.position == 1
            )
            total_count = (
                db.query(func.count())
                .select_from(ranked)
                .filter(ranked.c.position == 1)
                .scalar()
                or 0
            )
            result_query = owned_email_query(db, user.id).filter(
                EmailLog.id.in_(representative_ids)
            )

        if cursor:
            try:
                cursor_date, cursor_id = verify_search_cursor(
                    cursor,
                    user.id,
                    query_hash,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            result_query = _apply_keyset(result_query, cursor_date, cursor_id)
        page_limit = min(max(1, limit), 50)
        messages = (
            result_query.order_by(
                EmailLog.email_date.desc().nullslast(),
                EmailLog.id.desc(),
            )
            .limit(page_limit + 1)
            .all()
        )
    except DBAPIError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="Regex failed or exceeded its execution limit") from exc
    has_more = len(messages) > page_limit
    messages = messages[:page_limit]
    message_ids = [message.id for message in messages]
    matches: dict[int, list[dict[str, Any]]] = {
        message_id: [] for message_id in message_ids
    }
    values_by_field = {
        "sender": lambda message: message.sender or "",
        "recipient": lambda message: message.recipient or "",
        "subject": lambda message: message.subject or "",
        "body": lambda message: message.body_plain or "",
    }
    for message in messages:
        for name in selected_fields:
            if name == "attachment":
                continue
            value = values_by_field[name](message)
            for match in list(compiled.finditer(value))[:10]:
                start = max(0, match.start() - 80)
                end = min(len(value), match.end() + 80)
                matches[message.id].append(
                    {
                        "field": name,
                        "match": match.group(0),
                        "snippet": value[start:end],
                    }
                )

    if "attachment" in selected_fields and message_ids:
        attachments = (
            db.query(EmailAttachment)
            .filter(
                EmailAttachment.email_log_id.in_(message_ids),
                EmailAttachment.text_content.op("~*")(database_pattern),
            )
            .all()
        )
        for attachment in attachments:
            value = attachment.text_content or ""
            for match in list(compiled.finditer(value))[:10]:
                start = max(0, match.start() - 80)
                end = min(len(value), match.end() + 80)
                matches[attachment.email_log_id].append(
                    {
                        "field": "attachment",
                        "attachment_id": attachment.id,
                        "filename": attachment.filename,
                        "match": match.group(0),
                        "snippet": value[start:end],
                    }
                )

    logical_keys = {}
    duplicate_sources: dict[str, list[dict[str, int]]] = {}
    if messages:
        logical_keys = dict(
            db.query(EmailLog.id, key_expression)
            .filter(EmailLog.id.in_(message_ids))
            .all()
        )
        if deduplicate != "none":
            selected_keys = set(logical_keys.values())
            source_rows = (
                db.query(
                    EmailLog.id,
                    EmailLog.smtp_config_id,
                    key_expression.label("logical_key"),
                )
                .join(SMTPConfig)
                .filter(
                    SMTPConfig.owner_user_id == user.id,
                    key_expression.in_(selected_keys),
                    EmailLog.deleted_at.is_(None),
                )
                .all()
            )
            for row in source_rows:
                duplicate_sources.setdefault(row.logical_key, []).append(
                    {
                        "email_id": row.id,
                        "account_id": row.smtp_config_id,
                    }
                )

    items = []
    for message in messages:
        item = serialize_email(message)
        key = logical_keys.get(message.id, f"id:{message.id}")
        sources = duplicate_sources.get(
            key,
            [{"email_id": message.id, "account_id": message.smtp_config_id}],
        )
        item["logical_group"] = hashlib.sha256(key.encode()).hexdigest()[:16]
        item["duplicate_count"] = len(sources)
        item["sources"] = sources
        item["matches"] = matches[message.id]
        items.append(item)
    next_cursor = None
    if has_more and messages:
        last = messages[-1]
        next_cursor = issue_search_cursor(
            user.id,
            query_hash,
            last.email_date,
            last.id,
        )
    coverage, warnings = _account_coverage(db, user, account_id)
    if folder_warning:
        warnings.append(folder_warning)
    if flag_warning:
        warnings.append(flag_warning)
    return {
        "items": items,
        "total_count": int(total_count),
        "raw_count": int(raw_count),
        "returned_count": len(items),
        "has_more": has_more,
        "next_cursor": next_cursor,
        "fields": selected_fields,
        "account_coverage": coverage,
        "warnings": warnings,
    }


def _message_header_ids(value: str | None) -> set[str]:
    return {
        match.strip()
        for match in re.findall(r"<[^<>]+>", value or "")
        if match.strip()
    }


def get_thread(
    db: Session,
    user: User,
    email_id: int,
    *,
    body_format: str = "plain",
    max_body_chars: int = 20_000,
    limit: int = 100,
) -> dict[str, Any]:
    anchor = owned_email(db, user.id, email_id)
    page_limit = min(max(1, limit), 100)
    q = owned_email_query(db, user.id).filter(
        EmailLog.smtp_config_id == anchor.smtp_config_id,
        EmailLog.deleted_at.is_(None),
    )
    if anchor.provider_thread_id:
        q = q.filter(EmailLog.provider_thread_id == anchor.provider_thread_id)
        method = "provider_thread_id"
        confidence = "high"
        messages = q.order_by(EmailLog.email_date.asc(), EmailLog.id.asc()).limit(
            page_limit + 1
        ).all()
    else:
        known_headers = {anchor.message_id}
        known_headers.update(_message_header_ids(anchor.in_reply_to))
        known_headers.update(_message_header_ids(anchor.references))
        message_ids = {anchor.id}
        for _ in range(12):
            reference_conditions = [
                EmailLog.references.contains(header)
                for header in sorted(known_headers)
            ]
            candidates = (
                q.filter(
                    or_(
                        EmailLog.message_id.in_(known_headers),
                        EmailLog.in_reply_to.in_(known_headers),
                        *reference_conditions,
                    )
                )
                .limit(page_limit + 1)
                .all()
            )
            previous = (len(message_ids), len(known_headers))
            for message in candidates:
                message_ids.add(message.id)
                known_headers.add(message.message_id)
                known_headers.update(_message_header_ids(message.in_reply_to))
                known_headers.update(_message_header_ids(message.references))
            if previous == (len(message_ids), len(known_headers)):
                break
            if len(message_ids) > page_limit:
                break

        if len(message_ids) > 1:
            method = "reply_headers"
            confidence = "high"
            messages = (
                q.filter(EmailLog.id.in_(message_ids))
                .order_by(EmailLog.email_date.asc(), EmailLog.id.asc())
                .limit(page_limit + 1)
                .all()
            )
        else:
            normalized = re.sub(
                r"^(re|fw|fwd):\s*",
                "",
                anchor.subject or "",
                flags=re.IGNORECASE,
            ).strip()
            if normalized:
                messages = (
                    q.filter(func.lower(EmailLog.subject).contains(normalized.lower()))
                    .order_by(EmailLog.email_date.asc(), EmailLog.id.asc())
                    .limit(page_limit + 1)
                    .all()
                )
                method = "subject_fallback"
                confidence = "low"
            else:
                messages = [anchor]
                method = "single_message"
                confidence = "low"
    truncated = len(messages) > page_limit
    messages = messages[:page_limit]
    return {
        "messages": [
            serialize_email(
                message,
                body_format=body_format,
                max_body_chars=max_body_chars,
            )
            for message in messages
        ],
        "message_count": len(messages),
        "truncated": truncated,
        "reconstruction_method": method,
        "confidence": confidence,
    }


async def send_mail(
    db: Session,
    user: User,
    payload: SendMailInput,
    *,
    attachments: list[dict] | None = None,
    send_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from src.handlers.email_handler import email_sender_manager

    account = owned_account(db, user.id, payload.account_id)
    reply_message = None
    effective_send_options = dict(send_options or {})
    if payload.reply_to_email_id is not None:
        reply_message = owned_email(db, user.id, payload.reply_to_email_id)
        if reply_message.smtp_config_id != account.id:
            raise HTTPException(
                status_code=400,
                detail="A threaded reply must use the mailbox that owns the original message",
            )
        parent_ids = re.findall(
            r"<[^<>\s\r\n]{1,253}>",
            reply_message.message_id or "",
        )
        if not parent_ids:
            raise HTTPException(
                status_code=400,
                detail="The original message has no usable RFC Message-ID for threading",
            )
        parent_id = parent_ids[0]
        reference_ids = re.findall(
            r"<[^<>\s\r\n]{1,253}>",
            reply_message.references or "",
        )
        if parent_id not in reference_ids:
            reference_ids.append(parent_id)
        effective_send_options.update(
            {
                "in_reply_to": parent_id,
                "references": " ".join(reference_ids[-100:]),
            }
        )
    if not account.credential_ciphertext:
        raise HTTPException(
            status_code=400,
            detail="Mailbox password is not configured",
        )
    recipients = payload.to_addresses + payload.cc_addresses + payload.bcc_addresses
    if not recipients or len(recipients) > settings.max_send_recipients:
        raise HTTPException(status_code=400, detail="Recipient count is outside the configured limit")
    # Newly uploaded bytes and already-indexed attachments are parts of one
    # message, so one cap governs both rather than each list separately -- and
    # an embedded image is as much a part of the message as an appended file,
    # so the two upload lists are weighed together as well.
    upload_ids, inline_upload_ids, named_uploads = plan_upload_dispositions(
        payload.upload_ids, payload.inline_upload_ids
    )
    if len(attachments or []) + len(named_uploads) > settings.max_outbound_attachments:
        raise HTTPException(status_code=400, detail="Too many outbound attachments")
    attachment_bytes = sum(len(item.get("data", b"")) for item in attachments or [])
    if attachment_bytes > settings.max_outbound_attachment_bytes:
        raise HTTPException(status_code=400, detail="Outbound attachments exceed the configured limit")
    recent_sends = (
        db.query(SendAudit)
        .filter(
            SendAudit.owner_user_id == user.id,
            SendAudit.created_at >= datetime.now(tz=timezone.utc) - timedelta(minutes=1),
        )
        .count()
    )
    if recent_sends >= settings.max_sends_per_minute:
        raise HTTPException(status_code=429, detail="Outbound send rate limit exceeded")

    canonical_payload = payload.model_dump(mode="json")
    canonical_payload["attachments"] = [
        {
            "filename": item.get("filename"),
            "sha256": hashlib.sha256(item["data"]).hexdigest(),
        }
        for item in attachments or []
    ]
    # The deduplicated lists, not the ones as spelled: naming the same slot twice is
    # the same request as naming it once, and a retry that tidies its own argument
    # must not read as a different one.
    canonical_payload["upload_ids"] = upload_ids
    canonical_payload["inline_upload_ids"] = inline_upload_ids
    canonical_payload["send_options"] = effective_send_options
    canonical = json.dumps(canonical_payload, separators=(",", ":"), sort_keys=True)
    request_hash = hashlib.sha256(canonical.encode()).hexdigest()
    key = payload.idempotency_key or str(uuid.uuid4())
    existing = (
        db.query(SendAudit)
        .filter(SendAudit.owner_user_id == user.id, SendAudit.idempotency_key == key)
        .first()
    )
    if existing:
        if existing.request_hash != request_hash:
            raise HTTPException(status_code=409, detail="Idempotency key was used for a different request")
        if existing.status == "sent":
            return {
                "success": True,
                "idempotent_replay": True,
                "audit_id": existing.id,
                "threaded_reply": existing.reply_to_email_id is not None,
                "reply_to_email_id": existing.reply_to_email_id,
            }
        raise HTTPException(status_code=409, detail=f"Prior send is {existing.status}; it will not be retried")

    # Only now are the uploaded payloads claimed and read. A replay of an
    # already-sent key returned above without touching them, which is what makes
    # a genuine retry work: the slots it names are consumed, and the claim would
    # refuse them. The hash stays identical across the two calls because
    # upload_ids are server-generated, single-use identifiers carried in the
    # payload itself, so naming them discriminates a request exactly as well as
    # hashing their bytes would -- without needing bytes that a spent slot will
    # no longer hand over.
    #
    # Sized before anything is claimed. The rows already record what arrived, so
    # a message that cannot fit is refused without touching a single slot --
    # where doing it after the claim cost two write transactions per named slot
    # (one to take it, one to hand it back) for a request that was always going
    # to be refused. The refusal path writes no SendAudit row and the rate limit
    # is a COUNT over those rows, so that cost was unthrottled and repeatable.
    #
    # measure_uploads only reads the recorded sizes; it never opens a payload,
    # so at stock caps this refuses a 500MB request without reading 500MB. Both
    # upload lists are weighed together, because an embedded image and an
    # appended file occupy the same message.
    if attachment_bytes + measure_uploads(db, user.id, named_uploads) > settings.max_outbound_attachment_bytes:
        raise HTTPException(status_code=400, detail="Outbound attachments exceed the configured limit")

    # The claim comes first and the bytes second. Between reading a slot's state
    # and consuming it there is a whole SMTP conversation, which is more than
    # enough room for a second request to pass the same check and send the same
    # payload again; moving the slot out of 'stored' in one conditional UPDATE is
    # what makes "single use" true rather than merely likely.
    #
    # One claim over both lists, so a message that cannot have every part it
    # names sends none of them -- and so a slot cannot be spent as an attachment
    # by one request while another embeds it.
    claimed = claim_uploads_for_send(db, user.id, named_uploads) if named_uploads else []
    # What the slots' settlement will be told. It starts at the only thing that
    # is true before a transport exists: nothing has been delivered, so the
    # slots stay honestly re-sendable. It is widened exactly when a delivery
    # attempt becomes possible, and narrowed again the moment the sender
    # actually reports.
    outcome = "failed"
    try:
        uploads = load_uploads(db, claimed)
        outbound = list(attachments or []) + outbound_attachments(uploads, inline_upload_ids)

        audit = SendAudit(
            owner_user_id=user.id,
            smtp_config_id=account.id,
            reply_to_email_id=reply_message.id if reply_message else None,
            idempotency_key=key,
            recipients_json=json.dumps(recipients),
            subject=payload.subject,
            request_hash=request_hash,
            status="pending",
        )
        db.add(audit)
        db.commit()
        db.refresh(audit)

        # From here the message may reach an MTA, and anything that stops this
        # coroutine without a reported result -- a cancelled request, a worker
        # shutting down mid-conversation -- leaves delivery genuinely
        # unestablished. The pessimistic value holds only until the sender
        # answers.
        outcome = "unknown"
        result = await email_sender_manager.send_email_via_config(
            smtp_config_id=account.id,
            owner_user_id=user.id,
            to_addresses=payload.to_addresses,
            subject=payload.subject,
            body_text=payload.body_text,
            body_html=payload.body_html,
            cc_addresses=payload.cc_addresses or None,
            bcc_addresses=payload.bcc_addresses or None,
            reply_to=payload.reply_to,
            attachments=outbound or None,
            **effective_send_options,
        )
        # The sender did report, so route what it said rather than the default.
        # 'failed' means refused and the slots go back; 'unknown' and 'partial'
        # mean the message may be -- or, for 'partial', demonstrably is --
        # delivered, and those retire the slots instead of recycling them.
        outcome = delivery_outcome(result)
        audit.status = "sent" if result.get("success") else result.get("delivery_state", "failed")
        audit.provider_message_id = result.get("message_id")
        audit.provider_response = json.dumps(result, default=str)[:10_000]
        audit.completed_at = datetime.now(tz=timezone.utc)
        db.commit()
        if not result.get("success"):
            raise HTTPException(status_code=502, detail=result.get("message", "SMTP send failed"))
    except BaseException:
        # A refused send hands the slots back, so the caller can correct
        # whatever failed and send the same bytes again without uploading them
        # a second time -- but an *ambiguous* one must not, or the retry
        # delivers the same confidential message twice. Defensively, because a
        # cleanup that raises here would strand the very slots it exists to
        # free.
        settle_uploads(db, claimed, outcome=outcome)
        raise
    # Inside no try of its own but never able to raise: by this point the
    # message is on the wire, and a bookkeeping failure that propagated would
    # leave the slot stuck in 'sending' for the whole TTL while telling the
    # caller a delivered message had failed.
    settle_uploads(db, claimed, outcome=outcome)
    return {
        **result,
        "audit_id": audit.id,
        "idempotency_key": key,
        "threaded_reply": reply_message is not None,
        "reply_to_email_id": reply_message.id if reply_message else None,
    }
