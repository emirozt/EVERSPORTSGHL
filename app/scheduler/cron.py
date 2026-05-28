"""
APScheduler cron setup for M4 event-driven scheduler.

Configures three recurring jobs that enqueue sync work into the scheduler_jobs
table (see app/scheduler/orchestrator.py):

  daily_event_schedule  — 06:00 UTC daily
    Reads the sessions table for each active location and pre-loads event-driven
    jobs for class_end + 15 min slots across the day.

  hourly_catchup        — :00 every hour (07:00–22:00 local filtered by orchestrator)
    Enqueues a failsafe incremental sync for each active location once per hour.
    The orchestrator suppresses slots outside the 07:00–22:00 local window.

  overnight             — 03:00 UTC daily
    Enqueues a nightly full reconciliation.  Uses 'historical_backfill' for
    locations that have not yet completed their first historical sync.

APScheduler version:
    Targets APScheduler 3.x (AsyncIOScheduler).  Version 4.x has a different
    API and is not compatible with this module.

Usage:
    Called from app/main.py lifespan — see start_scheduler() / stop_scheduler().
"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
from sqlalchemy import select

from app.db.models.location import Location
from app.db.session import get_session_factory
from app.scheduler.orchestrator import (
    enqueue_event_driven_jobs,
    enqueue_hourly_catchup,
    enqueue_overnight,
)

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def _for_all_locations(job_type: str) -> None:
    """
    Call the appropriate enqueue function for every location in the DB.

    Used as the APScheduler job target — runs as a coroutine inside the
    existing event loop.
    """
    factory = get_session_factory()
    async with factory() as db:
        result = await db.execute(select(Location.id))
        location_ids = [row[0] for row in result.fetchall()]

    for location_id in location_ids:
        async with factory() as db:
            try:
                if job_type == "event_driven":
                    count = await enqueue_event_driven_jobs(location_id, db)
                    await db.commit()
                    if count:
                        logger.info(
                            "cron/event_driven: enqueued %d jobs for location_id=%s",
                            count,
                            location_id,
                        )
                elif job_type == "hourly_catchup":
                    inserted = await enqueue_hourly_catchup(location_id, db)
                    await db.commit()
                    if inserted:
                        logger.debug(
                            "cron/hourly_catchup: enqueued for location_id=%s", location_id
                        )
                elif job_type == "overnight":
                    inserted = await enqueue_overnight(location_id, db)
                    await db.commit()
                    if inserted:
                        logger.info(
                            "cron/overnight: enqueued for location_id=%s", location_id
                        )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "cron/%s: error for location_id=%s: %s",
                    job_type,
                    location_id,
                    exc,
                    exc_info=True,
                )


async def _daily_event_schedule() -> None:
    await _for_all_locations("event_driven")


async def _hourly_catchup() -> None:
    await _for_all_locations("hourly_catchup")


async def _overnight() -> None:
    await _for_all_locations("overnight")


def get_scheduler() -> AsyncIOScheduler:
    """Return the singleton AsyncIOScheduler (creates it on first call)."""
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")

        # Daily 06:00 UTC — pre-load event-driven schedule from sessions
        _scheduler.add_job(
            _daily_event_schedule,
            CronTrigger(hour=6, minute=0, timezone="UTC"),
            id="daily_event_schedule",
            replace_existing=True,
            max_instances=1,
        )

        # Every hour :00 — hourly catchup (orchestrator filters by local-time window)
        _scheduler.add_job(
            _hourly_catchup,
            CronTrigger(minute=0, timezone="UTC"),
            id="hourly_catchup",
            replace_existing=True,
            max_instances=1,
        )

        # Daily 03:00 UTC — overnight reconciliation
        _scheduler.add_job(
            _overnight,
            CronTrigger(hour=3, minute=0, timezone="UTC"),
            id="overnight",
            replace_existing=True,
            max_instances=1,
        )

        logger.info("cron: scheduler configured (3 jobs)")
    return _scheduler


def start_scheduler() -> None:
    """Start the APScheduler if not already running."""
    scheduler = get_scheduler()
    if not scheduler.running:
        scheduler.start()
        logger.info("cron: scheduler started")
    else:
        logger.debug("cron: scheduler already running")


def stop_scheduler() -> None:
    """Shut down the APScheduler gracefully."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("cron: scheduler stopped")
    _scheduler = None
