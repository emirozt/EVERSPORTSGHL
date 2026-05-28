"""
Signed preference-centre tokens (M6).

Each contact gets a unique URL:
  GET /api/v1/consent/preference-centre/{token}

The token is an HMAC-SHA256-signed, URL-safe base64-encoded payload:
  {ghl_contact_id}:{location_id}:{expires_ts}

Expiry: 90 days from generation; tokens are refreshable on each view.

The signing secret is derived from SECRET_KEY in settings.  Tokens are
stateless — no DB lookup required to verify.

Usage:
    token = generate_token(ghl_contact_id, location_id)
    payload = verify_token(token)   # raises TokenError on bad/expired
    # payload.ghl_contact_id, payload.location_id

References:
  - requirements_v2/08_consent_model.md § "Preference centre"
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Token lifetime in seconds (90 days)
TOKEN_TTL_SECONDS = 90 * 24 * 60 * 60
SEPARATOR = ":"


class TokenError(Exception):
    """Raised when a token is invalid, tampered, or expired."""


@dataclass(frozen=True)
class TokenPayload:
    ghl_contact_id: str
    location_id: str
    expires_at: int  # unix timestamp


def _get_secret() -> bytes:
    """Load the signing secret from application settings."""
    from app.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    secret = getattr(settings, "secret_key", None)
    if not secret:
        raise RuntimeError("SECRET_KEY not configured — cannot sign preference-centre tokens")
    return secret.encode() if isinstance(secret, str) else secret


def _sign(payload: str, secret: bytes) -> str:
    """Return URL-safe base64 HMAC-SHA256 signature."""
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).rstrip(b"=").decode()


def generate_token(ghl_contact_id: str, location_id: str) -> str:
    """
    Generate a signed, 90-day preference-centre token.

    Args:
        ghl_contact_id: GHL contact ID string.
        location_id:    Location UUID string.

    Returns:
        URL-safe token string.
    """
    expires_at = int(time.time()) + TOKEN_TTL_SECONDS
    payload = f"{ghl_contact_id}{SEPARATOR}{location_id}{SEPARATOR}{expires_at}"
    secret = _get_secret()
    sig = _sign(payload, secret)
    raw = f"{payload}{SEPARATOR}{sig}"
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()


def verify_token(token: str) -> TokenPayload:
    """
    Verify and decode a preference-centre token.

    Args:
        token: The raw token string from the URL.

    Returns:
        TokenPayload with ghl_contact_id, location_id, expires_at.

    Raises:
        TokenError: if the token is malformed, tampered, or expired.
    """
    try:
        padding = 4 - len(token) % 4
        padded = token + "=" * (padding % 4)
        raw = base64.urlsafe_b64decode(padded).decode()
    except Exception as exc:
        raise TokenError("Malformed token (base64 decode failed)") from exc

    parts = raw.split(SEPARATOR)
    if len(parts) != 4:
        raise TokenError(f"Malformed token (expected 4 parts, got {len(parts)})")

    ghl_contact_id, location_id, expires_str, provided_sig = parts

    try:
        expires_at = int(expires_str)
    except ValueError as exc:
        raise TokenError("Malformed token (invalid expiry)") from exc

    if int(time.time()) > expires_at:
        raise TokenError("Token has expired")

    # Re-compute signature
    expected_payload = f"{ghl_contact_id}{SEPARATOR}{location_id}{SEPARATOR}{expires_str}"
    secret = _get_secret()
    expected_sig = _sign(expected_payload, secret)

    if not hmac.compare_digest(provided_sig, expected_sig):
        raise TokenError("Token signature invalid")

    return TokenPayload(
        ghl_contact_id=ghl_contact_id,
        location_id=location_id,
        expires_at=expires_at,
    )
