"""
Writeback handler — reschedule_booking.

Payload schema:
  {
    "booking_id":       str,
    "new_session_id":   str,
    "new_class_name":   str,
    "new_session_datetime": str (ISO-8601 UTC),
    "customer_email":   str,
    "reason":           str | None
  }

Idempotency key: sha256(booking_id + new_session_id)

Dev-mode guard:
  Rejects any target new_session != "Reformer Booty Burn Group Class"
  on 2026-11-30T19:00 UTC.

Live mode (TODO):
  Playwright: Booking detail → Move to new session.

  TODO (live Playwright implementation):
    1. Navigate to /bookings/{booking_id}
    2. Click "Move booking"
    3. Search for new session by new_session_id
    4. Confirm reschedule
    5. Return {"booking_id": booking_id, "new_session_id": new_session_id, "status": "rescheduled"}
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


async def handle_reschedule_booking(
    payload: dict[str, Any],
    location_id: str,
    *,
    dry_run: bool,
    safety_mode: str,
) -> dict[str, Any]:
    """
    Execute or simulate a reschedule_booking writeback action.

    Raises:
        SafetyGuardError: in dev mode when new session is not whitelisted.
        WritebackHandlerError: when the Playwright action fails (live mode).
    """
    from app.writeback.safety import SafetyGuard  # noqa: PLC0415

    booking_id = payload.get("booking_id", "")
    new_session_id = payload.get("new_session_id", "")
    new_class_name = payload.get("new_class_name", "")
    new_session_datetime_str = payload.get("new_session_datetime", "")
    customer_email = payload.get("customer_email", "")
    reason = payload.get("reason")

    # Parse new_session_datetime
    try:
        new_start_dt = datetime.fromisoformat(new_session_datetime_str)
        if new_start_dt.tzinfo is None:
            new_start_dt = new_start_dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"reschedule_booking: invalid new_session_datetime "
            f"'{new_session_datetime_str}': {exc}"
        ) from exc

    # Safety guard — checks the NEW target session
    guard = SafetyGuard(mode=safety_mode)
    guard.check_booking_target(new_class_name, new_start_dt)

    if dry_run:
        logger.info(
            "writeback[dry_run] reschedule_booking: location_id=%s booking_id=%s "
            "new_session_id=%s class='%s' new_start=%s reason=%s customer=%s",
            location_id,
            booking_id,
            new_session_id,
            new_class_name,
            new_start_dt.isoformat(),
            reason,
            customer_email,
        )
        return {
            "booking_id": booking_id,
            "new_session_id": new_session_id,
            "status": "dry_run",
            "payload_logged": payload,
        }

    raise NotImplementedError(
        "reschedule_booking live Playwright implementation is TODO — "
        "set WRITEBACK_DRY_RUN=true until user reviews dry-run logs."
    )
