"""
Gatekeeper router (M6b).

Translates a ClassificationResult + Location config into (route_to, action_taken)
and, for noise messages, the full list of GHL workflow actions.

Routing rules (in priority order):
  1. Confidence below threshold → owner escalation (low_confidence)
  2. Inquiry categories (pricing, class_info, membership, trial_reply) → UC04
  3. Booking → UC05
  4. Categories in owner_alert_categories → owner escalation
  5. Noise categories → execute_noise_policy()
  6. Unknown category (should not happen) → owner escalation

References:
  - requirements_v2/07_foundation_layer.md § "Layer 6 — Gatekeeper"
"""

from __future__ import annotations

import logging
from typing import Any

from app.gatekeeper.classifier import ClassificationResult
from app.gatekeeper.noise_policy import execute_noise_policy

logger = logging.getLogger(__name__)

# Category → route_to / action_taken constants
INQUIRY_CATEGORIES: frozenset[str] = frozenset(
    {"inquiry_pricing", "inquiry_class_info", "inquiry_membership", "trial_reply"}
)
BOOKING_CATEGORIES: frozenset[str] = frozenset({"booking"})
NOISE_CATEGORIES: frozenset[str] = frozenset(
    {"acknowledgment", "emoji_reaction", "social_compliment", "off_topic", "spam"}
)

# Default owner-alert categories (mirrors Location model default)
_DEFAULT_OWNER_ALERT: frozenset[str] = frozenset(
    {"complaint", "injury_medical", "billing_dispute", "low_confidence"}
)


def route_classification(
    result: ClassificationResult,
    *,
    confidence_threshold: float,
    owner_alert_categories: str,  # comma-separated string from Location
    noise_action_map: dict[str, Any],  # from Location.gatekeeper_noise_action
    channel: str,
    locale: str = "de-AT",
    custom_templates: dict[str, Any] | None = None,
) -> tuple[str, str, list[dict[str, Any]]]:
    """
    Route a classification result to the appropriate destination.

    Args:
        result:                   ClassificationResult from the Haiku classifier.
        confidence_threshold:     Min confidence before treating as low_confidence
                                  (from Location.gatekeeper_confidence_threshold).
        owner_alert_categories:   Comma-separated category list that triggers
                                  owner escalation (from Location setting).
        noise_action_map:         Dict of category → policy
                                  (from Location.gatekeeper_noise_action).
        channel:                  Inbound channel (for noise action GHL calls).
        locale:                   Contact locale (for auto_reply_template).
        custom_templates:         Location template map (for auto_reply_template).

    Returns:
        Tuple of (route_to, action_taken, ghl_actions).
    """
    alert_cats: frozenset[str] = _parse_alert_cats(owner_alert_categories)

    # 1. Confidence floor — treat as low_confidence for routing
    effective_category = result.category
    if result.confidence < confidence_threshold and effective_category != "low_confidence":
        logger.debug(
            "router: confidence %.2f < threshold %.2f — treating as low_confidence",
            result.confidence,
            confidence_threshold,
        )
        effective_category = "low_confidence"

    # 2. opt_out → consent gate
    # The STOP regex normally catches opt-out intent before the classifier runs,
    # but Haiku may identify near-STOP phrases ("remove me", "delete my data")
    # that the regex doesn't anchor-match.  These are legally equivalent to STOP
    # and must trigger the consent-revocation workflow — never merely page the owner.
    # The webhook handler recognises route_to="consent_gate" and calls _handle_stop().
    if effective_category == "opt_out":
        return "consent_gate", "consent_gate_opt_out", []

    # 3. Inquiry → UC04
    if effective_category in INQUIRY_CATEGORIES:
        return "uc04", f"routed_{effective_category}", []

    # 3. Booking → UC05
    if effective_category in BOOKING_CATEGORIES:
        return "uc05", "routed_booking", []

    # 4. Owner-alert categories
    if effective_category in alert_cats:
        return "owner", f"escalated_{effective_category}", []

    # 5. Noise
    if effective_category in NOISE_CATEGORIES:
        policy: str = noise_action_map.get(effective_category, "silent_ignore")
        actions = execute_noise_policy(
            policy,
            channel=channel,
            category=effective_category,
            locale=locale,
            custom_templates=custom_templates,
        )
        return "noise", policy, actions

    # 6. Fallback — unknown or unhandled category → owner
    logger.warning(
        "router: unhandled category %r — escalating to owner", effective_category
    )
    return "owner", "escalated_unknown_category", []


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_alert_cats(setting: str) -> frozenset[str]:
    """Parse a comma-separated owner_alert_categories string."""
    if not setting or not setting.strip():
        return _DEFAULT_OWNER_ALERT
    return frozenset(c.strip() for c in setting.split(",") if c.strip())
