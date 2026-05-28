"""M4: add scheduler_jobs table.

Adds the Postgres-backed job queue for the event-driven scheduler (M4).

Table: scheduler_jobs
  id             UUID   PK  (gen_random_uuid())
  location_id    UUID   NOT NULL  FK (no DB-level FK — locations enforced at app level)
  job_type       TEXT   NOT NULL  -- 'event_driven' | 'hourly_catchup' | 'overnight'
  run_type       TEXT   NOT NULL  DEFAULT 'incremental'
  scheduled_at   TIMESTAMPTZ NOT NULL
  status         TEXT   NOT NULL  DEFAULT 'pending'
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
  started_at     TIMESTAMPTZ NULL
  completed_at   TIMESTAMPTZ NULL
  error          TEXT   NULL

Indexes:
  ix_scheduler_jobs_location_id             (location_id)
  ix_scheduler_jobs_scheduled_at            (scheduled_at)
  ix_scheduler_jobs_status_scheduled        (status, scheduled_at) — worker poll

Revision ID: g2h3i4j5k6l7
Revises: f1a2b3c4d5e6
Create Date: 2026-05-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "g2h3i4j5k6l7"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scheduler_jobs",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("location_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("job_type", sa.Text(), nullable=False),
        sa.Column(
            "run_type",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'incremental'"),
        ),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scheduler_jobs_location_id", "scheduler_jobs", ["location_id"])
    op.create_index("ix_scheduler_jobs_scheduled_at", "scheduler_jobs", ["scheduled_at"])
    op.create_index(
        "ix_scheduler_jobs_status_scheduled",
        "scheduler_jobs",
        ["status", "scheduled_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_scheduler_jobs_status_scheduled", table_name="scheduler_jobs")
    op.drop_index("ix_scheduler_jobs_scheduled_at", table_name="scheduler_jobs")
    op.drop_index("ix_scheduler_jobs_location_id", table_name="scheduler_jobs")
    op.drop_table("scheduler_jobs")
