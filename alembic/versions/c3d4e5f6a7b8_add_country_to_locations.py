"""add country to locations

Revision ID: c3d4e5f6a7b8
Revises: a1b2c3d4e5f6
Create Date: 2026-05-27

Adds `country` TEXT NOT NULL DEFAULT 'DE' to the locations table.

Rationale: the phone normaliser (app/ingest/normaliser.py) spec requires
`default_region` to come from `location.country` (DE/AT/CH). The initial
scaffold used a timezone→region heuristic as a workaround, but that left
the model out of sync with 07_foundation_layer.md § "Configuration (per
location)". This migration adds the canonical column with a safe DACH
default ('DE') so the normaliser can read it directly.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "locations",
        sa.Column(
            "country",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'DE'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("locations", "country")
