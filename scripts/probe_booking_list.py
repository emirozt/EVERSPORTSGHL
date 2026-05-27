#!/usr/bin/env python3
"""
Quick probe: verify that /api/event/export-booking-list returns a presigned URL
whose CSV matches the bookings.csv sample format.
"""
import asyncio, csv, io, json, os, sys
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

async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        await ctx.add_cookies(_load_cookies())

        # Step 1: Get facilityId
        r = await ctx.request.get(f"{BASE_URL}/api/admin-facilities?facilityShortId=Yneu3U")
        fac = (await r.json())["facilities"][0]["id"]
        print(f"facilityId = {fac}")

        # Step 2: Fetch export-booking-list
        from_date = "2026-05-01"
        to_date   = "2026-05-27"
        booking_url = (
            f"{BASE_URL}/api/event/export-booking-list"
            f"?facilityId={fac}&fromDate={from_date}&toDate={to_date}"
        )
        print(f"\nGET {booking_url}")
        r2 = await ctx.request.get(booking_url, timeout=30_000)
        ct = r2.headers.get("content-type", "")
        body = bytes(await r2.body())
        print(f"  HTTP {r2.status}  ct={ct!r}  len={len(body):,}")
        print(f"  body: {body[:500].decode('utf-8', errors='replace')!r}")

        if r2.status == 200 and "json" in ct:
            js = json.loads(body)
            print(f"\n  JSON keys: {list(js.keys())}")
            presigned = js.get("url") or js.get("downloadUrl") or js.get("link") or js.get("fileUrl")
            if presigned:
                print(f"\n  Presigned URL: {presigned[:100]}...")
                r3 = await ctx.request.get(presigned, timeout=30_000)
                csv_ct = r3.headers.get("content-type", "")
                csv_body = bytes(await r3.body())
                print(f"\n  Presigned response: HTTP {r3.status}  ct={csv_ct!r}  len={len(csv_body):,}")
                # Decode and show headers + first row
                text = csv_body.decode("utf-8-sig", errors="replace")
                reader = csv.DictReader(io.StringIO(text), delimiter=";")
                headers = reader.fieldnames or []
                first = next(reader, None)
                print(f"\n  CSV Headers ({len(headers)} cols):")
                for h in headers:
                    print(f"    {h!r}")
                if first:
                    print(f"\n  First row sample:")
                    for k, v in first.items():
                        if v: print(f"    {k}: {v!r}")
                else:
                    print("  (no data rows)")
                    print(f"  Raw (first 200 bytes): {csv_body[:200]!r}")
            else:
                print(f"  Could not find presigned URL key in JSON: {js}")

        await browser.close()

asyncio.run(main())
