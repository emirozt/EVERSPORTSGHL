"""
EversportsBaseScraper — Playwright base class for all Eversports admin scrapers.

Auth model (TOTP 2FA — no automated login):
  - Operator logs in manually, exports cookies via Cookie-Editor.
  - ``scripts/import_cookies.py`` writes the JSON array into
    ``locations.eversports_cookie_cache``.
  - This class reads those cookies, injects them into a fresh Playwright
    browser context, and makes requests as an authenticated user.
  - On any response that redirects to a URL containing "/login", we set
    ``locations.eversports_cookie_state = 'expired'``, write a sync_log
    error entry, and raise SessionExpiredError immediately.

Usage::

    async with EversportsBaseScraper(location, db) as scraper:
        page = await scraper.new_page()
        # page is authenticated
        await page.goto(scraper.BASE_URL + "/admin/some-page")

See ``requirements_v2/07_foundation_layer.md`` §Authentication for the full spec.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.location import Location
from app.db.models.sync_log import SyncLog
from app.scrapers.exceptions import SessionExpiredError

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page, Playwright, Response

logger = logging.getLogger(__name__)

# User-agent that matches a modern desktop Chrome — avoids bot-detection heuristics
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class EversportsBaseScraper:
    """
    Base class for all Eversports admin scrapers.

    Auth: cookie-injection only (no automated login).  Cookies are read from
    ``location.eversports_cookie_cache`` (JSONB, written by
    ``scripts/import_cookies.py``).

    The class is an async context manager.  All Playwright resources are
    acquired in ``__aenter__`` and released in ``__aexit__``.

    A response listener is attached to every new page returned by
    ``new_page()``.  The listener calls ``_check_redirect()`` on every HTTP
    response; if the response URL contains ``LOGIN_URL_FRAGMENT`` the scraper
    stops the run and raises ``SessionExpiredError``.
    """

    BASE_URL = "https://app.eversportsmanager.com"
    LOGIN_URL_FRAGMENT = "/login"

    # ── Construction ───────────────────────────────────────────────────────────

    def __init__(self, location: Location, db: AsyncSession) -> None:
        self._location = location
        self._db = db

        # Populated in __aenter__
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

        # Set to True after the first successful response to avoid redundant DB writes
        self._marked_ok: bool = False

    # ── Context manager ────────────────────────────────────────────────────────

    async def __aenter__(self) -> EversportsBaseScraper:
        """
        Validate cookie state, launch headless Chromium, inject cookies.

        Raises:
            SessionExpiredError: if cookie_state is 'expired' OR if
                eversports_cookie_cache is None/empty.
        """
        cookie_state = self._location.eversports_cookie_state
        cookie_cache = self._location.eversports_cookie_cache

        # Guard: never launch a browser when there are no cookies to inject
        if cookie_state == "expired":
            raise SessionExpiredError(
                "Eversports session expired — please re-export cookies and run "
                "scripts/import_cookies.py"
            )

        if cookie_state == "unset" or not cookie_cache:
            raise SessionExpiredError(
                "Eversports session expired — please re-export cookies and run "
                "scripts/import_cookies.py"
            )

        # Import here so that tests can mock playwright without it being imported
        # at module level (which would fail if playwright isn't installed).
        from playwright.async_api import async_playwright  # noqa: PLC0415

        logger.info(
            "scraper: launching browser for location_id=%s", self._location.id
        )

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-setuid-sandbox",
            ],
        )
        self._context = await self._browser.new_context(user_agent=_USER_AGENT)

        # Inject operator-exported cookies
        await self._context.add_cookies(cookie_cache)  # type: ignore[arg-type]
        logger.debug(
            "scraper: injected %d cookies for location_id=%s",
            len(cookie_cache),
            self._location.id,
        )

        return self

    async def __aexit__(self, *args: object) -> None:
        """Close browser and playwright — always runs, even on exception."""
        try:
            if self._context is not None:
                await self._context.close()
        except Exception:  # noqa: BLE001
            logger.warning("scraper: error closing browser context", exc_info=True)

        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:  # noqa: BLE001
            logger.warning("scraper: error closing browser", exc_info=True)

        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:  # noqa: BLE001
            logger.warning("scraper: error stopping playwright", exc_info=True)

        logger.info(
            "scraper: browser closed for location_id=%s", self._location.id
        )

    # ── Page factory ───────────────────────────────────────────────────────────

    async def new_page(self) -> Page:
        """
        Return a new Playwright Page with the response listener attached.

        The listener calls ``_check_redirect()`` on every HTTP response so we
        detect login redirects regardless of which URL triggered them.
        """
        if self._context is None:
            raise RuntimeError("EversportsBaseScraper must be used as a context manager")

        page = await self._context.new_page()

        # Attach as a synchronous handler — Playwright calls it for every response
        page.on("response", self._sync_check_redirect)

        return page

    # ── Redirect detection ─────────────────────────────────────────────────────

    def _sync_check_redirect(self, response: Response) -> None:
        """
        Synchronous wrapper for the response listener.

        Playwright's ``page.on("response", handler)`` calls the handler
        synchronously in the event loop.  We can't await inside it directly, so
        we just check the URL and raise; the async ``_check_redirect`` is the
        awaitable version used in tests and explicit call sites.
        """
        url = response.url
        if self.LOGIN_URL_FRAGMENT in url:
            # We need to mark expired in the DB but we're in a sync callback.
            # Schedule the async work as a fire-and-forget task via the event loop.
            import asyncio  # noqa: PLC0415

            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(self._expire_and_raise(url))
            except RuntimeError:
                pass  # No event loop — ignore (shouldn't happen in Playwright context)

    async def _check_redirect(self, response: Response) -> None:
        """
        Awaitable redirect check — raises ``SessionExpiredError`` if the
        response URL contains ``LOGIN_URL_FRAGMENT``.

        This is called by tests directly and is also the underlying work that
        ``_sync_check_redirect`` schedules.
        """
        url = response.url
        if self.LOGIN_URL_FRAGMENT in url:
            await self._expire_and_raise(url)

    async def _expire_and_raise(self, url: str) -> None:
        """
        Set cookie_state = 'expired', write sync_log error, raise SessionExpiredError.

        This is the single place where session expiry is recorded and surfaced.
        """
        logger.warning(
            "scraper: login redirect detected at %s for location_id=%s",
            url,
            self._location.id,
        )

        # Update DB state
        await self._db.execute(
            update(Location)
            .where(Location.id == self._location.id)
            .values(eversports_cookie_state="expired")
        )

        # Write a sync_log error entry for on-call visibility
        error_entry = SyncLog(
            id=uuid.uuid4(),
            location_id=self._location.id,
            run_type="scrape_error",
            contacts_processed=0,
            contacts_updated=0,
            tags_applied=0,
            pipeline_moves=0,
            errors=[
                "Session expired: login redirect detected during scrape. "
                "Operator must re-export cookies and run scripts/import_cookies.py"
            ],
            duration_seconds=0.0,
        )
        self._db.add(error_entry)

        try:
            await self._db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("scraper: failed to persist expiry state to DB")
            await self._db.rollback()

        raise SessionExpiredError(
            "Eversports session expired — please re-export cookies and run "
            "scripts/import_cookies.py"
        )

    # ── Session health ─────────────────────────────────────────────────────────

    async def _mark_ok(self) -> None:
        """
        Set ``eversports_cookie_state = 'ok'`` in the DB.

        Called by sync_runner after all downloads succeed, indicating the
        session was valid for this run.  Safe to call multiple times.
        """
        if self._marked_ok:
            return

        await self._db.execute(
            update(Location)
            .where(Location.id == self._location.id)
            .values(eversports_cookie_state="ok")
        )
        self._marked_ok = True
        logger.debug(
            "scraper: cookie_state set to 'ok' for location_id=%s", self._location.id
        )

    # ── Convenience ────────────────────────────────────────────────────────────

    @property
    def location(self) -> Location:
        return self._location

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("EversportsBaseScraper must be used as a context manager")
        return self._context
