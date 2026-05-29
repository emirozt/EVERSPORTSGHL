"""
Writeback executor — polls writeback_jobs and executes pending actions.

The executor runs as a long-lived asyncio background task started inside the
FastAPI lifespan alongside the M4 scheduler worker.

Job lifecycle:
  queued → running → succeeded
                   ↘ failed  (attempt_count < MAX_ATTEMPTS; next_retry_at set)
                   ↘ dead    (attempt_count == MAX_ATTEMPTS; owner notified)

Retry policy:
  Attempt 1 → on failure: retry after 30 seconds
  Attempt 2 → on failure: retry after 2 minutes
  Attempt 3 → on failure: mark dead, apply writeback-failed tag (GHL webhook),
               notify owner with failure payload

Idempotency:
  idempotency_key on writeback_jobs is UNIQUE — a second insert with the
  same key fails at the DB level.  Handlers must be written to tolerate
  a duplicate Playwright action that may reach Eversports (network retry
  after a timeout).  In practice, the idempotency key prevents re-queuing;
  Eversports-side deduplication is handled by the handlers themselves (e.g.
  checking if the customer already exists before creating).

Safety modes:
  WRITEBACK_SAFETY_MODE=dev (default): only whitelisted targets allowed
  WRITEBACK_DRY_RUN=true   (default): log payload, no Eversports contact

GHL webhook:
  On success: POST to ghl_success_webhook_url if configured.
  On dead:    POST to ghl_failure_webhook_url if configured.
  URLs are per-location; if not configured, webhooks are skipped (logged).

References:
  - requirements_v2/07_foundation_layer.md §Layer 4
  - app/writeback/handlers/  — per-job-type Playwright actions
  - app/writeback/safety.py  — SafetyGuard
  - app/writeback/audit.py   — audit log + email
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.writeback_job import WritebackJob
from app.db.session import get_session_factory

logger = logging.getLogger(__name__)

# ── Retry policy ──────────────────────────────────────────────────────────────

MAX_ATTEMPTS = 3
# Delay (seconds) after each failed attempt: 30s, 2min (120s), 10min (600s)
RETRY_DELAYS: list[int] = [30, 120, 600]

# How often to poll for due jobs (seconds)
POLL_INTERVAL_SECONDS = 15

# Maximum parallel writeback jobs per worker process
MAX_CONCURRENT_JOBS = 2


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _next_retry_at(attempt_count: int) -> datetime:
    """Return the datetime after which the next retry is allowed."""
    delay = RETRY_DELAYS[min(attempt_count, len(RETRY_DELAYS) - 1)]
    return _now_utc() + timedelta(seconds=delay)


# ── Job claiming ──────────────────────────────────────────────────────────────


async def _claim_next_job(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, str, dict[str, Any], str, int] | None:
    """
    Atomically claim the oldest due writeback job.

    Due = status 'queued' AND (next_retry_at IS NULL OR next_retry_at <= now).
    'failed' jobs with a due next_retry_at are also eligible.

    Returns:
        (job_id, location_id, job_type, payload, idempotency_key, attempt_count)
        or None if no due job.
    """
    async with factory() as db:
        async with db.begin():
            now = _now_utc()
            stmt = (
                select(WritebackJob)
                .where(
                    WritebackJob.status.in_(["queued", "failed"]),
                    (WritebackJob.next_retry_at.is_(None))
                    | (WritebackJob.next_retry_at <= now),
                )
                .order_by(WritebackJob.created_at)
                .limit(1)
                # Prevents two concurrent processes from claiming the same job.
                # SKIP LOCKED means a second worker moves on rather than waiting.
                .with_for_update(skip_locked=True)
            )
            result = await db.execute(stmt)
            job = result.scalar_one_or_none()
            if job is None:
                return None

            # Claim it
            job.status = "running"
            job.started_at = _now_utc()
            return (
                job.id,
                job.location_id,
                job.job_type,
                job.payload,
                job.idempotency_key,
                job.attempt_count,
            )


async def _mark_succeeded(
    factory: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
) -> None:
    async with factory() as db:
        await db.execute(
            update(WritebackJob)
            .where(WritebackJob.id == job_id)
            .values(status="succeeded", completed_at=_now_utc(), error=None)
        )
        await db.commit()


async def _mark_failed_or_dead(
    factory: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    attempt_count: int,
    error: str,
) -> str:
    """
    Increment attempt count, then:
      - If attempts < MAX_ATTEMPTS: mark 'failed', set next_retry_at
      - If attempts >= MAX_ATTEMPTS: mark 'dead'

    Returns the new status ("failed" or "dead").
    """
    new_count = attempt_count + 1
    if new_count >= MAX_ATTEMPTS:
        new_status = "dead"
        next_retry = None
    else:
        new_status = "failed"
        # Index by OLD attempt_count so delays map: 0→30s, 1→120s, 2→600s
        next_retry = _next_retry_at(attempt_count)

    async with factory() as db:
        await db.execute(
            update(WritebackJob)
            .where(WritebackJob.id == job_id)
            .values(
                status=new_status,
                attempt_count=new_count,
                next_retry_at=next_retry,
                completed_at=_now_utc() if new_status == "dead" else None,
                error=error,
            )
        )
        await db.commit()

    return new_status


# ── GHL webhook ───────────────────────────────────────────────────────────────


async def _fire_ghl_webhook(
    url: str | None,
    payload: dict[str, Any],
    label: str,
) -> str | None:
    """
    POST a result payload to a GHL inbound webhook URL.

    Args:
        url:     GHL webhook URL.  If None, skip and return None.
        payload: JSON body to send.
        label:   Label for logging ("writeback-success" | "writeback-failed").

    Returns:
        label if fired, None if skipped.
    """
    if not url:
        logger.debug("executor: no GHL webhook configured for %s — skipped", label)
        return None

    import httpx  # noqa: PLC0415

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        logger.info("executor: GHL webhook fired: %s → %s", label, url)
        return label
    except Exception as exc:  # noqa: BLE001
        logger.warning("executor: GHL webhook %s failed: %s", label, exc)
        return None


# ── Job execution ─────────────────────────────────────────────────────────────

HANDLER_MAP = {
    "create_customer": "app.writeback.handlers.create_customer.handle_create_customer",
    "create_booking": "app.writeback.handlers.create_booking.handle_create_booking",
    "reschedule_booking": "app.writeback.handlers.reschedule_booking.handle_reschedule_booking",
    "cancel_booking": "app.writeback.handlers.cancel_booking.handle_cancel_booking",
}


async def _load_handler(job_type: str):  # type: ignore[return]
    """Lazily import the handler function for a job type."""
    import importlib  # noqa: PLC0415

    dotted = HANDLER_MAP.get(job_type)
    if dotted is None:
        raise ValueError(f"No handler registered for job_type='{job_type}'")
    module_path, func_name = dotted.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, func_name)


async def execute_writeback_job(
    job_id: uuid.UUID,
    location_id: uuid.UUID,
    job_type: str,
    payload: dict[str, Any],
    idempotency_key: str,
    attempt_count: int,
    factory: async_sessionmaker[AsyncSession],
    *,
    dry_run: bool | None = None,
    safety_mode: str | None = None,
    ghl_success_webhook_url: str | None = None,
    ghl_failure_webhook_url: str | None = None,
) -> None:
    """
    Execute a single writeback job.

    This function is separated from the worker loop so it can be tested in
    isolation.

    Args:
        job_id:                  WritebackJob.id to update on completion.
        location_id:             Location UUID to pass to the handler.
        job_type:                One of create_customer / create_booking /
                                 reschedule_booking / cancel_booking.
        payload:                 Job payload dict.
        idempotency_key:         sha256 key (logged in audit).
        attempt_count:           Current attempt number (before this execution).
        factory:                 DB session factory.
        dry_run:                 Override WRITEBACK_DRY_RUN from settings if provided.
        safety_mode:             Override WRITEBACK_SAFETY_MODE from settings if provided.
        ghl_success_webhook_url: URL to POST on success (optional).
        ghl_failure_webhook_url: URL to POST on dead (optional).
    """
    from app.config import get_settings  # noqa: PLC0415
    from app.writeback.audit import AuditError, record_writeback  # noqa: PLC0415
    from app.writeback.safety import SafetyGuardError  # noqa: PLC0415

    settings = get_settings()
    effective_dry_run = dry_run if dry_run is not None else settings.writeback_dry_run
    effective_safety_mode = safety_mode or settings.writeback_safety_mode

    logger.info(
        "executor: executing job_id=%s job_type=%s attempt=%d location_id=%s "
        "dry_run=%s safety_mode=%s",
        job_id,
        job_type,
        attempt_count + 1,
        location_id,
        effective_dry_run,
        effective_safety_mode,
    )

    error: str | None = None
    result: dict[str, Any] | None = None

    try:
        handler = await _load_handler(job_type)
        result = await handler(
            payload,
            str(location_id),
            dry_run=effective_dry_run,
            safety_mode=effective_safety_mode,
        )
        logger.info(
            "executor: job_id=%s succeeded result=%s",
            job_id,
            result,
        )

        # ── Audit log — always written (dry_run or live) ──────────────────────
        # Resolve customer email from whichever field the job type uses:
        #   create_customer         → payload["email"]
        #   create/cancel_booking   → payload["customer_email"]  (if present)
        #   reschedule_booking      → payload["customer_email"]
        # create_booking has no email field in the spec — fall back to
        # "customer_id:<id>" so the audit entry is never blank.
        customer_email = (
            payload.get("email")
            or payload.get("customer_email")
            or (
                f"customer_id:{payload['customer_id']}"
                if "customer_id" in payload
                else ""
            )
        )
        class_name = payload.get("class_name") or payload.get("new_class_name")
        start_dt_str = payload.get("session_datetime") or payload.get("new_session_datetime")
        start_dt = None
        if start_dt_str:
            try:
                from datetime import datetime as _dt  # noqa: PLC0415
                start_dt = _dt.fromisoformat(start_dt_str)
            except ValueError:
                pass

        # Fire GHL webhook for live mode only
        ghl_fired = None
        if not effective_dry_run:
            ghl_fired = await _fire_ghl_webhook(
                ghl_success_webhook_url,
                {"job_type": job_type, "result": result, "idempotency_key": idempotency_key},
                "writeback-success",
            )

        audit_mode = "dry_run" if effective_dry_run else "live"
        await record_writeback(
            action=job_type,
            customer_email=customer_email,
            class_name=class_name,
            start_dt=start_dt,
            idempotency_key=idempotency_key,
            eversports_response=result,
            ghl_webhook_fired=ghl_fired,
            mode=audit_mode,
        )

        await _mark_succeeded(factory, job_id)

    except SafetyGuardError as exc:
        # Safety violations are hard failures — no retry, mark dead immediately
        error = f"SafetyGuardError: {exc}"
        logger.error(
            "executor: job_id=%s SAFETY GUARD violation — marking dead: %s",
            job_id,
            exc,
        )
        # Force attempt_count to MAX_ATTEMPTS so it goes straight to dead
        await _mark_failed_or_dead(factory, job_id, MAX_ATTEMPTS - 1, error)
        await _fire_ghl_webhook(
            ghl_failure_webhook_url,
            {"job_type": job_type, "error": error, "idempotency_key": idempotency_key},
            "writeback-failed",
        )
        return

    except AuditError as exc:
        # Audit failure = test failure per safety constraints
        error = f"AuditError: {exc}"
        logger.error(
            "executor: job_id=%s audit failure — marking dead: %s",
            job_id,
            exc,
        )
        await _mark_failed_or_dead(factory, job_id, MAX_ATTEMPTS - 1, error)
        return

    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        logger.error(
            "executor: job_id=%s failed (attempt %d/%d): %s",
            job_id,
            attempt_count + 1,
            MAX_ATTEMPTS,
            exc,
            exc_info=True,
        )
        new_status = await _mark_failed_or_dead(factory, job_id, attempt_count, error)
        if new_status == "dead":
            logger.warning(
                "executor: job_id=%s marked DEAD after %d attempts — firing owner notification",
                job_id,
                MAX_ATTEMPTS,
            )
            await _fire_ghl_webhook(
                ghl_failure_webhook_url,
                {"job_type": job_type, "error": error, "idempotency_key": idempotency_key},
                "writeback-failed",
            )


# ── Worker loop ───────────────────────────────────────────────────────────────


async def run_writeback_worker(*, stop_event: asyncio.Event | None = None) -> None:
    """
    Background worker loop.  Claims and executes due writeback jobs.

    Designed to run as an asyncio background task inside the FastAPI lifespan:

        task = asyncio.create_task(run_writeback_worker(stop_event=shutdown_event))

    Args:
        stop_event: Optional event that signals the loop to stop.
    """
    factory = get_session_factory()
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
    active: set[asyncio.Task] = set()  # type: ignore[type-arg]

    logger.info(
        "writeback_worker: started (poll_interval=%ds, max_jobs=%d)",
        POLL_INTERVAL_SECONDS,
        MAX_CONCURRENT_JOBS,
    )

    try:
        while not (stop_event and stop_event.is_set()):
            claimed = await _claim_next_job(factory)
            if claimed is not None:
                job_id, location_id, job_type, payload, idempotency_key, attempt_count = claimed

                async def _run(
                    _jid: uuid.UUID = job_id,
                    _lid: uuid.UUID = location_id,
                    _jt: str = job_type,
                    _p: dict[str, Any] = payload,
                    _ik: str = idempotency_key,
                    _ac: int = attempt_count,
                ) -> None:
                    async with semaphore:
                        await execute_writeback_job(
                            _jid, _lid, _jt, _p, _ik, _ac, factory
                        )

                task = asyncio.create_task(_run())
                active.add(task)
                task.add_done_callback(active.discard)
                continue  # immediately check for more due jobs

            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    except asyncio.CancelledError:
        logger.info(
            "writeback_worker: cancelled — waiting for %d active job(s)", len(active)
        )
    finally:
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        logger.info("writeback_worker: stopped")
