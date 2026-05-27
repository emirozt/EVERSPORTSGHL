"""Booking model — one row per Eversports booking."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Numeric, Text, UniqueConstraint, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class Booking(Base):
    __tablename__ = "bookings"

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
    contact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        index=True,
    )

    # ── Booking identity ──────────────────────────────────────────────────
    # Synthesised as sha256(location_id|email_lower|session_datetime.isoformat()|activity_name)
    # when the CSV has no explicit booking ID column (which is the case for all known exports).
    eversports_booking_id: Mapped[str] = mapped_column(Text, nullable=False)

    # ── Session details ───────────────────────────────────────────────────
    activity_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_datetime: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    session_end_datetime: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    trainer: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Package / price ───────────────────────────────────────────────────
    package_used: Mapped[str | None] = mapped_column(Text, nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)

    # ── Attendance ────────────────────────────────────────────────────────
    # Values: 'attended' | 'no_show' | 'late_cancel' | 'unknown'
    attendance_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'unknown'")
    )

    # ── Bootstrap tracking ────────────────────────────────────────────────
    bootstrap_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    # ── Timestamps ───────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "location_id",
            "eversports_booking_id",
            name="uq_bookings_location_booking",
        ),
    )
