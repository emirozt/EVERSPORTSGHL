"""
Consent audit record helpers (M6).

All writes to `consent_audit` must go through this module.  The table is
APPEND-ONLY — these functions only INSERT, never UPDATE or DELETE.

Usage:
    from app.consent.record import record_grant, record_revocation, record_blocked_send

    await record_grant(db, ghl_contact_id="abc", location_id=..., channel="email",
                       source="double-opt-in", actor="customer", ip="1.2.3.4")

References:
  - requirements_v2/08_consent_model.md § "consent_audit table"
  - app/db/models/consent_audit.py
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.consent_audit import ConsentAudit

logger = logging.getLogger(__name__)

# ── Valid enum values (enforced at DB level too — belt-and-suspenders) ─────────

VALID_CHANNELS = {"email", "whatsapp", "voice"}
VALID_EVENTS = {"granted", "revoked", "blocked-send", "preference-centre-update"}
VALID_SOURCES = {
    "onboarding-form",
    "double-opt-in",
    "studio-import",
    "preference-centre",
    "whatsapp-opt-in",
    "stop-keyword",
    "unsubscribe-link",
    # Internal source used by the consent gate for blocked-send events.
    # Not a capture source — only valid for event="blocked-send".
    "system",
}
VALID_ACTORS = {"system", "customer", "studio-staff"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _insert(
    db: AsyncSession,
    *,
    ghl_contact_id: str,
    contact_id: uuid.UUID | None,
    location_id: uuid.UUID,
    channel: str,
    event: str,
    value: bool | None,
    source: str,
    actor: str,
    message_shown: str | None,
    ip: str | None,
) -> ConsentAudit:
    """Internal: INSERT one consent_audit row."""
    if channel not in VALID_CHANNELS:
        raise ValueError(f"Invalid channel '{channel}'; must be one of {VALID_CHANNELS}")
    if event not in VALID_EVENTS:
        raise ValueError(f"Invalid event '{event}'; must be one of {VALID_EVENTS}")
    if source not in VALID_SOURCES:
        raise ValueError(f"Invalid source '{source}'; must be one of {VALID_SOURCES}")
    if actor not in VALID_ACTORS:
        raise ValueError(f"Invalid actor '{actor}'; must be one of {VALID_ACTORS}")

    row = ConsentAudit(
        ghl_contact_id=ghl_contact_id,
        contact_id=contact_id,
        location_id=location_id,
        channel=channel,
        event=event,
        value=value,
        source=source,
        ts=_now(),
        actor=actor,
        message_shown=message_shown,
        ip=ip,
    )
    db.add(row)
    await db.flush()
    logger.info(
        "consent_audit: INSERT event=%s channel=%s ghl_contact_id=%s location_id=%s source=%s",
        event,
        channel,
        ghl_contact_id,
        location_id,
        source,
    )
    return row


async def record_grant(
    db: AsyncSession,
    *,
    ghl_contact_id: str,
    location_id: uuid.UUID,
    channel: str,
    source: str,
    actor: str = "customer",
    contact_id: uuid.UUID | None = None,
    message_shown: str | None = None,
    ip: str | None = None,
) -> ConsentAudit:
    """Record a consent grant (new opt-in)."""
    return await _insert(
        db,
        ghl_contact_id=ghl_contact_id,
        contact_id=contact_id,
        location_id=location_id,
        channel=channel,
        event="granted",
        value=True,
        source=source,
        actor=actor,
        message_shown=message_shown,
        ip=ip,
    )


async def record_revocation(
    db: AsyncSession,
    *,
    ghl_contact_id: str,
    location_id: uuid.UUID,
    channel: str,
    source: str,
    actor: str = "customer",
    contact_id: uuid.UUID | None = None,
    message_shown: str | None = None,
    ip: str | None = None,
) -> ConsentAudit:
    """Record a consent revocation (opt-out)."""
    return await _insert(
        db,
        ghl_contact_id=ghl_contact_id,
        contact_id=contact_id,
        location_id=location_id,
        channel=channel,
        event="revoked",
        value=False,
        source=source,
        actor=actor,
        message_shown=message_shown,
        ip=ip,
    )


async def record_blocked_send(
    db: AsyncSession,
    *,
    ghl_contact_id: str,
    location_id: uuid.UUID,
    channel: str,
    source: str = "system",
    contact_id: uuid.UUID | None = None,
) -> ConsentAudit:
    """Record that an outbound send was blocked by the consent gate."""
    return await _insert(
        db,
        ghl_contact_id=ghl_contact_id,
        contact_id=contact_id,
        location_id=location_id,
        channel=channel,
        event="blocked-send",
        value=None,
        source=source,
        actor="system",
        message_shown=None,
        ip=None,
    )


async def record_preference_centre_update(
    db: AsyncSession,
    *,
    ghl_contact_id: str,
    location_id: uuid.UUID,
    channel: str,
    new_value: bool,
    contact_id: uuid.UUID | None = None,
    ip: str | None = None,
) -> ConsentAudit:
    """Record a contact's preference-centre consent change."""
    return await _insert(
        db,
        ghl_contact_id=ghl_contact_id,
        contact_id=contact_id,
        location_id=location_id,
        channel=channel,
        event="preference-centre-update",
        value=new_value,
        source="preference-centre",
        actor="customer",
        message_shown=None,
        ip=ip,
    )
