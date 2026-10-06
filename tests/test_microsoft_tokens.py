"""Microsoft mailbox credentials: what is stored, how access tokens are obtained, and what a refusal means."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.config import settings
from src.models.base import Base
from src.models.smtp_config import SMTPConfig
from src.models.user import User
from src.security import provider_tokens
from src.security.crypto import decrypt_secret


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        return self._body


@pytest.fixture
def microsoft_settings(monkeypatch):
    monkeypatch.setattr(settings, "microsoft_client_id", "client-id")
    monkeypatch.setattr(settings, "microsoft_client_secret", "client-secret")
    monkeypatch.setattr(settings, "microsoft_tenant", "common")


@pytest.fixture
def token_endpoint(monkeypatch, microsoft_settings):
    """Record what is POSTed to Microsoft and answer with a queue of responses."""
    calls = []
    responses = []

    def post(url, *, data, timeout):
        calls.append({"url": url, "data": dict(data), "timeout": timeout})
        return responses.pop(0) if responses else FakeResponse(
            200, {"access_token": "access-token", "refresh_token": "refresh-token", "expires_in": 3600}
        )

    monkeypatch.setattr(provider_tokens.httpx, "post", post)
    return SimpleNamespace(calls=calls, responses=responses)


def microsoft_config(refresh="refresh-token", tenant="tenant-id", account_id=None):
    return SimpleNamespace(
        id=account_id,
        provider="microsoft",
        auth_type="oauth2",
        credential_ciphertext=provider_tokens.encode_microsoft_credential(refresh, tenant),
    )


def test_stored_credential_holds_the_refresh_token_but_not_the_client_secret(microsoft_settings):
    ciphertext = provider_tokens.encode_microsoft_credential("the-refresh-token", "tenant-id")

    payload = json.loads(decrypt_secret(ciphertext))

    assert payload == {"provider": "microsoft", "refresh_token": "the-refresh-token", "tenant": "tenant-id"}
    assert "the-refresh-token" not in ciphertext
    # The app registration's secret is read from the environment at use, so
    # rotating it does not mean connecting every mailbox again.
    assert "client-secret" not in json.dumps(payload)


@pytest.mark.parametrize(
    ("purpose", "scope"),
    [
        ("imap", "https://outlook.office.com/IMAP.AccessAsUser.All"),
        ("send", "https://graph.microsoft.com/Mail.Send"),
    ],
)
def test_each_use_asks_for_only_the_scope_it_needs(token_endpoint, purpose, scope):
    token = provider_tokens.access_token_for(microsoft_config(tenant="tenant-id"), purpose)

    assert token == "access-token"
    call = token_endpoint.calls[0]
    # A token is issued for one resource, so IMAP and sending cannot share one.
    assert call["data"]["scope"] == scope
    assert call["data"]["grant_type"] == "refresh_token"
    assert call["data"]["refresh_token"] == "refresh-token"
    assert call["url"] == "https://login.microsoftonline.com/tenant-id/oauth2/v2.0/token"


def test_redeeming_the_code_names_one_resource_because_the_token_endpoint_accepts_no_more(token_endpoint):
    """Both resources go on the consent screen; only one may go in the redemption (AADSTS28000)."""
    token_endpoint.responses.append(FakeResponse(200, {"access_token": "a", "refresh_token": "r", "id_token": "i"}))

    provider_tokens.redeem_microsoft_authorization_code("code", "verifier", "https://mail.example.com/cb", "common")

    requested = token_endpoint.calls[0]["data"]["scope"].split()
    resources = {scope.rsplit("/", 1)[0] for scope in requested if scope.startswith("https://")}
    assert resources == {"https://outlook.office.com"}
    assert {"openid", "offline_access"} <= set(requested)
    assert token_endpoint.calls[0]["data"]["grant_type"] == "authorization_code"
    assert token_endpoint.calls[0]["data"]["code_verifier"] == "verifier"
    # ...while the consent screen still asks for sending as well.
    assert provider_tokens.MICROSOFT_SEND_SCOPE in provider_tokens.MICROSOFT_CONNECTION_SCOPES


def test_the_client_secret_is_sent_only_when_one_is_configured(token_endpoint, monkeypatch):
    provider_tokens.access_token_for(microsoft_config(), "imap")
    assert token_endpoint.calls[-1]["data"]["client_secret"] == "client-secret"

    monkeypatch.setattr(settings, "microsoft_client_secret", "")
    provider_tokens.access_token_for(microsoft_config(), "imap")
    assert "client_secret" not in token_endpoint.calls[-1]["data"]


def test_an_unknown_purpose_is_refused_rather_than_given_some_other_scope(token_endpoint):
    with pytest.raises(ValueError, match="purpose"):
        provider_tokens.access_token_for(microsoft_config(), "calendar")
    assert token_endpoint.calls == []


def test_a_revoked_sign_in_is_an_authentication_error_that_does_not_echo_the_token(token_endpoint):
    token_endpoint.responses.append(
        FakeResponse(
            400,
            {
                "error": "invalid_grant",
                "error_description": "AADSTS70008: The refresh token has expired.\r\nTrace ID: abc",
            },
        )
    )

    with pytest.raises(provider_tokens.ProviderAuthError) as raised:
        provider_tokens.access_token_for(microsoft_config("super-secret-refresh-token"), "imap")

    message = str(raised.value)
    assert "invalid_grant" in message
    assert "AADSTS70008" in message
    assert "super-secret-refresh-token" not in message
    assert "\n" not in message
    # The classifier that labels sync failures keys on this word.
    assert "authentication" in message.lower()


@pytest.mark.parametrize("status", [429, 500, 503])
def test_a_struggling_token_endpoint_is_not_blamed_on_the_user(token_endpoint, status):
    token_endpoint.responses.append(FakeResponse(status, {"error": "temporarily_unavailable"}))

    with pytest.raises(RuntimeError) as raised:
        provider_tokens.access_token_for(microsoft_config(), "imap")

    # Retrying later is right; asking the owner to sign in again is not.
    assert not isinstance(raised.value, provider_tokens.ProviderAuthError)


def test_an_unreachable_token_endpoint_is_a_transient_failure(monkeypatch, microsoft_settings):
    def unreachable(url, *, data, timeout):
        raise provider_tokens.httpx.ConnectError("no route")

    monkeypatch.setattr(provider_tokens.httpx, "post", unreachable)

    with pytest.raises(RuntimeError) as raised:
        provider_tokens.access_token_for(microsoft_config(), "imap")

    assert not isinstance(raised.value, provider_tokens.ProviderAuthError)


def test_a_missing_client_registration_is_reported_as_server_configuration(monkeypatch):
    monkeypatch.setattr(settings, "microsoft_client_id", "")

    with pytest.raises(provider_tokens.ProviderNotConfigured):
        provider_tokens.access_token_for(microsoft_config(), "imap")


def test_google_credentials_still_go_through_the_google_refresh(monkeypatch):
    seen = []
    monkeypatch.setattr(
        provider_tokens, "refresh_access_token", lambda ciphertext: seen.append(ciphertext) or "google-token"
    )
    google = SimpleNamespace(
        id=1,
        provider="gmail",
        auth_type="oauth2",
        credential_ciphertext=provider_tokens.encode_oauth_credential("google-refresh"),
    )

    assert provider_tokens.access_token_for(google, "imap") == "google-token"
    assert seen == [google.credential_ciphertext]


# ---- rotation -------------------------------------------------------------------------------------------


@pytest.fixture
def stored_account(monkeypatch, microsoft_settings):
    """A real row in a shared in-memory database, reachable through SessionLocal."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr("src.database.connection.SessionLocal", factory)
    with factory() as db:
        owner = User(google_sub="owner", email="owner@example.com")
        db.add(owner)
        db.flush()
        account = SMTPConfig(
            owner_user_id=owner.id,
            provider="microsoft",
            auth_type="oauth2",
            name="Microsoft - owner@example.com",
            host="outlook.office365.com",
            username="owner@example.com",
            credential_ciphertext=provider_tokens.encode_microsoft_credential("refresh-1", "tenant-id"),
        )
        db.add(account)
        db.commit()
        account_id = account.id
    return SimpleNamespace(factory=factory, id=account_id)


def stored_refresh_token(stored_account):
    with stored_account.factory() as db:
        account = db.get(SMTPConfig, stored_account.id)
        return json.loads(decrypt_secret(account.credential_ciphertext))["refresh_token"]


def detached_view(stored_account):
    with stored_account.factory() as db:
        account = db.get(SMTPConfig, stored_account.id)
        return SimpleNamespace(
            id=account.id,
            provider=account.provider,
            auth_type=account.auth_type,
            credential_ciphertext=account.credential_ciphertext,
        )


def test_a_rotated_refresh_token_replaces_the_stored_one(token_endpoint, stored_account):
    token_endpoint.responses.append(
        FakeResponse(200, {"access_token": "a", "refresh_token": "refresh-2", "expires_in": 3600})
    )
    config = detached_view(stored_account)

    provider_tokens.access_token_for(config, "imap")

    assert stored_refresh_token(stored_account) == "refresh-2"
    # The caller's own copy follows, so a second use in the same process does
    # not redeem a token that has already been replaced.
    assert json.loads(decrypt_secret(config.credential_ciphertext))["refresh_token"] == "refresh-2"


def test_a_stale_rotation_never_overwrites_a_newer_credential(token_endpoint, stored_account):
    """Two workers refresh at once: whoever lost the race must not roll the credential back."""
    config = detached_view(stored_account)
    # The other worker (or a reconnect) stored something newer first.
    with stored_account.factory() as db:
        account = db.get(SMTPConfig, stored_account.id)
        account.credential_ciphertext = provider_tokens.encode_microsoft_credential("refresh-newer", "tenant-id")
        db.commit()
    token_endpoint.responses.append(
        FakeResponse(200, {"access_token": "a", "refresh_token": "refresh-stale-rotation", "expires_in": 3600})
    )

    assert provider_tokens.access_token_for(config, "imap") == "a"

    assert stored_refresh_token(stored_account) == "refresh-newer"


def test_an_unchanged_refresh_token_writes_nothing(token_endpoint, stored_account):
    token_endpoint.responses.append(
        FakeResponse(200, {"access_token": "a", "refresh_token": "refresh-1", "expires_in": 3600})
    )
    config = detached_view(stored_account)
    before = config.credential_ciphertext

    provider_tokens.access_token_for(config, "imap")

    assert config.credential_ciphertext == before


def test_failing_to_store_a_rotation_never_costs_the_caller_its_token(token_endpoint, monkeypatch):
    token_endpoint.responses.append(
        FakeResponse(200, {"access_token": "still-usable", "refresh_token": "rotated", "expires_in": 3600})
    )

    class BrokenFactory:
        @staticmethod
        def begin():
            raise RuntimeError("database is down")

    monkeypatch.setattr("src.database.connection.SessionLocal", BrokenFactory)

    # The old refresh token remains valid, so the sync pass must carry on.
    assert provider_tokens.access_token_for(microsoft_config(account_id=7), "imap") == "still-usable"


def test_a_response_without_an_access_token_is_an_error(token_endpoint):
    token_endpoint.responses.append(FakeResponse(200, {"refresh_token": "x"}))

    with pytest.raises(RuntimeError, match="access token"):
        provider_tokens.access_token_for(microsoft_config(), "imap")
