#!/usr/bin/env python3
"""Quick live verification of all four AdminApiClient endpoints."""
import asyncio, csv, io, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

BASE_URL = "https://app.eversportsmanager.com"

def _load_cookies():
    for line in (Path(__file__).parent.parent / ".env").read_text().splitlines():
        if line.startswith("EVERSPORTS_TEST_COOKIE_JSON="):
            raw = line.split("=", 1)[1]; break
    _F = {"name","value","domain","path","expires","httpOnly","secure","sameSite"}
    _S = {"strict":"Strict","lax":"Lax","none":"None","no_restriction":"None"}
    out = []
    for c in json.loads(raw):
        cl = {k:v for k,v in c.items() if k in _F}
        if "expires" not in cl and "expirationDate" in c: cl["expires"] = c["expirationDate"]
        if isinstance(cl.get("sameSite"),str): cl["sameSite"] = _S.get(cl["sameSite"].lower(), cl["sameSite"])
        out.append(cl)
    return out

def csv_headers(body: bytes) -> list[str]:
    text = body.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    return list(reader.fieldnames or [])

async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        await ctx.add_cookies(_load_cookies())

        # ── 1. Facility metadata ───────────────────────────────────────────────
        print("=" * 60)
        print("1. GET /api/admin-facilities?facilityShortId=Yneu3U")
        r = await ctx.request.get(f"{BASE_URL}/api/admin-facilities?facilityShortId=Yneu3U")
        body = bytes(await r.body())
        print(f"   HTTP {r.status}  ct={r.headers.get('content-type','')!r}  len={len(body)}")
        data = json.loads(body)
        fac = data["facilities"][0]
        facility_id = fac["id"]
        print(f"   facilityId={facility_id}  name={fac.get('name')!r}")

        FROM = "2026-05-01"
        TO   = "2026-05-28"

        # ── 2. Booking list export ─────────────────────────────────────────────
        print()
        print("=" * 60)
        url2 = f"{BASE_URL}/api/event/export-booking-list?facilityId={facility_id}&fromDate={FROM}&toDate={TO}"
        print(f"2. GET /api/event/export-booking-list?facilityId={facility_id}&fromDate={FROM}&toDate={TO}")
        r2 = await ctx.request.get(url2)
        body2 = bytes(await r2.body())
        print(f"   HTTP {r2.status}  ct={r2.headers.get('content-type','')!r}  len={len(body2)}")
        js2 = json.loads(body2)
        presigned = js2.get("url","")
        print(f"   JSON keys: {list(js2.keys())}")
        print(f"   Presigned URL host: {presigned.split('/')[2] if presigned else 'MISSING'}")
        # Fetch presigned
        r2b = await ctx.request.get(presigned)
        csv2 = bytes(await r2b.body())
        print(f"   Presigned → HTTP {r2b.status}  ct={r2b.headers.get('content-type','')!r}  len={len(csv2):,}")
        print(f"   CSV headers: {csv_headers(csv2)}")

        # ── 3. Scheduler / activities export ──────────────────────────────────
        print()
        print("=" * 60)
        url3 = f"{BASE_URL}/api/scheduler/list/download?facilityId={facility_id}&fromDate={FROM}&toDate={TO}&exportType=active"
        print(f"3. GET /api/scheduler/list/download?facilityId={facility_id}&fromDate={FROM}&toDate={TO}&exportType=active")
        r3 = await ctx.request.get(url3)
        csv3 = bytes(await r3.body())
        print(f"   HTTP {r3.status}  ct={r3.headers.get('content-type','')!r}  len={len(csv3):,}")
        print(f"   CSV headers: {csv_headers(csv3)}")

        # ── 4. Per-session participant list ────────────────────────────────────
        # First get a real sessionId from the classes page
        print()
        print("=" * 60)
        print("4. GET /api/event/participant/list/download")
        print("   (need a live sessionId — fetching classes page first)")
        page = await ctx.new_page()
        await page.goto(f"{BASE_URL}/admin/Yneu3U/classes?date=2026-05-28", timeout=30_000, wait_until="commit")
        await page.wait_for_timeout(2000)
        sessions = await page.evaluate("""
            () => Array.from(document.querySelectorAll('tr.js_quick-data[data-eventsession]'))
                .map(r => { try { return JSON.parse(r.getAttribute('data-eventsession')); } catch { return null; } })
                .filter(Boolean)
        """)
        await page.close()
        # Pick first session with participants
        chosen = next((s for s in sessions if s.get("sessionParticipantsCount", 0) > 0), sessions[0] if sessions else None)
        if chosen:
            sid = chosen["eventSessionId"]
            url4 = f"{BASE_URL}/api/event/participant/list/download?facilityId={facility_id}&sessionId={sid}"
            print(f"   sessionId={sid}  ({chosen.get('eventName')} @ {chosen.get('startDate')} {chosen.get('startTime')})")
            print(f"   GET /api/event/participant/list/download?facilityId={facility_id}&sessionId={sid}")
            r4 = await ctx.request.get(url4)
            csv4 = bytes(await r4.body())
            print(f"   HTTP {r4.status}  ct={r4.headers.get('content-type','')!r}  len={len(csv4):,}")
            print(f"   CSV headers: {csv_headers(csv4)}")
        else:
            print("   No sessions found for 2026-05-28")

        await browser.close()

    print()
    print("=" * 60)
    print("All endpoints checked.")

asyncio.run(main())
