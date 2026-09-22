"""Signed grants for outbound attachment uploads, and their purpose separation."""

import base64
import hashlib
import hmac
import json
import time

import pytest


def _sign(claims: dict) -> str:
    """Mint a token the server would accept as authentic, with arbitrary claims.

    Models an attacker who holds no key but can influence what the server puts
    into a payload, plus the legacy grants that predate a claim. Deliberately
    built from the download module's helpers so the download-side compatibility
    tests do not depend on the upload module existing.
    """
    from src.security.download_tokens import _encode_segment, _key

    payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    signature = hmac.new(_key(), payload, hashlib.sha256).digest()
    return f"{_encode_segment(payload)}.{_encode_segment(signature)}"


def _claims_of(token: str) -> dict:
    segment = token.split(".", 1)[0]
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


def test_upload_token_round_trip_returns_the_owner():
    from src.security.upload_tokens import issue_upload_token, verify_upload_token

    token = issue_upload_token(user_id=7, upload_id="up_9f3a2b")

    assert token.count(".") == 1
    assert verify_upload_token(token, upload_id="up_9f3a2b") == 7


def test_upload_token_is_bound_to_one_upload_id():
    from src.security.upload_tokens import issue_upload_token, verify_upload_token

    token = issue_upload_token(user_id=7, upload_id="up_aaa")

    with pytest.raises(ValueError):
        verify_upload_token(token, upload_id="up_bbb")


def test_upload_token_uses_the_outbound_upload_ttl_and_expires():
    from src.config import settings
    from src.security.upload_tokens import issue_upload_token

    before = int(time.time())
    token = issue_upload_token(user_id=7, upload_id="up_ttl")

    assert _claims_of(token)["expires_at"] >= before + settings.outbound_upload_ttl_seconds


def test_expired_upload_token_is_rejected(monkeypatch):
    from src.security import upload_tokens

    token = upload_tokens.issue_upload_token(user_id=7, upload_id="up_expired")
    expiry = _claims_of(token)["expires_at"]

    class _Clock:
        @staticmethod
        def time() -> float:
            return expiry + 1

    monkeypatch.setattr(upload_tokens, "time", _Clock)
    with pytest.raises(ValueError):
        upload_tokens.verify_upload_token(token, upload_id="up_expired")


def test_tampered_signature_is_rejected():
    from src.security.upload_tokens import issue_upload_token, verify_upload_token

    token = issue_upload_token(user_id=7, upload_id="up_sig")
    payload_segment, signature_segment = token.split(".", 1)
    flipped = signature_segment[:-1] + ("A" if signature_segment[-1] != "A" else "B")

    with pytest.raises(ValueError):
        verify_upload_token(f"{payload_segment}.{flipped}", upload_id="up_sig")


def test_tampered_payload_is_rejected():
    """Re-encoding a raised user_id keeps the token well-formed but unsigned."""
    from src.security.upload_tokens import _encode_segment, issue_upload_token, verify_upload_token

    token = issue_upload_token(user_id=7, upload_id="up_payload")
    claims = _claims_of(token)
    claims["user_id"] = 1
    forged = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()

    with pytest.raises(ValueError):
        verify_upload_token(f"{_encode_segment(forged)}.{token.split('.', 1)[1]}", upload_id="up_payload")


@pytest.mark.parametrize(
    "token",
    [
        "",
        ".",
        "...",
        "not-a-token",
        "garbage.garbage",
        "!!!!.????",
        "a.b.c",
        "eyJ1c2VyX2lkIjo3fQ",  # unsigned payload, segmented format absent
        "*" * 64,
        None,
        12345,
    ],
)
def test_malformed_input_raises_value_error_only(token):
    from src.security.upload_tokens import verify_upload_token

    with pytest.raises(ValueError, match="Invalid or expired upload token"):
        verify_upload_token(token, upload_id="up_any")


def test_issued_tokens_carry_an_explicit_purpose_claim():
    from src.security.download_tokens import issue_download_token
    from src.security.upload_tokens import issue_upload_token

    assert _claims_of(issue_upload_token(user_id=7, upload_id="up_p"))["purpose"] == "outbound_upload"
    assert _claims_of(issue_download_token(user_id=7, attachment_id=42))["purpose"] == "attachment_download"


def test_download_token_is_rejected_by_the_upload_verifier():
    """Both kinds are signed with one key: only the purpose claim separates them."""
    from src.security.download_tokens import issue_download_token
    from src.security.upload_tokens import verify_upload_token

    # An id the attacker steers so the stolen read grant names the upload slot.
    token = issue_download_token(user_id=7, attachment_id="up_crossed")

    with pytest.raises(ValueError, match="Invalid or expired upload token"):
        verify_upload_token(token, upload_id="up_crossed")


def test_purpose_alone_rejects_an_otherwise_perfect_upload_token():
    """Isolates the purpose check: every other claim is exactly what upload wants.

    Without this, cross-purpose rejection could rest only on the id claim being
    named differently, which an attacker who shapes the payload could satisfy.
    """
    from src.security.upload_tokens import verify_upload_token

    token = _sign(
        {
            "purpose": "attachment_download",
            "user_id": 7,
            "upload_id": "up_perfect",
            "attachment_id": "up_perfect",
            "expires_at": int(time.time()) + 900,
        }
    )

    with pytest.raises(ValueError, match="Invalid or expired upload token"):
        verify_upload_token(token, upload_id="up_perfect")


def test_upload_token_is_rejected_by_the_download_verifier():
    from src.security.download_tokens import verify_download_token
    from src.security.upload_tokens import issue_upload_token

    token = issue_upload_token(user_id=7, upload_id=42)

    with pytest.raises(ValueError, match="Invalid or expired download token"):
        verify_download_token(token, attachment_id=42)


def test_a_purposeless_upload_token_is_rejected():
    """Absent purpose is never acceptable for a write grant, only for legacy reads."""
    from src.security.upload_tokens import verify_upload_token

    token = _sign(
        {
            "user_id": 7,
            "upload_id": "up_nopurpose",
            "expires_at": int(time.time()) + 900,
        }
    )

    with pytest.raises(ValueError, match="Invalid or expired upload token"):
        verify_upload_token(token, upload_id="up_nopurpose")


def test_legacy_download_token_without_a_purpose_claim_still_verifies():
    """Grants minted before the claim existed are in flight and must keep working."""
    from src.security.download_tokens import verify_download_token

    legacy = _sign(
        {
            "user_id": 7,
            "attachment_id": 42,
            "expires_at": int(time.time()) + 300,
        }
    )

    assert verify_download_token(legacy, attachment_id=42) == 7


def test_legacy_segmentless_download_token_still_verifies():
    """The pre-segmented wire format keeps its own compatibility branch."""
    from src.security.download_tokens import _encode_segment, _key, verify_download_token

    payload = json.dumps(
        {"attachment_id": 42, "expires_at": int(time.time()) + 300, "user_id": 7},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    signature = hmac.new(_key(), payload, hashlib.sha256).digest()

    assert verify_download_token(_encode_segment(payload + b"." + signature), attachment_id=42) == 7


def test_download_token_with_a_wrong_purpose_is_rejected():
    """Missing is tolerated for compatibility; present-but-wrong never is."""
    from src.security.download_tokens import verify_download_token

    token = _sign(
        {
            "purpose": "outbound_upload",
            "user_id": 7,
            "attachment_id": 42,
            "expires_at": int(time.time()) + 300,
        }
    )

    with pytest.raises(ValueError, match="Invalid or expired download token"):
        verify_download_token(token, attachment_id=42)

