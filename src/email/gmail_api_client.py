"""Small Gmail REST client for resumable mailbox synchronization."""

import asyncio
import base64
import json
import logging
import math
import time
from datetime import datetime, timezone

import httpx

from src.email.gmail_labels import location_for
from src.email.message_dates import parse_header_date
from src.email.smtp_client import SMTPClient
from src.security.provider_tokens import refresh_access_token

logger = logging.getLogger(__name__)

# Gmail answers per-user quota exhaustion with 403 and one of these reasons (and
# sometimes with 429). Any other 403 -- insufficientPermissions, a domain policy --
# is final and must not be retried.
THROTTLE_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})


class GmailHistoryExpired(RuntimeError):
    """The saved Gmail history ID is no longer available."""


class GmailRateLimited(RuntimeError):
    """Gmail kept throttling this mailbox for longer than the caller can wait.

    ``fetched_messages`` counts the messages this client did fetch before giving
    up, so a synchronization can tell a slow run from one that made no progress.
    """

    def __init__(self, retry_after: float, *, fetched_messages: int = 0):
        self.retry_after = max(1.0, float(retry_after))
        self.fetched_messages = fetched_messages
        super().__init__(f"Gmail rate limit: retry in {math.ceil(self.retry_after)}s")


class GmailApiClient:
    BASE_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
    TRANSIENT_STATUSES = {500, 502, 503, 504}
    # After a throttle, every request of this client waits behind one shared gate,
    # backing off further on each fresh throttle, and from then on requests start
    # one at a time at this spacing. A mailbox's per-user quota is enforced over
    # seconds, so ten parallel fetches trip it where paced ones do not.
    THROTTLE_BACKOFF_SECONDS = (5.0, 10.0, 20.0, 40.0, 60.0)
    PACED_INTERVAL_SECONDS = 0.25
    MIN_THROTTLE_PAUSE_SECONDS = 1.0
    MAX_RETRY_AFTER_SECONDS = 300.0
    # How long a caller without a deadline of its own (a tool call) may wait.
    INTERACTIVE_THROTTLE_BUDGET_SECONDS = 15.0

    def __init__(
        self,
        config,
        *,
        http_client: httpx.AsyncClient | None = None,
        access_token: str | None = None,
        throttle_deadline: float | None = None,
        clock=time.monotonic,
        sleep=asyncio.sleep,
    ):
        self.config = config
        self._client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self._owns_client = http_client is None
        self._access_token = access_token
        self._parser = SMTPClient(config)
        self._clock = clock
        self._sleep = sleep
        # A monotonic time no throttle wait may run past; then GmailRateLimited.
        self._throttle_deadline = (
            throttle_deadline
            if throttle_deadline is not None
            else clock() + self.INTERACTIVE_THROTTLE_BUDGET_SECONDS
        )
        self._gate_until = 0.0
        self._last_throttle_at = float("-inf")
        self._throttle_streak = 0
        self._paced = False
        self._pace_lock = asyncio.Lock()
        self._next_slot = 0.0
        self.fetched_messages = 0
        self.throttle_pauses: list[float] = []

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _token(self) -> str:
        if not self._access_token:
            self._access_token = await asyncio.to_thread(
                refresh_access_token, self.config.credential_ciphertext
            )
        return self._access_token

    @staticmethod
    def _is_throttle(response: httpx.Response) -> bool:
        if response.status_code == 429:
            return True
        if response.status_code != 403:
            return False
        try:
            body = response.json()
        except ValueError:
            return False
        error = body.get("error") if isinstance(body, dict) else None
        errors = error.get("errors") if isinstance(error, dict) else None
        return isinstance(errors, list) and any(
            isinstance(item, dict) and item.get("reason") in THROTTLE_REASONS for item in errors
        )

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        try:
            seconds = float(value) if value else None
        except ValueError:
            return None
        return seconds if seconds is not None and math.isfinite(seconds) and seconds >= 0 else None

    def _rate_limited(self, now: float) -> GmailRateLimited:
        return GmailRateLimited(self._gate_until - now, fetched_messages=self.fetched_messages)

    async def _admit(self) -> None:
        """Wait until this client may send: past the throttle gate, and paced once throttled."""
        while True:
            now = self._clock()
            if self._gate_until > now:
                if self._gate_until > self._throttle_deadline:
                    raise self._rate_limited(now)
                await self._sleep(self._gate_until - now)
                continue
            if not self._paced:
                return
            async with self._pace_lock:
                now = self._clock()
                if self._gate_until > now:
                    continue
                wait = self._next_slot - now
                if wait > 0:
                    await self._sleep(wait)
                    continue
                self._next_slot = now + self.PACED_INTERVAL_SECONDS
                return

    def _note_throttle(self, response: httpx.Response, sent_at: float) -> None:
        """Close the gate for a fresh throttle; raise when the wait outlasts the deadline."""
        now = self._clock()
        self._paced = True
        # Requests already in flight when a throttle was noted answer with their
        # own throttles; they belong to the same burst and must not escalate.
        if sent_at >= self._last_throttle_at:
            delay = self._retry_after(response)
            if delay is None:
                delay = self.THROTTLE_BACKOFF_SECONDS[
                    min(self._throttle_streak, len(self.THROTTLE_BACKOFF_SECONDS) - 1)
                ]
            # A floor, so a Retry-After of 0 cannot turn into a tight retry loop.
            delay = min(max(delay, self.MIN_THROTTLE_PAUSE_SECONDS), self.MAX_RETRY_AFTER_SECONDS)
            self._throttle_streak += 1
            self._last_throttle_at = now
            self._gate_until = max(self._gate_until, now + delay)
            self.throttle_pauses.append(delay)
            logger.warning(
                "Gmail is rate-limiting account %s; pausing its requests for %.0fs",
                getattr(self.config, "id", "?"),
                self._gate_until - now,
            )
        if self._gate_until > self._throttle_deadline:
            raise self._rate_limited(now)

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        payload: dict | None = None,
    ) -> httpx.Response:
        """One Gmail call with token refresh, throttle backoff and transient retries."""
        url = f"{self.BASE_URL}/{path.lstrip('/')}"
        refreshed = False
        transient_attempts = 0
        while True:
            await self._admit()
            headers = {"Authorization": f"Bearer {await self._token()}"}
            sent_at = self._clock()
            if method == "GET":
                response = await self._client.get(url, params=params, headers=headers)
            else:
                response = await self._client.post(
                    url, json=payload if payload is not None else {}, headers=headers
                )
            if response.status_code == 401 and not refreshed:
                refreshed = True
                self._access_token = None
                continue
            if self._is_throttle(response):
                self._note_throttle(response, sent_at)
                continue
            if response.status_code in self.TRANSIENT_STATUSES and transient_attempts < 2:
                retry_after = self._retry_after(response)
                delay = min(retry_after, 10.0) if retry_after is not None else 2**transient_attempts
                transient_attempts += 1
                await self._sleep(delay)
                continue
            if response.is_success:
                self._throttle_streak = 0
            return response

    async def _request(
        self,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        allow_missing: bool = False,
        history_request: bool = False,
    ) -> dict | None:
        response = await self._send("GET", path, params=params)
        if response.status_code == 404:
            if history_request:
                raise GmailHistoryExpired("Gmail history ID expired or is invalid")
            if allow_missing:
                return None
        response.raise_for_status()
        return response.json()

    async def get_profile(self) -> dict:
        return await self._request("profile") or {}

    async def list_messages(self, *, page_token: str | None = None, max_results: int = 100) -> dict:
        params: dict[str, str | int] = {
            "maxResults": max_results,
            "includeSpamTrash": "true",
        }
        if page_token:
            params["pageToken"] = page_token
        return await self._request("messages", params=params) or {}

    async def list_history(
        self, start_history_id: str, *, page_token: str | None = None
    ) -> dict:
        params = {"startHistoryId": start_history_id, "maxResults": 500}
        if page_token:
            params["pageToken"] = page_token
        return (
            await self._request("history", params=params, history_request=True)
            or {}
        )

    async def _post(self, path: str, payload: dict | None = None) -> dict | None:
        """POST with the same auth refresh and backoff the read path uses.

        Retrying a throttled POST is safe: Gmail refuses it before doing anything.
        """
        response = await self._send("POST", path, payload=payload)
        response.raise_for_status()
        # trash and delete answer 204 with no body.
        return response.json() if response.content else None

    async def modify_labels(
        self, message_id: str, *, add: list[str], remove: list[str]
    ) -> list[str]:
        """Apply label changes and return the labels the message now carries.

        messages.modify rather than batchModify: modify answers with the full
        updated resource, so the new state is server-confirmed in one round trip.
        batchModify answers 204, needing a second read to learn what happened,
        and costs more quota for the pair than modify does per message.
        """
        payload = {}
        if add:
            payload["addLabelIds"] = add
        if remove:
            payload["removeLabelIds"] = remove
        if not payload:
            return []
        result = await self._post(f"messages/{message_id}/modify", payload) or {}
        return sorted(str(label) for label in result.get("labelIds", []))

    async def trash_message(self, message_id: str) -> list[str]:
        """Move a message to Gmail's Bin."""
        result = await self._post(f"messages/{message_id}/trash") or {}
        return sorted(str(label) for label in result.get("labelIds", []))

    async def delete_message_permanently(self, message_id: str) -> None:
        """Remove a message outright. There is no undo for this on Gmail's side."""
        response = await self._client.post(
            f"{self.BASE_URL}/messages/{message_id}/delete",
            headers={"Authorization": f"Bearer {await self._token()}"},
        )
        if response.status_code == 405:
            response = await self._client.delete(
                f"{self.BASE_URL}/messages/{message_id}",
                headers={"Authorization": f"Bearer {await self._token()}"},
            )
        response.raise_for_status()

    async def create_draft(self, raw: bytes, *, thread_id: str | None = None) -> dict | None:
        """Store a draft, from the same RFC 5322 bytes the IMAP path appends."""
        message: dict = {"raw": base64.urlsafe_b64encode(raw).decode("ascii")}
        if thread_id:
            message["threadId"] = thread_id
        return await self._post("drafts", {"message": message})

    async def list_labels(self) -> list[dict]:
        result = await self._request("labels") or {}
        return list(result.get("labels", []))

    async def get_message(self, message_id: str, *, raw: bool = True) -> dict | None:
        params = {"format": "raw" if raw else "metadata"}
        message = await self._request(
            f"messages/{message_id}",
            params=params,
            allow_missing=True,
        )
        if message is not None:
            self.fetched_messages += 1
        return message

    async def get_parsed_message(self, message_id: str) -> dict | None:
        provider_message = await self.get_message(message_id)
        if not provider_message:
            return None
        encoded = provider_message.get("raw")
        if not encoded:
            raise RuntimeError(f"Gmail message {message_id} did not include an RFC822 payload")
        raw_email = self.decode_raw(encoded)
        labels = sorted(provider_message.get("labelIds", []))
        parsed = await self._parser._parse_email(
            raw_email,
            message_id,
            # Gmail has no folders, so a location is projected from the labels.
            # "gmail" told search and every write nothing at all.
            folder=location_for(labels),
            uid_validity=None,
            flags=json.dumps(labels),
        )
        if not parsed:
            return None
        parsed["provider_message_id"] = str(provider_message["id"])
        parsed["provider_thread_id"] = str(provider_message.get("threadId") or "") or None
        parsed["imap_uid"] = None
        parsed["uid_validity"] = None
        if parsed.get("email_date") is None and provider_message.get("internalDate"):
            parsed["email_date"] = datetime.fromtimestamp(
                int(provider_message["internalDate"]) / 1000, tz=timezone.utc
            )
        return parsed

    async def get_raw_message(self, message_id: str) -> bytes | None:
        provider_message = await self.get_message(message_id)
        if not provider_message:
            return None
        encoded = provider_message.get("raw")
        return self.decode_raw(encoded) if encoded else None

    @staticmethod
    def decode_raw(value: str) -> bytes:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(value + padding)

    @staticmethod
    def message_date(provider_message: dict) -> datetime | None:
        """Best-effort date helper for metadata-only callers."""
        internal_date = provider_message.get("internalDate")
        if internal_date:
            return datetime.fromtimestamp(int(internal_date) / 1000, tz=timezone.utc)
        for header in provider_message.get("payload", {}).get("headers", []):
            if header.get("name", "").lower() == "date":
                return parse_header_date(header.get("value", ""))
        return None
