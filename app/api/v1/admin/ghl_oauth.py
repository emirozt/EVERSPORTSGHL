"""
GHL OAuth callback endpoint.

Exchanges the authorization code from GHL's OAuth flow for access + refresh
tokens, and stores them in ``locations.ghl_oauth_token_cache``.

Flow:
  1. Operator installs the GHL marketplace app on their sub-account.
  2. GHL redirects to ``GHL_REDIRECT_URI?code=...&locationId=...``
  3. This endpoint exchanges the code for tokens and stores them.

Additionally exposes:
  - ``GET /api/v1/admin/ghl/oauth/status/{location_id}`` — check token state
  - ``DELETE /api/v1/admin/ghl/oauth/{location_id}`` — revoke / clear tokens
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models.location import Location
from app.db.session import get_db
from app.ghl.client import GHL_TOKEN_URL

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ghl/oauth", tags=["ghl-oauth"])


@router.get("/callback")
async def ghl_oauth_callback(
    code: str = Query(..., description="Authorization code from GHL"),
    locationId: str = Query(..., description="GHL location (sub-account) ID"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Handle the GHL OAuth redirect callback.

    Exchanges the authorization code for tokens and stores them on the
    matching Location row (looked up by ``ghl_subaccount_id == locationId``).
    """
    settings = get_settings()
    if not settings.ghl_oauth_client_id or not settings.ghl_oauth_client_secret:
        raise HTTPException(
            status_code=500,
            detail="GHL OAuth credentials not configured (GHL_OAUTH_CLIENT_ID / GHL_OAUTH_CLIENT_SECRET)",
        )

    # Look up the location by GHL sub-account ID
    res = await db.execute(
        select(Location).where(Location.ghl_subaccount_id == locationId)
    )
    location = res.scalar_one_or_none()
    if location is None:
        raise HTTPException(
            status_code=404,
            detail=f"No location found with ghl_subaccount_id={locationId!r}",
        )

    # Exchange code for tokens
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


@router.get("/status/{location_id}")
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


@router.delete("/{location_id}")
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
