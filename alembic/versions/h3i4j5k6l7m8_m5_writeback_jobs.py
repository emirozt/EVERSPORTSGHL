"""m5_writeback_jobs

Adds the writeback_jobs table for the M5 writeback executor (Layer 4).

Revision ID: h3i4j5k6l7m8
Revises: g2h3i4j5k6l7
Create Date: 2026-05-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "h3i4j5k6l7m8"
down_revision: str | None = "g2h3i4j5k6l7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "writeback_jobs",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("location_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("job_type", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'queued'"),
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_writeback_jobs_idempotency_key"),
    )
    op.create_index(
        "ix_writeback_jobs_location_id",
        "writeback_jobs",
        ["location_id"],
    )
    op.create_index(
        "ix_writeback_jobs_status_retry",
        "writeback_jobs",
        ["status", "next_retry_at"],
    )
    op.create_index(
        "ix_writeback_jobs_location_created",
        "writeback_jobs",
        ["location_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_writeback_jobs_location_created", table_name="writeback_jobs")
    op.drop_index("ix_writeback_jobs_status_retry", table_name="writeback_jobs")
    op.drop_index("ix_writeback_jobs_location_id", table_name="writeback_jobs")
    op.drop_table("writeback_jobs")
