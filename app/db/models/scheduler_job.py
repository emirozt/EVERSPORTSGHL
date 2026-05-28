"""
SchedulerJob model — Postgres-backed job queue for M4 event-driven scheduler.

One row per enqueued sync run.  The worker polls this table and claims jobs
by updating status to 'running', then marks them 'done' or 'failed'.

Job types:
  event_driven   — triggered by class-end + 15 min (from sessions table)
  hourly_catchup — hourly slot within 07:00–22:00 local time window
  overnight      — nightly full reconciliation at 03:00 local time

Status lifecycle:
  pending → running → done
                    ↘ failed

See:
  - requirements_v2/07_foundation_layer.md §Layer 1
  - app/scheduler/orchestrator.py — enqueue helpers
  - app/scheduler/worker.py       — claims and executes jobs
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Text, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class SchedulerJob(Base):
    __tablename__ = "scheduler_jobs"

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

    # ── Job identity ──────────────────────────────────────────────────────
    # 'event_driven' | 'hourly_catchup' | 'overnight'
    job_type: Mapped[str] = mapped_column(Text, nullable=False)

    # 'incremental' | 'historical_backfill'
    run_type: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'incremental'"),
        default="incremental",
    )

    # ── Scheduling ────────────────────────────────────────────────────────
    # UTC datetime when the job should be executed
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )

    # ── Status ────────────────────────────────────────────────────────────
    # 'pending' | 'running' | 'done' | 'failed'
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'pending'"),
        default="pending",
    )

    # ── Timing ───────────────────────────────────────────────────────────
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

    # ── Error capture ─────────────────────────────────────────────────────
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # Fast lookup for worker poll: find due pending jobs
        Index("ix_scheduler_jobs_status_scheduled", "status", "scheduled_at"),
    )
