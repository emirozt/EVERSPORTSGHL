"""
AI budget enforcement (M7).

Reads ``ai_usage`` to compute monthly spend per location, then enforces:
  - Soft cap (80% of ai_monthly_budget_usd):
      Owner warning email sent once per calendar month (non-fatal).
  - Hard cap (100% of ai_monthly_budget_usd):
      Non-essential AI calls raise ``BudgetExceededError``.
      Essential calls (gatekeeper + UC04 inbound) are always allowed through.

Essentiality:
  ESSENTIAL (never blocked):
    - gatekeeper / classification — inbound routing must always work
    - UC04 / reply_handling       — live customer support must continue
  NON-ESSENTIAL (blocked at hard cap):
    - UC01 / * (trial follow-up outbound)
    - UC04 / message_generation   (outbound message drafting)
    - UC05 / *                    (booking assistant)
    - All other use_case / step combinations

Typical call-site pattern::

    status = await check_budget(location, db)
    await assert_budget_available(status, use_case="UC04", step="message_generation")
    result = await ai_client.complete(...)

References:
  - requirements_v2/07_foundation_layer.md § "AI Usage Logger" (soft cap / hard cap)
  - app/ai/pricing.py           — compute_cost
  - app/db/models/ai_usage.py   — AiUsage table
  - app/db/models/location.py   — Location.ai_monthly_budget_usd
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from email.mime.text import MIMEText
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.ai_usage import AiUsage

if TYPE_CHECKING:
    from app.db.models.location import Location

logger = logging.getLogger(__name__)


# ── Essentiality rules ────────────────────────────────────────────────────────

# (use_case, step) pairs that are ALWAYS allowed, even when hard cap is hit.
_ESSENTIAL_PAIRS: frozenset[tuple[str, str]] = frozenset(
    {
        # Inbound routing — must always classify incoming messages
        ("gatekeeper", "classification"),
        # Live inbound support — do not break customer conversations
        ("UC04", "reply_handling"),
    }
)

# Use cases where ALL steps are essential (regardless of step value).
_ESSENTIAL_USE_CASES: frozenset[str] = frozenset(
    {
        "gatekeeper",  # all gatekeeper steps are essential
    }
)


def is_essential_call(use_case: str, step: str) -> bool:
    """
    Return True if this call should bypass the hard budget cap.

    Essential calls are never suppressed, even when the monthly budget is
    fully exhausted.  Non-essential calls raise ``BudgetExceededError`` once
    the hard cap is hit.

    Args:
        use_case: e.g. ``"gatekeeper"``, ``"UC04"``, ``"UC01"``.
        step:     e.g. ``"classification"``, ``"reply_handling"``.
    """
    if use_case in _ESSENTIAL_USE_CASES:
        return True
    return (use_case, step) in _ESSENTIAL_PAIRS


# ── Error types ───────────────────────────────────────────────────────────────


class BudgetExceededError(Exception):
    """
    Raised when the monthly AI budget hard cap is hit and the requested call
    is non-essential.

    Callers should catch this and fall back to a human-handoff message or
    skip the AI step silently.
    """


# ── BudgetStatus dataclass ────────────────────────────────────────────────────


@dataclass(frozen=True)
class BudgetStatus:
    """
    Snapshot of a location's AI budget for the current calendar month.

    Attributes:
        location_id:    UUID of the location.
        spend:          Total AI cost so far this month (USD).
        budget:         Monthly budget cap from ``locations.ai_monthly_budget_usd``.
        ratio:          ``spend / budget``.  0.0 if budget is zero.
    """

    location_id: uuid.UUID
    spend: Decimal
    budget: Decimal
    ratio: Decimal  # spend / budget (may exceed 1.0)

    @property
    def is_soft_cap_exceeded(self) -> bool:
        """True when spend ≥ 80% of budget."""
        return self.ratio >= Decimal("0.8")

    @property
    def is_hard_cap_exceeded(self) -> bool:
        """True when spend ≥ 100% of budget."""
        return self.ratio >= Decimal("1.0")

    @property
    def remaining_usd(self) -> Decimal:
        """USD remaining before the hard cap is hit (can be negative)."""
        return self.budget - self.spend

    def summary(self) -> str:
        pct = int(self.ratio * 100)
        return (
            f"AI budget: ${self.spend:.4f} / ${self.budget:.2f} "
            f"({pct}%) for location {self.location_id}"
        )


# ── Query ─────────────────────────────────────────────────────────────────────


async def get_monthly_spend(
    location_id: uuid.UUID,
    db: AsyncSession,
) -> Decimal:
    """
    Return the total ``cost_usd`` for ``location_id`` in the current calendar
    month (UTC).

    Returns ``Decimal("0")`` if there are no rows yet.
    """
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    stmt = select(
        func.coalesce(func.sum(AiUsage.cost_usd), Decimal("0"))
    ).where(
        AiUsage.location_id == location_id,
        AiUsage.ts >= month_start,
    )
    result = await db.scalar(stmt)
    return Decimal(str(result))


# ── Budget check ──────────────────────────────────────────────────────────────


async def check_budget(
    location: "Location",
    db: AsyncSession,
) -> BudgetStatus:
    """
    Compute the current budget status for a location.

    This function is PURE READ — it does not send emails or raise.  Callers
    that need enforcement should call ``assert_budget_available()`` afterwards.

    Args:
        location: ORM ``Location`` row (needs ``id`` and ``ai_monthly_budget_usd``).
        db:       Active async DB session.

    Returns:
        ``BudgetStatus`` snapshot.
    """
    spend = await get_monthly_spend(location.id, db)
    budget = Decimal(str(location.ai_monthly_budget_usd))

    if budget > 0:
        ratio = spend / budget
    else:
        ratio = Decimal("0")

    return BudgetStatus(
        location_id=location.id,
        spend=spend,
        budget=budget,
        ratio=ratio,
    )


# ── Enforcement gate ──────────────────────────────────────────────────────────


async def assert_budget_available(
    status: BudgetStatus,
    *,
    use_case: str,
    step: str,
) -> None:
    """
    Enforce budget caps before an AI call.

    - Soft cap (80%): logs a warning.  Email is sent separately by the
      budget monitor (see ``maybe_send_soft_cap_warning()``).
    - Hard cap (100%) + non-essential call: raises ``BudgetExceededError``.
    - Hard cap (100%) + essential call: logs a warning, allows through.

    Args:
        status:   Current BudgetStatus from ``check_budget()``.
        use_case: e.g. ``"UC04"``.
        step:     e.g. ``"message_generation"``.

    Raises:
        BudgetExceededError: if hard cap is hit for a non-essential call.
    """
    if status.is_soft_cap_exceeded and not status.is_hard_cap_exceeded:
        logger.warning(
            "ai.budget: soft cap exceeded for location %s — %s",
            status.location_id,
            status.summary(),
        )

    if status.is_hard_cap_exceeded:
        essential = is_essential_call(use_case, step)
        if essential:
            logger.warning(
                "ai.budget: HARD CAP exceeded for location %s but call is essential "
                "(%s/%s) — allowing through.  %s",
                status.location_id,
                use_case,
                step,
                status.summary(),
            )
        else:
            raise BudgetExceededError(
                f"Monthly AI budget exhausted for location {status.location_id}: "
                f"{status.summary()}.  Non-essential call ({use_case}/{step}) suppressed."
            )


# ── Soft-cap warning email ────────────────────────────────────────────────────


async def maybe_send_soft_cap_warning(
    status: BudgetStatus,
    *,
    owner_email: str | None,
    location_name: str = "your location",
) -> bool:
    """
    Send a soft-cap warning email to the owner if ``status.is_soft_cap_exceeded``
    and ``owner_email`` is configured.

    This is intentionally non-fatal — SMTP failures are logged but not raised.

    Args:
        status:         Current BudgetStatus.
        owner_email:    Destination email (from settings or location config).
        location_name:  Human-readable studio name for the email body.

    Returns:
        True if the email was sent, False otherwise.
    """
    if not status.is_soft_cap_exceeded:
        return False
    if not owner_email:
        logger.debug(
            "ai.budget: soft cap exceeded but no owner_email configured — skipping warning"
        )
        return False

    from app.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    pct = int(status.ratio * 100)
    subject = (
        f"[AI Budget Warning] {location_name} has used {pct}% of monthly AI budget"
    )
    body = (
        f"Studio: {location_name}\n"
        f"Location ID: {status.location_id}\n\n"
        f"AI spend this month: ${status.spend:.4f}\n"
        f"Monthly budget: ${status.budget:.2f}\n"
        f"Usage: {pct}%\n\n"
        "Non-essential AI calls will be suppressed when usage reaches 100%.\n"
        "Essential calls (inbound routing, live customer support) are always allowed.\n\n"
        "To raise the budget, update ai_monthly_budget_usd in the location settings."
    )

    if not settings.notification_smtp_host:
        logger.warning(
            "ai.budget: soft cap warning — SMTP not configured, stub only.\n"
            "  Subject: %s\n  Body preview: %s",
            subject,
            body[:200],
        )
        return False

    try:
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _send_smtp_email(
                subject=subject,
                body=body,
                to_email=owner_email,
                smtp_host=settings.notification_smtp_host,
                smtp_port=settings.notification_smtp_port,
                smtp_user=settings.notification_smtp_user,
                smtp_password=settings.notification_smtp_password,
                from_email=settings.notification_from_email,
            ),
        )
        logger.info(
            "ai.budget: soft cap warning email sent to %s for location %s",
            owner_email,
            status.location_id,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "ai.budget: failed to send soft cap warning email to %s: %s",
            owner_email,
            exc,
        )
        return False


# ── SMTP helper (sync, called via run_in_executor) ────────────────────────────


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
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email

    with smtplib.SMTP(smtp_host, smtp_port) as s:
        s.ehlo()
        if smtp_port == 587:
            s.starttls()
            s.ehlo()
        if smtp_user and smtp_password:
            s.login(smtp_user, smtp_password)
        s.sendmail(from_email, [to_email], msg.as_string())
