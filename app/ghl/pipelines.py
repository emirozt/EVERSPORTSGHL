"""
GHL pipeline engine — moves contacts through the three opportunity pipelines.

Pipelines are GHL opportunities.  Each contact can have up to one opportunity
per pipeline (lead-to-sale, card, membership).  The engine:

  1. Looks up the pipeline + stage IDs for this sub-account (cached per client).
  2. Finds the existing opportunity for this contact in the pipeline (if any).
  3. Creates one if it doesn't exist; moves it to the new stage if it does.

Pipeline names in code ("lead", "card", "membership") map to the display names
configured in the GHL sub-account.  The mapping is stored in
``locations.ghl_oauth_token_cache`` (or derived from the GHL pipeline list).

References:
  - requirements_v2/03_ghl_pipelines.md
  - app/delta/flags.py §Pipeline stage names
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.delta.engine import PipelineMove

if TYPE_CHECKING:
    from app.db.models.contacts import Contact
    from app.db.models.location import Location
    from app.ghl.client import GhlClient

logger = logging.getLogger(__name__)

# Map from our internal pipeline key to the GHL display name.
# These names must match what is configured in the GHL sub-account exactly.
_PIPELINE_DISPLAY_NAMES: dict[str, str] = {
    "lead": "Lead to Sale",
    "card": "Card Package",
    "membership": "Membership",
}


class PipelineCache:
    """
    Lazy-loaded cache of GHL pipeline + stage metadata for one sub-account.

    Populated on first use (one API call), then reused for all contacts in
    the same sync run.
    """

    def __init__(self) -> None:
        # pipeline_key → {"id": str, "stages": {stage_name: stage_id}}
        self._data: dict[str, dict] = {}
        self._loaded = False

    async def load(self, client: "GhlClient") -> None:
        if self._loaded:
            return
        pipelines = await client.get_pipelines()
        for pl in pipelines:
            for key, display_name in _PIPELINE_DISPLAY_NAMES.items():
                if pl.get("name") == display_name:
                    stages = {
                        s["name"]: s["id"]
                        for s in pl.get("stages", [])
                        if "name" in s and "id" in s
                    }
                    self._data[key] = {"id": pl["id"], "stages": stages}
                    break
        self._loaded = True
        logger.debug(
            "pipelines: loaded %d pipeline(s): %s",
            len(self._data),
            list(self._data.keys()),
        )

    def pipeline_id(self, key: str) -> str | None:
        return self._data.get(key, {}).get("id")

    def stage_id(self, key: str, stage_name: str) -> str | None:
        return self._data.get(key, {}).get("stages", {}).get(stage_name)


async def execute_pipeline_moves(
    client: "GhlClient",
    contact: "Contact",
    moves: list[PipelineMove],
    cache: PipelineCache,
) -> list[str]:
    """
    Execute a list of pipeline stage moves for a contact.

    Args:
        client: Authenticated GhlClient.
        contact: SQLAlchemy Contact row (must have ``ghl_contact_id``).
        moves: List of ``PipelineMove`` from the delta engine.
        cache: Pre-loaded ``PipelineCache`` for this sync batch.

    Returns:
        List of human-readable strings describing what moved.
    """
    if not moves:
        return []

    results: list[str] = []

    for move in moves:
        pipeline_id = cache.pipeline_id(move.pipeline_name)
        if pipeline_id is None:
            logger.warning(
                "pipelines: pipeline %r not found in GHL sub-account %s — skipping move",
                move.pipeline_name,
                contact.location_id,
            )
            continue

        stage_id = cache.stage_id(move.pipeline_name, move.new_stage)
        if stage_id is None:
            logger.warning(
                "pipelines: stage %r not found in pipeline %r — skipping",
                move.new_stage,
                move.pipeline_name,
            )
            continue

        # Find existing opportunity for this contact in this pipeline
        opp = await client.search_opportunity(contact.ghl_contact_id, pipeline_id)

        if opp:
            opp_id = opp["id"]
            await client.move_opportunity_stage(
                opp_id,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
            )
            msg = f"{move.pipeline_name}→'{move.new_stage}'"
        else:
            # Create a new opportunity
            display = _PIPELINE_DISPLAY_NAMES.get(move.pipeline_name, move.pipeline_name)
            opp_id = await client.create_opportunity(
                contact_id=contact.ghl_contact_id,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                name=f"{display} — {contact.first_name or ''} {contact.last_name or ''}".strip(),
            )
            msg = f"{move.pipeline_name}→'{move.new_stage}' (created)"

        logger.info(
            "pipelines: contact %s  opp=%s  %s",
            contact.id,
            opp_id,
            msg,
        )
        results.append(msg)

    return results
