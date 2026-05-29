"""
M7 — AI pricing, client wrapper, and budget enforcement tests.

Test classes:
  TestPricing      (12) — compute_cost, known_models, get_price, edge cases
  TestAiClient     (9)  — success, fallback, both-fail, injectable stub
  TestBudgetStatus (7)  — dataclass properties and summary
  TestIsEssential  (7)  — essentiality rules for all use-case / step combos
  TestCheckBudget  (5)  — DB query via in-memory mock
  TestAssertBudget (6)  — soft cap, hard cap essential, hard cap non-essential
  TestSoftCapEmail (6)  — maybe_send_soft_cap_warning paths + dedup

Total: 52 tests
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.budget import (
    BudgetExceededError,
    BudgetStatus,
    assert_budget_available,
    check_budget,
    get_monthly_spend,
    is_essential_call,
    maybe_send_soft_cap_warning,
)
from app.ai.client import AnthropicClient, CompletionResult, get_ai_client
from app.ai.pricing import compute_cost, get_price, known_models


# ═══════════════════════════════════════════════════════════════════════════════
# TestPricing
# ═══════════════════════════════════════════════════════════════════════════════


class TestPricing:
    def test_haiku_known_price(self):
        cost = compute_cost("claude-haiku-4-5", prompt_tokens=1000, completion_tokens=1000)
        # input: 0.00025 + output: 0.00125 = 0.00150
        assert cost == Decimal("0.001500")

    def test_sonnet_known_price(self):
        cost = compute_cost("claude-sonnet-4-6", prompt_tokens=1000, completion_tokens=1000)
        # input: 0.003 + output: 0.015 = 0.018
        assert cost == Decimal("0.018000")

    def test_opus_known_price(self):
        cost = compute_cost("claude-opus-4-7", prompt_tokens=1000, completion_tokens=1000)
        # input: 0.015 + output: 0.075 = 0.090
        assert cost == Decimal("0.090000")

    def test_unknown_model_falls_back_to_sonnet(self):
        sonnet_cost = compute_cost("claude-sonnet-4-6", 500, 100)
        unknown_cost = compute_cost("gpt-99-turbo", 500, 100)
        assert unknown_cost == sonnet_cost

    def test_zero_tokens_returns_zero(self):
        cost = compute_cost("claude-haiku-4-5", 0, 0)
        assert cost == Decimal("0.000000")

    def test_result_is_decimal_not_float(self):
        cost = compute_cost("claude-haiku-4-5", 300, 80)
        assert isinstance(cost, Decimal)

    def test_quantised_to_six_decimals(self):
        cost = compute_cost("claude-haiku-4-5", 300, 80)
        # Verify no more than 6 decimal places
        assert cost == cost.quantize(Decimal("0.000001"))

    def test_negative_tokens_raises(self):
        with pytest.raises(ValueError, match="non-negative"):
            compute_cost("claude-haiku-4-5", -1, 0)

    def test_known_models_returns_sorted_list(self):
        models = known_models()
        assert isinstance(models, list)
        assert models == sorted(models)
        assert "claude-haiku-4-5" in models
        assert "claude-sonnet-4-6" in models
        assert "claude-opus-4-7" in models

    def test_get_price_known_model(self):
        in_p, out_p = get_price("claude-haiku-4-5")
        assert in_p == Decimal("0.00025")
        assert out_p == Decimal("0.00125")

    def test_get_price_unknown_model_returns_sonnet(self):
        unknown = get_price("totally-made-up-model")
        sonnet = get_price("claude-sonnet-4-6")
        assert unknown == sonnet

    def test_dated_haiku_variant_has_same_price_as_base(self):
        base = compute_cost("claude-haiku-4-5", 1000, 1000)
        dated = compute_cost("claude-haiku-4-5-20251001", 1000, 1000)
        assert base == dated


# ═══════════════════════════════════════════════════════════════════════════════
# TestAiClient
# ═══════════════════════════════════════════════════════════════════════════════


def _make_response(text: str, input_tokens: int = 100, output_tokens: int = 50) -> MagicMock:
    """Build a stub anthropic.Message-like object."""
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens

    content_block = MagicMock()
    content_block.text = text

    resp = MagicMock()
    resp.content = [content_block]
    resp.usage = usage
    return resp


class TestAiClient:
    def _stub_client(self, text: str = "Hello!", fail: bool = False) -> MagicMock:
        """Return a stub AsyncAnthropic client."""
        stub = MagicMock()
        if fail:
            stub.messages.create = AsyncMock(side_effect=RuntimeError("API error"))
        else:
            stub.messages.create = AsyncMock(return_value=_make_response(text))
        return stub

    @pytest.mark.asyncio
    async def test_success_returns_completion_result(self):
        stub = self._stub_client("Test response")
        client = AnthropicClient(
            primary_model="claude-sonnet-4-6",
            raw_client=stub,
        )
        result = await client.complete(
            system="You are a test assistant.",
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=100,
        )
        assert isinstance(result, CompletionResult)
        assert result.text == "Test response"
        assert result.model == "claude-sonnet-4-6"
        assert result.prompt_tokens == 100
        assert result.completion_tokens == 50

    @pytest.mark.asyncio
    async def test_uses_primary_model_by_default(self):
        stub = self._stub_client()
        client = AnthropicClient(primary_model="claude-sonnet-4-6", raw_client=stub)
        await client.complete(system="s", messages=[], max_tokens=10)
        call_kwargs = stub.messages.create.call_args.kwargs
        assert call_kwargs["model"] == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_model_override_per_call(self):
        stub = self._stub_client()
        client = AnthropicClient(primary_model="claude-sonnet-4-6", raw_client=stub)
        result = await client.complete(
            system="s", messages=[], max_tokens=10, model="claude-haiku-4-5"
        )
        assert result.model == "claude-haiku-4-5"
        call_kwargs = stub.messages.create.call_args.kwargs
        assert call_kwargs["model"] == "claude-haiku-4-5"

    @pytest.mark.asyncio
    async def test_fallback_triggered_on_primary_failure(self):
        """Primary fails → fallback succeeds → result uses fallback model."""
        fallback_stub = MagicMock()
        calls = []

        async def create_side_effect(**kwargs):
            calls.append(kwargs["model"])
            if kwargs["model"] == "claude-sonnet-4-6":
                raise RuntimeError("Sonnet down")
            return _make_response("fallback answer")

        fallback_stub.messages.create = create_side_effect

        client = AnthropicClient(
            primary_model="claude-sonnet-4-6",
            fallback_model="claude-haiku-4-5",
            raw_client=fallback_stub,
        )
        result = await client.complete(system="s", messages=[], max_tokens=10)
        assert result.model == "claude-haiku-4-5"
        assert result.text == "fallback answer"
        assert calls == ["claude-sonnet-4-6", "claude-haiku-4-5"]

    @pytest.mark.asyncio
    async def test_no_fallback_raises_immediately(self):
        stub = self._stub_client(fail=True)
        client = AnthropicClient(
            primary_model="claude-sonnet-4-6",
            fallback_model=None,
            raw_client=stub,
        )
        with pytest.raises(RuntimeError, match="API error"):
            await client.complete(system="s", messages=[], max_tokens=10)

    @pytest.mark.asyncio
    async def test_both_models_fail_raises_last_exception(self):
        stub = MagicMock()

        async def fail(**kwargs):
            if kwargs["model"] == "claude-sonnet-4-6":
                raise RuntimeError("Sonnet error")
            raise RuntimeError("Haiku error")

        stub.messages.create = fail
        client = AnthropicClient(
            primary_model="claude-sonnet-4-6",
            fallback_model="claude-haiku-4-5",
            raw_client=stub,
        )
        with pytest.raises(RuntimeError, match="Haiku error"):
            await client.complete(system="s", messages=[], max_tokens=10)

    @pytest.mark.asyncio
    async def test_get_ai_client_factory_uses_settings(self):
        """get_ai_client() builds from settings without hitting the API."""
        with patch("app.config.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                ai_default_model="claude-sonnet-4-6",
                ai_classifier_model="claude-haiku-4-5",
                anthropic_api_key="test-key",
            )
            client = get_ai_client(raw_client=MagicMock())
        assert client.primary_model == "claude-sonnet-4-6"
        assert client.fallback_model == "claude-haiku-4-5"

    @pytest.mark.asyncio
    async def test_resolve_client_raises_when_no_api_key(self):
        """Without raw_client and without api_key, resolve raises RuntimeError."""
        with patch("app.config.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(anthropic_api_key=None)
            client = AnthropicClient(primary_model="claude-sonnet-4-6")
            with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
                client._resolve_client()

    @pytest.mark.asyncio
    async def test_fallback_model_same_as_primary_not_duplicated(self):
        """If fallback == primary, only one attempt is made."""
        attempts = []
        stub = MagicMock()

        async def create_side(**kwargs):
            attempts.append(kwargs["model"])
            raise RuntimeError("always fails")

        stub.messages.create = create_side
        client = AnthropicClient(
            primary_model="claude-sonnet-4-6",
            fallback_model="claude-sonnet-4-6",
            raw_client=stub,
        )
        with pytest.raises(RuntimeError):
            await client.complete(system="s", messages=[], max_tokens=10)
        assert len(attempts) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# TestBudgetStatus
# ═══════════════════════════════════════════════════════════════════════════════


def _make_status(spend: str, budget: str) -> BudgetStatus:
    s = Decimal(spend)
    b = Decimal(budget)
    ratio = s / b if b > 0 else Decimal("0")
    return BudgetStatus(
        location_id=uuid.uuid4(),
        spend=s,
        budget=b,
        ratio=ratio,
    )


class TestBudgetStatus:
    def test_under_soft_cap(self):
        status = _make_status("75", "100")
        assert not status.is_soft_cap_exceeded
        assert not status.is_hard_cap_exceeded

    def test_exactly_at_soft_cap(self):
        status = _make_status("80", "100")
        assert status.is_soft_cap_exceeded
        assert not status.is_hard_cap_exceeded

    def test_between_soft_and_hard(self):
        status = _make_status("95", "100")
        assert status.is_soft_cap_exceeded
        assert not status.is_hard_cap_exceeded

    def test_at_hard_cap(self):
        status = _make_status("100", "100")
        assert status.is_soft_cap_exceeded
        assert status.is_hard_cap_exceeded

    def test_over_hard_cap(self):
        status = _make_status("150", "100")
        assert status.is_hard_cap_exceeded

    def test_remaining_usd(self):
        status = _make_status("30", "100")
        assert status.remaining_usd == Decimal("70")

    def test_summary_contains_location_id(self):
        status = _make_status("80", "100")
        assert str(status.location_id) in status.summary()


# ═══════════════════════════════════════════════════════════════════════════════
# TestIsEssential
# ═══════════════════════════════════════════════════════════════════════════════


class TestIsEssential:
    def test_gatekeeper_classification_is_essential(self):
        assert is_essential_call("gatekeeper", "classification") is True

    def test_gatekeeper_all_steps_essential(self):
        # Any step under gatekeeper is essential
        for step in ("classification", "intent_detection", "summary", "anything"):
            assert is_essential_call("gatekeeper", step) is True

    def test_uc04_reply_handling_is_essential(self):
        assert is_essential_call("UC04", "reply_handling") is True

    def test_uc04_message_generation_is_not_essential(self):
        assert is_essential_call("UC04", "message_generation") is False

    def test_uc01_all_steps_not_essential(self):
        for step in ("message_generation", "intent_detection", "classification"):
            assert is_essential_call("UC01", step) is False

    def test_uc05_all_steps_not_essential(self):
        for step in ("message_generation", "reply_handling", "summary"):
            assert is_essential_call("UC05", step) is False

    def test_unknown_use_case_not_essential(self):
        assert is_essential_call("UC99", "anything") is False


# ═══════════════════════════════════════════════════════════════════════════════
# TestCheckBudget
# ═══════════════════════════════════════════════════════════════════════════════


class TestCheckBudget:
    def _make_location(self, budget: str = "200") -> MagicMock:
        loc = MagicMock()
        loc.id = uuid.uuid4()
        loc.ai_monthly_budget_usd = Decimal(budget)
        return loc

    @pytest.mark.asyncio
    async def test_returns_budget_status(self):
        location = self._make_location("200")
        db = AsyncMock()
        with patch("app.ai.budget.get_monthly_spend", return_value=Decimal("50")):
            status = await check_budget(location, db)
        assert isinstance(status, BudgetStatus)
        assert status.spend == Decimal("50")
        assert status.budget == Decimal("200")

    @pytest.mark.asyncio
    async def test_ratio_computed_correctly(self):
        location = self._make_location("100")
        db = AsyncMock()
        with patch("app.ai.budget.get_monthly_spend", return_value=Decimal("80")):
            status = await check_budget(location, db)
        assert status.ratio == Decimal("0.8")

    @pytest.mark.asyncio
    async def test_zero_budget_ratio_is_zero(self):
        location = self._make_location("0")
        db = AsyncMock()
        with patch("app.ai.budget.get_monthly_spend", return_value=Decimal("0")):
            status = await check_budget(location, db)
        assert status.ratio == Decimal("0")

    @pytest.mark.asyncio
    async def test_get_monthly_spend_queries_db(self):
        """get_monthly_spend returns DB scalar result as Decimal."""
        location_id = uuid.uuid4()
        db = AsyncMock()
        db.scalar = AsyncMock(return_value=Decimal("42.500000"))
        result = await get_monthly_spend(location_id, db)
        assert result == Decimal("42.5")
        db.scalar.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_monthly_spend_none_returns_zero(self):
        location_id = uuid.uuid4()
        db = AsyncMock()
        db.scalar = AsyncMock(return_value=Decimal("0"))
        result = await get_monthly_spend(location_id, db)
        assert result == Decimal("0")


# ═══════════════════════════════════════════════════════════════════════════════
# TestAssertBudget
# ═══════════════════════════════════════════════════════════════════════════════


class TestAssertBudget:
    @pytest.mark.asyncio
    async def test_under_soft_cap_passes_silently(self):
        status = _make_status("50", "100")
        await assert_budget_available(status, use_case="UC04", step="message_generation")

    @pytest.mark.asyncio
    async def test_soft_cap_exceeded_passes_with_warning(self, caplog):
        status = _make_status("85", "100")
        import logging

        with caplog.at_level(logging.WARNING, logger="app.ai.budget"):
            await assert_budget_available(
                status, use_case="UC04", step="message_generation"
            )
        # Warning logged but no exception raised
        assert any("soft cap" in r.message.lower() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_hard_cap_non_essential_raises(self):
        status = _make_status("100", "100")
        with pytest.raises(BudgetExceededError, match="UC04"):
            await assert_budget_available(
                status, use_case="UC04", step="message_generation"
            )

    @pytest.mark.asyncio
    async def test_hard_cap_essential_passes_with_warning(self, caplog):
        status = _make_status("100", "100")
        import logging

        with caplog.at_level(logging.WARNING, logger="app.ai.budget"):
            await assert_budget_available(
                status, use_case="gatekeeper", step="classification"
            )
        assert any("essential" in r.message.lower() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_hard_cap_uc04_inbound_passes(self):
        status = _make_status("200", "100")
        # UC04 / reply_handling is essential — must not raise
        await assert_budget_available(
            status, use_case="UC04", step="reply_handling"
        )

    @pytest.mark.asyncio
    async def test_hard_cap_uc01_raises(self):
        status = _make_status("100", "100")
        with pytest.raises(BudgetExceededError):
            await assert_budget_available(
                status, use_case="UC01", step="message_generation"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# TestSoftCapEmail
# ═══════════════════════════════════════════════════════════════════════════════


class TestSoftCapEmail:
    @pytest.mark.asyncio
    async def test_no_email_if_under_soft_cap(self):
        status = _make_status("70", "100")
        result = await maybe_send_soft_cap_warning(
            status, owner_email="owner@example.com", location_name="Test Studio"
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_no_email_if_owner_email_not_configured(self):
        status = _make_status("85", "100")
        result = await maybe_send_soft_cap_warning(
            status, owner_email=None, location_name="Test Studio"
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_no_smtp_host_returns_false(self):
        status = _make_status("85", "100")
        with patch("app.config.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(notification_smtp_host=None)
            result = await maybe_send_soft_cap_warning(
                status, owner_email="owner@example.com"
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_smtp_send_returns_true(self):
        status = _make_status("85", "100")
        with patch("app.config.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                notification_smtp_host="smtp.example.com",
                notification_smtp_port=587,
                notification_smtp_user="user",
                notification_smtp_password="pass",
                notification_from_email="noreply@example.com",
            )
            with patch("asyncio.get_event_loop") as mock_loop:
                mock_loop.return_value.run_in_executor = AsyncMock(return_value=None)
                result = await maybe_send_soft_cap_warning(
                    status, owner_email="owner@example.com", location_name="Studio"
                )
        assert result is True

    @pytest.mark.asyncio
    async def test_smtp_failure_returns_false_not_raises(self):
        status = _make_status("85", "100")
        with patch("app.config.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                notification_smtp_host="smtp.example.com",
                notification_smtp_port=587,
                notification_smtp_user=None,
                notification_smtp_password=None,
                notification_from_email="noreply@example.com",
            )
            with patch("asyncio.get_event_loop") as mock_loop:
                mock_loop.return_value.run_in_executor = AsyncMock(
                    side_effect=OSError("Connection refused")
                )
                result = await maybe_send_soft_cap_warning(
                    status, owner_email="owner@example.com"
                )
        # Non-fatal — returns False instead of raising
        assert result is False

    @pytest.mark.asyncio
    async def test_dedup_prevents_second_email_same_month(self):
        """
        A second call for the same location in the same calendar month should
        return False immediately without touching SMTP.
        """
        import app.ai.budget as budget_module

        loc_id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        status = BudgetStatus(
            location_id=loc_id,
            spend=Decimal("85"),
            budget=Decimal("100"),
            ratio=Decimal("0.85"),
        )
        # Ensure clean state for this location before the test
        budget_module._soft_cap_warned.pop(str(loc_id), None)

        try:
            with patch("app.config.get_settings") as mock_settings:
                mock_settings.return_value = MagicMock(
                    notification_smtp_host="smtp.example.com",
                    notification_smtp_port=587,
                    notification_smtp_user=None,
                    notification_smtp_password=None,
                    notification_from_email="noreply@example.com",
                )
                with patch("asyncio.get_event_loop") as mock_loop:
                    mock_loop.return_value.run_in_executor = AsyncMock(return_value=None)
                    # First call — should send
                    first = await maybe_send_soft_cap_warning(
                        status, owner_email="owner@example.com", location_name="Studio"
                    )
                    # Second call — same location, same month — must be deduplicated
                    second = await maybe_send_soft_cap_warning(
                        status, owner_email="owner@example.com", location_name="Studio"
                    )
            assert first is True, "first call should have sent the warning"
            assert second is False, "second call must be suppressed by dedup cache"
        finally:
            # Leave no state for other tests
            budget_module._soft_cap_warned.pop(str(loc_id), None)
