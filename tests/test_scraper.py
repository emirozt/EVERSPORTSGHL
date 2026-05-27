"""
Unit tests for the M2 Eversports scraper layer.

All Playwright interactions are mocked — no real browser is launched.
These tests run in CI without Chromium installed.

Test coverage:
  1. test_session_expired_raises          — __aenter__ raises SessionExpiredError when
                                            eversports_cookie_state == 'expired'
  2. test_unset_cookies_raises            — __aenter__ raises SessionExpiredError when
                                            eversports_cookie_cache is None
  3. test_unset_state_raises              — __aenter__ raises SessionExpiredError when
                                            eversports_cookie_state == 'unset'
  4. test_login_redirect_sets_expired_state — _check_redirect marks cookie_state='expired'
                                              and raises SessionExpiredError
  5. test_sync_endpoint_returns_503_on_expired — POST /sync returns 503 when state='expired'
  6. test_sync_endpoint_returns_400_on_unset   — POST /sync returns 400 when state='unset'

See requirements_v2/07_foundation_layer.md §Authentication for the auth model.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models.base import Base
from app.db.models.location import Location

# ── Shared in-memory DB fixture ────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    """In-memory SQLite async session for scraper unit tests."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _make_location(**overrides: Any) -> Location:
    """Build a minimal Location object suitable for scraper tests."""
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "eversports_studio_id": "TestStudio",
        "ghl_subaccount_id": f"ghl-{uuid.uuid4().hex[:8]}",
        "ghl_oauth_token_ref": "secret://test/ghl",
        "eversports_credentials_ref": "secret://test/ev",
        "timezone": "Europe/Berlin",
        "studio_owner_email": "owner@test.com",
        "studio_name": "Test Studio",
        "location_name": "Test Studio — Main",
        "eversports_cookie_cache": [
            {
                "name": "eversports-manager.sid",
                "value": "abc123",
                "domain": "app.eversportsmanager.com",
                "path": "/",
            }
        ],
        "eversports_cookie_state": "ok",
    }
    defaults.update(overrides)
    return Location(**defaults)


# ── 1. test_session_expired_raises ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_expired_raises(db: AsyncSession) -> None:
    """
    EversportsBaseScraper.__aenter__ raises SessionExpiredError immediately
    when eversports_cookie_state == 'expired'.
    No browser is launched.
    """
    from app.scrapers.base import EversportsBaseScraper
    from app.scrapers.exceptions import SessionExpiredError

    location = _make_location(
        eversports_cookie_state="expired",
        eversports_cookie_cache=[{"name": "sid", "value": "x", "domain": "test.com", "path": "/"}],
    )

    scraper = EversportsBaseScraper(location, db)

    with pytest.raises(SessionExpiredError) as exc_info:
        await scraper.__aenter__()

    assert "re-export cookies" in str(exc_info.value).lower()

    # Browser was never started — nothing to clean up
    assert scraper._playwright is None
    assert scraper._browser is None


# ── 2. test_unset_cookies_raises ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unset_cookies_raises(db: AsyncSession) -> None:
    """
    EversportsBaseScraper.__aenter__ raises SessionExpiredError when
    eversports_cookie_cache is None (cookies never imported).
    """
    from app.scrapers.base import EversportsBaseScraper
    from app.scrapers.exceptions import SessionExpiredError

    location = _make_location(
        eversports_cookie_state="ok",  # state says ok but cache is empty
        eversports_cookie_cache=None,
    )

    scraper = EversportsBaseScraper(location, db)

    with pytest.raises(SessionExpiredError):
        await scraper.__aenter__()


# ── 3. test_unset_state_raises ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unset_state_raises(db: AsyncSession) -> None:
    """
    EversportsBaseScraper.__aenter__ raises SessionExpiredError when
    eversports_cookie_state == 'unset' (initial state, cookies never imported).
    """
    from app.scrapers.base import EversportsBaseScraper
    from app.scrapers.exceptions import SessionExpiredError

    location = _make_location(
        eversports_cookie_state="unset",
        eversports_cookie_cache=None,
    )

    scraper = EversportsBaseScraper(location, db)

    with pytest.raises(SessionExpiredError):
        await scraper.__aenter__()


# ── 4. test_login_redirect_sets_expired_state ─────────────────────────────────


@pytest.mark.asyncio
async def test_login_redirect_sets_expired_state(db: AsyncSession) -> None:
    """
    _check_redirect sets cookie_state='expired' and raises SessionExpiredError
    when the response URL contains '/login'.

    This simulates the scenario where the scraper has a page open, makes a
    request, and Eversports redirects to the login page because the session
    has expired mid-run.
    """
    from app.scrapers.base import EversportsBaseScraper
    from app.scrapers.exceptions import SessionExpiredError

    location = _make_location(
        eversports_cookie_state="ok",
        eversports_cookie_cache=[
            {"name": "sid", "value": "abc", "domain": "app.eversportsmanager.com", "path": "/"}
        ],
    )

    # Insert the location into the test DB so the UPDATE in _expire_and_raise
    # has something to update
    db.add(location)
    await db.flush()

    scraper = EversportsBaseScraper(location, db)

    # Build a fake Playwright Response object
    fake_response = MagicMock()
    fake_response.url = "https://app.eversportsmanager.com/login?returnUrl=%2Fadmin%2FTestStudio%2Fbookings"

    with pytest.raises(SessionExpiredError) as exc_info:
        await scraper._check_redirect(fake_response)

    assert "re-export cookies" in str(exc_info.value).lower()

    # Verify DB state was updated to 'expired'
    await db.flush()
    result = await db.execute(
        select(Location.eversports_cookie_state).where(Location.id == location.id)
    )
    state = result.scalar_one()
    assert state == "expired", f"Expected 'expired', got {state!r}"


@pytest.mark.asyncio
async def test_check_redirect_noop_for_non_login_url(db: AsyncSession) -> None:
    """
    _check_redirect is a no-op when the response URL does not contain '/login'.
    """
    from app.scrapers.base import EversportsBaseScraper

    location = _make_location()
    scraper = EversportsBaseScraper(location, db)

    fake_response = MagicMock()
    fake_response.url = "https://app.eversportsmanager.com/admin/TestStudio/bookings?export=csv"

    # Should not raise
    await scraper._check_redirect(fake_response)


# ── 5 & 6. HTTP endpoint tests (mock run_sync) ────────────────────────────────


@pytest_asyncio.fixture
async def app_with_db(db: AsyncSession) -> Any:
    """
    Build a FastAPI test app with the sync router wired in and the DB
    dependency overridden to use the in-memory SQLite session.
    """
    from fastapi import FastAPI

    from app.api.v1.admin.sync import router as sync_router
    from app.db.session import get_db

    test_app = FastAPI()
    test_app.include_router(sync_router, prefix="/api/v1/admin")

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db

    test_app.dependency_overrides[get_db] = override_get_db

    return test_app


async def _insert_location(db: AsyncSession, **overrides: Any) -> Location:
    """Insert a Location into the test DB and return it."""
    loc = _make_location(**overrides)
    db.add(loc)
    await db.flush()
    return loc


@pytest.mark.asyncio
async def test_sync_endpoint_returns_503_on_expired(
    db: AsyncSession, app_with_db: Any
) -> None:
    """
    POST /api/v1/admin/locations/{id}/sync returns 503 when
    eversports_cookie_state == 'expired'.
    """
    location = await _insert_location(
        db,
        eversports_cookie_state="expired",
        eversports_cookie_cache=[
            {"name": "sid", "value": "x", "domain": "test.com", "path": "/"}
        ],
    )

    async with AsyncClient(
        transport=ASGITransport(app=app_with_db), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/admin/locations/{location.id}/sync",
            json={"run_type": "incremental"},
        )

    assert resp.status_code == 503, resp.text
    assert "expired" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_sync_endpoint_returns_400_on_unset(
    db: AsyncSession, app_with_db: Any
) -> None:
    """
    POST /api/v1/admin/locations/{id}/sync returns 400 when
    eversports_cookie_state == 'unset' (cookies not yet imported).
    """
    location = await _insert_location(
        db,
        eversports_cookie_state="unset",
        eversports_cookie_cache=None,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app_with_db), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/admin/locations/{location.id}/sync",
            json={"run_type": "incremental"},
        )

    assert resp.status_code == 400, resp.text
    # Should mention cookie import
    detail = resp.json()["detail"].lower()
    assert "cookie" in detail or "import" in detail


@pytest.mark.asyncio
async def test_sync_endpoint_returns_404_for_unknown_location(
    db: AsyncSession, app_with_db: Any
) -> None:
    """
    POST /api/v1/admin/locations/{id}/sync returns 404 for an unknown location UUID.
    """
    fake_id = uuid.uuid4()

    async with AsyncClient(
        transport=ASGITransport(app=app_with_db), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/admin/locations/{fake_id}/sync",
            json={"run_type": "incremental"},
        )

    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_sync_endpoint_returns_422_for_invalid_run_type(
    db: AsyncSession, app_with_db: Any
) -> None:
    """
    POST /api/v1/admin/locations/{id}/sync returns 422 for an invalid run_type.
    """
    location = await _insert_location(
        db,
        eversports_cookie_state="ok",
        eversports_cookie_cache=[
            {"name": "sid", "value": "abc", "domain": "app.eversportsmanager.com", "path": "/"}
        ],
    )

    async with AsyncClient(
        transport=ASGITransport(app=app_with_db), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/admin/locations/{location.id}/sync",
            json={"run_type": "invalid_type"},
        )

    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_sync_endpoint_success_with_mocked_run_sync(
    db: AsyncSession, app_with_db: Any
) -> None:
    """
    POST /api/v1/admin/locations/{id}/sync returns 200 when run_sync succeeds.

    run_sync is mocked — no real browser or Postgres needed.
    """
    location = await _insert_location(
        db,
        eversports_cookie_state="ok",
        eversports_cookie_cache=[
            {"name": "sid", "value": "abc", "domain": "app.eversportsmanager.com", "path": "/"}
        ],
    )

    fake_result = {
        "bootstrap_run_id": str(uuid.uuid4()),
        "contacts_seeded": 5,
        "bookings_seeded": 10,
        "sessions_seeded": 3,
        "products_discovered": [],
        "contacts_missing_email": 0,
        "contacts_invalid_phone": 0,
        "warnings": [],
        "errors": [],
        "run_type": "incremental",
        "scraper_duration_seconds": 1.23,
    }

    with patch(
        "app.api.v1.admin.sync.run_sync",
        new_callable=AsyncMock,
        return_value=fake_result,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app_with_db), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/admin/locations/{location.id}/sync",
                json={"run_type": "incremental"},
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["contacts_seeded"] == 5
    assert body["run_type"] == "incremental"
    assert body["scraper_duration_seconds"] == 1.23


@pytest.mark.asyncio
async def test_sync_endpoint_propagates_session_expired_as_503(
    db: AsyncSession, app_with_db: Any
) -> None:
    """
    If run_sync raises SessionExpiredError (e.g. detected mid-run), the
    endpoint returns 503, not 500.
    """
    from app.scrapers.exceptions import SessionExpiredError

    location = await _insert_location(
        db,
        eversports_cookie_state="ok",
        eversports_cookie_cache=[
            {"name": "sid", "value": "abc", "domain": "app.eversportsmanager.com", "path": "/"}
        ],
    )

    with patch(
        "app.api.v1.admin.sync.run_sync",
        new_callable=AsyncMock,
        side_effect=SessionExpiredError(
            "Eversports session expired — please re-export cookies and run "
            "scripts/import_cookies.py"
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app_with_db), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/admin/locations/{location.id}/sync",
            )

    assert resp.status_code == 503, resp.text
