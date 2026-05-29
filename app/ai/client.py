"""
Anthropic AI client wrapper (M7).

Wraps ``anthropic.AsyncAnthropic`` with:
  - Model fallback: if the primary model fails, automatically retries with the
    fallback model (e.g. Sonnet → Haiku) before raising.
  - Structured result (text, model used, token counts) so callers can write
    to ``ai_usage`` without parsing the raw SDK response.
  - Injectable in tests: pass a pre-configured stub as ``raw_client=``.

Usage::

    client = get_ai_client()   # reads settings; cached per process
    result = await client.complete(
        system="You are ...",
        messages=[{"role": "user", "content": "Hello"}],
        max_tokens=500,
    )
    cost = compute_cost(result.model, result.prompt_tokens, result.completion_tokens)

References:
  - requirements_v2/07_foundation_layer.md § "AI: Anthropic Claude … model fallback"
  - app/ai/pricing.py — compute_cost
  - app/gatekeeper/classifier.py — uses raw AsyncAnthropic directly (not this wrapper)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ── Result dataclass ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CompletionResult:
    """Structured output from a single AI completion call."""

    text: str
    model: str          # actual model used (may differ from requested if fallback triggered)
    prompt_tokens: int
    completion_tokens: int


# ── Client ────────────────────────────────────────────────────────────────────


class AnthropicClient:
    """
    Async Anthropic client with model fallback.

    Args:
        primary_model:   Model to try first (e.g. ``"claude-sonnet-4-6"``).
        fallback_model:  Model to try if primary fails.  Pass ``None`` to
                         disable fallback (raises immediately on primary failure).
        raw_client:      Pre-configured ``anthropic.AsyncAnthropic`` instance.
                         If ``None``, one is created from settings on first call.
                         Inject a stub in tests.
    """

    def __init__(
        self,
        *,
        primary_model: str,
        fallback_model: str | None = None,
        raw_client: Any | None = None,  # anthropic.AsyncAnthropic | stub
    ) -> None:
        self.primary_model = primary_model
        self.fallback_model = fallback_model
        self._raw_client = raw_client  # may be None until first call

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _resolve_client(self) -> Any:
        """Return the underlying AsyncAnthropic client, creating it if needed."""
        if self._raw_client is not None:
            return self._raw_client

        import anthropic  # noqa: PLC0415

        from app.config import get_settings  # noqa: PLC0415

        settings = get_settings()
        if not settings.anthropic_api_key:
            raise RuntimeError(
                "AnthropicClient: ANTHROPIC_API_KEY is not set. "
                "Configure it in .env or pass raw_client= for testing."
            )
        self._raw_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        return self._raw_client

    async def _call_model(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, str]],
        max_tokens: int,
    ) -> CompletionResult:
        client = self._resolve_client()
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        text = response.content[0].text
        usage = response.usage
        return CompletionResult(
            text=text,
            model=model,
            prompt_tokens=getattr(usage, "input_tokens", 0),
            completion_tokens=getattr(usage, "output_tokens", 0),
        )

    # ── Public API ────────────────────────────────────────────────────────────

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        model: str | None = None,
    ) -> CompletionResult:
        """
        Run a single completion, falling back to ``fallback_model`` on failure.

        Args:
            system:     System prompt string.
            messages:   List of ``{"role": ..., "content": ...}`` dicts.
            max_tokens: Maximum tokens to generate.
            model:      Override the primary model for this call only.
                        If ``None``, uses ``self.primary_model``.

        Returns:
            CompletionResult with text, the model actually used, and token counts.

        Raises:
            Exception: re-raises the last error if all models fail.
        """
        primary = model or self.primary_model
        models_to_try: list[str] = [primary]
        if self.fallback_model and self.fallback_model != primary:
            models_to_try.append(self.fallback_model)

        last_exc: Exception | None = None
        for m in models_to_try:
            try:
                result = await self._call_model(
                    model=m,
                    system=system,
                    messages=messages,
                    max_tokens=max_tokens,
                )
                if m != primary:
                    logger.warning(
                        "ai.client: primary model %r failed; used fallback %r", primary, m
                    )
                return result
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ai.client: model %r failed (will try next if available): %s", m, exc
                )
                last_exc = exc

        # All models exhausted
        logger.error(
            "ai.client: all models failed (%s) — raising last exception",
            ", ".join(models_to_try),
        )
        raise last_exc  # type: ignore[misc]


# ── Module-level factory ──────────────────────────────────────────────────────


def get_ai_client(
    *,
    primary_model: str | None = None,
    fallback_model: str | None = None,
    raw_client: Any | None = None,
) -> AnthropicClient:
    """
    Build an ``AnthropicClient`` using settings defaults.

    Args:
        primary_model:  Defaults to ``settings.ai_default_model``.
        fallback_model: Defaults to ``settings.ai_classifier_model`` (Haiku).
                        Pass ``None`` explicitly to disable fallback.
        raw_client:     Inject a test stub; skips settings lookup for api_key.

    Returns:
        A configured ``AnthropicClient`` instance.  **Not** cached — callers
        that need a singleton should manage that themselves.
    """
    from app.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    return AnthropicClient(
        primary_model=primary_model or settings.ai_default_model,
        fallback_model=fallback_model if fallback_model is not None else settings.ai_classifier_model,
        raw_client=raw_client,
    )
