"""Sending through Microsoft Graph: what leaves, and what each answer means for a retry."""

import base64
import json
from email import message_from_bytes, policy
from types import SimpleNamespace

import httpx
import pytest

from src.email import graph_sender
from src.email.graph_sender import GraphMailSender, uses_graph
from src.security.provider_tokens import ProviderAuthError
from src.services.upload_service import delivery_outcome


def account(**overrides):
    values = {
        "id": 11,
        "name": "contoso.com",
        "provider": "microsoft",
        "auth_type": "oauth2",
        "account_name": "owner@contoso.com",
        "username": "owner@contoso.com",
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "credential_ciphertext": "enc:v1:unused",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class Graph:
    """A stand-in for graph.microsoft.com that records what it was sent."""

    def __init__(self, *answers):
        self.requests = []
        self.answers = list(answers) or [httpx.Response(202)]

    def client(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
            if isinstance(answer, Exception):
                raise answer
            return answer

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    @property
    def sent(self):
        """The MIME of the last request, decoded the way Graph decodes it."""
        return message_from_bytes(base64.b64decode(self.requests[-1].content), policy=policy.default)


@pytest.fixture
def tokens(monkeypatch):
    issued = []

    def token(config, purpose):
        issued.append(purpose)
        return f"graph-token-{len(issued)}"

    monkeypatch.setattr(graph_sender, "access_token_for", token)
    return issued


async def send(graph, **kwargs):
    sender = GraphMailSender(account(), http_client=graph.client())
    kwargs.setdefault("to_addresses", ["to@example.com"])
    kwargs.setdefault("subject", "Hello")
    kwargs.setdefault("body_text", "Body")
    return await sender.send_email(**kwargs)


@pytest.mark.asyncio
async def test_the_message_goes_out_as_base64_mime_with_the_bearer_token(tokens):
    graph = Graph()

    result = await send(graph, body_html="<p>Body</p>", cc_addresses=["cc@example.com"])

    request = graph.requests[0]
    assert str(request.url) == "https://graph.microsoft.com/v1.0/me/sendMail"
    assert request.headers["authorization"] == "Bearer graph-token-1"
    # Graph's raw-MIME form: the message itself as base64 text, not a JSON document.
    assert request.headers["content-type"] == "text/plain"
    assert tokens == ["send"]
    assert graph.sent["From"] == "owner@contoso.com"
    assert graph.sent["To"] == "to@example.com"
    assert graph.sent["Cc"] == "cc@example.com"
    assert graph.sent["Subject"] == "Hello"
    assert result["success"] is True
    assert result["delivery_state"] == "sent"
    assert result["transport"] == "microsoft_graph"
    assert result["recipients"] == 2
    # Exchange keeps the Message-ID it is handed, which is what lets the sent
    # copy that sync indexes later be recognised as this message.
    assert result["message_id"] == graph.sent["Message-ID"]


@pytest.mark.asyncio
async def test_bcc_is_written_into_the_message_because_graph_has_no_envelope(tokens):
    graph = Graph()

    result = await send(graph, bcc_addresses=["hidden@example.com", "also@example.com"])

    assert graph.sent["Bcc"] == "hidden@example.com, also@example.com"
    assert result["recipients"] == 3


@pytest.mark.asyncio
async def test_no_bcc_header_is_invented_when_there_are_no_bcc_recipients(tokens):
    graph = Graph()

    await send(graph)

    assert graph.sent["Bcc"] is None


@pytest.mark.asyncio
async def test_attachments_and_inline_images_survive_into_the_mime(tokens):
    graph = Graph()
    png = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 32

    await send(
        graph,
        body_html='<p><img src="cid:logo@mailindex.invalid"></p>',
        attachments=[
            {"filename": "report.pdf", "data": b"%PDF-1.4 \x00\xff", "content_type": "application/pdf"},
            {
                "filename": "logo.png",
                "data": png,
                "content_type": "image/png",
                "disposition": "inline",
                "content_id": "logo@mailindex.invalid",
            },
        ],
    )

    leaves = {
        part.get_filename(): (part.get_content_type(), part.get_content_disposition(), part["Content-ID"])
        for part in graph.sent.walk()
        if part.get_filename()
    }
    assert leaves["report.pdf"] == ("application/pdf", "attachment", None)
    assert leaves["logo.png"] == ("image/png", "inline", "<logo@mailindex.invalid>")
    pdf = next(part for part in graph.sent.walk() if part.get_filename() == "report.pdf")
    assert pdf.get_payload(decode=True) == b"%PDF-1.4 \x00\xff"


@pytest.mark.asyncio
async def test_non_ascii_text_round_trips(tokens):
    graph = Graph()

    await send(graph, subject="Grüße — Überweisung €", body_text="Umlaute: äöü ß")

    assert graph.sent["Subject"] == "Grüße — Überweisung €"
    assert graph.sent.get_body(("plain",)).get_content().strip() == "Umlaute: äöü ß"


# ---- what each answer means ------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 403, 404, 413])
@pytest.mark.asyncio
async def test_a_refusal_is_a_definite_non_send_with_microsofts_reason(tokens, status):
    graph = Graph(
        httpx.Response(
            status,
            json={"error": {"code": "ErrorSendAsDenied", "message": "The user may not send as owner@contoso.com."}},
        )
    )

    result = await send(graph)

    assert result["success"] is False
    # 'failed' hands the caller's upload slots back: nothing was sent.
    assert result["delivery_state"] == "failed"
    assert delivery_outcome(result) == "failed"
    assert "ErrorSendAsDenied" in result["message"]
    assert "may not send as" in result["message"]
    assert len(graph.requests) == 1, "a refusal is never retried"


@pytest.mark.asyncio
async def test_throttling_is_a_definite_non_send_that_says_when_to_retry(tokens):
    graph = Graph(httpx.Response(429, headers={"Retry-After": "17"}, json={"error": {"code": "TooManyRequests"}}))

    result = await send(graph)

    assert result["delivery_state"] == "failed"
    assert "retry after 17 seconds" in result["message"]


@pytest.mark.parametrize("status", [500, 502, 503, 504, 408])
@pytest.mark.asyncio
async def test_a_server_side_failure_is_ambiguous_and_is_never_retried(tokens, status):
    """Exchange may have accepted the message before the gateway failed, so a retry could send it twice."""
    graph = Graph(httpx.Response(status, json={"error": {"code": "ServiceUnavailable"}}))

    result = await send(graph)

    assert result["success"] is False
    assert result["delivery_state"] == "unknown"
    assert delivery_outcome(result) == "unknown"
    assert len(graph.requests) == 1


@pytest.mark.asyncio
async def test_no_answer_after_the_request_left_is_ambiguous(tokens):
    graph = Graph(httpx.ReadTimeout("no response"))

    result = await send(graph)

    assert result["delivery_state"] == "unknown"
    assert len(graph.requests) == 1


@pytest.mark.asyncio
async def test_failing_to_connect_at_all_proves_nothing_was_sent(tokens):
    graph = Graph(httpx.ConnectError("no route to graph.microsoft.com"))

    result = await send(graph)

    assert result["delivery_state"] == "failed"


@pytest.mark.asyncio
async def test_an_expired_token_is_renewed_once_and_the_send_goes_through(tokens):
    graph = Graph(httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}}), httpx.Response(202))

    result = await send(graph)

    assert result["success"] is True
    assert [request.headers["authorization"] for request in graph.requests] == [
        "Bearer graph-token-1",
        "Bearer graph-token-2",
    ]


@pytest.mark.asyncio
async def test_a_second_401_is_a_refusal_not_a_loop(tokens):
    graph = Graph(httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}}))

    result = await send(graph)

    assert result["delivery_state"] == "failed"
    assert len(graph.requests) == 2


@pytest.mark.asyncio
async def test_a_refused_sign_in_stops_before_anything_is_sent(monkeypatch):
    def refused(config, purpose):
        raise ProviderAuthError("Microsoft authentication refused the stored sign-in (invalid_grant)")

    monkeypatch.setattr(graph_sender, "access_token_for", refused)
    graph = Graph()

    result = await send(graph)

    assert result["delivery_state"] == "failed"
    assert "invalid_grant" in result["message"]
    assert graph.requests == []


@pytest.mark.asyncio
async def test_an_attachment_that_cannot_be_rendered_is_reported_exactly_as_smtp_reports_it(tokens, monkeypatch):
    from src.email.smtp_sender import MIMEBase

    original = MIMEBase.add_header

    def reject(self, name, *args, **kwargs):
        if name == "Content-Disposition":
            raise ValueError("invalid attachment header")
        return original(self, name, *args, **kwargs)

    monkeypatch.setattr("src.email.smtp_sender.MIMEBase.add_header", reject)
    graph = Graph()

    result = await send(graph, attachments=[{"filename": "report.pdf", "data": b"%PDF"}])

    assert result == {
        "success": False,
        "message": "Failed to construct an outbound attachment",
        "delivery_state": "failed",
    }
    assert graph.requests == []


# ---- connection test and routing -------------------------------------------------------------------------


def jwt(claims):
    def segment(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{segment({'alg': 'none'})}.{segment(claims)}.signature"


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        (jwt({"scp": "Mail.Send User.Read"}), True),
        (jwt({"scp": "User.Read"}), False),
        ("EwB4A8l6BAAUO9chh8cJscQLmU", True),  # personal accounts get opaque tokens that say nothing
    ],
)
@pytest.mark.asyncio
async def test_the_connection_test_checks_the_consent_it_can_see(monkeypatch, token, expected):
    monkeypatch.setattr(graph_sender, "access_token_for", lambda config, purpose: token)

    assert await GraphMailSender(account()).connect() is expected


@pytest.mark.asyncio
async def test_the_connection_test_fails_when_the_token_cannot_be_obtained(monkeypatch):
    def refused(config, purpose):
        raise ProviderAuthError("refused")

    monkeypatch.setattr(graph_sender, "access_token_for", refused)

    assert await GraphMailSender(account()).connect() is False


def test_only_oauth_microsoft_accounts_send_through_graph():
    assert uses_graph(account()) is True
    assert uses_graph(account(provider="gmail")) is False
    # An Exchange server reached with a password is an ordinary IMAP/SMTP account.
    assert uses_graph(account(auth_type="password")) is False
    assert uses_graph(SimpleNamespace(id=1)) is False


@pytest.mark.asyncio
async def test_the_sender_manager_picks_the_transport_by_account():
    from src.email.smtp_sender import EmailSender, EmailSenderManager

    def smtp_view(**overrides):
        return account(smtp_host="smtp.example.com", smtp_port=587, **overrides)

    manager = EmailSenderManager()

    assert isinstance(await manager.get_sender(account()), GraphMailSender)
    assert isinstance(await manager.get_sender(smtp_view(id=12, provider="gmail")), EmailSender)
    # The same account changing transport gets the right sender, not the cached one.
    assert isinstance(await manager.get_sender(smtp_view(id=11, provider="imap", auth_type="password")), EmailSender)
    assert isinstance(await manager.get_sender(account()), GraphMailSender)
