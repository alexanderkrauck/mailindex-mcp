"""Liveness regressions against loopback IMAP peers (synthetic credentials only)."""

import asyncio
import contextlib
import ipaddress
import ssl
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from src.email import smtp_client


@pytest_asyncio.fixture(autouse=True)
async def no_orphan_tasks_or_unhandled_errors():
    loop = asyncio.get_running_loop()
    baseline = asyncio.all_tasks()
    errors = []
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    try:
        yield
        # Flush done callbacks and connection_lost delivery before checking.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        leaked = asyncio.all_tasks() - baseline - {asyncio.current_task()}
        assert not leaked, "IMAP operations left orphan tasks"
        assert not errors, "IMAP operations emitted an unhandled callback/task exception"
    finally:
        loop.set_exception_handler(old_handler)


@asynccontextmanager
async def imap_peer(mode="silent", logout="healthy", ssl_context=None):
    """A controllable IMAP peer; never connects to a real mailbox."""
    tasks = set()
    writers = set()
    commands = []
    capability_seen = asyncio.Event()
    logout_seen = asyncio.Event()
    fetch_seen = asyncio.Event()
    drop = asyncio.Event()
    closed = asyncio.Event()

    async def serve(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        writers.add(writer)
        try:
            if mode == "no_greeting":
                await reader.read()
                return
            if mode == "bad_greeting":
                writer.write(b"* BAD testuser testpass\r\n")
                await writer.drain()
                await reader.read()
                return
            writer.write(b"* OK loopback IMAP ready\r\n")
            await writer.drain()
            while line := await reader.readline():
                tag, command, *_ = line.split()
                commands.append(command)
                if command == b"CAPABILITY":
                    capability_seen.set()
                    if mode == "disconnect":
                        return
                    if mode == "silent":
                        await reader.read()
                        return
                    if mode == "trickle":
                        while True:
                            writer.write(b"* CAPABILITY IMAP4rev1\r\n")
                            await writer.drain()
                            await asyncio.sleep(0.01)
                    if mode == "slow_login":
                        await asyncio.sleep(0.1)
                    writer.write(b"* CAPABILITY IMAP4rev1\r\n" + tag + b" OK done\r\n")
                elif command == b"FETCH" and mode == "disconnect_fetch":
                    fetch_seen.set()
                    await drop.wait()
                    return
                elif command == b"LOGIN":
                    if mode == "slow_login":
                        await asyncio.sleep(0.1)
                    if mode == "reject_login":
                        writer.write(tag + b" NO testuser testpass\r\n")
                    else:
                        writer.write(tag + b" OK logged in\r\n")
                elif command == b"LOGOUT":
                    logout_seen.set()
                    if logout == "silent":
                        await reader.read()
                        return
                    if logout == "disconnect":
                        return
                    if logout == "reject":
                        writer.write(tag + b" NO cannot log out\r\n")
                        await writer.drain()
                        await reader.read()
                        return
                    writer.write(b"* BYE closing\r\n" + tag + b" OK logged out\r\n")
                    await writer.drain()
                    return
                else:
                    writer.write(tag + b" OK done\r\n")
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            writers.discard(writer)
            tasks.discard(task)
            closed.set()

    server = await asyncio.start_server(serve, "127.0.0.1", 0, ssl=ssl_context)
    peer = SimpleNamespace(
        port=server.sockets[0].getsockname()[1], commands=commands,
        capability_seen=capability_seen, logout_seen=logout_seen, closed=closed,
        fetch_seen=fetch_seen, drop=drop,
    )
    try:
        yield peer
    finally:
        server.close()
        await server.wait_closed()
        for writer in tuple(writers):
            writer.close()
        for task in tuple(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture
def local_client(monkeypatch, mock_smtp_config):
    """Use the real protocol over loopback, omitting TLS only at this test seam."""
    # A SimpleNamespace keeps this test independent of settings validation.
    monkeypatch.setattr("src.config.settings", SimpleNamespace(
        imap_command_timeout_seconds=0.15, imap_cleanup_timeout_seconds=0.15,
    ))
    clients = []
    factory = smtp_client.aioimaplib.IMAP4

    def connect_local(**kwargs):
        kwargs.pop("ssl_context", None)
        client = factory(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(smtp_client.aioimaplib, "IMAP4_SSL", connect_local)
    mock_smtp_config.host = "127.0.0.1"
    return smtp_client.SMTPClient(mock_smtp_config), clients


async def stop_test_tasks(client, operation):
    """Make a failing old-library test exit instead of hanging pytest shutdown."""
    protocol = client.protocol
    if protocol.transport is not None:
        protocol.transport.abort()
    for task in tuple(protocol.tasks) + tuple(client.tasks) + (client._client_task,):
        task.cancel()
    operation.cancel()
    await asyncio.gather(operation, *protocol.tasks, *client.tasks, client._client_task,
                         return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["silent", "trickle"])
async def test_handshake_has_absolute_deadline(local_client, mode):
    client, transports = local_client
    async with imap_peer(mode) as peer:
        client.config.port = peer.port
        operation = asyncio.create_task(client.connect())
        done, _ = await asyncio.wait({operation}, timeout=0.65)
        try:
            assert done, "CAPABILITY holds the condition lock past the handshake deadline"
            assert operation.result() is False
        finally:
            await stop_test_tasks(transports[0], operation)


def assert_reaped(transport):
    assert transport._client_task.done()
    assert all(task.done() for task in transport.tasks | transport.protocol.tasks)
    assert transport.protocol.transport.is_closing()
    assert transport.protocol.pending_sync_command is None
    assert not transport.protocol.pending_async_commands
    assert not transport.protocol.state_condition.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["silent", "trickle", "no_greeting"])
async def test_failed_handshake_closes_socket_and_reaps_tasks(local_client, mode):
    client, transports = local_client
    async with imap_peer(mode) as peer:
        client.config.port = peer.port
        operation = asyncio.create_task(client.connect())
        try:
            done, _ = await asyncio.wait({operation}, timeout=0.65)
            assert done
            assert operation.result() is False
            assert client.client is None
            assert not client._connected
            assert_reaped(transports[0])
        finally:
            await stop_test_tasks(transports[0], operation)


@pytest.mark.asyncio
@pytest.mark.parametrize("logout", ["silent", "reject", "disconnect"])
async def test_failed_logout_is_aborted_within_cleanup_budget(local_client, logout):
    client, transports = local_client
    async with imap_peer("healthy", logout=logout) as peer:
        client.config.port = peer.port
        assert await client.connect()
        # An ordinary command can have a much longer timeout than cleanup.
        transports[0].timeout = 10
        operation = asyncio.create_task(client.disconnect())
        try:
            done, _ = await asyncio.wait({operation}, timeout=0.45)
            assert done, "logout must not consume the ordinary command timeout"
            assert operation.result() is None
            assert_reaped(transports[0])
        finally:
            await stop_test_tasks(transports[0], operation)


@pytest.mark.asyncio
async def test_disconnect_during_capability_is_not_reported_as_timeout(local_client):
    from src.config import settings

    settings.imap_command_timeout_seconds = 2
    client, transports = local_client
    async with imap_peer("disconnect") as peer:
        client.config.port = peer.port
        operation = asyncio.create_task(client.connect())
        try:
            done, _ = await asyncio.wait({operation}, timeout=0.45)
            assert done, "connection_lost must wake the handshake immediately"
            assert operation.result() is False
            assert isinstance(client._connection_error, ConnectionError)
            assert not isinstance(client._connection_error, TimeoutError)
            assert_reaped(transports[0])
        finally:
            await stop_test_tasks(transports[0], operation)


@pytest.mark.asyncio
async def test_bad_greeting_preserves_cause_without_logging_wire_data(local_client, caplog):
    from aioimaplib import Error

    client, transports = local_client
    async with imap_peer("bad_greeting") as peer:
        client.config.port = peer.port
        with pytest.raises(ConnectionError) as error:
            await anext(client.fetch_new_emails())
        assert isinstance(error.value.__cause__, Error)
        assert "testuser" not in caplog.text
        assert "testpass" not in caplog.text
        assert_reaped(transports[0])


@pytest.mark.asyncio
async def test_cancelled_handshake_reaps_before_propagating(local_client):
    client, transports = local_client
    async with imap_peer("silent") as peer:
        client.config.port = peer.port
        operation = asyncio.create_task(client.connect())
        try:
            await asyncio.wait_for(peer.capability_seen.wait(), 0.4)
            operation.cancel()
            done, _ = await asyncio.wait({operation}, timeout=0.4)
            assert done
            with pytest.raises(asyncio.CancelledError):
                operation.result()
            assert client.client is None
            assert not client._connected
            assert_reaped(transports[0])
        finally:
            await stop_test_tasks(transports[0], operation)


@pytest.mark.asyncio
async def test_repeated_cancel_during_logout_does_not_interrupt_reaping(local_client):
    client, transports = local_client
    finished = asyncio.Event()

    async def owned_task():
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.04)
            finished.set()

    async with imap_peer("healthy", logout="silent") as peer:
        client.config.port = peer.port
        assert await client.connect()
        worker = asyncio.create_task(owned_task())
        transports[0].protocol.tasks.add(worker)
        operation = asyncio.create_task(client.disconnect())
        try:
            await asyncio.wait_for(peer.logout_seen.wait(), 0.4)
            operation.cancel()
            await asyncio.sleep(0.01)
            operation.cancel()
            done, _ = await asyncio.wait({operation}, timeout=0.4)
            assert done
            with pytest.raises(asyncio.CancelledError):
                operation.result()
            assert finished.is_set(), "disconnect returned before owned task was reaped"
            assert_reaped(transports[0])
        finally:
            await stop_test_tasks(transports[0], operation)


@pytest.mark.asyncio
async def test_connection_refusal_surfaces_original_error_promptly(local_client):
    from src.config import settings

    settings.imap_command_timeout_seconds = 2
    client, transports = local_client
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    client.config.port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    operation = asyncio.create_task(client.connect())
    try:
        done, _ = await asyncio.wait({operation}, timeout=0.45)
        assert done, "connect task errors must not become greeting timeouts"
        assert operation.result() is False
        assert isinstance(client._connection_error, ConnectionRefusedError)
        assert transports[0]._client_task.done()
    finally:
        await stop_test_tasks(transports[0], operation)


@pytest.mark.asyncio
async def test_connection_lost_fails_active_and_queued_commands_without_orphans(local_client):
    client, transports = local_client
    async with imap_peer("disconnect_fetch") as peer:
        client.config.port = peer.port
        assert await client.connect()
        imap = transports[0]
        imap.timeout = 2
        await imap.select("INBOX")
        baseline = asyncio.all_tasks()
        fetching = asyncio.create_task(imap.fetch("1", "(UID)"))
        await asyncio.wait_for(peer.fetch_seen.wait(), 0.4)
        selecting = asyncio.create_task(imap.select("INBOX"))
        # Let SELECT reach the barrier behind the outstanding async FETCH.
        for _ in range(4):
            await asyncio.sleep(0)
        peer.drop.set()
        try:
            done, _ = await asyncio.wait({fetching, selecting}, timeout=0.4)
            assert len(done) == 2, "queued commands must fail on socket loss, not time out"
            for task in done:
                with pytest.raises(ConnectionError):
                    task.result()
            await client.disconnect()
            assert_reaped(imap)
            await asyncio.sleep(0)
            assert not (asyncio.all_tasks() - baseline)
        finally:
            for task in (fetching, selecting):
                task.cancel()
            await asyncio.gather(fetching, selecting, return_exceptions=True)
            await client.disconnect()


@pytest.mark.asyncio
async def test_login_shares_absolute_handshake_budget(local_client):
    client, transports = local_client
    async with imap_peer("slow_login") as peer:
        client.config.port = peer.port
        try:
            assert await client.connect() is False, "login must not restart the handshake clock"
            assert isinstance(client._connection_error, TimeoutError)
            assert_reaped(transports[0])
        finally:
            await client.disconnect()


@pytest.mark.asyncio
async def test_login_rejection_does_not_log_server_supplied_credentials(local_client, caplog):
    client, transports = local_client
    async with imap_peer("reject_login") as peer:
        client.config.port = peer.port
        assert await client.connect() is False
        assert "testuser" not in caplog.text
        assert "testpass" not in caplog.text
        assert isinstance(client._connection_error, PermissionError)
        assert_reaped(transports[0])


@pytest.fixture
def tls_contexts(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "loopback IMAP test")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        ]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert_path, key_path)
    verified_client = ssl.create_default_context(cafile=str(cert_path))
    return server, verified_client


@pytest.mark.asyncio
async def test_healthy_verified_tls_login_commands_logout_and_reconnect(
    monkeypatch, mock_smtp_config, tls_contexts,
):
    server_context, verified_client = tls_contexts
    monkeypatch.setattr(smtp_client.ssl, "create_default_context", lambda: verified_client)
    monkeypatch.setattr("src.config.settings", SimpleNamespace(
        imap_command_timeout_seconds=1, imap_cleanup_timeout_seconds=0.15,
    ))
    async with imap_peer("healthy", ssl_context=server_context) as peer:
        mock_smtp_config.host = "127.0.0.1"
        mock_smtp_config.port = peer.port
        client = smtp_client.SMTPClient(mock_smtp_config)
        for _ in range(2):
            assert await client.connect()
            transport = client.client
            assert isinstance(transport.protocol, smtp_client.aioimaplib.IMAP4ClientProtocol)
            assert transport.protocol.transport.get_extra_info("ssl_object") is not None
            assert await client._ensure_connected()
            assert (await transport.select("INBOX")).result == "OK"
            await client.disconnect()
            await client.disconnect()  # Idempotent cleanup.
            assert_reaped(transport)
        assert peer.commands == [b"CAPABILITY", b"LOGIN", b"NOOP", b"SELECT", b"LOGOUT"] * 2


@pytest.mark.asyncio
async def test_stalled_tls_handshake_cancels_connection_task(monkeypatch, mock_smtp_config):
    monkeypatch.setattr("src.config.settings", SimpleNamespace(
        imap_command_timeout_seconds=0.15, imap_cleanup_timeout_seconds=0.15,
    ))
    # A plaintext peer accepting TCP but never speaking TLS.
    async with imap_peer("no_greeting") as peer:
        mock_smtp_config.host = "127.0.0.1"
        mock_smtp_config.port = peer.port
        client = smtp_client.SMTPClient(mock_smtp_config)
        operation = asyncio.create_task(client.connect())
        # Capture the transport before SMTPClient detaches it on failure.
        while client.client is None and not operation.done():
            await asyncio.sleep(0)
        transport = client.client
        try:
            done, _ = await asyncio.wait({operation}, timeout=0.5)
            assert done
            assert operation.result() is False
            assert isinstance(client._connection_error, TimeoutError)
            assert transport._client_task.done()
            assert client.client is None
            await asyncio.wait_for(peer.closed.wait(), 0.2)
        finally:
            await stop_test_tasks(transport, operation)


@pytest.mark.asyncio
async def test_cancelling_active_command_releases_queued_waiters(local_client):
    client, transports = local_client
    async with imap_peer("disconnect_fetch") as peer:
        client.config.port = peer.port
        assert await client.connect()
        imap = transports[0]
        imap.timeout = 2
        await imap.select("INBOX")
        fetching = asyncio.create_task(imap.fetch("1", "(UID)"))
        await asyncio.wait_for(peer.fetch_seen.wait(), 0.4)
        selecting = asyncio.create_task(imap.select("INBOX"))
        for _ in range(4):
            await asyncio.sleep(0)
        fetching.cancel()
        try:
            done, _ = await asyncio.wait({fetching, selecting}, timeout=0.4)
            assert len(done) == 2, "cancelled commands must not leave queue barriers"
            with pytest.raises(asyncio.CancelledError):
                fetching.result()
            with pytest.raises(ConnectionError):
                selecting.result()
            assert imap.protocol.transport.is_closing()
        finally:
            for task in (fetching, selecting):
                task.cancel()
            await asyncio.gather(fetching, selecting, return_exceptions=True)
            await client.disconnect()
