"""
Orchestrator — computes and enqueues sync jobs into the scheduler_jobs table.

Three job types, each idempotent (safe to call multiple times):

  event_driven
    Reads the sessions table for target_date, collects distinct class-end
    times, and enqueues one job per class-end + 15 min.  Called once daily
    at 06:00 UTC so the day's schedule is preloaded.

  hourly_catchup
    Enqueues one incremental sync job for the next whole hour, but only if
    the local time at that hour falls within 07:00–22:00.  Called every hour
    by the cron scheduler.

  overnight
    Enqueues a full-reconciliation job at 03:00 local time.  Uses
    run_type='historical_backfill' for locations that have not completed
    their first historical sync; otherwise 'incremental'.  Called once daily
    at 03:00 UTC (close enough for most EU/AU timezones; per-location 03:00
    would require per-timezone cron entries — out of scope for M4).

Idempotency:
    Before inserting any job a ±5-minute (or ±30-minute for overnight)
    bucket check is performed.  Non-failed jobs within the bucket suppress
    duplicate inserts, so calling the enqueue functions repeatedly is safe.

References:
  - requirements_v2/07_foundation_layer.md §Layer 1 — schedule algorithm
  - app/db/models/scheduler_job.py — SchedulerJob model
  - app/scheduler/worker.py        — claims and executes jobs
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.location import Location
from app.db.models.scheduler_job import SchedulerJob
from app.db.models.sessions import Session as EversportsSession

logger = logging.getLogger(__name__)

# How many minutes after class-end to schedule the sync run
CLASS_END_OFFSET_MINUTES = 15

# Hourly-catchup window in local time (inclusive on both ends)
CATCHUP_START_HOUR = 7
CATCHUP_END_HOUR = 22


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def _job_exists(
    location_id: uuid.UUID,
    job_type: str,
    scheduled_at: datetime,
    db: AsyncSession,
    *,
    bucket_minutes: int = 5,
) -> bool:
    """
    Return True if a non-failed job already exists within ±bucket_minutes of
    ``scheduled_at`` for this (location_id, job_type) pair.

    This prevents duplicate enqueues when the cron trigger fires multiple times
    in the same window (e.g. server restart during the trigger interval).
    """
    lo = scheduled_at - timedelta(minutes=bucket_minutes)
    hi = scheduled_at + timedelta(minutes=bucket_minutes)
    stmt = select(SchedulerJob.id).where(
        SchedulerJob.location_id == location_id,
        SchedulerJob.job_type == job_type,
        SchedulerJob.scheduled_at >= lo,
        SchedulerJob.scheduled_at <= hi,
        SchedulerJob.status.in_(["pending", "running", "done"]),
    )
    row = (await db.execute(stmt)).first()
    return row is not None


async def compute_daily_schedule(
    location_id: uuid.UUID,
    db: AsyncSession,
    *,
    target_date: datetime | None = None,
) -> list[datetime]:
    """
    Read the sessions table for ``target_date`` and return a sorted list of
    UTC datetimes at which sync jobs should run (class_end + 15 min).

    Duplicate class-end times (multiple sessions ending at the exact same
    minute) are deduplicated — one job covers all of them.

    Args:
        location_id: Location whose sessions to read.
        db:          Async DB session.
        target_date: UTC datetime whose date to use (default: today UTC).

    Returns:
        Sorted list of UTC-aware datetimes (may be empty if no sessions).
    """
    if target_date is None:
        target_date = _now_utc()

    day_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    stmt = select(EversportsSession.end_time).where(
        EversportsSession.location_id == location_id,
        EversportsSession.end_time.is_not(None),
        EversportsSession.end_time >= day_start,
        EversportsSession.end_time < day_end,
    )
    rows = (await db.execute(stmt)).fetchall()

    end_times: set[datetime] = set()
    for (end_time,) in rows:
        if end_time is None:
            continue
        # Normalise to UTC regardless of how the DB stored it
        if end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=timezone.utc)
        else:
            end_time = end_time.astimezone(timezone.utc)
        end_times.add(end_time)

    return sorted(t + timedelta(minutes=CLASS_END_OFFSET_MINUTES) for t in end_times)


async def enqueue_event_driven_jobs(
    location_id: uuid.UUID,
    db: AsyncSession,
    *,
    target_date: datetime | None = None,
) -> int:
    """
    Enqueue event-driven sync jobs for all class-end slots on ``target_date``.

    Idempotent: slots that already have a pending/running/done job within the
    ±5-minute bucket are skipped.

    Args:
        location_id: Location to schedule for.
        db:          Async DB session (caller commits).
        target_date: Date to compute the schedule for (default: today UTC).

    Returns:
        Number of new jobs inserted.
    """
    run_times = await compute_daily_schedule(location_id, db, target_date=target_date)
    enqueued = 0
    for run_at in run_times:
        if await _job_exists(location_id, "event_driven", run_at, db):
            logger.debug(
                "orchestrator: event_driven slot %s already queued for location_id=%s — skip",
                run_at.isoformat(),
                location_id,
            )
            continue
        db.add(
            SchedulerJob(
                id=uuid.uuid4(),
                location_id=location_id,
                job_type="event_driven",
                run_type="incremental",
                scheduled_at=run_at,
            )
        )
        enqueued += 1
        logger.info(
            "orchestrator: enqueued event_driven job at %s for location_id=%s",
            run_at.isoformat(),
            location_id,
        )
    return enqueued


async def enqueue_hourly_catchup(
    location_id: uuid.UUID,
    db: AsyncSession,
    *,
    at: datetime | None = None,
) -> bool:
    """
    Enqueue one hourly-catchup sync job, if the requested time falls within
    the 07:00–22:00 local-time window for this location.

    Args:
        location_id: Location to schedule for.
        db:          Async DB session (caller commits).
        at:          UTC datetime to schedule (default: next whole hour from now).

    Returns:
        True if a job was inserted, False if skipped (outside window or duplicate).
    """
    if at is None:
        now = _now_utc()
        at = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)

    # Determine the location's timezone for the local-hour check
    tz_row = (
        await db.execute(select(Location.timezone).where(Location.id == location_id))
    ).scalar_one_or_none()
    tz = ZoneInfo(tz_row or "UTC")
    local_hour = at.astimezone(tz).hour

    if not (CATCHUP_START_HOUR <= local_hour <= CATCHUP_END_HOUR):
        logger.debug(
            "orchestrator: hourly_catchup at local hour=%d is outside window [%d, %d] "
            "for location_id=%s — skip",
            local_hour,
            CATCHUP_START_HOUR,
            CATCHUP_END_HOUR,
            location_id,
        )
        return False

    if await _job_exists(location_id, "hourly_catchup", at, db):
        logger.debug(
            "orchestrator: hourly_catchup at %s already queued for location_id=%s — skip",
            at.isoformat(),
            location_id,
        )
        return False

    db.add(
        SchedulerJob(
            id=uuid.uuid4(),
            location_id=location_id,
            job_type="hourly_catchup",
            run_type="incremental",
            scheduled_at=at,
        )
    )
    logger.info(
        "orchestrator: enqueued hourly_catchup job at %s for location_id=%s",
        at.isoformat(),
        location_id,
    )
    return True


async def enqueue_overnight(
    location_id: uuid.UUID,
    db: AsyncSession,
    *,
    at: datetime | None = None,
) -> bool:
    """
    Enqueue one overnight full-reconciliation sync job.

    If ``at`` is not given, the job is scheduled for the next 03:00 in the
    location's local timezone.

    Run type:
      'historical_backfill' — location has never completed a historical sync
                              (historical_sync_flag != 'complete')
      'incremental'          — normal nightly sweep

    Idempotency bucket: ±30 minutes (wider than hourly because the cron
    trigger fires once a day and small clock drift should not cause duplicates).

    Args:
        location_id: Location to schedule for.
        db:          Async DB session (caller commits).
        at:          UTC datetime to schedule (default: next 03:00 local).

    Returns:
        True if a job was inserted, False if skipped (location missing or duplicate).
    """
    row = (
        await db.execute(
            select(Location.timezone, Location.historical_sync_flag).where(
                Location.id == location_id
            )
        )
    ).first()
    if row is None:
        logger.warning("orchestrator: location_id=%s not found — cannot enqueue overnight", location_id)
        return False

    tz_str, historical_flag = row
    tz = ZoneInfo(tz_str or "UTC")

    if at is None:
        now_local = datetime.now(tz)
        candidate = now_local.replace(hour=3, minute=0, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += timedelta(days=1)
        at = candidate.astimezone(timezone.utc)

    if await _job_exists(location_id, "overnight", at, db, bucket_minutes=30):
        logger.debug(
            "orchestrator: overnight at %s already queued for location_id=%s — skip",
            at.isoformat(),
            location_id,
        )
        return False

    run_type = "historical_backfill" if historical_flag != "complete" else "incremental"
    db.add(
        SchedulerJob(
            id=uuid.uuid4(),
            location_id=location_id,
            job_type="overnight",
            run_type=run_type,
            scheduled_at=at,
        )
    )
    logger.info(
        "orchestrator: enqueued overnight job at %s run_type=%s for location_id=%s",
        at.isoformat(),
        run_type,
        location_id,
    )
    return True
