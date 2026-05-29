"""
GHL inbound webhook handler (M6 / M6b).

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
  4. If NOT STOP:
       a. If gatekeeper enabled: runs Claude Haiku classification (M6b).
       b. Returns classification + routing + noise-policy actions.
       c. If gatekeeper disabled: returns legacy route (GHL workflow sends to UC04).

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
    "firstName": "<first_name>",       // optional
    "locale": "de-AT",                 // optional, from contact custom field
    "surface": "<surface context>",    // optional, e.g. "post_abc123"
    "messageId": "<ghl_message_id>"    // optional
  }

References:
  - requirements_v2/08_consent_model.md § "Opt-out detection"
  - requirements_v2/07_foundation_layer.md § "Layer 6 — Gatekeeper"
  - app/consent/stop_detector.py
  - app/consent/record.py
  - app/gatekeeper/gate.py
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
from app.gatekeeper.gate import process_inbound

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

    # Use the dedicated GHL webhook signing secret from settings.
    # GHL signs the POST body with HMAC-SHA256 using the secret configured in the
    # GHL sub-account's webhook settings — this is a separate credential from the
    # OAuth token.  ghl_oauth_token_ref is a secret-manager reference, not a key.
    # If GHL_WEBHOOK_SIGNING_SECRET is not configured, we reject rather than silently
    # accepting everything (fail-closed > fail-open).
    signing_secret = settings.ghl_webhook_signing_secret or ""
    if not signing_secret:
        logger.error(
            "ghl_inbound: GHL_WEBHOOK_SIGNING_SECRET not configured — "
            "rejecting request (set the secret or enable GHL_WEBHOOK_SKIP_SIG_CHECK=true "
            "for local dev)"
        )
        raise HTTPException(
            status_code=401,
            detail="Webhook signature secret not configured on server",
        )

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
    locale: str | None = None       # e.g. "de-AT"; falls back to location default
    surface: str | None = None      # e.g. "post_abc123" for Instagram comments
    messageId: str | None = None    # GHL message ID for dedup


class GatekeeperResult(BaseModel):
    """Gatekeeper classification info (present only if gatekeeper ran)."""
    classification: str
    confidence: float
    route_to: str
    action_taken: str
    log_id: uuid.UUID | None


class InboundHandlerResponse(BaseModel):
    is_stop: bool
    channel: str
    confirmation_message: str | None = None
    ghl_actions: list[dict[str, Any]]
    gatekeeper: GatekeeperResult | None = None


# ── Channel normalisation ─────────────────────────────────────────────────────


def _normalise_channel(raw: str) -> str:
    """Map GHL channel names to our canonical set."""
    mapping = {
        "whatsapp":           "whatsapp",
        "sms":                "whatsapp",  # treat SMS as whatsapp for consent purposes
        "email":              "email",
        "voice":              "voice",
        "instagram_dm":       "instagram_dm",
        "instagram_comment":  "instagram_comment",
        "facebook_dm":        "facebook_dm",
        "facebook_comment":   "facebook_comment",
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
    Receive an inbound message from GHL.

    GHL workflow flow:
    1. POST here on any inbound message.
    2. If ``is_stop: true`` — execute ``ghl_actions`` to flip consent fields,
       apply opted-out tag, remove from sequences, send ``confirmation_message``.
    3. If ``is_stop: false`` and gatekeeper ran:
       - Check ``gatekeeper.route_to``:
         - "uc04" / "uc05" → hand off to the appropriate sub-workflow
         - "owner"         → page the owner via GHL notification workflow
         - "noise"         → execute ``ghl_actions`` (react/reply/silent)
         - "legacy"        → route direct to UC04 (gatekeeper disabled)
    """
    # ── Look up location ──────────────────────────────────────────────────────
    result = await db.execute(
        select(Location).where(Location.ghl_subaccount_id == payload.locationId)
    )
    location = result.scalar_one_or_none()
    if location is None:
        raise HTTPException(
            status_code=404,
            detail=f"No location found for ghl_location_id={payload.locationId!r}",
        )

    # ── Validate signature ────────────────────────────────────────────────────
    raw_body = await request.body()
    await _verify_ghl_signature(raw_body, x_ghl_signature, location)

    channel = _normalise_channel(payload.channel)
    locale = payload.locale or location.consent_default_locale

    # ── STOP detection (runs before gatekeeper) ───────────────────────────────
    stop_detected = is_stop_keyword(
        payload.messageBody,
        custom_pattern=location.stop_keywords or None,
    )

    if stop_detected:
        return await _handle_stop(
            db=db,
            payload=payload,
            location=location,
            channel=channel,
            locale=locale,
        )

    # ── Gatekeeper (M6b) ──────────────────────────────────────────────────────
    logger.debug(
        "ghl_inbound: routing to gatekeeper ghl_contact=%s channel=%s",
        payload.contactId,
        channel,
    )

    decision = await process_inbound(
        db,
        location=location,
        ghl_contact_id=payload.contactId,
        message=payload.messageBody,
        channel=channel,
        locale=locale,
        inbound_surface=payload.surface,
        ghl_message_id=payload.messageId,
        contact_first_name=payload.firstName,
    )
    await db.commit()

    # ── Consent-gate escalation from classifier ───────────────────────────────
    # If Haiku classified the message as opt_out (a near-STOP phrase that the
    # STOP regex didn't anchor-match), the router returns route_to="consent_gate".
    # We call the same STOP handler so consent revocation is recorded and GHL
    # workflow actions are returned — legally equivalent to a STOP keyword match.
    if decision.route_to == "consent_gate":
        logger.info(
            "ghl_inbound: gatekeeper escalated opt_out to consent_gate "
            "ghl_contact=%s channel=%s",
            payload.contactId,
            channel,
        )
        return await _handle_stop(
            db=db,
            payload=payload,
            location=location,
            channel=channel,
            locale=locale,
        )

    return InboundHandlerResponse(
        is_stop=False,
        channel=channel,
        ghl_actions=decision.ghl_actions,
        gatekeeper=GatekeeperResult(
            classification=decision.classification,
            confidence=decision.confidence,
            route_to=decision.route_to,
            action_taken=decision.action_taken,
            log_id=decision.log_id,
        ),
    )


# ── STOP flow ─────────────────────────────────────────────────────────────────


async def _handle_stop(
    *,
    db: AsyncSession,
    payload: GHLInboundMessage,
    location: Location,
    channel: str,
    locale: str,
) -> InboundHandlerResponse:
    """Handle a confirmed STOP keyword message."""
    logger.info(
        "ghl_inbound: STOP keyword detected ghl_contact=%s channel=%s location=%s",
        payload.contactId,
        channel,
        location.id,
    )

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

    first_name = payload.firstName or ""
    confirmation = get_opt_out_confirmation(first_name, locale)

    ghl_actions: list[dict[str, Any]] = [
        {
            "action": "update_contact_field",
            "field": f"consent_marketing_{channel}",
            "value": False,
        },
        {
            "action": "update_contact_field",
            "field": f"consent_revoked_{channel}_at",
            "value": "__now__",
        },
        {
            "action": "apply_tag",
            "tag": "opted-out",
        },
        {
            "action": "remove_from_all_sequences",
        },
        {
            # TRANSACTIONAL BYPASS — this send is an acknowledgment to a customer-
            # initiated opt-out, NOT a marketing communication.  It explicitly
            # bypasses the consent gate per spec (08_consent_model.md § "Opt-out
            # detection") and DSGVO Art. 7(3) (withdrawal must be as easy as consent).
            # Do not add a consent gate check here.
            "action": "send_message",
            "channel": channel,
            "body": confirmation,
            "bypass_reason": "opt_out_confirmation_transactional",
        },
    ]

    return InboundHandlerResponse(
        is_stop=True,
        channel=channel,
        confirmation_message=confirmation,
        ghl_actions=ghl_actions,
        gatekeeper=None,
    )
