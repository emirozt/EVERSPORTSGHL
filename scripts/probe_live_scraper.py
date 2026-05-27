#!/usr/bin/env python3
"""
Live scraper probe — no DB required.

Tests all three real Eversports scraping use cases:

  USE CASE 1 — Participant data per session (PoC-confirmed working approach)
    Step 1: GET /admin/{companyId}/classes?date=TODAY
            Parse <tr class="js_quick-data" data-eventsession="..."> HTML attrs.
    Step 2: GET /api/event/participant/list/download?facilityId={id}&sessionId={id}
            Returns semicolon-delimited CSV with contact + booking info.

  USE CASE 2 — Class/schedule export (scheduler overview)
    Navigate to /admin/{id}/scheduler/overview?modal=export-class-list&export=active
    Interact with date-picker modal, click Export, capture download.

  USE CASE 3 — Facility metadata
    GET /api/admin-facilities?facilityShortId={companyId}
    Returns numeric facilityId needed for participant export.

Usage:
    python scripts/probe_live_scraper.py
    python scripts/probe_live_scraper.py --studio-id Yneu3U
    python scripts/probe_live_scraper.py --studio-id Yneu3U --days 3
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

BASE_URL = "https://app.eversportsmanager.com"
_NAV_TIMEOUT_MS = 30_000
_DOWNLOAD_TIMEOUT_MS = 60_000


# ── Cookie helpers ─────────────────────────────────────────────────────────────

def _load_cookies() -> list[dict]:
    cookie_raw = os.getenv("EVERSPORTS_TEST_COOKIE_JSON")
    if not cookie_raw:
        env_path = Path(__file__).parent.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("EVERSPORTS_TEST_COOKIE_JSON="):
                    cookie_raw = line.split("=", 1)[1]
                    break
    if not cookie_raw:
        print("ERROR: EVERSPORTS_TEST_COOKIE_JSON not found in environment or .env")
        sys.exit(1)

    _PLAYWRIGHT_FIELDS = {"name","value","domain","path","expires","httpOnly","secure","sameSite"}
    _SAMESITE_MAP = {"strict":"Strict","lax":"Lax","none":"None","no_restriction":"None"}
    cookies = []
    for c in json.loads(cookie_raw):
        cleaned = {k: v for k, v in c.items() if k in _PLAYWRIGHT_FIELDS}
        if "expires" not in cleaned and "expirationDate" in c:
            cleaned["expires"] = c["expirationDate"]
        if isinstance(cleaned.get("sameSite"), str):
            cleaned["sameSite"] = _SAMESITE_MAP.get(cleaned["sameSite"].lower(), cleaned["sameSite"])
        cookies.append(cleaned)
    return cookies


# ── CSV helpers ────────────────────────────────────────────────────────────────

def print_sample(label: str, data: bytes, delimiter: str = ";") -> None:
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    headers = reader.fieldnames or []
    first_row = next(reader, None)
    bar = "─" * 60
    print(f"\n  ┌─ {label} ({len(headers)} cols) ────")
    print(f"  │  Headers: {', '.join(str(h) for h in headers)}")
    if first_row:
        for k, v in first_row.items():
            if v:
                print(f"  │  {k}: {v}")
    else:
        print("  │  (no data rows)")
    print(f"  └{bar}")


# ── Use Case 3: Facility metadata ──────────────────────────────────────────────

async def get_facility_id(context, company_id: str) -> int:
    """
    GET /api/admin-facilities?facilityShortId={company_id}
    Returns the numeric facilityId required for participant CSV downloads.
    """
    print(f"\n[UC3] Facility metadata — GET /api/admin-facilities?facilityShortId={company_id}")
    resp = await context.request.get(
        f"{BASE_URL}/api/admin-facilities?facilityShortId={company_id}",
        timeout=15_000,
    )
    print(f"  Status: {resp.status}")
    if resp.status != 200:
        raise RuntimeError(f"Facilities API returned {resp.status}")
    data = await resp.json()
    facilities = data.get("facilities", [])
    if not facilities:
        raise RuntimeError(f"No facilities found for companyId={company_id}")
    facility_id = facilities[0]["id"]
    print(f"  ✓ facilityId={facility_id}  (from {len(facilities)} facilities)")
    print(f"  Sample facility: {json.dumps(facilities[0], indent=4)}")
    return facility_id


# ── Use Case 1 step 1: Class list for a date ───────────────────────────────────

async def get_sessions_for_date(context, company_id: str, date_str: str) -> list[dict]:
    """
    Navigate to /admin/{companyId}/classes?date={date_str}.
    Extract session metadata from <tr class="js_quick-data" data-eventsession="..."> attrs.
    Returns list of session dicts with eventSessionId, eventName, startDate,
    startTime, sessionParticipantsCount.
    """
    url = f"{BASE_URL}/admin/{company_id}/classes?date={date_str}"
    page = await context.new_page()
    try:
        resp = await page.goto(url, timeout=_NAV_TIMEOUT_MS, wait_until="commit")
        await page.wait_for_timeout(2000)

        if "/login" in page.url:
            raise RuntimeError("Session expired — redirected to login")

        status = resp.status if resp else "?"
        print(f"  classes page: HTTP {status}  url={page.url}")

        # Extract data-eventsession JSON attributes from the DOM
        sessions_raw = await page.evaluate("""
            () => Array.from(
                document.querySelectorAll('tr.js_quick-data[data-eventsession]')
            ).map(row => {
                try { return JSON.parse(row.getAttribute('data-eventsession')); }
                catch { return null; }
            }).filter(Boolean)
        """)
        return sessions_raw
    finally:
        await page.close()


# ── Use Case 1 step 2: Participant CSV per session ─────────────────────────────

async def download_participant_csv(context, facility_id: int, session_id: str) -> bytes:
    """
    GET /api/event/participant/list/download?facilityId={id}&sessionId={id}
    Returns semicolon-delimited CSV with columns:
      Kundennummer, Nachname, Vorname, E-Mail-Adresse, Clubgroup name,
      Marketing Kommunikation, Telefonnummer, Alter, Geburtsdatum, Land,
      PLZ, city, Strasse, Kommentar, Notiz, Warnung, Klasse, Optionen,
      Texte, Produkt, Gesamtpreis, Zahlungsstatus, Aggregator
    """
    url = (
        f"{BASE_URL}/api/event/participant/list/download"
        f"?facilityId={facility_id}&sessionId={session_id}"
    )
    resp = await context.request.get(url, timeout=20_000)
    if resp.status != 200:
        raise RuntimeError(f"Participant export returned HTTP {resp.status}")
    data = (await resp.body())
    return bytes(data)


# ── Use Case 2: Scheduler overview export ─────────────────────────────────────

async def download_scheduler_export(context, company_id: str) -> bytes | None:
    """
    Navigate to /admin/{id}/scheduler/overview?modal=export-class-list&export=active
    and capture the file download after the date-picker modal opens.

    Flow observed:
    1. A "Select your language" modal appears first — must be dismissed.
    2. The export-class-list modal then appears with date pickers.
    3. Click Export to download.
    """
    url = (
        f"{BASE_URL}/admin/{company_id}/scheduler/overview"
        "?modal=export-class-list&export=active"
    )
    print(f"\n  Navigating to scheduler export: {url}")
    page = await context.new_page()
    try:
        await page.goto(url, timeout=_NAV_TIMEOUT_MS, wait_until="commit")
        await page.wait_for_timeout(3000)

        if "/login" in page.url:
            raise RuntimeError("Session expired — redirected to login")

        # Step 1: Dismiss the language modal if it appeared
        lang_close_selectors = [
            "button.close-language-modal",
            ".modal .close-language-modal",
            "button[data-dismiss='modal']",
        ]
        for sel in lang_close_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=2000):
                    print(f"  Dismissing language modal ({sel})")
                    await btn.click()
                    await page.wait_for_timeout(1500)
                    break
            except Exception:
                pass

        await page.wait_for_timeout(2000)

        # Take a screenshot after language modal dismissed
        screenshot_path = Path("/tmp/eversports_scheduler_export.png")
        await page.screenshot(path=str(screenshot_path))
        print(f"  Screenshot → {screenshot_path}")

        # Step 2: Wait for export modal; intercept API responses to capture export URL/data
        await page.wait_for_selector("#export-class-list-modal", timeout=5000)
        captured_responses: list[dict] = []
        all_api_responses: list[dict] = []
        # Capture requests too so we see what's being sent
        captured_requests: list[str] = []

        async def on_response(response) -> None:
            url = response.url
            # Skip static assets, fonts, images, HubSpot tracking
            skip = any(x in url for x in [
                ".js", ".css", ".png", ".gif", ".woff", ".svg",
                "hubspot", "hubapi", "ably.io", "pusher", "sentry",
                "fonts.googleapis", "cartographer",
            ])
            if skip:
                return
            try:
                body = await response.body()
                entry = {
                    "url": url,
                    "status": response.status,
                    "ct": response.headers.get("content-type", ""),
                    "len": len(body),
                    "body_prefix": body[:400].decode("utf-8-sig", errors="replace"),
                }
            except Exception:
                entry = {"url": url, "status": response.status}
            all_api_responses.append(entry)

        def on_request(request) -> None:
            url = request.url
            skip = any(x in url for x in [
                ".js", ".css", ".png", ".gif", ".woff", ".svg",
                "hubspot", "hubapi", "ably.io", "pusher", "sentry",
                "fonts.googleapis", "cartographer",
            ])
            if not skip:
                captured_requests.append(f"{request.method} {url}")

        page.on("response", on_response)
        page.on("request", on_request)

        # The export handler is in export-activity-list.d4983627.js.
        # Key flow:
        #   createExportLink(startDate, endDate)
        #     → getFilterLink(exportType, fromDate, toDate)
        #     → `/api/${eventType}/list/download?facilityId=${facilityId}&fromDate=...&toDate=...&exportType=active`
        #     → sets that URL as href on #export-activity-list-submit + removes 'disabled'
        #   handleExportRequest(href)
        #     → fetch(href) → if JSON: use response.url; else redirect URL
        #     → sets href on #export-download-link + clicks it
        #
        # Strategy: call createExportLink directly to get the pre-computed URL,
        # then fetch it ourselves via context.request.get().

        today = date.today()
        date_start_iso = today.replace(day=1).strftime("%Y-%m-%d")
        date_end_iso   = today.strftime("%Y-%m-%d")

        # Step 3: Set export type (triggers showExportUI which initialises datepickers)
        await page.evaluate("""
            () => {
                $('#export-select').val('active').trigger('change');
            }
        """)
        await page.wait_for_timeout(1000)

        # Step 4: Call createExportLink with JS Date objects → sets href + removes disabled
        result = await page.evaluate(f"""
            async () => {{
                const s = new Date('{date_start_iso}');
                const e = new Date('{date_end_iso}');
                // Also set the datepickers so getDateElements calls in the click handler work
                $('#export-date-start').datepicker('setDate', s);
                $('#export-date-end').datepicker('setDate', e);
                // Call createExportLink (async)
                await createExportLink(s, e);
                const btn = document.getElementById('export-activity-list-submit');
                return {{
                    href: btn?.getAttribute('href') || '',
                    cls:  btn?.className || '',
                    eventType: typeof eventType !== 'undefined' ? eventType : '(not found)',
                    facilityId: typeof facilityId !== 'undefined' ? facilityId : '(not found)',
                }};
            }}
        """)
        print(f"  createExportLink result: {result}")
        export_url = result.get("href", "")

        if not export_url:
            print("  (no href set — falling back to manual URL construction)")
            ev_type  = result.get("eventType", "event")
            fac_id   = result.get("facilityId", facility_id)
            export_url = (
                f"{BASE_URL}/api/{ev_type}/list/download"
                f"?facilityId={fac_id}&fromDate={date_start_iso}&toDate={date_end_iso}&exportType=active"
            )
            print(f"  Constructed URL: {export_url}")

        if not export_url.startswith("http"):
            export_url = BASE_URL + export_url

        # Step 5: Fetch the export URL directly via the authenticated context
        print(f"  Fetching: {export_url}")
        resp = await context.request.get(export_url, timeout=30_000)
        ct = resp.headers.get("content-type", "")
        body = bytes(await resp.body())
        print(f"  Response: HTTP {resp.status}  ct={ct!r}  len={len(body):,}")

        if resp.status == 200:
            if "json" in ct:
                js = json.loads(body.decode("utf-8", errors="replace"))
                print(f"  JSON: {js}")
                download_url = js.get("url") or js.get("downloadUrl") or js.get("link")
                if download_url:
                    print(f"  Fetching pre-signed URL: {download_url[:80]}...")
                    dl_resp = await context.request.get(download_url, timeout=30_000)
                    data = bytes(await dl_resp.body())
                    print(f"  ✓ {len(data):,} bytes from pre-signed URL")
                    return data
            elif "text/csv" in ct or "octet-stream" in ct or (len(body) > 50 and b";" in body[:200]):
                print(f"  ✓ {len(body):,} bytes CSV (inline)")
                return body
            else:
                print(f"  Unexpected content-type — body: {body[:300].decode('utf-8-sig', errors='replace')!r}")
        else:
            print(f"  Error: {body[:300].decode('utf-8-sig', errors='replace')!r}")

        print("  (export endpoint did not return a CSV)")
        return None

    finally:
        await page.close()


# ── Main ───────────────────────────────────────────────────────────────────────

async def main(studio_id: str, days_back: int = 3) -> None:
    cookies = _load_cookies()
    print(f"Loaded {len(cookies)} cookie(s)")
    print(f"Studio ID: {studio_id}")

    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        await context.add_cookies(cookies)

        # ── UC3: Facility metadata ─────────────────────────────────────────────
        try:
            facility_id = await get_facility_id(context, studio_id)
        except Exception as exc:
            print(f"  ✗ UC3 failed: {exc}")
            facility_id = None

        # ── UC1: Participant data per session ──────────────────────────────────
        print(f"\n[UC1] Participant data — classes page + per-session participant export")
        uc1_success = False
        today = date.today()
        for days_ago in range(0, days_back + 1):
            date_str = (today - timedelta(days=days_ago)).isoformat()
            print(f"\n  Checking date: {date_str}")
            try:
                sessions = await get_sessions_for_date(context, studio_id, date_str)
                print(f"  Sessions found: {len(sessions)}")
                if not sessions:
                    print("  (no sessions on this date — trying next)")
                    continue

                # Print first session metadata
                first = sessions[0]
                print(f"  Sample session: {json.dumps(first, indent=4)}")

                # Download participant CSV for first session with participants
                if facility_id:
                    for s in sessions:
                        if s.get("sessionParticipantsCount", 0) > 0 and not s.get("eventSessionCancelled"):
                            sid = str(s["eventSessionId"])
                            print(f"\n  Downloading participants for session {sid} "
                                  f"({s.get('eventName')} — {s.get('startDate')} {s.get('startTime')})")
                            try:
                                csv_bytes = await download_participant_csv(context, facility_id, sid)
                                print(f"  ✓ {len(csv_bytes):,} bytes")
                                print_sample(
                                    f"participants/{s.get('eventName')}/{date_str}",
                                    csv_bytes,
                                )
                                uc1_success = True
                                break
                            except Exception as exc:
                                print(f"  ✗ participant download failed: {exc}")

                if uc1_success:
                    break
                print("  No sessions with participants on this date")
            except Exception as exc:
                print(f"  ✗ {exc}")

        if not uc1_success:
            print(f"\n  (no participant data found in last {days_back} days)")

        # ── UC2: Scheduler overview export ────────────────────────────────────
        print(f"\n[UC2] Scheduler overview export")
        try:
            uc2_data = await download_scheduler_export(context, studio_id)
            if uc2_data:
                print_sample("scheduler_export", uc2_data)
            else:
                print("  (export requires manual date selection — see screenshot)")
        except Exception as exc:
            print(f"  ✗ UC2 failed: {exc}")

        await browser.close()

    print("\n" + "─" * 60)
    print("Probe complete.")
    print("─" * 60)


if __name__ == "__main__":
    args = sys.argv[1:]
    studio_override = "Yneu3U"  # default
    days_back = 3
    if "--studio-id" in args:
        i = args.index("--studio-id")
        if i + 1 < len(args):
            studio_override = args[i + 1]
    if "--days" in args:
        i = args.index("--days")
        if i + 1 < len(args):
            days_back = int(args[i + 1])

    asyncio.run(main(studio_id=studio_override, days_back=days_back))
