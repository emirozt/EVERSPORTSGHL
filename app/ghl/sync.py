"""
GHL contact sync — orchestrates the full GHL push for one contact.

For each contact:
  1. Compute GhlDelta (engine.compute_delta)
  2. If delta.needs_create: upsert contact in GHL (create or find existing)
  3. Push changed custom fields (update_contact)
  4. Apply/remove tags (tags.apply_tag_changes — with 60s race guard)
  5. Execute pipeline moves (pipelines.execute_pipeline_moves)
  6. Update contact.ghl_prev_state + ghl_last_synced_at in Postgres

The batch-level entry point ``sync_all_contacts_for_location()`` iterates all
contacts for a location, reusing one GhlClient + PipelineCache across all
contacts in the batch (minimises API round-trips).

Error handling:
  - Per-contact errors are caught and collected; the batch continues.
  - SessionExpiredError / GhlAuthError propagate immediately (auth failure
    affects all contacts, no point continuing).

References:
  - requirements_v2/00_master_overview.md §Foundation Layer
  - app/delta/engine.py — delta computation
  - app/ghl/client.py  — GHL API
  - app/ghl/tags.py    — tag race guard
  - app/ghl/pipelines.py — pipeline stage moves
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.contacts import Contact
from app.db.models.location import Location
from app.delta.engine import compute_delta
from app.ghl.client import GhlAuthError, GhlClient
from app.ghl.pipelines import PipelineCache, execute_pipeline_moves
from app.ghl.tags import apply_tag_changes

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ── Result dataclass ───────────────────────────────────────────────────────────

@dataclass
class ContactSyncResult:
    contact_id: uuid.UUID
    ghl_contact_id: str | None
    created: bool = False
    custom_fields_pushed: int = 0
    tags_added: list[str] = field(default_factory=list)
    tags_removed: list[str] = field(default_factory=list)
    pipeline_moves: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class BatchSyncResult:
    location_id: uuid.UUID
    contacts_synced: int = 0
    contacts_created: int = 0
    contacts_updated: int = 0
    contacts_failed: int = 0
    tags_applied: int = 0
    pipeline_moves: int = 0
    errors: list[str] = field(default_factory=list)


# ── Single-contact sync ────────────────────────────────────────────────────────

async def sync_contact(
    contact: Contact,
    location: Location,
    client: GhlClient,
    pipeline_cache: PipelineCache,
    db: AsyncSession,
) -> ContactSyncResult:
    """
    Push one contact's current Postgres state to GHL.

    Returns a ``ContactSyncResult`` describing what changed.
    Does NOT commit the DB session.
    """
    result = ContactSyncResult(
        contact_id=contact.id,
        ghl_contact_id=contact.ghl_contact_id,
    )

    try:
        # Current GHL tag state (loaded from prev_state snapshot)
        prev: dict[str, Any] = contact.ghl_prev_state or {}
        current_tags: set[str] = set(prev.get("_tags", []))
        current_lead_stage: str | None = prev.get("_lead_stage")
        current_card_stage: str | None = prev.get("_card_stage")
        current_membership_stage: str | None = prev.get("_membership_stage")

        # Compute what needs to change
        delta = compute_delta(
            contact,
            location,
            current_ghl_tags=current_tags,
            current_lead_stage=current_lead_stage,
            current_card_stage=current_card_stage,
            current_membership_stage=current_membership_stage,
        )

        if delta.is_empty:
            logger.debug("ghl_sync: contact %s — no changes", contact.id)
            return result

        # ── Step 1: Ensure contact exists in GHL ──────────────────────────────
        if delta.needs_create or not contact.ghl_contact_id:
            ghl_id, created = await client.upsert_contact(
                email=contact.email,
                first_name=contact.first_name,
                last_name=contact.last_name,
                phone=contact.phone,
                custom_fields=delta.custom_fields if delta.needs_create else None,
            )
            contact.ghl_contact_id = ghl_id
            result.ghl_contact_id = ghl_id
            result.created = created
            if delta.needs_create:
                result.custom_fields_pushed = len(delta.custom_fields)
        else:
            # ── Step 2: Push changed custom fields ────────────────────────────
            if delta.custom_fields:
                await client.update_contact(
                    contact.ghl_contact_id,
                    custom_fields=delta.custom_fields,
                )
                result.custom_fields_pushed = len(delta.custom_fields)

        # ── Step 3: Apply / remove tags ───────────────────────────────────────
        tag_result = await apply_tag_changes(
            client,
            contact,
            tags_to_add=delta.tags_to_add,
            tags_to_remove=delta.tags_to_remove,
        )
        result.tags_added = tag_result["added"]
        result.tags_removed = tag_result["removed"]

        # ── Step 4: Pipeline moves ─────────────────────────────────────────────
        if delta.pipeline_moves:
            await pipeline_cache.load(client)
            moves_done = await execute_pipeline_moves(
                client, contact, delta.pipeline_moves, pipeline_cache
            )
            result.pipeline_moves = moves_done

        # ── Step 5: Update prev_state snapshot ────────────────────────────────
        # Compute the new desired tag set for persistence
        from app.delta.flags import compute_flags  # noqa: PLC0415
        flags = compute_flags(contact, location)
        new_tags = (current_tags | set(result.tags_added)) - set(result.tags_removed)

        # Pipeline stage tracking
        from app.delta.flags import LeadStage, CardStage, MembershipStage  # noqa: PLC0415
        new_lead = flags.lead_stage or current_lead_stage
        new_card = flags.card_stage or current_card_stage
        new_membership = flags.membership_stage or current_membership_stage

        # Build new prev_state — includes all current custom field values + tag/pipeline state
        from app.delta.engine import _extract_current_ghl_fields  # noqa: PLC0415
        new_prev: dict[str, Any] = _extract_current_ghl_fields(contact)
        new_prev["_tags"] = sorted(new_tags)
        new_prev["_lead_stage"] = new_lead
        new_prev["_card_stage"] = new_card
        new_prev["_membership_stage"] = new_membership

        contact.ghl_prev_state = new_prev
        contact.ghl_last_synced_at = datetime.now(tz=timezone.utc)

    except GhlAuthError:
        raise  # Auth errors stop the whole batch
    except Exception as exc:  # noqa: BLE001
        msg = f"contact {contact.id}: {exc}"
        logger.error("ghl_sync: %s", msg, exc_info=True)
        result.error = msg

    return result


# ── Batch sync for all contacts in a location ──────────────────────────────────

async def sync_all_contacts_for_location(
    location_id: uuid.UUID,
    db: AsyncSession,
) -> BatchSyncResult:
    """
    Push all contacts for a location to GHL.

    Loads the location + all its contacts, then syncs each one.
    Reuses a single GhlClient + PipelineCache across all contacts.

    Args:
        location_id: UUID of the location to sync.
        db: Async SQLAlchemy session.

    Returns:
        ``BatchSyncResult`` with aggregate counts.
    """
    result = BatchSyncResult(location_id=location_id)

    # Load location
    loc_res = await db.execute(
        select(Location).where(Location.id == location_id)
    )
    location = loc_res.scalar_one_or_none()
    if location is None:
        result.errors.append(f"Location {location_id} not found")
        return result

    if not location.ghl_oauth_token_cache:
        result.errors.append(
            f"Location {location_id} has no GHL OAuth tokens — "
            "complete the OAuth flow first"
        )
        return result

    # Load all contacts for this location
    contacts_res = await db.execute(
        select(Contact).where(Contact.location_id == location_id)
    )
    contacts: list[Contact] = list(contacts_res.scalars().all())
    logger.info(
        "ghl_sync: syncing %d contacts for location %s",
        len(contacts),
        location_id,
    )

    pipeline_cache = PipelineCache()

    async with GhlClient(location, db) as client:
        for contact in contacts:
            cr = await sync_contact(contact, location, client, pipeline_cache, db)
            result.contacts_synced += 1
            if cr.ok:
                if cr.created:
                    result.contacts_created += 1
                elif cr.custom_fields_pushed > 0 or cr.tags_added or cr.tags_removed or cr.pipeline_moves:
                    result.contacts_updated += 1
                result.tags_applied += len(cr.tags_added)
                result.pipeline_moves += len(cr.pipeline_moves)
            else:
                result.contacts_failed += 1
                result.errors.append(cr.error or "unknown error")

    logger.info(
        "ghl_sync: done location=%s  synced=%d  created=%d  updated=%d  "
        "failed=%d  tags=%d  pipeline_moves=%d",
        location_id,
        result.contacts_synced,
        result.contacts_created,
        result.contacts_updated,
        result.contacts_failed,
        result.tags_applied,
        result.pipeline_moves,
    )
    return result
