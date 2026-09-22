"""Short-lived signed attachment download grants."""

import base64
import binascii
import hashlib
import hmac
import json
import time

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


PURPOSE = "attachment_download"


def issue_download_token(user_id: int, attachment_id: int) -> str:
    payload = json.dumps(
        {
            "purpose": PURPOSE,
            "user_id": user_id,
            "attachment_id": attachment_id,
            "expires_at": int(time.time()) + settings.attachment_token_ttl_seconds,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    signature = hmac.new(_key(), payload, hashlib.sha256).digest()
    return f"{_encode_segment(payload)}.{_encode_segment(signature)}"


def verify_download_token(token: str, attachment_id: int) -> int:
    try:
        if "." in token:
            payload_segment, signature_segment = token.split(".", 1)
            payload = _decode_segment(payload_segment)
            signature = _decode_segment(signature_segment)
        else:
            # Compatibility for grants issued before the segmented format.
            raw = _decode_segment(token)
            payload, signature = raw.split(b".", 1)
        if not hmac.compare_digest(signature, hmac.new(_key(), payload, hashlib.sha256).digest()):
            raise ValueError
        claims = json.loads(payload)
        # Upload and download grants are signed with the same key, so only this
        # claim keeps a write grant from being spent as a read grant. A missing
        # purpose is accepted because tokens issued before the claim existed are
        # still inside their TTL; a present-but-wrong one never is. Drop the
        # fallback once the longest attachment TTL has elapsed past deployment.
        if claims.get("purpose", PURPOSE) != PURPOSE:
            raise ValueError
        if claims["attachment_id"] != attachment_id or claims["expires_at"] < int(time.time()):
            raise ValueError
        return int(claims["user_id"])
    except (binascii.Error, UnicodeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid or expired download token") from exc
