# Changelog

All notable changes to this project are documented here.
Entries are in reverse chronological order (newest first).

---

## [Unreleased]

---

## M2 — Eversports Scraper Rewrite (2026-05-28)

### What changed

Replaced the old `AdminCsvDownloader` (which targeted non-existent bulk-export
URLs that returned HTTP 403/404) with `AdminApiClient` — a direct API client that
calls the correct Eversports admin API endpoints.

### Root cause of old implementation

The previous implementation (`app/scrapers/admin_csv.py`) was written against
assumed URL patterns (`/admin/{id}/bookings?export=csv`, etc.) that do not exist
in the Eversports admin SPA.  The SPA is a jQuery/Bootstrap application served
from S3; its export flows are driven by JS click handlers that call undocumented
JSON/CSV API endpoints.

### Correct endpoints (discovered 2026-05-27 via live probing)

| Endpoint | Method | Returns |
|---|---|---|
| `/api/admin-facilities?facilityShortId={id}` | GET | JSON — numeric `facilityId` |
| `/api/event/export-booking-list?facilityId={id}&fromDate={date}&toDate={date}` | GET | JSON `{"url":"<S3 presigned URL>"}` → fetch URL → CSV matching `bookings.csv` |
| `/api/scheduler/list/download?facilityId={id}&fromDate={date}&toDate={date}&exportType=active` | GET | `text/csv` — matches `all activities.csv` format |
| `/api/event/participant/list/download?facilityId={id}&sessionId={id}` | GET | `text/csv` — per-session participant roster |

Endpoints were discovered by:
1. Loading the scheduler export modal page in a headless browser
2. Searching all 49 loaded JS chunks for export handler code
3. Reverse-engineering `export-activity-list.d4983627.js` which contains
   `createExportLink()` / `getFilterLink()` / `handleExportRequest()`
4. Confirming each endpoint with live HTTP requests via `scripts/probe_live_scraper.py`
5. Verifying `export-booking-list` CSV format with `scripts/probe_booking_list.py`
   (318 KB CSV, 15 columns, exact match with `bookings.csv` sample)

### Files changed

| File | Change |
|---|---|
| `app/scrapers/admin_csv.py` | Full rewrite — `AdminCsvDownloader` → `AdminApiClient`; all page-nav methods replaced with `context.request.get()` calls |
| `app/scrapers/sync_runner.py` | Updated to use `AdminApiClient`; added rolling 30-day date window; added `get_facility_id()` call before downloads |
| `requirements_v2/07_foundation_layer.md` | §Read sources updated with correct API endpoints and discovery notes |
| `scripts/probe_live_scraper.py` | Rewrote to probe all three confirmed endpoints |
| `scripts/probe_booking_list.py` | New — verifies `export-booking-list` presigned URL CSV format |
| `scripts/find_export_endpoint.py` | New — searches all loaded JS files for export handler |
| `scripts/decode_export_handler.py` | New — fetches and parses the export handler JS |

### No schema changes

No DB migrations required.  The `AdminApiClient` stores no new state; the
`locations.eversports_cookie_cache` field is the only Eversports auth state
written by this layer.

### Tests

All 16 scraper tests pass unchanged (`tests/test_scraper.py`).  Tests operate at
the `EversportsBaseScraper` / `sync_runner` level and mock the actual download
calls, so the class rename is invisible to the test suite.

---

## M1.5 — CSV Bootstrap (2026-05-24)

One-time CSV bootstrap pipeline: parsers, normaliser, activity classifier,
orchestrator, 4 new DB models (`contacts`, `bookings`, `sessions`, `products`),
1 Alembic migration, 3 new API endpoints, 70 tests.

See `app/ingest/` for the implementation.

---

## M1 — Repo Scaffold (2026-05-21)

Initial project setup: FastAPI application, SQLAlchemy 2.0 async + asyncpg,
Alembic migrations, `locations` table, health endpoint, GitHub Actions CI,
`uv` package manager.

---

## Spec import (2026-05-20)

Imported `requirements_v2/` specification docs (v2 revision notes, use-case
specs, foundation layer, sample exports).  Also imported
`reference/eversports_scraping_poc/` — a working Node.js PoC that confirmed
the cookie-export auth pattern and the correct endpoint for session/participant
data.

---

## Why the Eversports Provider API is NOT used

The Eversports Provider API (GraphQL, `api.eversports.com`) is documented and
available, but **not used in this product**.  Reasons:

1. **Scope mismatch**: the Provider API exposes consumer-facing data (class
   schedules, venue profiles).  The admin panel exposes operator data (customer
   contacts, booking history, attendance, packages).  We need admin data.

2. **Missing fields**: the Provider API does not expose `Customer number`,
   `Phone number`, `Clubgroup name`, `Products purchased`, or per-booking
   `Attended` status.  All of these are present in the admin CSV exports.

3. **Auth complexity**: the Provider API uses OAuth2 client credentials (one
   client per studio).  The admin panel uses the same session cookie the
   operator already has.  Cookie-export auth is simpler to operate and requires
   no per-studio API registration.

This decision is final for v1.  It is recorded here so future contributors
do not re-investigate the Provider API path.
