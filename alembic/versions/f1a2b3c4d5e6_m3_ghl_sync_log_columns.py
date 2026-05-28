"""M3 (fix): add GHL push counters to sync_log.

Adds to ``sync_log``:
  - ``ghl_contacts_synced``   INT DEFAULT 0 — total contacts pushed to GHL
  - ``ghl_contacts_created``  INT DEFAULT 0 — new GHL contacts created
  - ``ghl_contacts_failed``   INT DEFAULT 0 — contacts that failed GHL push

Revision ID: f1a2b3c4d5e6
Revises: e5f6a7b8c9d0
Create Date: 2026-05-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f1a2b3c4d5e6"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sync_log",
        sa.Column(
            "ghl_contacts_synced",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "sync_log",
        sa.Column(
            "ghl_contacts_created",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "sync_log",
        sa.Column(
            "ghl_contacts_failed",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("sync_log", "ghl_contacts_synced")
    op.drop_column("sync_log", "ghl_contacts_created")
    op.drop_column("sync_log", "ghl_contacts_failed")
