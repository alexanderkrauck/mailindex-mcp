"""Transaction fencing only for sessions created inside an account job scope.

The conditional UPDATE acquires the mailbox row lock BEFORE any message/cursor
SQL, holding it through commit. A successor cannot take the lease between a
check and a write. The second check rejects expiry during long extraction work.
No API session or unrelated ORM class has global hooks installed.
"""

import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

from sqlalchemy import event, update
from sqlalchemy.orm import Session

_fence = ContextVar("mail_sync_fence", default=None)


class LeaseLost(RuntimeError):
    """This attempt is no longer permitted to commit mail state."""


@contextmanager
def lease_scope(account_id: int, token: str, deadline: float):
    handle = _fence.set((account_id, token, deadline))
    try:
        yield
    finally:
        _fence.reset(handle)


class SyncSession(Session):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sync_fence = _fence.get()


def _check(session, connection):
    if session.sync_fence is None:
        return
    from src.models.smtp_config import SMTPConfig

    account_id, token, deadline = session.sync_fence
    if time.monotonic() >= deadline:
        raise LeaseLost("Synchronization attempt deadline expired")
    changed = connection.execute(
        update(SMTPConfig.__table__).where(
            SMTPConfig.id == account_id,
            SMTPConfig.sync_lock_token == token,
            SMTPConfig.sync_lock_expires_at > datetime.now(timezone.utc),
        ).values(sync_lock_token=token)
    ).rowcount
    if changed != 1:
        raise LeaseLost("Synchronization lease expired or was replaced")


@event.listens_for(SyncSession, "after_begin")
def _lock_lease(session, transaction, connection):
    _check(session, connection)


@event.listens_for(SyncSession, "before_commit")
def _validate_commit(session):
    if session.sync_fence is not None:
        _check(session, session.connection())
