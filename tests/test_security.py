"""Security boundary tests."""

import pytest


def test_credentials_are_encrypted_and_round_trip():
    from src.security.crypto import decrypt_secret, encrypt_secret

    encrypted = encrypt_secret("not-stored-in-plaintext")

    assert encrypted.startswith("enc:v1:")
    assert "not-stored-in-plaintext" not in encrypted
    assert decrypt_secret(encrypted) == "not-stored-in-plaintext"


def test_download_token_is_bound_to_attachment_and_expiry_claim():
    from src.security.download_tokens import issue_download_token, verify_download_token

    token = issue_download_token(user_id=7, attachment_id=42)

    assert token.count(".") == 1
    assert verify_download_token(token, attachment_id=42) == 7
    with pytest.raises(ValueError):
        verify_download_token(token, attachment_id=43)
    with pytest.raises(ValueError):
        verify_download_token(token[:-1] + ("A" if token[-1] != "A" else "B"), attachment_id=42)


def test_account_connect_token_is_signed_and_user_bound():
    from src.security.account_connect_tokens import (
        issue_account_connect_token,
        issue_password_setup_token,
        verify_account_connect_token,
        verify_password_setup_token,
    )

    token = issue_account_connect_token(user_id=7)

    assert token.count(".") == 1
    assert verify_account_connect_token(token) == 7
    with pytest.raises(ValueError):
        verify_account_connect_token(token[:-1] + ("A" if token[-1] != "A" else "B"))

    password_token = issue_password_setup_token(
        user_id=7,
        account_id=42,
        credential_ciphertext=None,
    )
    claims = verify_password_setup_token(password_token, 42, None)
    assert claims.user_id == 7
    assert claims.account_id == 42
    with pytest.raises(ValueError):
        verify_password_setup_token(password_token, 43, None)
    with pytest.raises(ValueError):
        verify_password_setup_token(password_token, 42, "changed-credential")
    with pytest.raises(ValueError):
        verify_account_connect_token(password_token)
    with pytest.raises(ValueError):
        verify_password_setup_token(token, 42, None)


@pytest.mark.asyncio
async def test_google_oauth_provider_emits_audience_bound_mcp_challenge():
    from fastmcp import FastMCP
    from fastmcp.server.auth.providers.google import GoogleProvider
    from httpx import ASGITransport, AsyncClient

    provider = GoogleProvider(
        client_id="test.apps.googleusercontent.com",
        client_secret="test-secret",
        base_url="https://mail.example.com",
        resource_base_url="https://mail.example.com",
        allowed_client_redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
        jwt_signing_key="long-enough-test-signing-key",
    )
    app = FastMCP("auth-test", auth=provider).http_app(path="/mcp")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://mail.example.com") as client:
        response = await client.post(
            "/mcp",
            headers={"accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == (
            'Bearer resource_metadata="https://mail.example.com/.well-known/oauth-protected-resource/mcp"'
        )
        assert (await client.get("/.well-known/oauth-protected-resource/mcp")).status_code == 200
        assert (await client.get("/.well-known/oauth-authorization-server")).status_code == 200


def test_chatgpt_cimd_client_assertion_uses_advertised_token_endpoint():
    from types import SimpleNamespace

    from fastmcp.server.auth.auth import PrivateKeyJWTClientAuthenticator

    from src.security.fastmcp_compat import apply_fastmcp_cimd_token_audience_fix

    apply_fastmcp_cimd_token_audience_fix()
    authenticator = PrivateKeyJWTClientAuthenticator(
        provider=SimpleNamespace(base_url="https://mail.example.com/"),
        cimd_manager=object(),
        token_endpoint_url="https://mail.example.com//token",
    )

    assert authenticator._token_endpoint_url == "https://mail.example.com/token"


def test_google_oauth_repeat_authorization_requests_refresh_token():
    from src.security.auth import GOOGLE_AUTHORIZE_PARAMS

    assert GOOGLE_AUTHORIZE_PARAMS == {
        "access_type": "offline",
        "prompt": "consent select_account",
    }


def test_unverified_google_email_is_rejected():
    from fastapi import HTTPException

    from src.security.auth import _claims_from_mapping

    with pytest.raises(HTTPException) as error:
        _claims_from_mapping(
            {"sub": "subject", "email": "unverified@example.com", "email_verified": False}
        )

    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_expected_mcp_errors_are_structured_without_disabling_masking():
    import json

    from fastapi import HTTPException
    from fastmcp.exceptions import ToolError

    from src.security.mcp_errors import mcp_error_boundary

    @mcp_error_boundary
    async def fails():
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Provider connection failed",
                "imap": True,
                "smtp": False,
            },
        )

    with pytest.raises(ToolError) as error:
        await fails()
    payload = json.loads(str(error.value))
    assert payload == {
        "code": "PROVIDER_CONNECTION_FAILED",
        "details": {"imap": True, "smtp": False},
        "message": "Provider connection failed",
        "retryable": False,
    }


@pytest.mark.asyncio
async def test_mcp_permission_errors_do_not_become_generic_failures():
    import json

    from fastmcp.exceptions import ToolError

    from src.security.mcp_errors import mcp_error_boundary

    @mcp_error_boundary
    async def fails():
        raise PermissionError("internal identity context")

    with pytest.raises(ToolError) as error:
        await fails()
    assert json.loads(str(error.value)) == {
        "code": "AUTHENTICATION_REQUIRED",
        "message": "Authentication required",
        "retryable": False,
    }


def test_chatgpt_dynamic_connector_callback_is_allowlisted():
    from fastmcp.server.auth.redirect_validation import validate_redirect_uri

    from src.config import settings

    callback = "https://chatgpt.com/connector/oauth/rpYtr-3A0Ot6"
    assert validate_redirect_uri(callback, settings.allowed_client_redirect_uris)
    assert not validate_redirect_uri(
        "https://chatgpt.com.evil.example/connector/oauth/rpYtr-3A0Ot6",
        settings.allowed_client_redirect_uris,
    )
    assert not validate_redirect_uri(
        "https://chatgpt.com/other/oauth/rpYtr-3A0Ot6",
        settings.allowed_client_redirect_uris,
    )


GROK_CALLBACKS = [
    "https://grok.com/connectors-oauth-exchange-code/",
    "https://grok.com/connectors-oauth-exchange-code",
    "https://console.x.ai/connectors-oauth-exchange-code/",
    "https://console.x.ai/connectors-oauth-exchange-code",
]


def test_grok_connector_callbacks_are_allowlisted_and_nothing_near_them():
    from fastmcp.server.auth.redirect_validation import validate_redirect_uri

    from src.config import settings

    for callback in GROK_CALLBACKS:
        assert validate_redirect_uri(callback, settings.allowed_client_redirect_uris), callback
    for lookalike in (
        "https://grok.com.evil.example/connectors-oauth-exchange-code/",
        "https://evil.example/connectors-oauth-exchange-code/",
        "https://x.ai/connectors-oauth-exchange-code/",
        "https://grok.com/connectors-oauth-exchange-code/elsewhere",
        "https://grok.com/connectors-oauth-exchange-code/../elsewhere",
        "https://someone@grok.com/connectors-oauth-exchange-code/",
        "http://grok.com/connectors-oauth-exchange-code/",
    ):
        assert not validate_redirect_uri(lookalike, settings.allowed_client_redirect_uris), lookalike


@pytest.mark.asyncio
async def test_grok_can_register_itself_by_dynamic_client_registration(caplog):
    """Grok's first step. Before its callback was allowed, this answered 400 invalid_redirect_uri."""
    import logging

    from fastmcp import FastMCP
    from httpx import ASGITransport, AsyncClient
    from key_value.aio.stores.memory import MemoryStore

    from src.config import settings
    from src.security.google_oauth import McpGoogleProvider

    provider = McpGoogleProvider(
        client_id="test.apps.googleusercontent.com",
        client_secret="test-secret",
        base_url="https://mail.example.com",
        resource_base_url="https://mail.example.com",
        required_scopes=settings.google_required_scopes,
        valid_scopes=settings.google_required_scopes,
        allowed_client_redirect_uris=settings.allowed_client_redirect_uris,
        jwt_signing_key="long-enough-test-signing-key",
        client_storage=MemoryStore(),
    )
    app = FastMCP("auth-test", auth=provider).http_app(path="/mcp")

    def registration(callback: str) -> dict:
        return {
            "client_name": "Grok",
            "redirect_uris": [callback],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://mail.example.com") as client:
        for callback in GROK_CALLBACKS:
            response = await client.post("/register", json=registration(callback))
            assert response.status_code == 201, response.text
            assert response.json()["redirect_uris"] == [callback]
        assert not [r for r in caplog.records if r.name == "src.security.google_oauth"]

        lookalike = "https://grok.com.evil.example/connectors-oauth-exchange-code/"
        with caplog.at_level(logging.WARNING, logger="src.security.google_oauth"):
            refused = await client.post("/register", json=registration(lookalike))
        assert refused.status_code == 400
        assert refused.json()["error"] == "invalid_redirect_uri"
        # The refusal names the callback, so the next client that cannot connect is diagnosable.
        logged = [r.getMessage() for r in caplog.records if r.name == "src.security.google_oauth"]
        assert len(logged) == 1
        assert "'Grok'" in logged[0] and lookalike in logged[0] and "invalid_redirect_uri" in logged[0]
