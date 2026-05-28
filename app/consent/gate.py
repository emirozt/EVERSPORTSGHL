"""
Consent gate — checks whether a marketing message may be sent (M6).

Every outbound use case message MUST pass through this gate before send.
Transactional messages (UC05 reschedule confirmations) are explicitly exempt
— callers pass `transactional=True` to bypass the gate while still logging.

Gate logic (per spec § "Consent Gate"):
  1. Contact has "opted-out" tag → DENY (global, all channels)
  2. Channel consent field is False or absent → DENY
  3. Otherwise → ALLOW

The gate writes a "blocked-send" row to consent_audit on every DENY.
ALLOW events are not logged here (too noisy); grant/revoke events are logged
by record.py at the time consent changes.

The gate does NOT call GHL directly — it receives the contact's current
state as a dict (custom fields + tags) from the caller.  This keeps the
gate synchronous-capable and easy to test without network stubs.

Usage:
    result = await consent_gate(
        db=db,
        ghl_contact_id="ghl-abc123",
        location_id=loc_uuid,
        channel="whatsapp",
        contact_tags=["trial-active"],
        contact_custom_fields={"consent_marketing_whatsapp": True},
    )
    if result.denied:
        return  # don't send

References:
  - requirements_v2/08_consent_model.md § "Consent Gate (shared workflow)"
  - app/consent/record.py — audit logging
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.consent.record import record_blocked_send

logger = logging.getLogger(__name__)

# ── Result type ───────────────────────────────────────────────────────────────


class ConsentDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class ConsentResult:
    decision: ConsentDecision
    reason: str  # human-readable, for logging

    @property
    def allowed(self) -> bool:
        return self.decision == ConsentDecision.ALLOW

    @property
    def denied(self) -> bool:
        return self.decision == ConsentDecision.DENY


# ── Channel → custom field name mapping ──────────────────────────────────────

_CONSENT_FIELD: dict[str, str] = {
    "email":     "consent_marketing_email",
    "whatsapp":  "consent_marketing_whatsapp",
    "voice":     "consent_marketing_voice",
}


# ── Gate ──────────────────────────────────────────────────────────────────────


async def consent_gate(
    db: AsyncSession,
    *,
    ghl_contact_id: str,
    location_id: uuid.UUID,
    channel: str,
    contact_tags: list[str],
    contact_custom_fields: dict[str, Any],
    transactional: bool = False,
    contact_id: uuid.UUID | None = None,
) -> ConsentResult:
    """
    Check whether a message on `channel` may be sent to this contact.

    Args:
        db:                    DB session (used only on DENY to write audit row).
        ghl_contact_id:        GHL contact ID string.
        location_id:           Our location UUID.
        channel:               "email" | "whatsapp" | "voice"
        contact_tags:          Current list of tags on the GHL contact.
        contact_custom_fields: Flat dict of GHL custom field name → value.
        transactional:         If True, bypass the marketing consent check but
                               still return ALLOW (transactional messages are
                               exempt from marketing consent — UC05 etc.).
        contact_id:            Optional internal UUID for the consent_audit FK.

    Returns:
        ConsentResult — always.  Never raises.
    """
    if channel not in _CONSENT_FIELD:
        logger.warning("consent_gate: unknown channel '%s' — denying by default", channel)
        return ConsentResult(ConsentDecision.DENY, f"unknown channel '{channel}'")

    # Transactional bypass — no consent check, no audit row
    if transactional:
        logger.debug(
            "consent_gate: transactional bypass for ghl_contact=%s channel=%s",
            ghl_contact_id,
            channel,
        )
        return ConsentResult(ConsentDecision.ALLOW, "transactional bypass")

    # ── 1. Global opted-out tag check ────────────────────────────────────────
    if "opted-out" in contact_tags:
        reason = "contact has opted-out tag"
        logger.info(
            "consent_gate: DENY ghl_contact=%s channel=%s reason=%s",
            ghl_contact_id,
            channel,
            reason,
        )
        await record_blocked_send(
            db,
            ghl_contact_id=ghl_contact_id,
            location_id=location_id,
            channel=channel,
            contact_id=contact_id,
        )
        return ConsentResult(ConsentDecision.DENY, reason)

    # ── 2. Channel-specific consent field ────────────────────────────────────
    field_name = _CONSENT_FIELD[channel]
    field_value = contact_custom_fields.get(field_name)

    # Treat None, False, "", "false", 0 all as no-consent
    has_consent = bool(field_value) and str(field_value).lower() not in ("false", "0", "no")

    if not has_consent:
        reason = f"no consent: {field_name} = {field_value!r}"
        logger.info(
            "consent_gate: DENY ghl_contact=%s channel=%s reason=%s",
            ghl_contact_id,
            channel,
            reason,
        )
        await record_blocked_send(
            db,
            ghl_contact_id=ghl_contact_id,
            location_id=location_id,
            channel=channel,
            contact_id=contact_id,
        )
        return ConsentResult(ConsentDecision.DENY, reason)

    logger.debug(
        "consent_gate: ALLOW ghl_contact=%s channel=%s",
        ghl_contact_id,
        channel,
    )
    return ConsentResult(ConsentDecision.ALLOW, "consent verified")
