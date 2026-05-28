"""
Scheduler admin endpoints.

Provides visibility into and manual control of the M4 event-driven scheduler.

Endpoints:
  GET  /api/v1/admin/scheduler/status
      Returns aggregate job counts by status and a summary of the scheduler state.

  GET  /api/v1/admin/scheduler/jobs/{location_id}
      Lists recent scheduler_jobs for a given location (last 50, newest first).

  POST /api/v1/admin/scheduler/trigger/{location_id}
      Immediately enqueues all three job types for a location (useful for
      manual resyncs and integration testing).
      Body: {"job_type": "event_driven" | "hourly_catchup" | "overnight" | "all"}

  DELETE /api/v1/admin/scheduler/jobs/{location_id}/pending
      Marks all pending jobs for a location as 'failed' with reason
      'cancelled_by_admin'.  Does not affect running jobs.

Security:
  All endpoints require ``X-Admin-Key`` when ``ADMIN_API_KEY`` is configured
  (same guard as ghl_oauth.py).

References:
  - requirements_v2/07_foundation_layer.md §Layer 1
  - app/scheduler/orchestrator.py — enqueue helpers
  - app/scheduler/worker.py       — worker state
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models.scheduler_job import SchedulerJob
from app.db.session import get_db
from app.scheduler.orchestrator import (
    enqueue_event_driven_jobs,
    enqueue_hourly_catchup,
    enqueue_overnight,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scheduler", tags=["scheduler"])


# ── Admin auth (shared with ghl_oauth.py) ─────────────────────────────────────

_admin_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)


async def _require_admin(
    key: str | None = Security(_admin_key_header),
) -> None:
    settings = get_settings()
    if not settings.admin_api_key:
        return  # Open mode for local dev
    if not key or key != settings.admin_api_key:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Admin-Key header")


# ── Request / Response models ──────────────────────────────────────────────────


class TriggerRequest(BaseModel):
    """Body for POST /scheduler/trigger/{location_id}."""

    job_type: str = "all"  # "event_driven" | "hourly_catchup" | "overnight" | "all"


class JobSummary(BaseModel):
    id: str
    location_id: str
    job_type: str
    run_type: str
    scheduled_at: str
    status: str
    created_at: str
    started_at: str | None
    completed_at: str | None
    error: str | None


# ── Helpers ────────────────────────────────────────────────────────────────────


def _fmt_dt(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _job_to_summary(job: SchedulerJob) -> dict[str, Any]:
    return {
        "id": str(job.id),
        "location_id": str(job.location_id),
        "job_type": job.job_type,
        "run_type": job.run_type,
        "scheduled_at": _fmt_dt(job.scheduled_at),
        "status": job.status,
        "created_at": _fmt_dt(job.created_at),
        "started_at": _fmt_dt(job.started_at),
        "completed_at": _fmt_dt(job.completed_at),
        "error": job.error,
    }


# ── Endpoints ──────────────────────────────────────────────────────────────────


@router.get("/status", dependencies=[Depends(_require_admin)])
async def scheduler_status(
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Return aggregate counts of scheduler_jobs by status.

    Response:
      {
        "counts": {"pending": N, "running": N, "done": N, "failed": N},
        "total": N,
        "worker_poll_interval_seconds": 30
      }
    """
    from app.scheduler.worker import POLL_INTERVAL_SECONDS  # noqa: PLC0415

    stmt = (
        select(SchedulerJob.status, func.count(SchedulerJob.id).label("n"))
        .group_by(SchedulerJob.status)
    )
    rows = (await db.execute(stmt)).fetchall()
    counts: dict[str, int] = {"pending": 0, "running": 0, "done": 0, "failed": 0}
    for row_status, n in rows:
        counts[row_status] = n

    return {
        "counts": counts,
        "total": sum(counts.values()),
        "worker_poll_interval_seconds": POLL_INTERVAL_SECONDS,
    }


@router.get("/jobs/{location_id}", dependencies=[Depends(_require_admin)])
async def list_jobs(
    location_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    limit: int = 50,
) -> dict[str, Any]:
    """
    List the most recent scheduler_jobs for a location (newest first).

    Query params:
      limit (int, default 50) — max rows to return
    """
    stmt = (
        select(SchedulerJob)
        .where(SchedulerJob.location_id == location_id)
        .order_by(SchedulerJob.scheduled_at.desc())
        .limit(min(limit, 200))
    )
    jobs = (await db.execute(stmt)).scalars().all()
    return {
        "location_id": str(location_id),
        "jobs": [_job_to_summary(j) for j in jobs],
        "count": len(jobs),
    }


@router.post("/trigger/{location_id}", dependencies=[Depends(_require_admin)])
async def trigger_jobs(
    location_id: uuid.UUID,
    body: TriggerRequest = Depends(),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Immediately enqueue sync jobs for a location.

    ``job_type`` controls which job types are enqueued:
      "event_driven"   — enqueue today's event-driven slots now
      "hourly_catchup" — enqueue one hourly-catchup job for the next hour
      "overnight"      — enqueue tonight's overnight job
      "all"            — enqueue all three types (default)

    Returns:
      {
        "location_id": "...",
        "enqueued": {
          "event_driven": N,   // 0 if not requested
          "hourly_catchup": T, // true/false
          "overnight": T
        }
      }
    """
    valid = {"event_driven", "hourly_catchup", "overnight", "all"}
    if body.job_type not in valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"job_type must be one of {sorted(valid)}",
        )

    result: dict[str, Any] = {
        "event_driven": 0,
        "hourly_catchup": False,
        "overnight": False,
    }

    if body.job_type in ("event_driven", "all"):
        result["event_driven"] = await enqueue_event_driven_jobs(location_id, db)

    if body.job_type in ("hourly_catchup", "all"):
        result["hourly_catchup"] = await enqueue_hourly_catchup(location_id, db)

    if body.job_type in ("overnight", "all"):
        result["overnight"] = await enqueue_overnight(location_id, db)

    await db.commit()

    logger.info(
        "scheduler/trigger: location_id=%s job_type=%s → %s",
        location_id,
        body.job_type,
        result,
    )
    return {"location_id": str(location_id), "enqueued": result}


@router.delete("/jobs/{location_id}/pending", dependencies=[Depends(_require_admin)])
async def cancel_pending_jobs(
    location_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Cancel (mark failed) all pending jobs for a location.

    Running jobs are not affected.  Returns the number of jobs cancelled.
    """
    stmt = (
        update(SchedulerJob)
        .where(
            SchedulerJob.location_id == location_id,
            SchedulerJob.status == "pending",
        )
        .values(
            status="failed",
            completed_at=datetime.now(tz=timezone.utc),
            error="cancelled_by_admin",
        )
    )
    result = await db.execute(stmt)
    await db.commit()
    cancelled = result.rowcount

    logger.info(
        "scheduler/cancel_pending: location_id=%s cancelled=%d",
        location_id,
        cancelled,
    )
    return {"location_id": str(location_id), "cancelled": cancelled}
