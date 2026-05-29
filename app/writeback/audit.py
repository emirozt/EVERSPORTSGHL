"""
M5 writeback audit log + email notification.

Every successful live writeback (not dry-run) must:
  1. Append a structured line to ops/m5_writeback_audit.log
  2. Send a notification email to the configured owner address

If either side effect fails, the function raises AuditError so the calling
code can treat it as a test failure (per the user's safety constraint:
"notification failure = test failure").

Log format (one JSON object per line):
  {
    "ts": "2026-11-30T19:15:02.123456+00:00",
    "action": "create_booking",
    "customer_email": "emiroztrk@gmail.com",
    "class_name": "Reformer Booty Burn Group Class",
    "idempotency_key": "abc123...",
    "eversports_response": {...},
    "ghl_webhook_fired": "writeback-success"
  }

SMTP:
  Uses notification_smtp_host / _port / _user / _password from settings.
  If smtp_host is None (the default), logs a warning and skips SMTP but
  still writes the audit log.  Set smtp_host to enable email.

Subject format: "[M5 TEST] {action} for {class_name} {start_dt}"

References:
  - requirements_v2/07_foundation_layer.md §Layer 4 (result reporting)
  - User safety constraints (2026-05-28 session)
"""

from __future__ import annotations

import asyncio
import json
import logging
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Audit log path (relative to the project root, created if absent)
AUDIT_LOG_PATH = Path("ops/m5_writeback_audit.log")


class AuditError(Exception):
    """Raised when audit log write or notification email fails."""


def _get_audit_log_path() -> Path:
    """Return the audit log path, ensuring the parent directory exists."""
    path = AUDIT_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _append_audit_line(entry: dict[str, Any]) -> None:
    """Append a JSON line to the audit log (sync, called from async context)."""
    line = json.dumps(entry, default=str) + "\n"
    with _get_audit_log_path().open("a", encoding="utf-8") as f:
        f.write(line)


def _send_smtp_email(
    *,
    subject: str,
    body: str,
    to_email: str,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str | None,
    smtp_password: str | None,
    from_email: str,
) -> None:
    """Send a plain-text email via SMTP (sync, called from async context)."""
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email

    with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
        server.ehlo()
        if smtp_port != 25:
            server.starttls()
            server.ehlo()
        if smtp_user and smtp_password:
            server.login(smtp_user, smtp_password)
        server.sendmail(from_email, [to_email], msg.as_string())


async def record_writeback(
    *,
    action: str,
    customer_email: str,
    class_name: str | None,
    start_dt: datetime | None,
    idempotency_key: str,
    eversports_response: Any,
    ghl_webhook_fired: str | None,
    mode: str = "live",
) -> None:
    """
    Record a writeback attempt: append to audit log + send email (live only).

    Args:
        action:                e.g. "create_booking"
        customer_email:        customer email address involved
        class_name:            Eversports class name (None for create_customer)
        start_dt:              session start datetime UTC (None for create_customer)
        idempotency_key:       sha256 key used for this job
        eversports_response:   raw response from Eversports (any JSON-serialisable)
        ghl_webhook_fired:     e.g. "writeback-success" or None
        mode:                  "live" | "dry_run" — controls whether email is sent

    Raises:
        AuditError: if audit log write or email notification fails.
    """
    from app.config import get_settings  # noqa: PLC0415

    settings = get_settings()

    ts = datetime.now(timezone.utc)
    entry: dict[str, Any] = {
        "ts": ts.isoformat(),
        "mode": mode,
        "action": action,
        "customer_email": customer_email,
        "class_name": class_name,
        "start_dt": start_dt.isoformat() if start_dt else None,
        "idempotency_key": idempotency_key,
        "eversports_response": eversports_response,
        "ghl_webhook_fired": ghl_webhook_fired,
    }

    # ── 1. Audit log (always required) ────────────────────────────────────────
    try:
        await asyncio.get_event_loop().run_in_executor(None, _append_audit_line, entry)
        logger.info(
            "audit: wrote entry action=%s idempotency_key=%s",
            action,
            idempotency_key,
        )
    except Exception as exc:  # noqa: BLE001
        raise AuditError(f"Failed to write audit log: {exc}") from exc

    # ── 2. Email notification (live mode only) ────────────────────────────────
    if mode != "live":
        logger.debug("audit: mode=%s — skipping email notification", mode)
        return

    to_email = settings.notification_owner_email
    if not to_email:
        logger.warning(
            "audit: notification_owner_email not configured — skipping email "
            "(set NOTIFICATION_OWNER_EMAIL in .env)"
        )
        return

    # Build subject
    if class_name and start_dt:
        dt_str = start_dt.strftime("%Y-%m-%d %H:%M")
        subject = f"[M5 TEST] {action} for {class_name} {dt_str}"
    else:
        subject = f"[M5 TEST] {action} for {customer_email}"

    body = json.dumps(entry, indent=2, default=str)

    if not settings.notification_smtp_host:
        # TODO: wire to real SMTP/SES provider when notification_smtp_host is configured
        logger.warning(
            "audit: notification_smtp_host not set — email stub only.\n"
            "  Subject: %s\n  Body preview: %s",
            subject,
            body[:200],
        )
        return

    try:
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _send_smtp_email(
                subject=subject,
                body=body,
                to_email=to_email,
                smtp_host=settings.notification_smtp_host,
                smtp_port=settings.notification_smtp_port,
                smtp_user=settings.notification_smtp_user,
                smtp_password=settings.notification_smtp_password,
                from_email=settings.notification_from_email,
            ),
        )
        logger.info("audit: notification email sent to %s (subject=%s)", to_email, subject)
    except Exception as exc:  # noqa: BLE001
        raise AuditError(
            f"Failed to send notification email to {to_email}: {exc}"
        ) from exc


async def record_safety_rejection(
    *,
    action: str,
    payload: dict[str, Any],
    rejection_reason: str,
) -> None:
    """
    Record an API-level safety-guard rejection to the audit log.

    Called when the enqueue endpoint rejects a job before it is queued
    (SafetyGuardError at submission time).  Does NOT send email.

    Args:
        action:           Job type (e.g. "create_customer").
        payload:          The submitted payload that was rejected.
        rejection_reason: Human-readable reason from SafetyGuardError.
    """
    ts = datetime.now(timezone.utc)
    entry: dict[str, Any] = {
        "ts": ts.isoformat(),
        "mode": "safety_guard_rejected",
        "action": action,
        "customer_email": payload.get("email") or payload.get("customer_email", ""),
        "class_name": payload.get("class_name") or payload.get("new_class_name"),
        "start_dt": payload.get("session_datetime") or payload.get("new_session_datetime"),
        "idempotency_key": None,
        "eversports_response": None,
        "ghl_webhook_fired": None,
        "rejection_reason": rejection_reason,
    }
    try:
        await asyncio.get_event_loop().run_in_executor(None, _append_audit_line, entry)
        logger.info("audit: safety_guard_rejected entry written action=%s", action)
    except Exception as exc:  # noqa: BLE001
        # Non-fatal — log but don't propagate (the 422 response already informs the caller)
        logger.error("audit: failed to write safety_rejection entry: %s", exc)


async def record_teardown(
    *,
    action: str,
    booking_id: str | None,
    customer_email: str,
    reason: str = "test_teardown",
) -> None:
    """
    Record a teardown action (booking cancellation after test run).

    Appends to the same audit log as record_writeback.  Does NOT send email.
    """
    ts = datetime.now(timezone.utc)
    entry: dict[str, Any] = {
        "ts": ts.isoformat(),
        "action": f"teardown:{action}",
        "booking_id": booking_id,
        "customer_email": customer_email,
        "reason": reason,
    }
    try:
        await asyncio.get_event_loop().run_in_executor(None, _append_audit_line, entry)
        logger.info("audit: teardown recorded action=%s booking_id=%s", action, booking_id)
    except Exception as exc:  # noqa: BLE001
        raise AuditError(f"Failed to write teardown audit entry: {exc}") from exc
