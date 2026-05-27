"""Session model — one row per activity session from the Eversports activities export."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class Session(Base):
    __tablename__ = "sessions"

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

    # ── Session identity ──────────────────────────────────────────────────
    session_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activity_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    activity_group: Mapped[str | None] = mapped_column(Text, nullable=True)
    sport: Mapped[str | None] = mapped_column(Text, nullable=True)
    trainer: Mapped[str | None] = mapped_column(Text, nullable=True)
    location_label: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Capacity / attendance ─────────────────────────────────────────────
    total_spots: Mapped[int | None] = mapped_column(Integer, nullable=True)
    registered_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attended_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    waitlist_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Derived at parse time: max(0, total_spots - registered_count)
    # Used by UC05 availability check — see 07_foundation_layer.md § UC05 availability freshness
    available_spots: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── Metadata ──────────────────────────────────────────────────────────
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    published: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # ── Bootstrap tracking ────────────────────────────────────────────────
    bootstrap_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    # ── Timestamps ───────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "location_id",
            "start_time",
            "activity_name",
            "trainer",
            name="uq_sessions_location_start_activity_trainer",
        ),
    )
