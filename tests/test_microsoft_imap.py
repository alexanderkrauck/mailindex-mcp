"""A Microsoft mailbox is read over IMAP with an OAuth token, and says so when that stops working."""

import asyncio
import base64
import contextlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.security.provider_tokens import ProviderAuthError


def microsoft_imap_config(**overrides):
    values = {
        "id": 11,
        "name": "contoso.com",
        "provider": "microsoft",
        "auth_type": "oauth2",
        "host": "outlook.office365.com",
        "port": 993,
        "username": "owner@contoso.com",
        "credential_ciphertext": "enc:v1:unused",
        "imap_use_ssl": True,
        "imap_use_tls": False,
        "sync_cursors": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_imap_sign_in_uses_the_exchange_token_over_xoauth2(monkeypatch):
    from src.email.smtp_client import SMTPClient

    purposes = []
    monkeypatch.setattr(
        "src.security.provider_tokens.access_token_for",
        lambda config, purpose: purposes.append(purpose) or "exchange-access-token",
    )
    imap = AsyncMock()
    imap.xoauth2.return_value = SimpleNamespace(result="OK", lines=[])

    client = SMTPClient(microsoft_imap_config())
    with patch("src.email.smtp_client.aioimaplib.IMAP4_SSL", return_value=imap):
        assert await client.connect() is True

    # The IMAP token and the sending token are different resources; asking for
    # the wrong one fails at the server with an opaque authentication error.
    assert purposes == ["imap"]
    imap.xoauth2.assert_awaited_once_with("owner@contoso.com", "exchange-access-token")
    imap.login.assert_not_called()


@pytest.mark.asyncio
async def test_a_revoked_sign_in_is_reported_as_an_authentication_failure_with_the_remedy(monkeypatch):
    from src.email.email_processor import EmailProcessor
    from src.email.smtp_client import SMTPClient

    def refused(config, purpose):
        raise ProviderAuthError(
            "Microsoft authentication refused the stored sign-in (invalid_grant: AADSTS70008). "
            "Connect the mailbox again with begin_microsoft_connection."
        )

    monkeypatch.setattr("src.security.provider_tokens.access_token_for", refused)
    client = SMTPClient(microsoft_imap_config())
    with patch("src.email.smtp_client.aioimaplib.IMAP4_SSL", return_value=AsyncMock()):
        assert await client.connect() is False
        with pytest.raises(ConnectionError) as raised:
            async for _batch in client.fetch_new_emails(limit=1):
                pass

    code, message = EmailProcessor._sync_error(raised.value)
    # The mailbox needs the owner, not a retry: that is what the status must say.
    assert code == "ACCOUNT_AUTH_FAILED"
    assert "begin_microsoft_connection" in message
    assert "contoso.com" in message


@pytest.mark.asyncio
async def test_an_ordinary_connection_failure_does_not_pretend_to_know_why(monkeypatch):
    from src.email.smtp_client import SMTPClient

    def unreachable(config, purpose):
        raise RuntimeError("Microsoft sign-in service unreachable (ConnectError)")

    monkeypatch.setattr("src.security.provider_tokens.access_token_for", unreachable)
    client = SMTPClient(microsoft_imap_config())
    with patch("src.email.smtp_client.aioimaplib.IMAP4_SSL", return_value=AsyncMock()):
        assert await client.connect() is False
        with pytest.raises(ConnectionError) as raised:
            async for _batch in client.fetch_new_emails(limit=1):
                pass

    # An outage is retried by the scheduler; sending the owner to reconnect
    # would be wrong, so only a refused sign-in gets the extra text.
    assert str(raised.value) == "Could not connect to contoso.com"


@asynccontextmanager
async def loopback_imap(respond):
    """A real IMAP conversation over loopback, answering each command as `respond` says.

    The library's behaviour is part of what is under test, so nothing here
    mocks aioimaplib: ``respond(command, tag, rest, line)`` returns the bytes
    to send back, or None for the generic "OK".
    """
    writers = set()

    async def serve(reader, writer):
        writers.add(writer)
        try:
            writer.write(b"* OK loopback IMAP ready\r\n")
            await writer.drain()
            while line := await reader.readline():
                tag, command, *rest = line.split()
                if command == b"CAPABILITY":
                    writer.write(b"* CAPABILITY IMAP4rev1 AUTH=XOAUTH2\r\n" + tag + b" OK done\r\n")
                elif command == b"LOGOUT":
                    writer.write(b"* BYE closing\r\n" + tag + b" OK logged out\r\n")
                    await writer.drain()
                    return
                else:
                    writer.write(respond(command, tag, rest, line) or tag + b" OK done\r\n")
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            writers.discard(writer)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        for writer in tuple(writers):
            writer.close()
        await server.wait_closed()


@pytest.fixture
def plaintext_loopback(monkeypatch):
    """Use the real protocol over loopback, omitting TLS only at this test seam."""
    from src.email import smtp_client

    plain = smtp_client.aioimaplib.IMAP4
    monkeypatch.setattr(
        smtp_client.aioimaplib,
        "IMAP4_SSL",
        lambda **kwargs: plain(**{k: v for k, v in kwargs.items() if k != "ssl_context"}),
    )
    monkeypatch.setattr(
        "src.config.settings", SimpleNamespace(imap_command_timeout_seconds=2, imap_cleanup_timeout_seconds=0.5)
    )
    monkeypatch.setattr("src.security.provider_tokens.access_token_for", lambda config, purpose: "the-exchange-token")


@pytest.mark.asyncio
async def test_the_sasl_string_on_the_wire_carries_the_bare_token(plaintext_loopback):
    """Through the real protocol, because the wrapper's signature and its behaviour disagree.

    aioimaplib annotates xoauth2's token as bytes but interpolates it into an
    f-string and calls .encode() on it for logging, so bytes both corrupt the
    bearer value ("b'...'") and crash. Only a real exchange shows that.
    """
    from src.email.smtp_client import SMTPClient

    seen = {}

    def respond(command, tag, rest, line):
        if command == b"AUTHENTICATE":
            seen["mechanism"] = rest[0]
            seen["sasl"] = base64.b64decode(rest[1])
            return tag + b" OK AUTHENTICATE completed\r\n"

    async with loopback_imap(respond) as port:
        client = SMTPClient(microsoft_imap_config(host="127.0.0.1", port=port))
        try:
            assert await client.connect() is True
        finally:
            await client.disconnect()

    assert seen["mechanism"] == b"XOAUTH2"
    assert seen["sasl"] == b"user=owner@contoso.com\x01auth=Bearer the-exchange-token\x01\x01"


@pytest.mark.asyncio
async def test_a_server_that_refuses_a_search_charset_is_searched_without_one(plaintext_loopback):
    """Exchange Online: `SEARCH CHARSET utf-8 ALL` -> NO [BADCHARSET (US-ASCII)], `SEARCH ALL` -> the UIDs.

    Before this, every folder of a Microsoft mailbox failed with "Search failed"
    and nothing was ever synchronised.
    """
    from src.email.smtp_client import SMTPClient

    searches = []

    def respond(command, tag, rest, line):
        if command == b"AUTHENTICATE":
            return tag + b" OK AUTHENTICATE completed\r\n"
        if command == b"SELECT":
            return b"* 3 EXISTS\r\n" + tag + b" OK [READ-WRITE] SELECT completed.\r\n"
        if command == b"SEARCH":
            searches.append(line.strip())
            if b"CHARSET" in line.upper():
                return tag + b" NO [BADCHARSET (US-ASCII)] The specified charset is not supported.\r\n"
            return b"* SEARCH 1 2 3\r\n" + tag + b" OK SEARCH completed.\r\n"

    async with loopback_imap(respond) as port:
        client = SMTPClient(microsoft_imap_config(host="127.0.0.1", port=port))
        try:
            assert await client.connect() is True
            await client.client.select("INBOX")

            first = await client.search("ALL")
            second = await client.search("UID", "2:*")
        finally:
            await client.disconnect()

    assert first.result == "OK" and first.lines[0].split() == [b"1", b"2", b"3"]
    assert second.result == "OK"
    # One refused attempt, then the server is remembered: the later search
    # does not pay for the refusal again.
    assert [search.split()[2:] for search in searches] == [
        [b"CHARSET", b"utf-8", b"ALL"],
        [b"ALL"],
        [b"UID", b"2:*"],
    ]


@pytest.mark.asyncio
async def test_a_server_that_takes_utf8_is_never_asked_differently(plaintext_loopback):
    from src.email.smtp_client import SMTPClient

    searches = []

    def respond(command, tag, rest, line):
        if command == b"AUTHENTICATE":
            return tag + b" OK AUTHENTICATE completed\r\n"
        if command == b"SELECT":
            return b"* 1 EXISTS\r\n" + tag + b" OK [READ-WRITE] SELECT completed.\r\n"
        if command == b"SEARCH":
            searches.append(line.strip())
            return b"* SEARCH 1\r\n" + tag + b" OK SEARCH completed.\r\n"

    async with loopback_imap(respond) as port:
        client = SMTPClient(microsoft_imap_config(host="127.0.0.1", port=port))
        try:
            assert await client.connect() is True
            await client.client.select("INBOX")
            await client.search("ALL")
            await client.search("ALL")
        finally:
            await client.disconnect()

    assert all(b"CHARSET" in search.upper() for search in searches) and len(searches) == 2
    assert client._ascii_search is False


@pytest.mark.asyncio
async def test_any_other_refusal_of_a_search_is_reported_not_retried(plaintext_loopback):
    from src.email.smtp_client import SMTPClient

    searches = []

    def respond(command, tag, rest, line):
        if command == b"AUTHENTICATE":
            return tag + b" OK AUTHENTICATE completed\r\n"
        if command == b"SELECT":
            return b"* 1 EXISTS\r\n" + tag + b" OK [READ-WRITE] SELECT completed.\r\n"
        if command == b"SEARCH":
            searches.append(line)
            return tag + b" NO [SERVERBUG] try later\r\n"

    async with loopback_imap(respond) as port:
        client = SMTPClient(microsoft_imap_config(host="127.0.0.1", port=port))
        try:
            assert await client.connect() is True
            await client.client.select("INBOX")
            response = await client.search("ALL")
        finally:
            await client.disconnect()

    assert response.result == "NO"
    assert len(searches) == 1
