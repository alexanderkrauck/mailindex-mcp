"""Gmail throttling: wait, pace and resume instead of failing the synchronization."""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.email import email_processor
from src.email.email_processor import EmailProcessor
from src.email.gmail_api_client import GmailApiClient, GmailRateLimited
from src.models.base import Base
from src.models.smtp_config import SMTPConfig
from src.models.user import User

# What Gmail answered production during a catch-up (message text shortened).
THROTTLED = {
    "error": {
        "code": 403,
        "message": "Quota exceeded for quota metric 'Total Query Cost' and limit 'Units per minute per user'",
        "errors": [{"message": "Quota exceeded", "domain": "usageLimits", "reason": "rateLimitExceeded"}],
        "status": "PERMISSION_DENIED",
    }
}
FORBIDDEN = {
    "error": {
        "code": 403,
        "message": "Request had insufficient authentication scopes.",
        "errors": [{"message": "Insufficient Permission", "domain": "global", "reason": "insufficientPermissions"}],
        "status": "PERMISSION_DENIED",
    }
}


class FakeTime:
    """A clock that moves only when the client sleeps; for one coroutine at a time."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 6))
        self.now += seconds
        await asyncio.sleep(0)


def _config():
    return SimpleNamespace(id=9, name="OAuth Gmail", credential_ciphertext="unused", sync_cursors={})


def _client(handler, fake: FakeTime | None = None, **kwargs):
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    if fake is not None:
        kwargs.setdefault("clock", fake.clock)
        kwargs.setdefault("sleep", fake.sleep)
    return GmailApiClient(_config(), http_client=http_client, access_token="token", **kwargs), http_client


def _scripted(fake: FakeTime, *responses: httpx.Response):
    sent: list[float] = []
    queue = list(responses)

    async def handler(request: httpx.Request) -> httpx.Response:
        sent.append(fake.now)
        return queue.pop(0)

    return handler, sent


@pytest.mark.asyncio
async def test_a_throttled_fetch_waits_then_succeeds():
    fake = FakeTime()
    handler, sent = _scripted(fake, httpx.Response(403, json=THROTTLED), httpx.Response(200, json={"id": "m1"}))
    client, http_client = _client(handler, fake, throttle_deadline=fake.now + 120)
    try:
        message = await client.get_message("m1")
    finally:
        await http_client.aclose()

    assert message == {"id": "m1"}
    assert fake.sleeps == [5.0]
    assert sent[1] - sent[0] == 5.0
    assert client.fetched_messages == 1


@pytest.mark.asyncio
async def test_a_permission_403_is_final():
    fake = FakeTime()
    handler, sent = _scripted(fake, httpx.Response(403, json=FORBIDDEN))
    client, http_client = _client(handler, fake, throttle_deadline=fake.now + 120)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_message("m1")
    finally:
        await http_client.aclose()

    assert len(sent) == 1
    assert fake.sleeps == []


@pytest.mark.asyncio
async def test_a_403_without_a_json_body_is_final():
    fake = FakeTime()
    handler, sent = _scripted(fake, httpx.Response(403, text="<html>Forbidden</html>"))
    client, http_client = _client(handler, fake, throttle_deadline=fake.now + 120)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_message("m1")
    finally:
        await http_client.aclose()
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_a_429_waits_as_long_as_gmail_asks():
    fake = FakeTime()
    handler, _ = _scripted(
        fake,
        httpx.Response(429, headers={"Retry-After": "7"}, json={}),
        httpx.Response(200, json={"id": "m1"}),
    )
    client, http_client = _client(handler, fake, throttle_deadline=fake.now + 120)
    try:
        assert await client.get_message("m1") == {"id": "m1"}
    finally:
        await http_client.aclose()
    assert fake.sleeps == [7.0]


@pytest.mark.asyncio
async def test_a_retry_after_of_zero_still_pauses():
    fake = FakeTime()
    handler, _ = _scripted(
        fake,
        httpx.Response(429, headers={"Retry-After": "0"}, json={}),
        httpx.Response(200, json={"id": "m1"}),
    )
    client, http_client = _client(handler, fake, throttle_deadline=fake.now + 120)
    try:
        assert await client.get_message("m1") == {"id": "m1"}
    finally:
        await http_client.aclose()
    assert fake.sleeps == [1.0]


@pytest.mark.asyncio
async def test_fresh_throttles_back_off_further_and_a_success_resets():
    fake = FakeTime()
    handler, _ = _scripted(
        fake,
        httpx.Response(403, json=THROTTLED),
        httpx.Response(403, json=THROTTLED),
        httpx.Response(403, json=THROTTLED),
        httpx.Response(200, json={"id": "m1"}),
        httpx.Response(403, json=THROTTLED),
        httpx.Response(200, json={"id": "m2"}),
    )
    client, http_client = _client(handler, fake, throttle_deadline=fake.now + 600)
    try:
        await client.get_message("m1")
        await client.get_message("m2")
    finally:
        await http_client.aclose()

    assert client.throttle_pauses == [5.0, 10.0, 20.0, 5.0]
    # Paced once throttled: the second fetch waits its slot before sending.
    assert fake.sleeps == [5.0, 10.0, 20.0, 0.25, 5.0]


@pytest.mark.asyncio
async def test_it_gives_up_when_the_wait_would_outlast_the_deadline():
    fake = FakeTime()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json=THROTTLED)

    client, http_client = _client(handler, fake, throttle_deadline=fake.now + 12)
    try:
        with pytest.raises(GmailRateLimited) as raised:
            await client.get_message("m1")
    finally:
        await http_client.aclose()

    # Waited 5s once; the next pause (10s) would end past the deadline.
    assert fake.sleeps == [5.0]
    assert raised.value.retry_after == 10.0
    assert raised.value.fetched_messages == 0


@pytest.mark.asyncio
async def test_giving_up_reports_the_messages_already_fetched():
    fake = FakeTime()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/m1"):
            return httpx.Response(200, json={"id": "m1"})
        return httpx.Response(403, json=THROTTLED)

    client, http_client = _client(handler, fake, throttle_deadline=fake.now + 3)
    try:
        await client.get_message("m1")
        with pytest.raises(GmailRateLimited) as raised:
            await client.get_message("m2")
    finally:
        await http_client.aclose()

    assert raised.value.fetched_messages == 1
    assert fake.sleeps == []


@pytest.mark.asyncio
async def test_a_tool_call_waits_only_briefly():
    fake = FakeTime()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json=THROTTLED)

    client, http_client = _client(handler, fake)  # no deadline of its own
    try:
        with pytest.raises(GmailRateLimited):
            await client.get_message("m1")
    finally:
        await http_client.aclose()
    assert sum(fake.sleeps) <= GmailApiClient.INTERACTIVE_THROTTLE_BUDGET_SECONDS


@pytest.mark.asyncio
async def test_a_throttled_burst_backs_off_once_then_sends_one_at_a_time(monkeypatch):
    monkeypatch.setattr(GmailApiClient, "THROTTLE_BACKOFF_SECONDS", (0.2, 0.4, 0.8))
    monkeypatch.setattr(GmailApiClient, "MIN_THROTTLE_PAUSE_SECONDS", 0.01)
    monkeypatch.setattr(GmailApiClient, "PACED_INTERVAL_SECONDS", 0.02)
    sends: list[tuple[float, int]] = []
    burst = 10

    async def handler(request: httpx.Request) -> httpx.Response:
        at = time.monotonic()
        answered_before = len(sends)
        await asyncio.sleep(0.01)  # in flight together, as on the wire
        if answered_before < burst:
            sends.append((at, 403))
            return httpx.Response(403, json=THROTTLED)
        sends.append((at, 200))
        return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[-1]})

    client, http_client = _client(handler, throttle_deadline=time.monotonic() + 30)
    try:
        ids = [f"m{n}" for n in range(burst)]
        results = await asyncio.gather(*(client.get_message(i) for i in ids))
    finally:
        await http_client.aclose()

    assert [r["id"] for r in results] == ids
    assert [code for _, code in sends[:burst]] == [403] * burst
    # One pause for the whole burst: the other nine throttles were in flight already.
    assert client.throttle_pauses == [0.2]
    retries = sorted(at for at, code in sends[burst:])
    assert len(retries) == burst
    # Nothing was sent while the gate was closed ...
    assert retries[0] >= client._gate_until
    # ... and afterwards one request at a time, at the paced spacing.
    assert retries[-1] - retries[0] >= (burst - 1) * 0.02 - 0.03


# ---- the synchronization run


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    from src.database import connection, sync_fence

    engine = create_engine(f"sqlite:///{tmp_path / 'throttle.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=sync_fence.SyncSession, autoflush=False)
    with factory.begin() as db:
        db.add(User(id=1, google_sub="owner", email="owner@example.invalid"))
        db.add(
            SMTPConfig(
                id=1,
                owner_user_id=1,
                name="OAuth Gmail",
                host="imap.gmail.com",
                port=993,
                username="owner@example.invalid",
                credential_ciphertext="test",
                provider="gmail",
                auth_type="oauth2",
                backfill_complete=False,
                consecutive_failures=4,
            )
        )
    monkeypatch.setattr(email_processor, "SessionLocal", factory)
    monkeypatch.setattr(connection, "SessionLocal", factory)
    yield factory
    engine.dispose()


async def _run_throttled(sessions, monkeypatch, error: Exception) -> SMTPConfig:
    with sessions() as db:
        config = SMTPConfig.create_detached(db.get(SMTPConfig, 1))
    processor = EmailProcessor()
    processor.processing = True
    monkeypatch.setattr(processor, "_process_gmail_api", AsyncMock(side_effect=error))
    assert await processor._process_server(config, force=True) is False
    with sessions() as db:
        account = db.get(SMTPConfig, 1)
        db.expunge(account)
        return account


@pytest.mark.asyncio
async def test_a_throttled_run_that_fetched_mail_is_not_an_error(sessions, monkeypatch):
    before = datetime.now(timezone.utc)
    account = await _run_throttled(sessions, monkeypatch, GmailRateLimited(42, fetched_messages=3))

    assert account.sync_state == "backfilling"
    assert account.last_error_code == "PROVIDER_RATE_LIMITED"
    assert account.consecutive_failures == 0
    retry_at = account.retry_at.replace(tzinfo=timezone.utc)
    assert before + timedelta(seconds=42) <= retry_at <= datetime.now(timezone.utc) + timedelta(seconds=43)
    assert account.sync_lock_token is None


@pytest.mark.asyncio
async def test_a_throttled_run_that_fetched_nothing_is_an_error(sessions, monkeypatch):
    before = datetime.now(timezone.utc)
    account = await _run_throttled(sessions, monkeypatch, GmailRateLimited(900, fetched_messages=0))

    assert account.sync_state == "error"
    assert account.last_error_code == "PROVIDER_RATE_LIMITED"
    assert account.consecutive_failures == 5
    # The usual backoff for a fifth failure is 480s; Gmail's longer wait wins.
    assert account.retry_at.replace(tzinfo=timezone.utc) >= before + timedelta(seconds=900)


@pytest.mark.asyncio
async def test_a_chunk_keeps_what_arrived_and_waits_for_every_fetch(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "gmail_request_concurrency", 3)
    finished: list[str] = []

    async def get_parsed_message(message_id):
        if message_id == "two":
            raise GmailRateLimited(30, fetched_messages=1)
        await asyncio.sleep(0.02 if message_id == "three" else 0)
        finished.append(message_id)
        return {"provider_message_id": message_id}

    processor = EmailProcessor()
    processor._process_emails = AsyncMock()
    with pytest.raises(GmailRateLimited):
        await processor._fetch_and_process_gmail_messages(
            SimpleNamespace(get_parsed_message=get_parsed_message), ["one", "two", "three", "four"]
        )

    assert sorted(finished) == ["one", "three"]
    processor._process_emails.assert_awaited_once()
    assert [m["provider_message_id"] for m in processor._process_emails.await_args.args[0]] == ["one", "three"]


def test_the_run_stops_waiting_before_its_job_deadline():
    processor = EmailProcessor()
    processor.job_deadline = 5000.0
    assert processor._gmail_throttle_deadline() == 5000.0 - EmailProcessor.GMAIL_THROTTLE_MARGIN_SECONDS


@pytest.mark.asyncio
async def test_the_worker_hands_its_deadline_to_the_run(sessions, monkeypatch):
    from src import sync_worker

    seen = []

    async def process_server(self, config, force=False, *, leased=False):
        seen.append(self.job_deadline)
        return True

    monkeypatch.setattr(EmailProcessor, "_process_server", process_server)
    deadline = time.monotonic() + 30
    assert await sync_worker.run_account(1, "worker-token", deadline) == 0
    assert seen == [deadline]


@pytest.mark.asyncio
async def test_a_tool_call_reports_gmail_throttling_as_retryable(monkeypatch):
    from src.services import write_service

    monkeypatch.setattr(write_service, "acquire_mailbox_lease", lambda db, account, seconds: "lease")
    monkeypatch.setattr(write_service, "refresh_mailbox_lease", lambda token, seconds: True)
    monkeypatch.setattr(write_service, "release_mailbox_lease", lambda db, token: None)
    account = SMTPConfig(
        id=5,
        owner_user_id=1,
        name="OAuth Gmail",
        host="imap.gmail.com",
        port=993,
        username="owner@example.invalid",
        provider="gmail",
        auth_type="oauth2",
    )

    async def operation(client):
        raise GmailRateLimited(12.2)

    with pytest.raises(HTTPException) as raised:
        await write_service._gmail(None, account, operation)
    assert raised.value.status_code == 429
    assert raised.value.detail["retry_after"] == 13
