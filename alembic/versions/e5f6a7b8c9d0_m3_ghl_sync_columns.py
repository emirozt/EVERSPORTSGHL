"""M3: add GHL sync columns to contacts and locations.

Adds to ``contacts``:
  - ``ghl_prev_state``      JSONB — snapshot of last-pushed GHL custom field values
  - ``ghl_tag_timestamps``  JSONB — map of tag_name → ISO timestamp when applied
  - ``ghl_last_synced_at``  TIMESTAMPTZ — when this contact was last pushed to GHL
  - ``converted_package_name`` TEXT — first non-trial package (UC02 canonical field)
  - ``conversion_date``     DATE — when trial conversion was detected
  - ``conversion_source``   TEXT — "chatbot" | "direct" | "in-studio"

Adds to ``locations``:
  - ``ghl_oauth_token_cache`` JSONB — per-location OAuth tokens
      {access_token, refresh_token, token_type, expires_at (ISO)}

Revision ID: e5f6a7b8c9d0
Revises: c3d4e5f6a7b8
Create Date: 2026-05-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e5f6a7b8c9d0"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── contacts: GHL sync fields ──────────────────────────────────────────────
    op.add_column(
        "contacts",
        sa.Column("ghl_prev_state", sa.JSON(), nullable=True),
    )
    op.add_column(
        "contacts",
        sa.Column("ghl_tag_timestamps", sa.JSON(), nullable=True),
    )
    op.add_column(
        "contacts",
        sa.Column(
            "ghl_last_synced_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "contacts",
        sa.Column("converted_package_name", sa.Text(), nullable=True),
    )
    op.add_column(
        "contacts",
        sa.Column("conversion_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "contacts",
        sa.Column("conversion_source", sa.Text(), nullable=True),
    )

    # ── locations: GHL OAuth token cache ──────────────────────────────────────
    op.add_column(
        "locations",
        sa.Column("ghl_oauth_token_cache", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("contacts", "ghl_prev_state")
    op.drop_column("contacts", "ghl_tag_timestamps")
    op.drop_column("contacts", "ghl_last_synced_at")
    op.drop_column("contacts", "converted_package_name")
    op.drop_column("contacts", "conversion_date")
    op.drop_column("contacts", "conversion_source")
    op.drop_column("locations", "ghl_oauth_token_cache")
