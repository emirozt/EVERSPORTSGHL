"""Sync log model — one row per sync run for observability and audit."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, DateTime, Integer, Numeric, Text, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class SyncLog(Base):
    __tablename__ = "sync_log"

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

    # ── Run metadata ──────────────────────────────────────────────────────
    # Values: 'bootstrap' | 'incremental' | 'full'
    run_type: Mapped[str] = mapped_column(Text, nullable=False)

    # ── Counters ──────────────────────────────────────────────────────────
    contacts_processed: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    contacts_updated: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    tags_applied: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    pipeline_moves: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # ── Errors / warnings ────────────────────────────────────────────────
    # JSON (→ jsonb in Postgres migration; sqlite-compatible for tests)
    errors: Mapped[list] = mapped_column(  # type: ignore[type-arg]
        JSON, nullable=False, server_default=text("'[]'")
    )

    # ── GHL push counters (populated after M3 GHL sync) ─────────────────────
    ghl_contacts_synced: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    ghl_contacts_created: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    ghl_contacts_failed: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    # ── Bootstrap tracking ────────────────────────────────────────────────
    bootstrap_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    # ── Performance ───────────────────────────────────────────────────────
    duration_seconds: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)

    # ── Timestamps ───────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
