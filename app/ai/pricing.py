"""
AI model price card (M7).

Provides per-call cost computation based on token usage and model pricing.
All prices are in USD.  Cost is computed at write time and stored in
``ai_usage.cost_usd`` as ``NUMERIC(12, 6)``.

Price sources (current as of 2026-05):
  Haiku  4.5  — $0.00025 / 1K input,  $0.00125 / 1K output
  Sonnet 4.6  — $0.003   / 1K input,  $0.015   / 1K output
  Opus   4.7  — $0.015   / 1K input,  $0.075   / 1K output

Unknown models fall back to the Sonnet price (conservative overestimate).

Usage::

    from app.ai.pricing import compute_cost
    cost = compute_cost("claude-haiku-4-5", prompt_tokens=300, completion_tokens=80)
    # Decimal("0.000175")

References:
  - requirements_v2/07_foundation_layer.md § "AI Usage Logger"
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# ── Price card ────────────────────────────────────────────────────────────────
# Each entry: (input_usd_per_1k_tokens, output_usd_per_1k_tokens)

_PRICE_CARD: dict[str, tuple[Decimal, Decimal]] = {
    # Claude Haiku 4.5 — cheap/fast; used for gatekeeper classification
    "claude-haiku-4-5": (
        Decimal("0.00025"),
        Decimal("0.00125"),
    ),
    # Dated variant (Haiku 4.5 GA release ID)
    "claude-haiku-4-5-20251001": (
        Decimal("0.00025"),
        Decimal("0.00125"),
    ),
    # Claude Sonnet 4.6 — default for UC01/UC04/UC05 message generation
    "claude-sonnet-4-6": (
        Decimal("0.003"),
        Decimal("0.015"),
    ),
    # Claude Opus 4.7 — highest capability; reserved for complex generation
    "claude-opus-4-7": (
        Decimal("0.015"),
        Decimal("0.075"),
    ),
}

# Fallback for unknown/future model IDs — Sonnet pricing is a safe overestimate
_FALLBACK_PRICE = _PRICE_CARD["claude-sonnet-4-6"]

# Quantisation: 6 decimal places (matches NUMERIC(12,6) column)
_QUANT = Decimal("0.000001")


# ── Public API ────────────────────────────────────────────────────────────────


def compute_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> Decimal:
    """
    Compute the USD cost for one AI call.

    Args:
        model:             Model ID string (e.g. ``"claude-haiku-4-5"``).
        prompt_tokens:     Number of input tokens consumed.
        completion_tokens: Number of output tokens generated.

    Returns:
        Decimal rounded to 6 decimal places (NUMERIC(12,6) compatible).
        Returns ``Decimal("0.000000")`` for zero-token calls (e.g. dry-run stubs).

    Examples::

        >>> compute_cost("claude-haiku-4-5", 300, 80)
        Decimal('0.000175')

        >>> compute_cost("unknown-model", 1000, 200)  # falls back to Sonnet price
        Decimal('0.006000')
    """
    if prompt_tokens < 0 or completion_tokens < 0:
        raise ValueError(
            f"Token counts must be non-negative; got prompt={prompt_tokens}, "
            f"completion={completion_tokens}"
        )

    in_price, out_price = _PRICE_CARD.get(model, _FALLBACK_PRICE)

    cost = (
        Decimal(prompt_tokens) / 1000 * in_price
        + Decimal(completion_tokens) / 1000 * out_price
    )
    return cost.quantize(_QUANT, rounding=ROUND_HALF_UP)


def known_models() -> list[str]:
    """Return the list of model IDs in the price card (sorted)."""
    return sorted(_PRICE_CARD)


def get_price(model: str) -> tuple[Decimal, Decimal]:
    """
    Return the (input_per_1k, output_per_1k) price for a model.

    Falls back to Sonnet pricing for unknown models.
    """
    return _PRICE_CARD.get(model, _FALLBACK_PRICE)
