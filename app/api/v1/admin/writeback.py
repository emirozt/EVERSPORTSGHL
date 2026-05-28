"""
Writeback admin endpoints.

Provides visibility into and manual control of the M5 writeback executor.

Endpoints:
  POST /api/v1/admin/writeback/jobs
      Enqueue a writeback job.  Body: {"location_id", "job_type", "payload"}.
      Returns the created WritebackJob row.

  GET  /api/v1/admin/writeback/status
      Aggregate counts by status; dry_run + safety_mode from settings.

  GET  /api/v1/admin/writeback/jobs/{location_id}
      List recent writeback_jobs for a location (last 50, newest first).

  DELETE /api/v1/admin/writeback/jobs/{location_id}/pending
      Cancel all queued/failed jobs for a location (marks them 'dead' with
      reason 'cancelled_by_admin').

Security:
  All endpoints require ``X-Admin-Key`` when ``ADMIN_API_KEY`` is configured.

References:
  - requirements_v2/07_foundation_layer.md §Layer 4
  - app/writeback/executor.py    — worker + execute_writeback_job
  - app/writeback/safety.py      — SafetyGuard, idempotency key helpers
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models.writeback_job import WritebackJob
from app.db.session import get_db
from app.writeback.safety import SafetyGuard, get_safety_guard

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/writeback", tags=["writeback"])

# ── Admin auth (shared pattern with scheduler.py) ─────────────────────────────

_admin_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)


async def _require_admin(
    key: str | None = Security(_admin_key_header),
) -> None:
    settings = get_settings()
    if not settings.admin_api_key:
        return
    if not key or key != settings.admin_api_key:
        raise HTTPException(
            status_code=403,
            detail="Invalid or missing X-Admin-Key header",
        )


# ── Request / Response models ─────────────────────────────────────────────────


class EnqueueRequest(BaseModel):
    location_id: uuid.UUID
    job_type: str  # create_customer | create_booking | reschedule_booking | cancel_booking
    payload: dict[str, Any]


class WritebackJobSummary(BaseModel):
    id: str
    location_id: str
    job_type: str
    idempotency_key: str
    status: str
    attempt_count: int
    next_retry_at: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None
    error: str | None


class WritebackJobListResponse(BaseModel):
    location_id: str
    jobs: list[WritebackJobSummary]
    count: int


# ── Helpers ───────────────────────────────────────────────────────────────────

_VALID_JOB_TYPES = frozenset(
    {"create_customer", "create_booking", "reschedule_booking", "cancel_booking"}
)


def _fmt_dt(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _job_to_summary(job: WritebackJob) -> WritebackJobSummary:
    return WritebackJobSummary(
        id=str(job.id),
        location_id=str(job.location_id),
        job_type=job.job_type,
        idempotency_key=job.idempotency_key,
        status=job.status,
        attempt_count=job.attempt_count,
        next_retry_at=_fmt_dt(job.next_retry_at),
        created_at=_fmt_dt(job.created_at) or "",
        started_at=_fmt_dt(job.started_at),
        completed_at=_fmt_dt(job.completed_at),
        error=job.error,
    )


def _make_idempotency_key(
    job_type: str, location_id: str, payload: dict[str, Any]
) -> str:
    """Derive the idempotency key for a job based on its type and payload."""
    guard = SafetyGuard(mode="prod")  # key derivation is mode-agnostic
    if job_type == "create_customer":
        return guard.make_create_customer_key(location_id, payload.get("email", ""))
    if job_type == "create_booking":
        return guard.make_create_booking_key(
            payload.get("customer_id", ""),
            payload.get("session_id", ""),
        )
    if job_type == "reschedule_booking":
        return guard.make_reschedule_booking_key(
            payload.get("booking_id", ""),
            payload.get("new_session_id", ""),
        )
    if job_type == "cancel_booking":
        return guard.make_cancel_booking_key(payload.get("booking_id", ""))
    raise ValueError(f"Unknown job_type '{job_type}'")


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/status", dependencies=[Depends(_require_admin)])
async def writeback_status(
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Return aggregate counts of writeback_jobs by status, plus current
    safety mode and dry-run flag.
    """
    from app.writeback.executor import MAX_ATTEMPTS, POLL_INTERVAL_SECONDS  # noqa: PLC0415

    settings = get_settings()
    stmt = (
        select(WritebackJob.status, func.count(WritebackJob.id).label("n"))
        .group_by(WritebackJob.status)
    )
    rows = (await db.execute(stmt)).fetchall()
    counts: dict[str, int] = {
        "queued": 0, "running": 0, "succeeded": 0, "failed": 0, "dead": 0,
    }
    for row_status, n in rows:
        counts[row_status] = n

    return {
        "counts": counts,
        "total": sum(counts.values()),
        "writeback_dry_run": settings.writeback_dry_run,
        "writeback_safety_mode": settings.writeback_safety_mode,
        "max_attempts": MAX_ATTEMPTS,
        "worker_poll_interval_seconds": POLL_INTERVAL_SECONDS,
    }


@router.post("/jobs", dependencies=[Depends(_require_admin)], status_code=status.HTTP_201_CREATED)
async def enqueue_writeback_job(
    body: EnqueueRequest = Body(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Enqueue a writeback job for manual triggering (admin / testing).

    Validates job_type, derives the idempotency key, and inserts the row.
    Returns 409 if a non-dead job with the same idempotency key already exists.
    """
    from app.writeback.safety import SafetyGuardError  # noqa: PLC0415
    from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

    if body.job_type not in _VALID_JOB_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"job_type must be one of {sorted(_VALID_JOB_TYPES)}",
        )

    # Validate against safety guard
    settings = get_settings()
    if settings.writeback_safety_mode == "dev":
        try:
            guard = get_safety_guard()
            if body.job_type == "create_customer":
                guard.check_create_customer(body.payload.get("email", ""))
            elif body.job_type in ("create_booking", "reschedule_booking", "cancel_booking"):
                class_key = "new_class_name" if body.job_type == "reschedule_booking" else "class_name"
                dt_key = "new_session_datetime" if body.job_type == "reschedule_booking" else "session_datetime"
                from datetime import datetime as _dt  # noqa: PLC0415
                from datetime import timezone as _tz  # noqa: PLC0415
                raw_dt = body.payload.get(dt_key, "")
                try:
                    parsed_dt = _dt.fromisoformat(raw_dt)
                    if parsed_dt.tzinfo is None:
                        parsed_dt = parsed_dt.replace(tzinfo=_tz.utc)
                except (ValueError, TypeError):
                    parsed_dt = _dt.now(_tz.utc)
                guard.check_booking_target(body.payload.get(class_key, ""), parsed_dt)
        except SafetyGuardError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc

    idem_key = _make_idempotency_key(
        body.job_type, str(body.location_id), body.payload
    )

    job = WritebackJob(
        id=uuid.uuid4(),
        location_id=body.location_id,
        job_type=body.job_type,
        payload=body.payload,
        idempotency_key=idem_key,
    )

    try:
        db.add(job)
        await db.flush()
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A non-dead job with idempotency_key '{idem_key}' already exists.",
        )

    logger.info(
        "writeback/enqueue: job_id=%s job_type=%s location_id=%s idem_key=%s",
        job.id,
        body.job_type,
        body.location_id,
        idem_key,
    )
    return {
        "job_id": str(job.id),
        "idempotency_key": idem_key,
        "status": "queued",
    }


@router.get(
    "/jobs/{location_id}",
    response_model=WritebackJobListResponse,
    dependencies=[Depends(_require_admin)],
)
async def list_writeback_jobs(
    location_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    limit: int = 50,
) -> WritebackJobListResponse:
    """List the most recent writeback jobs for a location (newest first)."""
    stmt = (
        select(WritebackJob)
        .where(WritebackJob.location_id == location_id)
        .order_by(WritebackJob.created_at.desc())
        .limit(min(limit, 200))
    )
    jobs = (await db.execute(stmt)).scalars().all()
    return WritebackJobListResponse(
        location_id=str(location_id),
        jobs=[_job_to_summary(j) for j in jobs],
        count=len(jobs),
    )


@router.delete(
    "/jobs/{location_id}/pending",
    dependencies=[Depends(_require_admin)],
)
async def cancel_pending_writeback_jobs(
    location_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Cancel (mark dead) all queued/failed jobs for a location."""
    stmt = (
        update(WritebackJob)
        .where(
            WritebackJob.location_id == location_id,
            WritebackJob.status.in_(["queued", "failed"]),
        )
        .values(
            status="dead",
            completed_at=datetime.now(tz=timezone.utc),
            error="cancelled_by_admin",
        )
    )
    result = await db.execute(stmt)
    await db.commit()
    cancelled = result.rowcount

    logger.info(
        "writeback/cancel_pending: location_id=%s cancelled=%d",
        location_id,
        cancelled,
    )
    return {"location_id": str(location_id), "cancelled": cancelled}
