"""
GHL tag engine — applies and removes tags with the 60-second race-condition guard.

The 60-second guard:
  When a tag is applied to a GHL contact, it may trigger a GHL automation.
  If the same tag is removed within 60 seconds of being applied, the automation
  may fire on the wrong (pre-removal) state.  To prevent this:

  Before removing a tag, check ``contact.ghl_tag_timestamps[tag]``.
  If the tag was applied within the last 60 seconds, defer the removal and
  log a warning.  The next sync run will attempt the removal again.

  Tags that are newly being ADDED in this sync run are also excluded from
  the removal list (apply-then-immediately-remove is always skipped).

Writes ``ghl_tag_timestamps`` back to the Contact row (caller must flush/commit).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.db.models.contacts import Contact
    from app.ghl.client import GhlClient

logger = logging.getLogger(__name__)

# How long to wait before removing a recently-applied tag
_RACE_GUARD_SECONDS = 60


async def apply_tag_changes(
    client: "GhlClient",
    contact: "Contact",
    tags_to_add: list[str],
    tags_to_remove: list[str],
) -> dict[str, list[str]]:
    """
    Apply tag additions and removals to a GHL contact.

    Enforces the 60-second race-condition guard on removals.

    Args:
        client: Authenticated GhlClient.
        contact: SQLAlchemy Contact row (``ghl_tag_timestamps`` will be updated).
        tags_to_add: Tags to apply.
        tags_to_remove: Tags to remove (subject to race guard).

    Returns:
        Dict with ``"added"`` and ``"removed"`` lists of tags actually changed.
    """
    now = datetime.now(tz=timezone.utc)
    timestamps: dict[str, str] = dict(contact.ghl_tag_timestamps or {})

    # 1. Add tags
    actually_added: list[str] = []
    if tags_to_add:
        await client.add_tags(contact.ghl_contact_id, tags_to_add)
        actually_added = tags_to_add
        # Record timestamps for newly applied tags
        for tag in tags_to_add:
            timestamps[tag] = now.isoformat()

    # 2. Remove tags — enforce race guard
    safe_to_remove: list[str] = []
    deferred: list[str] = []

    for tag in tags_to_remove:
        # Never remove a tag we just added in this cycle
        if tag in tags_to_add:
            deferred.append(tag)
            continue

        applied_at_str = timestamps.get(tag)
        if applied_at_str:
            try:
                applied_at = datetime.fromisoformat(applied_at_str)
                age = (now - applied_at).total_seconds()
                if age < _RACE_GUARD_SECONDS:
                    logger.info(
                        "tags: deferring removal of %r on contact %s "
                        "(applied %.0fs ago — within %ds guard window)",
                        tag,
                        contact.id,
                        age,
                        _RACE_GUARD_SECONDS,
                    )
                    deferred.append(tag)
                    continue
            except (ValueError, TypeError):
                pass  # Malformed timestamp — proceed with removal

        safe_to_remove.append(tag)

    actually_removed: list[str] = []
    if safe_to_remove:
        await client.remove_tags(contact.ghl_contact_id, safe_to_remove)
        actually_removed = safe_to_remove
        # Remove from timestamps cache
        for tag in safe_to_remove:
            timestamps.pop(tag, None)

    if deferred:
        logger.info(
            "tags: deferred %d tag removal(s) on contact %s: %s",
            len(deferred),
            contact.id,
            deferred,
        )

    # Persist updated timestamps back to contact row
    contact.ghl_tag_timestamps = timestamps

    return {"added": actually_added, "removed": actually_removed}
