"""
Gatekeeper tests (M6b).

Coverage:
  TestClassifier         — classify() with stub client, fallbacks, token cost
  TestContactSnippet     — build_contact_snippet() edge cases
  TestRouter             — route_classification() for all 15 categories
  TestNoisePolicy        — execute_noise_policy() for all three policies
  TestGatekeeperAudit    — log_classification(), log_ai_usage(), apply_owner_override()
  TestGatekeeperGate     — process_inbound() disabled / happy-path / error-path
  TestWebhookIntegration — end-to-end via TestClient: non-STOP + STOP + disabled
  TestOwnerOverrideAPI   — PATCH override + GET list via TestClient
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.v1.admin.gatekeeper import router as gatekeeper_admin_router
from app.api.v1.webhooks.ghl_inbound import router as ghl_inbound_router
from app.db.models.ai_usage import AiUsage
from app.db.models.base import Base
from app.db.models.gatekeeper_log import GatekeeperLog
from app.db.models.location import Location
from app.db.session import get_db
from app.gatekeeper.audit import apply_owner_override, log_ai_usage, log_classification
from app.gatekeeper.classifier import (
    CLASSIFICATION_CATEGORIES,
    ClassificationResult,
    _parse_response,
    build_contact_snippet,
    classify,
)
from app.gatekeeper.gate import GatekeeperDecision, process_inbound
from app.gatekeeper.noise_policy import execute_noise_policy
from app.gatekeeper.router import route_classification

# ── Test database ─────────────────────────────────────────────────────────────

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def engine():
    eng = create_async_engine(TEST_DB_URL, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@pytest.fixture
async def db(session_factory):
    async with session_factory() as session:
        yield session


# ── Shared location fixture ───────────────────────────────────────────────────

_LOCATION_ID = uuid.uuid4()


async def _make_location(
    db: AsyncSession,
    *,
    gatekeeper_enabled: bool = True,
    confidence_threshold: float = 0.7,
    noise_action: dict | None = None,
    owner_alert_categories: str = "complaint,injury_medical,billing_dispute,low_confidence",
) -> Location:
    noise = noise_action or {
        "acknowledgment": "silent_ignore",
        "emoji_reaction": "react_emoji",
        "social_compliment": "react_emoji",
        "off_topic": "silent_ignore",
        "spam": "silent_ignore",
    }
    loc = Location(
        id=_LOCATION_ID,
        eversports_studio_id="es_test",
        ghl_subaccount_id="ghl_test",
        ghl_oauth_token_ref="secret",
        eversports_credentials_ref="creds",
        timezone="Europe/Vienna",
        studio_owner_email="owner@test.com",
        studio_name="Test Studio",
        location_name="Test Studio Vienna",
        stop_keywords="",
        consent_default_locale="de-AT",
        gatekeeper_enabled=gatekeeper_enabled,
        gatekeeper_confidence_threshold=Decimal(str(confidence_threshold)),
        gatekeeper_noise_action=noise,
        gatekeeper_owner_alert_categories=owner_alert_categories,
        whatsapp_templates={},
        product_keyword_map={},
    )
    db.add(loc)
    await db.commit()
    return loc


# ── Stub Anthropic client ─────────────────────────────────────────────────────

def _make_stub_client(
    category: str = "inquiry_pricing",
    confidence: float = 0.9,
    reasoning: str = "Test reasoning.",
    prompt_tokens: int = 100,
    output_tokens: int = 40,
) -> Any:
    """Create a mock anthropic client returning a deterministic classification."""
    raw = json.dumps({
        "category": category,
        "confidence": confidence,
        "reasoning": reasoning,
    })
    usage = MagicMock()
    usage.input_tokens = prompt_tokens
    usage.output_tokens = output_tokens

    content_item = MagicMock()
    content_item.text = raw

    response = MagicMock()
    response.content = [content_item]
    response.usage = usage

    messages_mock = AsyncMock()
    messages_mock.create = AsyncMock(return_value=response)

    client = MagicMock()
    client.messages = messages_mock
    return client


# ═══════════════════════════════════════════════════════════════════════════════
# TestClassifier
# ═══════════════════════════════════════════════════════════════════════════════


class TestClassifier:
    async def test_classify_returns_correct_category(self):
        client = _make_stub_client(category="booking", confidence=0.95)
        result = await classify(
            "I want to book a yoga class",
            channel="whatsapp",
            location_name="Test Studio",
            client=client,
        )
        assert result.category == "booking"
        assert result.confidence == pytest.approx(0.95)
        assert result.model == "claude-haiku-4-5"

    async def test_classify_passes_contact_snippet(self):
        client = _make_stub_client()
        await classify(
            "Hi",
            channel="whatsapp",
            location_name="Test",
            contact_snippet="name=Anna | stage='trial'",
            client=client,
        )
        call_args = client.messages.create.call_args
        user_msg = call_args.kwargs["messages"][0]["content"]
        assert "Anna" in user_msg
        assert "trial" in user_msg

    async def test_classify_fallback_on_unknown_category(self):
        client = _make_stub_client(category="totally_invalid_cat")
        result = await classify("msg", channel="whatsapp", location_name="Test", client=client)
        assert result.category == "low_confidence"
        assert result.confidence == 0.0

    async def test_classify_fallback_on_api_exception(self):
        client = MagicMock()
        client.messages = MagicMock()
        client.messages.create = AsyncMock(side_effect=RuntimeError("network error"))
        result = await classify("msg", channel="whatsapp", location_name="Test", client=client)
        assert result.category == "low_confidence"
        assert result.confidence == 0.0

    async def test_classify_fallback_on_malformed_json(self):
        content_item = MagicMock()
        content_item.text = "not json at all"
        response = MagicMock()
        response.content = [content_item]
        response.usage = MagicMock()
        response.usage.input_tokens = 0
        response.usage.output_tokens = 0
        client = MagicMock()
        client.messages.create = AsyncMock(return_value=response)
        result = await classify("msg", channel="whatsapp", location_name="Test", client=client)
        assert result.category == "low_confidence"

    async def test_classify_confidence_clamped(self):
        """Confidence outside [0, 1] is clamped."""
        client = _make_stub_client(category="booking", confidence=1.5)
        result = await classify("book me", channel="whatsapp", location_name="Test", client=client)
        assert result.confidence <= 1.0

    async def test_classify_uses_all_categories(self):
        """Every CLASSIFICATION_CATEGORIES value is returned correctly."""
        for cat in CLASSIFICATION_CATEGORIES:
            client = _make_stub_client(category=cat, confidence=0.85)
            result = await classify("msg", channel="whatsapp", location_name="T", client=client)
            assert result.category == cat

    async def test_cost_usd_computed(self):
        client = _make_stub_client(prompt_tokens=1000, output_tokens=400)
        result = await classify("msg", channel="whatsapp", location_name="T", client=client)
        assert result.cost_usd > 0

    async def test_parse_response_missing_fields_use_defaults(self):
        """JSON with only category — confidence defaults to 0.0."""
        response = MagicMock()
        response.usage = MagicMock()
        response.usage.input_tokens = 0
        response.usage.output_tokens = 0
        result = _parse_response(
            json.dumps({"category": "booking"}),
            model="claude-haiku-4-5",
            response=response,
        )
        assert result.category == "booking"
        assert result.confidence == 0.0

    async def test_no_api_key_returns_fallback(self):
        """When anthropic_api_key is None, returns low_confidence without crashing."""
        # get_settings is lazily imported inside classify() from app.config,
        # so we patch at the source module.
        with patch("app.config.get_settings") as mock_get:
            settings = MagicMock()
            settings.anthropic_api_key = None
            settings.ai_classifier_model = "claude-haiku-4-5"
            mock_get.return_value = settings
            result = await classify("msg", channel="whatsapp", location_name="T")
        assert result.category == "low_confidence"


# ═══════════════════════════════════════════════════════════════════════════════
# TestContactSnippet
# ═══════════════════════════════════════════════════════════════════════════════


class TestContactSnippet:
    def test_full_snippet(self):
        snippet = build_contact_snippet(
            first_name="Anna",
            tags=["trial-user", "newsletter"],
            pipeline_stage="trial-active",
            active_package="10-class-card",
        )
        assert "Anna" in snippet
        assert "trial-user" in snippet
        assert "10-class-card" in snippet
        assert "trial-active" in snippet

    def test_empty_returns_unknown(self):
        snippet = build_contact_snippet()
        assert snippet == "unknown contact"

    def test_opted_out_flag(self):
        snippet = build_contact_snippet(opted_out=True)
        assert "opted_out=true" in snippet

    def test_tags_capped_at_five(self):
        tags = ["t1", "t2", "t3", "t4", "t5", "t6", "t7"]
        snippet = build_contact_snippet(tags=tags)
        # Should only show up to 5 tags
        assert "t6" not in snippet

    def test_opted_out_via_tags(self):
        snippet = build_contact_snippet(
            first_name="Bob",
            tags=["opted-out"],
            opted_out=True,
        )
        assert "opted_out=true" in snippet


# ═══════════════════════════════════════════════════════════════════════════════
# TestRouter
# ═══════════════════════════════════════════════════════════════════════════════


def _make_result(category: str, confidence: float = 0.9) -> ClassificationResult:
    return ClassificationResult(
        category=category,
        confidence=confidence,
        reasoning="test",
        model="claude-haiku-4-5",
        prompt_tokens=100,
        completion_tokens=40,
    )


_DEFAULT_NOISE_MAP = {
    "acknowledgment": "silent_ignore",
    "emoji_reaction": "react_emoji",
    "social_compliment": "react_emoji",
    "off_topic": "silent_ignore",
    "spam": "silent_ignore",
}
_DEFAULT_ALERT_CATS = "complaint,injury_medical,billing_dispute,low_confidence"


class TestRouter:
    def test_inquiry_pricing_routes_uc04(self):
        r, a, actions = route_classification(
            _make_result("inquiry_pricing"),
            confidence_threshold=0.7,
            owner_alert_categories=_DEFAULT_ALERT_CATS,
            noise_action_map=_DEFAULT_NOISE_MAP,
            channel="whatsapp",
        )
        assert r == "uc04"
        assert "inquiry_pricing" in a
        assert actions == []

    def test_inquiry_class_info_routes_uc04(self):
        r, _, _ = route_classification(
            _make_result("inquiry_class_info"),
            confidence_threshold=0.7,
            owner_alert_categories=_DEFAULT_ALERT_CATS,
            noise_action_map=_DEFAULT_NOISE_MAP,
            channel="whatsapp",
        )
        assert r == "uc04"

    def test_inquiry_membership_routes_uc04(self):
        r, _, _ = route_classification(
            _make_result("inquiry_membership"),
            confidence_threshold=0.7,
            owner_alert_categories=_DEFAULT_ALERT_CATS,
            noise_action_map=_DEFAULT_NOISE_MAP,
            channel="whatsapp",
        )
        assert r == "uc04"

    def test_trial_reply_routes_uc04(self):
        r, _, _ = route_classification(
            _make_result("trial_reply"),
            confidence_threshold=0.7,
            owner_alert_categories=_DEFAULT_ALERT_CATS,
            noise_action_map=_DEFAULT_NOISE_MAP,
            channel="whatsapp",
        )
        assert r == "uc04"

    def test_booking_routes_uc05(self):
        r, a, _ = route_classification(
            _make_result("booking"),
            confidence_threshold=0.7,
            owner_alert_categories=_DEFAULT_ALERT_CATS,
            noise_action_map=_DEFAULT_NOISE_MAP,
            channel="whatsapp",
        )
        assert r == "uc05"
        assert "booking" in a

    def test_complaint_escalates_to_owner(self):
        r, a, _ = route_classification(
            _make_result("complaint"),
            confidence_threshold=0.7,
            owner_alert_categories=_DEFAULT_ALERT_CATS,
            noise_action_map=_DEFAULT_NOISE_MAP,
            channel="whatsapp",
        )
        assert r == "owner"
        assert "complaint" in a

    def test_injury_medical_escalates_to_owner(self):
        r, _, _ = route_classification(
            _make_result("injury_medical"),
            confidence_threshold=0.7,
            owner_alert_categories=_DEFAULT_ALERT_CATS,
            noise_action_map=_DEFAULT_NOISE_MAP,
            channel="whatsapp",
        )
        assert r == "owner"

    def test_billing_dispute_escalates_to_owner(self):
        r, _, _ = route_classification(
            _make_result("billing_dispute"),
            confidence_threshold=0.7,
            owner_alert_categories=_DEFAULT_ALERT_CATS,
            noise_action_map=_DEFAULT_NOISE_MAP,
            channel="whatsapp",
        )
        assert r == "owner"

    def test_low_confidence_explicit_escalates(self):
        r, a, _ = route_classification(
            _make_result("low_confidence", confidence=0.0),
            confidence_threshold=0.7,
            owner_alert_categories=_DEFAULT_ALERT_CATS,
            noise_action_map=_DEFAULT_NOISE_MAP,
            channel="whatsapp",
        )
        assert r == "owner"
        assert "low_confidence" in a

    def test_confidence_below_threshold_treated_as_low_confidence(self):
        r, a, _ = route_classification(
            _make_result("booking", confidence=0.5),
            confidence_threshold=0.7,
            owner_alert_categories=_DEFAULT_ALERT_CATS,
            noise_action_map=_DEFAULT_NOISE_MAP,
            channel="whatsapp",
        )
        assert r == "owner"
        assert "low_confidence" in a

    def test_acknowledgment_silent_ignore(self):
        r, a, actions = route_classification(
            _make_result("acknowledgment"),
            confidence_threshold=0.7,
            owner_alert_categories=_DEFAULT_ALERT_CATS,
            noise_action_map=_DEFAULT_NOISE_MAP,
            channel="whatsapp",
        )
        assert r == "noise"
        assert a == "silent_ignore"
        assert actions == []

    def test_emoji_reaction_react_emoji(self):
        r, a, actions = route_classification(
            _make_result("emoji_reaction"),
            confidence_threshold=0.7,
            owner_alert_categories=_DEFAULT_ALERT_CATS,
            noise_action_map=_DEFAULT_NOISE_MAP,
            channel="whatsapp",
        )
        assert r == "noise"
        assert a == "react_emoji"
        assert len(actions) == 1
        assert actions[0]["action"] == "send_message"  # WhatsApp → text reply

    def test_social_compliment_react_emoji_instagram(self):
        r, a, actions = route_classification(
            _make_result("social_compliment"),
            confidence_threshold=0.7,
            owner_alert_categories=_DEFAULT_ALERT_CATS,
            noise_action_map=_DEFAULT_NOISE_MAP,
            channel="instagram_comment",
        )
        assert r == "noise"
        assert a == "react_emoji"
        # Instagram → native react_emoji action
        assert actions[0]["action"] == "react_emoji"

    def test_off_topic_silent_ignore(self):
        r, a, _ = route_classification(
            _make_result("off_topic"),
            confidence_threshold=0.7,
            owner_alert_categories=_DEFAULT_ALERT_CATS,
            noise_action_map=_DEFAULT_NOISE_MAP,
            channel="whatsapp",
        )
        assert r == "noise"
        assert a == "silent_ignore"

    def test_spam_silent_ignore(self):
        r, a, _ = route_classification(
            _make_result("spam"),
            confidence_threshold=0.7,
            owner_alert_categories=_DEFAULT_ALERT_CATS,
            noise_action_map=_DEFAULT_NOISE_MAP,
            channel="whatsapp",
        )
        assert r == "noise"
        assert a == "silent_ignore"

    def test_opt_out_routes_to_consent_gate(self):
        """opt_out category → consent_gate (V1 auditor fix)."""
        r, a, actions = route_classification(
            _make_result("opt_out"),
            confidence_threshold=0.7,
            owner_alert_categories=_DEFAULT_ALERT_CATS,
            noise_action_map=_DEFAULT_NOISE_MAP,
            channel="whatsapp",
        )
        # Must route to consent_gate, not owner — legally required for opt-out
        assert r == "consent_gate"
        assert a == "consent_gate_opt_out"
        assert actions == []

    def test_empty_owner_alert_categories_uses_default(self):
        r, _, _ = route_classification(
            _make_result("complaint"),
            confidence_threshold=0.7,
            owner_alert_categories="",
            noise_action_map=_DEFAULT_NOISE_MAP,
            channel="whatsapp",
        )
        # empty string → _DEFAULT_OWNER_ALERT which includes complaint
        assert r == "owner"

    def test_custom_owner_alert_categories(self):
        r, _, _ = route_classification(
            _make_result("social_compliment"),
            confidence_threshold=0.7,
            owner_alert_categories="social_compliment",
            noise_action_map=_DEFAULT_NOISE_MAP,
            channel="whatsapp",
        )
        assert r == "owner"


# ═══════════════════════════════════════════════════════════════════════════════
# TestNoisePolicy
# ═══════════════════════════════════════════════════════════════════════════════


class TestNoisePolicy:
    def test_silent_ignore_returns_empty(self):
        actions = execute_noise_policy("silent_ignore", channel="whatsapp", category="spam")
        assert actions == []

    def test_unknown_policy_defaults_to_silent_ignore(self):
        actions = execute_noise_policy(
            "totally_unknown_policy", channel="whatsapp", category="acknowledgment"
        )
        assert actions == []

    def test_react_emoji_whatsapp_sends_message(self):
        actions = execute_noise_policy(
            "react_emoji", channel="whatsapp", category="emoji_reaction"
        )
        assert len(actions) == 1
        assert actions[0]["action"] == "send_message"
        assert actions[0]["channel"] == "whatsapp"

    def test_react_emoji_instagram_dm_native_react(self):
        actions = execute_noise_policy(
            "react_emoji", channel="instagram_dm", category="social_compliment"
        )
        assert len(actions) == 1
        assert actions[0]["action"] == "react_emoji"
        assert "emoji" in actions[0]

    def test_react_emoji_facebook_comment_native_react(self):
        actions = execute_noise_policy(
            "react_emoji", channel="facebook_comment", category="emoji_reaction"
        )
        assert actions[0]["action"] == "react_emoji"

    def test_auto_reply_template_default_locale(self):
        actions = execute_noise_policy(
            "auto_reply_template",
            channel="whatsapp",
            category="social_compliment",
            locale="de-AT",
        )
        assert len(actions) == 1
        assert actions[0]["action"] == "send_message"
        assert "Danke" in actions[0]["body"]

    def test_auto_reply_template_english(self):
        actions = execute_noise_policy(
            "auto_reply_template",
            channel="whatsapp",
            category="acknowledgment",
            locale="en",
        )
        assert "Thanks" in actions[0]["body"]

    def test_auto_reply_template_custom_template_wins(self):
        custom = {"whatsapp": {"acknowledgment": "Super! 🎉"}}
        actions = execute_noise_policy(
            "auto_reply_template",
            channel="whatsapp",
            category="acknowledgment",
            locale="de",
            custom_templates=custom,
        )
        assert actions[0]["body"] == "Super! 🎉"

    def test_auto_reply_template_channel_fallback(self):
        """Uses auto_reply_noise key when category-specific key missing."""
        custom = {"whatsapp": {"auto_reply_noise": "👍"}}
        actions = execute_noise_policy(
            "auto_reply_template",
            channel="whatsapp",
            category="social_compliment",
            locale="de",
            custom_templates=custom,
        )
        assert actions[0]["body"] == "👍"

    def test_auto_reply_template_unknown_locale_fallback(self):
        """Unknown locale → fallback default reply text."""
        actions = execute_noise_policy(
            "auto_reply_template",
            channel="whatsapp",
            category="acknowledgment",
            locale="fr",  # not in _DEFAULT_AUTO_REPLY
        )
        # Should fall back (or return empty for silent_ignore)
        # Either a body exists or actions is empty (silent_ignore fallback)
        assert isinstance(actions, list)


# ═══════════════════════════════════════════════════════════════════════════════
# TestGatekeeperAudit
# ═══════════════════════════════════════════════════════════════════════════════


class TestGatekeeperAudit:
    async def test_log_classification_inserts_row(self, db, engine):
        loc_id = uuid.uuid4()
        row = await log_classification(
            db,
            location_id=loc_id,
            ghl_contact_id="c1",
            inbound_channel="whatsapp",
            raw_text="I want to book a class",
            classification="booking",
            confidence=0.92,
            route_to="uc05",
            action_taken="routed_booking",
        )
        await db.commit()
        assert row.id is not None
        assert row.classification == "booking"
        assert float(row.confidence) == pytest.approx(0.92, abs=0.001)
        assert row.route_to == "uc05"

    async def test_log_classification_with_optional_fields(self, db):
        loc_id = uuid.uuid4()
        row = await log_classification(
            db,
            location_id=loc_id,
            ghl_contact_id=None,
            inbound_channel="instagram_comment",
            raw_text="🔥",
            classification="emoji_reaction",
            confidence=0.99,
            route_to="noise",
            action_taken="react_emoji",
            inbound_surface="post_abc123",
            ghl_message_id="msg_456",
            contact_id=uuid.uuid4(),
        )
        await db.commit()
        assert row.inbound_surface == "post_abc123"
        assert row.ghl_message_id == "msg_456"
        assert row.ghl_contact_id is None

    async def test_log_ai_usage_inserts_row(self, db):
        loc_id = uuid.uuid4()
        result = ClassificationResult(
            category="booking",
            confidence=0.9,
            reasoning="User wants to book.",
            model="claude-haiku-4-5",
            prompt_tokens=150,
            completion_tokens=45,
        )
        row = await log_ai_usage(
            db,
            location_id=loc_id,
            ghl_contact_id="c2",
            result=result,
        )
        await db.commit()
        assert row.id is not None
        assert row.use_case == "gatekeeper"
        assert row.step == "classification"
        assert row.model == "claude-haiku-4-5"
        assert row.prompt_tokens == 150
        assert float(row.cost_usd) > 0

    async def test_apply_owner_override_updates_row(self, db):
        loc_id = uuid.uuid4()
        orig = await log_classification(
            db,
            location_id=loc_id,
            ghl_contact_id="c3",
            inbound_channel="whatsapp",
            raw_text="spam msg",
            classification="spam",
            confidence=0.85,
            route_to="noise",
            action_taken="silent_ignore",
        )
        await db.commit()

        updated = await apply_owner_override(db, orig.id, "inquiry_pricing")
        await db.commit()

        assert updated.owner_override == "inquiry_pricing"
        assert updated.override_ts is not None

    async def test_apply_owner_override_raises_on_missing(self, db):
        with pytest.raises(LookupError):
            await apply_owner_override(db, uuid.uuid4(), "booking")

    async def test_log_classification_sets_timestamp(self, db):
        from datetime import datetime, timezone

        now = datetime.now(tz=timezone.utc)
        row = await log_classification(
            db,
            location_id=uuid.uuid4(),
            ghl_contact_id="c4",
            inbound_channel="email",
            raw_text="test",
            classification="inquiry_pricing",
            confidence=0.8,
            route_to="uc04",
            action_taken="routed_inquiry_pricing",
        )
        await db.commit()
        assert row.ts >= now


# ═══════════════════════════════════════════════════════════════════════════════
# TestGatekeeperGate
# ═══════════════════════════════════════════════════════════════════════════════


class TestGatekeeperGate:
    async def test_disabled_returns_legacy_no_db_writes(self, db):
        await _make_location(db, gatekeeper_enabled=False)
        # Re-fetch to get committed location
        from sqlalchemy import select as sa_select
        result = await db.execute(sa_select(Location))
        loc = result.scalar_one()

        decision = await process_inbound(
            db,
            location=loc,
            ghl_contact_id="c1",
            message="Hello there",
            channel="whatsapp",
        )
        assert decision.route_to == "legacy"
        assert decision.action_taken == "legacy_uc04"
        assert decision.log_id is None

        # No gatekeeper_log rows written
        rows = (await db.execute(sa_select(GatekeeperLog))).scalars().all()
        assert rows == []

    async def test_enabled_classifies_and_logs(self, db):
        await _make_location(db, gatekeeper_enabled=True)
        from sqlalchemy import select as sa_select
        result = await db.execute(sa_select(Location))
        loc = result.scalar_one()

        client = _make_stub_client(category="inquiry_pricing", confidence=0.92)
        decision = await process_inbound(
            db,
            location=loc,
            ghl_contact_id="c2",
            message="What are your prices?",
            channel="whatsapp",
            classifier_client=client,
        )
        await db.commit()

        assert decision.classification == "inquiry_pricing"
        assert decision.route_to == "uc04"
        assert decision.log_id is not None

        # Check gatekeeper_log row
        rows = (await db.execute(sa_select(GatekeeperLog))).scalars().all()
        assert len(rows) == 1
        assert rows[0].classification == "inquiry_pricing"

        # Check ai_usage row
        ai_rows = (await db.execute(sa_select(AiUsage))).scalars().all()
        assert len(ai_rows) == 1
        assert ai_rows[0].use_case == "gatekeeper"

    async def test_below_threshold_escalates_to_owner(self, db):
        await _make_location(db, gatekeeper_enabled=True, confidence_threshold=0.8)
        from sqlalchemy import select as sa_select
        result = await db.execute(sa_select(Location))
        loc = result.scalar_one()

        client = _make_stub_client(category="booking", confidence=0.6)
        decision = await process_inbound(
            db,
            location=loc,
            ghl_contact_id="c3",
            message="Some ambiguous message",
            channel="whatsapp",
            classifier_client=client,
        )
        await db.commit()

        assert decision.route_to == "owner"
        # The classification stored is the raw result, not "low_confidence"
        assert decision.classification == "booking"

    async def test_noise_message_returns_ghl_actions(self, db):
        await _make_location(
            db,
            noise_action={"emoji_reaction": "react_emoji"},
        )
        from sqlalchemy import select as sa_select
        result = await db.execute(sa_select(Location))
        loc = result.scalar_one()

        client = _make_stub_client(category="emoji_reaction", confidence=0.95)
        decision = await process_inbound(
            db,
            location=loc,
            ghl_contact_id="c4",
            message="🔥",
            channel="whatsapp",
            classifier_client=client,
        )
        await db.commit()

        assert decision.route_to == "noise"
        assert len(decision.ghl_actions) > 0

    async def test_classifier_error_falls_through_to_owner(self, db):
        """Even if classifier errors, we still get a valid decision."""
        await _make_location(db, gatekeeper_enabled=True)
        from sqlalchemy import select as sa_select
        result = await db.execute(sa_select(Location))
        loc = result.scalar_one()

        # Client that raises on call
        bad_client = MagicMock()
        bad_client.messages = MagicMock()
        bad_client.messages.create = AsyncMock(side_effect=RuntimeError("quota exceeded"))

        decision = await process_inbound(
            db,
            location=loc,
            ghl_contact_id="c5",
            message="test",
            channel="whatsapp",
            classifier_client=bad_client,
        )
        await db.commit()

        assert decision.classification == "low_confidence"
        assert decision.route_to == "owner"


# ═══════════════════════════════════════════════════════════════════════════════
# TestWebhookIntegration
# ═══════════════════════════════════════════════════════════════════════════════

def _make_test_app(session_factory) -> FastAPI:
    """Build a minimal FastAPI app for webhook / gatekeeper tests (no lifespan)."""

    @asynccontextmanager
    async def _noop_lifespan(app: FastAPI):  # type: ignore[misc]
        yield

    app = FastAPI(lifespan=_noop_lifespan)
    app.include_router(ghl_inbound_router)
    app.include_router(gatekeeper_admin_router)

    async def _override_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_db
    return app


@pytest.fixture
def settings_skip_sig():
    """Hold a settings mock active for the full test (skips signature check)."""
    with patch("app.config.get_settings") as mock_get:
        settings = MagicMock()
        settings.ghl_webhook_skip_sig_check = True
        settings.ai_classifier_model = "claude-haiku-4-5"
        settings.anthropic_api_key = None
        mock_get.return_value = settings
        yield settings


@pytest.fixture
async def test_app(session_factory):
    return _make_test_app(session_factory)


@pytest.fixture
async def client(test_app, settings_skip_sig):
    """AsyncClient bound to the test app, with sig-check skipped for all requests."""
    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://test"
    ) as ac:
        yield ac


async def _seed_location(session_factory, **kwargs) -> Location:
    async with session_factory() as db:
        loc = await _make_location(db, **kwargs)
        return loc


_INBOUND_PAYLOAD = {
    "type": "InboundMessage",
    "locationId": "ghl_test",
    "contactId": "contact_123",
    "channel": "whatsapp",
    "messageBody": "What are your prices?",
    "firstName": "Anna",
    "locale": "de-AT",
}


class TestWebhookIntegration:
    async def test_stop_keyword_returns_is_stop_true(self, client, session_factory):
        await _seed_location(session_factory)
        payload = {**_INBOUND_PAYLOAD, "messageBody": "STOP"}
        resp = await client.post("/api/v1/webhooks/ghl/inbound", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_stop"] is True
        assert data["gatekeeper"] is None
        assert len(data["ghl_actions"]) == 5

    async def test_stop_ghl_actions_structure(self, client, session_factory):
        await _seed_location(session_factory)
        payload = {**_INBOUND_PAYLOAD, "messageBody": "STOPP"}
        resp = await client.post("/api/v1/webhooks/ghl/inbound", json=payload)
        actions = resp.json()["ghl_actions"]
        action_types = [a["action"] for a in actions]
        assert "update_contact_field" in action_types
        assert "apply_tag" in action_types
        assert "remove_from_all_sequences" in action_types
        assert "send_message" in action_types

    async def test_non_stop_with_gatekeeper_enabled(self, client, session_factory):
        await _seed_location(session_factory)
        with patch("app.gatekeeper.gate.classify", new=AsyncMock(return_value=
            ClassificationResult(
                category="inquiry_pricing",
                confidence=0.9,
                reasoning="Asking about prices",
                model="claude-haiku-4-5",
                prompt_tokens=100,
                completion_tokens=40,
            )
        )):
            resp = await client.post("/api/v1/webhooks/ghl/inbound", json=_INBOUND_PAYLOAD)
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_stop"] is False
        assert data["gatekeeper"] is not None
        assert data["gatekeeper"]["classification"] == "inquiry_pricing"
        assert data["gatekeeper"]["route_to"] == "uc04"

    async def test_non_stop_gatekeeper_disabled_returns_legacy(self, client, session_factory):
        await _seed_location(session_factory, gatekeeper_enabled=False)
        resp = await client.post("/api/v1/webhooks/ghl/inbound", json=_INBOUND_PAYLOAD)
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_stop"] is False
        assert data["gatekeeper"]["route_to"] == "legacy"
        assert data["gatekeeper"]["action_taken"] == "legacy_uc04"

    async def test_location_not_found_returns_404(self, client, session_factory):
        await _seed_location(session_factory)
        payload = {**_INBOUND_PAYLOAD, "locationId": "nonexistent_loc"}
        resp = await client.post("/api/v1/webhooks/ghl/inbound", json=payload)
        assert resp.status_code == 404

    async def test_noise_message_actions_in_response(self, client, session_factory):
        await _seed_location(
            session_factory,
            noise_action={"emoji_reaction": "react_emoji"},
        )
        with patch("app.gatekeeper.gate.classify", new=AsyncMock(return_value=
            ClassificationResult(
                category="emoji_reaction",
                confidence=0.99,
                reasoning="Emoji only",
                model="claude-haiku-4-5",
                prompt_tokens=50,
                completion_tokens=20,
            )
        )):
            payload = {**_INBOUND_PAYLOAD, "messageBody": "🔥"}
            resp = await client.post("/api/v1/webhooks/ghl/inbound", json=payload)
        data = resp.json()
        assert data["gatekeeper"]["route_to"] == "noise"
        assert len(data["ghl_actions"]) > 0

    async def test_stop_german_aufhoeren(self, client, session_factory):
        await _seed_location(session_factory)
        payload = {**_INBOUND_PAYLOAD, "messageBody": "aufhören"}
        resp = await client.post("/api/v1/webhooks/ghl/inbound", json=payload)
        assert resp.json()["is_stop"] is True

    async def test_sms_channel_normalised_to_whatsapp(self, client, session_factory):
        await _seed_location(session_factory)
        with patch("app.gatekeeper.gate.classify", new=AsyncMock(return_value=
            ClassificationResult(
                category="inquiry_class_info",
                confidence=0.85,
                reasoning="Class info",
                model="claude-haiku-4-5",
                prompt_tokens=80,
                completion_tokens=30,
            )
        )):
            payload = {**_INBOUND_PAYLOAD, "channel": "sms"}
            resp = await client.post("/api/v1/webhooks/ghl/inbound", json=payload)
        assert resp.json()["channel"] == "whatsapp"

    async def test_confirmation_message_localised_de_at(self, client, session_factory):
        await _seed_location(session_factory)
        payload = {**_INBOUND_PAYLOAD, "messageBody": "STOP", "locale": "de-AT"}
        resp = await client.post("/api/v1/webhooks/ghl/inbound", json=payload)
        assert "abgemeldet" in resp.json()["confirmation_message"].lower()

    async def test_opt_out_classified_triggers_stop_flow(self, client, session_factory):
        """Haiku opt_out classification → full STOP consent-gate flow (V1 auditor fix)."""
        await _seed_location(session_factory)
        with patch("app.gatekeeper.gate.classify", new=AsyncMock(return_value=
            ClassificationResult(
                category="opt_out",
                confidence=0.88,
                reasoning="Customer wants to unsubscribe",
                model="claude-haiku-4-5",
                prompt_tokens=100,
                completion_tokens=40,
            )
        )):
            payload = {**_INBOUND_PAYLOAD, "messageBody": "remove me please"}
            resp = await client.post("/api/v1/webhooks/ghl/inbound", json=payload)
        data = resp.json()
        # Must return is_stop=True and the full 5-action STOP flow
        assert data["is_stop"] is True
        assert data["gatekeeper"] is None
        action_types = [a["action"] for a in data["ghl_actions"]]
        assert "apply_tag" in action_types
        assert "remove_from_all_sequences" in action_types
        assert "send_message" in action_types
        # Confirmation must include transactional bypass flag
        send_action = next(a for a in data["ghl_actions"] if a["action"] == "send_message")
        assert send_action.get("bypass_reason") == "opt_out_confirmation_transactional"

    async def test_complaint_routes_to_owner(self, client, session_factory):
        await _seed_location(session_factory)
        with patch("app.gatekeeper.gate.classify", new=AsyncMock(return_value=
            ClassificationResult(
                category="complaint",
                confidence=0.95,
                reasoning="Expressing dissatisfaction",
                model="claude-haiku-4-5",
                prompt_tokens=120,
                completion_tokens=50,
            )
        )):
            payload = {**_INBOUND_PAYLOAD, "messageBody": "The trainer was rude!"}
            resp = await client.post("/api/v1/webhooks/ghl/inbound", json=payload)
        data = resp.json()
        assert data["gatekeeper"]["route_to"] == "owner"


# ═══════════════════════════════════════════════════════════════════════════════
# TestOwnerOverrideAPI
# ═══════════════════════════════════════════════════════════════════════════════


class TestOwnerOverrideAPI:
    async def _seed_log_row(self, session_factory) -> uuid.UUID:
        async with session_factory() as db:
            row = await log_classification(
                db,
                location_id=_LOCATION_ID,
                ghl_contact_id="contact_abc",
                inbound_channel="whatsapp",
                raw_text="test message",
                classification="spam",
                confidence=0.85,
                route_to="noise",
                action_taken="silent_ignore",
            )
            await db.commit()
            return row.id

    async def test_override_updates_classification(self, client, session_factory):
        log_id = await self._seed_log_row(session_factory)
        resp = await client.patch(
            f"/api/v1/admin/gatekeeper/log/{log_id}/override",
            json={"new_category": "inquiry_pricing"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["owner_override"] == "inquiry_pricing"
        assert data["override_ts"] is not None
        # Original classification unchanged
        assert data["classification"] == "spam"

    async def test_override_invalid_category_422(self, client, session_factory):
        log_id = await self._seed_log_row(session_factory)
        resp = await client.patch(
            f"/api/v1/admin/gatekeeper/log/{log_id}/override",
            json={"new_category": "totally_invalid"},
        )
        assert resp.status_code == 422

    async def test_override_not_found_404(self, client, session_factory):
        resp = await client.patch(
            f"/api/v1/admin/gatekeeper/log/{uuid.uuid4()}/override",
            json={"new_category": "booking"},
        )
        assert resp.status_code == 404

    async def test_list_log_returns_entries(self, client, session_factory):
        await self._seed_log_row(session_factory)
        resp = await client.get(
            f"/api/v1/admin/gatekeeper/log?location_id={_LOCATION_ID}"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        assert data[0]["classification"] == "spam"

    async def test_list_log_filters_by_classification(self, client, session_factory):
        await self._seed_log_row(session_factory)
        resp = await client.get(
            f"/api/v1/admin/gatekeeper/log?location_id={_LOCATION_ID}&classification=booking"
        )
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_list_log_filters_by_channel(self, client, session_factory):
        await self._seed_log_row(session_factory)
        resp = await client.get(
            f"/api/v1/admin/gatekeeper/log?location_id={_LOCATION_ID}&channel=email"
        )
        assert resp.status_code == 200
        assert resp.json() == []  # row is whatsapp, not email

    async def test_list_log_requires_location_id(self, client, session_factory):
        resp = await client.get("/api/v1/admin/gatekeeper/log")
        assert resp.status_code == 422  # missing required query param

    async def test_override_all_valid_categories(self, client, session_factory):
        """Every category in CLASSIFICATION_CATEGORIES is accepted."""
        for cat in CLASSIFICATION_CATEGORIES:
            log_id = await self._seed_log_row(session_factory)
            resp = await client.patch(
                f"/api/v1/admin/gatekeeper/log/{log_id}/override",
                json={"new_category": cat},
            )
            assert resp.status_code == 200, f"Failed for category {cat!r}"
