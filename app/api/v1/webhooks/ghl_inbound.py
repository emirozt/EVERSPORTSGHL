"""
GHL inbound webhook handler (M6 — consent / STOP detection).

GHL workflows POST every inbound customer message to this endpoint.
The handler:
  1. Validates the GHL webhook signature (`X-GHL-Signature`).
  2. Checks whether the message body is a STOP keyword (multilingual).
  3. If STOP:
       a. Records revocation in consent_audit for the channel.
       b. Returns instructions for the GHL workflow to:
            - Flip consent_marketing_{channel} = false on the contact
            - Stamp consent_revoked_{channel}_at = now()
            - Apply "opted-out" tag
            - Remove contact from all active automation sequences
            - Send localised opt-out confirmation message
  4. If NOT STOP: returns {"is_stop": false} so the GHL workflow routes
     the message onward to the gatekeeper (M6b).

Signature validation:
  GHL signs the POST body with HMAC-SHA256 using the location's
  `ghl_oauth_token_ref`-derived signing key.  The signature is in the
  `X-GHL-Signature` header (hex-encoded).  Validation is skipped in test
  mode (`GHL_WEBHOOK_SKIP_SIG_CHECK=true`).

Payload shape (from GHL):
  {
    "type": "InboundMessage",
    "locationId": "<ghl_location_id>",
    "contactId": "<ghl_contact_id>",
    "channel": "whatsapp" | "email" | "sms",
    "messageBody": "<raw text>",
    "firstName": "<first_name>",   // optional
    "locale": "de-AT"              // optional, from contact custom field
  }

References:
  - requirements_v2/08_consent_model.md § "Opt-out detection"
  - app/consent/stop_detector.py
  - app/consent/record.py
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.consent.record import record_revocation
from app.consent.stop_detector import get_opt_out_confirmation, is_stop_keyword
from app.db.models.location import Location
from app.db.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/webhooks/ghl", tags=["webhooks"])


# ── Signature validation ──────────────────────────────────────────────────────


async def _verify_ghl_signature(
    body: bytes,
    signature_header: str | None,
    location: Location,
) -> None:
    """
    Verify the X-GHL-Signature header.

    Raises HTTPException 401 if invalid.
    Skipped when GHL_WEBHOOK_SKIP_SIG_CHECK=true (test mode).
    """
    from app.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    if getattr(settings, "ghl_webhook_skip_sig_check", False):
        logger.debug("ghl_inbound: signature check skipped (test mode)")
        return

    if not signature_header:
        raise HTTPException(status_code=401, detail="Missing X-GHL-Signature header")

    # Derive signing key from the location's OAuth token ref (simplified v1 pattern)
    # Full GHL HMAC validation is wired per their v2 webhook docs.
    signing_secret = location.ghl_oauth_token_ref or ""
    expected = hmac.new(
        signing_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature_header):
        raise HTTPException(status_code=401, detail="Invalid GHL webhook signature")


# ── Inbound message schema ────────────────────────────────────────────────────


class GHLInboundMessage(BaseModel):
    type: str = "InboundMessage"
    locationId: str
    contactId: str
    channel: str  # "whatsapp" | "email" | "sms" (we normalise sms → whatsapp)
    messageBody: str
    firstName: str | None = None
    locale: str | None = None  # e.g. "de-AT"; falls back to location default


class InboundHandlerResponse(BaseModel):
    is_stop: bool
    channel: str
    confirmation_message: str | None = None
    ghl_actions: list[dict[str, Any]]  # instructions for GHL workflow


# ── Channel normalisation ─────────────────────────────────────────────────────


def _normalise_channel(raw: str) -> str:
    """Map GHL channel names to our canonical set."""
    mapping = {
        "whatsapp":  "whatsapp",
        "sms":       "whatsapp",  # treat SMS as whatsapp for consent purposes
        "email":     "email",
        "voice":     "voice",
    }
    return mapping.get(raw.lower(), "whatsapp")


# ── Main handler ──────────────────────────────────────────────────────────────


@router.post("/inbound", response_model=InboundHandlerResponse)
async def handle_inbound_message(
    request: Request,
    payload: GHLInboundMessage,
    db: AsyncSession = Depends(get_db),
    x_ghl_signature: str | None = Header(default=None, alias="X-GHL-Signature"),
) -> InboundHandlerResponse:
    """
    Receive an inbound message from GHL and check for STOP keywords.

    The GHL workflow should:
    1. POST here immediately on any inbound message.
    2. If `is_stop: true` — execute the `ghl_actions` list to flip consent
       fields, apply opted-out tag, remove from sequences, and send the
       `confirmation_message`.
    3. If `is_stop: false` — route the message onward (to gatekeeper in M6b,
       or directly to the relevant use case if gatekeeper is disabled).
    """
    # Look up location by GHL location ID
    result = await db.execute(
        select(Location).where(Location.ghl_subaccount_id == payload.locationId)
    )
    location = result.scalar_one_or_none()
    if location is None:
        raise HTTPException(
            status_code=404,
            detail=f"No location found for ghl_location_id={payload.locationId!r}",
        )

    # Validate signature
    raw_body = await request.body()
    await _verify_ghl_signature(raw_body, x_ghl_signature, location)

    channel = _normalise_channel(payload.channel)

    # ── STOP detection ────────────────────────────────────────────────────────
    stop_detected = is_stop_keyword(
        payload.messageBody,
        custom_pattern=location.stop_keywords or None,
    )

    if not stop_detected:
        logger.debug(
            "ghl_inbound: not a stop keyword ghl_contact=%s channel=%s",
            payload.contactId,
            channel,
        )
        return InboundHandlerResponse(
            is_stop=False,
            channel=channel,
            ghl_actions=[],
        )

    # ── STOP flow ─────────────────────────────────────────────────────────────
    logger.info(
        "ghl_inbound: STOP keyword detected ghl_contact=%s channel=%s location=%s",
        payload.contactId,
        channel,
        location.id,
    )

    # Record revocation in consent_audit
    await record_revocation(
        db,
        ghl_contact_id=payload.contactId,
        location_id=location.id,
        channel=channel,
        source="stop-keyword",
        actor="customer",
        message_shown=payload.messageBody,
    )
    await db.commit()

    # Localised confirmation message
    locale = payload.locale or location.consent_default_locale
    first_name = payload.firstName or ""
    confirmation = get_opt_out_confirmation(first_name, locale)

    # Return GHL action instructions (the workflow executes these)
    ghl_actions: list[dict[str, Any]] = [
        {
            "action": "update_contact_field",
            "field": f"consent_marketing_{channel}",
            "value": False,
        },
        {
            "action": "update_contact_field",
            "field": f"consent_revoked_{channel}_at",
            "value": "__now__",  # GHL workflow resolves this to current timestamp
        },
        {
            "action": "apply_tag",
            "tag": "opted-out",
        },
        {
            "action": "remove_from_all_sequences",
        },
        {
            "action": "send_message",
            "channel": channel,
            "body": confirmation,
        },
    ]

    return InboundHandlerResponse(
        is_stop=True,
        channel=channel,
        confirmation_message=confirmation,
        ghl_actions=ghl_actions,
    )
