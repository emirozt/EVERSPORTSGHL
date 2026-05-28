"""
ConsentAudit model — append-only consent event log (M6).

This table is the tamper-evident audit trail for all marketing consent
events: grants, revocations, blocked sends, and preference-centre updates.

Design constraints:
  - APPEND ONLY — no UPDATE or DELETE in normal operation.
  - All columns are non-nullable except value, message_shown, ip, contact_id.
  - contact_id is a nullable FK to contacts.id so that events for contacts
    that haven't been scraped yet (or opted out before bootstrap) can still
    be recorded.
  - Retention: 6 years (DSGVO Art. 17).

Event types:
  granted              — consent was captured (new opt-in)
  revoked              — consent was revoked (STOP / preference-centre)
  blocked-send         — outbound send blocked by consent gate
  preference-centre-update — customer changed any channel preference

Source enum:
  onboarding-form / double-opt-in / studio-import / preference-centre /
  whatsapp-opt-in / stop-keyword / unsubscribe-link

Actor enum:
  system / customer / studio-staff

References:
  - requirements_v2/08_consent_model.md — full consent model
  - app/consent/gate.py  — reads from GHL; writes blocked-send rows here
  - app/consent/record.py — write helpers for this table
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class ConsentAudit(Base):
    __tablename__ = "consent_audit"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,                   # Python-side (SQLite compat)
        server_default=func.gen_random_uuid(), # Postgres server-side fallback
    )

    # GHL contact ID (text, always present — even before our contacts row exists)
    # No index=True here — the composite idx_consent_audit_ghl_contact_ts in
    # __table_args__ is the right index for the primary access pattern.
    # index=True would generate ix_consent_audit_ghl_contact_id (different name
    # from the migration's idx_*) and create a redundant single-col index.
    ghl_contact_id: Mapped[str] = mapped_column(Text, nullable=False)

    # Optional FK to our internal contacts table (populated when contact is known)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
        # Named in __table_args__ to match the migration's idx_consent_audit_contact_id.
        # Do NOT add index=True here — that would generate ix_consent_audit_contact_id
        # (different name) and create a duplicate index if create_all() is ever called.
    )

    # No index=True — covered by the composite idx_consent_audit_location_ts in
    # __table_args__. index=True would generate ix_consent_audit_location_id
    # (naming drift vs migration) and create a redundant single-col index.
    location_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )

    # email | whatsapp | voice
    channel: Mapped[str] = mapped_column(Text, nullable=False)

    # granted | revoked | blocked-send | preference-centre-update
    event: Mapped[str] = mapped_column(Text, nullable=False)

    # New boolean value for grant/revoke events; None for blocked-send
    value: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # onboarding-form | double-opt-in | studio-import | preference-centre |
    # whatsapp-opt-in | stop-keyword | unsubscribe-link
    source: Mapped[str] = mapped_column(Text, nullable=False)

    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # system | customer | studio-staff
    actor: Mapped[str] = mapped_column(Text, nullable=False)

    # Snapshot of the exact consent copy shown (for audit purposes)
    message_shown: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Inbound IP address if this event was HTTP-sourced (preference-centre, opt-in form)
    ip: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # Primary access pattern: events for a specific contact ordered by time
        Index("idx_consent_audit_ghl_contact_ts", "ghl_contact_id", "ts"),
        # Per-location audit sweep
        Index("idx_consent_audit_location_ts", "location_id", "ts"),
        # Internal contact FK — name matches migration to avoid duplicate-index drift
        Index("idx_consent_audit_contact_id", "contact_id"),
    )
