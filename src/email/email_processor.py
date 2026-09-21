"""Email processing and orchestration."""

import asyncio
import contextlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import List

from sqlalchemy import func, or_

from src.database.connection import SessionLocal, get_db_session
from src.email import sanitize_db_text
from src.email.gmail_api_client import GmailApiClient, GmailHistoryExpired
from src.email.message_flags import apply_flag_state, normalize_flags
from src.email.smtp_client import SMTPClient
from src.email.text_extractor import TextExtractor
from src.models.email import EmailLog
from src.models.participant import MailParticipant
from src.models.placement import MessagePlacement
from src.models.smtp_config import SMTPConfig
from src.services.message_metadata import content_fingerprint, participant_models

logger = logging.getLogger(__name__)


def upsert_placement(
    db,
    email_log_id: int,
    folder: str | None,
    uid: int | None,
    uid_validity: int | None,
    *,
    exclusive: bool = False,
) -> None:
    """Record that a message is filed in a folder, replacing any earlier UID.

    exclusive is for providers where a message has one location rather than
    several. An IMAP message really can sit in INBOX and Trash at once; a Gmail
    message cannot, and letting its old location survive a relabel would leave it
    filed in both, counted twice, and addressable at whichever the ranking picked.
    """
    if not folder:
        return
    if exclusive:
        db.query(MessagePlacement).filter(
            MessagePlacement.email_log_id == email_log_id,
            MessagePlacement.folder != folder,
        ).delete(synchronize_session=False)
    placement = (
        db.query(MessagePlacement)
        .filter(MessagePlacement.email_log_id == email_log_id, MessagePlacement.folder == folder)
        .first()
    )
    if placement:
        placement.uid = uid
        placement.uid_validity = uid_validity
        placement.seen_at = datetime.now(tz=timezone.utc)
        return
    db.add(MessagePlacement(email_log_id=email_log_id, folder=folder, uid=uid, uid_validity=uid_validity))


def mailbox_key(account: SMTPConfig) -> tuple[str, int, str]:
    """What a lease must actually protect.

    Two smtp_configs rows can name the same physical mailbox -- in production two
    do, under different owners. A lease keyed on the row id would let a write on
    one run concurrently with a census on the other, and an untagged EXPUNGE
    landing mid-census renumbers the sequence numbers the census is reading.
    """
    return (account.host, account.port, (account.username or "").lower())


def acquire_mailbox_lease(db, account: SMTPConfig, *, seconds: float, token: str | None = None) -> str | None:
    """Take the sync lease on every account row sharing this mailbox.

    Returns a token, or None if any of them is already held. One statement, so
    there is no window between testing and taking.
    """
    from src.config import settings

    now = datetime.now(tz=timezone.utc)
    seconds = min(seconds, settings.sync_job_timeout_seconds + settings.sync_job_cleanup_seconds)
    if seconds <= 0:
        return None
    token = token or uuid.uuid4().hex
    host, port, username = mailbox_key(account)
    siblings = (
        db.query(SMTPConfig)
        .filter(
            SMTPConfig.host == host,
            SMTPConfig.port == port,
            func.lower(SMTPConfig.username) == username,
        )
        .count()
    )
    updated = (
        db.query(SMTPConfig)
        .filter(
            SMTPConfig.host == host,
            SMTPConfig.port == port,
            func.lower(SMTPConfig.username) == username,
            or_(
                SMTPConfig.sync_lock_expires_at.is_(None),
                SMTPConfig.sync_lock_expires_at < now,
            ),
        )
        .update(
            {
                SMTPConfig.sync_lock_token: token,
                SMTPConfig.sync_locked_at: now,
                SMTPConfig.sync_lock_expires_at: now + timedelta(seconds=seconds),
            },
            synchronize_session=False,
        )
    )
    if updated != siblings:
        # Partial acquisition is not acquisition: release what was taken.
        db.rollback()
        return None
    db.commit()
    return token


def refresh_mailbox_lease(token: str, *, seconds: int) -> bool:
    """Refresh without resurrecting or extending beyond the original attempt cap."""
    from src.config import settings

    now = datetime.now(tz=timezone.utc)
    with SessionLocal.begin() as db:
        rows = db.query(SMTPConfig).filter(
            SMTPConfig.sync_lock_token == token,
        ).order_by(SMTPConfig.id).with_for_update().all()
        if not rows:
            return False
        expirations = []
        for account in rows:
            started = account.sync_locked_at
            expires = account.sync_lock_expires_at
            if started is None or expires is None:
                return False
            started = started.replace(tzinfo=timezone.utc) if started.tzinfo is None else started
            expires = expires.replace(tzinfo=timezone.utc) if expires.tzinfo is None else expires
            cap = started + timedelta(seconds=settings.sync_job_timeout_seconds + settings.sync_job_cleanup_seconds)
            if now >= min(expires, cap):
                return False
            expirations.append(min(now + timedelta(seconds=seconds), cap))
        for account, expires in zip(rows, expirations, strict=True):
            account.sync_lock_expires_at = expires
    return True


def release_mailbox_lease(db, token: str) -> None:
    db.query(SMTPConfig).filter(SMTPConfig.sync_lock_token == token).update(
        {
            SMTPConfig.sync_lock_token: None,
            SMTPConfig.sync_locked_at: None,
            SMTPConfig.sync_lock_expires_at: None,
        },
        synchronize_session=False,
    )
    db.commit()


def _chunks(values, size: int = 1000):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start : start + size]


def apply_folder_snapshots(db, *, config_id: int, snapshots: dict) -> None:
    """Mirror one full pass over every folder into placements.

    Deliberately does not try to recognise a moved message here. The ordinary sync
    pass discovers it in its new folder and upserts by identity, which is far cheaper
    than fetching a Message-ID header for every UID in the mailbox. This function only
    retires placements and tombstones messages that lost every placement they had.

    Everything is set-based. Loading an EmailLog per placement pulls body_plain and
    body_html into the session for every message in the mailbox: on a 27,000-message
    account that is 1.2 GB, which killed the container and left the pass unable to
    finish, so last_reconciled_at never advanced and the next cycle retried forever.
    """
    from src.config import settings
    from src.services.mail_service import is_excluded_folder

    now = datetime.now(tz=timezone.utc)
    excluded = settings.excluded_folder_suffixes

    # Columns only: 27,000 tuples is fine, 27,000 mapped objects with bodies is not.
    rows = (
        db.query(
            MessagePlacement.id,
            MessagePlacement.email_log_id,
            MessagePlacement.folder,
            MessagePlacement.uid,
            MessagePlacement.uid_validity,
        )
        .join(EmailLog, EmailLog.id == MessagePlacement.email_log_id)
        .filter(EmailLog.smtp_config_id == config_id)
        .all()
    )

    # Only a message that demonstrably had a placement can be judged missing. Rows
    # that predate the folder column have none and must never be touched.
    candidates = {row.email_log_id for row in rows}
    retired: list[int] = []
    observed: dict[int, tuple[tuple, str]] = {}

    # A UIDVALIDITY change renumbers every UID, so a placement carrying the old
    # value names nothing. Skipping it forever is what left 6,978 messages on one
    # account filed in a folder they had already left, still ranked as live mail.
    # It is only safe to retire such a placement when the message is addressable
    # somewhere else, so that this can never be the step that unplaces a message
    # and makes it look deleted.
    addressable: set[int] = set()
    for row in rows:
        state = snapshots.get(row.folder)
        # A folder the census could not see is not evidence of anything.
        if state is None or row.uid_validity is None or row.uid_validity == state["uid_validity"]:
            addressable.add(row.email_log_id)

    for row in rows:
        state = snapshots.get(row.folder)
        if state is None:
            continue
        if row.uid_validity is not None and row.uid_validity != state["uid_validity"]:
            if row.email_log_id in addressable:
                retired.append(row.id)
            continue
        if row.uid not in state["uids"]:
            retired.append(row.id)
            continue
        raw = state["flags"].get(row.uid)
        if raw is None:
            continue
        # A message can sit in INBOX and Trash at once. Take the live folder's flags,
        # then the first folder by name; without a rule this was decided by whichever
        # folder the snapshot dict happened to yield last.
        key = (is_excluded_folder(row.folder, excluded), row.folder or "")
        current = observed.get(row.email_log_id)
        if current is None or key < current[0]:
            observed[row.email_log_id] = (key, raw)

    for chunk in _chunks(retired):
        db.query(MessagePlacement).filter(MessagePlacement.id.in_(chunk)).delete(
            synchronize_session=False
        )

    by_flags: dict[str, list[int]] = {}
    for email_log_id, (_key, raw) in observed.items():
        by_flags.setdefault(raw, []).append(email_log_id)

    for raw, ids in by_flags.items():
        state = normalize_flags(raw)
        stored = sanitize_db_text(raw)
        values = {
            "flags": stored,
            "is_unread": state.is_unread,
            "is_flagged": state.is_flagged,
            "is_answered": state.is_answered,
        }
        for chunk in _chunks(ids):
            # is_distinct_from keeps an unchanged mailbox from rewriting every row,
            # which would rebuild two GIN body indexes on every pass.
            db.query(EmailLog).filter(
                EmailLog.smtp_config_id == config_id,
                EmailLog.id.in_(chunk),
                EmailLog.flags.is_distinct_from(stored),
            ).update(values, synchronize_session=False)

    db.flush()

    if not candidates:
        return
    survivors: set[int] = set()
    for chunk in _chunks(candidates):
        survivors.update(
            row[0]
            for row in db.query(MessagePlacement.email_log_id)
            .filter(MessagePlacement.email_log_id.in_(chunk))
            .all()
        )
    doomed = candidates - survivors
    # A census that silently came back short looks exactly like a mailbox someone
    # emptied. Refuse rather than guess: no real cycle retires a large fraction of
    # an account at once, and a refusal costs one stale cycle where a wrong
    # inference costs the mail.
    limit = max(20, min(200, len(candidates) // 50))
    if len(doomed) > limit:
        logger.error(
            "Refusing to tombstone %d of %d message(s) on account %s in one pass "
            "(limit %d): the folder census is probably incomplete",
            len(doomed),
            len(candidates),
            config_id,
            limit,
        )
        return

    # Always tombstone, whatever upstream_delete_policy says. A message is briefly
    # unplaced while a move is in flight, and reap_tombstoned_messages performs the
    # physical removal once the account is healthy and the grace period has passed.
    for chunk in _chunks(sorted(doomed)):
        db.query(EmailLog).filter(
            EmailLog.smtp_config_id == config_id,
            EmailLog.id.in_(chunk),
            EmailLog.deleted_at.is_(None),
        ).update({"deleted_at": now}, synchronize_session=False)


def reap_tombstoned_messages(db, *, config_id: int, now: datetime | None = None) -> int:
    """Physically remove messages tombstoned for longer than the grace period.

    Only from a healthy, fully backfilled account: a partial view of a mailbox is
    what produces a wrong deletion inference in the first place.
    """
    from src.config import settings

    if settings.upstream_delete_policy == "retain":
        return 0

    account = db.query(SMTPConfig).filter(SMTPConfig.id == config_id).first()
    if account is None or not account.backfill_complete or account.sync_state != "healthy":
        return 0

    cutoff = (now or datetime.now(tz=timezone.utc)) - timedelta(days=settings.tombstone_grace_days)
    doomed = (
        db.query(EmailLog)
        .filter(
            EmailLog.smtp_config_id == config_id,
            EmailLog.deleted_at.is_not(None),
            EmailLog.deleted_at < cutoff,
        )
        .all()
    )
    for message in doomed:
        db.delete(message)
    if doomed:
        logger.info("Reaped %d tombstoned message(s) from account %s", len(doomed), config_id)
    return len(doomed)



class EmailProcessor:
    """Main email processing orchestrator."""

    def __init__(self):
        self.active_clients = {}
        self.processing = False
        self._active_lock_tokens = {}

    async def start_processing(self):
        """Start the email processing loop."""
        if self.processing:
            logger.warning("Email processing already running")
            return

        self.processing = True
        logger.info("Starting email processing")

        while self.processing:
            try:
                await self._process_all_servers()
                from src.config import settings

                await asyncio.sleep(settings.email_check_interval)
            except Exception as e:
                logger.error("Error in email processing loop: %s", e)
                await asyncio.sleep(60)  # Wait longer on error

    async def stop_processing(self):
        """Stop email processing and cleanup."""
        logger.info("Stopping email processing")
        self.processing = False

        # Disconnect all clients
        for client in self.active_clients.values():
            await client.disconnect()
        self.active_clients.clear()

    async def _process_all_servers(self):
        """Process emails from all enabled SMTP servers."""
        with get_db_session() as db:
            # Get all enabled SMTP configs
            configs = (
                db.query(SMTPConfig)
                .filter(
                    SMTPConfig.enabled,
                    SMTPConfig.credential_ciphertext.isnot(None),
                    SMTPConfig.credential_ciphertext != "",
                )
                .order_by(
                    SMTPConfig.backfill_complete.desc(),
                    SMTPConfig.last_success_at.asc().nullsfirst(),
                    SMTPConfig.id,
                )
                .all()
            )

            if not configs:
                logger.debug("No enabled SMTP configurations found")
                return

            # Create detached config copies to avoid session issues
            config_copies = [SMTPConfig.create_detached(config) for config in configs]

        from src.config import settings

        semaphore = asyncio.Semaphore(max(1, settings.sync_account_concurrency))

        async def process_bounded(config_copy):
            async with semaphore:
                return await self._process_server(config_copy)

        tasks = [
            asyncio.create_task(process_bounded(config_copy))
            for config_copy in config_copies
        ]

        # Wait for all servers to complete (outside the loop for parallel processing)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _process_server(self, config: SMTPConfig, force: bool = False, *, leased: bool = False):
        """Process emails from a single server."""
        config_id = config.id
        completed = False
        lock_token = None
        heartbeat_task = None

        try:
            retry_after = config.retry_at
            if retry_after and retry_after.tzinfo is None:
                retry_after = retry_after.replace(tzinfo=timezone.utc)
            if not force and retry_after and datetime.now(tz=timezone.utc) < retry_after:
                return False
            lock_token = (
                self._active_lock_tokens.get(config_id) if leased else self._acquire_sync_lease(config_id)
            )
            if not lock_token:
                logger.info("Skipping account %s because another worker holds its sync lock", config_id)
                return False
            self._active_lock_tokens[config_id] = lock_token
            self._mark_sync_attempt(config_id)
            # Supervised attempts get one finite lease, never a background renewal.
            # The external process supervisor, not cancellation, owns their lifetime.

            if config.provider == "gmail" and config.auth_type == "oauth2":
                await self._process_gmail_api(config)
            else:
                # Get or create an IMAP client for this server.
                client_key = config_id
                if client_key not in self.active_clients:
                    self.active_clients[client_key] = SMTPClient(config)
                else:
                    await self.active_clients[client_key].update_config(config)

                client = self.active_clients[client_key]

                # Stats are updated after each batch so an interrupted
                # mailbox scan can resume from its durable folder cursor.
                from src.config import settings

                async for batch in client.fetch_new_emails(
                    limit=settings.imap_backfill_messages_per_cycle,
                    since=None,
                ):
                    await self._process_emails(batch)
                    if batch:
                        await self._update_server_stats(config, len(batch))
                        self._persist_sync_cursors(batch)
                self._persist_client_cursors(config_id, client)
                with get_db_session() as db:
                    db_account = (
                        db.query(SMTPConfig)
                        .filter(SMTPConfig.id == config_id)
                        .one()
                    )
                    db_account.backfill_complete = client.backfill_complete
                if client.backfill_complete:
                    try:
                        await self._reconcile_provider_state(config_id, client)
                    except Exception as exc:
                        logger.warning(
                            "Provider state reconciliation failed for account %s: %s",
                            config_id,
                            exc,
                        )
            completed = True

        except asyncio.CancelledError:
            self._mark_sync_interrupted(config_id)
            raise
        except Exception as e:
            if not self.processing:
                self._mark_sync_interrupted(config_id)
                logger.info(
                    "Synchronization interrupted during shutdown for account %s",
                    config_id,
                )
                return False
            failures = max(1, int(getattr(config, "consecutive_failures", 0) or 0) + 1)
            retry_seconds = min(3600, 30 * (2 ** min(failures - 1, 7)))
            self._mark_sync_failure(config_id, e, failures, retry_seconds)
            logger.error(
                "Error processing server %s: %s: %r; retrying in %ss",
                getattr(config, "name", "unknown"),
                type(e).__name__,
                e,
                retry_seconds,
            )
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            if completed:
                try:
                    self._mark_sync_success(config_id)
                except Exception as e:
                    logger.error("Error updating last_check for config %s: %s", config_id, e)
            if lock_token and not leased:
                self._release_sync_lease(config_id, lock_token)
                self._active_lock_tokens.pop(config_id, None)

        return completed

    @staticmethod
    def _sync_error(exc: Exception) -> tuple[str, str]:
        message = str(exc).replace("\n", " ").strip()[:500]
        lowered = message.lower()
        if "auth" in lowered or "login" in lowered:
            code = "ACCOUNT_AUTH_FAILED"
        elif "timeout" in lowered or "timed out" in lowered:
            code = "PROVIDER_TIMEOUT"
        elif "rate" in lowered or "429" in lowered:
            code = "PROVIDER_RATE_LIMITED"
        else:
            code = "PROVIDER_SYNC_FAILED"
        return code, message or type(exc).__name__

    @staticmethod
    def _acquire_sync_lease(config_id: int) -> str | None:
        from src.config import settings

        now = datetime.now(tz=timezone.utc)
        token = uuid.uuid4().hex
        with SessionLocal.begin() as db:
            updated = (
                db.query(SMTPConfig)
                .filter(
                    SMTPConfig.id == config_id,
                    or_(
                        SMTPConfig.sync_lock_expires_at.is_(None),
                        SMTPConfig.sync_lock_expires_at < now,
                    ),
                )
                .update(
                    {
                        SMTPConfig.sync_lock_token: token,
                        SMTPConfig.sync_locked_at: now,
                        SMTPConfig.sync_lock_expires_at: now
                        + timedelta(seconds=settings.sync_lease_seconds),
                    },
                    synchronize_session=False,
                )
            )
        return token if updated else None

    def _refresh_sync_lease(self, config_id: int) -> bool:
        from src.config import settings

        token = self._active_lock_tokens.get(config_id)
        if not token:
            return False
        return refresh_mailbox_lease(token, seconds=settings.sync_lease_seconds)

    async def _maintain_sync_lease(
        self,
        config_id: int,
        owner_task: asyncio.Task | None,
    ) -> None:
        """Refresh a live lease independently of slow provider operations."""
        from src.config import settings

        interval = max(1.0, min(30.0, settings.sync_lease_seconds / 3))
        while True:
            await asyncio.sleep(interval)
            try:
                if not self._refresh_sync_lease(config_id):
                    logger.error("Lost synchronization lease for account %s", config_id)
                    if owner_task:
                        owner_task.cancel()
                    return
            except Exception as exc:
                logger.warning(
                    "Could not refresh synchronization lease for account %s: %s",
                    config_id,
                    exc,
                )

    @staticmethod
    def _release_sync_lease(config_id: int, token: str) -> None:
        with SessionLocal.begin() as db:
            db.query(SMTPConfig).filter(
                SMTPConfig.id == config_id,
                SMTPConfig.sync_lock_token == token,
            ).update(
                {
                    SMTPConfig.sync_lock_token: None,
                    SMTPConfig.sync_locked_at: None,
                    SMTPConfig.sync_lock_expires_at: None,
                },
                synchronize_session=False,
            )

    @staticmethod
    def _mark_sync_attempt(config_id: int) -> None:
        now = datetime.now(tz=timezone.utc)
        with SessionLocal.begin() as db:
            db.query(SMTPConfig).filter(SMTPConfig.id == config_id).update(
                {
                    SMTPConfig.last_attempt_at: now,
                    SMTPConfig.sync_state: "syncing",
                },
                synchronize_session=False,
            )

    @staticmethod
    def _mark_sync_success(config_id: int) -> None:
        now = datetime.now(tz=timezone.utc)
        with SessionLocal.begin() as db:
            account = db.query(SMTPConfig).filter(SMTPConfig.id == config_id).one()
            account.last_check = now
            account.last_success_at = now
            account.sync_state = (
                "healthy" if account.backfill_complete else "backfilling"
            )
            account.last_error_code = None
            account.last_error_message = None
            account.consecutive_failures = 0
            account.retry_at = None

    @staticmethod
    def _mark_sync_interrupted(config_id: int) -> None:
        with SessionLocal.begin() as db:
            account = db.query(SMTPConfig).filter(SMTPConfig.id == config_id).one()
            account.sync_state = (
                "healthy" if account.backfill_complete else "backfilling"
            )

    @classmethod
    def _mark_sync_failure(
        cls,
        config_id: int,
        exc: Exception,
        failures: int,
        retry_seconds: int,
    ) -> None:
        code, message = cls._sync_error(exc)
        now = datetime.now(tz=timezone.utc)
        with SessionLocal.begin() as db:
            db.query(SMTPConfig).filter(SMTPConfig.id == config_id).update(
                {
                    SMTPConfig.sync_state: "error",
                    SMTPConfig.last_error_code: code,
                    SMTPConfig.last_error_message: message,
                    SMTPConfig.consecutive_failures: failures,
                    SMTPConfig.retry_at: now
                    + timedelta(seconds=retry_seconds),
                },
                synchronize_session=False,
            )

    async def _process_gmail_api(self, config: SMTPConfig) -> None:
        """Use Gmail history checkpoints for OAuth-backed Gmail accounts."""
        client = GmailApiClient(config)
        try:
            with get_db_session() as db:
                account = db.query(SMTPConfig).filter(SMTPConfig.id == config.id).one()
                initial_sync_complete = account.initial_sync_complete
                history_id = account.provider_sync_token

            if not initial_sync_complete:
                await self._process_gmail_backfill(config, client)
                return

            if not history_id:
                self._reset_gmail_backfill(config.id)
                await self._process_gmail_backfill(config, client)
                return

            try:
                await self._process_gmail_history(config, client, history_id)
            except GmailHistoryExpired:
                logger.warning(
                    "Gmail history expired for account %s; starting a resumable full sync",
                    config.id,
                )
                self._reset_gmail_backfill(config.id)
                await self._process_gmail_backfill(config, client)
        finally:
            await client.close()

    async def _process_gmail_backfill(
        self, config: SMTPConfig, client: GmailApiClient
    ) -> None:
        """Process a bounded number of Gmail listing pages and persist progress."""
        from src.config import settings

        with get_db_session() as db:
            account = db.query(SMTPConfig).filter(SMTPConfig.id == config.id).one()
            needs_snapshot = not account.initial_sync_complete and not account.sync_page_token

        if needs_snapshot:
            profile = await client.get_profile()
            history_id = str(profile.get("historyId") or "")
            if not history_id:
                raise RuntimeError("Gmail profile did not include a history ID")
            with get_db_session() as db:
                account = db.query(SMTPConfig).filter(SMTPConfig.id == config.id).one()
                if not account.initial_sync_complete and not account.sync_page_token:
                    account.sync_generation = (account.sync_generation or 0) + 1
                    account.provider_sync_token = history_id
                    account.backfill_complete = False
                    account.backfill_processed = 0
                    account.backfill_total = int(profile.get("messagesTotal") or 0) or None

        for _ in range(settings.gmail_backfill_pages_per_cycle):
            with get_db_session() as db:
                account = db.query(SMTPConfig).filter(SMTPConfig.id == config.id).one()
                if account.initial_sync_complete:
                    return
                page_token = account.sync_page_token
                generation = account.sync_generation

            page = await client.list_messages(
                page_token=page_token,
                max_results=settings.gmail_page_size,
            )
            message_ids = [str(item["id"]) for item in page.get("messages", [])]
            processed_count, missing_ids = await self._fetch_and_process_gmail_messages(
                client,
                message_ids,
                sync_generation=generation,
            )
            if processed_count:
                await self._update_server_stats(config, processed_count)

            next_page_token = page.get("nextPageToken")
            if next_page_token:
                with get_db_session() as db:
                    account = db.query(SMTPConfig).filter(SMTPConfig.id == config.id).one()
                    account.sync_page_token = str(next_page_token)
                continue

            self._complete_gmail_backfill(config.id, generation)
            logger.info(
                "Completed Gmail full sync generation %s for account %s (%s vanished during fetch)",
                generation,
                config.id,
                len(missing_ids),
            )
            return

    async def _process_gmail_history(
        self,
        config: SMTPConfig,
        client: GmailApiClient,
        start_history_id: str,
    ) -> None:
        """Apply bounded Gmail history pages without advancing early."""
        from src.config import settings

        for _ in range(settings.gmail_history_pages_per_cycle):
            with get_db_session() as db:
                account = db.query(SMTPConfig).filter(SMTPConfig.id == config.id).one()
                page_token = account.sync_page_token

            page = await client.list_history(start_history_id, page_token=page_token)
            changed_ids, deleted_ids = self._gmail_history_changes(page)
            processed_count, missing_ids = await self._fetch_and_process_gmail_messages(
                client,
                sorted(changed_ids - deleted_ids),
            )
            self._apply_gmail_deletions(config.id, deleted_ids | missing_ids)
            if processed_count:
                await self._update_server_stats(config, processed_count)

            next_page_token = page.get("nextPageToken")
            with get_db_session() as db:
                account = db.query(SMTPConfig).filter(SMTPConfig.id == config.id).one()
                if next_page_token:
                    account.sync_page_token = str(next_page_token)
                else:
                    account.provider_sync_token = str(
                        page.get("historyId") or start_history_id
                    )
                    account.sync_page_token = None
            if not next_page_token:
                return

    async def _fetch_and_process_gmail_messages(
        self,
        client: GmailApiClient,
        message_ids: list[str],
        *,
        sync_generation: int | None = None,
    ) -> tuple[int, set[str]]:
        """Fetch and commit small RFC822 chunks to cap peak memory use."""
        from src.config import settings

        concurrency = max(1, settings.gmail_request_concurrency)
        processed_count = 0
        missing: set[str] = set()
        for offset in range(0, len(message_ids), concurrency):
            chunk_ids = message_ids[offset : offset + concurrency]
            fetched = await asyncio.gather(
                *(client.get_parsed_message(message_id) for message_id in chunk_ids)
            )
            messages = []
            for message_id, message in zip(chunk_ids, fetched, strict=True):
                if message is None:
                    missing.add(message_id)
                    continue
                if sync_generation is not None:
                    message["last_seen_sync_generation"] = sync_generation
                messages.append(message)
            await self._process_emails(messages)
            processed_count += len(messages)
        return processed_count, missing

    @staticmethod
    def _gmail_history_changes(page: dict) -> tuple[set[str], set[str]]:
        changed_ids: set[str] = set()
        deleted_ids: set[str] = set()
        for history in page.get("history", []):
            changed_ids.update(
                str(message["id"]) for message in history.get("messages", [])
            )
            for key in ("messagesAdded", "labelsAdded", "labelsRemoved"):
                changed_ids.update(
                    str(event["message"]["id"])
                    for event in history.get(key, [])
                )
            deleted_ids.update(
                str(event["message"]["id"])
                for event in history.get("messagesDeleted", [])
            )
        return changed_ids, deleted_ids

    @staticmethod
    def _reset_gmail_backfill(config_id: int) -> None:
        with get_db_session() as db:
            account = db.query(SMTPConfig).filter(SMTPConfig.id == config_id).one()
            account.initial_sync_complete = False
            account.backfill_complete = False
            account.backfill_processed = 0
            account.backfill_total = None
            account.provider_sync_token = None
            account.sync_page_token = None

    @staticmethod
    def _complete_gmail_backfill(config_id: int, generation: int) -> None:
        """Reconcile upstream removals only after every listing page succeeds."""
        from src.config import settings

        now = datetime.now(tz=timezone.utc)
        with get_db_session() as db:
            account = db.query(SMTPConfig).filter(SMTPConfig.id == config_id).one()
            stale_messages = (
                db.query(EmailLog)
                .filter(
                    EmailLog.smtp_config_id == config_id,
                    (
                        EmailLog.last_seen_sync_generation.is_(None)
                        | (EmailLog.last_seen_sync_generation != generation)
                    ),
                )
                .all()
            )
            for message in stale_messages:
                if settings.upstream_delete_policy == "hard_delete":
                    db.delete(message)
                elif settings.upstream_delete_policy == "tombstone":
                    message.deleted_at = now
                    # A tombstoned message has no location: the provider no longer
                    # has it. The IMAP path retires placements before tombstoning;
                    # this one has to do it explicitly or the two disagree.
                    db.query(MessagePlacement).filter(
                        MessagePlacement.email_log_id == message.id
                    ).delete(synchronize_session=False)
            account.initial_sync_complete = True
            account.backfill_complete = True
            account.sync_page_token = None

    @staticmethod
    def _apply_gmail_deletions(config_id: int, provider_message_ids: set[str]) -> None:
        if not provider_message_ids:
            return
        from src.config import settings

        now = datetime.now(tz=timezone.utc)
        with get_db_session() as db:
            messages = (
                db.query(EmailLog)
                .filter(
                    EmailLog.smtp_config_id == config_id,
                    EmailLog.provider_message_id.in_(provider_message_ids),
                )
                .all()
            )
            for message in messages:
                if settings.upstream_delete_policy == "hard_delete":
                    db.delete(message)
                elif settings.upstream_delete_policy == "tombstone":
                    message.deleted_at = now

    async def _process_emails(self, emails: List[dict]):
        """Process a batch of emails."""
        from src.email.attachment_handler import AttachmentHandler
        from src.storage_config.resolver import resolve_storage_config

        text_extractor = TextExtractor()
        attachment_handler = AttachmentHandler()

        for email_data in emails:
            try:
                with get_db_session() as db:
                    smtp_config = db.query(SMTPConfig).filter(SMTPConfig.id == email_data["smtp_config_id"]).first()
                    storage_config = resolve_storage_config(smtp_config)
                    # Gmail projects one location from labels; IMAP folders stack.
                    exclusive = (
                        smtp_config is not None
                        and smtp_config.provider == "gmail"
                        and smtp_config.auth_type == "oauth2"
                    )

                    # Check if email already exists (upsert pattern)
                    existing_email = (
                        db.query(EmailLog)
                        .filter(
                            EmailLog.smtp_config_id == email_data["smtp_config_id"],
                            EmailLog.provider_message_id == email_data["provider_message_id"],
                        )
                        .first()
                    )

                    if existing_email:
                        existing_email.provider_thread_id = sanitize_db_text(
                            email_data.get("provider_thread_id")
                        )
                        apply_flag_state(existing_email, email_data.get("flags"))
                        existing_email.provider_size = (
                            email_data.get("provider_size")
                            or existing_email.provider_size
                        )
                        existing_email.content_state = email_data.get(
                            "content_state",
                            existing_email.content_state,
                        )
                        existing_email.deleted_at = None
                        if email_data.get("last_seen_sync_generation") is not None:
                            existing_email.last_seen_sync_generation = email_data[
                                "last_seen_sync_generation"
                            ]
                        if not existing_email.content_fingerprint:
                            existing_email.content_fingerprint = content_fingerprint(
                                email_data.get("subject"),
                                email_data.get("body_plain"),
                            )
                        if not db.query(MailParticipant.id).filter(
                            MailParticipant.email_log_id == existing_email.id
                        ).first():
                            db.add_all(
                                participant_models(existing_email.id, email_data)
                            )
                        upsert_placement(
                            db,
                            existing_email.id,
                            email_data.get("folder"),
                            email_data.get("imap_uid"),
                            email_data.get("uid_validity"),
                            exclusive=exclusive,
                        )
                        logger.debug("Email already exists: %s", email_data["message_id"])
                        continue

                    # Upgrade a pre-cursor row in place on first replay after migration.
                    legacy_email = (
                        db.query(EmailLog)
                        .filter(
                            EmailLog.smtp_config_id == email_data["smtp_config_id"],
                            EmailLog.message_id == email_data["message_id"],
                            EmailLog.provider_message_id == EmailLog.message_id,
                        )
                        .first()
                    )
                    if legacy_email:
                        legacy_email.provider_message_id = email_data["provider_message_id"]
                        legacy_email.folder = email_data.get("folder")
                        legacy_email.imap_uid = email_data.get("imap_uid")
                        legacy_email.uid_validity = email_data.get("uid_validity")
                        apply_flag_state(legacy_email, email_data.get("flags"))
                        legacy_email.provider_size = email_data.get("provider_size")
                        legacy_email.content_state = email_data.get(
                            "content_state",
                            "complete",
                        )
                        legacy_email.last_seen_sync_generation = email_data.get(
                            "last_seen_sync_generation"
                        )
                        legacy_email.content_fingerprint = content_fingerprint(
                            email_data.get("subject"),
                            email_data.get("body_plain"),
                        )
                        if not db.query(MailParticipant.id).filter(
                            MailParticipant.email_log_id == legacy_email.id
                        ).first():
                            db.add_all(
                                participant_models(legacy_email.id, email_data)
                            )
                        upsert_placement(
                            db,
                            legacy_email.id,
                            email_data.get("folder"),
                            email_data.get("imap_uid"),
                            email_data.get("uid_validity"),
                            exclusive=exclusive,
                        )
                        logger.debug("Upgraded legacy provider reference for %s", email_data["message_id"])
                        continue

                    # Get body content, converting HTML to plain text if needed
                    body_plain = sanitize_db_text(email_data.get("body_plain", ""))
                    body_html = sanitize_db_text(email_data.get("body_html", ""))
                    if not body_plain and body_html:
                        body_plain = text_extractor._extract_html(body_html.encode("utf-8", errors="replace")) or ""

                    email_log = EmailLog(
                        smtp_config_id=email_data["smtp_config_id"],
                        sender=sanitize_db_text(email_data["sender"]),
                        recipient=sanitize_db_text(email_data["recipient"]),
                        subject=sanitize_db_text(email_data["subject"]),
                        message_id=sanitize_db_text(email_data["message_id"])[:255],
                        provider_message_id=sanitize_db_text(email_data["provider_message_id"])[:768],
                        provider_thread_id=sanitize_db_text(email_data.get("provider_thread_id"))[:768]
                        if email_data.get("provider_thread_id")
                        else None,
                        folder=sanitize_db_text(email_data.get("folder")),
                        imap_uid=email_data.get("imap_uid"),
                        uid_validity=email_data.get("uid_validity"),
                        to_addresses=sanitize_db_text(email_data.get("to_addresses")),
                        cc_addresses=sanitize_db_text(email_data.get("cc_addresses")),
                        bcc_addresses=sanitize_db_text(email_data.get("bcc_addresses")),
                        in_reply_to=sanitize_db_text(email_data.get("in_reply_to")),
                        references=sanitize_db_text(email_data.get("references")),
                        content_fingerprint=content_fingerprint(
                            email_data.get("subject"),
                            body_plain,
                        ),
                        provider_size=email_data.get("provider_size"),
                        content_state=email_data.get("content_state", "complete"),
                        last_seen_sync_generation=email_data.get(
                            "last_seen_sync_generation"
                        ),
                        email_date=email_data["email_date"],
                        attachment_count=email_data["attachment_count"],
                        body_plain=body_plain,
                        body_html=body_html,
                    )
                    apply_flag_state(email_log, email_data.get("flags"))

                    db.add(email_log)
                    db.flush()
                    upsert_placement(
                        db,
                        email_log.id,
                        email_data.get("folder"),
                        email_data.get("imap_uid"),
                        email_data.get("uid_validity"),
                        exclusive=exclusive,
                    )
                    db.add_all(participant_models(email_log.id, email_data))

                    if email_data["attachment_count"] > 0 and "raw_email" in email_data:
                        attachments = await attachment_handler.extract_attachments(
                            email_data["raw_email"], email_log.id, storage_config
                        )
                        for attachment in attachments:
                            db.add(attachment)

                        email_log.attachment_count = len(attachments)

                logger.info(
                    "Processed provider message for account %s (%s attachments)",
                    email_data["smtp_config_id"],
                    email_data["attachment_count"],
                )

            except Exception as e:
                logger.error("Error processing email %s: %s", email_data.get("message_id", "unknown"), e)
                raise

    async def _update_server_stats(self, config: SMTPConfig, email_count: int):
        """Update server statistics."""
        try:
            config_id = config.id  # Store ID to avoid detachment issues
            with get_db_session() as db:
                db_config = db.query(SMTPConfig).filter(SMTPConfig.id == config_id).first()
                if db_config:
                    db_config.total_emails_processed += email_count
                    if not db_config.backfill_complete:
                        db_config.backfill_processed += email_count
        except Exception as e:
            logger.error("Error updating server stats: %s", e)

    def _persist_sync_cursors(self, emails: List[dict]) -> None:
        """Advance a folder cursor only after its message batch commits."""
        from src.models.sync_cursor import MailSyncCursor

        by_folder = {}
        for email in emails:
            folder = email.get("folder")
            uid = email.get("imap_uid")
            if folder and uid is not None:
                state = by_folder.setdefault(
                    (email["smtp_config_id"], folder),
                    {"last_uid": uid, "uid_validity": email.get("uid_validity")},
                )
                state["last_uid"] = max(state["last_uid"], uid)

        with get_db_session() as db:
            for (account_id, folder), state in by_folder.items():
                cursor = (
                    db.query(MailSyncCursor)
                    .filter(
                        MailSyncCursor.smtp_config_id == account_id,
                        MailSyncCursor.folder == folder,
                    )
                    .first()
                )
                if not cursor:
                    cursor = MailSyncCursor(smtp_config_id=account_id, folder=folder)
                    db.add(cursor)
                validity_changed = (
                    cursor.uid_validity is not None
                    and state["uid_validity"] is not None
                    and cursor.uid_validity != state["uid_validity"]
                )
                cursor.uid_validity = state["uid_validity"]
                cursor.last_uid = (
                    state["last_uid"]
                    if validity_changed
                    else max(cursor.last_uid or 0, state["last_uid"])
                )
                if validity_changed:
                    cursor.backfill_complete = False
                cursor.last_success_at = datetime.now(tz=timezone.utc)
                cursor.last_error = None

    def _persist_client_cursors(self, account_id: int, client: SMTPClient) -> None:
        """Persist idle-folder checkpoints as well as checkpoints from message batches."""
        from src.models.sync_cursor import MailSyncCursor

        with get_db_session() as db:
            for folder, last_uid in client._last_uids.items():
                cursor = (
                    db.query(MailSyncCursor)
                    .filter(
                        MailSyncCursor.smtp_config_id == account_id,
                        MailSyncCursor.folder == folder,
                    )
                    .first()
                )
                if not cursor:
                    cursor = MailSyncCursor(smtp_config_id=account_id, folder=folder)
                    db.add(cursor)
                uid_validity = client._uid_validities.get(folder)
                validity_changed = (
                    cursor.uid_validity is not None
                    and uid_validity is not None
                    and cursor.uid_validity != uid_validity
                )
                cursor.uid_validity = uid_validity
                cursor.last_uid = (
                    last_uid
                    if validity_changed
                    else max(cursor.last_uid or 0, last_uid)
                )
                cursor.backfill_complete = client._folder_backfill_complete.get(
                    folder,
                    False,
                )
                cursor.last_success_at = datetime.now(tz=timezone.utc)
                cursor.last_error = None

    async def _reconcile_provider_state(self, config_id: int, client: SMTPClient) -> None:
        """Periodically mirror upstream deletions and flags without downloading bodies."""
        from src.config import settings

        now = datetime.now(tz=timezone.utc)
        with get_db_session() as db:
            account = db.query(SMTPConfig).filter(SMTPConfig.id == config_id).one()
            last = account.last_reconciled_at
        if last and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if last and (now - last).total_seconds() < settings.deletion_reconcile_interval:
            return
        snapshots = await client.fetch_folder_state()
        with get_db_session() as db:
            apply_folder_snapshots(db, config_id=config_id, snapshots=snapshots)
            reap_tombstoned_messages(db, config_id=config_id, now=now)
            account = db.query(SMTPConfig).filter(SMTPConfig.id == config_id).one()
            account.last_reconciled_at = now

    async def process_server_now(self, server_id: int, owner_user_id: int | None = None) -> dict:
        """Manually trigger processing for a specific server."""
        try:
            with get_db_session() as db:
                query = db.query(SMTPConfig).filter(SMTPConfig.id == server_id)
                if owner_user_id is not None:
                    query = query.filter(SMTPConfig.owner_user_id == owner_user_id)
                config = query.first()
                if not config:
                    return {"error": "Server not found"}

                if not config.enabled:
                    return {"error": "Server is disabled"}
                if not config.credential_ciphertext:
                    return {"error": "Mailbox password is not configured"}

            from src.config import settings
            from src.sync_health import fresh, heartbeat, read_record, runtime_dir, write_record

            root = runtime_dir()
            supervisor = read_record(root / "supervisor.json")
            if not fresh(supervisor, settings.sync_watchdog_timeout_seconds):
                return {"error": "Synchronization supervisor is unavailable"}
            # One bounded, coalescing request per account. The supervisor owns
            # concurrency and deadlines for manual and automatic work alike.
            write_record(root / f"request-{server_id}.json", heartbeat(account_id=server_id))
            return {"success": True, "queued": True, "message": "Synchronization queued"}

        except Exception as e:
            logger.error("Manual processing failed for server %s: %s", server_id, e)
            return {"error": str(e)}
