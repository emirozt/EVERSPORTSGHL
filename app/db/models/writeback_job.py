"""
WritebackJob model — Postgres-backed job queue for M5 writeback executor.

One row per writeback action requested by a use case.  The executor polls
this table, claims jobs, calls the appropriate Playwright handler, and
updates the row to 'succeeded', 'failed', or 'dead'.

Job types:
  create_customer    — Customers → New customer form
  create_booking     — Activity calendar → Add participant
  reschedule_booking — Booking detail → Move to new session
  cancel_booking     — Booking detail → Cancel

Status lifecycle:
  queued → running → succeeded
                   ↘ failed  (attempt_count < 3; next_retry_at set)
                   ↘ dead    (attempt_count == 3; owner notified)

Idempotency:
  idempotency_key is UNIQUE — a second job with the same key is rejected
  at the DB level.  Keys are sha256(payload fields) — see safety.py for
  the derivation per job type.

References:
  - requirements_v2/07_foundation_layer.md §Layer 4
  - app/writeback/executor.py   — claims and executes jobs
  - app/writeback/handlers/     — per-job-type Playwright actions
  - app/writeback/safety.py     — WRITEBACK_SAFETY_MODE guard
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, Integer, Text, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class WritebackJob(Base):
    __tablename__ = "writeback_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    location_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        index=True,
    )

    # ── Job identity ──────────────────────────────────────────────────────────
    # 'create_customer' | 'create_booking' | 'reschedule_booking' | 'cancel_booking'
    job_type: Mapped[str] = mapped_column(Text, nullable=False)

    # JSON payload — schema depends on job_type (see spec §Layer 4).
    # Stored as JSON (SQLAlchemy cross-dialect type); Alembic migration uses
    # JSONB on Postgres for indexing.
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # sha256-derived unique key (see writeback/safety.py); prevents duplicate actions
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)

    # ── Status ────────────────────────────────────────────────────────────────
    # 'queued' | 'running' | 'succeeded' | 'failed' | 'dead'
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'queued'"),
        default="queued",
    )

    # ── Retry tracking ────────────────────────────────────────────────────────
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
        default=0,
    )
    # UTC datetime of next allowed retry; None means "claim immediately"
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # ── Timing ───────────────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # ── Error capture ─────────────────────────────────────────────────────────
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # Fast lookup for executor poll: find due queued/failed jobs ready to retry
        Index("ix_writeback_jobs_status_retry", "status", "next_retry_at"),
        # Per-location view
        Index("ix_writeback_jobs_location_created", "location_id", "created_at"),
    )
