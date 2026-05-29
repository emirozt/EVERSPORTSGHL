"""
Gatekeeper noise policy handler (M6b).

Translates a noise policy name into a list of GHL workflow action dicts.

Three policies (per spec § "Noise policies"):
  silent_ignore       — do nothing; logged, not surfaced to owner.
  react_emoji         — emoji reaction (Instagram/Facebook native) or
                        single-emoji WhatsApp reply.
  auto_reply_template — short pre-approved text reply in the contact's locale;
                        falls back to silent_ignore if no template is configured.

Auto-reactions and auto-replies bypass the consent gate because they are
acknowledgments to customer-initiated contact, not marketing communications.

References:
  - requirements_v2/07_foundation_layer.md § "Noise policies"
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Default emoji per category — used for react_emoji on WhatsApp when no
# custom emoji is configured.
DEFAULT_REACTION_EMOJI: dict[str, str] = {
    "emoji_reaction":    "🙏",
    "social_compliment": "🙏",
    "acknowledgment":    "👍",
    "off_topic":         "🙏",
    "spam":              "🙏",
}
_FALLBACK_EMOJI = "🙏"

# Default auto-reply templates keyed by locale, used when location has no
# custom template configured for the category/channel.
_DEFAULT_AUTO_REPLY: dict[str, str] = {
    "en":    "Thanks! 🙏",
    "de":    "Danke! 🙏",
    "de-AT": "Danke! 🙏",
    "de-DE": "Danke! 🙏",
    "de-CH": "Danke schön! 🙏",
}
_FALLBACK_AUTO_REPLY = "🙏"

NoisePolicy = str  # "silent_ignore" | "react_emoji" | "auto_reply_template"
VALID_NOISE_POLICIES: frozenset[str] = frozenset(
    {"silent_ignore", "react_emoji", "auto_reply_template"}
)


def execute_noise_policy(
    policy: str,
    *,
    channel: str,
    category: str,
    locale: str = "de-AT",
    custom_templates: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Convert a noise policy name into GHL workflow action dicts.

    Args:
        policy:           One of "silent_ignore", "react_emoji",
                          "auto_reply_template".
        channel:          Inbound channel (whatsapp, email, instagram_dm, …).
        category:         The noise category (used to pick reaction emoji).
        locale:           Contact locale for auto_reply_template localisation.
        custom_templates: Optional dict from ``location.whatsapp_templates``
                          (or a broader template map); checked before falling
                          back to _DEFAULT_AUTO_REPLY.

    Returns:
        List of GHL action dicts (empty list for silent_ignore).
    """
    if policy == "silent_ignore" or policy not in VALID_NOISE_POLICIES:
        if policy not in VALID_NOISE_POLICIES:
            logger.warning("noise_policy: unknown policy %r — defaulting to silent_ignore", policy)
        return []

    if policy == "react_emoji":
        return _react_emoji_actions(channel=channel, category=category)

    # auto_reply_template
    return _auto_reply_actions(
        channel=channel,
        locale=locale,
        category=category,
        custom_templates=custom_templates or {},
    )


# ── Internal helpers ──────────────────────────────────────────────────────────


def _react_emoji_actions(*, channel: str, category: str) -> list[dict[str, Any]]:
    """
    Return GHL actions for an emoji reaction.

    - Instagram / Facebook: native emoji reaction via GHL conversation API.
    - WhatsApp / email: single-emoji message reply (no native reactions).
    """
    emoji = DEFAULT_REACTION_EMOJI.get(category, _FALLBACK_EMOJI)

    if channel in ("instagram_dm", "instagram_comment", "facebook_dm", "facebook_comment"):
        return [{"action": "react_emoji", "emoji": emoji}]

    # WhatsApp / email — send a plain emoji message
    return [{"action": "send_message", "channel": channel, "body": emoji}]


def _auto_reply_actions(
    *,
    channel: str,
    locale: str,
    category: str,
    custom_templates: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Return GHL actions for an auto-reply template.

    Lookup order:
      1. custom_templates[channel][category]
      2. custom_templates[channel]["auto_reply_noise"]
      3. _DEFAULT_AUTO_REPLY[locale] / _DEFAULT_AUTO_REPLY[locale[:2]]
      4. silent_ignore fallback (empty list)
    """
    body: str | None = None

    channel_templates = custom_templates.get(channel, {})
    if isinstance(channel_templates, dict):
        body = channel_templates.get(category) or channel_templates.get("auto_reply_noise")

    if not body:
        body = (
            _DEFAULT_AUTO_REPLY.get(locale)
            or _DEFAULT_AUTO_REPLY.get(locale[:2] if len(locale) >= 2 else locale)
        )

    if not body:
        logger.debug(
            "noise_policy: no auto_reply_template found for channel=%s locale=%s "
            "category=%s — falling back to silent_ignore",
            channel,
            locale,
            category,
        )
        return []

    return [{"action": "send_message", "channel": channel, "body": body}]
