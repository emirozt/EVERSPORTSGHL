"""add cookie cache columns to locations

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-05-27

Adds two columns supporting the M2 cookie-export auth model.

  eversports_cookie_cache  JSONB  nullable
    Cookie-Editor JSON export. Injected by scripts/import_cookies.py.
    NULL means cookies have not been imported yet for this location.

  eversports_cookie_state  TEXT  NOT NULL  DEFAULT 'unset'
    Scraper session state: 'unset' | 'ok' | 'expired'
    - 'unset'   : no cookies imported; scraper skips the location
    - 'ok'      : last run authenticated successfully
    - 'expired' : last run received a login redirect; operator must
                  re-export and re-import cookies

See 07_foundation_layer.md § Authentication for the full model.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "locations",
        sa.Column(
            "eversports_cookie_cache",
            postgresql.JSONB(),
            nullable=True,
        ),
    )
    op.add_column(
        "locations",
        sa.Column(
            "eversports_cookie_state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'unset'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("locations", "eversports_cookie_state")
    op.drop_column("locations", "eversports_cookie_cache")
