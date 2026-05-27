"""Contact model — one row per Eversports customer per location."""

import uuid
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Integer,
    Numeric,
    Text,
    Time,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class Contact(Base):
    __tablename__ = "contacts"

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

    # ── Identity fields ──────────────────────────────────────────────────
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_lower: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    first_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone_raw: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Eversports-sourced fields ─────────────────────────────────────────
    eversports_customer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    eversports_clubgroup: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Eversports' OWN newsletter opt-in — NOT our consent_marketing_email.
    # Used as a soft signal for consent-invitation wording only.
    eversports_newsletter_optin: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    eversports_location_address: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Package / product fields ──────────────────────────────────────────
    # JSON (→ jsonb in Postgres migration; sqlite-compatible for tests)
    products_purchased: Mapped[list] = mapped_column(  # type: ignore[type-arg]
        JSON, nullable=False, server_default=text("'[]'")
    )
    active_package_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    active_package_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    active_package_expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    active_package_sessions_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── Attendance / engagement derived fields ────────────────────────────
    total_sessions_attended: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    no_show_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_session_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_session_end_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    last_class_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_booking_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_no_show_email_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sessions_attended_this_month: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    sessions_attended_last_month: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    sessions_per_week_last_month: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)

    # ── GHL sync fields ───────────────────────────────────────────────────
    # Populated after first GHL sync (M3)
    ghl_contact_id: Mapped[str | None] = mapped_column(Text, nullable=True)

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
        UniqueConstraint("location_id", "email_lower", name="uq_contacts_location_email"),
    )
