"""
AiUsage model — per-call AI token usage log (M6b / M7).

Every AI call (gatekeeper classification, UC01/UC04/UC05 message generation,
intent detection) writes a row here for cost tracking, budget enforcement, and
audit.

Design notes:
  - APPEND ONLY — no UPDATE or DELETE in normal operation.
  - cost_usd is computed at write time from the model price card; the price card
    lives in app/ai/pricing.py (added in M7).
  - M7 adds soft-cap (80%) and hard-cap (100%) budget checks that read from this
    table's monthly aggregate per location.

Use cases:
  gatekeeper / UC01 / UC02 / UC03 / UC04 / UC05

Steps:
  classification / intent_detection / message_generation / reply_handling / summary

References:
  - requirements_v2/07_foundation_layer.md § "AI Usage Logger"
  - app/gatekeeper/audit.py  — writes gatekeeper classification rows
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Index, Integer, Numeric, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class AiUsage(Base):
    __tablename__ = "ai_usage"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,                    # Python-side (SQLite compat)
        server_default=func.gen_random_uuid(),  # Postgres server-side
    )

    location_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        # No index=True here — covered by idx_ai_usage_location_ts composite.
    )

    # GHL contact ID (text).  Nullable for location-level calls (e.g. batch).
    ghl_contact_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    # UC01 | UC02 | UC03 | UC04 | UC05 | gatekeeper
    use_case: Mapped[str] = mapped_column(Text, nullable=False)

    # classification | intent_detection | message_generation | reply_handling | summary
    step: Mapped[str] = mapped_column(Text, nullable=False)

    # e.g. "claude-haiku-4-5", "claude-sonnet-4-6"
    model: Mapped[str] = mapped_column(Text, nullable=False)

    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False)

    # Computed from model price card at write time (M7 populates this properly;
    # M6b uses a conservative Haiku estimate of $0.001/call).
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)

    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        # Primary query: monthly spend per location
        Index("idx_ai_usage_location_ts", "location_id", "ts"),
        # Cross-location usage analysis by use-case type
        Index("idx_ai_usage_use_case_ts", "use_case", "ts"),
    )
