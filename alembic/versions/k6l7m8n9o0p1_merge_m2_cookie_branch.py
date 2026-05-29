"""merge_m2_cookie_branch

Merges the M2 cookie-cache branch (d4e5f6a7b8c9) with the main chain
that runs through M3→M4→M5→M6→M6b (j5k6l7m8n9o0).

Background:
  d4e5f6a7b8c9 (add_cookie_cache_to_locations) was written as a sibling
  of e5f6a7b8c9d0 (m3_ghl_sync_columns), both descending from c3d4e5f6a7b8.
  The main chain picked up e5f6a7b8c9d0 and continued; d4e5f6a7b8c9 was
  never merged back, leaving two heads.  This no-op migration unifies them
  so `alembic upgrade heads` can run cleanly.

Revision ID: k6l7m8n9o0p1
Revises: d4e5f6a7b8c9, j5k6l7m8n9o0
Create Date: 2026-05-29
"""

from alembic import op

revision = "k6l7m8n9o0p1"
down_revision = ("d4e5f6a7b8c9", "j5k6l7m8n9o0")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass  # both branches already applied independently


def downgrade() -> None:
    pass
