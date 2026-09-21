"""DB discovery/recovery in a replaceable process, never in the supervisor.

A heartbeat is published only AFTER a complete DB scan and recovery sweep.
Individual failed results remain pending without suppressing discovery. A
blocked DB driver still stops the heartbeat and is externally replaced.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from src.config import settings
from src.database import connection
from src.models.smtp_config import SMTPConfig
from src.sync_health import MAX_RECORD_BYTES, heartbeat, runtime_dir, write_record

logger = logging.getLogger(__name__)


class InvalidResult(ValueError):
    """Permanently malformed completion, never eligible for DB recovery."""


def _read_result(path) -> dict:
    # Unlike health reads, transient I/O failures must not look like empty
    # records: acknowledging them would lose a valid unprocessed completion.
    with path.open("rb") as stream:
        data = stream.read(MAX_RECORD_BYTES + 1)
    if len(data) > MAX_RECORD_BYTES:
        raise InvalidResult
    try:
        return json.loads(data)
    except (ValueError, RecursionError) as exc:
        raise InvalidResult from exc


def recover_result(result: dict) -> None:
    """Only the supervisor publishes results, after waitpid and group cleanup."""
    if (
        not isinstance(result, dict)
        or type(result.get("account_id")) is not int
        or result["account_id"] <= 0
        or not isinstance(result.get("token"), str)
        or not result["token"]
        or type(result.get("exit_code")) is not int
        or result.get("reason") not in ("deadline", "failed", "success")
    ):
        raise InvalidResult
    if not result["exit_code"]:
        return
    # Own the transaction here: the generic DB helper logs raw exceptions,
    # potentially including SQL parameters. Scheduler logs stay payload-free.
    with connection.SessionLocal.begin() as db:
        rows = db.query(SMTPConfig).filter(
            SMTPConfig.sync_lock_token == result["token"],
        ).order_by(SMTPConfig.id).with_for_update().all()
        now = datetime.now(timezone.utc)
        for account in rows:
            # Other owners' duplicate mailbox rows share the lease, not state.
            if account.id == result["account_id"]:
                failures = (account.consecutive_failures or 0) + 1
                account.sync_state = "error"
                account.last_error_code = (
                    "SYNC_JOB_DEADLINE" if result["reason"] == "deadline" else "SYNC_WORKER_EXITED"
                )
                account.last_error_message = "Synchronization worker exited; committed checkpoints retained"
                account.consecutive_failures = failures
                account.retry_at = now + timedelta(seconds=min(3600, 30 * 2 ** min(failures - 1, 7)))
            account.sync_lock_token = None
            account.sync_locked_at = None
            account.sync_lock_expires_at = None


def _timestamp(value):
    if value is None:
        return None
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).timestamp()


def discover_accounts() -> list[dict]:
    with connection.SessionLocal.begin() as db:
        rows = db.query(
            SMTPConfig.id, SMTPConfig.retry_at, SMTPConfig.last_attempt_at,
            SMTPConfig.last_success_at, SMTPConfig.sync_state, SMTPConfig.sync_lock_expires_at,
        ).filter(
            SMTPConfig.enabled,
            SMTPConfig.credential_ciphertext.isnot(None),
            SMTPConfig.credential_ciphertext != "",
        ).order_by(SMTPConfig.id).limit(settings.sync_max_accounts + 1).all()
        if len(rows) > settings.sync_max_accounts:
            raise RuntimeError("Enabled accounts exceed scheduler record limit")
        return [
            {"id": row.id, "retry_at": _timestamp(row.retry_at),
             "last_attempt_at": _timestamp(row.last_attempt_at),
             "last_success_at": _timestamp(row.last_success_at),
             "state": row.sync_state, "lease_expires_at": _timestamp(row.sync_lock_expires_at)}
            for row in rows
        ]


async def run():
    root = runtime_dir()
    while True:
        try:
            # One result file per account; idempotent matching-token recovery.
            for path in root.glob("result-*.json"):
                try:
                    try:
                        result = _read_result(path)
                        if not isinstance(result, dict) or path.name != f"result-{result.get('account_id')}.json":
                            raise InvalidResult
                        recover_result(result)
                    except InvalidResult:
                        logger.warning("Malformed synchronization result discarded")
                    path.unlink(missing_ok=True)
                except Exception:
                    # Keep unacknowledged results for retry; never log payloads
                    # or DB exceptions (which can contain credentials).
                    logger.warning("Synchronization result recovery deferred")
            accounts = discover_accounts()
            write_record(root / "scheduler.json", heartbeat(accounts=accounts))
        except Exception:
            # Do not update the successful-pass timestamp on errors.
            logger.error("Synchronization discovery/recovery pass failed")
        await asyncio.sleep(settings.sync_scheduler_interval_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=settings.log_level)
    asyncio.run(run())
