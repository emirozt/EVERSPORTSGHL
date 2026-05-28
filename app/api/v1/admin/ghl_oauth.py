"""
GHL OAuth callback endpoint.

Exchanges the authorization code from GHL's OAuth flow for access + refresh
tokens, and stores them in ``locations.ghl_oauth_token_cache``.

Flow:
  1. Operator navigates to ``GET /api/v1/admin/ghl/oauth/install/{location_id}``
     to get the GHL authorization URL (includes HMAC state for CSRF protection).
  2. Operator visits that URL and installs the GHL marketplace app.
  3. GHL redirects to ``GHL_REDIRECT_URI?code=...&locationId=...&state=...``
  4. This module's callback endpoint exchanges the code for tokens and stores them.

Additionally exposes:
  - ``GET  /api/v1/admin/ghl/oauth/status/{location_id}``  — check token state
  - ``DELETE /api/v1/admin/ghl/oauth/{location_id}``        — revoke / clear tokens

Security:
  All endpoints require ``X-Admin-Key: <admin_api_key>`` when ``ADMIN_API_KEY``
  is set in the environment.  The install endpoint signs the OAuth ``state``
  parameter with ``GHL_INSTALL_SECRET`` (HMAC-SHA256) so the callback can
  reject forged redirects.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Security
from fastapi.security import APIKeyHeader
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models.location import Location
from app.db.session import get_db
from app.ghl.client import GHL_TOKEN_URL

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ghl/oauth", tags=["ghl-oauth"])

# GHL marketplace authorization URL
GHL_AUTH_URL = "https://marketplace.gohighlevel.com/oauth/chooselocation"

# Scopes required by this integration
GHL_INSTALL_SCOPES = " ".join([
    "contacts.readonly",
    "contacts.write",
    "opportunities.readonly",
    "opportunities.write",
    "locations/customFields.readonly",
    "locations/customValues.readonly",
    "locations/customValues.write",
    "conversations/message.readonly",
    "conversations/message.write",
    "workflows.readonly",
])

# State token is valid for this many minutes (covers the OAuth redirect round-trip)
_STATE_VALID_MINUTES = 60


# ── Admin auth dependency ──────────────────────────────────────────────────────

_admin_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)


async def _require_admin(
    key: str | None = Security(_admin_key_header),
) -> None:
    """
    Require ``X-Admin-Key`` header when ``ADMIN_API_KEY`` is configured.

    In local development (``ADMIN_API_KEY`` not set) all requests are allowed
    through so the OAuth flow can be tested without extra ceremony.
    """
    settings = get_settings()
    if not settings.admin_api_key:
        return  # Open mode — useful for local dev
    if not key or key != settings.admin_api_key:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Admin-Key header")


# ── OAuth state helpers (CSRF protection) ─────────────────────────────────────

def _make_state(location_id: str, secret: str) -> str:
    """
    Generate a time-limited HMAC state token for the OAuth install flow.

    Token format: HMAC-SHA256(secret, "{location_id}:{minute_bucket}") where
    ``minute_bucket`` changes every minute, making each token valid for up to
    ``_STATE_VALID_MINUTES`` minutes.
    """
    ts = str(int(time.time()) // 60)
    msg = f"{location_id}:{ts}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def _verify_state(state: str, location_id: str, secret: str) -> bool:
    """
    Verify a state token produced by ``_make_state``.

    Checks the current minute bucket and the previous ``_STATE_VALID_MINUTES``
    buckets so a token issued at any point in the validity window is accepted.
    """
    now_bucket = int(time.time()) // 60
    for offset in range(_STATE_VALID_MINUTES + 1):
        ts = str(now_bucket - offset)
        msg = f"{location_id}:{ts}".encode()
        expected = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
        if hmac.compare_digest(state, expected):
            return True
    return False


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/install/{location_id}", dependencies=[Depends(_require_admin)])
async def ghl_oauth_install(
    location_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Return the GHL marketplace authorization URL for a location.

    The URL includes a signed ``state`` parameter that the callback will verify,
    preventing CSRF-style code-injection attacks.

    Requires ``GHL_OAUTH_CLIENT_ID``, ``GHL_REDIRECT_URI``, and (for state
    signing) ``GHL_INSTALL_SECRET`` to be set.
    """
    settings = get_settings()
    if not settings.ghl_oauth_client_id or not settings.ghl_redirect_uri:
        raise HTTPException(
            status_code=500,
            detail="GHL_OAUTH_CLIENT_ID and GHL_REDIRECT_URI must be configured",
        )

    state: str | None = None
    if settings.ghl_install_secret:
        state = _make_state(location_id, settings.ghl_install_secret)
    else:
        logger.warning(
            "ghl_oauth/install: GHL_INSTALL_SECRET not set — state parameter omitted; "
            "OAuth flow is vulnerable to CSRF"
        )

    params: dict[str, str] = {
        "response_type": "code",
        "redirect_uri": settings.ghl_redirect_uri,
        "client_id": settings.ghl_oauth_client_id,
        "scope": GHL_INSTALL_SCOPES,
    }
    if state:
        params["state"] = state

    install_url = f"{GHL_AUTH_URL}?{urlencode(params)}"
    return {
        "install_url": install_url,
        "location_id": location_id,
        "state_protected": state is not None,
    }


@router.get("/callback", dependencies=[Depends(_require_admin)])
async def ghl_oauth_callback(
    code: str = Query(..., description="Authorization code from GHL"),
    locationId: str = Query(..., description="GHL location (sub-account) ID"),
    state: str | None = Query(None, description="HMAC state token (CSRF protection)"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Handle the GHL OAuth redirect callback.

    Verifies the ``state`` parameter (when ``GHL_INSTALL_SECRET`` is configured),
    exchanges the authorization code for tokens, and stores them on the matching
    Location row (looked up by ``ghl_subaccount_id == locationId``).
    """
    settings = get_settings()

    # ── State verification (CSRF guard) ───────────────────────────────────────
    if settings.ghl_install_secret:
        if not state:
            raise HTTPException(
                status_code=400,
                detail="Missing 'state' parameter — use the /install endpoint to start the OAuth flow",
            )
        if not _verify_state(state, locationId, settings.ghl_install_secret):
            raise HTTPException(
                status_code=403,
                detail="Invalid or expired OAuth state — restart the install flow",
            )
    elif state:
        logger.warning(
            "ghl_oauth/callback: state=%r received but GHL_INSTALL_SECRET not set — skipping verification",
            state,
        )

    if not settings.ghl_oauth_client_id or not settings.ghl_oauth_client_secret:
        raise HTTPException(
            status_code=500,
            detail="GHL OAuth credentials not configured (GHL_OAUTH_CLIENT_ID / GHL_OAUTH_CLIENT_SECRET)",
        )

    # ── Look up the location by GHL sub-account ID ────────────────────────────
    res = await db.execute(
        select(Location).where(Location.ghl_subaccount_id == locationId)
    )
    location = res.scalar_one_or_none()
    if location is None:
        raise HTTPException(
            status_code=404,
            detail=f"No location found with ghl_subaccount_id={locationId!r}",
        )

    # ── Exchange code for tokens ───────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                GHL_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "client_id": settings.ghl_oauth_client_id,
                    "client_secret": settings.ghl_oauth_client_secret,
                    "code": code,
                    "redirect_uri": settings.ghl_redirect_uri,
                },
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Token exchange request failed: {exc}") from exc

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"GHL token endpoint returned {resp.status_code}: {resp.text[:200]}",
        )

    data = resp.json()
    expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=data.get("expires_in", 86400))

    token_cache = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "token_type": data.get("token_type", "Bearer"),
        "expires_at": expires_at.isoformat(),
    }

    await db.execute(
        update(Location)
        .where(Location.id == location.id)
        .values(ghl_oauth_token_cache=token_cache)
    )
    await db.commit()

    logger.info(
        "ghl_oauth: tokens stored for location_id=%s ghl_subaccount=%s",
        location.id,
        locationId,
    )
    return {
        "status": "ok",
        "location_id": str(location.id),
        "ghl_subaccount_id": locationId,
        "expires_at": expires_at.isoformat(),
    }


@router.get("/status/{location_id}", dependencies=[Depends(_require_admin)])
async def ghl_oauth_status(
    location_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Return the current OAuth token state for a location.
    """
    import uuid  # noqa: PLC0415
    try:
        loc_uuid = uuid.UUID(location_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid location_id UUID")

    res = await db.execute(select(Location).where(Location.id == loc_uuid))
    location = res.scalar_one_or_none()
    if location is None:
        raise HTTPException(status_code=404, detail="Location not found")

    cache: dict | None = location.ghl_oauth_token_cache
    if not cache or not cache.get("access_token"):
        return {"status": "not_configured", "location_id": location_id}

    expires_at_str = cache.get("expires_at")
    if expires_at_str:
        expires_at = datetime.fromisoformat(expires_at_str)
        now = datetime.now(tz=timezone.utc)
        state = "ok" if expires_at > now else "expired"
        return {
            "status": state,
            "location_id": location_id,
            "expires_at": expires_at_str,
            "expires_in_seconds": max(0, int((expires_at - now).total_seconds())),
        }

    return {"status": "ok", "location_id": location_id, "expires_at": None}


@router.delete("/{location_id}", dependencies=[Depends(_require_admin)])
async def revoke_ghl_oauth(
    location_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Clear the stored GHL OAuth tokens for a location."""
    import uuid  # noqa: PLC0415
    try:
        loc_uuid = uuid.UUID(location_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid location_id UUID")

    res = await db.execute(select(Location).where(Location.id == loc_uuid))
    if res.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Location not found")

    await db.execute(
        update(Location)
        .where(Location.id == loc_uuid)
        .values(ghl_oauth_token_cache=None)
    )
    await db.commit()
    logger.info("ghl_oauth: tokens cleared for location_id=%s", location_id)
    return {"status": "revoked", "location_id": location_id}
