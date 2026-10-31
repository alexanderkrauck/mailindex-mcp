"""The OAuth provider MCP clients sign in through in google auth mode."""

from __future__ import annotations

import logging

from fastmcp.server.auth.providers.google import GoogleProvider
from mcp.server.auth.provider import RegistrationError
from mcp.shared.auth import OAuthClientInformationFull

logger = logging.getLogger(__name__)


class McpGoogleProvider(GoogleProvider):
    """FastMCP's GoogleProvider, plus a log line when a registration's callback is refused.

    FastMCP answers a registration whose callback is not allowed with
    ``400 invalid_redirect_uri`` and logs nothing, so a client that could not
    connect left no trace on the server. The callback it asked for is what an
    operator needs to decide whether to allow it. Only refusals raised here are
    logged: the MCP SDK rejects a bad scope, grant type or response type before
    this method runs. Client name and callbacks are the caller's own claims, so
    they are logged truncated and quoted.
    """

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        try:
            await super().register_client(client_info)
        except RegistrationError as error:
            logger.warning(
                "Refused OAuth client registration from %r with redirect URIs %r: %s",
                str(client_info.client_name or "")[:100],
                [str(uri)[:300] for uri in (client_info.redirect_uris or [])[:5]],
                error.error,
            )
            raise
