"""
Scheduler worker — polls scheduler_jobs and executes due sync runs.

The worker runs as a long-lived asyncio background task started inside the
FastAPI lifespan.  It polls the scheduler_jobs table every POLL_INTERVAL_SECONDS
seconds, claims the next due pending job, and calls run_sync() for it.

Claim strategy:
  The worker uses SELECT FOR UPDATE SKIP LOCKED to atomically claim jobs.
  Two concurrent worker processes will never claim the same job: one acquires
  the row lock, the other skips the locked row and moves on.  The claim and
  the status update happen in the same transaction, so there is no window
  between read and write.

Error handling:
  - SessionExpiredError   → job marked 'failed'; operator must re-import cookies.
  - Any other exception   → job marked 'failed' with the exception message.
  - Worker loop never propagates exceptions from individual jobs — it always
    continues to the next poll after logging.

Concurrency:
  At most MAX_CONCURRENT_JOBS jobs execute simultaneously per worker process.
  The semaphore prevents the job list from being exhausted too fast and causing
  a thundering-herd on Postgres.

See:
  - app/scheduler/orchestrator.py — enqueue helpers
  - app/scheduler/cron.py         — APScheduler cron triggers
  - app/scrapers/sync_runner.py   — the actual sync logic
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.scheduler_job import SchedulerJob
from app.db.session import get_session_factory
from app.scrapers.exceptions import SessionExpiredError

logger = logging.getLogger(__name__)

# How often the worker polls for new jobs (seconds)
POLL_INTERVAL_SECONDS = 30

# Maximum parallel sync runs per worker process
MAX_CONCURRENT_JOBS = 3


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def recover_stuck_jobs(factory: async_sessionmaker[AsyncSession]) -> int:
    """
    Mark any jobs left in 'running' state as 'failed' with reason
    'recovered_stale_at_startup'.

    These are jobs that were claimed by a previous worker process that died
    before marking them done/failed.  Called once at worker startup so stale
    jobs don't block the queue indefinitely.

    Returns:
        Number of jobs recovered.
    """
    async with factory() as db:
        result = await db.execute(
            update(SchedulerJob)
            .where(SchedulerJob.status == "running")
            .values(
                status="failed",
                completed_at=_now_utc(),
                error="recovered_stale_at_startup",
            )
        )
        await db.commit()
    count = result.rowcount
    if count:
        logger.warning("worker: recovered %d stale 'running' job(s) at startup", count)
    return count


async def _claim_next_job(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, str, str] | None:
    """
    Atomically claim the oldest due pending job.

    Updates status from 'pending' → 'running' in the same transaction that
    reads the row, preventing two concurrent workers from claiming the same job.

    Returns:
        (job_id, location_id, job_type, run_type) tuple, or None if the queue
        is empty.
    """
    async with factory() as db:
        async with db.begin():
            # Find the oldest due pending job.
            # FOR UPDATE SKIP LOCKED: a second concurrent worker skips a
            # row that is already being claimed, preventing double-processing.
            stmt = (
                select(SchedulerJob)
                .where(
                    SchedulerJob.status == "pending",
                    SchedulerJob.scheduled_at <= _now_utc(),
                )
                .order_by(SchedulerJob.scheduled_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            result = await db.execute(stmt)
            job = result.scalar_one_or_none()
            if job is None:
                return None

            # Claim it — set status to 'running'
            job.status = "running"
            job.started_at = _now_utc()
            # expire_on_commit=False so fields remain accessible after commit
            return (job.id, job.location_id, job.job_type, job.run_type)


async def _mark_job_complete(
    factory: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    *,
    error: str | None = None,
) -> None:
    """Update a job's terminal status in a fresh session."""
    async with factory() as db:
        await db.execute(
            update(SchedulerJob)
            .where(SchedulerJob.id == job_id)
            .values(
                status="failed" if error else "done",
                completed_at=_now_utc(),
                error=error,
            )
        )
        await db.commit()


async def execute_job(
    job_id: uuid.UUID,
    location_id: uuid.UUID,
    job_type: str,
    run_type: str,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """
    Execute a single scheduler job.

    Opens a fresh DB session for the sync run, commits on success, then marks
    the job done/failed in a second session.

    This function is separated from the worker loop so it can be tested in
    isolation without starting the polling loop.

    Args:
        job_id:      ID of the SchedulerJob row to update on completion.
        location_id: Location to sync.
        job_type:    'event_driven' | 'hourly_catchup' | 'overnight' (for logging).
        run_type:    'incremental' | 'historical_backfill'
        factory:     Session factory used to open DB connections.
    """
    # Import here to avoid a circular import at module load time
    from app.scrapers.sync_runner import run_sync  # noqa: PLC0415

    logger.info(
        "worker: executing job_id=%s job_type=%s run_type=%s location_id=%s",
        job_id,
        job_type,
        run_type,
        location_id,
    )

    error: str | None = None
    try:
        async with factory() as db:
            await run_sync(location_id=location_id, db=db, run_type=run_type)
            await db.commit()
        logger.info("worker: job_id=%s completed successfully", job_id)
    except SessionExpiredError as exc:
        error = f"SessionExpiredError: {exc}"
        logger.warning(
            "worker: job_id=%s session expired for location_id=%s: %s",
            job_id,
            location_id,
            exc,
        )
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        logger.error(
            "worker: job_id=%s failed for location_id=%s: %s",
            job_id,
            location_id,
            exc,
            exc_info=True,
        )

    await _mark_job_complete(factory, job_id, error=error)


async def run_worker(*, stop_event: asyncio.Event | None = None) -> None:
    """
    Background worker loop.  Claims and executes due scheduler jobs.

    Designed to run as an asyncio background task inside the FastAPI lifespan:

        task = asyncio.create_task(run_worker(stop_event=shutdown_event))

    The loop exits when:
      - ``stop_event`` is set (graceful shutdown)
      - The task is cancelled (e.g. process exit)

    Args:
        stop_event: Optional event that signals the loop to stop.
                    If None, the loop runs until cancelled.
    """
    factory = get_session_factory()
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
    active: set[asyncio.Task] = set()  # type: ignore[type-arg]

    # Recover any jobs left 'running' by a previous crashed worker process
    await recover_stuck_jobs(factory)

    logger.info("worker: started (poll_interval=%ds, max_jobs=%d)", POLL_INTERVAL_SECONDS, MAX_CONCURRENT_JOBS)

    try:
        while not (stop_event and stop_event.is_set()):
            claimed = await _claim_next_job(factory)
            if claimed is not None:
                job_id, location_id, job_type, run_type = claimed

                async def _run(
                    _jid: uuid.UUID = job_id,
                    _lid: uuid.UUID = location_id,
                    _jt: str = job_type,
                    _rt: str = run_type,
                ) -> None:
                    async with semaphore:
                        await execute_job(_jid, _lid, _jt, _rt, factory)

                task = asyncio.create_task(_run())
                active.add(task)
                task.add_done_callback(active.discard)
                # Don't sleep — immediately check for more due jobs
                continue

            # Queue empty (or no due jobs) — wait before next poll
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    except asyncio.CancelledError:
        logger.info("worker: cancelled — waiting for %d active job(s)", len(active))
    finally:
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        logger.info("worker: stopped")
