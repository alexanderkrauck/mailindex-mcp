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

from src.email import email_processor, gmail_api_client
from src.email.email_processor import EmailProcessor
from src.email.gmail_api_client import GmailApiClient, GmailRateLimited
from src.models.base import Base
from src.models.email import EmailLog
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
    kwargs.setdefault("access_token", "token")
    return GmailApiClient(_config(), http_client=http_client, **kwargs), http_client


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
async def test_pauses_grow_within_a_throttled_period_even_between_successes():
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

    assert client.throttle_pauses == [5.0, 10.0, 20.0, 40.0]
    # Paced once throttled: the second fetch waits for its slot first.
    assert fake.sleeps == [5.0, 10.0, 20.0, 0.1, 40.0]


@pytest.mark.asyncio
async def test_the_throttled_period_ends_after_a_quiet_minute():
    fake = FakeTime()
    handler, _ = _scripted(
        fake,
        httpx.Response(403, json=THROTTLED),
        httpx.Response(200, json={"id": "m1"}),
        httpx.Response(403, json=THROTTLED),
        httpx.Response(200, json={"id": "m2"}),
        httpx.Response(200, json={"id": "m3"}),
    )
    client, http_client = _client(handler, fake, throttle_deadline=fake.now + 600)
    try:
        await client.get_message("m1")
        fake.now += GmailApiClient.PACE_HOLD_SECONDS + 1  # a quiet minute
        await client.get_message("m2")
        fake.now += GmailApiClient.PACE_HOLD_SECONDS + 1
        await client.get_message("m3")
    finally:
        await http_client.aclose()

    # The second period starts over at 5 s, and after it ends nothing is paced.
    assert client.throttle_pauses == [5.0, 5.0]
    assert fake.sleeps == [5.0, 5.0]


@pytest.mark.asyncio
async def test_paced_requests_stop_at_the_deadline():
    fake = FakeTime()
    deadline = fake.now + 15.95
    sent: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        sent.append(fake.now)
        if len(sent) in (1, 3):
            return httpx.Response(403, json=THROTTLED)
        return httpx.Response(200, json={"id": "m"})

    client, http_client = _client(handler, fake, throttle_deadline=deadline)
    try:
        with pytest.raises(GmailRateLimited) as raised:
            for _ in range(50):
                await client.get_message("m")
    finally:
        await http_client.aclose()

    # Throttled at 1000.0 (5 s) and at 1005.1 (10 s); then one request every
    # 0.1 s from 1015.1 until a slot would fall past the deadline: twelve in all,
    # not the fifty asked for.
    assert client.throttle_pauses == [5.0, 10.0]
    assert len(sent) == 12
    assert max(sent) <= deadline
    # Come back after as long as the quota last needed, not straight away.
    assert raised.value.retry_after == 10.0


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
    assert raised.value.made_progress is False


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
async def test_a_token_refresh_does_not_carry_a_request_past_a_closed_gate():
    fake = FakeTime()
    handler, sent = _scripted(fake, httpx.Response(200, json={"id": "m1"}))
    client, http_client = _client(handler, fake, throttle_deadline=fake.now + 120)
    refreshes = 0

    async def token():
        nonlocal refreshes
        refreshes += 1
        if refreshes == 1:
            # Another request's throttle lands while this one fetches its token.
            client._note_throttle(httpx.Response(403, json=THROTTLED), sent_at=fake.now)
        return "token"

    client._token = token
    try:
        await client.get_message("m1")
    finally:
        await http_client.aclose()
    assert sent == [1005.0]


@pytest.mark.asyncio
@pytest.mark.parametrize("late_answers_after", [0.0, 0.2])
async def test_parallel_401s_refresh_the_token_once(monkeypatch, late_answers_after):
    """At 0.0 every 401 needs a new token at once (one refresh, not five); at 0.2 four
    401s for the old token arrive after the refresh and must not discard the new one."""
    refreshed: list[str] = []

    def refresh(ciphertext):
        refreshed.append(ciphertext)
        return "fresh"

    monkeypatch.setattr(gmail_api_client, "refresh_access_token", refresh)
    stale_seen = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal stale_seen
        if request.headers["Authorization"] == "Bearer stale":
            stale_seen += 1
            # Every request is in flight with the stale token before any answer.
            await asyncio.sleep(0.01 if stale_seen == 1 else 0.01 + late_answers_after)
            return httpx.Response(401, json={})
        return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[-1]})

    client, http_client = _client(handler, access_token="stale")
    try:
        results = await asyncio.gather(*(client.get_message(f"m{n}") for n in range(5)))
    finally:
        await http_client.aclose()

    assert stale_seen == 5
    assert [r["id"] for r in results] == [f"m{n}" for n in range(5)]
    assert refreshed == ["unused"]


@pytest.mark.asyncio
async def test_a_throttled_permanent_delete_waits_then_deletes():
    fake = FakeTime()
    calls: list[tuple[str, str, int]] = []
    answers = [httpx.Response(403, json=THROTTLED), httpx.Response(204)]

    async def handler(request: httpx.Request) -> httpx.Response:
        response = answers.pop(0)
        calls.append((request.method, request.url.path, response.status_code))
        return response

    client, http_client = _client(handler, fake, throttle_deadline=fake.now + 120)
    try:
        await client.delete_message_permanently("m1")
    finally:
        await http_client.aclose()

    # Gmail's route is DELETE messages/{id}; it has no POST messages/{id}/delete.
    path = "/gmail/v1/users/me/messages/m1"
    assert calls == [("DELETE", path, 403), ("DELETE", path, 204)]
    assert fake.sleeps == [5.0]


@pytest.mark.asyncio
async def test_a_permanent_delete_of_a_message_gmail_no_longer_has_succeeds():
    fake = FakeTime()
    handler, sent = _scripted(fake, httpx.Response(404, json={"error": {"code": 404, "message": "Not Found"}}))
    client, http_client = _client(handler, fake, throttle_deadline=fake.now + 120)
    try:
        await client.delete_message_permanently("gone")
    finally:
        await http_client.aclose()
    assert len(sent) == 1


INVALID = {"error": {"code": 400, "message": "Invalid id value", "status": "INVALID_ARGUMENT"}}
NO_SUCH_ROUTE = "<!DOCTYPE html><html lang=en><title>Error 404 (Not Found)!!1</title></html>"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(400, json=INVALID),
        httpx.Response(403, json=FORBIDDEN),
        # What a route Gmail does not have answers: not "already deleted".
        httpx.Response(404, text=NO_SUCH_ROUTE, headers={"content-type": "text/html"}),
    ],
)
async def test_a_permanent_delete_gmail_refuses_is_an_error(answer):
    fake = FakeTime()
    handler, sent = _scripted(fake, answer)
    client, http_client = _client(handler, fake, throttle_deadline=fake.now + 120)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.delete_message_permanently("m1")
    finally:
        await http_client.aclose()
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_labels_are_read_fresh_from_gmail():
    fake = FakeTime()
    seen: list[tuple[str, str]] = []
    answers = [
        httpx.Response(200, json={"id": "m1", "labelIds": ["TRASH", "IMPORTANT"]}),
        httpx.Response(404, json={"error": {"code": 404, "message": "Requested entity was not found."}}),
        httpx.Response(404, text=NO_SUCH_ROUTE, headers={"content-type": "text/html"}),
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.url.params.get("format")))
        return answers.pop(0)

    client, http_client = _client(handler, fake, throttle_deadline=fake.now + 120)
    try:
        assert await client.get_labels("m1") == ["IMPORTANT", "TRASH"]
        assert await client.get_labels("gone") is None
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_labels("m3")
    finally:
        await http_client.aclose()
    assert seen[0] == ("/gmail/v1/users/me/messages/m1", "minimal")


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


def _account(sessions) -> SMTPConfig:
    with sessions() as db:
        account = db.get(SMTPConfig, 1)
        db.expunge(account)
        return account


async def _run_throttled(sessions, monkeypatch, error: Exception) -> SMTPConfig:
    with sessions() as db:
        config = SMTPConfig.create_detached(db.get(SMTPConfig, 1))
    processor = EmailProcessor()
    processor.processing = True
    monkeypatch.setattr(processor, "_process_gmail_api", AsyncMock(side_effect=error))
    assert await processor._process_server(config, force=True) is False
    return _account(sessions)


def _progressed(retry_after: float) -> GmailRateLimited:
    error = GmailRateLimited(retry_after)
    error.made_progress = True
    return error


@pytest.mark.asyncio
async def test_a_throttled_run_that_made_progress_is_not_an_error(sessions, monkeypatch):
    before = datetime.now(timezone.utc)
    account = await _run_throttled(sessions, monkeypatch, _progressed(42))

    assert account.sync_state == "backfilling"
    assert account.last_error_code == "PROVIDER_RATE_LIMITED"
    assert account.consecutive_failures == 0
    retry_at = account.retry_at.replace(tzinfo=timezone.utc)
    assert before + timedelta(seconds=42) <= retry_at <= datetime.now(timezone.utc) + timedelta(seconds=43)
    assert account.sync_lock_token is None


@pytest.mark.asyncio
async def test_a_throttled_run_without_progress_is_an_error(sessions, monkeypatch):
    before = datetime.now(timezone.utc)
    account = await _run_throttled(sessions, monkeypatch, GmailRateLimited(900))

    assert account.sync_state == "error"
    assert account.last_error_code == "PROVIDER_RATE_LIMITED"
    assert account.consecutive_failures == 5
    # The usual backoff for a fifth failure is 480s; Gmail's longer wait wins.
    assert account.retry_at.replace(tzinfo=timezone.utc) >= before + timedelta(seconds=900)


class FakeGmail:
    """The Gmail calls a synchronization run makes, with throttles where asked."""

    def __init__(self, ids, *, throttle=(), history=None):
        self.ids = list(ids)
        self.throttle = set(throttle)
        self.history = history
        self.fetched: list[str] = []
        self.profile_calls = 0
        self.page_sizes: list[int] = []

    async def get_profile(self):
        self.profile_calls += 1
        return {"historyId": "500", "messagesTotal": len(self.ids)}

    async def list_messages(self, *, page_token=None, max_results=100):
        return {"messages": [{"id": message_id} for message_id in self.ids]}

    async def list_history(self, start_history_id, *, page_token=None, max_results=100):
        self.page_sizes.append(max_results)
        return self.history

    async def get_parsed_message(self, message_id):
        if message_id in self.throttle:
            raise GmailRateLimited(30)
        self.fetched.append(message_id)
        return {"provider_message_id": message_id}

    async def close(self):
        pass


def _recording_processor(sessions, monkeypatch, gmail: FakeGmail) -> EmailProcessor:
    processor = EmailProcessor()
    processor.processing = True
    monkeypatch.setattr(email_processor, "GmailApiClient", lambda config, **kwargs: gmail)

    async def record(messages):
        with sessions.begin() as db:
            for message in messages:
                row = (
                    db.query(EmailLog)
                    .filter_by(smtp_config_id=1, provider_message_id=message["provider_message_id"])
                    .one_or_none()
                )
                if row is None:
                    row = EmailLog(
                        smtp_config_id=1,
                        sender="a",
                        recipient="b",
                        subject="s",
                        message_id=message["provider_message_id"],
                        provider_message_id=message["provider_message_id"],
                        email_date=datetime.now(timezone.utc),
                    )
                    db.add(row)
                row.last_seen_sync_generation = message.get("last_seen_sync_generation")

    processor._process_emails = record
    return processor


async def _sync(sessions, processor) -> bool:
    with sessions() as db:
        config = SMTPConfig.create_detached(db.get(SMTPConfig, 1))
    return await processor._process_server(config, force=True)


@pytest.mark.asyncio
async def test_a_full_sync_cut_short_on_its_first_page_resumes_where_it_stopped(sessions, monkeypatch):
    ids = [f"m{n}" for n in range(1, 6)]
    first = FakeGmail(ids, throttle={"m4"})
    assert await _sync(sessions, _recording_processor(sessions, monkeypatch, first)) is False

    account = _account(sessions)
    assert account.sync_state == "backfilling"
    assert account.last_error_code == "PROVIDER_RATE_LIMITED"
    assert account.consecutive_failures == 0
    assert account.sync_page_token is None  # the page never finished
    assert account.backfill_processed == 4

    second = FakeGmail(ids)
    assert await _sync(sessions, _recording_processor(sessions, monkeypatch, second)) is True

    # Same generation, no new snapshot, and only the message the first run did not record.
    assert second.profile_calls == 0
    assert second.fetched == ["m4"]
    account = _account(sessions)
    assert account.sync_generation == 1
    assert account.initial_sync_complete is True
    assert account.sync_state == "healthy"
    assert account.last_error_code is None
    assert account.backfill_processed == 5
    with sessions() as db:
        rows = db.query(EmailLog).filter_by(smtp_config_id=1).all()
        assert sorted(row.provider_message_id for row in rows) == ids
        assert {row.last_seen_sync_generation for row in rows} == {1}
        assert all(row.deleted_at is None for row in rows)


@pytest.mark.asyncio
async def test_a_history_page_cut_short_is_not_progress(sessions, monkeypatch):
    with sessions.begin() as db:
        account = db.get(SMTPConfig, 1)
        account.initial_sync_complete = True
        account.backfill_complete = True
        account.provider_sync_token = "500"
    history = {"history": [{"messages": [{"id": "h1"}, {"id": "h2"}]}], "historyId": "600"}

    gmail = FakeGmail([], throttle={"h2"}, history=history)
    assert await _sync(sessions, _recording_processor(sessions, monkeypatch, gmail)) is False
    # h1 was fetched, but the page is fetched again in full next time: no progress.
    assert gmail.fetched == ["h1"]
    assert gmail.page_sizes == [100]
    account = _account(sessions)
    assert account.sync_state == "error"
    assert account.last_error_code == "PROVIDER_RATE_LIMITED"
    assert account.consecutive_failures == 5
    assert account.provider_sync_token == "500"

    gmail = FakeGmail([], history=history)
    assert await _sync(sessions, _recording_processor(sessions, monkeypatch, gmail)) is True
    # Smaller pages after runs that could not finish one: 100 halved per failure.
    assert gmail.page_sizes == [3]
    account = _account(sessions)
    assert account.provider_sync_token == "600"
    assert account.sync_state == "healthy"
    assert account.consecutive_failures == 0

    gmail = FakeGmail([], history={"history": [], "historyId": "700"})
    assert await _sync(sessions, _recording_processor(sessions, monkeypatch, gmail)) is True
    assert gmail.page_sizes == [100]


@pytest.mark.asyncio
async def test_a_chunk_keeps_what_arrived_and_waits_for_every_fetch(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "gmail_request_concurrency", 3)
    finished: list[str] = []

    async def get_parsed_message(message_id):
        if message_id == "two":
            raise GmailRateLimited(30)
        await asyncio.sleep(0.02 if message_id == "three" else 0)
        finished.append(message_id)
        return {"provider_message_id": message_id}

    processor = EmailProcessor()
    processor._process_emails = AsyncMock()
    committed = []

    async def on_committed(count):
        committed.append(count)

    with pytest.raises(GmailRateLimited):
        await processor._fetch_and_process_gmail_messages(
            SimpleNamespace(get_parsed_message=get_parsed_message),
            ["one", "two", "three", "four"],
            on_committed=on_committed,
        )

    assert sorted(finished) == ["one", "three"]
    processor._process_emails.assert_awaited_once()
    assert [m["provider_message_id"] for m in processor._process_emails.await_args.args[0]] == ["one", "three"]
    assert committed == [2]


@pytest.mark.asyncio
async def test_a_processing_failure_keeps_the_throttle_as_its_cause():
    async def get_parsed_message(message_id):
        if message_id == "two":
            raise GmailRateLimited(30)
        return {"provider_message_id": message_id}

    processor = EmailProcessor()
    processor._process_emails = AsyncMock(side_effect=RuntimeError("database gone"))
    with pytest.raises(RuntimeError) as raised:
        await processor._fetch_and_process_gmail_messages(
            SimpleNamespace(get_parsed_message=get_parsed_message), ["one", "two"]
        )
    assert isinstance(raised.value.__cause__, GmailRateLimited)


def test_history_pages_halve_per_failed_throttled_run_down_to_one():
    size = EmailProcessor._history_page_size
    assert [size("PROVIDER_RATE_LIMITED", failures) for failures in range(9)] == [100, 50, 25, 12, 6, 3, 1, 1, 1]
    # A throttled run that saved its place resets the count: full pages again.
    assert size("PROVIDER_RATE_LIMITED", 0) == 100
    # Other failures do not shrink them.
    assert size("PROVIDER_TIMEOUT", 5) == 100
    assert size(None, 0) == 100


def _http_error(status: int, url: str) -> httpx.HTTPStatusError:
    response = httpx.Response(status, request=httpx.Request("GET", url))
    with pytest.raises(httpx.HTTPStatusError) as raised:
        response.raise_for_status()
    return raised.value


MESSAGE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/18c4291fa0b1c2d3?format=raw"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (GmailRateLimited(30), "PROVIDER_RATE_LIMITED"),
        (_http_error(429, MESSAGE_URL), "PROVIDER_RATE_LIMITED"),
        # The URL holds "429", in the message id: a 500 is still a 500.
        (_http_error(500, MESSAGE_URL), "PROVIDER_SYNC_FAILED"),
        (_http_error(401, MESSAGE_URL), "ACCOUNT_AUTH_FAILED"),
        (_http_error(504, MESSAGE_URL), "PROVIDER_TIMEOUT"),
        (_http_error(403, MESSAGE_URL), "PROVIDER_SYNC_FAILED"),
        # Text from other providers is still read as before.
        (RuntimeError("LOGIN failed"), "ACCOUNT_AUTH_FAILED"),
        (RuntimeError("rate limit exceeded"), "PROVIDER_RATE_LIMITED"),
        (RuntimeError("connection timed out"), "PROVIDER_TIMEOUT"),
    ],
)
def test_a_sync_error_is_classified_by_what_it_is(error, code):
    assert EmailProcessor._sync_error(error)[0] == code


def test_the_run_stops_waiting_well_before_its_job_deadline():
    processor = EmailProcessor()
    processor.job_deadline = 5000.0
    # Longer than one request may take (httpx's 30 s timeout), with room to commit.
    assert EmailProcessor.GMAIL_THROTTLE_MARGIN_SECONDS >= 45
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


# ---- tool calls


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


class _Db:
    def __init__(self):
        self.deleted = []
        self.commits = 0

    def delete(self, row):
        self.deleted.append(row)

    def commit(self):
        self.commits += 1


def _gmail_messages():
    return [SimpleNamespace(id=n, provider_message_id=f"g{n}", flags='["INBOX"]', deleted_at=None) for n in (1, 2, 3)]


@pytest.mark.asyncio
async def test_a_throttled_relabel_records_what_gmail_applied(monkeypatch):
    from src.services import write_service

    recorded = []
    monkeypatch.setattr(write_service, "_record_gmail_labels", lambda db, message, labels: recorded.append(message.id))

    class Client:
        async def modify_labels(self, message_id, *, add, remove):
            if message_id == "g2":
                raise GmailRateLimited(9)
            return sorted(["INBOX", *add])

    async def fake_gmail(db, account, operation):
        return await operation(Client())

    monkeypatch.setattr(write_service, "_gmail", fake_gmail)
    messages = _gmail_messages()
    selection = SimpleNamespace(account=SimpleNamespace(id=5), targets=[(m, None) for m in messages], skipped=[])
    db = _Db()
    with pytest.raises(HTTPException) as raised:
        await write_service._gmail_relabel(
            db, selection, add_for=lambda labels: ["STARRED"], remove_for=lambda labels: []
        )

    assert sorted(recorded) == [1, 3]
    assert db.commits == 1
    assert raised.value.status_code == 429
    assert raised.value.detail["retry_after"] == 9
    assert "2 of 3 messages now have the requested labels." in raised.value.detail["message"]


@pytest.mark.asyncio
async def test_a_throttled_permanent_delete_records_what_gmail_removed(monkeypatch):
    from src.services import write_service

    messages = _gmail_messages()
    placements = {m.id: SimpleNamespace(folder="[Gmail]/Trash") for m in messages}
    selection = SimpleNamespace(
        account=SimpleNamespace(id=5, provider="gmail", auth_type="oauth2"),
        targets=[(m, placements[m.id]) for m in messages],
        skipped=[],
        matched=3,
    )
    monkeypatch.setattr(write_service, "_rate_limit", lambda user_id: None)
    monkeypatch.setattr(write_service, "_select", lambda db, user, ids, filters, limit: selection)

    class Client:
        async def get_labels(self, message_id):
            return ["TRASH"]

        async def delete_message_permanently(self, message_id):
            if message_id == "g2":
                raise GmailRateLimited(20)

    async def fake_gmail(db, account, operation):
        return await operation(Client())

    monkeypatch.setattr(write_service, "_gmail", fake_gmail)
    db = _Db()
    with pytest.raises(HTTPException) as raised:
        await write_service.delete_mail(db, SimpleNamespace(id=1), permanent=True, email_ids=[1, 2, 3])

    assert db.deleted == [placements[1], placements[3]]
    assert [m.deleted_at is not None for m in messages] == [True, False, True]
    assert db.commits == 1
    assert raised.value.status_code == 429
    assert "2 of 3 messages were deleted permanently." in raised.value.detail["message"]


@pytest.mark.asyncio
async def test_a_permanent_delete_leaves_a_message_restored_in_gmail_alone(monkeypatch):
    from src.services import write_service

    messages = _gmail_messages()
    placements = {m.id: SimpleNamespace(folder="TRASH") for m in messages}
    selection = SimpleNamespace(
        account=SimpleNamespace(id=5, provider="gmail", auth_type="oauth2"),
        targets=[(m, placements[m.id]) for m in messages],
        skipped=[],
        matched=3,
    )
    monkeypatch.setattr(write_service, "_rate_limit", lambda user_id: None)
    monkeypatch.setattr(write_service, "_select", lambda db, user, ids, filters, limit: selection)
    recorded = []
    monkeypatch.setattr(
        write_service, "_record_gmail_labels", lambda db, message, labels: recorded.append((message.id, labels))
    )
    # The index says all three are in the Bin. In Gmail, g1 was restored to the
    # Inbox since, g2 is gone already, and only g3 is still in the Bin.
    now_in_gmail = {"g1": ["INBOX"], "g2": None, "g3": ["TRASH"]}
    deleted = []

    class Client:
        async def get_labels(self, message_id):
            return now_in_gmail[message_id]

        async def delete_message_permanently(self, message_id):
            deleted.append(message_id)

    async def fake_gmail(db, account, operation):
        return await operation(Client())

    monkeypatch.setattr(write_service, "_gmail", fake_gmail)
    db = _Db()
    result = await write_service.delete_mail(db, SimpleNamespace(id=1), permanent=True, email_ids=[1, 2, 3])

    assert deleted == ["g3"]
    assert recorded == [(1, ["INBOX"])]
    assert db.deleted == [placements[2], placements[3]]
    assert [m.deleted_at is not None for m in messages] == [False, True, True]
    assert result["affected"] == 2
    assert result["skipped"] == 1
    assert result["skipped_reasons"][0]["email_ids"] == [1]
