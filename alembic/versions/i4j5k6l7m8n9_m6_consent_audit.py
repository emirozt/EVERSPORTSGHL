"""m6_consent_audit

Adds the consent_audit table for M6 (consent layer + opt-out).

Append-only. No UPDATE / DELETE triggers — enforced at application layer.
Postgres CHECK constraints prevent invalid channel / event / actor values.

Revision ID: i4j5k6l7m8n9
Revises: h3i4j5k6l7m8
Create Date: 2026-05-29
"""

from alembic import op
import sqlalchemy as sa

revision = "i4j5k6l7m8n9"
down_revision = "h3i4j5k6l7m8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "consent_audit",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("ghl_contact_id", sa.Text, nullable=False),
        sa.Column("contact_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("location_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "channel",
            sa.Text,
            sa.CheckConstraint(
                "channel IN ('email', 'whatsapp', 'voice')",
                name="ck_consent_audit_channel",
            ),
            nullable=False,
        ),
        sa.Column(
            "event",
            sa.Text,
            sa.CheckConstraint(
                "event IN ('granted', 'revoked', 'blocked-send', 'preference-centre-update')",
                name="ck_consent_audit_event",
            ),
            nullable=False,
        ),
        sa.Column("value", sa.Boolean, nullable=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "actor",
            sa.Text,
            sa.CheckConstraint(
                "actor IN ('system', 'customer', 'studio-staff')",
                name="ck_consent_audit_actor",
            ),
            nullable=False,
        ),
        sa.Column("message_shown", sa.Text, nullable=True),
        sa.Column("ip", sa.Text, nullable=True),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["locations.id"],
            name="fk_consent_audit_location",
            ondelete="RESTRICT",
        ),
        # Note: contact_id FK omitted intentionally — events may arrive before
        # the contact row is created (e.g. STOP keyword from unknown number).
    )

    op.create_index("idx_consent_audit_ghl_contact_ts", "consent_audit", ["ghl_contact_id", "ts"])
    op.create_index("idx_consent_audit_location_ts", "consent_audit", ["location_id", "ts"])
    op.create_index("idx_consent_audit_contact_id", "consent_audit", ["contact_id"])


def downgrade() -> None:
    op.drop_index("idx_consent_audit_contact_id", table_name="consent_audit")
    op.drop_index("idx_consent_audit_location_ts", table_name="consent_audit")
    op.drop_index("idx_consent_audit_ghl_contact_ts", table_name="consent_audit")
    op.drop_table("consent_audit")
