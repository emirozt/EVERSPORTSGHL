"""m6b_gatekeeper_log

Adds gatekeeper_log and ai_usage tables for M6b (Gatekeeper).

gatekeeper_log — one row per inbound message processed by the gatekeeper.
  Append-only except owner_override / override_ts columns.

ai_usage — per-call AI token + cost log (referenced by gatekeeper in M6b;
  extended by M7 AI client).

Revision ID: j5k6l7m8n9o0
Revises: i4j5k6l7m8n9
Create Date: 2026-05-29
"""

from alembic import op
import sqlalchemy as sa

revision = "j5k6l7m8n9o0"
down_revision = "i4j5k6l7m8n9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── ai_usage ──────────────────────────────────────────────────────────────
    op.create_table(
        "ai_usage",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("location_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("ghl_contact_id", sa.Text, nullable=True),
        sa.Column("use_case", sa.Text, nullable=False),
        sa.Column("step", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("prompt_tokens", sa.Integer, nullable=False),
        sa.Column("completion_tokens", sa.Integer, nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["locations.id"],
            name="fk_ai_usage_location",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("idx_ai_usage_location_ts", "ai_usage", ["location_id", "ts"])
    op.create_index("idx_ai_usage_use_case_ts", "ai_usage", ["use_case", "ts"])

    # ── gatekeeper_log ────────────────────────────────────────────────────────
    op.create_table(
        "gatekeeper_log",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("location_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("ghl_contact_id", sa.Text, nullable=True),
        sa.Column("contact_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "inbound_channel",
            sa.Text,
            sa.CheckConstraint(
                "inbound_channel IN ("
                "'whatsapp', 'email', 'instagram_dm', 'instagram_comment',"
                " 'facebook_dm', 'facebook_comment')",
                name="ck_gatekeeper_log_channel",
            ),
            nullable=False,
        ),
        sa.Column("inbound_surface", sa.Text, nullable=True),
        sa.Column("ghl_message_id", sa.Text, nullable=True),
        sa.Column("raw_text", sa.Text, nullable=False),
        sa.Column(
            "classification",
            sa.Text,
            sa.CheckConstraint(
                "classification IN ("
                "'inquiry_pricing', 'inquiry_class_info', 'inquiry_membership',"
                " 'booking', 'trial_reply', 'complaint', 'injury_medical',"
                " 'billing_dispute', 'opt_out', 'acknowledgment', 'emoji_reaction',"
                " 'social_compliment', 'off_topic', 'spam', 'low_confidence')",
                name="ck_gatekeeper_log_classification",
            ),
            nullable=False,
        ),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False),
        sa.Column(
            "route_to",
            sa.Text,
            sa.CheckConstraint(
                "route_to IN ('uc04', 'uc05', 'owner', 'noise', 'consent_gate', 'legacy')",
                name="ck_gatekeeper_log_route_to",
            ),
            nullable=False,
        ),
        sa.Column("action_taken", sa.Text, nullable=False),
        sa.Column("owner_override", sa.Text, nullable=True),
        sa.Column("override_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["locations.id"],
            name="fk_gatekeeper_log_location",
            ondelete="RESTRICT",
        ),
        # contact_id FK intentionally omitted — may arrive before contact row exists.
    )
    op.create_index("idx_gatekeeper_log_location_ts", "gatekeeper_log", ["location_id", "ts"])
    op.create_index("idx_gatekeeper_log_contact_id", "gatekeeper_log", ["contact_id"])
    op.create_index(
        "idx_gatekeeper_log_classification", "gatekeeper_log", ["classification", "ts"]
    )


def downgrade() -> None:
    op.drop_index("idx_gatekeeper_log_classification", table_name="gatekeeper_log")
    op.drop_index("idx_gatekeeper_log_contact_id", table_name="gatekeeper_log")
    op.drop_index("idx_gatekeeper_log_location_ts", table_name="gatekeeper_log")
    op.drop_table("gatekeeper_log")

    op.drop_index("idx_ai_usage_use_case_ts", table_name="ai_usage")
    op.drop_index("idx_ai_usage_location_ts", table_name="ai_usage")
    op.drop_table("ai_usage")
