"""
Writeback handler — create_customer.

Payload schema:
  {
    "first_name": str,
    "last_name":  str,
    "email":      str,
    "phone":      str | None,
    "marketing_consents": bool (default False)
  }

Idempotency key: sha256(location_id + email)

Dev-mode guard:
  Rejects any email != emiroztrk@gmail.com with SafetyGuardError.

Dry-run mode:
  Logs the full payload and returns a fake success response without
  touching Eversports.

Live mode (WRITEBACK_DRY_RUN=false, WRITEBACK_SAFETY_MODE=prod):
  Uses Playwright to navigate Eversports admin Customers → New customer.

  TODO (live Playwright implementation):
    1. Navigate to /customers/new
    2. Fill first_name, last_name, email, phone fields
    3. Toggle marketing consent checkbox if requested
    4. Click "Save"
    5. Parse response: extract new customer_id from URL or response JSON
    6. Return {"customer_id": "...", "status": "created"}

References:
  - requirements_v2/07_foundation_layer.md §Layer 4
  - app/writeback/safety.py — SafetyGuard
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def handle_create_customer(
    payload: dict[str, Any],
    location_id: str,
    *,
    dry_run: bool,
    safety_mode: str,
) -> dict[str, Any]:
    """
    Execute or simulate a create_customer writeback action.

    Args:
        payload:      Job payload with first_name, last_name, email, phone,
                      marketing_consents.
        location_id:  Location string (for safety guard + idempotency key).
        dry_run:      If True, log payload and return fake success.
        safety_mode:  "dev" | "prod" — passed to SafetyGuard.

    Returns:
        Result dict: {"customer_id": str, "status": "created"|"dry_run"}

    Raises:
        SafetyGuardError: in dev mode when email is not whitelisted.
        WritebackHandlerError: when the Playwright action fails (live mode).
    """
    from app.writeback.safety import SafetyGuard  # noqa: PLC0415

    email = payload.get("email", "")
    first_name = payload.get("first_name", "")
    last_name = payload.get("last_name", "")
    phone = payload.get("phone")
    marketing_consents = payload.get("marketing_consents", False)

    # Safety guard — raises SafetyGuardError immediately if non-whitelisted
    guard = SafetyGuard(mode=safety_mode)
    guard.check_create_customer(email)

    if dry_run:
        logger.info(
            "writeback[dry_run] create_customer: location_id=%s email=%s name='%s %s' "
            "phone=%s marketing=%s",
            location_id,
            email,
            first_name,
            last_name,
            phone,
            marketing_consents,
        )
        return {
            "customer_id": "DRY_RUN_CUSTOMER_ID",
            "status": "dry_run",
            "payload_logged": payload,
        }

    # ── Live Playwright implementation ────────────────────────────────────────
    # TODO: wire Playwright browser context; currently raises to prevent accidental
    #       live execution until the user has reviewed dry-run logs and given
    #       written go-ahead in chat.
    raise NotImplementedError(
        "create_customer live Playwright implementation is TODO — "
        "set WRITEBACK_DRY_RUN=true to use dry-run mode, or implement "
        "the Playwright steps after the user reviews dry-run logs."
    )
