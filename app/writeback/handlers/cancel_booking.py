"""
Writeback handler — cancel_booking.

Payload schema:
  {
    "booking_id":     str,
    "class_name":     str,
    "session_datetime": str (ISO-8601 UTC),
    "customer_email": str,
    "reason":         str | None
  }

Idempotency key: sha256(booking_id + "cancel")

Dev-mode guard:
  Rejects any target session != "Reformer Booty Burn Group Class"
  on 2026-11-30T19:00 UTC.

Live mode (TODO):
  Playwright: Booking detail → Cancel booking.

  TODO (live Playwright implementation):
    1. Navigate to /bookings/{booking_id}
    2. Click "Cancel booking"
    3. Confirm cancellation
    4. Return {"booking_id": booking_id, "status": "cancelled"}

Teardown note:
  cancel_booking is also used as the teardown step after test runs that
  created a real booking.  The executor calls audit.record_teardown()
  instead of record_writeback() when teardown=True is set in the payload.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


async def handle_cancel_booking(
    payload: dict[str, Any],
    location_id: str,
    *,
    dry_run: bool,
    safety_mode: str,
) -> dict[str, Any]:
    """
    Execute or simulate a cancel_booking writeback action.

    Raises:
        SafetyGuardError: in dev mode when target session is not whitelisted.
        WritebackHandlerError: when the Playwright action fails (live mode).
    """
    from app.writeback.safety import SafetyGuard  # noqa: PLC0415

    booking_id = payload.get("booking_id", "")
    class_name = payload.get("class_name", "")
    session_datetime_str = payload.get("session_datetime", "")
    customer_email = payload.get("customer_email", "")
    reason = payload.get("reason")

    # Parse session_datetime
    try:
        start_dt = datetime.fromisoformat(session_datetime_str)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"cancel_booking: invalid session_datetime '{session_datetime_str}': {exc}"
        ) from exc

    # Safety guard
    guard = SafetyGuard(mode=safety_mode)
    guard.check_booking_target(class_name, start_dt)

    if dry_run:
        logger.info(
            "writeback[dry_run] cancel_booking: location_id=%s booking_id=%s "
            "class='%s' start=%s customer=%s reason=%s",
            location_id,
            booking_id,
            class_name,
            start_dt.isoformat(),
            customer_email,
            reason,
        )
        return {
            "booking_id": booking_id,
            "status": "dry_run",
            "payload_logged": payload,
        }

    raise NotImplementedError(
        "cancel_booking live Playwright implementation is TODO — "
        "set WRITEBACK_DRY_RUN=true until user reviews dry-run logs."
    )
