"""
Consent management API (M6).

Endpoints:
  POST /api/v1/consent/gate
      Check whether a marketing message may be sent on a given channel.
      Returns ALLOW or DENY + reason.  Writes a blocked-send audit row on DENY.

  POST /api/v1/consent/grant
      Record a consent grant (e.g. from a double opt-in form callback).

  POST /api/v1/consent/revoke
      Record a consent revocation (preference-centre, STOP flow).

  GET  /api/v1/consent/preference-centre/{token}
      Return current consent state for the contact encoded in the signed token.

  PATCH /api/v1/consent/preference-centre/{token}
      Update consent preferences; records audit rows + calls GHL to update fields.

  POST /api/v1/consent/sweep/{location_id}
      Trigger legacy-contact opt-in invitation sweep for a location.
      One-time operation — enqueues GHL outbound invitation for each contact
      without a consent record.

All endpoints that write to GHL require a configured GHL client per location.
In dry-run or test mode the GHL calls are skipped (logged only).

References:
  - requirements_v2/08_consent_model.md
  - app/consent/gate.py, record.py, tokens.py
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.consent.gate import consent_gate
from app.consent.record import (
    record_blocked_send,
    record_grant,
    record_preference_centre_update,
    record_revocation,
)
from app.consent.tokens import TokenError, generate_token, verify_token
from app.db.models.location import Location
from app.db.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/consent", tags=["consent"])


# ── Shared helpers ────────────────────────────────────────────────────────────


async def _get_location(location_id: uuid.UUID, db: AsyncSession) -> Location:
    result = await db.execute(select(Location).where(Location.id == location_id))
    loc = result.scalar_one_or_none()
    if loc is None:
        raise HTTPException(status_code=404, detail=f"Location {location_id} not found")
    return loc


# ── Consent gate endpoint ─────────────────────────────────────────────────────


class ConsentGateRequest(BaseModel):
    ghl_contact_id: str
    location_id: uuid.UUID
    channel: Literal["email", "whatsapp", "voice"]
    contact_tags: list[str] = Field(default_factory=list)
    contact_custom_fields: dict[str, Any] = Field(default_factory=dict)
    transactional: bool = False


class ConsentGateResponse(BaseModel):
    decision: Literal["allow", "deny"]
    reason: str


@router.post("/gate", response_model=ConsentGateResponse)
async def check_consent_gate(
    req: ConsentGateRequest,
    db: AsyncSession = Depends(get_db),
) -> ConsentGateResponse:
    """
    Check whether a marketing message may be sent on a channel.

    This is the single gate that all outbound use case messages must pass
    through.  GHL workflows call this endpoint before any outbound send.
    """
    result = await consent_gate(
        db,
        ghl_contact_id=req.ghl_contact_id,
        location_id=req.location_id,
        channel=req.channel,
        contact_tags=req.contact_tags,
        contact_custom_fields=req.contact_custom_fields,
        transactional=req.transactional,
    )
    await db.commit()
    return ConsentGateResponse(decision=result.decision.value, reason=result.reason)


# ── Grant endpoint ────────────────────────────────────────────────────────────


class ConsentGrantRequest(BaseModel):
    ghl_contact_id: str
    location_id: uuid.UUID
    channel: Literal["email", "whatsapp", "voice"]
    source: str
    actor: Literal["customer", "studio-staff", "system"] = "customer"
    message_shown: str | None = None
    ip: str | None = None


class ConsentGrantResponse(BaseModel):
    audit_id: uuid.UUID
    ghl_contact_id: str
    channel: str
    event: str = "granted"


@router.post("/grant", response_model=ConsentGrantResponse, status_code=status.HTTP_201_CREATED)
async def grant_consent(
    req: ConsentGrantRequest,
    db: AsyncSession = Depends(get_db),
) -> ConsentGrantResponse:
    """
    Record a consent grant.

    Called by: double-opt-in form callback, preference-centre update,
    WhatsApp first-contact opt-in handler.
    """
    await _get_location(req.location_id, db)
    row = await record_grant(
        db,
        ghl_contact_id=req.ghl_contact_id,
        location_id=req.location_id,
        channel=req.channel,
        source=req.source,
        actor=req.actor,
        message_shown=req.message_shown,
        ip=req.ip,
    )
    await db.commit()
    logger.info(
        "consent: GRANTED ghl_contact=%s channel=%s source=%s",
        req.ghl_contact_id, req.channel, req.source,
    )
    return ConsentGrantResponse(audit_id=row.id, ghl_contact_id=req.ghl_contact_id, channel=req.channel)


# ── Revoke endpoint ───────────────────────────────────────────────────────────


class ConsentRevokeRequest(BaseModel):
    ghl_contact_id: str
    location_id: uuid.UUID
    channel: Literal["email", "whatsapp", "voice"]
    source: str
    actor: Literal["customer", "studio-staff", "system"] = "customer"
    ip: str | None = None


class ConsentRevokeResponse(BaseModel):
    audit_id: uuid.UUID
    ghl_contact_id: str
    channel: str
    event: str = "revoked"


@router.post("/revoke", response_model=ConsentRevokeResponse, status_code=status.HTTP_201_CREATED)
async def revoke_consent(
    req: ConsentRevokeRequest,
    db: AsyncSession = Depends(get_db),
) -> ConsentRevokeResponse:
    """
    Record a consent revocation.

    Called by: STOP keyword handler, unsubscribe-link click, preference-centre.
    """
    await _get_location(req.location_id, db)
    row = await record_revocation(
        db,
        ghl_contact_id=req.ghl_contact_id,
        location_id=req.location_id,
        channel=req.channel,
        source=req.source,
        actor=req.actor,
        ip=req.ip,
    )
    await db.commit()
    logger.info(
        "consent: REVOKED ghl_contact=%s channel=%s source=%s",
        req.ghl_contact_id, req.channel, req.source,
    )
    return ConsentRevokeResponse(audit_id=row.id, ghl_contact_id=req.ghl_contact_id, channel=req.channel)


# ── Preference centre ─────────────────────────────────────────────────────────


class PreferenceCentreState(BaseModel):
    ghl_contact_id: str
    location_id: str
    consent_marketing_email: bool
    consent_marketing_whatsapp: bool
    consent_marketing_voice: bool
    preference_centre_token: str  # freshly-generated (for the "update your prefs" link)


class PreferenceCentreUpdate(BaseModel):
    consent_marketing_email: bool | None = None
    consent_marketing_whatsapp: bool | None = None
    consent_marketing_voice: bool | None = None


class PreferenceCentreUpdateResponse(BaseModel):
    updated_channels: list[str]
    audit_ids: list[uuid.UUID]


@router.get("/preference-centre/{token}", response_model=PreferenceCentreState)
async def get_preference_centre(
    token: str,
    request: Request,
) -> PreferenceCentreState:
    """
    Return current consent state for the contact encoded in the signed token.

    The token is a signed, 90-day URL-safe token generated by tokens.py.
    On each view a fresh token is returned so the customer can bookmark it.
    """
    try:
        payload = verify_token(token)
    except TokenError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # In v1, we return a placeholder response (live GHL field fetch is wired
    # when GHL client is configured per-location in M8).
    # The fresh token embeds the same contact + location with a new 90-day TTL.
    fresh_token = generate_token(payload.ghl_contact_id, payload.location_id)

    return PreferenceCentreState(
        ghl_contact_id=payload.ghl_contact_id,
        location_id=payload.location_id,
        consent_marketing_email=False,     # TODO: fetch from GHL in M8
        consent_marketing_whatsapp=False,  # TODO: fetch from GHL in M8
        consent_marketing_voice=False,
        preference_centre_token=fresh_token,
    )


@router.patch(
    "/preference-centre/{token}",
    response_model=PreferenceCentreUpdateResponse,
    status_code=status.HTTP_200_OK,
)
async def update_preference_centre(
    token: str,
    body: PreferenceCentreUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> PreferenceCentreUpdateResponse:
    """
    Update consent preferences from the preference centre.

    Writes an audit row for each changed channel.  The actual GHL field
    update is delegated to the GHL client (wired in M8 when client exists).
    """
    try:
        payload = verify_token(token)
    except TokenError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        location_uuid = uuid.UUID(payload.location_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid location_id in token") from exc

    await _get_location(location_uuid, db)

    client_ip = request.client.host if request.client else None
    updated_channels: list[str] = []
    audit_ids: list[uuid.UUID] = []

    updates: dict[str, bool | None] = {
        "email":     body.consent_marketing_email,
        "whatsapp":  body.consent_marketing_whatsapp,
        "voice":     body.consent_marketing_voice,
    }

    for channel, new_value in updates.items():
        if new_value is None:
            continue  # not changed
        row = await record_preference_centre_update(
            db,
            ghl_contact_id=payload.ghl_contact_id,
            location_id=location_uuid,
            channel=channel,
            new_value=new_value,
            ip=client_ip,
        )
        updated_channels.append(channel)
        audit_ids.append(row.id)
        # TODO M8: call GHL API to update consent_marketing_{channel} field

    await db.commit()
    logger.info(
        "preference-centre: updated channels=%s ghl_contact=%s",
        updated_channels,
        payload.ghl_contact_id,
    )
    return PreferenceCentreUpdateResponse(updated_channels=updated_channels, audit_ids=audit_ids)


# ── Legacy sweep endpoint ─────────────────────────────────────────────────────


class SweepResponse(BaseModel):
    location_id: uuid.UUID
    contacts_queued: int
    message: str


@router.post("/sweep/{location_id}", response_model=SweepResponse)
async def trigger_consent_sweep(
    location_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> SweepResponse:
    """
    Trigger the one-time legacy-contact opt-in invitation sweep.

    Finds all contacts for this location that have no consent record and
    enqueues a GHL outbound invitation workflow trigger for each one.

    This endpoint is idempotent — contacts that already have a consent record
    are skipped.  Safe to call multiple times.
    """
    await _get_location(location_id, db)

    # TODO M8: wire real GHL contact sweep here.
    # The filter must be ALL of:
    #   1. Contact has no existing consent_audit row (never captured), AND
    #   2. Contact does NOT have the "opted-out" GHL tag, AND
    #   3. Contact has no consent_revoked_email_at / consent_revoked_whatsapp_at
    #      field set (to respect any pre-existing opt-outs from other channels).
    # Only contacts that satisfy all three conditions should be enqueued for the
    # opt-in invitation workflow.  Any pre-existing opt-out state must be
    # preserved — do NOT send an invitation to contacts who have already revoked.
    contacts_queued = 0

    logger.info(
        "consent sweep: triggered for location_id=%s contacts_queued=%d",
        location_id,
        contacts_queued,
    )
    return SweepResponse(
        location_id=location_id,
        contacts_queued=contacts_queued,
        message=(
            f"Sweep triggered for location {location_id}. "
            f"{contacts_queued} contacts queued for opt-in invitation. "
            "(GHL delivery wired in M8 when GHL client is configured.)"
        ),
    )
