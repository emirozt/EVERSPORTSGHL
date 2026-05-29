"""
Gatekeeper classifier (M6b).

Classifies an inbound customer message into one of 15 categories using
Claude Haiku (the cheap, fast model — ~€0.001/call at 200 msg/day ≈ €6/mo).

The classifier is designed to be injectable in tests: pass a pre-configured
`anthropic.AsyncAnthropic` instance as `client=` to override the default
(which reads `anthropic_api_key` from settings).

Fallback: any exception during the API call (network, quota, parse error)
falls through to `ClassificationResult(category="low_confidence", confidence=0.0)`.
This ensures the gatekeeper always produces a result — the router then
escalates low_confidence messages to the owner.

References:
  - requirements_v2/07_foundation_layer.md § "Layer 6 — Gatekeeper"
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ── Classification categories ─────────────────────────────────────────────────

CLASSIFICATION_CATEGORIES: list[str] = [
    "inquiry_pricing",      # Pricing questions, packages, trials
    "inquiry_class_info",   # Class schedules, beginner info, what to bring
    "inquiry_membership",   # Renewals, plan changes, freezes
    "booking",              # Schedule / reschedule / cancel
    "trial_reply",          # Reply to a UC01 trial follow-up message
    "complaint",            # Escalate immediately
    "injury_medical",       # Sensitive — owner only
    "billing_dispute",      # Sensitive — owner only
    "opt_out",              # STOP / unsubscribe (normally caught before classifier)
    "acknowledgment",       # "Thanks!" / "OK" / "Got it" / "👍"
    "emoji_reaction",       # Standalone emoji(s)
    "social_compliment",    # "Amazing class!" "Love this studio!"
    "off_topic",            # Unrelated to the studio
    "spam",                 # Marketing pitches, scams
    "low_confidence",       # Classifier not confident — escalate to owner
]

# Default model cost constants (Haiku — conservative overestimate for M6b;
# M7 will replace with a proper price card).
_HAIKU_COST_PER_1K_INPUT = 0.00025   # USD
_HAIKU_COST_PER_1K_OUTPUT = 0.00125  # USD

# ── System prompt ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are an inbound message classifier for a fitness / yoga / pilates studio.
Your job: assign each customer message to exactly one category and provide a
confidence score from 0.0 (no idea) to 1.0 (certain).

## Categories

| Category | When to use |
|---|---|
| inquiry_pricing | Asking about prices, packages, trials, memberships |
| inquiry_class_info | Class schedules, what to bring, difficulty, beginner suitability |
| inquiry_membership | Renewal, plan change, freeze, cancellation request |
| booking | Explicit request to book, reschedule, or cancel a class slot |
| trial_reply | Customer is replying to a follow-up message about their trial |
| complaint | Dissatisfaction, bad experience, complaint about staff/facility |
| injury_medical | Any mention of injury, medical issue, health concern |
| billing_dispute | Payment problem, unexpected charge, refund request |
| opt_out | STOP, unsubscribe, "remove me", opt-out intent |
| acknowledgment | Simple confirmation: "thanks", "ok", "got it", "👍", "alright" |
| emoji_reaction | Message is solely one or more emoji characters |
| social_compliment | Positive comment, praise, encouragement, love for the studio |
| off_topic | Unrelated to the studio (e.g. spam, wrong number, personal message) |
| spam | Marketing pitch, bot spam, scam, or irrelevant commercial message |
| low_confidence | You are not confident in any other category |

## Rules

1. Choose **one** category — the best fit.
2. Set confidence = 1.0 only when unambiguous. Use 0.7–0.9 for typical clear cases.
   Drop below 0.5 only if genuinely ambiguous; use `low_confidence` if confidence < 0.4.
3. If the message is a STOP keyword and you would classify it as `opt_out`, do so —
   but normally STOP messages are intercepted before reaching you.
4. Respond with valid JSON only. No markdown, no explanation outside the JSON.

## Response format

{"category": "<category_name>", "confidence": <0.0-1.0>, "reasoning": "<one sentence>"}
"""


# ── Result dataclass ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ClassificationResult:
    """Immutable result from the Haiku classifier."""

    category: str       # one of CLASSIFICATION_CATEGORIES
    confidence: float   # 0.0 → 1.0
    reasoning: str      # one-sentence explanation (for audit log)
    model: str          # model ID used
    prompt_tokens: int
    completion_tokens: int

    @property
    def cost_usd(self) -> float:
        """Conservative per-call cost estimate."""
        return (
            self.prompt_tokens / 1000 * _HAIKU_COST_PER_1K_INPUT
            + self.completion_tokens / 1000 * _HAIKU_COST_PER_1K_OUTPUT
        )


# ── Contact snippet builder ───────────────────────────────────────────────────


def build_contact_snippet(
    *,
    first_name: str | None = None,
    tags: list[str] | None = None,
    pipeline_stage: str | None = None,
    active_package: str | None = None,
    opted_out: bool = False,
) -> str:
    """
    Build a short contact-context string injected into the classifier prompt.

    Keeps the context small to conserve tokens.  Only surface what helps the
    classifier distinguish trial_reply / inquiry_membership / booking.
    """
    parts: list[str] = []
    if first_name:
        parts.append(f"name={first_name}")
    if active_package:
        parts.append(f"package={active_package!r}")
    if pipeline_stage:
        parts.append(f"stage={pipeline_stage!r}")
    if tags:
        parts.append(f"tags=[{', '.join(tags[:5])}]")  # cap at 5
    if opted_out:
        parts.append("opted_out=true")
    return " | ".join(parts) if parts else "unknown contact"


# ── Classifier ────────────────────────────────────────────────────────────────

_FALLBACK_RESULT = ClassificationResult(
    category="low_confidence",
    confidence=0.0,
    reasoning="Classifier error — defaulted to low_confidence for owner escalation.",
    model="unknown",
    prompt_tokens=0,
    completion_tokens=0,
)


async def classify(
    message: str,
    channel: str,
    location_name: str,
    *,
    contact_snippet: str | None = None,
    client: Any | None = None,  # anthropic.AsyncAnthropic | stub
) -> ClassificationResult:
    """
    Classify `message` into one of the 15 gatekeeper categories.

    Args:
        message:         Raw inbound text.
        channel:         Inbound channel (whatsapp, email, instagram_dm, …).
        location_name:   Studio name for context.
        contact_snippet: Optional pre-built contact context string
                         (from `build_contact_snippet()`).
        client:          Optional pre-configured ``anthropic.AsyncAnthropic``
                         instance.  If None, one is created from settings.
                         Pass a mock in tests.

    Returns:
        ClassificationResult — always returns something; falls back to
        low_confidence on any error.
    """
    if client is None:
        try:
            import anthropic  # noqa: PLC0415

            from app.config import get_settings  # noqa: PLC0415

            settings = get_settings()
            if not settings.anthropic_api_key:
                logger.warning(
                    "gatekeeper.classifier: anthropic_api_key not set — "
                    "returning low_confidence fallback"
                )
                return _FALLBACK_RESULT
            client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
            model = settings.ai_classifier_model
        except Exception as exc:  # noqa: BLE001
            logger.error("gatekeeper.classifier: failed to create Anthropic client: %s", exc)
            return _FALLBACK_RESULT
    else:
        from app.config import get_settings  # noqa: PLC0415

        model = get_settings().ai_classifier_model

    user_content = _build_user_message(
        message=message,
        channel=channel,
        location_name=location_name,
        contact_snippet=contact_snippet,
    )

    try:
        response = await client.messages.create(
            model=model,
            max_tokens=200,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        raw_json = response.content[0].text.strip()
        return _parse_response(raw_json, model=model, response=response)

    except Exception as exc:  # noqa: BLE001
        logger.error(
            "gatekeeper.classifier: API call failed — returning low_confidence: %s", exc
        )
        return _FALLBACK_RESULT


# ── Internal helpers ──────────────────────────────────────────────────────────


def _build_user_message(
    *,
    message: str,
    channel: str,
    location_name: str,
    contact_snippet: str | None,
) -> str:
    lines = [
        f"Studio: {location_name}",
        f"Channel: {channel}",
    ]
    if contact_snippet:
        lines.append(f"Contact: {contact_snippet}")
    lines.append(f'Message: """{message}"""')
    return "\n".join(lines)


def _parse_response(
    raw_json: str,
    *,
    model: str,
    response: Any,
) -> ClassificationResult:
    """Parse the JSON response from Claude.  Falls back to low_confidence on error."""
    try:
        data = json.loads(raw_json)
        category = data.get("category", "low_confidence")
        confidence = float(data.get("confidence", 0.0))
        reasoning = str(data.get("reasoning", ""))

        # Guard: only accept known categories
        if category not in CLASSIFICATION_CATEGORIES:
            logger.warning(
                "gatekeeper.classifier: unknown category %r — using low_confidence", category
            )
            category = "low_confidence"
            confidence = 0.0
            reasoning = f"Unknown category {category!r} returned by model."

        # Extract token usage (anthropic SDK provides usage object)
        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "input_tokens", 0)
        completion_tokens = getattr(usage, "output_tokens", 0)

        return ClassificationResult(
            category=category,
            confidence=max(0.0, min(1.0, confidence)),
            reasoning=reasoning,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.error("gatekeeper.classifier: failed to parse response %r: %s", raw_json, exc)
        return _FALLBACK_RESULT
