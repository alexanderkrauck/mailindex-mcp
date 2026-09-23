"""A cached SMTP connection the server has already closed.

Every SMTP server drops an idle connection after a few minutes. ``EmailSender``
caches one for the life of the process and, before this module existed, reused
it without ever asking whether it was still there -- so the first send after an
idle period wrote to a dead socket and reported an *ambiguous* outcome, which
retired the caller's upload slot for a message that had demonstrably not been
sent.

These tests drive a real ``aiosmtpd`` server over a real socket rather than
mocking ``smtplib``: the defect is in socket lifetime, and a mock has none.
"""

import datetime
import json
import socket
import ssl
import threading
from email import message_from_bytes
from types import SimpleNamespace

import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import SMTP as AioSMTP
from aiosmtpd.smtp import AuthResult
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from src.config import settings

# --------------------------------------------------------------------------
# A real SMTP server, on a real socket, that can be told how to misbehave
# --------------------------------------------------------------------------


def _self_signed(tmp_path):
    """A certificate for localhost, so STARTTLS is genuine rather than skipped."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=2))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    certificate_path = tmp_path / "smtp-cert.pem"
    key_path = tmp_path / "smtp-key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return certificate_path, key_path


def _free_port() -> int:
    """A port nobody holds. aiosmtpd's readiness probe cannot use port 0."""
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
    finally:
        probe.close()


class _Handler:
    """Accepts mail, and can drop the connection at a chosen point.

    ``drop_at`` names the SMTP verb whose handler closes the transport instead
    of answering. The distinction the fix turns on lives here: ``MAIL`` fires
    on the first command of a transaction, before any message content exists on
    the wire, while ``DATA`` fires once the content has been transferred.
    """

    def __init__(self):
        self.delivered: list[bytes] = []
        self.drop_at: str | None = None
        self.idle_close: threading.Event = threading.Event()

    async def handle_MAIL(self, server, session, envelope, address, mail_options):
        envelope.mail_from = address
        if self.drop_at == "MAIL":
            server.transport.close()
        return "250 OK"

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        envelope.rcpt_tos.append(address)
        if self.drop_at == "RCPT":
            server.transport.close()
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        # Reached only after the client has written the whole message and the
        # terminating dot: by here the bytes are demonstrably at the MTA.
        if self.drop_at == "DATA":
            server.transport.close()
            return "250 Message accepted"
        self.delivered.append(envelope.content)
        return "250 Message accepted"


class _Server:
    """Owns the controller and exposes the knobs a test needs."""

    def __init__(self, controller, port, handler, ca_file):
        self._controller = controller
        self.port = port
        self.handler = handler
        self.ca_file = ca_file
        self.sessions: list = []

    @property
    def delivered(self) -> list[bytes]:
        return self.handler.delivered

    def drop_at(self, verb: str | None) -> None:
        self.handler.drop_at = verb

    def close_every_idle_connection(self) -> None:
        """Do what a real server does to an idle client: hang up on it.

        Closing from the *server* side is the whole point. The client is left
        holding a file descriptor that looks open and is not, which is exactly
        the state a cached connection reaches after a few idle minutes -- and
        is not reproducible by setting a flag on the sender.
        """
        loop = self._controller.loop
        done = threading.Event()

        def close_them():
            for smtp_session in list(self.sessions):
                transport = getattr(smtp_session, "transport", None)
                if transport is not None:
                    transport.abort()
            done.set()

        loop.call_soon_threadsafe(close_them)
        assert done.wait(timeout=10), "the server never closed its connections"

    def stop(self):
        self._controller.stop()


@pytest.fixture
def smtp_server(tmp_path):
    """A real STARTTLS SMTP server on loopback, for the duration of one test."""
    certificate_path, key_path = _self_signed(tmp_path)
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_context.load_cert_chain(str(certificate_path), str(key_path))

    handler = _Handler()
    port = _free_port()
    sessions: list = []

    class _TrackingSMTP(AioSMTP):
        """Keeps a handle on every live session so a test can hang up on it."""

        def connection_made(self, transport):
            super().connection_made(transport)
            sessions.append(self)

        def connection_lost(self, error):
            if self in sessions:
                sessions.remove(self)
            super().connection_lost(error)

    def factory():
        return _TrackingSMTP(
            handler,
            tls_context=tls_context,
            require_starttls=False,
            auth_require_tls=True,
            authenticator=lambda *args, **kwargs: AuthResult(success=True),
        )

    controller = Controller(handler, hostname="127.0.0.1", port=port)
    controller.factory = factory
    controller.start()
    server = _Server(controller, port, handler, certificate_path)
    server.sessions = sessions
    try:
        yield server
    finally:
        controller.stop()


@pytest.fixture
def sender(smtp_server, monkeypatch):
    """An ``EmailSender`` pointed at the in-process server, with real TLS."""
    from src.email import smtp_sender as smtp_sender_module

    # The certificate is self-signed, so the client has to be told to trust it.
    # Everything else about the handshake stays genuine.
    original_default_context = ssl.create_default_context

    def trusting_context(*args, **kwargs):
        kwargs.setdefault("cafile", str(smtp_server.ca_file))
        return original_default_context(*args, **kwargs)

    monkeypatch.setattr(smtp_sender_module.ssl, "create_default_context", trusting_context)

    config = SimpleNamespace(
        id=1,
        name="Goedly Business",
        account_name="owner@example.com",
        username="owner@example.com",
        password="password",
        auth_type="password",
        host="localhost",
        port=smtp_server.port,
        smtp_host="localhost",
        smtp_port=smtp_server.port,
        smtp_use_ssl=False,
        smtp_use_tls=True,
        credential_ciphertext="enc:test",
    )
    return smtp_sender_module.EmailSender(config)


async def _send(sender, subject="Quarterly numbers"):
    return await sender.send_email(
        to_addresses=["recipient@example.com"],
        subject=subject,
        body_text="Body",
    )


# --------------------------------------------------------------------------
# 1. The reported bug: the first send after an idle period
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_send_after_the_server_closed_the_idle_connection_succeeds(
    sender, smtp_server
):
    """The user-reported failure: first send in some time, on a cached socket.

    Send one message so a connection is cached, let the server hang up the way
    it really does when the connection goes idle, and send again. The second
    send must go out -- not report an error the caller cannot honestly retry.
    """
    first = await _send(sender, subject="Before the idle period")
    assert first["success"] is True, first
    assert len(smtp_server.delivered) == 1

    # The server hangs up, as Zoho does after a few idle minutes. The sender
    # still holds what looks to it like a live connection.
    smtp_server.close_every_idle_connection()

    second = await _send(sender, subject="After the idle period")

    assert second["success"] is True, (
        f"the first send after an idle period failed: {second}"
    )
    assert second["delivery_state"] == "sent"
    assert len(smtp_server.delivered) == 2


# --------------------------------------------------------------------------
# 2. A connection that died before any bytes were written is a definite
#    non-send, and must leave the slot usable
# --------------------------------------------------------------------------


@pytest.mark.parametrize("drop_at", ["MAIL", "RCPT"])
@pytest.mark.asyncio
async def test_a_disconnect_before_any_message_bytes_is_failed_not_unknown(
    sender, smtp_server, drop_at
):
    """Nothing was transmitted, so the outcome is not ambiguous.

    The server drops the connection on MAIL FROM or RCPT TO. smtplib runs a
    transaction as MAIL FROM -> RCPT TO -> DATA, and only ``data()`` puts
    message content on the wire, so a failure at either of these precedes any
    content by construction. Reporting that as 'unknown' retires the caller's
    upload slot for a message that provably never left.

    The warm-up send matters: it drives ``data()`` on this very connection, so
    this also proves the evidence is per-send rather than sticky.
    """
    assert (await _send(sender, subject="Warm the connection"))["success"] is True

    # Every subsequent transaction dies on a pre-DATA command. The liveness
    # probe cannot help here: the connection is alive when it is checked.
    smtp_server.drop_at(drop_at)

    result = await _send(sender, subject="Dies before DATA")

    assert result["success"] is False
    assert result["delivery_state"] == "failed", (
        "a disconnect before any message bytes were written was reported as "
        f"ambiguous, which burns the caller's upload slot: {result}"
    )
    # And the premise holds: only the warm-up message ever reached the server.
    assert len(smtp_server.delivered) == 1


# --------------------------------------------------------------------------
# 3. A disconnect once the content may be on the wire stays ambiguous
#    THIS IS THE DUPLICATE-DELIVERY GUARD. It must never regress.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_disconnect_once_data_was_transmitted_stays_unknown(sender, smtp_server):
    """The safety property the whole design exists to protect.

    The server takes the entire message and then dies before the final 250.
    The bytes are at the MTA; whether it queued them is unknowable. This must
    stay 'unknown' so the slot is retired and no retry can deliver the same
    confidential message a second time.
    """
    assert (await _send(sender, subject="Warm the connection"))["success"] is True

    smtp_server.drop_at("DATA")

    result = await _send(sender, subject="Dies after the bytes were handed over")

    assert result["success"] is False
    assert result["delivery_state"] == "unknown", (
        "a disconnect after the message was transmitted must stay ambiguous, "
        f"or a retry can deliver it twice: {result}"
    )


# --------------------------------------------------------------------------
# 4. End to end: an idle-connection failure leaves the slot re-sendable, and
#    the SAME upload_id goes out on the retry with no new reservation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_idle_connection_failure_leaves_the_same_upload_sendable(
    tmp_path, monkeypatch, smtp_server
):
    """The blast radius the user actually hit, through the real send path.

    The reported incident was not only the failed send: the ambiguous
    classification retired the upload slot, so the same-key retry was refused
    with "Prior send is unknown", a new-key retry was refused with "already
    claimed", and the user had to reserve a fresh slot and upload the bytes
    again.

    Here the real ``send_mail`` tool, a real upload slot and a real SMTP socket
    are used. The connection dies before any message bytes are written, so the
    slot must come back to 'stored' -- and the *same* upload_id must then send
    successfully, without a second reservation.
    """
    import hashlib
    from urllib.parse import urlsplit

    from fastapi.testclient import TestClient
    from fastmcp.exceptions import ToolError
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from src import mcp_tools
    from src.database import connection
    from src.database.connection import get_db
    from src.email import smtp_sender as smtp_sender_module
    from src.models.base import Base
    from src.models.outbound_upload import OutboundUpload
    from src.models.smtp_config import SMTPConfig
    from src.security.auth import local_owner_user
    from src.server import api_app, final_app, mcp

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    engine = create_engine(
        f"sqlite:///{tmp_path}/idle.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(connection, "engine", engine)
    monkeypatch.setattr(connection, "SessionLocal", Session)
    monkeypatch.setattr(mcp_tools, "SessionLocal", Session)

    def override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = override_db

    # The self-signed certificate has to be trusted; the handshake stays real.
    original_default_context = ssl.create_default_context

    def trusting_context(*args, **kwargs):
        kwargs.setdefault("cafile", str(smtp_server.ca_file))
        return original_default_context(*args, **kwargs)

    monkeypatch.setattr(smtp_sender_module.ssl, "create_default_context", trusting_context)

    try:
        with TestClient(final_app) as client:
            with Session() as db:
                owner = local_owner_user(db)
                account = SMTPConfig(
                    owner_user_id=owner.id,
                    provider="imap",
                    auth_type="password",
                    name="Goedly Business",
                    account_name="owner@example.com",
                    host="localhost",
                    port=993,
                    smtp_host="localhost",
                    smtp_port=smtp_server.port,
                    username="owner@example.com",
                    credential_ciphertext="enc:test",
                    imap_use_ssl=True,
                    smtp_use_ssl=False,
                    smtp_use_tls=True,
                )
                account.password = "password"
                db.add(account)
                db.commit()
                db.refresh(account)
                account_id = account.id

            payload = b"%PDF-1.7 the confidential quarterly numbers"
            begin = (await mcp.get_tool("begin_attachment_upload")).fn
            slot = await begin(filename="quarterly.pdf", size_bytes=len(payload))
            upload_id = slot["upload_id"]

            parts = urlsplit(slot["upload_url"])
            stored = client.put(
                f"{parts.path}?{parts.query}" if parts.query else parts.path,
                content=payload,
            )
            assert stored.status_code == 200, stored.text
            assert stored.json()["sha256"] == hashlib.sha256(payload).hexdigest()

            send = (await mcp.get_tool("send_mail")).fn

            # The connection dies on the first command of the transaction --
            # before DATA, so no message content is ever written.
            smtp_server.drop_at("MAIL")

            with pytest.raises(ToolError) as error:
                await send(
                    account_id=account_id,
                    to_addresses=["recipient@example.com"],
                    subject="Quarterly numbers",
                    body_text="Attached.",
                    upload_ids=[upload_id],
                    idempotency_key="first-attempt",
                )
            assert json.loads(str(error.value))["code"] == "PROVIDER_ERROR"

            # Nothing reached the server, so the slot must be sendable again.
            assert smtp_server.delivered == []
            with Session() as db:
                state = db.query(OutboundUpload).one().state
            assert state == "stored", (
                "an SMTP failure that transmitted nothing retired the upload slot; "
                f"the caller must re-upload the bytes to send at all (state={state!r})"
            )

            # The server recovers, and the SAME upload_id goes out -- no new
            # reservation, no second upload of the bytes.
            smtp_server.drop_at(None)
            result = await send(
                account_id=account_id,
                to_addresses=["recipient@example.com"],
                subject="Quarterly numbers",
                body_text="Attached.",
                upload_ids=[upload_id],
                idempotency_key="second-attempt",
            )

            assert result["success"] is True, result
            assert len(smtp_server.delivered) == 1
            # Delivered exactly once, and with the bytes that were uploaded.
            attachments = [
                (part.get_filename(), part.get_payload(decode=True))
                for part in message_from_bytes(smtp_server.delivered[0]).walk()
                if part.get_content_disposition() == "attachment"
            ]
            assert attachments == [("quarterly.pdf", payload)]
            with Session() as db:
                assert db.query(OutboundUpload).one().state == "consumed"
    finally:
        api_app.dependency_overrides.clear()


# --------------------------------------------------------------------------
# 5. The connect timeout is configuration, not a hardcoded 10
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("use_ssl", [False, True], ids=["starttls", "implicit_ssl"])
async def test_both_connect_branches_honour_the_configured_timeouts(
    monkeypatch, use_ssl
):
    """A large attachment on a slow link must not trip the *connect* timeout.

    One hardcoded ``timeout=10`` governed the socket for its whole life, so the
    data phase inherited a connect-sized budget; exceeding it raised
    TimeoutError, which is classified ambiguous and burns the slot.
    """
    from src.email import smtp_sender as smtp_sender_module

    monkeypatch.setattr(settings, "smtp_connect_timeout_seconds", 17.0)
    monkeypatch.setattr(settings, "smtp_command_timeout_seconds", 123.0)

    observed: dict = {}

    class _FakeSMTP:
        """Stands in for the socket so only the timeout wiring is exercised."""

        def __init__(self, host=None, port=0, *args, timeout=None, context=None, **kwargs):
            observed["timeout"] = timeout
            self.sock = socket.socket()
            self.sock.settimeout(timeout)
            self.timeout = timeout
            self.message_data_started = False

        def starttls(self, context=None):
            return (220, b"ready")

        def login(self, username, password):
            return (235, b"ok")

    # connect() builds the tracked subclasses, which is what production uses.
    monkeypatch.setattr(smtp_sender_module, "_TrackedSMTP", _FakeSMTP)
    monkeypatch.setattr(smtp_sender_module, "_TrackedSMTP_SSL", _FakeSMTP)

    config = SimpleNamespace(
        id=1,
        name="Timeouts",
        account_name="owner@example.com",
        username="owner@example.com",
        password="password",
        auth_type="password",
        host="smtp.example.com",
        port=465 if use_ssl else 587,
        smtp_host="smtp.example.com",
        smtp_port=465 if use_ssl else 587,
        smtp_use_ssl=use_ssl,
        smtp_use_tls=not use_ssl,
        credential_ciphertext="enc:test",
    )
    sender = smtp_sender_module.EmailSender(config)

    assert await sender.connect() is True

    assert observed["timeout"] == 17.0, (
        "connect() ignored smtp_connect_timeout_seconds and used a hardcoded value"
    )
    # And the data phase gets the wider budget rather than the connect one.
    assert sender._server.sock.gettimeout() == 123.0, (
        "the data phase inherited the connect timeout"
    )
