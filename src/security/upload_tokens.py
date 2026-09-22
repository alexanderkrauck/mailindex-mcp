"""Short-lived signed grants for outbound attachment uploads.

An AI client cannot hand the server bytes: they would have to cross the MCP
conversation. It asks for an upload slot instead and PUTs the bytes to the URL
this token signs, mirroring attachment downloads in reverse.
"""

import base64
import binascii
import hashlib
import hmac
import json
import time

from src.config import settings
from src.security.crypto import persistent_secret

PURPOSE = "outbound_upload"


def _key() -> bytes:
    return persistent_secret(settings.session_secret, "session.key").encode()


def _encode_segment(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode_segment(value: str) -> bytes:
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if _encode_segment(decoded) != value:
        raise binascii.Error("Non-canonical base64url segment")
    return decoded


def issue_upload_token(user_id: int, upload_id: str) -> str:
    payload = json.dumps(
        {
            "purpose": PURPOSE,
            "user_id": user_id,
            "upload_id": upload_id,
            "expires_at": int(time.time()) + settings.outbound_upload_ttl_seconds,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    signature = hmac.new(_key(), payload, hashlib.sha256).digest()
    return f"{_encode_segment(payload)}.{_encode_segment(signature)}"


def verify_upload_token(token: str, upload_id: str) -> int:
    """Return the owner id of a valid upload grant, or raise ValueError.

    This grant authorises a write to the data volume, so the purpose claim is
    mandatory: unlike the download verifier there is no legacy fleet of
    purposeless upload tokens to accommodate, and never will be.
    """
    try:
        payload_segment, signature_segment = token.split(".", 1)
        payload = _decode_segment(payload_segment)
        signature = _decode_segment(signature_segment)
        if not hmac.compare_digest(signature, hmac.new(_key(), payload, hashlib.sha256).digest()):
            raise ValueError
        claims = json.loads(payload)
        if (
            claims.get("purpose") != PURPOSE
            or claims["upload_id"] != upload_id
            or claims["expires_at"] < int(time.time())
        ):
            raise ValueError
        return int(claims["user_id"])
    except (
        binascii.Error,
        AttributeError,
        UnicodeError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
    ) as exc:
        raise ValueError("Invalid or expired upload token") from exc
