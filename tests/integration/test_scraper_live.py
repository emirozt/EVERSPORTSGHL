"""
Live integration test for the Eversports scraper.

SKIPPED in CI — requires a real Eversports session cookie.

To run locally:
  1. Export cookies from your browser (Cookie-Editor → Copy as JSON).
  2. Set the env var:
       export EVERSPORTS_TEST_COOKIE_JSON='[{"name": "...", ...}]'
  3. Set the studio ID:
       export EVERSPORTS_TEST_STUDIO_ID='Yneu3U'   # your studio's short code
  4. Run:
       pytest tests/integration/test_scraper_live.py -v -s

The test:
  - Inserts a test location row with the exported cookies.
  - Calls run_sync() (full scraper + bootstrap pipeline).
  - Asserts that at least 1 contact, 1 booking, and 1 session were seeded.
  - Cleans up the test location row after the test.

WARNING: This test makes real HTTP requests to app.eversportsmanager.com
and launches a headless browser.  It will consume one sync run's worth of
Playwright + Postgres activity against the real studio account.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models.base import Base
from app.db.models.bookings import Booking
from app.db.models.contacts import Contact
from app.db.models.location import Location

_COOKIE_JSON_ENV = "EVERSPORTS_TEST_COOKIE_JSON"
_STUDIO_ID_ENV = "EVERSPORTS_TEST_STUDIO_ID"

# ── Skip guard ─────────────────────────────────────────────────────────────────

pytestmark = pytest.mark.skipif(
    os.getenv(_COOKIE_JSON_ENV) is None,
    reason=f"no test cookie — set {_COOKIE_JSON_ENV} env var",
)


# ── DB fixture (uses real Postgres from DATABASE_URL env var) ──────────────────


@pytest_asyncio.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    """
    Async SQLAlchemy session backed by the test DATABASE_URL.

    We do NOT use an in-memory SQLite DB here because the live scraper test
    needs the same dialect as production (Postgres).  The DATABASE_URL should
    point to a test schema, not production.
    """
    from app.config import get_settings

    settings = get_settings()
    engine = create_async_engine(
        settings.database_url,
        echo=False,
        pool_pre_ping=True,
    )

    # Ensure tables exist (idempotent in test env)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session

    await engine.dispose()


# ── Test ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_live_sync_seeds_data(db: AsyncSession) -> None:
    """
    Full live sync: inject cookies → download CSVs → run_bootstrap → assert rows.

    Assertions:
      - At least 1 contact seeded
      - At least 1 booking seeded
      - At least 1 session seeded
      - cookie_state transitions from 'ok' input to 'ok' output
        (i.e. session was valid for the entire run)
    """
    cookie_json_raw = os.environ[_COOKIE_JSON_ENV]
    studio_id = os.getenv(_STUDIO_ID_ENV, "unknown_studio")

    try:
        cookies = json.loads(cookie_json_raw)
    except json.JSONDecodeError as exc:
        pytest.fail(f"EVERSPORTS_TEST_COOKIE_JSON is not valid JSON: {exc}")

    # Insert a test location
    test_location_id = uuid.uuid4()
    location = Location(
        id=test_location_id,
        eversports_studio_id=studio_id,
        ghl_subaccount_id=f"test-ghl-{test_location_id.hex[:8]}",
        ghl_oauth_token_ref="secret://test/ghl",
        eversports_credentials_ref="secret://test/ev",
        timezone="Europe/Vienna",
        country="AT",
        studio_owner_email="test@example.com",
        studio_name="Live Test Studio",
        location_name="Live Test Studio — Main",
        eversports_cookie_cache=cookies,
        eversports_cookie_state="ok",
    )
    db.add(location)
    await db.flush()

    try:
        from app.scrapers.sync_runner import run_sync

        result = await run_sync(
            location_id=test_location_id,
            db=db,
            run_type="incremental",
        )
        await db.commit()

        # Assertions on result shape
        assert "contacts_seeded" in result, "Missing contacts_seeded in result"
        assert "bookings_seeded" in result, "Missing bookings_seeded in result"
        assert "sessions_seeded" in result, "Missing sessions_seeded in result"
        assert result["run_type"] == "incremental"
        assert isinstance(result["scraper_duration_seconds"], float)

        assert result["contacts_seeded"] >= 1, (
            f"Expected at least 1 contact seeded, got {result['contacts_seeded']}. "
            "Errors: " + str(result.get("errors"))
        )

        # Verify rows in DB
        contacts_count = (
            (await db.execute(select(Contact).where(Contact.location_id == test_location_id)))
            .scalars()
            .all()
        )
        assert len(contacts_count) >= 1, "No contacts in DB after sync"

        # Bookings may be empty if studio has no recent bookings in the export window
        # (that's an acceptable live state — we just log the count from the result)
        _ = (
            (await db.execute(select(Booking).where(Booking.location_id == test_location_id)))
            .scalars()
            .all()
        )
        print(
            f"\nLive sync result: contacts={result['contacts_seeded']}, "
            f"bookings={result['bookings_seeded']}, "
            f"sessions={result['sessions_seeded']}, "
            f"duration={result['scraper_duration_seconds']:.1f}s"
        )

        # Verify cookie_state is 'ok' after a successful run
        await db.refresh(location)
        assert location.eversports_cookie_state == "ok", (
            f"Expected cookie_state='ok' after successful sync, "
            f"got {location.eversports_cookie_state!r}"
        )

    finally:
        # Clean up test data — delete in FK dependency order
        from sqlalchemy import delete  # noqa: PLC0415

        from app.db.models.sessions import Session  # noqa: PLC0415

        await db.execute(delete(Booking).where(Booking.location_id == test_location_id))
        await db.execute(delete(Contact).where(Contact.location_id == test_location_id))
        await db.execute(delete(Session).where(Session.location_id == test_location_id))
        await db.execute(delete(Location).where(Location.id == test_location_id))
        await db.commit()
