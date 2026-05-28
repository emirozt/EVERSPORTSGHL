"""
Writeback handler — create_booking.

Payload schema:
  {
    "customer_id":       str,
    "activity_id":       str,
    "session_id":        str,
    "session_datetime":  str (ISO-8601 UTC),
    "class_name":        str,
    "package_id":        str | None
  }

Idempotency key: sha256(customer_id + session_id)

Dev-mode guard:
  Rejects any target session != "Reformer Booty Burn Group Class"
  on 2026-11-30T19:00 UTC with SafetyGuardError.

Dry-run mode:
  Logs the full payload and returns a fake success response.

Live mode (TODO):
  Playwright: Activity calendar → Find session → Add participant.

  TODO (live Playwright implementation):
    1. Navigate to /activities/{activity_id}/sessions/{session_id}
    2. Click "Add participant"
    3. Search for customer by customer_id or email
    4. Select package if package_id provided
    5. Confirm booking
    6. Return {"booking_id": "...", "status": "created"}

References:
  - requirements_v2/07_foundation_layer.md §Layer 4
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


async def handle_create_booking(
    payload: dict[str, Any],
    location_id: str,
    *,
    dry_run: bool,
    safety_mode: str,
) -> dict[str, Any]:
    """
    Execute or simulate a create_booking writeback action.

    Args:
        payload:      Job payload — see module docstring for schema.
        location_id:  Location string (for logging).
        dry_run:      If True, log payload and return fake success.
        safety_mode:  "dev" | "prod" — passed to SafetyGuard.

    Returns:
        Result dict: {"booking_id": str, "status": "created"|"dry_run"}

    Raises:
        SafetyGuardError: in dev mode when target session is not whitelisted.
        WritebackHandlerError: when the Playwright action fails (live mode).
    """
    from app.writeback.safety import SafetyGuard  # noqa: PLC0415

    customer_id = payload.get("customer_id", "")
    session_id = payload.get("session_id", "")
    class_name = payload.get("class_name", "")
    session_datetime_str = payload.get("session_datetime", "")
    package_id = payload.get("package_id")

    # Parse session_datetime
    try:
        start_dt = datetime.fromisoformat(session_datetime_str)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"create_booking: invalid session_datetime '{session_datetime_str}': {exc}"
        ) from exc

    # Safety guard
    guard = SafetyGuard(mode=safety_mode)
    guard.check_booking_target(class_name, start_dt)

    if dry_run:
        logger.info(
            "writeback[dry_run] create_booking: location_id=%s customer_id=%s "
            "session_id=%s class='%s' start=%s package=%s",
            location_id,
            customer_id,
            session_id,
            class_name,
            start_dt.isoformat(),
            package_id,
        )
        return {
            "booking_id": "DRY_RUN_BOOKING_ID",
            "status": "dry_run",
            "payload_logged": payload,
        }

    # ── Live Playwright implementation ────────────────────────────────────────
    raise NotImplementedError(
        "create_booking live Playwright implementation is TODO — "
        "set WRITEBACK_DRY_RUN=true until user reviews dry-run logs."
    )
