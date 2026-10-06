"""Short-lived grants for provider and mailbox credential connection flows."""

import base64
import binascii
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from src.config import settings
from src.security.crypto import persistent_secret


def _key() -> bytes:
    return persistent_secret(settings.session_secret, "session.key").encode()


def _encode_segment(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode_segment(value: str) -> bytes:
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if _encode_segment(decoded) != value:
        raise binascii.Error("Non-canonical base64url segment")
    return decoded


def _issue(claims: dict) -> str:
    payload = json.dumps(
        claims,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    signature = hmac.new(_key(), payload, hashlib.sha256).digest()
    return f"{_encode_segment(payload)}.{_encode_segment(signature)}"


def _verify(token: str) -> dict:
    try:
        payload_segment, signature_segment = token.split(".", 1)
        payload = _decode_segment(payload_segment)
        signature = _decode_segment(signature_segment)
        expected = hmac.new(_key(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        claims = json.loads(payload)
        if claims["expires_at"] < int(time.time()):
            raise ValueError
        return claims
    except (
        binascii.Error,
        UnicodeError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
    ) as exc:
        raise ValueError("Invalid or expired account connection token") from exc


def issue_account_connect_token(user_id: int, provider: str = "gmail") -> str:
    # The provider is part of the purpose, not just a parameter of the route: all
    # signed grants share one key, so a link minted to start one provider's
    # consent flow must not be spendable on another's.
    return _issue(
        {
            "purpose": f"{provider}-account-connect",
            "user_id": user_id,
            "expires_at": int(time.time()) + settings.account_connect_token_ttl_seconds,
        }
    )


def verify_account_connect_token(token: str, provider: str = "gmail") -> int:
    claims = _verify(token)
    if claims.get("purpose") != f"{provider}-account-connect":
        raise ValueError("Invalid or expired account connection token")
    return int(claims["user_id"])


def credential_version(credential_ciphertext: str | None) -> str:
    value = credential_ciphertext or ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class PasswordSetupClaims:
    user_id: int
    account_id: int


def issue_password_setup_token(
    user_id: int,
    account_id: int,
    credential_ciphertext: str | None,
) -> str:
    return _issue(
        {
            "purpose": "mail-password-setup",
            "user_id": user_id,
            "account_id": account_id,
            "credential_version": credential_version(credential_ciphertext),
            "expires_at": int(time.time()) + settings.password_setup_token_ttl_seconds,
        }
    )


def verify_password_setup_token(
    token: str,
    account_id: int,
    credential_ciphertext: str | None,
) -> PasswordSetupClaims:
    claims = _verify(token)
    try:
        if (
            claims.get("purpose") != "mail-password-setup"
            or int(claims["account_id"]) != account_id
            or not hmac.compare_digest(
                str(claims["credential_version"]),
                credential_version(credential_ciphertext),
            )
        ):
            raise ValueError
        return PasswordSetupClaims(
            user_id=int(claims["user_id"]),
            account_id=int(claims["account_id"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid or expired password setup link") from exc
