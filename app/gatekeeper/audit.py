"""
Gatekeeper audit helpers (M6b).

Two append-only write operations:
  1. log_classification() → GatekeeperLog row (inbound message + decision)
  2. log_ai_usage()        → AiUsage row (token cost for billing roll-up)

One mutable write:
  3. apply_owner_override() → sets GatekeeperLog.owner_override + override_ts

None of these commit the session — the caller is responsible for db.commit()
to keep the write atomic with surrounding business logic.

References:
  - requirements_v2/07_foundation_layer.md § "Layer 6 — Gatekeeper"
  - app/gatekeeper/classifier.py  — ClassificationResult consumed here
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.ai_usage import AiUsage
from app.db.models.gatekeeper_log import GatekeeperLog
from app.gatekeeper.classifier import ClassificationResult


async def log_classification(
    db: AsyncSession,
    *,
    location_id: uuid.UUID,
    ghl_contact_id: str | None,
    inbound_channel: str,
    raw_text: str,
    classification: str,
    confidence: float,
    route_to: str,
    action_taken: str,
    inbound_surface: str | None = None,
    ghl_message_id: str | None = None,
    contact_id: uuid.UUID | None = None,
    ts: datetime | None = None,
) -> GatekeeperLog:
    """
    Insert a GatekeeperLog row for an inbound message decision.

    Returns the inserted (unsaved) ORM object.  Caller must commit.
    """
    row = GatekeeperLog(
        id=uuid.uuid4(),  # pre-generate so log_id is available before flush
        location_id=location_id,
        ghl_contact_id=ghl_contact_id,
        contact_id=contact_id,
        inbound_channel=inbound_channel,
        inbound_surface=inbound_surface,
        ghl_message_id=ghl_message_id,
        raw_text=raw_text,
        classification=classification,
        confidence=Decimal(str(round(confidence, 3))),
        route_to=route_to,
        action_taken=action_taken,
        ts=ts or datetime.now(tz=timezone.utc),
    )
    db.add(row)
    return row


async def log_ai_usage(
    db: AsyncSession,
    *,
    location_id: uuid.UUID,
    ghl_contact_id: str | None,
    result: ClassificationResult,
    use_case: str = "gatekeeper",
    step: str = "classification",
    ts: datetime | None = None,
) -> AiUsage:
    """
    Insert an AiUsage row for a gatekeeper classification call.

    Returns the inserted (unsaved) ORM object.  Caller must commit.
    """
    row = AiUsage(
        location_id=location_id,
        ghl_contact_id=ghl_contact_id,
        use_case=use_case,
        step=step,
        model=result.model,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        cost_usd=Decimal(str(round(result.cost_usd, 6))),
        ts=ts or datetime.now(tz=timezone.utc),
    )
    db.add(row)
    return row


async def apply_owner_override(
    db: AsyncSession,
    log_id: uuid.UUID,
    new_category: str,
) -> GatekeeperLog:
    """
    Set ``owner_override`` and ``override_ts`` on an existing GatekeeperLog row.

    Raises:
        LookupError: if no row with ``log_id`` exists.
    """
    result = await db.execute(
        select(GatekeeperLog).where(GatekeeperLog.id == log_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise LookupError(f"gatekeeper_log row {log_id} not found")

    row.owner_override = new_category
    row.override_ts = datetime.now(tz=timezone.utc)
    return row
