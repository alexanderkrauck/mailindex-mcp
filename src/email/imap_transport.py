"""Per-connection liveness fixes for the pinned aioimaplib 2.0.1 protocol.

Keep private-library access here, never monkeypatch aioimaplib globally. Changes
of aioimaplib version must run the loopback liveness tests before deployment.
"""

import asyncio
import logging

from aioimaplib import aioimaplib as library

logger = logging.getLogger(__name__)


class IMAP4ClientProtocol(library.IMAP4ClientProtocol):
    def __init__(self, loop, conn_lost_cb, handshake_deadline):
        super().__init__(loop, conn_lost_cb)
        self.handshake_deadline = handshake_deadline
        self.hello_done = asyncio.Event()
        self.failure = None

    def fail(self, exc):
        """Wake command waiters without exposing command arguments in errors."""
        if self.failure is None:
            self.failure = exc
        commands = list(self.pending_async_commands.values())
        if self.pending_sync_command is not None:
            commands.append(self.pending_sync_command)
        for command in commands:
            command._exception = self.failure
            command._timer.cancel()
            command._event.set()
        self.pending_sync_command = None
        self.pending_async_commands.clear()
        self.hello_done.set()

    async def execute(self, command, scrub=None):
        try:
            if self.failure is not None:
                raise self.failure
            return await super().execute(command, scrub=scrub)
        except asyncio.CancelledError:
            # The response stream cannot safely be reused after abandoning an
            # in-flight command. Wake any queue barriers BEFORE removing it.
            self.abort()
            raise
        finally:
            # Upstream only removes timed-out commands, not cancelled ones.
            # A cancelled command must not leave a timer or a queue barrier.
            command._timer.cancel()
            if self.pending_sync_command is command:
                self.pending_sync_command = None
            key = command.untagged_resp_name
            if self.pending_async_commands.get(key) is command:
                self.pending_async_commands.pop(key)

    async def wait_async_pending_commands(self):
        # Upstream creates unowned Command.wait tasks and discards their
        # exceptions. Await the already-running commands without child tasks.
        for command in tuple(self.pending_async_commands.values()):
            await command.wait()
        if self.failure is not None:
            raise self.failure

    def connection_lost(self, exc):
        self.fail(exc if exc is not None else ConnectionError("IMAP peer closed connection"))
        if self.conn_lost_cb is not None:
            self.conn_lost_cb(exc)

    def abort(self):
        self.fail(ConnectionAbortedError("IMAP connection closed"))
        if self.transport is not None:
            self.transport.abort()

    async def welcome(self, command):
        # The upstream change_state wrapper holds state_condition throughout
        # welcome -> CAPABILITY. CAPABILITY has no timeout, and a command's
        # inactivity timer alone would be reset by trickling response lines.
        # Cancel the lock OWNER at an absolute deadline, not a Condition waiter
        # (whose cancellation must reacquire the very same lock).
        try:
            async with asyncio.timeout_at(self.handshake_deadline):
                await super().welcome(command)
        except Exception as exc:
            self.fail(exc)
        finally:
            self.hello_done.set()


class IMAP4(library.IMAP4):
    def create_client(self, host, port, loop, conn_lost_cb=None, ssl_context=None):
        loop = loop or asyncio.get_running_loop()
        self.handshake_deadline = loop.time() + self.timeout
        self.protocol = IMAP4ClientProtocol(loop, conn_lost_cb, self.handshake_deadline)
        self._client_task = loop.create_task(
            loop.create_connection(lambda: self.protocol, host, port, ssl=ssl_context)
        )
        self._client_task.add_done_callback(self._connection_done)

    def _connection_done(self, task):
        if task.cancelled():
            self.protocol.fail(ConnectionAbortedError("IMAP connection cancelled"))
        elif (exc := task.exception()) is not None:
            self.protocol.fail(exc)

    async def wait_hello_from_server(self):
        # Do not wait on state_condition: NONAUTH is assigned before CAPABILITY
        # has completed and failed greetings must not look like ready clients.
        async with asyncio.timeout_at(self.handshake_deadline):
            await self.protocol.hello_done.wait()
        if self.protocol.failure is not None:
            raise self.protocol.failure


class IMAP4_SSL(library.IMAP4_SSL, IMAP4):
    """Retain upstream TLS setup while using the per-client protocol factory."""


def _consume_task(task):
    if not task.cancelled():
        task.exception()


def _owned_tasks(client):
    tasks = set()
    for owner in (client, getattr(client, "protocol", None)):
        collection = getattr(owner, "tasks", ())
        if isinstance(collection, set):
            tasks.update(collection)
    connection = getattr(client, "_client_task", None)
    if isinstance(connection, asyncio.Future):
        tasks.add(connection)
    return tasks


async def close_client(client, timeout):
    """Finish bounded cleanup even if the caller is cancelled repeatedly."""
    cleanup = asyncio.create_task(_close_client(client, timeout))
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
            protocol = getattr(client, "protocol", None)
            if isinstance(protocol, IMAP4ClientProtocol):
                protocol.abort()
    cleanup.result()
    if cancelled:
        raise asyncio.CancelledError


async def _close_client(client, timeout):
    """Graceful logout, then force-close and reap within ONE cleanup budget."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    protocol = getattr(client, "protocol", None)
    logout = None
    try:
        if not isinstance(protocol, IMAP4ClientProtocol) or (
            protocol.failure is None and protocol.hello_done.is_set()
            and protocol.state in {library.AUTH, library.NONAUTH, library.SELECTED}
        ):
            logout = asyncio.create_task(client.logout())
            # Reserve half the total budget for abort + cancellation/reaping.
            # wait_for is unsuitable here: it waits indefinitely for cancel.
            await asyncio.wait({logout}, timeout=timeout / 2)
    finally:
        if isinstance(protocol, IMAP4ClientProtocol):
            protocol.abort()
        idle_waiter = getattr(client, "_idle_waiter", None)
        if isinstance(idle_waiter, asyncio.TimerHandle):
            idle_waiter.cancel()
        tasks = _owned_tasks(client)
        if logout is not None:
            tasks.add(logout)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            done, pending = await asyncio.wait(
                tasks, timeout=max(0, deadline - loop.time())
            )
            for task in done:
                _consume_task(task)
            for task in pending:
                task.cancel()
                task.add_done_callback(_consume_task)
            if pending:
                logger.error("IMAP cleanup deadline exceeded; %d tasks pending", len(pending))
