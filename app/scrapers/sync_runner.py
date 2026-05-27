"""
Sync runner — full Eversports → Postgres sync for one location.

Orchestrates:
  1. Load location from DB; validate cookie_state.
  2. Open EversportsBaseScraper context (injects cookies, launches Playwright).
  3. Resolve numeric facilityId via AdminApiClient.get_facility_id().
  4. Download bookings CSV and activities CSV via AdminApiClient.
  5. Call run_bootstrap() from app/ingest/bootstrap.py with the downloaded bytes.
     (bootstrap is already idempotent — re-running on the same data is safe.)
  6. Mark scraper._mark_ok() (sets cookie_state = 'ok').
  7. Return BootstrapResult + run metadata.

API endpoints used (all direct API calls — no page navigation required):
  facilityId lookup:  GET /api/admin-facilities?facilityShortId={companyId}
  Bookings CSV:       GET /api/event/export-booking-list
                          ?facilityId={id}&fromDate={date}&toDate={date}
                      → JSON {"url": "<presigned_s3_url>"}
                      → fetch presigned URL → CSV matching bookings.csv format
  Activities CSV:     GET /api/scheduler/list/download
                          ?facilityId={id}&fromDate={date}&toDate={date}&exportType=active
                      → direct text/csv matching "all activities.csv" format

Date window:
  Both exports use a rolling 30-day window (today − 30 days → today).
  This is computed at run time so incremental and historical_backfill runs
  both cover the same window — the distinction lies in the historical_sync_flag.

Run types:
  ``incremental``  (default):
    Downloads bookings + activities for the last 30 days.
    Suitable for hourly/event-driven runs.

  ``historical_backfill``:
    Identical download logic; on success sets historical_sync_flag = 'complete'.
    This signals that the Mode B first-run sweep is done.

The sync_runner does NOT implement the no-show export download (UC03 removed
in v2).  run_bootstrap() receives ``noshows_bytes=None`` for all scraper runs.

Error handling:
  - SessionExpiredError from the scraper propagates up unchanged.
  - facilityId lookup failure aborts the run (we can't download anything without it).
  - Download errors for individual reports are caught; partial results are
    still persisted (spec §Partial report failure: "update only fields sourced
    from successfully downloaded reports").
  - A sync_log entry is always written, even on partial failure.

See:
  - requirements_v2/07_foundation_layer.md §Layer 1, §Sync Log
  - app/ingest/bootstrap.py — the persistence layer (idempotent upserts)
  - app/scrapers/admin_csv.py — AdminApiClient (API endpoints + format details)
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.location import Location
from app.db.models.sync_log import SyncLog
from app.ingest.bootstrap import BootstrapResult, run_bootstrap
from app.scrapers.admin_csv import AdminApiClient
from app.scrapers.base import EversportsBaseScraper
from app.scrapers.exceptions import SessionExpiredError

logger = logging.getLogger(__name__)

# Rolling window for both bookings and activities exports
_EXPORT_WINDOW_DAYS = 30


def _date_range(window_days: int = _EXPORT_WINDOW_DAYS) -> tuple[str, str]:
    """Return (from_date, to_date) as ``YYYY-MM-DD`` strings for the export window."""
    today = date.today()
    from_date = (today - timedelta(days=window_days)).isoformat()
    to_date = today.isoformat()
    return from_date, to_date


async def run_sync(
    location_id: uuid.UUID,
    db: AsyncSession,
    run_type: str = "incremental",
) -> dict[str, Any]:
    """
    Run a full Eversports → Postgres sync for one location.

    Args:
        location_id: UUID of the location row in the ``locations`` table.
        db: Async SQLAlchemy session (caller is responsible for commit/rollback).
        run_type: ``"incremental"`` (default) or ``"historical_backfill"``.

    Returns:
        A dict merging the BootstrapResult fields with:
          - ``run_type`` (str)
          - ``scraper_duration_seconds`` (float)

    Raises:
        SessionExpiredError: if cookie_state is 'expired' or 'unset'.
        ValueError: if the location is not found.
    """
    run_start = time.monotonic()

    if run_type not in ("incremental", "historical_backfill"):
        raise ValueError(
            f"Invalid run_type={run_type!r}. Must be 'incremental' or 'historical_backfill'."
        )

    # ── Step 1: Load location ──────────────────────────────────────────────────
    result = await db.execute(select(Location).where(Location.id == location_id))
    location = result.scalar_one_or_none()
    if location is None:
        raise ValueError(f"Location not found: {location_id}")

    logger.info(
        "sync_runner: starting run_type=%s location_id=%s cookie_state=%s",
        run_type,
        location_id,
        location.eversports_cookie_state,
    )

    # Pre-flight cookie state check — fail fast before launching a browser.
    #
    # 'unset' = location created but never onboarded.  The scheduled sweep may
    # encounter these; skip them quietly so one un-configured location does not
    # abort the entire batch.  The HTTP endpoint handles this path at 400 before
    # run_sync is called.
    #
    # 'expired' = was working but session timed out → operator must act → raise.
    cookie_no_cache = not location.eversports_cookie_cache
    if location.eversports_cookie_state == "unset" or cookie_no_cache:
        logger.warning(
            "sync_runner: location_id=%s not onboarded (cookie_state=%s) — skipping",
            location_id,
            location.eversports_cookie_state,
        )
        return {
            "run_type": run_type,
            "skipped": True,
            "skip_reason": "not_onboarded",
            "contacts_seeded": 0,
            "bookings_seeded": 0,
            "sessions_seeded": 0,
            "products_discovered": [],
            "contacts_missing_email": 0,
            "contacts_invalid_phone": 0,
            "warnings": [],
            "errors": [],
            "scraper_duration_seconds": 0.0,
        }

    if location.eversports_cookie_state == "expired":
        raise SessionExpiredError(
            "Eversports session expired — please re-export cookies and run "
            "scripts/import_cookies.py"
        )

    # ── Steps 2–4: Fetch data via AdminApiClient ───────────────────────────────
    bookings_bytes: bytes | None = None
    activities_bytes: bytes | None = None
    download_errors: list[str] = []

    from_date, to_date = _date_range()
    logger.info(
        "sync_runner: export window from=%s to=%s",
        from_date,
        to_date,
    )

    async with EversportsBaseScraper(location, db) as scraper:
        client = AdminApiClient(scraper, location)

        # Step 3: Resolve numeric facilityId (required for all export calls)
        try:
            facility_id = await client.get_facility_id(location.eversports_studio_id)
            logger.info("sync_runner: facilityId=%d", facility_id)
        except SessionExpiredError:
            raise
        except Exception as exc:  # noqa: BLE001
            # Without facilityId we can't download anything — abort early
            duration = time.monotonic() - run_start
            msg = f"facilityId lookup failed: {exc}"
            logger.error("sync_runner: %s", msg, exc_info=True)
            _write_error_sync_log(
                db=db,
                location_id=location_id,
                run_type=run_type,
                errors=[msg],
                duration=duration,
            )
            raise RuntimeError(
                f"sync_runner: cannot proceed without facilityId for "
                f"location_id={location_id}. {msg}"
            ) from exc

        # Step 4a: Download bookings CSV — required
        try:
            bookings_bytes = await client.download_bookings(facility_id, from_date, to_date)
            logger.info("sync_runner: bookings downloaded %d bytes", len(bookings_bytes))
        except SessionExpiredError:
            raise  # Let it propagate — session is expired, nothing more to do
        except Exception as exc:  # noqa: BLE001
            msg = f"bookings download failed: {exc}"
            logger.error("sync_runner: %s", msg, exc_info=True)
            download_errors.append(msg)

        # Step 4b: Download activities CSV — optional (UC05 availability)
        try:
            activities_bytes = await client.download_activities(facility_id, from_date, to_date)
            logger.info("sync_runner: activities downloaded %d bytes", len(activities_bytes))
        except SessionExpiredError:
            raise
        except Exception as exc:  # noqa: BLE001
            msg = f"activities download failed: {exc}"
            logger.error("sync_runner: %s", msg, exc_info=True)
            download_errors.append(msg)

        if bookings_bytes is None:
            # Without bookings we can't call run_bootstrap meaningfully.
            # Write a partial sync_log entry and abort.
            duration = time.monotonic() - run_start
            _write_error_sync_log(
                db=db,
                location_id=location_id,
                run_type=run_type,
                errors=download_errors,
                duration=duration,
            )
            raise RuntimeError(
                f"sync_runner: bookings download failed for location_id={location_id}. "
                "Cannot proceed without bookings. Errors: " + "; ".join(download_errors)
            )

        # ── Step 5: Persist via run_bootstrap ─────────────────────────────────
        logger.info("sync_runner: calling run_bootstrap")
        bootstrap_result: BootstrapResult = await run_bootstrap(
            location_id=location_id,
            bookings_bytes=bookings_bytes,
            activities_bytes=activities_bytes,
            noshows_bytes=None,  # no-show export not used in v2 (UC03 removed)
            db=db,
        )

        # Merge any download errors into the bootstrap result's error list
        if download_errors:
            bootstrap_result["errors"] = list(bootstrap_result["errors"]) + download_errors

        # ── Step 6: Mark session ok ────────────────────────────────────────────
        await scraper._mark_ok()

    # ── Step 7: Post-process for historical_backfill ───────────────────────────
    if run_type == "historical_backfill":
        await db.execute(
            update(Location)
            .where(Location.id == location_id)
            .values(historical_sync_flag="complete")
        )
        logger.info(
            "sync_runner: historical_sync_flag set to 'complete' for location_id=%s",
            location_id,
        )

    scraper_duration = time.monotonic() - run_start

    logger.info(
        "sync_runner: complete run_type=%s location_id=%s "
        "contacts=%d bookings=%d sessions=%d duration=%.2fs",
        run_type,
        location_id,
        bootstrap_result["contacts_seeded"],
        bootstrap_result["bookings_seeded"],
        bootstrap_result["sessions_seeded"],
        scraper_duration,
    )

    return {
        **bootstrap_result,
        "run_type": run_type,
        "scraper_duration_seconds": round(scraper_duration, 3),
    }


def _write_error_sync_log(
    db: AsyncSession,
    location_id: uuid.UUID,
    run_type: str,
    errors: list[str],
    duration: float,
) -> None:
    """Write a sync_log row for a failed sync run (fire-and-forget, no commit)."""
    sync_log = SyncLog(
        id=uuid.uuid4(),
        location_id=location_id,
        run_type=run_type,
        contacts_processed=0,
        contacts_updated=0,
        tags_applied=0,
        pipeline_moves=0,
        errors=errors,
        duration_seconds=round(duration, 3),
    )
    db.add(sync_log)
    logger.debug("sync_runner: error sync_log written for location_id=%s", location_id)
