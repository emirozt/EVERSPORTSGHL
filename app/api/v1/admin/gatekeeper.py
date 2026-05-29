"""
Gatekeeper owner-override admin API (M6b).

Endpoints:
  PATCH /api/v1/admin/gatekeeper/log/{log_id}/override
      Reclassify a single gatekeeper_log entry. Sets owner_override +
      override_ts. The reclassified message is NOT re-routed — the override
      is purely for training data and audit purposes. Re-routing must be
      done manually by the owner in the GHL Conversations inbox.

  GET /api/v1/admin/gatekeeper/log
      List recent gatekeeper_log entries for a location (for the "Filtered out"
      inbox folder). Supports filtering by category and channel.

These endpoints are admin-only (same API-key guard used by other admin routes).

References:
  - requirements_v2/07_foundation_layer.md § "Owner overrides"
  - app/gatekeeper/audit.py  — apply_owner_override()
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.gatekeeper_log import GatekeeperLog
from app.db.session import get_db
from app.gatekeeper.audit import apply_owner_override
from app.gatekeeper.classifier import CLASSIFICATION_CATEGORIES

router = APIRouter(prefix="/api/v1/admin/gatekeeper", tags=["admin", "gatekeeper"])

# ── Request / response schemas ────────────────────────────────────────────────


class OverrideRequest(BaseModel):
    new_category: str  # Must be one of CLASSIFICATION_CATEGORIES


class GatekeeperLogEntry(BaseModel):
    id: uuid.UUID
    location_id: uuid.UUID
    ghl_contact_id: str | None
    inbound_channel: str
    inbound_surface: str | None
    ghl_message_id: str | None
    raw_text: str
    classification: str
    confidence: float
    route_to: str
    action_taken: str
    owner_override: str | None
    override_ts: str | None  # ISO datetime string
    ts: str                  # ISO datetime string

    model_config = {"from_attributes": True}


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.patch(
    "/log/{log_id}/override",
    response_model=GatekeeperLogEntry,
    summary="Reclassify a gatekeeper log entry (owner override)",
)
async def override_classification(
    log_id: uuid.UUID,
    body: OverrideRequest,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """
    Apply an owner override to a gatekeeper_log row.

    Sets ``owner_override`` to ``new_category`` and stamps ``override_ts``.
    The new category must be one of the 15 classification categories.
    """
    if body.new_category not in CLASSIFICATION_CATEGORIES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Invalid category {body.new_category!r}. "
                f"Must be one of: {', '.join(sorted(CLASSIFICATION_CATEGORIES))}"
            ),
        )

    try:
        row = await apply_owner_override(db, log_id, body.new_category)
    except LookupError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"GatekeeperLog {log_id} not found",
        )

    await db.commit()
    await db.refresh(row)
    return _to_response(row)


@router.get(
    "/log",
    response_model=list[GatekeeperLogEntry],
    summary="List recent gatekeeper log entries for a location",
)
async def list_log(
    location_id: uuid.UUID = Query(..., description="Location UUID"),
    classification: str | None = Query(None, description="Filter by category"),
    channel: str | None = Query(None, description="Filter by inbound_channel"),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """
    Fetch recent gatekeeper_log entries for a location, newest first.

    Used by the owner to review filtered messages ("Filtered out" folder).
    """
    stmt = (
        select(GatekeeperLog)
        .where(GatekeeperLog.location_id == location_id)
        .order_by(desc(GatekeeperLog.ts))
        .limit(limit)
    )
    if classification:
        stmt = stmt.where(GatekeeperLog.classification == classification)
    if channel:
        stmt = stmt.where(GatekeeperLog.inbound_channel == channel)

    result = await db.execute(stmt)
    rows = result.scalars().all()
    return [_to_response(r) for r in rows]


# ── Helpers ───────────────────────────────────────────────────────────────────


def _to_response(row: GatekeeperLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "location_id": row.location_id,
        "ghl_contact_id": row.ghl_contact_id,
        "inbound_channel": row.inbound_channel,
        "inbound_surface": row.inbound_surface,
        "ghl_message_id": row.ghl_message_id,
        "raw_text": row.raw_text,
        "classification": row.classification,
        "confidence": float(row.confidence),
        "route_to": row.route_to,
        "action_taken": row.action_taken,
        "owner_override": row.owner_override,
        "override_ts": row.override_ts.isoformat() if row.override_ts else None,
        "ts": row.ts.isoformat(),
    }
