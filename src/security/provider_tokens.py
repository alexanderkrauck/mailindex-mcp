"""Mailbox-provider OAuth credentials: Google and Microsoft.

A stored credential is an encrypted JSON document, and what it holds differs on
purpose. Google's carries the whole client, which is how the first mailboxes were
connected. Microsoft's carries only the refresh token and the tenant it was
issued in: the app registration's secret stays in the server's environment, so
rotating it -- they expire, by design -- does not mean connecting every mailbox
again.
"""

import base64
import json
import logging
import re
import time

import httpx

from src.config import settings
from src.security.crypto import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)

GMAIL_MAIL_SCOPE = "https://mail.google.com/"

MICROSOFT_AUTHORITY = "https://login.microsoftonline.com"
# IMAP is served by Exchange Online, whose own resource is outlook.office.com.
MICROSOFT_IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All"
# Sending goes through Microsoft Graph rather than SMTP AUTH: Exchange Online
# ships with SMTP AUTH switched off for the whole organisation, and turning it on
# is a tenant-wide security decision this server has no business asking for.
MICROSOFT_SEND_SCOPE = "https://graph.microsoft.com/Mail.Send"
_MICROSOFT_IDENTITY_SCOPES = ("openid", "profile", "email", "offline_access")
# What the consent screen asks for: both resources, so one approval covers
# reading and sending.
MICROSOFT_CONNECTION_SCOPES = (*_MICROSOFT_IDENTITY_SCOPES, MICROSOFT_IMAP_SCOPE, MICROSOFT_SEND_SCOPE)
# What redeeming the resulting code asks for. The token endpoint accepts scopes
# from one resource only (AADSTS28000 otherwise), so this names Exchange alone;
# the refresh token that comes back still covers everything that was consented
# to, and each later use asks for the resource it needs.
_MICROSOFT_REDEEM_SCOPES = (*_MICROSOFT_IDENTITY_SCOPES, MICROSOFT_IMAP_SCOPE)
# An access token is issued for one resource, so each use asks for its own.
_MICROSOFT_PURPOSE_SCOPES = {"imap": MICROSOFT_IMAP_SCOPE, "send": MICROSOFT_SEND_SCOPE}

_TENANT = re.compile(r"[A-Za-z0-9.-]{1,128}")
_TOKEN_REQUEST_TIMEOUT = 20.0


class ProviderAuthError(RuntimeError):
    """The provider refused the stored sign-in, so the mailbox has to be connected again."""


class ProviderNotConfigured(RuntimeError):
    """This server has no registration with the provider; nothing the user can do will help."""


def encode_oauth_credential(refresh_token: str, scopes: list[str] | None = None) -> str:
    payload = {
        "refresh_token": refresh_token,
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "scopes": scopes or [GMAIL_MAIL_SCOPE],
    }
    return encrypt_secret(json.dumps(payload, separators=(",", ":"), sort_keys=True))


def refresh_access_token(credential_ciphertext: str) -> str:
    """Refresh a Google mailbox token from an encrypted provider credential."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    payload = json.loads(decrypt_secret(credential_ciphertext))
    credentials = Credentials(
        token=None,
        refresh_token=payload["refresh_token"],
        token_uri=payload["token_uri"],
        client_id=payload["client_id"],
        client_secret=payload["client_secret"],
        scopes=payload.get("scopes"),
    )
    credentials.refresh(Request())
    if not credentials.token:
        raise RuntimeError("Provider did not return an access token")
    return credentials.token


# ---- Microsoft ---------------------------------------------------------------------------------------


def microsoft_configured() -> bool:
    return bool(settings.microsoft_client_id)


def encode_microsoft_credential(refresh_token: str, tenant: str) -> str:
    payload = {"provider": "microsoft", "refresh_token": refresh_token, "tenant": tenant}
    return encrypt_secret(json.dumps(payload, separators=(",", ":"), sort_keys=True))


def microsoft_endpoint(tenant: str, name: str) -> str:
    """An OAuth endpoint of the tenant. The tenant is a path segment, so it is checked."""
    if not _TENANT.fullmatch(tenant or ""):
        raise ValueError("Microsoft tenant must be a tenant ID, a domain, or common/organizations/consumers")
    return f"{MICROSOFT_AUTHORITY}/{tenant}/oauth2/v2.0/{name}"


def _one_line(value: object, limit: int = 300) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def microsoft_token_request(form: dict[str, str], tenant: str) -> dict:
    """POST to the token endpoint and sort a refusal from an outage.

    The distinction decides what the owner is told. A 400 from the token endpoint
    is Microsoft saying this sign-in is no good -- revoked, expired, password
    changed -- and only signing in again helps. A 429 or 5xx, or no answer at
    all, says nothing about the sign-in, and the sync pass that hit it simply
    retries; blaming the user for it would send them off to fix something that
    is not broken.
    """
    if not microsoft_configured():
        raise ProviderNotConfigured(
            "This server has no Microsoft app registration; set EMAILSERVER_MICROSOFT_CLIENT_ID."
        )
    data = {"client_id": settings.microsoft_client_id, **form}
    if settings.microsoft_client_secret:
        data["client_secret"] = settings.microsoft_client_secret
    try:
        response = httpx.post(
            microsoft_endpoint(tenant, "token"), data=data, timeout=_TOKEN_REQUEST_TIMEOUT
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Microsoft sign-in service unreachable ({type(exc).__name__})") from exc
    if response.status_code == 200:
        return response.json()
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if response.status_code in (400, 401, 403):
        raise ProviderAuthError(
            "Microsoft authentication refused the stored sign-in "
            f"({_one_line(body.get('error'), 64) or response.status_code}: "
            f"{_one_line(body.get('error_description'))}). "
            "Connect the mailbox again with begin_microsoft_connection."
        )
    raise RuntimeError(f"Microsoft sign-in service answered {response.status_code}")


def redeem_microsoft_authorization_code(code: str, code_verifier: str, redirect_uri: str, tenant: str) -> dict:
    """Exchange the authorization code for tokens; the consent screen's one outcome."""
    return microsoft_token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "scope": " ".join(_MICROSOFT_REDEEM_SCOPES),
        },
        tenant,
    )


def microsoft_identity(token_response: dict, *, nonce: str) -> dict:
    """Who signed in, read from the ID token that came back with the tokens.

    The signature is not checked, and that is deliberate rather than an
    oversight: OpenID Connect Core 3.1.3.7 lets a client that received the token
    straight from the token endpoint over TLS rely on the TLS server's identity
    instead. Everything a *forged* token could change is still checked -- who it
    is for, who issued it, that it is current, and that it answers this very
    request -- so a token replayed from elsewhere is refused.
    """
    raw = token_response.get("id_token")
    if not isinstance(raw, str) or raw.count(".") != 2:
        raise ValueError("Microsoft did not return an identity token")
    try:
        segment = raw.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Microsoft returned an unreadable identity token") from exc
    if not isinstance(claims, dict):
        raise ValueError("Microsoft returned an unreadable identity token")

    tenant = str(claims.get("tid") or "")
    if (
        claims.get("aud") != settings.microsoft_client_id
        or not _TENANT.fullmatch(tenant)
        or claims.get("iss") != f"{MICROSOFT_AUTHORITY}/{tenant}/v2.0"
        or not isinstance(claims.get("exp"), int)
        or claims["exp"] < time.time()
        or claims.get("nonce") != nonce
    ):
        raise ValueError("Microsoft returned an identity token that does not belong to this request")

    address = str(claims.get("email") or claims.get("preferred_username") or "").strip().lower()
    subject = str(claims.get("oid") or claims.get("sub") or "")
    if "@" not in address or not subject:
        raise ValueError("Microsoft did not report which mailbox signed in")
    return {
        # Object ids are only unique within a tenant, hence the pair.
        "subject": f"{tenant}:{subject}",
        "email": address,
        "tenant": tenant,
        "name": str(claims.get("name") or ""),
    }


def _store_rotated_refresh_token(config, old_ciphertext: str, payload: dict, new_refresh_token: str) -> None:
    """Keep the newest refresh token. Never raises: the old one still works.

    Microsoft hands back a fresh refresh token on every redemption, and a token
    that is never replaced ages out. The write is compare-and-swap on the exact
    ciphertext this caller read, so a worker that lost a race, or a mailbox
    that was reconnected in the meantime, cannot be rolled back to an older
    credential by a straggler.
    """
    account_id = getattr(config, "id", None)
    new_ciphertext = encode_microsoft_credential(new_refresh_token, payload.get("tenant") or "common")
    try:
        if account_id is not None:
            from src.database.connection import SessionLocal
            from src.models.smtp_config import SMTPConfig

            with SessionLocal.begin() as db:
                stored = (
                    db.query(SMTPConfig)
                    .filter(
                        SMTPConfig.id == account_id,
                        SMTPConfig.credential_ciphertext == old_ciphertext,
                    )
                    .update(
                        {SMTPConfig.credential_ciphertext: new_ciphertext},
                        synchronize_session=False,
                    )
                )
            if not stored:
                logger.info("Account %s was reconnected during a token refresh; keeping its newer credential", account_id)
                return
        config.credential_ciphertext = new_ciphertext
    except Exception as exc:
        logger.warning(
            "Could not store the rotated Microsoft refresh token for account %s (%s); "
            "the previous one remains valid",
            account_id,
            type(exc).__name__,
        )


def access_token_for(config, purpose: str = "imap") -> str:
    """An access token for one use of a mailbox, whichever provider issued its credential.

    ``purpose`` names what the token is for. Microsoft issues one token per
    resource, so it decides the scope; Google has one token for everything and
    ignores it.
    """
    ciphertext = config.credential_ciphertext
    payload = json.loads(decrypt_secret(ciphertext))
    if payload.get("provider") != "microsoft":
        return refresh_access_token(ciphertext)

    scope = _MICROSOFT_PURPOSE_SCOPES.get(purpose)
    if scope is None:
        raise ValueError(f"Unsupported Microsoft token purpose: {purpose!r}")
    response = microsoft_token_request(
        {
            "grant_type": "refresh_token",
            "refresh_token": payload["refresh_token"],
            "scope": scope,
        },
        payload.get("tenant") or settings.microsoft_tenant,
    )
    token = response.get("access_token")
    if not token:
        raise RuntimeError("Microsoft did not return an access token")
    rotated = response.get("refresh_token")
    if rotated and rotated != payload["refresh_token"]:
        _store_rotated_refresh_token(config, ciphertext, payload, rotated)
    return token
