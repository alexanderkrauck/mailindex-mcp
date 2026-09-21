"""Real process boundaries, not asyncio timeout mocks."""

import contextlib
import os
import signal
import sys
import time

import pytest

from src import sync_supervisor


def pump(pool, accounts, until, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        pool.tick(accounts)
        if until():
            return
        time.sleep(0.01)
    pytest.fail("supervision did not make progress within the test budget")


def test_resistant_job_cannot_hold_healthy_accounts_at_a_barrier(tmp_path):
    resistant = """
import asyncio, signal
signal.signal(signal.SIGTERM, signal.SIG_IGN)
async def stuck():
    while True:
        try: await asyncio.sleep(100)
        except asyncio.CancelledError: pass
asyncio.run(stuck())
"""
    def command(account_id, token, deadline):
        return [sys.executable, "-c", resistant if account_id == 1 else "pass"]

    pool = sync_supervisor.JobPool(
        command, concurrency=2, budget=0.5, grace=0.1, interval=0.02,
    )
    try:
        pump(pool, {1: 0, 2: 0}, lambda: pool.completions.get(2, 0) >= 3)
        assert 1 in pool.active
        assert pool.completions.get(1, 0) == 0
        old = pool.active[1].process
        pump(pool, {1: 0, 2: 0}, lambda: pool.completions.get(1, 0) >= 1)
        assert old.poll() == -signal.SIGKILL
        assert pool.results[1]["reason"] == "deadline"
        assert pool.completions[2] >= 3
    finally:
        pool.close()
    assert not pool.active


def test_blocked_loop_is_killed_and_slots_are_fair():
    def command(account_id, token, deadline):
        body = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(100)"
        return [sys.executable, "-c", body if account_id == 1 else "pass"]

    pool = sync_supervisor.JobPool(command, concurrency=1, budget=0.15, grace=0.05, interval=0)
    try:
        pool.tick({1: 0, 2: 0, 3: 0})
        old = pool.active[1].process
        pump(pool, {1: 0, 2: 0, 3: 0}, lambda: pool.completions.get(3, 0) == 1)
        assert old.poll() == -signal.SIGKILL
        assert pool.completions[2] >= 1
    finally:
        pool.close()


def test_shutdown_kills_ocr_descendants(tmp_path):
    pidfile = tmp_path / "descendant"
    body = f"""
import os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
p = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(100)'])
open({str(pidfile)!r}, 'w').write(str(p.pid))
time.sleep(100)
"""
    pool = sync_supervisor.JobPool(lambda *_: [sys.executable, "-c", body], grace=0.05)
    try:
        pump(pool, {1: 0}, pidfile.exists)
        child = int(pidfile.read_text())
        job = pool.active[1].process
    finally:
        pool.close()
    assert job.poll() is not None
    # A reparented zombie may briefly exist, but it cannot execute or own sockets.
    import subprocess
    end = time.monotonic() + 2
    while time.monotonic() < end:
        # noqa: S603, S607 - fixed argv, test-only
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(child)], capture_output=True, text=True).stdout.strip()  # noqa: S603, S607
        if not state or state.startswith("Z"):
            break
        time.sleep(.01)
    else:
        pytest.fail("orphan descendant still running")


def test_frozen_scheduler_is_replaced_without_using_its_loop(tmp_path):
    record = tmp_path / "scheduler.json"
    body = f"""
import os, time
from src.sync_health import write_record
write_record({str(record)!r}, {{'pid':os.getpid(), 'identity':os.environ['EMAILSERVER_PROCESS_IDENTITY'], 'heartbeat':time.monotonic()}})
time.sleep(100)
"""
    guard = sync_supervisor.ServiceGuard([sys.executable, "-c", body], record, timeout=.2, grace=.05)
    try:
        guard.tick()
        old = guard.process
        end = time.monotonic() + 4
        while time.monotonic() < end and guard.process is old:
            guard.tick()
            time.sleep(.01)
        assert guard.process is not old
        assert old.poll() is not None
    finally:
        guard.close()


def test_abrupt_supervisor_death_does_not_orphan_jobs(tmp_path):
    import subprocess
    pidfile = tmp_path / "job-pid"
    job = f"import os,time; open({str(pidfile)!r},'w').write(str(os.getpid())); time.sleep(100)"
    body = f"""
import sys,time
from src.sync_supervisor import JobPool
pool = JobPool(lambda *_: [sys.executable, '-c', {job!r}])
pool.tick({{1:0}})
time.sleep(100)
"""
    parent = subprocess.Popen([sys.executable, "-c", body])  # noqa: S603
    child = None
    try:
        end = time.monotonic() + 3
        while not pidfile.exists() and time.monotonic() < end:
            time.sleep(.01)
        assert pidfile.exists()
        child = int(pidfile.read_text())
        parent.kill()
        parent.wait(timeout=2)
        end = time.monotonic() + 2
        while time.monotonic() < end:
            state = subprocess.run(["ps", "-o", "stat=", "-p", str(child)], capture_output=True, text=True).stdout.strip()  # noqa: S603, S607
            if not state or state.startswith("Z"):
                break
            time.sleep(.01)
        else:
            pytest.fail("job survived the supervisor's death")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()
        if child is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(child, signal.SIGKILL)


def test_integrated_supervisor_exposes_progress_and_retry_without_pii(tmp_path, monkeypatch):
    from src.config import settings
    from src.sync_health import read_record

    monkeypatch.setattr(settings, "sync_watchdog_timeout_seconds", .4)
    monkeypatch.setattr(settings, "sync_job_timeout_seconds", .3)
    monkeypatch.setattr(settings, "sync_job_cleanup_seconds", .1)
    monkeypatch.setattr(settings, "email_check_interval", 0)
    monkeypatch.setattr(settings, "sync_account_concurrency", 2)
    api = f"""
import time
from src.sync_health import write_record, heartbeat
while True:
    write_record({str(tmp_path / 'api.json')!r}, heartbeat(database_ready=True))
    time.sleep(.03)
"""
    scheduler = f"""
import time
from src.sync_health import write_record, heartbeat
while True:
    write_record({str(tmp_path / 'scheduler.json')!r}, heartbeat(accounts=[{{'id':1,'retry_at':None}},{{'id':2,'retry_at':None}},{{'id':3,'retry_at':time.time()+100}}]))
    time.sleep(.03)
"""
    def job(aid, token, deadline):
        return [sys.executable, "-c", "import time; time.sleep(100)" if aid == 1 else "pass"]
    supervisor = sync_supervisor.Supervisor(
        tmp_path, api_command=[sys.executable, "-c", api],
        scheduler_command=[sys.executable, "-c", scheduler], worker_command=job,
    )
    try:
        end = time.monotonic() + 3
        while time.monotonic() < end:
            supervisor.tick()
            if supervisor.pool.completions.get(2, 0) >= 2:
                break
            time.sleep(.01)
        assert supervisor.pool.completions.get(2, 0) >= 2
        state = read_record(tmp_path / "supervisor.json")
        assert state["scheduler_healthy"]
        assert state["accounts"]["3"]["state"] == "backoff"
        assert "last_success_at" in state["accounts"]["2"]
        assert "username" not in str(state)
    finally:
        supervisor.close()
