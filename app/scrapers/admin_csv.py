"""
AdminApiClient — direct API client for Eversports admin data exports.

Replaces the old AdminCsvDownloader that mistakenly targeted non-existent bulk
export URLs (/admin/{id}/bookings?export=csv, /admin/{id}/activities?export=csv).

All endpoints were discovered via live probing on 2026-05-27 against Studio
Stuttgart (Yneu3U, facilityId 78034):

  Facility metadata
    GET /api/admin-facilities?facilityShortId={companyId}
    → JSON {"facilities": [{"id": 78034, "name": "...", ...}]}
    Used to obtain the numeric facilityId required by all other endpoints.

  Booking list export  (matches requirements_v2/sample_exports/bookings.csv)
    GET /api/event/export-booking-list
        ?facilityId={id}&fromDate={YYYY-MM-DD}&toDate={YYYY-MM-DD}
    → JSON {"url": "https://s3.eu-central-1.amazonaws.com/...presigned..."}
    → Fetch presigned URL → text/csv (English headers, semicolon-delimited,
      double-quoted, UTF-8 BOM, ``application/ms-excel`` content-type)
    Columns: Start; End; Activity name; Location; Trainer nickname;
             Customer number; First name; Last name; E-Mail; Clubgroup name;
             Newsletter; Product name; Price; Attended; Phone number

  Scheduler / activities export  (matches sample_exports/all activities.csv)
    GET /api/scheduler/list/download
        ?facilityId={id}&fromDate={YYYY-MM-DD}&toDate={YYYY-MM-DD}&exportType=active
    → text/csv  (German headers, semicolon-delimited, unquoted, UTF-8 BOM)
    Columns: Typ; Datum; Startzeit; Endzeit; Name; Angemeldet; Anwesend;
             Max. Teilnehmer; Warteliste; Trainer; Ort; Status; Sport;
             Aktivitätsgruppe; Kommentar zur Einheit; Veröffentlicht

  Per-session participant list  (UC1 session-level data, not used by bootstrap)
    GET /api/event/participant/list/download
        ?facilityId={id}&sessionId={sessionId}
    → text/csv  (German headers, semicolon-delimited, unquoted)

All requests use ``BrowserContext.request.get()`` — no page navigation required.
The Playwright browser context carries the injected session cookies so requests
are authenticated without any login flow.

Session expiry detection:
  HTTP 401/403 → raise SessionExpiredError
  HTML body (text/html) on an API endpoint → raise SessionExpiredError
  HTTP 200 with expected content-type → success

References:
  - scripts/probe_live_scraper.py — working probe confirming all endpoints
  - scripts/probe_booking_list.py — confirmed bookings format (318 KB, 15 cols)
  - reference/eversports_scraping_poc/src/scraper.js — original PoC (Node.js)
  - requirements_v2/07_foundation_layer.md §Layer 1 Read sources
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext

from app.db.models.location import Location
from app.scrapers.base import EversportsBaseScraper
from app.scrapers.exceptions import SessionExpiredError

logger = logging.getLogger(__name__)

# Request timeout for all API calls (milliseconds)
_API_TIMEOUT_MS = 30_000

# Eversports admin API base URL
_BASE_URL = "https://app.eversportsmanager.com"


class AdminApiClient:
    """
    Direct API client for Eversports admin data exports.

    Uses ``BrowserContext.request.get()`` to call Eversports JSON/CSV endpoints
    with the authenticated browser context (cookie injection, no page navigation).

    This replaces the old ``AdminCsvDownloader`` that attempted to use page
    navigation and file download events against endpoints that do not exist.

    Raises:
        SessionExpiredError: if any API call returns HTTP 401/403 or an HTML
            response body (indicating a login redirect on the final URL).
        RuntimeError: if an API call fails for any other reason.
    """

    def __init__(self, scraper: EversportsBaseScraper, location: Location) -> None:
        self._scraper = scraper
        self._location = location

    # ── Internal helpers ───────────────────────────────────────────────────────

    @property
    def _ctx(self) -> BrowserContext:
        return self._scraper.context

    def _check_session(self, status: int, content_type: str, body_prefix: bytes) -> None:
        """
        Raise ``SessionExpiredError`` if the response indicates an expired session.

        Checks:
          1. HTTP 401 or 403 → explicit authentication failure.
          2. ``text/html`` content-type on an API endpoint → login-page redirect
             (Playwright follows 302 redirects, ending at the login HTML page).
          3. HTML body prefix heuristic (for cases where content-type is absent).
        """
        if status in (401, 403):
            raise SessionExpiredError(
                "Eversports API returned HTTP {status} — session expired. "
                "Re-export cookies and run scripts/import_cookies.py"
            )
        if "text/html" in content_type:
            raise SessionExpiredError(
                "Eversports API returned HTML (expected JSON/CSV) — session may be "
                "expired. Re-export cookies and run scripts/import_cookies.py"
            )
        if body_prefix.lstrip().startswith((b"<!DOCTYPE", b"<html", b"<!doctype")):
            raise SessionExpiredError(
                "Eversports API returned HTML body — session may be expired. "
                "Re-export cookies and run scripts/import_cookies.py"
            )

    async def _get(self, url: str, *, label: str) -> bytes:
        """
        Perform an authenticated GET request via the browser context.

        Args:
            url: Full URL to fetch.
            label: Log label for diagnostics.

        Returns:
            Raw response body bytes (HTTP 200 only).

        Raises:
            SessionExpiredError: on 401/403 or HTML response.
            RuntimeError: on non-200 status (other than 401/403).
        """
        logger.info("api_client: GET %s [%s]", url, label)
        try:
            resp = await self._ctx.request.get(url, timeout=_API_TIMEOUT_MS)
        except Exception as exc:
            raise RuntimeError(f"api_client: request failed for {label}: {exc}") from exc

        status = resp.status
        content_type = resp.headers.get("content-type", "")
        body = bytes(await resp.body())

        self._check_session(status, content_type, body[:64])

        if status != 200:
            raise RuntimeError(
                f"api_client: HTTP {status} for {label}  url={url}"
            )

        logger.debug(
            "api_client: %s  HTTP 200  ct=%r  len=%d",
            label,
            content_type,
            len(body),
        )
        return body

    # ── Public API methods ─────────────────────────────────────────────────────

    async def get_facility_id(self, company_id: str) -> int:
        """
        Fetch the numeric facilityId for a studio.

        Args:
            company_id: The short studio code from the admin URL (e.g. ``"Yneu3U"``).
                Stored in ``locations.eversports_studio_id``.

        Returns:
            Numeric facility ID (e.g. ``78034``).

        Raises:
            RuntimeError: if the facilities endpoint returns no facilities.
            SessionExpiredError: if the session is expired.
        """
        url = f"{_BASE_URL}/api/admin-facilities?facilityShortId={company_id}"
        body = await self._get(url, label="facilities")

        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"api_client: facilities endpoint returned non-JSON body: {body[:200]!r}"
            ) from exc

        facilities = data.get("facilities", [])
        if not facilities:
            raise RuntimeError(
                f"api_client: no facilities found for companyId={company_id!r}"
            )

        facility_id: int = facilities[0]["id"]
        logger.info(
            "api_client: facilityId=%d for companyId=%s",
            facility_id,
            company_id,
        )
        return facility_id

    async def download_bookings(
        self,
        facility_id: int,
        from_date: str,
        to_date: str,
    ) -> bytes:
        """
        Download the booking list CSV export.

        Calls ``/api/event/export-booking-list`` which returns JSON containing a
        pre-signed S3 URL.  Fetches the S3 URL to obtain the actual CSV.

        Returns the same format as ``requirements_v2/sample_exports/bookings.csv``:
        UTF-8 with BOM, semicolon-delimited, double-quoted, English headers,
        dates as ``DD/MM/YYYY HH:MM``.

        Args:
            facility_id: Numeric facility ID (from ``get_facility_id()``).
            from_date: Start date in ``YYYY-MM-DD`` format.
            to_date: End date in ``YYYY-MM-DD`` format (inclusive).

        Returns:
            Raw CSV bytes.
        """
        api_url = (
            f"{_BASE_URL}/api/event/export-booking-list"
            f"?facilityId={facility_id}&fromDate={from_date}&toDate={to_date}"
        )
        body = await self._get(api_url, label="booking_list_json")

        try:
            js = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"api_client: booking-list endpoint returned non-JSON: {body[:200]!r}"
            ) from exc

        presigned_url: str | None = (
            js.get("url")
            or js.get("downloadUrl")
            or js.get("fileUrl")
            or js.get("link")
        )
        if not presigned_url:
            raise RuntimeError(
                f"api_client: booking-list JSON has no presigned URL. keys={list(js.keys())}"
            )

        logger.info("api_client: fetching presigned booking CSV URL")
        csv_bytes = await self._get(presigned_url, label="booking_list_csv")

        if not csv_bytes:
            raise RuntimeError("api_client: booking-list presigned URL returned empty body")

        logger.info(
            "api_client: bookings CSV downloaded  %d bytes  from=%s  to=%s",
            len(csv_bytes),
            from_date,
            to_date,
        )
        return csv_bytes

    async def download_activities(
        self,
        facility_id: int,
        from_date: str,
        to_date: str,
    ) -> bytes:
        """
        Download the activities (scheduler) CSV export.

        Calls ``/api/scheduler/list/download`` which returns the CSV directly.

        Returns the same format as
        ``requirements_v2/sample_exports/all activities.csv``:
        UTF-8 with BOM, semicolon-delimited, unquoted, German headers.

        Columns: Typ; Datum; Startzeit; Endzeit; Name; Angemeldet; Anwesend;
        Max. Teilnehmer; Warteliste; Trainer; Ort; Status; Sport;
        Aktivitätsgruppe; Kommentar zur Einheit; Veröffentlicht

        The ``Max. Teilnehmer`` and ``Angemeldet`` columns drive UC05
        availability: ``available_spots = Max. Teilnehmer − Angemeldet``.

        Args:
            facility_id: Numeric facility ID (from ``get_facility_id()``).
            from_date: Start date in ``YYYY-MM-DD`` format.
            to_date: End date in ``YYYY-MM-DD`` format (inclusive).

        Returns:
            Raw CSV bytes.
        """
        url = (
            f"{_BASE_URL}/api/scheduler/list/download"
            f"?facilityId={facility_id}&fromDate={from_date}&toDate={to_date}"
            "&exportType=active"
        )
        csv_bytes = await self._get(url, label="activities_csv")

        if not csv_bytes:
            raise RuntimeError("api_client: activities CSV endpoint returned empty body")

        logger.info(
            "api_client: activities CSV downloaded  %d bytes  from=%s  to=%s",
            len(csv_bytes),
            from_date,
            to_date,
        )
        return csv_bytes

    async def download_participant_csv(
        self,
        facility_id: int,
        session_id: str | int,
    ) -> bytes:
        """
        Download the per-session participant CSV.

        Returns semicolon-delimited CSV with German headers:
        Kundennummer; Nachname; Vorname; E-Mail-Adresse; Clubgroup name;
        Marketing Kommunikation; Telefonnummer; Alter; Geburtsdatum; Land;
        PLZ; city; Strasse; Kommentar; Notiz; Warnung; Klasse; Optionen;
        Texte; Produkt; Gesamtpreis; Zahlungsstatus; Aggregator

        NOTE: This method is provided for completeness and future event-driven
        use cases (UC01 trial follow-up — identifying participants of specific
        sessions).  The main sync pipeline uses ``download_bookings()`` instead,
        which returns pre-merged session + participant data.

        Args:
            facility_id: Numeric facility ID (from ``get_facility_id()``).
            session_id: Numeric session/event ID (``eventSessionId`` from the
                ``tr.js_quick-data[data-eventsession]`` DOM attribute).

        Returns:
            Raw CSV bytes.
        """
        url = (
            f"{_BASE_URL}/api/event/participant/list/download"
            f"?facilityId={facility_id}&sessionId={session_id}"
        )
        csv_bytes = await self._get(url, label=f"participants/session={session_id}")

        logger.info(
            "api_client: participant CSV downloaded  %d bytes  session=%s",
            len(csv_bytes),
            session_id,
        )
        return csv_bytes
