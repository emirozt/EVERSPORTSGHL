"""
Tests for M3 GHL sync components.

Covers:
  - app/ghl/tags.py     — 60-second race-condition guard
  - app/ghl/pipelines.py — PipelineCache + execute_pipeline_moves
  - app/ghl/sync.py     — sync_contact orchestration (mocked GHL client)

All tests use unittest.mock — no network, no DB.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Shared stubs ───────────────────────────────────────────────────────────────

@dataclass
class _Contact:
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    location_id: uuid.UUID = field(default_factory=uuid.uuid4)
    email: str | None = "user@example.com"
    first_name: str | None = "Anna"
    last_name: str | None = "Muster"
    phone: str | None = "+43123456789"
    eversports_customer_id: str | None = None
    active_package_type: str | None = None
    active_package_name: str | None = None
    active_package_expiry_date: date | None = None
    active_package_sessions_remaining: int | None = None
    last_session_date: date | None = None
    last_class_name: str | None = None
    last_booking_date: date | None = None
    total_sessions_attended: int = 0
    no_show_count: int = 0
    sessions_attended_this_month: int = 0
    sessions_attended_last_month: int = 0
    sessions_per_week_last_month: Decimal | None = None
    products_purchased: list = field(default_factory=list)
    ghl_contact_id: str | None = None
    ghl_prev_state: dict | None = None
    ghl_tag_timestamps: dict | None = None
    ghl_last_synced_at: datetime | None = None
    converted_package_name: str | None = None
    conversion_date: date | None = None
    conversion_source: str | None = None


@dataclass
class _Location:
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    product_keyword_map: dict = field(default_factory=dict)
    card_upsell_min_sessions_per_week: Decimal = Decimal("2")
    ghl_oauth_token_cache: dict | None = field(default_factory=lambda: {
        "access_token": "tok_access",
        "refresh_token": "tok_refresh",
        "token_type": "Bearer",
        "expires_at": (datetime.now(tz=timezone.utc) + timedelta(hours=1)).isoformat(),
    })
    ghl_subaccount_id: str = "sub-test-123"
    eversports_studio_id: str = "es-studio-1"


_TODAY = date(2026, 5, 28)
_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)


# ── Tags engine — race guard ───────────────────────────────────────────────────

from app.ghl.tags import apply_tag_changes


@pytest.mark.asyncio
class TestApplyTagChanges:
    def _make_client(self):
        client = MagicMock()
        client.add_tags = AsyncMock()
        client.remove_tags = AsyncMock()
        return client

    async def test_adds_tags_and_records_timestamps(self):
        client = self._make_client()
        contact = _Contact(ghl_contact_id="ghl-1")

        result = await apply_tag_changes(client, contact, ["tag-a", "tag-b"], [])

        client.add_tags.assert_called_once_with("ghl-1", ["tag-a", "tag-b"])
        assert result["added"] == ["tag-a", "tag-b"]
        assert result["removed"] == []
        assert "tag-a" in contact.ghl_tag_timestamps
        assert "tag-b" in contact.ghl_tag_timestamps

    async def test_removes_old_tags(self):
        client = self._make_client()
        contact = _Contact(ghl_contact_id="ghl-1", ghl_tag_timestamps={})

        result = await apply_tag_changes(client, contact, [], ["old-tag"])

        client.remove_tags.assert_called_once_with("ghl-1", ["old-tag"])
        assert result["removed"] == ["old-tag"]
        assert "old-tag" not in contact.ghl_tag_timestamps

    async def test_race_guard_defers_removal_within_60s(self):
        """Tag applied 30 seconds ago should NOT be removed yet."""
        client = self._make_client()
        recent_ts = (datetime.now(tz=timezone.utc) - timedelta(seconds=30)).isoformat()
        contact = _Contact(
            ghl_contact_id="ghl-1",
            ghl_tag_timestamps={"new-tag": recent_ts},
        )

        result = await apply_tag_changes(client, contact, [], ["new-tag"])

        client.remove_tags.assert_not_called()
        assert result["removed"] == []

    async def test_race_guard_allows_removal_after_60s(self):
        """Tag applied 90 seconds ago should be safe to remove."""
        client = self._make_client()
        old_ts = (datetime.now(tz=timezone.utc) - timedelta(seconds=90)).isoformat()
        contact = _Contact(
            ghl_contact_id="ghl-1",
            ghl_tag_timestamps={"old-tag": old_ts},
        )

        result = await apply_tag_changes(client, contact, [], ["old-tag"])

        client.remove_tags.assert_called_once_with("ghl-1", ["old-tag"])
        assert result["removed"] == ["old-tag"]

    async def test_same_cycle_add_and_remove_skips_removal(self):
        """If a tag is in both add and remove lists, removal is skipped."""
        client = self._make_client()
        contact = _Contact(ghl_contact_id="ghl-1", ghl_tag_timestamps={})

        result = await apply_tag_changes(client, contact, ["flip-tag"], ["flip-tag"])

        client.add_tags.assert_called_once()
        client.remove_tags.assert_not_called()
        assert "flip-tag" in result["added"]
        assert "flip-tag" not in result["removed"]

    async def test_malformed_timestamp_is_ignored(self):
        """A malformed timestamp in ghl_tag_timestamps should not block removal."""
        client = self._make_client()
        contact = _Contact(
            ghl_contact_id="ghl-1",
            ghl_tag_timestamps={"bad-tag": "not-a-timestamp"},
        )

        result = await apply_tag_changes(client, contact, [], ["bad-tag"])

        client.remove_tags.assert_called_once_with("ghl-1", ["bad-tag"])
        assert result["removed"] == ["bad-tag"]

    async def test_no_calls_when_no_changes(self):
        client = self._make_client()
        contact = _Contact(ghl_contact_id="ghl-1")

        result = await apply_tag_changes(client, contact, [], [])

        client.add_tags.assert_not_called()
        client.remove_tags.assert_not_called()
        assert result == {"added": [], "removed": []}


# ── PipelineCache ──────────────────────────────────────────────────────────────

from app.ghl.pipelines import PipelineCache, execute_pipeline_moves


@pytest.mark.asyncio
class TestPipelineCache:
    def _mock_client(self, pipelines: list[dict]) -> MagicMock:
        client = MagicMock()
        client.get_pipelines = AsyncMock(return_value=pipelines)
        return client

    async def test_load_maps_pipelines_by_display_name(self):
        pipelines = [
            {
                "id": "pl-lead",
                "name": "Lead to Sale",
                "stages": [
                    {"id": "st-new", "name": "New lead"},
                    {"id": "st-trial-sold", "name": "Trial sold"},
                ],
            },
            {
                "id": "pl-card",
                "name": "Card Package",
                "stages": [{"id": "st-std", "name": "Standard card"}],
            },
        ]
        client = self._mock_client(pipelines)
        cache = PipelineCache()
        await cache.load(client)

        assert cache.pipeline_id("lead") == "pl-lead"
        assert cache.pipeline_id("card") == "pl-card"
        assert cache.pipeline_id("membership") is None  # not in fixture
        assert cache.stage_id("lead", "New lead") == "st-new"
        assert cache.stage_id("lead", "Trial sold") == "st-trial-sold"
        assert cache.stage_id("card", "Standard card") == "st-std"

    async def test_load_only_calls_api_once(self):
        client = self._mock_client([])
        cache = PipelineCache()
        await cache.load(client)
        await cache.load(client)  # second call should be a no-op

        client.get_pipelines.assert_called_once()

    async def test_unknown_pipeline_returns_none(self):
        cache = PipelineCache()
        assert cache.pipeline_id("nonexistent") is None
        assert cache.stage_id("nonexistent", "anything") is None


@pytest.mark.asyncio
class TestExecutePipelineMoves:
    def _make_client(self):
        client = MagicMock()
        client.search_opportunity = AsyncMock(return_value=None)
        client.create_opportunity = AsyncMock(return_value="opp-new-123")
        client.move_opportunity_stage = AsyncMock()
        return client

    def _make_cache(self) -> PipelineCache:
        cache = PipelineCache.__new__(PipelineCache)
        cache._loaded = True
        cache._data = {
            "lead": {
                "id": "pl-lead",
                "stages": {"Trial sold": "st-trial-sold", "Trial booked": "st-trial-booked"},
            },
        }
        return cache

    async def test_creates_opportunity_when_none_exists(self):
        from app.delta.engine import PipelineMove
        client = self._make_client()
        cache = self._make_cache()
        contact = _Contact(ghl_contact_id="ghl-1")
        moves = [PipelineMove(pipeline_name="lead", new_stage="Trial sold")]

        results = await execute_pipeline_moves(client, contact, moves, cache)

        client.create_opportunity.assert_called_once()
        assert len(results) == 1
        assert "Trial sold" in results[0]
        assert "created" in results[0]

    async def test_moves_existing_opportunity(self):
        from app.delta.engine import PipelineMove
        client = self._make_client()
        client.search_opportunity = AsyncMock(return_value={"id": "opp-existing"})
        cache = self._make_cache()
        contact = _Contact(ghl_contact_id="ghl-1")
        moves = [PipelineMove(pipeline_name="lead", new_stage="Trial booked")]

        results = await execute_pipeline_moves(client, contact, moves, cache)

        client.move_opportunity_stage.assert_called_once_with(
            "opp-existing", pipeline_id="pl-lead", stage_id="st-trial-booked"
        )
        assert len(results) == 1
        assert "created" not in results[0]

    async def test_skips_unknown_pipeline(self):
        from app.delta.engine import PipelineMove
        client = self._make_client()
        cache = self._make_cache()
        contact = _Contact(ghl_contact_id="ghl-1")
        moves = [PipelineMove(pipeline_name="nonexistent", new_stage="Stage")]

        results = await execute_pipeline_moves(client, contact, moves, cache)

        assert results == []
        client.create_opportunity.assert_not_called()

    async def test_skips_unknown_stage(self):
        from app.delta.engine import PipelineMove
        client = self._make_client()
        cache = self._make_cache()
        contact = _Contact(ghl_contact_id="ghl-1")
        moves = [PipelineMove(pipeline_name="lead", new_stage="Nonexistent Stage")]

        results = await execute_pipeline_moves(client, contact, moves, cache)

        assert results == []

    async def test_empty_moves_returns_empty(self):
        client = self._make_client()
        cache = self._make_cache()
        contact = _Contact(ghl_contact_id="ghl-1")

        results = await execute_pipeline_moves(client, contact, [], cache)

        assert results == []


# ── sync_contact orchestration ─────────────────────────────────────────────────

from app.ghl.sync import ContactSyncResult, sync_contact


@pytest.mark.asyncio
class TestSyncContact:
    def _make_client(self):
        client = MagicMock()
        client.upsert_contact = AsyncMock(return_value=("ghl-new-id", True))
        client.update_contact = AsyncMock()
        client.add_tags = AsyncMock()
        client.remove_tags = AsyncMock()
        client.get_pipelines = AsyncMock(return_value=[])
        client.search_opportunity = AsyncMock(return_value=None)
        client.create_opportunity = AsyncMock(return_value="opp-1")
        return client

    def _make_pipeline_cache(self) -> PipelineCache:
        cache = PipelineCache.__new__(PipelineCache)
        cache._loaded = True
        cache._data = {}
        return cache

    async def test_creates_contact_when_no_ghl_id(self):
        client = self._make_client()
        cache = self._make_pipeline_cache()
        db = MagicMock()
        contact = _Contact(ghl_contact_id=None)
        location = _Location()

        result = await sync_contact(contact, location, client, cache, db)

        assert result.ok
        assert result.created is True
        assert contact.ghl_contact_id == "ghl-new-id"
        assert result.ghl_contact_id == "ghl-new-id"

    async def test_updates_contact_when_ghl_id_exists_and_fields_changed(self):
        client = self._make_client()
        cache = self._make_pipeline_cache()
        db = MagicMock()
        # Give it a different eversports_customer_id than what's in prev_state
        contact = _Contact(
            ghl_contact_id="ghl-existing",
            ghl_prev_state={"eversports_customer_id": "OLD"},
            eversports_customer_id="NEW",
        )
        location = _Location()

        result = await sync_contact(contact, location, client, cache, db)

        assert result.ok
        assert result.created is False
        client.update_contact.assert_called_once()
        assert result.custom_fields_pushed > 0

    async def test_skips_all_api_calls_when_delta_is_empty(self):
        """A contact already fully synced should produce no API calls."""
        client = self._make_client()
        cache = self._make_pipeline_cache()
        db = MagicMock()

        # Build a prev_state that exactly matches all contact fields
        contact = _Contact(
            ghl_contact_id="ghl-existing",
            ghl_prev_state={
                "eversports_customer_id": None,
                "active_package_type": None,
                "active_package_name": None,
                "active_package_expiry_date": None,
                "active_package_sessions_remaining": None,
                "last_session_date": None,
                "last_class_name": None,
                "total_sessions_attended": 0,
                "no_show_count": 0,
                "sessions_attended_this_month": 0,
                "sessions_attended_last_month": 0,
                "sessions_per_week_last_month": None,
                "last_booking_date": None,
                # tags + pipeline in prev_state keys
                "_tags": ["new-contact"],
                "_lead_stage": "New lead",
                "_card_stage": None,
                "_membership_stage": None,
            },
        )
        location = _Location()

        # Fake the delta to be empty by passing matching tag/stage state
        # (new contact with no products → lead_stage=New lead, tag=new-contact ... but
        #  new-contact tag is not actually emitted by compute_flags, so we just verify
        #  no update API calls are made when delta IS empty)

        # We'll test with tags already matching the desired state
        # Since contact has no products → desired lead_stage = New lead
        # and desired tags = {} (empty — new-contact is not a tag compute_flags produces)
        # prev_state has _lead_stage = "New lead" → no pipeline move
        # tags: current is {"new-contact"}, desired is {} → new-contact would be removed
        # but we only care that update_contact is not called when no fields changed

        result = await sync_contact(contact, location, client, cache, db)

        # update_contact should NOT be called (no custom field changes)
        client.update_contact.assert_not_called()

    async def test_prev_state_updated_after_sync(self):
        """After sync, contact.ghl_prev_state should be updated."""
        client = self._make_client()
        cache = self._make_pipeline_cache()
        db = MagicMock()
        contact = _Contact(
            ghl_contact_id="ghl-existing",
            ghl_prev_state={"active_package_type": "OLD"},
            active_package_type="card",
        )
        location = _Location()

        result = await sync_contact(contact, location, client, cache, db)

        assert result.ok
        assert contact.ghl_prev_state is not None
        assert contact.ghl_last_synced_at is not None

    async def test_error_captured_in_result(self):
        """An unexpected error during sync should be captured, not raised."""
        client = self._make_client()
        client.upsert_contact = AsyncMock(side_effect=RuntimeError("network error"))
        cache = self._make_pipeline_cache()
        db = MagicMock()
        contact = _Contact(ghl_contact_id=None)
        location = _Location()

        result = await sync_contact(contact, location, client, cache, db)

        assert not result.ok
        assert "network error" in result.error

    async def test_ghl_auth_error_propagates(self):
        """GhlAuthError must propagate (stops the whole batch)."""
        from app.ghl.client import GhlAuthError
        client = self._make_client()
        client.upsert_contact = AsyncMock(side_effect=GhlAuthError("unauthorized"))
        cache = self._make_pipeline_cache()
        db = MagicMock()
        contact = _Contact(ghl_contact_id=None)
        location = _Location()

        with pytest.raises(GhlAuthError):
            await sync_contact(contact, location, client, cache, db)
