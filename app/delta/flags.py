"""
Contact flags — computes GHL tags and pipeline stages from a Contact's current state.

This is pure business logic: given a Contact row (with all derived fields populated),
return which tags should be applied, which should be removed, and which pipeline
stages to move to.

All logic is derived from:
  - requirements_v2/00_master_overview.md §GHL Tags
  - requirements_v2/03_ghl_pipelines.md §Stage transition logic

Functions are synchronous and dependency-free (no DB, no HTTP).

Design contract:
  ``compute_flags()`` is the single entry point.  It returns a ``ContactFlags``
  dataclass whose fields are consumed by ``delta.engine`` and ``ghl.sync``.

  The caller is responsible for ensuring all relevant Contact fields are up-to-date
  before calling (i.e. the bootstrap/scraper ingest has already run).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from app.delta.classifiers import (
    active_package_type_from_products,
    classify_product,
    is_card,
    is_membership,
    is_trial,
)

if TYPE_CHECKING:
    from app.db.models.contacts import Contact
    from app.db.models.location import Location


# ── Output dataclass ───────────────────────────────────────────────────────────

@dataclass
class ContactFlags:
    """
    Result of ``compute_flags()``.

    ``tags_desired`` is the FULL set of tags this contact should have after sync.
    The delta engine diffs this against the contact's current GHL tags to produce
    ``tags_to_add`` and ``tags_to_remove``.

    ``lead_stage``, ``card_stage``, ``membership_stage`` are the pipeline stage
    names this contact should be in.  ``None`` means "do not touch that pipeline"
    (contact doesn't qualify for it).
    """

    # Full desired tag set
    tags_desired: set[str] = field(default_factory=set)

    # Pipeline stage names (None = not applicable for this contact)
    lead_stage: str | None = None
    card_stage: str | None = None
    membership_stage: str | None = None


# ── Stage name constants (must match GHL pipeline stage names exactly) ─────────

class LeadStage:
    NEW_LEAD = "New lead"
    TRIAL_SOLD = "Trial sold"
    TRIAL_BOOKED = "Trial booked"
    CONVERTED_CARD = "Converted (card)"
    CONVERTED_MEMBERSHIP = "Converted (membership)"
    LOST = "Lost"


class CardStage:
    STANDARD = "Standard card"
    LOW_ATTENDANCE = "Low attendance warning"
    MEMBERSHIP_READY = "Membership ready"
    CONVERTED = "Converted"
    CHURNED = "Churned"


class MembershipStage:
    ACTIVE = "Active"
    AT_RISK = "At risk"
    RENEWAL_DUE = "Renewal due"
    RENEWED = "Renewed"
    CHURNED = "Churned"


# ── Tag name constants ─────────────────────────────────────────────────────────
# Must match the tag names in requirements_v2/00_master_overview.md §GHL Tags.

class Tag:
    # Lifecycle
    NEW_CONTACT = "new-contact"
    TRIAL_ACTIVE = "trial-active"
    TRIAL_LAST_SESSION = "trial-last-session"
    TRIAL_PURCHASE_DETECTED = "trial-purchase-detected"
    TRIAL_CONVERTED = "trial-converted"
    TRIAL_NOT_CONVERTED = "trial-not-converted"
    TRIAL_FOLLOW_UP_ACTIVE = "trial-follow-up-active"
    # Product states
    CARD_ACTIVE = "card-active"
    MEMBERSHIP_ACTIVE = "membership-active"
    # Attendance health
    LOW_ATTENDANCE = "low-attendance"
    MEMBERSHIP_READY = "membership-ready"
    AT_RISK = "at-risk"
    RENEWAL_DUE = "renewal-due"
    RENEWED = "renewed"
    CHURNED = "churned"
    LAPSED = "lapsed"
    # Consent / communication
    OPTED_OUT = "opted-out"
    # Chatbot
    CHATBOT_ACTIVE = "chatbot-active"
    CHATBOT_SALE_INITIATED = "chatbot-sale-initiated"
    CHATBOT_CONVERTED = "chatbot-converted"
    CHATBOT_HANDOFF = "chatbot-handoff"
    # Writeback
    RESCHEDULE_IN_FLIGHT = "reschedule-in-flight"
    CANCEL_IN_FLIGHT = "cancel-in-flight"
    WRITEBACK_FAILED = "writeback-failed"


def compute_flags(
    contact: "Contact",
    location: "Location",
    *,
    today: date | None = None,
    current_ghl_tags: set[str] | None = None,
) -> ContactFlags:
    """
    Compute the desired GHL tags and pipeline stages for a contact.

    Args:
        contact: SQLAlchemy Contact row (all derived fields must be populated).
        location: SQLAlchemy Location row (for per-location thresholds).
        today: Override today's date (for testing). Defaults to ``date.today()``.
        current_ghl_tags: The tag set currently on the GHL contact (from the last
            sync snapshot or a live read).  Required to correctly evaluate tags
            that are set by external workflows (UC01, UC04) and live in
            ``_NEVER_REMOVE_TAGS`` — these tags are never added by ``compute_flags``
            itself, but their *presence* must influence pipeline stage decisions.
            Pass ``None`` (or omit) on first sync; treated as empty set.

    Returns:
        ``ContactFlags`` with the desired tag set and pipeline stage names.
    """
    today = today or date.today()
    ghl_tags: set[str] = current_ghl_tags or set()
    flags = ContactFlags()
    tags = flags.tags_desired

    keyword_map: dict = location.product_keyword_map or {}
    products: list = contact.products_purchased or []

    # ── Derive active package type ─────────────────────────────────────────────
    active_type = active_package_type_from_products(products, keyword_map)

    # If the contact has an explicit active_package_type in DB, trust that over
    # re-deriving from products_purchased (the scraper may set it directly).
    if contact.active_package_type:
        active_type = contact.active_package_type

    active_name: str = contact.active_package_name or ""
    expiry: date | None = contact.active_package_expiry_date
    sessions_remaining: int | None = contact.active_package_sessions_remaining

    # ── Determine product history ──────────────────────────────────────────────
    has_any_non_trial = any(
        not is_trial(p.get("name", "") if isinstance(p, dict) else str(p), keyword_map)
        for p in products
    )
    has_trial_ever = any(
        is_trial(p.get("name", "") if isinstance(p, dict) else str(p), keyword_map)
        for p in products
    )

    # ── Tags: Trial ────────────────────────────────────────────────────────────
    if active_type == "trial":
        tags.add(Tag.TRIAL_ACTIVE)

        # trial-last-session: all sessions on trial have been used
        if (
            sessions_remaining is not None
            and sessions_remaining <= 0
        ):
            tags.add(Tag.TRIAL_LAST_SESSION)

    elif has_trial_ever and has_any_non_trial:
        # Had a trial and now has a non-trial product → converted
        tags.add(Tag.TRIAL_CONVERTED)

        # Detect which non-trial product was purchased for the purchase-detected flow
        non_trial_products = [
            p for p in products
            if not is_trial(p.get("name", "") if isinstance(p, dict) else str(p), keyword_map)
        ]
        if non_trial_products:
            tags.add(Tag.TRIAL_PURCHASE_DETECTED)

    # ── Tags: Card ────────────────────────────────────────────────────────────
    if active_type == "card":
        tags.add(Tag.CARD_ACTIVE)

        # low-attendance: no booking in 14 days while sessions remain
        last_booking: date | None = contact.last_booking_date
        sessions_left = sessions_remaining or 0
        if (
            sessions_left > 0
            and last_booking is not None
            and (today - last_booking).days > 14
        ):
            tags.add(Tag.LOW_ATTENDANCE)

        # membership-ready: high-frequency card customer
        spw: Decimal | None = contact.sessions_per_week_last_month
        threshold: Decimal = Decimal(str(location.card_upsell_min_sessions_per_week or 2))
        if spw is not None and spw > threshold:
            tags.add(Tag.MEMBERSHIP_READY)

    # ── Tags: Membership ──────────────────────────────────────────────────────
    if active_type == "membership":
        tags.add(Tag.MEMBERSHIP_ACTIVE)

        # at-risk: no session in 14 days
        last_session: date | None = contact.last_session_date
        if last_session is not None and (today - last_session).days > 14:
            tags.add(Tag.AT_RISK)

        # at-risk: attendance drop 50%+
        this_month = contact.sessions_attended_this_month or 0
        last_month = contact.sessions_attended_last_month or 0
        if last_month > 0 and this_month < (last_month * 0.5):
            tags.add(Tag.AT_RISK)

        # renewal-due: expiry within 14 days
        if expiry is not None and expiry <= today + timedelta(days=14):
            tags.add(Tag.RENEWAL_DUE)

    # ── Tags: Churn ───────────────────────────────────────────────────────────
    if expiry is not None and expiry < today and active_type in ("card", "membership"):
        # Package expired — check if it's churned
        # (no new package detected means active_type would still be old type)
        # This is a conservative approximation; full churn detection needs history comparison
        tags.add(Tag.CHURNED)
        # Remove active tags if churned
        tags.discard(Tag.CARD_ACTIVE)
        tags.discard(Tag.MEMBERSHIP_ACTIVE)

    # ── Tags: Lapsed ─────────────────────────────────────────────────────────
    last_booking = contact.last_booking_date
    if last_booking is not None and (today - last_booking).days > 30:
        tags.add(Tag.LAPSED)

    # ── Pipeline: Lead to Sale ────────────────────────────────────────────────
    # TRIAL_NOT_CONVERTED is set by the UC01 follow-up workflow and lives in
    # _NEVER_REMOVE_TAGS — it is never emitted by compute_flags itself.  We must
    # check current_ghl_tags (the live GHL state) rather than tags_desired.
    if Tag.TRIAL_NOT_CONVERTED in ghl_tags:
        flags.lead_stage = LeadStage.LOST
    elif Tag.TRIAL_CONVERTED in tags:
        if active_type == "membership" or is_membership(active_name, keyword_map):
            flags.lead_stage = LeadStage.CONVERTED_MEMBERSHIP
        else:
            flags.lead_stage = LeadStage.CONVERTED_CARD
    elif Tag.TRIAL_ACTIVE in tags:
        # Check if they've used their trial
        if contact.total_sessions_attended > 0:
            flags.lead_stage = LeadStage.TRIAL_BOOKED
        else:
            flags.lead_stage = LeadStage.TRIAL_SOLD
    elif active_type == "trial":
        flags.lead_stage = LeadStage.TRIAL_SOLD
    elif not products:
        flags.lead_stage = LeadStage.NEW_LEAD
    # else: contact has non-trial products but no trial history — skip lead pipeline

    # ── Pipeline: Card ────────────────────────────────────────────────────────
    if active_type == "card" and Tag.CHURNED not in tags:
        if Tag.MEMBERSHIP_READY in tags:
            flags.card_stage = CardStage.MEMBERSHIP_READY
        elif Tag.LOW_ATTENDANCE in tags:
            flags.card_stage = CardStage.LOW_ATTENDANCE
        else:
            flags.card_stage = CardStage.STANDARD
    elif active_type == "card" and Tag.CHURNED in tags:
        flags.card_stage = CardStage.CHURNED
    elif active_type == "membership" and has_any_non_trial:
        # Previously had a card and converted to membership
        flags.card_stage = CardStage.CONVERTED

    # ── Pipeline: Membership ──────────────────────────────────────────────────
    if active_type == "membership" and Tag.CHURNED not in tags:
        if Tag.RENEWAL_DUE in tags:
            flags.membership_stage = MembershipStage.RENEWAL_DUE
        elif Tag.AT_RISK in tags:
            flags.membership_stage = MembershipStage.AT_RISK
        else:
            flags.membership_stage = MembershipStage.ACTIVE
    elif active_type == "membership" and Tag.CHURNED in tags:
        flags.membership_stage = MembershipStage.CHURNED

    return flags
