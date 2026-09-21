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
