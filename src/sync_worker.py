"""One account per interpreter, with its own DB pool and extraction descendants."""

import asyncio
import logging
import signal
import sys
import time

from src.config import settings
from src.database.connection import get_db_session
from src.database.sync_fence import LeaseLost, lease_scope
from src.email.email_processor import EmailProcessor, acquire_mailbox_lease, release_mailbox_lease
from src.models.smtp_config import SMTPConfig

logger = logging.getLogger(__name__)


async def run_account(account_id: int, token: str, deadline: float) -> int:
    if time.monotonic() >= deadline:
        return 75
    with get_db_session() as db:
        account = db.get(SMTPConfig, account_id)
        if account is None or not account.enabled or not account.credential_ciphertext:
            return 75
        # Reserve through the cleanup grace. Writes themselves are fenced at the
        # work deadline. No other owner may start while this job is being killed.
        acquired = acquire_mailbox_lease(
            db, account, token=token,
            seconds=deadline + settings.sync_job_cleanup_seconds - time.monotonic(),
        )
        if acquired is None:
            return 75
        config = SMTPConfig.create_detached(account)

    processor = EmailProcessor()
    processor.processing = True
    processor._active_lock_tokens[account_id] = token
    result = 1
    try:
        with lease_scope(account_id, token, deadline):
            result = 0 if await processor._process_server(config, force=True, leased=True) else 1
    except LeaseLost:
        logger.warning("Account %s lost its synchronization fence", account_id)
    finally:
        # Even asyncio.run's shutdown can get stuck on a cancellation-resistant
        # protocol task. The external supervisor kills this entire process group.
        await processor.stop_processing()
        with get_db_session() as db:
            release_mailbox_lease(db, token)
    return result


async def _run(account_id, token, deadline):
    task = asyncio.current_task()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    return await run_account(account_id, token, deadline)


def main():
    logging.basicConfig(level=settings.log_level)
    # All arguments are non-secret: account ID, random fence token, monotonic deadline.
    account_id, token, deadline = sys.argv[1:]
    try:
        code = asyncio.run(_run(int(account_id), token, float(deadline)))
    except asyncio.CancelledError:
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
