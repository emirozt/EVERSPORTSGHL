import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, DateTime, Integer, Numeric, Text, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base

_STOP_KEYWORDS_DEFAULT = text(
    r"'^(stop|stopp|aufhören|aufhoeren|abmelden|keine werbung|unsubscribe|opt out|opt-out)$'"
)

_GATEKEEPER_NOISE_DEFAULT_PG = text(
    """'{"acknowledgment":"silent_ignore","emoji_reaction":"react_emoji","""
    """"social_compliment":"react_emoji","off_topic":"silent_ignore","spam":"silent_ignore"}'::jsonb"""
)
# SQLite-safe version used by the ORM model — the migration file retains the ::jsonb cast
_GATEKEEPER_NOISE_DEFAULT = text(
    """'{"acknowledgment":"silent_ignore","emoji_reaction":"react_emoji","""
    """"social_compliment":"react_emoji","off_topic":"silent_ignore","spam":"silent_ignore"}'"""
)


class Location(Base):
    __tablename__ = "locations"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    eversports_studio_id: Mapped[str] = mapped_column(Text, nullable=False)
    eversports_location_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    ghl_subaccount_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    ghl_oauth_token_ref: Mapped[str] = mapped_column(Text, nullable=False)
    eversports_credentials_ref: Mapped[str] = mapped_column(Text, nullable=False)
    # ── Cookie-export auth (M2) ────────────────────────────────────────────────
    # Eversports admin uses TOTP 2FA — automated login is not used.
    # Operator exports cookies via Cookie-Editor → scripts/import_cookies.py → here.
    eversports_cookie_cache: Mapped[list | None] = mapped_column(  # type: ignore[type-arg]
        JSON, nullable=True
    )
    eversports_cookie_state: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'unset'"),
        default="unset",
    )
    timezone: Mapped[str] = mapped_column(Text, nullable=False)
    country: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'DE'"), default="DE"
    )
    late_cancel_window_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("24")
    )
    studio_owner_email: Mapped[str] = mapped_column(Text, nullable=False)
    studio_name: Mapped[str] = mapped_column(Text, nullable=False)
    location_name: Mapped[str] = mapped_column(Text, nullable=False)
    stop_keywords: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=_STOP_KEYWORDS_DEFAULT
    )
    ai_monthly_budget_usd: Mapped[Decimal] = mapped_column(
        Numeric, nullable=False, server_default=text("200")
    )
    renewal_handling_mode: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'studio_outreach'")
    )
    card_upsell_min_sessions_per_week: Mapped[Decimal] = mapped_column(
        Numeric, nullable=False, server_default=text("2")
    )
    gatekeeper_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    gatekeeper_confidence_threshold: Mapped[Decimal] = mapped_column(
        Numeric, nullable=False, server_default=text("0.7")
    )
    gatekeeper_noise_action: Mapped[dict] = mapped_column(  # type: ignore[type-arg]
        JSON, nullable=False, server_default=_GATEKEEPER_NOISE_DEFAULT
    )
    gatekeeper_owner_alert_categories: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'complaint,injury_medical,billing_dispute,low_confidence'"),
    )
    product_keyword_map: Mapped[dict] = mapped_column(  # type: ignore[type-arg]
        JSON, nullable=False, server_default=text("'{}'")
    )
    whatsapp_templates: Mapped[dict] = mapped_column(  # type: ignore[type-arg]
        JSON, nullable=False, server_default=text("'{}'")
    )
    consent_default_locale: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'de-AT'")
    )
    historical_sync_flag: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )

    # ── Added vs DEV_SPEC § 5 DDL (present in 07_foundation_layer.md config table) ──
    writeback_mode: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'auto_execute'")
    )
    uc05_slot_min_lead_time_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("60")
    )
    uc05_safety_margin_spots: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("2")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
