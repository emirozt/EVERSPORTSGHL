"""
Bootstrap API endpoints — CSV seed for a new location.

POST /api/v1/admin/locations/{location_id}/bootstrap
  Upload bookings, activities, and noshows CSVs to seed the Postgres datastore.
  Returns 200 with BootstrapResult (sync for now — async job queue is M2+).

GET /api/v1/admin/locations/{location_id}/bootstrap/status
  Returns the current historical_sync_flag and last bootstrap run summary.

POST /api/v1/admin/locations/{location_id}/bootstrap/reset
  Deletes rows tagged with the last bootstrap_run_id and resets historical_sync_flag.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.bookings import Booking
from app.db.models.contacts import Contact
from app.db.models.location import Location
from app.db.models.sessions import Session
from app.db.models.sync_log import SyncLog
from app.db.session import get_db
from app.ingest.bootstrap import BootstrapResult, run_bootstrap

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/locations/{location_id}/bootstrap",
    tags=["bootstrap"],
)


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
async def bootstrap_location(
    location_id: uuid.UUID,
    bookings: UploadFile = File(..., description="bookings.csv export (required)"),
    activities: UploadFile | None = File(None, description="all activities.csv (optional)"),
    noshows: UploadFile | None = File(None, description="noshows.csv (optional)"),
    db: AsyncSession = Depends(get_db),
) -> BootstrapResult:
    """
    Seed the datastore from Eversports CSV exports.

    Idempotent: re-uploading the same files produces zero new rows.

    The bookings file is required. Activities and noshows are optional but recommended.
    """
    await _get_location(location_id, db)

    bookings_bytes = await bookings.read()
    activities_bytes = await activities.read() if activities else None
    noshows_bytes = await noshows.read() if noshows else None

    if not bookings_bytes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="bookings file is empty — at least one booking row is required",
        )

    logger.info(
        "bootstrap POST: location_id=%s bookings=%d bytes activities=%s noshows=%s",
        location_id,
        len(bookings_bytes),
        f"{len(activities_bytes)} bytes" if activities_bytes else "absent",
        f"{len(noshows_bytes)} bytes" if noshows_bytes else "absent",
    )

    try:
        result = await run_bootstrap(
            location_id=location_id,
            bookings_bytes=bookings_bytes,
            activities_bytes=activities_bytes,
            noshows_bytes=noshows_bytes,
            db=db,
        )
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        await db.rollback()
        logger.exception("bootstrap: unexpected error for location_id=%s", location_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Bootstrap failed: {exc}",
        ) from exc

    if result["errors"]:
        logger.warning(
            "bootstrap: completed with %d error(s) for location_id=%s: %s",
            len(result["errors"]),
            location_id,
            result["errors"],
        )

    return result


@router.get("/status")
async def bootstrap_status(
    location_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Return the current bootstrap status for a location.

    Response shape:
      {
        "historical_sync_flag": "pending" | "bootstrapped" | "complete",
        "last_bootstrap_run_id": uuid | null,
        "sync_log_summary": { ... } | null
      }
    """
    location = await _get_location(location_id, db)

    # Find the most recent bootstrap sync_log for this location
    log_result = await db.execute(
        select(SyncLog)
        .where(SyncLog.location_id == location_id, SyncLog.run_type == "bootstrap")
        .order_by(SyncLog.created_at.desc())
        .limit(1)
    )
    last_log = log_result.scalar_one_or_none()

    sync_log_summary = None
    last_bootstrap_run_id = None

    if last_log:
        last_bootstrap_run_id = str(last_log.bootstrap_run_id)
        sync_log_summary = {
            "id": str(last_log.id),
            "contacts_processed": last_log.contacts_processed,
            "contacts_updated": last_log.contacts_updated,
            "errors": last_log.errors,
            "duration_seconds": (
                str(last_log.duration_seconds) if last_log.duration_seconds else None
            ),
            "created_at": last_log.created_at.isoformat() if last_log.created_at else None,
        }

    return {
        "historical_sync_flag": location.historical_sync_flag,
        "last_bootstrap_run_id": last_bootstrap_run_id,
        "sync_log_summary": sync_log_summary,
    }


@router.post("/reset", status_code=status.HTTP_200_OK)
async def reset_bootstrap(
    location_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Reset the bootstrap for a location.

    Deletes all rows tagged with the last bootstrap_run_id:
      - contacts, bookings, sessions with that bootstrap_run_id
      - the corresponding sync_log row

    Resets historical_sync_flag → 'pending'.

    Safe to call even if no bootstrap has run — returns counts of 0.
    """
    location = await _get_location(location_id, db)

    # Find last bootstrap_run_id for this location
    log_result = await db.execute(
        select(SyncLog)
        .where(SyncLog.location_id == location_id, SyncLog.run_type == "bootstrap")
        .order_by(SyncLog.created_at.desc())
        .limit(1)
    )
    last_log = log_result.scalar_one_or_none()

    deleted_contacts = 0
    deleted_bookings = 0
    deleted_sessions = 0
    deleted_logs = 0

    if last_log and last_log.bootstrap_run_id:
        run_id = last_log.bootstrap_run_id

        # Delete in dependency order (bookings before contacts, sessions independent)
        res_b = await db.execute(
            delete(Booking).where(
                Booking.location_id == location_id,
                Booking.bootstrap_run_id == run_id,
            )
        )
        deleted_bookings = res_b.rowcount or 0

        res_c = await db.execute(
            delete(Contact).where(
                Contact.location_id == location_id,
                Contact.bootstrap_run_id == run_id,
            )
        )
        deleted_contacts = res_c.rowcount or 0

        res_s = await db.execute(
            delete(Session).where(
                Session.location_id == location_id,
                Session.bootstrap_run_id == run_id,
            )
        )
        deleted_sessions = res_s.rowcount or 0

        res_l = await db.execute(
            delete(SyncLog).where(
                SyncLog.location_id == location_id,
                SyncLog.bootstrap_run_id == run_id,
            )
        )
        deleted_logs = res_l.rowcount or 0

    # Reset flag
    from sqlalchemy import update  # noqa: PLC0415 — local import to keep module-level clean
    await db.execute(
        update(Location)
        .where(Location.id == location_id)
        .values(historical_sync_flag="pending")
    )
    await db.commit()

    logger.info(
        "bootstrap reset: location_id=%s deleted contacts=%d bookings=%d sessions=%d logs=%d",
        location_id,
        deleted_contacts,
        deleted_bookings,
        deleted_sessions,
        deleted_logs,
    )

    return {
        "deleted_contacts": deleted_contacts,
        "deleted_bookings": deleted_bookings,
        "deleted_sessions": deleted_sessions,
        "deleted_sync_logs": deleted_logs,
        "historical_sync_flag": location.historical_sync_flag,  # pre-reset value for reference
    }
