"""Narrow compatibility fixes for released FastMCP authentication defects."""

from __future__ import annotations

import logging
from functools import wraps
from importlib.metadata import version
from typing import Any

logger = logging.getLogger(__name__)

# FastMCP 3.4.4-3.4.6 construct the CIMD private_key_jwt audience as
# ``f"{base_url}/token"``. Pydantic renders a bare-authority base URL with a
# trailing slash, so ChatGPT signs for the advertised ``/token`` endpoint while
# FastMCP incorrectly verifies against ``//token``. This is the application-level
# backport of https://github.com/PrefectHQ/fastmcp/commit/0c1c42f15139cc431850400c25322f1761703e88.
_AFFECTED_FASTMCP_VERSIONS = {"3.4.4", "3.4.5", "3.4.6"}
_PATCH_MARKER = "_mailindex_cimd_token_audience_fix"


def apply_fastmcp_cimd_token_audience_fix() -> bool:
    """Normalize the CIMD token audience until the upstream fix is released."""
    from fastmcp.server.auth.auth import PrivateKeyJWTClientAuthenticator

    if getattr(PrivateKeyJWTClientAuthenticator, _PATCH_MARKER, False):
        return True

    installed_version = version("fastmcp")
    if installed_version not in _AFFECTED_FASTMCP_VERSIONS:
        return False

    original_init = PrivateKeyJWTClientAuthenticator.__init__

    @wraps(original_init)
    def normalized_init(
        self: Any,
        provider: Any,
        cimd_manager: Any,
        token_endpoint_url: str,
    ) -> None:
        normalized_token_endpoint = f"{str(provider.base_url).rstrip('/')}/token"
        original_init(
            self,
            provider=provider,
            cimd_manager=cimd_manager,
            token_endpoint_url=normalized_token_endpoint,
        )

    PrivateKeyJWTClientAuthenticator.__init__ = normalized_init
    setattr(PrivateKeyJWTClientAuthenticator, _PATCH_MARKER, True)
    logger.warning(
        "Applied FastMCP %s CIMD token audience compatibility fix",
        installed_version,
    )
    return True
