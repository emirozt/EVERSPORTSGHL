"""
AdminCsvDownloader — downloads the Eversports admin CSV exports.

Each method navigates to an admin export URL using a Playwright Page from
EversportsBaseScraper, waits for the download (or direct response body), and
returns the raw bytes.

URL patterns (discovered from admin panel + spec §Layer 1 Read sources):

  Bookings (booking list CSV):
    GET /admin/{studio_id}/bookings?export=csv
    → Returns the bookings list CSV (English headers, semicolon-delimited, quoted).
      Corresponds to the ``bookings.csv`` sample export.

  Activities (all activities CSV):
    GET /admin/{studio_id}/activities?export=csv
    → Returns the activities export (German headers, semicolon-delimited, unquoted).
      Corresponds to the ``all activities.csv`` sample export.

  Active appointments:
    GET /admin/{studio_id}/bookings?export=active
    → Filtered bookings export — today's/upcoming active appointments only.

  No-shows:
    GET /admin/{studio_id}/bookings?export=no-show-all
    → NOTE: per spec v2, the no-show export is NOT used (UC03 removed in v2).
      The download_noshows method is retained for completeness but callers
      should not invoke it in normal sync runs.

Download behaviour:
  Some Eversports export buttons trigger a file download (Content-Disposition:
  attachment) rather than rendering the CSV inline.  Playwright's
  ``page.expect_download()`` context manager is used to capture these.
  For endpoints that respond with inline CSV bodies, we fall back to reading
  the response body directly via page.goto() + page.content().

  Both patterns are handled per-method based on observed behaviour.

IMPORTANT — admin URL detection:
  The ``eversports_studio_id`` in the locations table is the short company code
  that appears in the Eversports admin URL (e.g. "Yneu3U").  All admin export
  URLs are under ``/admin/{studio_id}/...``.

References:
  - PoC scraper.js §detectFacilityInfo — uses /admin/{companyId}/classes
  - spec 07_foundation_layer.md §Layer 1 Read sources
  - sample_exports/ — ground-truth CSV shapes
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page

from app.db.models.location import Location
from app.scrapers.base import EversportsBaseScraper

logger = logging.getLogger(__name__)

# Default timeout for navigation + download in milliseconds
_NAV_TIMEOUT_MS = 30_000
_DOWNLOAD_TIMEOUT_MS = 60_000

# Content-type snippets that indicate a direct CSV response body
_CSV_CONTENT_TYPES = ("text/csv", "application/csv", "text/plain", "application/octet-stream")


class AdminCsvDownloader:
    """
    Downloads Eversports admin CSV exports.

    Each method accepts nothing (uses the scraper's browser context internally)
    and returns the raw ``bytes`` of the CSV file.

    Raises:
        RuntimeError: if the download fails or the response is empty.
        SessionExpiredError: if a login redirect is detected mid-download
            (raised by the scraper's response listener).
    """

    def __init__(self, scraper: EversportsBaseScraper, location: Location) -> None:
        self._scraper = scraper
        self._location = location

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _build_admin_url(self, path: str) -> str:
        """Construct a full admin URL for the given path."""
        studio_id = self._location.eversports_studio_id
        return f"{self._scraper.BASE_URL}/admin/{studio_id}/{path}"

    async def _download_csv_via_export_button(
        self,
        page: Page,
        export_url: str,
        *,
        label: str,
    ) -> bytes:
        """
        Navigate to ``export_url``.

        Two behaviours are possible:
        1. The URL directly returns a CSV response body (most common for
           Eversports export endpoints when called with ``?export=csv``).
           Detected by content-type header.
        2. The URL triggers a file download (Content-Disposition: attachment).
           Detected by the download event.

        We use ``expect_download()`` as the primary strategy because it handles
        both: a download event fires for attachment responses, and for inline
        responses we fall back to reading response.body() from the navigation.
        """
        logger.info("csv_downloader: fetching %s [%s]", export_url, label)

        # Strategy 1: use expect_download() — handles Content-Disposition: attachment
        try:
            async with page.expect_download(timeout=_DOWNLOAD_TIMEOUT_MS) as download_info:
                response = await page.goto(
                    export_url,
                    timeout=_NAV_TIMEOUT_MS,
                    wait_until="commit",  # don't wait for full page load — CSV is streamed
                )

            download = await download_info.value
            download_path = await download.path()

            if download_path is None:
                failure = download.failure()
                raise RuntimeError(
                    f"csv_downloader: download path is None for {label}; "
                    f"failure={failure}"
                )

            with open(download_path, "rb") as fh:
                data = fh.read()

            if not data:
                raise RuntimeError(
                    f"csv_downloader: downloaded file is empty for {label}"
                )

            logger.info(
                "csv_downloader: downloaded %d bytes via download event [%s]",
                len(data),
                label,
            )
            return data

        except Exception as download_exc:
            # Strategy 2: the response body was served inline (no download event).
            # Re-navigate and read the raw body.
            logger.debug(
                "csv_downloader: download event not fired for %s (%s), "
                "falling back to inline body",
                label,
                download_exc,
            )

        # Fallback: direct navigation, read body from response
        response = await page.goto(
            export_url,
            timeout=_NAV_TIMEOUT_MS,
            wait_until="commit",
        )

        if response is None:
            raise RuntimeError(
                f"csv_downloader: no response for {export_url} [{label}]"
            )

        status = response.status
        if status not in (200, 204):
            raise RuntimeError(
                f"csv_downloader: HTTP {status} for {export_url} [{label}]"
            )

        content_type = response.headers.get("content-type", "")
        if not any(ct in content_type for ct in _CSV_CONTENT_TYPES):
            # Check if it's a login page (additional guard on top of the listener)
            if "/login" in page.url:
                from app.scrapers.exceptions import SessionExpiredError  # noqa: PLC0415

                raise SessionExpiredError(
                    "Eversports session expired — please re-export cookies and run "
                    "scripts/import_cookies.py"
                )
            logger.warning(
                "csv_downloader: unexpected content-type '%s' for %s [%s]",
                content_type,
                export_url,
                label,
            )

        data = await response.body()

        if not data:
            raise RuntimeError(
                f"csv_downloader: response body is empty for {export_url} [{label}]"
            )

        logger.info(
            "csv_downloader: received %d bytes via inline response [%s]",
            len(data),
            label,
        )
        return data

    # ── Public download methods ────────────────────────────────────────────────

    async def download_bookings(self) -> bytes:
        """
        Download the booking list CSV export.

        Admin URL: /admin/{studio_id}/bookings?export=csv

        Returns the same format as ``requirements_v2/sample_exports/bookings.csv``:
        UTF-8 with BOM, semicolon-delimited, double-quoted, English headers,
        dates as ``DD/MM/YYYY HH:MM``.

        This is the 30-day window booking history export.
        """
        page = await self._scraper.new_page()
        try:
            url = self._build_admin_url("bookings") + "?export=csv"
            return await self._download_csv_via_export_button(page, url, label="bookings")
        finally:
            await page.close()

    async def download_activities(self) -> bytes:
        """
        Download the activities (all) CSV export.

        Admin URL: /admin/{studio_id}/activities?export=csv

        Returns the same format as
        ``requirements_v2/sample_exports/all activities.csv``:
        UTF-8 with BOM, semicolon-delimited, unquoted, German headers,
        dates as ``DD.MM.YYYY`` + separate ``HH:MM`` column.

        The ``Max. Teilnehmer`` and ``Angemeldet`` columns drive UC05
        availability: ``available_spots = Max. Teilnehmer − Angemeldet``.
        """
        page = await self._scraper.new_page()
        try:
            url = self._build_admin_url("activities") + "?export=csv"
            return await self._download_csv_via_export_button(page, url, label="activities")
        finally:
            await page.close()

    async def download_active_appointments(self) -> bytes:
        """
        Download the active appointments CSV export.

        Admin URL: /admin/{studio_id}/bookings?export=active

        Used by the event-driven scheduler to compute today's class end times.
        Same format as bookings export, filtered to active/upcoming bookings only.
        """
        page = await self._scraper.new_page()
        try:
            url = self._build_admin_url("bookings") + "?export=active"
            return await self._download_csv_via_export_button(
                page, url, label="active_appointments"
            )
        finally:
            await page.close()

    async def download_noshows(self) -> bytes:
        """
        Download the no-shows CSV export.

        Admin URL: /admin/{studio_id}/bookings?export=no-show-all

        NOTE: Per spec v2 (07_foundation_layer.md), the no-show export is NOT
        used in UC03 (UC03 removed in v2).  This method is retained for
        completeness but the sync_runner does NOT call it in normal operation.

        The ``noshows.csv`` sample in sample_exports/ is 0 bytes, confirming
        the studio has no no-shows in the export window.
        """
        logger.warning(
            "csv_downloader: download_noshows() called — "
            "no-show export is NOT used in v2 (UC03 removed). "
            "This call should be removed from the caller."
        )
        page = await self._scraper.new_page()
        try:
            url = self._build_admin_url("bookings") + "?export=no-show-all"
            return await self._download_csv_via_export_button(page, url, label="noshows")
        finally:
            await page.close()
