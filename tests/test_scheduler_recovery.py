"""Scheduler recovery failures must remain local to a single account."""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src import sync_scheduler as scheduler
from src.database import connection
from src.models.base import Base
from src.models.smtp_config import SMTPConfig
from src.models.user import User
from src.sync_health import read_record, write_record


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'recovery.db'}")
    factory = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    with factory.begin() as db:
        db.add(User(id=1, google_sub="owner", email="owner@example.invalid"))
        for aid in (1, 2):
            db.add(SMTPConfig(id=aid, owner_user_id=1, name=f"Account {aid}",
                              host="imap.invalid", port=993, username=f"user{aid}",
                              credential_ciphertext="synthetic-secret", enabled=True,
                              sync_lock_token=f"attempt-{aid}", sync_state="syncing"))
    monkeypatch.setattr(connection, "SessionLocal", factory)
    monkeypatch.setattr(scheduler, "runtime_dir", lambda: tmp_path)
    # Ensure the poison file precedes the healthy result on every filesystem.
    original_glob = Path.glob
    monkeypatch.setattr(Path, "glob", lambda self, pattern: iter(sorted(original_glob(self, pattern))))
    yield tmp_path, factory
    engine.dispose()


async def one_cycle(monkeypatch):
    async def stop(_):
        raise asyncio.CancelledError
    monkeypatch.setattr(scheduler.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await scheduler.run()


def result(aid):
    return {"account_id": aid, "token": f"attempt-{aid}", "exit_code": -9, "reason": "deadline"}


def assert_discovered(root):
    record = read_record(root / "scheduler.json")
    assert {account["id"] for account in record.get("accounts", [])} == {1, 2}
    assert record["heartbeat"] > 0


@pytest.mark.asyncio
async def test_poison_result_does_not_block_discovery_or_other_results(recovery, monkeypatch):
    root, factory = recovery
    write_record(root / "result-1.json", {"exit_code": -9, "token": "attempt-1"})
    write_record(root / "result-2.json", result(2))
    await one_cycle(monkeypatch)
    assert_discovered(root)
    assert not (root / "result-2.json").exists()
    with factory() as db:
        assert db.get(SMTPConfig, 2).consecutive_failures == 1
        assert db.get(SMTPConfig, 1).sync_lock_token == "attempt-1"


@pytest.mark.asyncio
async def test_account_db_failure_retries_without_blocking_or_logging_secrets(recovery, monkeypatch, caplog):
    from sqlalchemy import event

    root, factory = recovery
    for aid in (1, 2):
        write_record(root / f"result-{aid}.json", result(aid))
    failed = False

    def fail_one_commit(db):
        nonlocal failed
        if not failed and any(isinstance(row, SMTPConfig) and row.id == 1 for row in db.dirty):
            failed = True
            raise RuntimeError("synthetic-secret password=do-not-log token=attempt-1")

    event.listen(factory, "before_commit", fail_one_commit)
    await one_cycle(monkeypatch)
    assert failed
    assert_discovered(root)
    assert read_record(root / "result-1.json") == result(1)
    assert not (root / "result-2.json").exists()
    with factory() as db:
        assert db.get(SMTPConfig, 1).sync_lock_token == "attempt-1"
        assert db.get(SMTPConfig, 1).consecutive_failures == 0
        assert db.get(SMTPConfig, 2).consecutive_failures == 1
    await one_cycle(monkeypatch)
    assert_discovered(root)
    assert not (root / "result-1.json").exists()
    with factory() as db:
        assert db.get(SMTPConfig, 1).sync_lock_token is None
        assert db.get(SMTPConfig, 1).consecutive_failures == 1
    assert "synthetic-secret" not in caplog.text
    assert "do-not-log" not in caplog.text
    assert "attempt-1" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    {"token": None}, {"token": ""}, {"token": ["synthetic-secret"]},
    {"account_id": True}, {"account_id": "1"}, {"account_id": 2},
    {"exit_code": "-9"}, {"exit_code": None}, {"reason": None},
])
async def test_malformed_result_is_discarded_without_mutating_accounts(recovery, monkeypatch, invalid):
    root, factory = recovery
    if invalid.get("token", "present") is None:
        with factory.begin() as db:
            db.get(SMTPConfig, 1).sync_lock_token = None
    write_record(root / "result-1.json", result(1) | invalid)
    write_record(root / "result-2.json", result(2))
    await one_cycle(monkeypatch)
    assert_discovered(root)
    assert not (root / "result-1.json").exists()
    with factory() as db:
        assert db.get(SMTPConfig, 1).consecutive_failures == 0
        assert db.get(SMTPConfig, 1).sync_state == "syncing"
        assert db.get(SMTPConfig, 1).sync_lock_token == (None if invalid.get("token", "present") is None else "attempt-1")
        assert db.get(SMTPConfig, 2).consecutive_failures == 1


@pytest.mark.asyncio
async def test_transient_read_error_preserves_unprocessed_result(recovery, monkeypatch, caplog):
    root, factory = recovery
    path = root / "result-1.json"
    write_record(path, result(1))
    write_record(root / "result-2.json", result(2))
    original_open = Path.open

    def fail_read(self, *args, **kwargs):
        if self == path:
            raise PermissionError("synthetic-secret")
        return original_open(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", fail_read)
        await one_cycle(patch)
    assert path.exists()
    assert_discovered(root)
    assert not (root / "result-2.json").exists()
    await one_cycle(monkeypatch)
    assert not path.exists()
    with factory() as db:
        assert db.get(SMTPConfig, 1).consecutive_failures == 1
    assert "synthetic-secret" not in caplog.text


@pytest.mark.asyncio
async def test_discovery_failure_keeps_old_heartbeat_without_logging_secrets(recovery, monkeypatch, caplog):
    root, _ = recovery
    write_record(root / "scheduler.json", {"heartbeat": 1})

    def fail():
        raise RuntimeError("synthetic-secret")

    monkeypatch.setattr(scheduler, "discover_accounts", fail)
    await one_cycle(monkeypatch)
    assert read_record(root / "scheduler.json") == {"heartbeat": 1}
    assert "synthetic-secret" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("successor", [False, True])
async def test_unlink_failure_replay_is_idempotent_and_fenced(recovery, monkeypatch, successor):
    root, factory = recovery
    path = root / "result-1.json"
    write_record(path, result(1))
    write_record(root / "result-2.json", result(2))
    original_unlink = Path.unlink

    def fail_unlink(self, *args, **kwargs):
        if self == path:
            raise OSError("cannot acknowledge yet")
        return original_unlink(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_unlink)
        await one_cycle(patch)
    assert path.exists()
    assert not (root / "result-2.json").exists()
    assert_discovered(root)
    with factory.begin() as db:
        account = db.get(SMTPConfig, 1)
        assert account.consecutive_failures == 1
        if successor:
            account.sync_lock_token = "successor"
            account.sync_state = "syncing"
    await one_cycle(monkeypatch)
    assert not path.exists()
    with factory() as db:
        account = db.get(SMTPConfig, 1)
        assert account.consecutive_failures == 1
        assert account.sync_lock_token == ("successor" if successor else None)
        assert account.sync_state == ("syncing" if successor else "error")


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [b'{"secret":', b'[]', b'null', b'\xff', b'[' * 2000])
async def test_invalid_json_does_not_block_other_accounts(recovery, monkeypatch, caplog, raw):
    root, _ = recovery
    (root / "result-1.json").write_bytes(raw)
    write_record(root / "result-2.json", result(2))
    await one_cycle(monkeypatch)
    assert_discovered(root)
    assert not (root / "result-1.json").exists()
    assert not (root / "result-2.json").exists()
    assert "secret" not in caplog.text
