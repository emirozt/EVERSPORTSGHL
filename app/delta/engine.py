"""
Delta engine — computes the GHL change set for a contact.

Given:
  - The contact's current Postgres state (freshly updated by scraper/bootstrap)
  - The ``prev_state`` snapshot of what was last pushed to GHL (stored on the contact)
  - The desired tags/pipeline stages from ``flags.compute_flags()``

Produces a ``GhlDelta`` describing exactly what needs to change in GHL:
  - Custom fields that differ from prev_state
  - Tags to add (desired ∖ current)
  - Tags to remove (current ∖ desired), subject to 60-second race-condition guard
  - Pipeline stage moves

The engine is pure Python — no DB, no HTTP.  All I/O is handled by ``ghl.sync``.

References:
  - requirements_v2/00_master_overview.md §GHL Data Model
  - requirements_v2/03_ghl_pipelines.md
  - app/delta/flags.py — business rules for tags + pipeline stages
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, TYPE_CHECKING

from app.delta.flags import ContactFlags, compute_flags

if TYPE_CHECKING:
    from app.db.models.contacts import Contact
    from app.db.models.location import Location


# ── Output dataclasses ─────────────────────────────────────────────────────────

@dataclass
class PipelineMove:
    """A requested pipeline stage move."""
    pipeline_name: str   # "lead" | "card" | "membership"
    new_stage: str       # Must match GHL stage name exactly


# Tags that should NEVER be removed by the delta engine (only removed by
# specific use case workflows or manually by studio staff).
# Defined here (before compute_delta) so it is visible to readers top-to-bottom.
_NEVER_REMOVE_TAGS: frozenset[str] = frozenset({
    "trial-not-converted",  # Permanent failure marker
    "trial-converted",      # Permanent achievement marker
    "trial-follow-up-active",  # Managed by UC01 workflow
    "chatbot-active",          # Managed by UC04 workflow
    "chatbot-sale-initiated",  # Managed by UC04 workflow
    "chatbot-converted",       # Permanent achievement marker
    "chatbot-handoff",         # Managed by UC04 workflow
    "opted-out",               # Only removed manually by studio staff
    "reschedule-in-flight",    # Managed by UC05 writeback
    "cancel-in-flight",        # Managed by UC05 writeback
    "writeback-failed",        # Managed by operator resolution
})


@dataclass
class GhlDelta:
    """
    The full set of changes to apply to GHL for one contact.

    All fields default to empty — callers only act on non-empty collections.

    ``flags`` is populated by ``compute_delta`` and carries the ``ContactFlags``
    used to derive tags and pipeline stages.  Consumers (e.g. ``ghl.sync``) can
    read it to build the new ``ghl_prev_state`` without calling ``compute_flags``
    a second time.
    """
    # Custom field key → new value (only fields that changed vs prev_state)
    custom_fields: dict[str, Any] = field(default_factory=dict)

    # Tags to add / remove
    tags_to_add: list[str] = field(default_factory=list)
    tags_to_remove: list[str] = field(default_factory=list)

    # Pipeline stage moves
    pipeline_moves: list[PipelineMove] = field(default_factory=list)

    # Whether to create a new GHL contact (True if contact has no ghl_contact_id)
    needs_create: bool = False

    # ContactFlags used to derive tags + pipeline stages — set by compute_delta.
    # Consumers can use this instead of re-calling compute_flags.
    flags: ContactFlags = field(default_factory=ContactFlags)

    @property
    def is_empty(self) -> bool:
        """True if there is nothing to push to GHL."""
        return (
            not self.custom_fields
            and not self.tags_to_add
            and not self.tags_to_remove
            and not self.pipeline_moves
            and not self.needs_create
        )


# ── Field name → GHL field key mapping ────────────────────────────────────────
# Maps Postgres Contact attribute names to the GHL custom field keys.
# These keys must match what is configured in the GHL sub-account.
# See requirements_v2/00_master_overview.md §Custom Fields.

_FIELD_MAP: dict[str, str] = {
    "eversports_customer_id":           "eversports_customer_id",
    "active_package_type":              "active_package_type",
    "active_package_name":              "active_package_name",
    "active_package_expiry_date":       "active_package_expiry_date",
    "active_package_sessions_remaining":"active_package_sessions_remaining",
    "last_session_date":                "last_session_date",
    "last_class_name":                  "last_class_name",
    "total_sessions_attended":          "total_sessions_attended",
    "no_show_count":                    "no_show_count",
    "sessions_attended_this_month":     "sessions_attended_this_month",
    "sessions_attended_last_month":     "sessions_attended_last_month",
    "sessions_per_week_last_month":     "sessions_per_week_last_month",
    "last_booking_date":                "last_booking_date",
    # Populated by GHL sync after creation
    # "ghl_contact_id" is not pushed as a custom field
}


def _serialize_value(v: Any) -> Any:
    """Convert Python types to GHL-compatible JSON-serialisable values."""
    if v is None:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v.isoformat()       # "YYYY-MM-DD"
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, list):
        # products_purchased etc. — store as compact JSON string for GHL text fields
        return json.dumps(v, ensure_ascii=False)
    return v


def _extract_current_ghl_fields(contact: "Contact") -> dict[str, Any]:
    """
    Extract the subset of Contact fields that map to GHL custom fields,
    serialised to GHL-compatible values.
    """
    result: dict[str, Any] = {}
    for attr, ghl_key in _FIELD_MAP.items():
        v = getattr(contact, attr, None)
        result[ghl_key] = _serialize_value(v)
    return result


def compute_delta(
    contact: "Contact",
    location: "Location",
    *,
    current_ghl_tags: set[str] | None = None,
    current_lead_stage: str | None = None,
    current_card_stage: str | None = None,
    current_membership_stage: str | None = None,
    today: date | None = None,
) -> GhlDelta:
    """
    Compute the GHL change set for a contact.

    Args:
        contact: Current SQLAlchemy Contact row (all derived fields populated).
        location: SQLAlchemy Location row.
        current_ghl_tags: Set of tag names currently on the GHL contact.
            Pass ``None`` on first sync — treated as empty set.
        current_lead_stage: Current GHL lead pipeline stage name (or None).
        current_card_stage: Current GHL card pipeline stage name (or None).
        current_membership_stage: Current GHL membership pipeline stage name (or None).
        today: Override today's date (for testing).

    Returns:
        ``GhlDelta`` describing what needs to change.
    """
    today = today or date.today()
    delta = GhlDelta()

    # ── Contact creation ───────────────────────────────────────────────────────
    if not contact.ghl_contact_id:
        delta.needs_create = True
        # On create, push all non-null fields
        for ghl_key, v in _extract_current_ghl_fields(contact).items():
            if v is not None:
                delta.custom_fields[ghl_key] = v
    else:
        # ── Custom field delta ─────────────────────────────────────────────────
        # Compare current contact state against prev_state snapshot
        prev: dict[str, Any] = contact.ghl_prev_state or {}
        current = _extract_current_ghl_fields(contact)

        for ghl_key, new_val in current.items():
            old_val = prev.get(ghl_key)
            # Compare as strings to handle None vs "None" edge cases gracefully
            if str(new_val) != str(old_val):
                delta.custom_fields[ghl_key] = new_val

    # ── Tags ───────────────────────────────────────────────────────────────────
    # Pass current_ghl_tags into compute_flags so it can evaluate tags that live
    # in NEVER_REMOVE_TAGS (e.g. trial-not-converted set by UC01 workflow).
    flags: ContactFlags = compute_flags(
        contact, location,
        today=today,
        current_ghl_tags=current_ghl_tags,
    )
    desired_tags = flags.tags_desired
    current_tags = current_ghl_tags or set()

    delta.tags_to_add = sorted(desired_tags - current_tags)
    delta.tags_to_remove = sorted(current_tags - desired_tags - _NEVER_REMOVE_TAGS)

    # ── Pipeline moves ─────────────────────────────────────────────────────────
    _add_pipeline_move(delta, "lead", flags.lead_stage, current_lead_stage)
    _add_pipeline_move(delta, "card", flags.card_stage, current_card_stage)
    _add_pipeline_move(delta, "membership", flags.membership_stage, current_membership_stage)

    # Attach flags so callers can read pipeline/tag decisions without re-computing.
    delta.flags = flags

    return delta


def _add_pipeline_move(
    delta: GhlDelta,
    pipeline_name: str,
    desired_stage: str | None,
    current_stage: str | None,
) -> None:
    """
    Add a pipeline move to the delta if the stage needs to change.

    Skips if:
    - ``desired_stage`` is None (contact doesn't qualify for this pipeline)
    - ``desired_stage == current_stage`` (already in the right stage)
    """
    if desired_stage is None:
        return
    if desired_stage == current_stage:
        return
    delta.pipeline_moves.append(
        PipelineMove(pipeline_name=pipeline_name, new_stage=desired_stage)
    )
