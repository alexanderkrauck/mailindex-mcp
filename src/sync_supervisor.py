"""Synchronous process supervision. No database or account event loop in this layer."""

import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Callable


def signal_group(process: subprocess.Popen, sig: int) -> None:
    # Every child is a new session leader; extraction/OCR descendants inherit it.
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Darwin can report EPERM for a group containing only an exiting zombie.
        # Do not hide a real failure to terminate a live worker.
        if process.poll() is None:
            raise


def spawn(command, *, env=None):
    read_fd, write_fd = os.pipe()
    try:
        process = subprocess.Popen(  # noqa: S603 - argv never contains a shell
            [sys.executable, "-m", "src.sync_child", str(read_fd), *command],
            start_new_session=True, stdin=subprocess.DEVNULL,
            pass_fds=(read_fd,), env=env,
        )
    except BaseException:
        os.close(write_fd)
        raise
    finally:
        os.close(read_fd)
    process.lifeline_fd = write_fd
    return process


def reap(process):
    signal_group(process, signal.SIGKILL)
    process.wait()
    fd = getattr(process, "lifeline_fd", None)
    if fd is not None:
        os.close(fd)
        process.lifeline_fd = None


@dataclass
class Attempt:
    process: subprocess.Popen
    token: str
    started: float
    deadline: float
    stopping: bool = False


class JobPool:
    """Fair, independently due accounts with one killable attempt per account.

    A slot is not reusable until waitpid has reaped its previous owner. Even a
    successfully exited worker gets a group kill: OCR children must not survive.
    """

    def __init__(self, command: Callable, *, concurrency=4, budget=300, grace=5,
                 interval=30, on_complete=None):
        self.command = command
        self.concurrency = max(1, concurrency)
        self.budget = budget
        self.grace = grace
        self.interval = interval
        self.on_complete = on_complete
        self.active: dict[int, Attempt] = {}
        self.due: dict[int, float] = {}
        self.completions: dict[int, int] = {}
        self.results: dict[int, dict] = {}

    def tick(self, accounts: dict[int, float]) -> None:
        now = time.monotonic()
        for account_id, job in list(self.active.items()):
            if now >= job.deadline and not job.stopping:
                job.stopping = True
                signal_group(job.process, signal.SIGTERM)
            if now >= job.deadline + self.grace:
                signal_group(job.process, signal.SIGKILL)
            code = job.process.poll()
            if code is None:
                continue
            reap(job.process)
            result = {
                "account_id": account_id, "token": job.token,
                "pid": job.process.pid, "started": job.started,
                "finished": now, "exit_code": code,
                "reason": "deadline" if job.stopping else ("success" if code == 0 else "failed"),
            }
            self.results[account_id] = result
            self.completions[account_id] = self.completions.get(account_id, 0) + 1
            # Failure cannot busy-loop while the scheduler persists backoff.
            self.due[account_id] = now + (max(30, self.interval) if code else self.interval)
            del self.active[account_id]
            if self.on_complete:
                self.on_complete(result)

        for account_id in list(self.due):
            if account_id not in accounts and account_id not in self.active:
                self.due.pop(account_id, None)
                self.results.pop(account_id, None)
                self.completions.pop(account_id, None)
        for account_id in accounts:
            self.due.setdefault(account_id, now)
        eligible = sorted(
            (max(self.due[aid], available), aid)
            for aid, available in accounts.items() if aid not in self.active
        )
        for due, account_id in eligible:
            if len(self.active) >= self.concurrency or due > now:
                break
            token = uuid.uuid4().hex
            deadline = time.monotonic() + self.budget
            process = spawn(self.command(account_id, token, deadline))
            self.active[account_id] = Attempt(process, token, now, deadline)

    def close(self) -> None:
        for job in self.active.values():
            signal_group(job.process, signal.SIGTERM)
        end = time.monotonic() + self.grace
        while self.active and time.monotonic() < end:
            for account_id, job in list(self.active.items()):
                if job.process.poll() is not None:
                    reap(job.process)
                    del self.active[account_id]
            if self.active:
                time.sleep(0.01)
        for job in self.active.values():
            reap(job.process)
        self.active.clear()


class ServiceGuard:
    """Restart an exited/frozen service using a heartbeat from its actual loop."""

    def __init__(self, command, record_path, *, timeout=30, grace=5):
        self.command = command
        self.record_path = record_path
        self.timeout = timeout
        self.grace = grace
        self.process = None
        self.identity = None
        self.started = 0.0
        self.stopping = None
        self.restarts = 0

    def tick(self):
        from src.sync_health import fresh, read_record

        now = time.monotonic()
        if self.process is not None:
            record = read_record(self.record_path)
            alive = fresh(record, self.timeout, identity=self.identity)
            if not alive and now - self.started > self.timeout and self.stopping is None:
                self.stopping = now
                signal_group(self.process, signal.SIGTERM)
            if self.stopping is not None and now - self.stopping >= self.grace:
                signal_group(self.process, signal.SIGKILL)
            if self.process.poll() is None:
                return
            reap(self.process)
            self.process = None
            self.restarts += 1
        self.identity = uuid.uuid4().hex
        self.process = spawn(
            self.command, env={**os.environ, "EMAILSERVER_PROCESS_IDENTITY": self.identity},
        )
        self.started = now
        self.stopping = None

    def close(self):
        if self.process is None:
            return
        signal_group(self.process, signal.SIGTERM)
        try:
            self.process.wait(timeout=self.grace)
        except subprocess.TimeoutExpired:
            signal_group(self.process, signal.SIGKILL)
            self.process.wait()
        reap(self.process)
        self.process = None


class Supervisor:
    """Root watchdog and job scheduler. This process never imports the DB driver."""

    def __init__(self, root, *, api_command=None, scheduler_command=None, worker_command=None):
        from src.config import settings
        from src.sync_health import write_record

        self.root = root
        self.identity = uuid.uuid4().hex
        self.settings = settings
        self.accounts = []
        self.api = ServiceGuard(
            api_command or [sys.executable, "-m", "src.main", "--api-child"], root / "api.json",
            timeout=settings.sync_watchdog_timeout_seconds, grace=settings.sync_job_cleanup_seconds,
        )
        self.scheduler = ServiceGuard(
            scheduler_command or [sys.executable, "-m", "src.sync_scheduler"], root / "scheduler.json",
            timeout=settings.sync_watchdog_timeout_seconds, grace=settings.sync_job_cleanup_seconds,
        )
        self.pool = JobPool(
            worker_command or (lambda aid, token, deadline: [
                sys.executable, "-m", "src.sync_worker", str(aid), token, str(deadline),
            ]), concurrency=settings.sync_account_concurrency,
            budget=settings.sync_job_timeout_seconds, grace=settings.sync_job_cleanup_seconds,
            interval=settings.email_check_interval,
            on_complete=lambda result: (
                write_record(root / f"result-{result['account_id']}.json", result)
                if result["exit_code"] else None
            ),
        )

    def tick(self):
        from src.sync_health import fresh, heartbeat, read_record, write_record

        timeout = self.settings.sync_watchdog_timeout_seconds
        self.api.tick()
        api = read_record(self.root / "api.json")
        api_ready = fresh(api, timeout, identity=self.api.identity) and api.get("database_ready", False)
        if api_ready or self.scheduler.process is not None:
            self.scheduler.tick()
        manifest = read_record(self.root / "scheduler.json")
        scheduler_healthy = self.scheduler.process is not None and fresh(
            manifest, timeout, identity=self.scheduler.identity,
        )
        if scheduler_healthy:
            self.accounts = manifest.get("accounts", [])
        now, wall = time.monotonic(), time.time()
        eligible = {}
        for row in self.accounts:
            aid = row["id"]
            available = max(row.get("retry_at") or 0, row.get("lease_expires_at") or 0)
            eligible[aid] = now + available - wall
            request = self.root / f"request-{aid}.json"
            if request.exists() and scheduler_healthy:
                # Coalesce requests for an active job, and retain queue fairness.
                self.pool.due[aid] = min(self.pool.due.get(aid, now), now)
                eligible[aid] = now + (row.get("lease_expires_at") or 0) - wall
                request.unlink(missing_ok=True)
            # Recovery must acknowledge a previous result before retrying it.
            if not scheduler_healthy or (self.root / f"result-{aid}.json").exists():
                eligible[aid] = float("inf")
        self.pool.tick(eligible)
        progress_healthy = scheduler_healthy
        details = {}
        for row in self.accounts:
            aid = row["id"]
            active = self.pool.active.get(aid)
            due = max(self.pool.due.get(aid, now), eligible[aid])
            state = "idle"
            if active:
                state = "running" if now < active.deadline else "terminating"
                progress_healthy &= now < active.deadline
            elif (row.get("retry_at") or 0) > wall:
                state = "backoff"
            elif now - due > self.settings.sync_stale_after_seconds:
                state = "overdue"
                progress_healthy = False
            progress = read_record(self.root / f"progress-{aid}.json")
            if active and progress.get("token") != active.token:
                progress = {}
            details[str(aid)] = {
                "state": state, "retry_at": row.get("retry_at"),
                "last_attempt_at": row.get("last_attempt_at"),
                "last_success_at": row.get("last_success_at"),
                "next_due_in": max(0, due - now) if due != float("inf") else None,
                "pid": active.process.pid if active else None,
                "identity": active.token if active else None,
                "started": active.started if active else None,
                "deadline": active.deadline if active else None,
                "last_checkpoint": progress.get("checkpoint"),
                "last_completion": self.pool.results.get(aid, {}).get("finished"),
            }
        write_record(self.root / "supervisor.json", heartbeat(
            identity=self.identity, api_ready=bool(api_ready),
            scheduler_healthy=bool(scheduler_healthy), progress_healthy=bool(progress_healthy),
            accounts=details,
        ))

    def close(self):
        self.pool.close()
        self.scheduler.close()
        self.api.close()
        (self.root / "supervisor.json").unlink(missing_ok=True)


def run_supervised():
    import fcntl

    from src.sync_health import runtime_dir

    root = runtime_dir()
    # One supervisor per data directory; the lock is NOT inherited by children.
    with (root / "supervisor.lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        supervisor = Supervisor(root)
        stopping = False

        def stop(*_):
            nonlocal stopping
            stopping = True

        previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            while not stopping:
                supervisor.tick()
                time.sleep(0.1)
        finally:
            supervisor.close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
