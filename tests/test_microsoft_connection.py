"""Connecting a Microsoft mailbox: the consent redirect, and what the callback is allowed to believe."""

import base64
import hashlib
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.config import settings
from src.handlers import email_handler
from src.models.base import Base
from src.models.smtp_config import SMTPConfig
from src.models.user import User
from src.security import provider_tokens
from src.security.account_connect_tokens import (
    issue_account_connect_token,
    verify_account_connect_token,
)
from src.security.crypto import decrypt_secret

TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT_ID = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(autouse=True)
def microsoft_settings(monkeypatch):
    monkeypatch.setattr(settings, "microsoft_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "microsoft_client_secret", "client-secret")
    monkeypatch.setattr(settings, "microsoft_tenant", "common")
    monkeypatch.setattr(settings, "public_base_url", "https://mail.example.com")


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def owner(db):
    user = User(google_sub="owner", email="owner@example.com")
    db.add(user)
    db.commit()
    return user


def start(user):
    request = SimpleNamespace(session={})
    response = email_handler._start_microsoft_connection(request, user)
    return request, response


def id_token(**overrides):
    claims = {
        "aud": CLIENT_ID,
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "tid": TENANT,
        "oid": "33333333-3333-3333-3333-333333333333",
        "email": "Owner@Contoso.com",
        "name": "Owner",
        "exp": int(time.time()) + 3600,
        "nonce": "the-nonce",
    }
    claims.update(overrides)
    claims = {key: value for key, value in claims.items() if value is not None}

    def segment(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{segment({'alg': 'RS256'})}.{segment(claims)}.signature"


GRANTED = "https://outlook.office.com/IMAP.AccessAsUser.All https://outlook.office.com/Mail.Send"


@pytest.fixture
def microsoft(monkeypatch):
    """Microsoft's side: what the token endpoint returns, and what the follow-up connection test says."""
    state = SimpleNamespace(
        redeemed=[],
        tokens={"access_token": "access", "refresh_token": "new-refresh-token", "scope": GRANTED, "id_token": id_token()},
        connection={"imap": True, "smtp": True},
        failure=None,
    )

    def redeem(code, verifier, redirect_uri, tenant):
        state.redeemed.append({"code": code, "verifier": verifier, "redirect_uri": redirect_uri, "tenant": tenant})
        if state.failure:
            raise state.failure
        return state.tokens

    async def connection_test(account):
        return state.connection

    monkeypatch.setattr(email_handler, "redeem_microsoft_authorization_code", redeem)
    monkeypatch.setattr(email_handler, "safe_connection_test", connection_test)
    return state


def returning_request(request, **session):
    """The request Microsoft's redirect arrives as, in the session /connect left behind."""
    request.session.update(session)
    return request


async def callback(db, request, **query):
    query.setdefault("state", request.session.get("microsoft_oauth_state"))
    query.setdefault("code", "authorization-code")
    return await email_handler.microsoft_callback(request=request, db=db, **query)


# ---- the redirect to Microsoft ---------------------------------------------------------------------------


def test_the_consent_url_binds_the_request_and_asks_for_exactly_what_is_needed(owner):
    request, response = start(owner)

    target = urlparse(response.headers["location"])
    query = {key: values[0] for key, values in parse_qs(target.query).items()}
    assert f"{target.scheme}://{target.netloc}{target.path}" == "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    assert query["client_id"] == CLIENT_ID
    assert query["response_type"] == "code"
    assert query["redirect_uri"] == "https://mail.example.com/api/v1/accounts/microsoft/callback"
    assert set(query["scope"].split()) == {
        "openid",
        "profile",
        "email",
        "offline_access",
        "https://outlook.office.com/IMAP.AccessAsUser.All",
        "https://graph.microsoft.com/Mail.Send",
    }
    assert query["prompt"] == "select_account"
    assert query["state"] == request.session["microsoft_oauth_state"]
    assert query["nonce"] == request.session["microsoft_oauth_nonce"]
    # PKCE: the challenge is the SHA-256 of a verifier that never leaves the session.
    verifier = request.session["microsoft_oauth_code_verifier"]
    assert query["code_challenge_method"] == "S256"
    assert query["code_challenge"] == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert verifier not in response.headers["location"]
    assert request.session["microsoft_connect_user_id"] == owner.id


def test_every_connection_attempt_gets_its_own_state_nonce_and_verifier(owner):
    first, _ = start(owner)
    second, _ = start(owner)

    for key in ("microsoft_oauth_state", "microsoft_oauth_nonce", "microsoft_oauth_code_verifier"):
        assert first.session[key] != second.session[key]


def test_a_specific_tenant_narrows_who_can_sign_in(owner, monkeypatch):
    monkeypatch.setattr(settings, "microsoft_tenant", TENANT)

    _, response = start(owner)

    assert response.headers["location"].startswith(f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize?")


def test_a_server_without_a_microsoft_registration_says_so_instead_of_redirecting(owner, monkeypatch):
    monkeypatch.setattr(settings, "microsoft_client_id", "")

    with pytest.raises(HTTPException) as raised:
        start(owner)

    assert raised.value.status_code == 503


def test_a_gmail_connect_link_cannot_start_a_microsoft_connection():
    token = issue_account_connect_token(7)  # a link minted for Gmail

    with pytest.raises(ValueError):
        verify_account_connect_token(token, provider="microsoft")
    assert verify_account_connect_token(issue_account_connect_token(7, provider="microsoft"), provider="microsoft") == 7
    with pytest.raises(ValueError):
        verify_account_connect_token(issue_account_connect_token(7, provider="microsoft"))


@pytest.mark.asyncio
async def test_the_mcp_connect_link_starts_the_flow_for_the_user_it_was_issued_to(db, owner):
    request = SimpleNamespace(session={})

    response = await email_handler.connect_microsoft_from_mcp(
        request, issue_account_connect_token(owner.id, provider="microsoft"), db
    )

    assert response.headers["location"].startswith("https://login.microsoftonline.com/")
    assert request.session["microsoft_connect_user_id"] == owner.id


@pytest.mark.asyncio
async def test_the_mcp_connect_link_refuses_a_forged_or_foreign_token(db, owner):
    for token in ("garbage", issue_account_connect_token(owner.id)):
        with pytest.raises(HTTPException) as raised:
            await email_handler.connect_microsoft_from_mcp(SimpleNamespace(session={}), token, db)
        assert raised.value.status_code == 401


# ---- the callback ----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_successful_consent_creates_a_ready_to_sync_account(db, owner, microsoft):
    request, _ = start(owner)
    microsoft.tokens["id_token"] = id_token(nonce=request.session["microsoft_oauth_nonce"])

    response = await callback(db, request)

    account = db.query(SMTPConfig).one()
    assert account.owner_user_id == owner.id
    assert (account.provider, account.auth_type) == ("microsoft", "oauth2")
    assert account.provider_account_id == f"{TENANT}:33333333-3333-3333-3333-333333333333"
    # The address is lower-cased: it becomes the IMAP login and the From header.
    assert account.account_name == account.username == "owner@contoso.com"
    assert account.name == "Microsoft - owner@contoso.com"
    assert (account.host, account.port, account.imap_use_ssl) == ("outlook.office365.com", 993, True)
    assert account.enabled is True
    assert account.sync_state == "pending"
    assert json.loads(decrypt_secret(account.credential_ciphertext)) == {
        "provider": "microsoft",
        "refresh_token": "new-refresh-token",
        "tenant": TENANT,
    }
    assert response.headers["location"] == "https://mail.example.com?connected=microsoft"


@pytest.mark.asyncio
async def test_the_code_is_redeemed_with_this_sessions_verifier_and_the_registered_redirect(db, owner, microsoft):
    request, _ = start(owner)
    microsoft.tokens["id_token"] = id_token(nonce=request.session["microsoft_oauth_nonce"])
    verifier = request.session["microsoft_oauth_code_verifier"]

    await callback(db, request, code="the-code")

    assert microsoft.redeemed == [
        {
            "code": "the-code",
            "verifier": verifier,
            "redirect_uri": "https://mail.example.com/api/v1/accounts/microsoft/callback",
            "tenant": "common",
        }
    ]


@pytest.mark.asyncio
async def test_connecting_the_same_mailbox_again_refreshes_it_rather_than_duplicating_it(db, owner, microsoft):
    request, _ = start(owner)
    microsoft.tokens["id_token"] = id_token(nonce=request.session["microsoft_oauth_nonce"])
    await callback(db, request)
    account = db.query(SMTPConfig).one()
    account.enabled = False
    account.sync_state = "error"
    account.last_error_code = "ACCOUNT_AUTH_FAILED"
    account.consecutive_failures = 4
    account.backfill_complete = True
    db.commit()

    request, _ = start(owner)
    microsoft.tokens.update(refresh_token="second-refresh-token", id_token=id_token(nonce=request.session["microsoft_oauth_nonce"]))
    await callback(db, request)

    account = db.query(SMTPConfig).one()
    assert json.loads(decrypt_secret(account.credential_ciphertext))["refresh_token"] == "second-refresh-token"
    assert account.enabled is True
    assert account.sync_state == "healthy"  # its history is already indexed; this is not a fresh backfill
    assert account.last_error_code is None
    assert account.consecutive_failures == 0


@pytest.mark.asyncio
async def test_a_different_mailbox_in_the_same_tenant_is_a_different_account(db, owner, microsoft):
    for oid, email in (("11111111-0000-0000-0000-000000000001", "a@contoso.com"),
                       ("11111111-0000-0000-0000-000000000002", "b@contoso.com")):
        request, _ = start(owner)
        microsoft.tokens["id_token"] = id_token(nonce=request.session["microsoft_oauth_nonce"], oid=oid, email=email)
        await callback(db, request)

    assert sorted(account.account_name for account in db.query(SMTPConfig)) == ["a@contoso.com", "b@contoso.com"]


@pytest.mark.asyncio
async def test_a_name_already_taken_does_not_fail_the_connection(db, owner, microsoft):
    db.add(SMTPConfig(owner_user_id=owner.id, name="Microsoft - owner@contoso.com", host="x", username="x"))
    db.commit()
    request, _ = start(owner)
    microsoft.tokens["id_token"] = id_token(nonce=request.session["microsoft_oauth_nonce"])

    await callback(db, request)

    names = sorted(account.name for account in db.query(SMTPConfig))
    assert len(names) == 2 and names[0] == "Microsoft - owner@contoso.com"


@pytest.mark.asyncio
async def test_a_callback_without_the_matching_state_is_refused_and_creates_nothing(db, owner, microsoft):
    request, _ = start(owner)

    for state in (None, "forged-state"):
        request.session["microsoft_oauth_state"] = request.session.get("microsoft_oauth_state") or "x"
        with pytest.raises(HTTPException) as raised:
            await email_handler.microsoft_callback(request=request, state=state, code="c", db=db)
        assert raised.value.status_code == 400
    assert db.query(SMTPConfig).count() == 0
    assert microsoft.redeemed == []


@pytest.mark.asyncio
async def test_a_state_is_single_use(db, owner, microsoft):
    request, _ = start(owner)
    microsoft.tokens["id_token"] = id_token(nonce=request.session["microsoft_oauth_nonce"])
    state = request.session["microsoft_oauth_state"]
    await callback(db, request)

    with pytest.raises(HTTPException):
        await email_handler.microsoft_callback(request=request, state=state, code="c", db=db)


@pytest.mark.asyncio
async def test_an_identity_token_for_another_request_is_refused(db, owner, microsoft):
    request, _ = start(owner)
    microsoft.tokens["id_token"] = id_token(nonce="a-different-request")

    with pytest.raises(HTTPException) as raised:
        await callback(db, request)

    assert raised.value.status_code == 400
    assert db.query(SMTPConfig).count() == 0


@pytest.mark.asyncio
async def test_a_grant_without_imap_access_creates_nothing(db, owner, microsoft):
    request, _ = start(owner)
    microsoft.tokens.update(
        scope="https://graph.microsoft.com/Mail.Send",
        id_token=id_token(nonce=request.session["microsoft_oauth_nonce"]),
    )

    with pytest.raises(HTTPException) as raised:
        await callback(db, request)

    assert "IMAP" in raised.value.detail
    assert db.query(SMTPConfig).count() == 0


@pytest.mark.asyncio
async def test_no_refresh_token_means_no_account(db, owner, microsoft):
    request, _ = start(owner)
    microsoft.tokens.pop("refresh_token")
    microsoft.tokens["id_token"] = id_token(nonce=request.session["microsoft_oauth_nonce"])

    with pytest.raises(HTTPException) as raised:
        await callback(db, request)

    assert "refresh token" in raised.value.detail
    assert db.query(SMTPConfig).count() == 0


@pytest.mark.asyncio
async def test_a_failed_code_exchange_is_a_clean_400_that_leaks_nothing(db, owner, microsoft):
    request, _ = start(owner)
    microsoft.failure = provider_tokens.ProviderAuthError("invalid_grant AADSTS54005 secret-detail")

    with pytest.raises(HTTPException) as raised:
        await callback(db, request)

    assert raised.value.status_code == 400
    assert "secret-detail" not in raised.value.detail
    assert db.query(SMTPConfig).count() == 0


@pytest.mark.asyncio
async def test_declining_consent_ends_cleanly_and_clears_the_session(db, owner, microsoft):
    request, _ = start(owner)

    response = await email_handler.microsoft_callback(
        request=request,
        state=request.session["microsoft_oauth_state"],
        error="access_denied",
        error_description="AADSTS65004: The user declined to consent to access the app.",
        db=db,
    )

    assert response.status_code == 400
    assert "declined" in response.body.decode()
    assert db.query(SMTPConfig).count() == 0
    assert not {key for key in request.session if key.startswith("microsoft_")}


@pytest.mark.asyncio
async def test_an_organisation_that_needs_admin_approval_is_told_what_to_ask_for(db, owner, microsoft):
    request, _ = start(owner)

    response = await email_handler.microsoft_callback(
        request=request,
        state=request.session["microsoft_oauth_state"],
        error="access_denied",
        error_description="AADSTS65001: The user or administrator has not consented to use the application.",
        db=db,
    )

    assert "administrator" in response.body.decode()


@pytest.mark.asyncio
async def test_an_error_page_does_not_reflect_markup_from_the_query_string(db, owner, microsoft):
    request, _ = start(owner)

    response = await email_handler.microsoft_callback(
        request=request,
        state=request.session["microsoft_oauth_state"],
        error="server_error",
        error_description="<script>alert(1)</script>",
        db=db,
    )

    assert "<script>" not in response.body.decode()


@pytest.mark.asyncio
async def test_a_mailbox_that_refuses_imap_is_saved_but_the_owner_is_told_at_once(db, owner, microsoft):
    request, _ = start(owner)
    microsoft.tokens["id_token"] = id_token(nonce=request.session["microsoft_oauth_nonce"])
    microsoft.connection = {"imap": False, "smtp": True}

    response = await callback(db, request)

    account = db.query(SMTPConfig).one()
    assert account.sync_state == "error"
    assert account.last_error_code == "CONNECTION_TEST_FAILED"
    assert response.headers["location"].endswith("?connected=microsoft&problem=imap")


@pytest.mark.parametrize(
    ("connection", "problem"),
    [({"imap": True, "smtp": False}, "send"), ({"imap": False, "smtp": False}, "both")],
)
@pytest.mark.asyncio
async def test_the_problem_named_matches_what_failed(db, owner, microsoft, connection, problem):
    request, _ = start(owner)
    microsoft.tokens["id_token"] = id_token(nonce=request.session["microsoft_oauth_nonce"])
    microsoft.connection = connection

    response = await callback(db, request)

    assert response.headers["location"].endswith(f"&problem={problem}")


@pytest.mark.asyncio
async def test_the_account_limit_applies_to_microsoft_mailboxes_too(db, owner, microsoft, monkeypatch):
    monkeypatch.setattr(settings, "max_accounts_per_user", 0)
    request, _ = start(owner)
    microsoft.tokens["id_token"] = id_token(nonce=request.session["microsoft_oauth_nonce"])

    with pytest.raises(HTTPException) as raised:
        await callback(db, request)

    assert "limit" in raised.value.detail


# ---- the landing page ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_landing_page_confirms_a_connection():
    from src.server import root

    response = await root(connected="microsoft")

    assert response.status_code == 200
    assert "Microsoft 365 is ready" in response.body.decode()


@pytest.mark.parametrize("problem", ["imap", "send", "both"])
@pytest.mark.asyncio
async def test_the_landing_page_explains_a_connection_that_does_not_work(problem):
    from src.server import root

    html = (await root(connected="microsoft", problem=problem)).body.decode()

    assert "not working yet" in html
    assert "Microsoft 365 is ready" not in html


@pytest.mark.asyncio
async def test_the_landing_page_never_echoes_the_problem_it_was_given():
    from src.server import root

    html = (await root(connected="microsoft", problem="<script>alert(1)</script>")).body.decode()

    assert "<script>" not in html
    assert "Microsoft 365 is ready" in html
