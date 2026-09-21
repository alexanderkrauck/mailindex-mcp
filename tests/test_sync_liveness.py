"""Finite lease and transaction fencing regressions."""

import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.email import email_processor
from src.models.base import Base
from src.models.smtp_config import SMTPConfig
from src.models.sync_cursor import MailSyncCursor
from src.models.user import User


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    from src.database import sync_fence

    engine = create_engine(f"sqlite:///{tmp_path / 'fence.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=sync_fence.SyncSession, autoflush=False)
    with factory.begin() as db:
        db.add(User(id=1, google_sub="owner", email="owner@example.invalid"))
        db.add(SMTPConfig(id=1, owner_user_id=1, name="Account", host="imap.invalid",
                          port=993, username="owner", credential_ciphertext="test"))
    monkeypatch.setattr(email_processor, "SessionLocal", factory)
    from src.database import connection
    monkeypatch.setattr(connection, "SessionLocal", factory)
    yield factory
    engine.dispose()


def test_expired_lease_cannot_be_resurrected(sessions):
    with sessions.begin() as db:
        account = db.get(SMTPConfig, 1)
        account.sync_lock_token = "old"
        account.sync_locked_at = datetime.now(timezone.utc) - timedelta(seconds=400)
        account.sync_lock_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    processor = email_processor.EmailProcessor()
    processor._active_lock_tokens[1] = "old"
    assert not processor._refresh_sync_lease(1)


def test_stale_token_rejects_message_cursor_and_state_in_one_transaction(sessions):
    from src.database.connection import get_db_session
    from src.database.sync_fence import LeaseLost, lease_scope
    from src.models.email import EmailLog

    with sessions.begin() as db:
        account = db.get(SMTPConfig, 1)
        account.sync_lock_token = "new"
        account.sync_lock_expires_at = datetime.now(timezone.utc) + timedelta(seconds=60)
    with pytest.raises(LeaseLost), lease_scope(1, "old", time.monotonic() + 60), get_db_session() as db:
        db.add(EmailLog(smtp_config_id=1, sender="a", recipient="b", subject="stale",
                        message_id="x", provider_message_id="x", email_date=datetime.now(timezone.utc)))
        db.add(MailSyncCursor(smtp_config_id=1, folder="INBOX", last_uid=99))
        db.query(SMTPConfig).filter(SMTPConfig.id == 1).update({SMTPConfig.sync_state: "healthy"})
    with sessions() as db:
        assert db.query(EmailLog).count() == 0
        assert db.query(MailSyncCursor).count() == 0
        assert db.get(SMTPConfig, 1).sync_state == "pending"


def test_lease_expiring_mid_transaction_rolls_back_commit(sessions):
    from src.database.connection import get_db_session
    from src.database.sync_fence import LeaseLost, lease_scope

    with sessions.begin() as db:
        account = db.get(SMTPConfig, 1)
        account.sync_lock_token = "live"
        account.sync_lock_expires_at = datetime.now(timezone.utc) + timedelta(seconds=60)
    with pytest.raises(LeaseLost), lease_scope(1, "live", time.monotonic() + .03), get_db_session() as db:
        db.get(SMTPConfig, 1).sync_page_token = "must-not-commit"
        time.sleep(.05)
    with sessions() as db:
        assert db.get(SMTPConfig, 1).sync_page_token is None


def test_renewal_is_capped_by_fixed_attempt_deadline(sessions, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "sync_job_timeout_seconds", 300)
    monkeypatch.setattr(settings, "sync_job_cleanup_seconds", 5)
    started = datetime.now(timezone.utc) - timedelta(seconds=299)
    with sessions.begin() as db:
        account = db.get(SMTPConfig, 1)
        account.sync_lock_token = "live"
        account.sync_locked_at = started
        account.sync_lock_expires_at = datetime.now(timezone.utc) + timedelta(seconds=2)
    assert email_processor.refresh_mailbox_lease("live", seconds=120)
    with sessions() as db:
        expires = db.get(SMTPConfig, 1).sync_lock_expires_at.replace(tzinfo=timezone.utc)
        assert expires <= started + timedelta(seconds=305)


def test_successor_replays_committed_cursor_without_reset(sessions):
    from src.database.connection import get_db_session
    from src.database.sync_fence import lease_scope

    with sessions.begin() as db:
        account = db.get(SMTPConfig, 1)
        account.sync_lock_token = "first"
        account.sync_lock_expires_at = datetime.now(timezone.utc) + timedelta(seconds=60)
    with lease_scope(1, "first", time.monotonic() + 60), get_db_session() as db:
        db.add(MailSyncCursor(smtp_config_id=1, folder="INBOX", last_uid=7, uid_validity=1))
    with sessions.begin() as db:
        account = db.get(SMTPConfig, 1)
        account.sync_lock_token = "second"
    with lease_scope(1, "second", time.monotonic() + 60):
        email_processor.EmailProcessor()._persist_sync_cursors([
            {"smtp_config_id": 1, "folder": "INBOX", "imap_uid": 5, "uid_validity": 1},
        ])
    with sessions() as db:
        assert db.query(MailSyncCursor).one().last_uid == 7


@pytest.mark.asyncio
async def test_api_reachable_but_stale_worker_is_not_healthy(tmp_path, monkeypatch):
    import json

    from src import server
    from src.config import settings
    from src.sync_health import heartbeat, runtime_dir, write_record

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "sync_external_supervisor", True)
    monkeypatch.setattr(server, "DATABASE_READY", True)
    monkeypatch.setattr(server, "DATABASE_CHECKED_AT", time.monotonic())
    monkeypatch.setenv("EMAILSERVER_PROCESS_IDENTITY", "test-api")
    record = heartbeat(scheduler_healthy=False, progress_healthy=False)
    write_record(runtime_dir() / "supervisor.json", record)
    response = await server.health_check()
    assert response.status_code == 503
    body = json.loads(response.body)
    assert body["api_ready"] is True
    assert body["sync_healthy"] is False
    assert "accounts" not in body


@pytest.mark.asyncio
async def test_manual_sync_queues_instead_of_bypassing_process_boundary(sessions, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from src.config import settings
    from src.sync_health import heartbeat, read_record, runtime_dir, write_record

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setenv("EMAILSERVER_PROCESS_IDENTITY", "supervisor-test")
    write_record(runtime_dir() / "supervisor.json", heartbeat(scheduler_healthy=True, progress_healthy=True))
    processor = email_processor.EmailProcessor()
    unsafe = AsyncMock(side_effect=AssertionError("must not enter provider code in API process"))
    monkeypatch.setattr(processor, "_process_server", unsafe)
    result = await processor.process_server_now(1, owner_user_id=1)
    assert result.get("queued") is True
    assert read_record(runtime_dir() / "request-1.json")["account_id"] == 1
    unsafe.assert_not_called()


@pytest.mark.asyncio
async def test_real_worker_uses_fenced_lease_and_releases_after_success(sessions, monkeypatch):

    from src import sync_worker

    with sessions.begin() as db:
        account = db.get(SMTPConfig, 1)
        account.auth_type = "oauth2"  # synthetic fixture, no plaintext credentials
    seen = []

    class Client:
        def __init__(self, config):
            self.backfill_complete = False
            self._last_uids = {"INBOX": 12}
            self._uid_validities = {"INBOX": 1}
            self._folder_backfill_complete = {"INBOX": False}
        async def fetch_new_emails(self, **kwargs):
            with sessions() as db:
                account = db.get(SMTPConfig, 1)
                seen.append(account.sync_lock_token)
            if False:
                yield []
        async def disconnect(self):
            pass

    monkeypatch.setattr(email_processor, "SMTPClient", Client)
    assert await sync_worker.run_account(1, "parent-token", time.monotonic() + 2) == 0
    with sessions() as db:
        account = db.get(SMTPConfig, 1)
        assert account.sync_lock_token is None
        assert account.last_success_at is not None
        assert db.query(MailSyncCursor).one().last_uid == 12
    assert seen == ["parent-token"]


def test_recovery_only_releases_reaped_matching_attempt(sessions):
    from src.sync_scheduler import recover_result

    with sessions.begin() as db:
        account = db.get(SMTPConfig, 1)
        account.sync_lock_token = "old"
        account.sync_lock_expires_at = datetime.now(timezone.utc) + timedelta(seconds=300)
        account.sync_state = "syncing"
        db.add(MailSyncCursor(smtp_config_id=1, folder="INBOX", last_uid=7))
    recover_result({"account_id": 1, "token": "old", "exit_code": -9, "reason": "deadline"})
    with sessions() as db:
        account = db.get(SMTPConfig, 1)
        assert account.sync_lock_token is None
        assert account.last_error_code == "SYNC_JOB_DEADLINE"
        assert account.retry_at is not None
        assert db.query(MailSyncCursor).one().last_uid == 7
    # Replaying a delayed completion cannot modify its successor.
    with sessions.begin() as db:
        account = db.get(SMTPConfig, 1)
        account.sync_lock_token = "new"
        account.sync_state = "syncing"
    recover_result({"account_id": 1, "token": "old", "exit_code": -9, "reason": "deadline"})
    with sessions() as db:
        assert db.get(SMTPConfig, 1).sync_state == "syncing"
        assert db.get(SMTPConfig, 1).sync_lock_token == "new"


@pytest.mark.asyncio
async def test_supervised_api_lifespan_does_not_launch_legacy_poller(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from src import server
    from src.config import settings

    monkeypatch.setattr(settings, "sync_external_supervisor", True)
    monkeypatch.setattr(server, "init_database", lambda: None)
    unsafe = AsyncMock()
    monkeypatch.setattr(server.email_processor, "start_processing", unsafe)
    app = SimpleNamespace(state=SimpleNamespace(processing_task=None))
    await server._prepare_database(app)
    unsafe.assert_not_called()
    assert app.state.processing_task is None


def test_default_main_starts_external_supervisor(monkeypatch):
    import sys
    from unittest.mock import Mock

    from src import main, sync_supervisor

    run = Mock()
    monkeypatch.setattr(sync_supervisor, "run_supervised", run)
    monkeypatch.setattr(main, "run_server", Mock(side_effect=AssertionError("API must be a child")))
    monkeypatch.setattr(sys, "argv", ["src.main"])
    main.main()
    run.assert_called_once_with()
