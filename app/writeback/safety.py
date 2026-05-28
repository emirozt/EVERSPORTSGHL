"""
Writeback safety guard — enforces WRITEBACK_SAFETY_MODE=dev whitelist.

When WRITEBACK_SAFETY_MODE=dev (the default), all writeback actions are
restricted to a hard-coded whitelist:

  Whitelisted customer:
    email: emiroztrk@gmail.com   (owner's personal account — safe to book against)

  Whitelisted class:
    name:     "Reformer Booty Burn Group Class"
    start_dt: 2026-11-30 19:00 UTC  (Monday, the test slot)

These constraints exist because the connector is wired to a LIVE Eversports
studio account and there is no separate test environment.  Any writeback
against a non-whitelisted target in dev mode raises SafetyGuardError
immediately — errors are loud and fail tests, not silently skipped.

To lift the guard, set WRITEBACK_SAFETY_MODE=prod explicitly.  This should
only happen in a real production deployment with operator sign-off.

Usage:
    from app.writeback.safety import get_safety_guard, SafetyGuardError

    guard = get_safety_guard()               # reads WRITEBACK_SAFETY_MODE from settings
    guard.check_create_customer("some@email")
    guard.check_booking_target("Class Name", start_dt)

References:
  - requirements_v2/07_foundation_layer.md §Layer 4
  - User safety constraints (2026-05-28 session)
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Dev-mode whitelist ─────────────────────────────────────────────────────────

WHITELISTED_EMAIL = "emiroztrk@gmail.com"
WHITELISTED_CLASS_NAME = "Reformer Booty Burn Group Class"
# Monday 2026-11-30 19:00 UTC (the test slot)
WHITELISTED_START_DT = datetime(2026, 11, 30, 19, 0, 0, tzinfo=timezone.utc)


class SafetyGuardError(Exception):
    """Raised when a writeback action targets a non-whitelisted resource in dev mode."""


class SafetyGuard:
    """
    Enforces the dev-mode hard whitelist on writeback targets.

    Args:
        mode: "dev" or "prod".  In dev mode all checks are active.
              In prod mode all checks pass silently.
    """

    def __init__(self, mode: str) -> None:
        self.mode = mode.lower()
        if self.mode not in ("dev", "prod"):
            raise ValueError(f"WRITEBACK_SAFETY_MODE must be 'dev' or 'prod', got '{mode}'")
        if self.mode == "dev":
            logger.info(
                "safety: WRITEBACK_SAFETY_MODE=dev — only whitelisted targets allowed "
                "(email=%s, class='%s', start=%s)",
                WHITELISTED_EMAIL,
                WHITELISTED_CLASS_NAME,
                WHITELISTED_START_DT.isoformat(),
            )

    # ── Public check API ──────────────────────────────────────────────────────

    def check_create_customer(self, email: str) -> None:
        """
        Raise SafetyGuardError if `email` is not whitelisted (dev mode only).

        Args:
            email: The customer email address to be created.

        Raises:
            SafetyGuardError: in dev mode when email != WHITELISTED_EMAIL.
        """
        if self.mode != "dev":
            return
        if email.lower().strip() != WHITELISTED_EMAIL.lower():
            raise SafetyGuardError(
                f"SafetyGuard: non-whitelisted email '{email}' — "
                f"dev mode only allows '{WHITELISTED_EMAIL}'. "
                "Set WRITEBACK_SAFETY_MODE=prod to lift this guard."
            )

    def check_booking_target(self, class_name: str, start_dt: datetime) -> None:
        """
        Raise SafetyGuardError if the booking target is not the whitelisted slot
        (dev mode only).

        Args:
            class_name: The Eversports class/activity name.
            start_dt:   UTC start datetime of the session.

        Raises:
            SafetyGuardError: in dev mode when class_name or start_dt mismatch.
        """
        if self.mode != "dev":
            return

        if class_name.strip() != WHITELISTED_CLASS_NAME:
            raise SafetyGuardError(
                f"SafetyGuard: non-whitelisted class '{class_name}' — "
                f"dev mode only allows '{WHITELISTED_CLASS_NAME}'. "
                "Set WRITEBACK_SAFETY_MODE=prod to lift this guard."
            )

        # Normalise to UTC for comparison
        if start_dt.tzinfo is None:
            start_dt_utc = start_dt.replace(tzinfo=timezone.utc)
        else:
            start_dt_utc = start_dt.astimezone(timezone.utc)

        expected = WHITELISTED_START_DT
        if start_dt_utc.replace(second=0, microsecond=0) != expected.replace(second=0, microsecond=0):
            raise SafetyGuardError(
                f"SafetyGuard: non-whitelisted start_dt '{start_dt_utc.isoformat()}' — "
                f"dev mode only allows '{expected.isoformat()}'. "
                "Set WRITEBACK_SAFETY_MODE=prod to lift this guard."
            )

    # ── Idempotency key helpers ───────────────────────────────────────────────

    @staticmethod
    def make_create_customer_key(location_id: str, email: str) -> str:
        """sha256(location_id + email) — unique per location+customer pair."""
        raw = f"{location_id}:{email.lower().strip()}"
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def make_create_booking_key(customer_id: str, session_id: str) -> str:
        """sha256(customer_id + session_id)."""
        raw = f"{customer_id}:{session_id}"
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def make_reschedule_booking_key(booking_id: str, new_session_id: str) -> str:
        """sha256(booking_id + new_session_id)."""
        raw = f"{booking_id}:{new_session_id}"
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def make_cancel_booking_key(booking_id: str) -> str:
        """sha256(booking_id + 'cancel')."""
        raw = f"{booking_id}:cancel"
        return hashlib.sha256(raw.encode()).hexdigest()


def get_safety_guard() -> SafetyGuard:
    """
    Return a SafetyGuard instance configured from the current settings.

    Imported at call time to allow test overrides via environment variables.
    """
    from app.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    return SafetyGuard(mode=settings.writeback_safety_mode)
