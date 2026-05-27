"""add_contacts_bookings_sessions_sync_log

Revision ID: a1b2c3d4e5f6
Revises: d3f8b2a4c1e9
Create Date: 2026-05-27

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "d3f8b2a4c1e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── contacts ──────────────────────────────────────────────────────────
    op.create_table(
        "contacts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("location_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Identity
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("email_lower", sa.Text(), nullable=True),
        sa.Column("first_name", sa.Text(), nullable=True),
        sa.Column("last_name", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("phone_raw", sa.Text(), nullable=True),
        # Eversports-sourced
        sa.Column("eversports_customer_id", sa.Text(), nullable=True),
        sa.Column("eversports_clubgroup", sa.Text(), nullable=True),
        sa.Column("eversports_newsletter_optin", sa.Boolean(), nullable=True),
        sa.Column("eversports_location_address", sa.Text(), nullable=True),
        # Package / product
        sa.Column(
            "products_purchased",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("active_package_type", sa.Text(), nullable=True),
        sa.Column("active_package_name", sa.Text(), nullable=True),
        sa.Column("active_package_expiry_date", sa.Date(), nullable=True),
        sa.Column("active_package_sessions_remaining", sa.Integer(), nullable=True),
        # Attendance / engagement derived
        sa.Column(
            "total_sessions_attended",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "no_show_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_session_date", sa.Date(), nullable=True),
        sa.Column("last_session_end_time", sa.Time(), nullable=True),
        sa.Column("last_class_name", sa.Text(), nullable=True),
        sa.Column("last_booking_date", sa.Date(), nullable=True),
        sa.Column(
            "last_no_show_email_sent_at", sa.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column(
            "sessions_attended_this_month",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "sessions_attended_last_month",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("sessions_per_week_last_month", sa.Numeric(), nullable=True),
        # GHL sync
        sa.Column("ghl_contact_id", sa.Text(), nullable=True),
        # Bootstrap tracking
        sa.Column("bootstrap_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        # Timestamps
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("location_id", "email_lower", name="uq_contacts_location_email"),
    )
    op.create_index("ix_contacts_location_id", "contacts", ["location_id"])
    op.create_index("ix_contacts_email_lower", "contacts", ["email_lower"])

    # ── bookings ──────────────────────────────────────────────────────────
    op.create_table(
        "bookings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("location_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Booking identity — synthesised sha256 when CSV has no booking ID
        sa.Column("eversports_booking_id", sa.Text(), nullable=False),
        # Session details
        sa.Column("activity_name", sa.Text(), nullable=True),
        sa.Column("session_datetime", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("session_end_datetime", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("trainer", sa.Text(), nullable=True),
        # Package / price
        sa.Column("package_used", sa.Text(), nullable=True),
        sa.Column("price", sa.Numeric(), nullable=True),
        # Attendance: 'attended' | 'no_show' | 'late_cancel' | 'unknown'
        sa.Column(
            "attendance_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'unknown'"),
        ),
        # Bootstrap tracking
        sa.Column("bootstrap_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        # Timestamps
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "location_id",
            "eversports_booking_id",
            name="uq_bookings_location_booking",
        ),
    )
    op.create_index("ix_bookings_location_id", "bookings", ["location_id"])
    op.create_index("ix_bookings_contact_id", "bookings", ["contact_id"])
    op.create_index("ix_bookings_session_datetime", "bookings", ["session_datetime"])

    # ── sessions ──────────────────────────────────────────────────────────
    op.create_table(
        "sessions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("location_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Session identity
        sa.Column("session_type", sa.Text(), nullable=True),
        sa.Column("start_time", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("end_time", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("activity_name", sa.Text(), nullable=True),
        sa.Column("activity_group", sa.Text(), nullable=True),
        sa.Column("sport", sa.Text(), nullable=True),
        sa.Column("trainer", sa.Text(), nullable=True),
        sa.Column("location_label", sa.Text(), nullable=True),
        # Capacity / attendance
        sa.Column("total_spots", sa.Integer(), nullable=True),
        sa.Column("registered_count", sa.Integer(), nullable=True),
        sa.Column("attended_count", sa.Integer(), nullable=True),
        sa.Column("waitlist_count", sa.Integer(), nullable=True),
        # Derived: max(0, total_spots - registered_count)
        sa.Column("available_spots", sa.Integer(), nullable=True),
        # Metadata
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("published", sa.Boolean(), nullable=True),
        # Bootstrap tracking
        sa.Column("bootstrap_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        # Timestamps
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "location_id",
            "start_time",
            "activity_name",
            "trainer",
            name="uq_sessions_location_start_activity_trainer",
        ),
    )
    op.create_index("ix_sessions_location_id", "sessions", ["location_id"])
    op.create_index("ix_sessions_start_time", "sessions", ["start_time"])

    # ── sync_log ──────────────────────────────────────────────────────────
    op.create_table(
        "sync_log",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("location_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Run metadata: 'bootstrap' | 'incremental' | 'full'
        sa.Column("run_type", sa.Text(), nullable=False),
        # Counters
        sa.Column(
            "contacts_processed",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "contacts_updated",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "tags_applied",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "pipeline_moves",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # Errors / warnings
        sa.Column(
            "errors",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        # Bootstrap tracking
        sa.Column("bootstrap_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        # Performance
        sa.Column("duration_seconds", sa.Numeric(), nullable=True),
        # Timestamps
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_sync_log_location_id", "sync_log", ["location_id"])


def downgrade() -> None:
    op.drop_table("sync_log")
    op.drop_table("sessions")
    op.drop_table("bookings")
    op.drop_table("contacts")
