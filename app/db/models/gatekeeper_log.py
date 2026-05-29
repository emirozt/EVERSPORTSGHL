"""
GatekeeperLog model — inbound message classification log (M6b).

One row per inbound message processed by the gatekeeper.  Rows are written
even for noise (silent_ignore) decisions so the owner can audit what was
filtered and override if needed.

Design constraints:
  - APPEND ONLY — no UPDATE except the owner_override / override_ts columns
    (owner reclassification).  All other columns are immutable after insert.
  - ghl_contact_id is nullable because some inbound surfaces (Instagram
    comments from new followers) may not yet be matched to a GHL contact.
  - contact_id is a nullable internal FK; populated once the GHL contact is
    synced into our contacts table.

Classification categories (15):
  inquiry_pricing | inquiry_class_info | inquiry_membership | booking |
  trial_reply | complaint | injury_medical | billing_dispute | opt_out |
  acknowledgment | emoji_reaction | social_compliment | off_topic | spam |
  low_confidence

Route-to values:
  uc04 | uc05 | owner | noise | consent_gate | legacy

Action-taken values (dynamic — see app/gatekeeper/router.py for full list):
  routed_<category>        — inquiry / booking route (e.g. routed_booking)
  escalated_<category>     — owner escalation (e.g. escalated_complaint)
  consent_gate_opt_out     — opt_out classified by Haiku, escalated to consent gate
  silent_ignore            — noise: do nothing
  react_emoji              — noise: emoji reaction / reply
  auto_reply_template      — noise: short localised reply
  legacy_uc04              — gatekeeper disabled, routed to legacy UC04 path

References:
  - requirements_v2/07_foundation_layer.md § "Layer 6 — Gatekeeper"
  - app/gatekeeper/audit.py    — write helpers
  - app/gatekeeper/classifier.py — AI classification
  - app/gatekeeper/router.py   — routing logic
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Index, Numeric, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class GatekeeperLog(Base):
    __tablename__ = "gatekeeper_log"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,                    # Python-side (SQLite compat)
        server_default=func.gen_random_uuid(),  # Postgres server-side
    )

    location_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        # No index=True — covered by idx_gatekeeper_log_location_ts composite.
    )

    # GHL contact ID (text).  Nullable when contact is not yet resolved.
    ghl_contact_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Optional internal contacts.id FK (nullable — not yet synced).
    # No index=True — covered by idx_gatekeeper_log_contact_id below.
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
    )

    # whatsapp | email | instagram_dm | instagram_comment | facebook_dm | facebook_comment
    inbound_channel: Mapped[str] = mapped_column(Text, nullable=False)

    # Additional surface context (e.g. "post_abc123" for Instagram comments).
    inbound_surface: Mapped[str | None] = mapped_column(Text, nullable=True)

    # GHL message ID (from payload, nullable if not provided).
    ghl_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Raw inbound text (stored for owner audit / training data).
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)

    # One of the 15 classification categories.
    classification: Mapped[str] = mapped_column(Text, nullable=False)

    # Model confidence score [0.0, 1.0].
    confidence: Mapped[Decimal] = mapped_column(
        Numeric(4, 3),  # 0.000 → 1.000
        nullable=False,
    )

    # uc04 | uc05 | owner | noise | consent_gate | legacy
    route_to: Mapped[str] = mapped_column(Text, nullable=False)

    # silent_ignore | react_emoji | auto_reply_template | escalated |
    # routed_uc04 | routed_uc05 | consent_gate | legacy_uc04
    action_taken: Mapped[str] = mapped_column(Text, nullable=False)

    # Owner reclassification — set via the override API.
    owner_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    override_ts: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        # Primary access pattern: per-location timeline
        Index("idx_gatekeeper_log_location_ts", "location_id", "ts"),
        # Per-contact message history
        Index("idx_gatekeeper_log_contact_id", "contact_id"),
        # Category analysis and owner audit sweep
        Index("idx_gatekeeper_log_classification", "classification", "ts"),
    )
