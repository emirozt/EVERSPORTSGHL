"""
Gatekeeper main entry point (M6b).

`process_inbound()` is the single function called by the webhook handler for
every non-STOP inbound message.  It orchestrates:

  1. Gatekeeper-enabled check (returns legacy decision if disabled).
  2. Claude Haiku classification.
  3. Route decision (via router.route_classification).
  4. Log to gatekeeper_log + ai_usage (both unflushed; caller commits).

The caller (webhook handler) receives a `GatekeeperDecision` with everything
needed to construct the HTTP response to the GHL workflow.

References:
  - requirements_v2/07_foundation_layer.md § "Layer 6 — Gatekeeper (algorithm)"
  - app/gatekeeper/classifier.py
  - app/gatekeeper/router.py
  - app/gatekeeper/audit.py
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.location import Location
from app.gatekeeper.audit import log_ai_usage, log_classification
from app.gatekeeper.classifier import ClassificationResult, build_contact_snippet, classify
from app.gatekeeper.router import route_classification

logger = logging.getLogger(__name__)


# ── Decision dataclass ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GatekeeperDecision:
    """Result of processing one inbound message through the gatekeeper."""

    classification: str               # category (or "legacy" when disabled)
    confidence: float                 # 0.0 – 1.0
    route_to: str                     # uc04 | uc05 | owner | noise | consent_gate | legacy
    action_taken: str                 # what was done
    ghl_actions: list[dict[str, Any]] = field(default_factory=list)
    log_id: uuid.UUID | None = None   # gatekeeper_log.id (None for legacy path)


# ── Main entry point ──────────────────────────────────────────────────────────


async def process_inbound(
    db: AsyncSession,
    *,
    location: Location,
    ghl_contact_id: str | None,
    message: str,
    channel: str,
    locale: str = "de-AT",
    inbound_surface: str | None = None,
    ghl_message_id: str | None = None,
    contact_id: uuid.UUID | None = None,
    # Contact context for the classifier (optional)
    contact_first_name: str | None = None,
    contact_tags: list[str] | None = None,
    contact_pipeline_stage: str | None = None,
    contact_active_package: str | None = None,
    # Dependency injection (tests pass a mock client here)
    classifier_client: Any | None = None,
) -> GatekeeperDecision:
    """
    Process a non-STOP inbound message through the gatekeeper.

    The function does NOT commit the session — the caller is responsible
    so the gatekeeper writes remain atomic with surrounding business logic.

    When ``location.gatekeeper_enabled`` is False, classification is skipped
    and a legacy_uc04 decision is returned without writing to gatekeeper_log.

    Returns:
        GatekeeperDecision — always returns something.
    """
    # ── 1. Gatekeeper disabled → legacy path ──────────────────────────────────
    if not location.gatekeeper_enabled:
        logger.debug(
            "gatekeeper: disabled for location=%s — returning legacy_uc04", location.id
        )
        return GatekeeperDecision(
            classification="legacy",
            confidence=1.0,
            route_to="legacy",
            action_taken="legacy_uc04",
            ghl_actions=[],
            log_id=None,
        )

    # ── 2. Classify ───────────────────────────────────────────────────────────
    snippet = build_contact_snippet(
        first_name=contact_first_name,
        tags=contact_tags,
        pipeline_stage=contact_pipeline_stage,
        active_package=contact_active_package,
        opted_out=("opted-out" in (contact_tags or [])),
    )

    result: ClassificationResult = await classify(
        message,
        channel=channel,
        location_name=location.location_name,
        contact_snippet=snippet,
        client=classifier_client,
    )

    logger.debug(
        "gatekeeper: classified channel=%s category=%s confidence=%.2f",
        channel,
        result.category,
        result.confidence,
    )

    # ── 3. Route ──────────────────────────────────────────────────────────────
    route_to, action_taken, ghl_actions = route_classification(
        result,
        confidence_threshold=float(location.gatekeeper_confidence_threshold),
        owner_alert_categories=location.gatekeeper_owner_alert_categories,
        noise_action_map=location.gatekeeper_noise_action or {},
        channel=channel,
        locale=locale,
        custom_templates=location.whatsapp_templates or {},
    )

    # ── 4. Audit log ──────────────────────────────────────────────────────────
    log_row = await log_classification(
        db,
        location_id=location.id,
        ghl_contact_id=ghl_contact_id,
        contact_id=contact_id,
        inbound_channel=channel,
        inbound_surface=inbound_surface,
        ghl_message_id=ghl_message_id,
        raw_text=message,
        classification=result.category,
        confidence=result.confidence,
        route_to=route_to,
        action_taken=action_taken,
    )

    await log_ai_usage(
        db,
        location_id=location.id,
        ghl_contact_id=ghl_contact_id,
        result=result,
    )

    return GatekeeperDecision(
        classification=result.category,
        confidence=result.confidence,
        route_to=route_to,
        action_taken=action_taken,
        ghl_actions=ghl_actions,
        log_id=log_row.id,
    )
