"""
Sync trigger endpoint.

POST /api/v1/admin/locations/{location_id}/sync
  Triggers an immediate Eversports → Postgres sync for the given location.

  Body (optional JSON):
    { "run_type": "incremental" | "historical_backfill" }
    Defaults to "incremental" if omitted.

  Response: SyncResult
    All BootstrapResult fields, plus:
      - run_type (str)
      - scraper_duration_seconds (float)

  Error responses:
    400 — cookie_state == 'unset' (cookies not yet imported)
    503 — cookie_state == 'expired' (session expired, operator must re-import)
    404 — location not found
    422 — invalid run_type
    500 — unexpected scraper or persistence error

Design notes:
  - This endpoint runs the sync SYNCHRONOUSLY inside the HTTP request.
    For production use with long-running syncs (>30s), the caller should
    use a background task queue (e.g. PgBoss) and poll for results.
    Synchronous execution is acceptable for M2 (no background worker yet).
  - The scraper opens a real Playwright browser — if the test environment
    does not have Chromium installed, this endpoint will fail with a
    ``playwright._impl._errors.Error``.  In unit tests the scraper is mocked.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.location import Location
from app.db.session import get_db
from app.scrapers.exceptions import SessionExpiredError
from app.scrapers.sync_runner import run_sync

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/locations/{location_id}/sync",
    tags=["sync"],
)


class SyncRequest(BaseModel):
    """Optional request body for POST /sync."""

    run_type: str = "incremental"


async def _get_location(
    location_id: uuid.UUID,
    db: AsyncSession,
) -> Location:
    """Fetch a location or raise 404."""
    result = await db.execute(select(Location).where(Location.id == location_id))
    location = result.scalar_one_or_none()
    if location is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Location {location_id} not found",
        )
    return location


@router.post("", response_model=None, status_code=status.HTTP_200_OK)
async def trigger_sync(
    location_id: uuid.UUID,
    body: SyncRequest = Body(default_factory=SyncRequest),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Trigger an Eversports → Postgres sync for the given location.

    The sync runs synchronously inside this request.  On success, returns the
    full BootstrapResult merged with scraper metadata.

    HTTP status codes:
      200 — sync succeeded (may contain non-fatal warnings/errors in the body)
      400 — cookies not yet imported (cookie_state == 'unset')
      503 — session expired (cookie_state == 'expired')
      404 — location not found
      422 — invalid run_type
      500 — scraper or persistence error
    """
    location = await _get_location(location_id, db)

    # Pre-flight: check cookie_state before launching a browser
    cookie_state = location.eversports_cookie_state
    cookie_cache = location.eversports_cookie_cache

    if cookie_state == "unset" or not cookie_cache:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Eversports cookies not imported yet. "
                "Export cookies via Cookie-Editor and run scripts/import_cookies.py."
            ),
        )

    if cookie_state == "expired":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session expired — re-import cookies via scripts/import_cookies.py",
        )

    # Validate run_type
    if body.run_type not in ("incremental", "historical_backfill"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Invalid run_type={body.run_type!r}. "
                "Must be 'incremental' or 'historical_backfill'."
            ),
        )

    logger.info("sync: POST location_id=%s run_type=%s", location_id, body.run_type)

    try:
        result = await run_sync(
            location_id=location_id,
            db=db,
            run_type=body.run_type,
        )
        await db.commit()
    except SessionExpiredError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session expired — re-import cookies via scripts/import_cookies.py",
        ) from exc
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        await db.rollback()
        logger.exception(
            "sync: unexpected error for location_id=%s run_type=%s",
            location_id,
            body.run_type,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Sync failed: {exc}",
        ) from exc

    if result.get("errors"):
        logger.warning(
            "sync: completed with %d error(s) for location_id=%s: %s",
            len(result["errors"]),
            location_id,
            result["errors"],
        )

    return result
