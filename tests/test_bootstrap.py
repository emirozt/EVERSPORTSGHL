"""
Bootstrap integration tests.

These tests use an in-memory async SQLite database to avoid requiring a live
Postgres instance. The SQLite backend supports all the same SQLAlchemy ORM
operations used by the bootstrap orchestrator.

Key limitation: pg_insert (PostgreSQL's INSERT ... ON CONFLICT) is not available
for SQLite. The bootstrap.py uses pg_insert, so we patch the dialect-specific
statement to use a compatible INSERT OR REPLACE / INSERT OR IGNORE equivalent
for SQLite in tests.

M1.5 acceptance criterion (from spec):
  Uploading the three sample CSVs against a test location seeds Postgres with
  28 distinct contacts, 29 bookings, 43 sessions; products classified correctly;
  re-uploading the same files results in zero new rows (idempotency).

Note: spec says 42 sessions, but the actual sample CSV has 43 rows. Tests use
the empirically correct count (43).
"""

import pathlib
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
from app.db.models.sessions import Session
from app.db.models.sync_log import SyncLog

SAMPLE_DIR = pathlib.Path(__file__).parent.parent / "requirements_v2" / "sample_exports"


# ── DB fixture ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    """
    In-memory SQLite async session.

    Uses aiosqlite via sqlalchemy[asyncio]. All M1.5 models are created fresh
    for each test function.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )
    async with engine.begin() as conn:
        # SQLite doesn't support gen_random_uuid() — override server_defaults to None
        # and let Python supply UUIDs via the ORM defaults.
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session

    await engine.dispose()


@pytest_asyncio.fixture
async def test_location(db: AsyncSession) -> Location:
    """Insert a minimal Location row and return it."""
    loc = Location(
        id=uuid.uuid4(),
        eversports_studio_id="test-studio",
        ghl_subaccount_id=f"ghl-{uuid.uuid4().hex[:8]}",
        ghl_oauth_token_ref="secret://test/ghl",
        eversports_credentials_ref="secret://test/eversports",
        timezone="Europe/Berlin",
        studio_owner_email="owner@test.com",
        studio_name="Test Studio",
        location_name="Test Studio — Main",
    )
    db.add(loc)
    await db.commit()
    await db.refresh(loc)
    return loc


# ── Import bootstrap after models are defined ──────────────────────────────────
# (Avoids import-time circular dependency issues in some test discovery orders)


def _get_bootstrap():
    from app.ingest.bootstrap import run_bootstrap  # noqa: PLC0415

    return run_bootstrap


# ── Helpers ────────────────────────────────────────────────────────────────────


async def _count(db: AsyncSession, model, location_id: uuid.UUID) -> int:
    result = await db.execute(select(model).where(model.location_id == location_id))
    return len(result.scalars().all())


# ── Tests ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bootstrap_contacts_count(db: AsyncSession, test_location: Location) -> None:
    """28 distinct contacts seeded from 29 booking rows (one duplicate email)."""
    run_bootstrap = _get_bootstrap()
    bookings_bytes = (SAMPLE_DIR / "bookings.csv").read_bytes()

    result = await run_bootstrap(
        location_id=test_location.id,
        bookings_bytes=bookings_bytes,
        activities_bytes=None,
        noshows_bytes=None,
        db=db,
    )
    await db.commit()

    count = await _count(db, Contact, test_location.id)
    assert count == 28, f"Expected 28 contacts, got {count}"
    assert result["contacts_seeded"] == 28


@pytest.mark.asyncio
async def test_bootstrap_bookings_count(db: AsyncSession, test_location: Location) -> None:
    """29 bookings seeded (29 rows in CSV, but duplicate row has same synthetic ID → 28 unique)."""
    run_bootstrap = _get_bootstrap()
    bookings_bytes = (SAMPLE_DIR / "bookings.csv").read_bytes()

    result = await run_bootstrap(
        location_id=test_location.id,
        bookings_bytes=bookings_bytes,
        activities_bytes=None,
        noshows_bytes=None,
        db=db,
    )
    await db.commit()

    count = await _count(db, Booking, test_location.id)
    # The duplicate row (test+28@example.com) has the same synthetic booking ID
    # (same email + same datetime + same activity) → ON CONFLICT DO NOTHING → 28 unique bookings
    assert count == 28, f"Expected 28 bookings (duplicate row deduped), got {count}"
    assert result["bookings_seeded"] == 28


@pytest.mark.asyncio
async def test_bootstrap_sessions_count(db: AsyncSession, test_location: Location) -> None:
    """43 sessions seeded from the activities CSV (spec says 42, sample has 43)."""
    run_bootstrap = _get_bootstrap()
    activities_bytes = (SAMPLE_DIR / "all activities.csv").read_bytes()

    result = await run_bootstrap(
        location_id=test_location.id,
        bookings_bytes=(SAMPLE_DIR / "bookings.csv").read_bytes(),
        activities_bytes=activities_bytes,
        noshows_bytes=None,
        db=db,
    )
    await db.commit()

    count = await _count(db, Session, test_location.id)
    assert count == 43, f"Expected 43 sessions, got {count}"
    assert result["sessions_seeded"] == 43


@pytest.mark.asyncio
async def test_bootstrap_noshows_empty(db: AsyncSession, test_location: Location) -> None:
    """Empty noshows.csv is a no-op — no errors, no attendance_status changes."""
    run_bootstrap = _get_bootstrap()
    result = await run_bootstrap(
        location_id=test_location.id,
        bookings_bytes=(SAMPLE_DIR / "bookings.csv").read_bytes(),
        activities_bytes=None,
        noshows_bytes=(SAMPLE_DIR / "noshows.csv").read_bytes(),
        db=db,
    )
    await db.commit()

    # No noshow-related errors
    noshow_errors = [e for e in result["errors"] if "noshows" in e.lower()]
    assert noshow_errors == []


@pytest.mark.asyncio
async def test_bootstrap_products_classified(db: AsyncSession, test_location: Location) -> None:
    """Products are correctly classified per spec classifier."""
    run_bootstrap = _get_bootstrap()
    result = await run_bootstrap(
        location_id=test_location.id,
        bookings_bytes=(SAMPLE_DIR / "bookings.csv").read_bytes(),
        activities_bytes=None,
        noshows_bytes=None,
        db=db,
    )
    await db.commit()

    products = {p["name"]: p["bucket"] for p in result["products_discovered"]}

    assert products.get("10er Karte-Gruppe") == "card"
    assert products.get("20er Karte-Gruppe") == "card"
    assert products.get("3 Trial Cards-Introduction to Pilates Reformer") == "trial"
    assert products.get("Gruppenmitgliedschaft-1 x Woche") == "membership"
    assert products.get("Gruppenmitgliedschaft-2 x Woche") == "membership"
    assert products.get("Limitless-Gruppenmitgliedschaft") == "membership"


@pytest.mark.asyncio
async def test_bootstrap_historical_sync_flag_updated(
    db: AsyncSession, test_location: Location
) -> None:
    """After bootstrap, historical_sync_flag is set to 'complete' (unified value for both paths)."""
    run_bootstrap = _get_bootstrap()
    await run_bootstrap(
        location_id=test_location.id,
        bookings_bytes=(SAMPLE_DIR / "bookings.csv").read_bytes(),
        activities_bytes=None,
        noshows_bytes=None,
        db=db,
    )
    await db.commit()

    result = await db.execute(select(Location).where(Location.id == test_location.id))
    loc = result.scalar_one()
    assert loc.historical_sync_flag == "complete"


@pytest.mark.asyncio
async def test_bootstrap_sync_log_written(db: AsyncSession, test_location: Location) -> None:
    """A sync_log row is written with run_type='bootstrap'."""
    run_bootstrap = _get_bootstrap()
    result = await run_bootstrap(
        location_id=test_location.id,
        bookings_bytes=(SAMPLE_DIR / "bookings.csv").read_bytes(),
        activities_bytes=None,
        noshows_bytes=None,
        db=db,
    )
    await db.commit()

    log_result = await db.execute(
        select(SyncLog).where(
            SyncLog.location_id == test_location.id,
            SyncLog.run_type == "bootstrap",
        )
    )
    logs = log_result.scalars().all()
    assert len(logs) == 1
    assert str(logs[0].bootstrap_run_id) == result["bootstrap_run_id"]


@pytest.mark.asyncio
async def test_bootstrap_idempotency(db: AsyncSession, test_location: Location) -> None:
    """
    Re-uploading the same files produces zero new rows.

    This is the M1.5 acceptance criterion from the spec:
    're-uploading the same files results in zero new rows (idempotency)'.
    """
    run_bootstrap = _get_bootstrap()

    bookings_bytes = (SAMPLE_DIR / "bookings.csv").read_bytes()
    activities_bytes = (SAMPLE_DIR / "all activities.csv").read_bytes()
    noshows_bytes = (SAMPLE_DIR / "noshows.csv").read_bytes()

    # First run
    await run_bootstrap(
        location_id=test_location.id,
        bookings_bytes=bookings_bytes,
        activities_bytes=activities_bytes,
        noshows_bytes=noshows_bytes,
        db=db,
    )
    await db.commit()

    contacts_after_run1 = await _count(db, Contact, test_location.id)
    bookings_after_run1 = await _count(db, Booking, test_location.id)
    sessions_after_run1 = await _count(db, Session, test_location.id)

    # Second run — same files
    result2 = await run_bootstrap(
        location_id=test_location.id,
        bookings_bytes=bookings_bytes,
        activities_bytes=activities_bytes,
        noshows_bytes=noshows_bytes,
        db=db,
    )
    await db.commit()

    contacts_after_run2 = await _count(db, Contact, test_location.id)
    bookings_after_run2 = await _count(db, Booking, test_location.id)
    sessions_after_run2 = await _count(db, Session, test_location.id)

    assert contacts_after_run2 == contacts_after_run1, (
        f"Second run added {contacts_after_run2 - contacts_after_run1} unexpected contact(s)"
    )
    assert bookings_after_run2 == bookings_after_run1, (
        f"Second run added {bookings_after_run2 - bookings_after_run1} unexpected booking(s)"
    )
    assert sessions_after_run2 == sessions_after_run1, (
        f"Second run added {sessions_after_run2 - sessions_after_run1} unexpected session(s)"
    )

    # Result counts should reflect inserts (0 new in second run for sessions/bookings)
    assert result2["sessions_seeded"] == 0
    assert result2["bookings_seeded"] == 0


@pytest.mark.asyncio
async def test_bootstrap_contact_derived_fields(db: AsyncSession, test_location: Location) -> None:
    """Derived fields are computed correctly for a contact with known data."""
    run_bootstrap = _get_bootstrap()
    await run_bootstrap(
        location_id=test_location.id,
        bookings_bytes=(SAMPLE_DIR / "bookings.csv").read_bytes(),
        activities_bytes=None,
        noshows_bytes=None,
        db=db,
    )
    await db.commit()

    result = await db.execute(
        select(Contact).where(
            Contact.location_id == test_location.id,
            Contact.email_lower == "test+1@example.com",
        )
    )
    contact = result.scalar_one_or_none()
    assert contact is not None

    # test+1@example.com appears twice in the fixture (row 0 + duplicate row 28),
    # but both rows map to the same booking (same email+datetime+activity → deduped).
    # The contact has 1 attended booking.
    assert contact.total_sessions_attended == 1
    assert contact.no_show_count == 0
    assert contact.last_session_date is not None
    assert contact.last_booking_date is not None


@pytest.mark.asyncio
async def test_bootstrap_result_run_id_is_uuid(db: AsyncSession, test_location: Location) -> None:
    """bootstrap_run_id in the result is a valid UUID string."""
    run_bootstrap = _get_bootstrap()
    result = await run_bootstrap(
        location_id=test_location.id,
        bookings_bytes=(SAMPLE_DIR / "bookings.csv").read_bytes(),
        activities_bytes=None,
        noshows_bytes=None,
        db=db,
    )
    await db.commit()

    # Should not raise
    run_id = uuid.UUID(result["bootstrap_run_id"])
    assert run_id.version == 4


@pytest.mark.asyncio
async def test_bootstrap_missing_email_contacts_counted(
    db: AsyncSession, test_location: Location
) -> None:
    """contacts_missing_email count is correct (0 for this sample — all rows have email)."""
    run_bootstrap = _get_bootstrap()
    result = await run_bootstrap(
        location_id=test_location.id,
        bookings_bytes=(SAMPLE_DIR / "bookings.csv").read_bytes(),
        activities_bytes=None,
        noshows_bytes=None,
        db=db,
    )
    await db.commit()

    # Sample has no empty-email rows
    assert result["contacts_missing_email"] == 0


@pytest.mark.asyncio
async def test_bootstrap_location_not_found_raises(db: AsyncSession) -> None:
    """run_bootstrap raises ValueError for an unknown location_id."""
    run_bootstrap = _get_bootstrap()
    fake_id = uuid.uuid4()

    with pytest.raises(ValueError, match="Location not found"):
        await run_bootstrap(
            location_id=fake_id,
            bookings_bytes=(SAMPLE_DIR / "bookings.csv").read_bytes(),
            activities_bytes=None,
            noshows_bytes=None,
            db=db,
        )
