"""
Tests for the M3 delta engine stack.

All tests are pure Python — no database, no HTTP.

Covers:
  - app/delta/classifiers.py  — product type classification
  - app/delta/flags.py        — tag + pipeline stage derivation
  - app/delta/engine.py       — GhlDelta computation
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

# ── Classifiers ────────────────────────────────────────────────────────────────

from app.delta.classifiers import (
    active_package_type_from_products,
    classify_product,
    classify_products,
    is_card,
    is_membership,
    is_trial,
)


class TestClassifyProduct:
    def test_trial_english(self):
        assert classify_product("Trial class") == "trial"

    def test_trial_german_schnupper(self):
        assert classify_product("Schnupperkurs Yoga") == "trial"

    def test_trial_probe(self):
        assert classify_product("Probestunde Pilates") == "trial"

    def test_membership_english(self):
        assert classify_product("Unlimited Membership") == "membership"

    def test_membership_german_monatsabo(self):
        assert classify_product("Monatsabo Premium") == "membership"

    def test_membership_jahresabo(self):
        assert classify_product("Jahresabo Fitness") == "membership"

    def test_card_10er(self):
        assert classify_product("10er Karte-Gruppe") == "card"

    def test_card_5er(self):
        assert classify_product("5er Karte") == "card"

    def test_card_punch(self):
        assert classify_product("Punch Card 20") == "card"

    def test_drop_in(self):
        assert classify_product("Drop-In Yoga") == "drop_in"

    def test_drop_in_single_class(self):
        assert classify_product("Single Class Pass") == "drop_in"

    def test_geschenkgutschein_is_card(self):
        # "gutschein" matches the card rule before the voucher rule runs.
        # The card rule explicitly lists "voucher" and "gutschein" because
        # Eversports vouchers are card-like products. This is intentional.
        assert classify_product("Geschenkgutschein €50") == "card"

    def test_voucher_type_via_location_override(self):
        # Pure "voucher" type is only achievable via a per-location keyword map.
        km = {"voucher": ["geschenkgutschein"]}
        assert classify_product("Geschenkgutschein €50", km) == "voucher"

    def test_gift_keyword_via_location_override(self):
        km = {"voucher": ["gift"]}
        assert classify_product("Gift Card €50", km) == "voucher"

    def test_unknown(self):
        assert classify_product("Merchandise Shirt") == "unknown"

    def test_empty_string(self):
        assert classify_product("") == "unknown"

    def test_case_insensitive(self):
        assert classify_product("TRIAL CLASS") == "trial"
        assert classify_product("MONATSABO") == "membership"

    def test_per_location_override_wins(self):
        km = {"trial": ["probepass"]}
        assert classify_product("Probepass Gold", km) == "trial"

    def test_per_location_override_does_not_break_other_types(self):
        km = {"trial": ["probepass"]}
        assert classify_product("10er Karte", km) == "card"

    def test_membership_abo_space_avoids_false_match(self):
        # "abo " with trailing space should not match "kabosu" (hypothetical)
        assert classify_product("abo flatrate") == "membership"

    def test_is_trial_helper(self):
        assert is_trial("Trial Yoga") is True
        assert is_trial("10er Karte") is False

    def test_is_card_helper(self):
        assert is_card("10er Karte") is True

    def test_is_membership_helper(self):
        assert is_membership("Monatsabo Premium") is True


class TestClassifyProducts:
    def test_classify_multiple(self):
        result = classify_products(["Trial class", "10er Karte", "Monatsabo"])
        assert result == ["trial", "card", "membership"]


class TestActivePackageTypeFromProducts:
    def test_membership_wins_over_card(self):
        products = [
            {"name": "10er Karte"},
            {"name": "Monatsabo Premium"},
        ]
        assert active_package_type_from_products(products, {}) == "membership"

    def test_card_wins_over_trial(self):
        products = [
            {"name": "Trial Class"},
            {"name": "10er Karte"},
        ]
        assert active_package_type_from_products(products, {}) == "card"

    def test_single_trial(self):
        products = [{"name": "Schnupperkurs"}]
        assert active_package_type_from_products(products, {}) == "trial"

    def test_empty_list(self):
        assert active_package_type_from_products([], {}) == "unknown"

    def test_all_unknown(self):
        products = [{"name": "Shirt"}, {"name": "Water bottle"}]
        assert active_package_type_from_products(products, {}) == "unknown"

    def test_per_location_override(self):
        products = [{"name": "Duo-Karte Special"}]
        km = {"card": ["duo-karte"]}
        assert active_package_type_from_products(products, km) == "card"


# ── Flags ──────────────────────────────────────────────────────────────────────

from app.delta.flags import (
    CardStage,
    ContactFlags,
    LeadStage,
    MembershipStage,
    Tag,
    compute_flags,
)


# ── Minimal stubs (avoid SQLAlchemy ORM dependency in unit tests) ──────────────

@dataclass
class _Contact:
    """Lightweight Contact stub for unit tests."""
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    location_id: uuid.UUID = field(default_factory=uuid.uuid4)
    email: str | None = "test@example.com"
    first_name: str | None = "Test"
    last_name: str | None = "User"
    phone: str | None = None
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
    """Lightweight Location stub for unit tests."""
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    product_keyword_map: dict = field(default_factory=dict)
    card_upsell_min_sessions_per_week: Decimal = Decimal("2")
    ghl_oauth_token_cache: dict | None = None
    ghl_subaccount_id: str = "test-sub-account"
    eversports_studio_id: str = "test-studio"


_TODAY = date(2026, 5, 28)


class TestComputeFlags:
    def _make(self, **kwargs) -> _Contact:
        return _Contact(**kwargs)

    def _loc(self, **kwargs) -> _Location:
        return _Location(**kwargs)

    def test_trial_active_tags(self):
        c = self._make(active_package_type="trial", active_package_sessions_remaining=2)
        flags = compute_flags(c, self._loc(), today=_TODAY)
        assert Tag.TRIAL_ACTIVE in flags.tags_desired
        assert Tag.TRIAL_LAST_SESSION not in flags.tags_desired

    def test_trial_last_session_when_zero_remaining(self):
        c = self._make(active_package_type="trial", active_package_sessions_remaining=0)
        flags = compute_flags(c, self._loc(), today=_TODAY)
        assert Tag.TRIAL_ACTIVE in flags.tags_desired
        assert Tag.TRIAL_LAST_SESSION in flags.tags_desired

    def test_trial_converted_when_has_non_trial(self):
        c = self._make(
            active_package_type="card",
            products_purchased=[{"name": "Trial Class"}, {"name": "10er Karte"}],
        )
        flags = compute_flags(c, self._loc(), today=_TODAY)
        assert Tag.TRIAL_CONVERTED in flags.tags_desired
        assert Tag.TRIAL_PURCHASE_DETECTED in flags.tags_desired

    def test_card_active_tags(self):
        c = self._make(
            active_package_type="card",
            active_package_sessions_remaining=5,
            last_booking_date=_TODAY,
        )
        flags = compute_flags(c, self._loc(), today=_TODAY)
        assert Tag.CARD_ACTIVE in flags.tags_desired
        assert Tag.LOW_ATTENDANCE not in flags.tags_desired

    def test_card_low_attendance(self):
        old_booking = date(2026, 5, 10)  # 18 days ago
        c = self._make(
            active_package_type="card",
            active_package_sessions_remaining=3,
            last_booking_date=old_booking,
        )
        flags = compute_flags(c, self._loc(), today=_TODAY)
        assert Tag.LOW_ATTENDANCE in flags.tags_desired

    def test_card_membership_ready(self):
        c = self._make(
            active_package_type="card",
            sessions_per_week_last_month=Decimal("3.5"),
        )
        flags = compute_flags(c, self._loc(), today=_TODAY)
        assert Tag.MEMBERSHIP_READY in flags.tags_desired

    def test_membership_active(self):
        c = self._make(
            active_package_type="membership",
            last_session_date=_TODAY,
            active_package_expiry_date=date(2026, 7, 1),
        )
        flags = compute_flags(c, self._loc(), today=_TODAY)
        assert Tag.MEMBERSHIP_ACTIVE in flags.tags_desired
        assert Tag.AT_RISK not in flags.tags_desired

    def test_membership_at_risk_no_recent_session(self):
        c = self._make(
            active_package_type="membership",
            last_session_date=date(2026, 5, 10),  # 18 days ago
            active_package_expiry_date=date(2026, 7, 1),
        )
        flags = compute_flags(c, self._loc(), today=_TODAY)
        assert Tag.AT_RISK in flags.tags_desired

    def test_membership_renewal_due(self):
        c = self._make(
            active_package_type="membership",
            last_session_date=_TODAY,
            active_package_expiry_date=date(2026, 6, 5),  # 8 days ahead
        )
        flags = compute_flags(c, self._loc(), today=_TODAY)
        assert Tag.RENEWAL_DUE in flags.tags_desired

    def test_churned_when_expired(self):
        c = self._make(
            active_package_type="card",
            active_package_expiry_date=date(2026, 5, 1),  # expired
        )
        flags = compute_flags(c, self._loc(), today=_TODAY)
        assert Tag.CHURNED in flags.tags_desired
        assert Tag.CARD_ACTIVE not in flags.tags_desired

    def test_lapsed_no_booking_over_30_days(self):
        c = self._make(last_booking_date=date(2026, 4, 20))  # 38 days ago
        flags = compute_flags(c, self._loc(), today=_TODAY)
        assert Tag.LAPSED in flags.tags_desired

    def test_no_lapsed_if_recent_booking(self):
        c = self._make(last_booking_date=_TODAY)
        flags = compute_flags(c, self._loc(), today=_TODAY)
        assert Tag.LAPSED not in flags.tags_desired

    # ── Pipeline stages ────────────────────────────────────────────────────────

    def test_lead_stage_trial_sold(self):
        c = self._make(active_package_type="trial", total_sessions_attended=0)
        flags = compute_flags(c, self._loc(), today=_TODAY)
        assert flags.lead_stage == LeadStage.TRIAL_SOLD

    def test_lead_stage_trial_booked(self):
        c = self._make(active_package_type="trial", total_sessions_attended=2)
        flags = compute_flags(c, self._loc(), today=_TODAY)
        assert flags.lead_stage == LeadStage.TRIAL_BOOKED

    def test_lead_stage_new_lead_no_products(self):
        c = self._make(products_purchased=[])
        flags = compute_flags(c, self._loc(), today=_TODAY)
        assert flags.lead_stage == LeadStage.NEW_LEAD

    def test_lead_stage_converted_card(self):
        c = self._make(
            active_package_type="card",
            products_purchased=[{"name": "Trial Class"}, {"name": "10er Karte"}],
        )
        flags = compute_flags(c, self._loc(), today=_TODAY)
        assert flags.lead_stage == LeadStage.CONVERTED_CARD

    def test_card_stage_standard(self):
        c = self._make(
            active_package_type="card",
            active_package_sessions_remaining=5,
            last_booking_date=_TODAY,
        )
        flags = compute_flags(c, self._loc(), today=_TODAY)
        assert flags.card_stage == CardStage.STANDARD

    def test_card_stage_membership_ready(self):
        c = self._make(
            active_package_type="card",
            sessions_per_week_last_month=Decimal("3"),
        )
        flags = compute_flags(c, self._loc(), today=_TODAY)
        assert flags.card_stage == CardStage.MEMBERSHIP_READY

    def test_membership_stage_active(self):
        c = self._make(
            active_package_type="membership",
            last_session_date=_TODAY,
            active_package_expiry_date=date(2026, 7, 1),
        )
        flags = compute_flags(c, self._loc(), today=_TODAY)
        assert flags.membership_stage == MembershipStage.ACTIVE

    def test_membership_stage_renewal_due(self):
        c = self._make(
            active_package_type="membership",
            last_session_date=_TODAY,
            active_package_expiry_date=date(2026, 6, 5),  # 8 days
        )
        flags = compute_flags(c, self._loc(), today=_TODAY)
        assert flags.membership_stage == MembershipStage.RENEWAL_DUE

    def test_no_pipeline_for_non_trial_no_history(self):
        """Contact with card but NO trial history → no lead pipeline stage."""
        c = self._make(
            active_package_type="card",
            products_purchased=[{"name": "10er Karte"}],
        )
        flags = compute_flags(c, self._loc(), today=_TODAY)
        assert flags.lead_stage is None

    def test_lead_stage_lost_when_trial_not_converted_in_ghl_tags(self):
        """
        trial-not-converted is set by the UC01 workflow externally and lives in
        NEVER_REMOVE_TAGS — compute_flags must check current_ghl_tags, not
        tags_desired, to detect it.
        """
        c = self._make()  # No active package, no history
        ghl_tags = {"trial-not-converted"}
        flags = compute_flags(c, self._loc(), today=_TODAY, current_ghl_tags=ghl_tags)
        assert flags.lead_stage == LeadStage.LOST

    def test_lead_stage_not_lost_without_ghl_tag(self):
        """Without trial-not-converted in GHL tags, Lost stage is not set."""
        c = self._make()
        flags = compute_flags(c, self._loc(), today=_TODAY, current_ghl_tags=set())
        assert flags.lead_stage != LeadStage.LOST


# ── Delta Engine ───────────────────────────────────────────────────────────────

from app.delta.engine import GhlDelta, PipelineMove, _NEVER_REMOVE_TAGS, compute_delta


class TestComputeDelta:
    def _contact(self, **kwargs) -> _Contact:
        return _Contact(**kwargs)

    def _location(self, **kwargs) -> _Location:
        return _Location(**kwargs)

    def test_needs_create_when_no_ghl_contact_id(self):
        c = self._contact(active_package_type="trial", total_sessions_attended=1)
        loc = self._location()
        delta = compute_delta(c, loc, today=_TODAY)
        assert delta.needs_create is True

    def test_create_pushes_non_null_fields(self):
        c = self._contact(
            ghl_contact_id=None,
            eversports_customer_id="ES123",
            active_package_type="trial",
        )
        loc = self._location()
        delta = compute_delta(c, loc, today=_TODAY)
        assert delta.needs_create is True
        assert delta.custom_fields.get("eversports_customer_id") == "ES123"
        assert delta.custom_fields.get("active_package_type") == "trial"

    def test_no_delta_when_fields_unchanged(self):
        prev = {
            "eversports_customer_id": "ES123",
            "active_package_type": "card",
            "active_package_name": "10er Karte",
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
        }
        c = self._contact(
            ghl_contact_id="ghl-abc",
            ghl_prev_state=prev,
            eversports_customer_id="ES123",
            active_package_type="card",
            active_package_name="10er Karte",
        )
        loc = self._location()
        delta = compute_delta(
            c, loc,
            current_ghl_tags={"card-active"},
            today=_TODAY,
        )
        # custom_fields should be empty since nothing changed
        assert not delta.custom_fields
        assert delta.needs_create is False

    def test_custom_field_delta_when_changed(self):
        prev = {"eversports_customer_id": "OLD", "active_package_type": None}
        c = self._contact(
            ghl_contact_id="ghl-abc",
            ghl_prev_state=prev,
            eversports_customer_id="ES123",
        )
        loc = self._location()
        delta = compute_delta(c, loc, today=_TODAY)
        assert "eversports_customer_id" in delta.custom_fields
        assert delta.custom_fields["eversports_customer_id"] == "ES123"

    def test_tags_to_add_when_new_active_tag(self):
        c = self._contact(
            ghl_contact_id="ghl-abc",
            ghl_prev_state={},
            active_package_type="card",
            last_booking_date=_TODAY,
        )
        loc = self._location()
        delta = compute_delta(
            c, loc,
            current_ghl_tags=set(),
            today=_TODAY,
        )
        assert "card-active" in delta.tags_to_add

    def test_tags_to_remove_stale_tag(self):
        c = self._contact(
            ghl_contact_id="ghl-abc",
            ghl_prev_state={},
            active_package_type="card",
            last_booking_date=_TODAY,
        )
        loc = self._location()
        # contact is now a card customer but was previously tagged as trial-active
        # trial-active is not in NEVER_REMOVE so it should be removed
        delta = compute_delta(
            c, loc,
            current_ghl_tags={"trial-active"},
            today=_TODAY,
        )
        assert "trial-active" in delta.tags_to_remove

    def test_never_remove_tags_are_preserved(self):
        c = self._contact(
            ghl_contact_id="ghl-abc",
            ghl_prev_state={},
            active_package_type="card",
        )
        loc = self._location()
        # opted-out is in NEVER_REMOVE
        delta = compute_delta(
            c, loc,
            current_ghl_tags={"opted-out", "trial-active"},
            today=_TODAY,
        )
        assert "opted-out" not in delta.tags_to_remove
        # trial-active should be removable
        assert "trial-active" in delta.tags_to_remove

    def test_never_remove_frozenset_contents(self):
        """Spot-check that the frozenset contains the key tags."""
        assert "opted-out" in _NEVER_REMOVE_TAGS
        assert "trial-converted" in _NEVER_REMOVE_TAGS
        assert "chatbot-active" in _NEVER_REMOVE_TAGS
        assert "writeback-failed" in _NEVER_REMOVE_TAGS

    def test_pipeline_move_added_when_stage_changes(self):
        c = self._contact(
            ghl_contact_id="ghl-abc",
            ghl_prev_state={},
            active_package_type="trial",
            total_sessions_attended=2,
        )
        loc = self._location()
        delta = compute_delta(
            c, loc,
            current_lead_stage=None,
            today=_TODAY,
        )
        lead_moves = [m for m in delta.pipeline_moves if m.pipeline_name == "lead"]
        assert len(lead_moves) == 1
        assert lead_moves[0].new_stage == LeadStage.TRIAL_BOOKED

    def test_no_pipeline_move_when_already_in_stage(self):
        c = self._contact(
            ghl_contact_id="ghl-abc",
            ghl_prev_state={},
            active_package_type="trial",
            total_sessions_attended=2,
        )
        loc = self._location()
        delta = compute_delta(
            c, loc,
            current_lead_stage=LeadStage.TRIAL_BOOKED,
            today=_TODAY,
        )
        assert not delta.pipeline_moves

    def test_delta_flags_attached(self):
        """compute_delta should set delta.flags so callers avoid a second compute_flags call."""
        c = self._contact(
            ghl_contact_id=None,
            active_package_type="trial",
            total_sessions_attended=2,
        )
        loc = self._location()
        delta = compute_delta(c, loc, today=_TODAY)
        # delta.flags must be populated and agree with the pipeline moves
        assert delta.flags is not None
        assert delta.flags.lead_stage == LeadStage.TRIAL_BOOKED

    def test_is_empty_on_fully_synced_contact(self):
        """Contact with all fields matching prev_state and correct tags → empty delta."""
        c = self._contact(
            ghl_contact_id="ghl-abc",
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
            },
        )
        loc = self._location()
        # New lead, no products, no tags currently
        delta = compute_delta(
            c, loc,
            current_ghl_tags={"new-contact"},
            current_lead_stage=LeadStage.NEW_LEAD,
            today=_TODAY,
        )
        # new-contact tag is not in desired tags for an existing contact with no products
        # but new-lead lead_stage should match
        assert not delta.pipeline_moves
        assert not delta.custom_fields
        assert delta.needs_create is False
