"""initial locations table

Revision ID: d3f8b2a4c1e9
Revises:
Create Date: 2026-05-26

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d3f8b2a4c1e9"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STOP_KEYWORDS_DEFAULT = (
    r"'^(stop|stopp|aufhören|aufhoeren|abmelden|keine werbung|unsubscribe|opt out|opt-out)$'"
)
_GATEKEEPER_NOISE_DEFAULT = (
    '\'{"acknowledgment":"silent_ignore","emoji_reaction":"react_emoji",'
    '"social_compliment":"react_emoji","off_topic":"silent_ignore","spam":"silent_ignore"}\'::jsonb'
)


def upgrade() -> None:
    op.create_table(
        "locations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("eversports_studio_id", sa.Text(), nullable=False),
        sa.Column("eversports_location_id", sa.Text(), nullable=True),
        sa.Column("ghl_subaccount_id", sa.Text(), nullable=False, unique=True),
        sa.Column("ghl_oauth_token_ref", sa.Text(), nullable=False),
        sa.Column("eversports_credentials_ref", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column(
            "late_cancel_window_hours",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("24"),
        ),
        sa.Column("studio_owner_email", sa.Text(), nullable=False),
        sa.Column("studio_name", sa.Text(), nullable=False),
        sa.Column("location_name", sa.Text(), nullable=False),
        sa.Column(
            "stop_keywords",
            sa.Text(),
            nullable=False,
            server_default=sa.text(_STOP_KEYWORDS_DEFAULT),
        ),
        sa.Column(
            "ai_monthly_budget_usd",
            sa.Numeric(),
            nullable=False,
            server_default=sa.text("200"),
        ),
        sa.Column(
            "renewal_handling_mode",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'studio_outreach'"),
        ),
        sa.Column(
            "card_upsell_min_sessions_per_week",
            sa.Numeric(),
            nullable=False,
            server_default=sa.text("2"),
        ),
        sa.Column(
            "gatekeeper_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "gatekeeper_confidence_threshold",
            sa.Numeric(),
            nullable=False,
            server_default=sa.text("0.7"),
        ),
        sa.Column(
            "gatekeeper_noise_action",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text(_GATEKEEPER_NOISE_DEFAULT),
        ),
        sa.Column(
            "gatekeeper_owner_alert_categories",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'complaint,injury_medical,billing_dispute,low_confidence'"),
        ),
        sa.Column(
            "product_keyword_map",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "whatsapp_templates",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "consent_default_locale",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'de-AT'"),
        ),
        sa.Column(
            "historical_sync_flag",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        # ── Columns present in 07_foundation_layer.md config table but absent ──
        # ── from DEV_SPEC § 5 DDL — included here per M1 assumption #1        ──
        sa.Column(
            "writeback_mode",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'auto_execute'"),
        ),
        sa.Column(
            "uc05_slot_min_lead_time_minutes",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("60"),
        ),
        sa.Column(
            "uc05_safety_margin_spots",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("2"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("locations")
