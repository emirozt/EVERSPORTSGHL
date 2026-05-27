#!/usr/bin/env python3
"""
Finds the Eversports scheduler export API endpoint by:
1. Loading the scheduler page
2. Searching the loaded JS sources for the export-activity-list-submit click handler
3. Making a direct API call with known parameters
"""
from __future__ import annotations
import asyncio, json, os, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

BASE_URL = "https://app.eversportsmanager.com"

def _load_cookies():
    cookie_raw = None
    env_path = Path(__file__).parent.parent / ".env"
    for line in env_path.read_text().splitlines():
        if line.startswith("EVERSPORTS_TEST_COOKIE_JSON="):
            cookie_raw = line.split("=", 1)[1]
    cookies_raw = json.loads(cookie_raw)
    _FIELDS = {"name","value","domain","path","expires","httpOnly","secure","sameSite"}
    _SS = {"strict":"Strict","lax":"Lax","none":"None","no_restriction":"None"}
    out = []
    for c in cookies_raw:
        cl = {k:v for k,v in c.items() if k in _FIELDS}
        if "expires" not in cl and "expirationDate" in c:
            cl["expires"] = c["expirationDate"]
        if isinstance(cl.get("sameSite"), str):
            cl["sameSite"] = _SS.get(cl["sameSite"].lower(), cl["sameSite"])
        out.append(cl)
    return out

async def main():
    from playwright.async_api import async_playwright

    cookies = _load_cookies()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        await context.add_cookies(cookies)
        page = await context.new_page()

        # Capture all script source URLs loaded by the page
        script_urls = []
        page.on("response", lambda r: script_urls.append(r.url) if r.url.endswith(".js") else None)

        # Also intercept ALL requests after we click Export
        all_requests_after_click: list[str] = []

        await page.goto(
            f"{BASE_URL}/admin/Yneu3U/scheduler/overview?modal=export-class-list&export=active",
            timeout=30_000, wait_until="commit"
        )
        await page.wait_for_timeout(3000)

        # Find JS files that might contain the export handler
        main_scripts = [u for u in script_urls if "eversports" in u and ".js" in u]
        print(f"Eversports JS files loaded ({len(main_scripts)}):")
        for u in main_scripts[:10]:
            print(f"  {u}")

        # Search loaded JS for export endpoint pattern
        print("\nSearching JS sources for export endpoint...")
        export_urls_found = []
        for script_url in main_scripts[:15]:
            try:
                resp = await context.request.get(script_url, timeout=10_000)
                if resp.status == 200:
                    src = (await resp.body()).decode("utf-8", errors="replace")
                    # Search for patterns around export-activity-list-submit or export endpoint
                    patterns = [
                        r'export[_\-]activity[^"\']{0,100}',
                        r'/api/[^"\']+export[^"\']{0,100}',
                        r'/admin/[^"\']+export[^"\']{0,50}',
                        r'export-date-start[^"\']{0,200}',
                        r'facilityId[^"\']{0,200}',
                        r'export-activity-list-submit[^"\']{0,300}',
                        r'getDate\(\)[^"\']{0,200}',
                    ]
                    for pat in patterns:
                        for m in re.finditer(pat, src, re.IGNORECASE):
                            snippet = m.group(0)[:200]
                            if snippet not in export_urls_found:
                                export_urls_found.append(snippet)
                                print(f"  [{script_url.split('/')[-1]}] {snippet}")
            except Exception as exc:
                print(f"  {script_url}: {exc}")

        # Now try to trigger the export by manipulating the datepicker properly
        print("\nAttempting proper jQuery datepicker setup...")
        setup = await page.evaluate("""
            () => {
                const log = [];
                // Set select
                const sel = document.getElementById('export-select');
                if (sel) { sel.value = 'active'; $(sel).trigger('change'); log.push('select → active'); }
                // Set datepicker via Date object (avoids format parsing issues)
                const setDP = (id, d) => {
                    const inp = document.getElementById(id);
                    if (!inp) { log.push(id + ': NOT found'); return; }
                    try {
                        $(inp).datepicker('setDate', d);
                        log.push(id + ' datepicker.setDate(' + $(inp).val() + ')');
                    } catch(e) {
                        inp.value = d.toLocaleDateString('de-DE');
                        log.push(id + ' fallback val=' + inp.value);
                    }
                };
                const now = new Date();
                const start = new Date(now.getFullYear(), now.getMonth(), 1);
                setDP('export-date-start', start);
                setDP('export-date-end',   now);
                return log;
            }
        """)
        print(f"  Setup: {setup}")
        await page.wait_for_timeout(1000)

        # Read form state + button state after proper datepicker setup
        state = await page.evaluate("""
            () => ({
                sel:   document.getElementById('export-select')?.value,
                start: document.getElementById('export-date-start')?.value,
                end:   document.getElementById('export-date-end')?.value,
                dpStart: (() => { try { return String($(document.getElementById('export-date-start')).datepicker('getDate')); } catch(e) { return 'err:'+e.message; }})(),
                dpEnd:   (() => { try { return String($(document.getElementById('export-date-end')).datepicker('getDate')); } catch(e) { return 'err:'+e.message; }})(),
                btnCls: document.getElementById('export-activity-list-submit')?.className,
            })
        """)
        print(f"  Form state: {json.dumps(state, indent=2)}")

        # Try clicking Export and capture ALL requests (no filter)
        print("\nCapturing ALL requests during Export click...")
        click_requests: list[str] = []
        click_responses: list[dict] = []

        async def on_req(req):
            click_requests.append(f"{req.method} {req.url}")
        async def on_resp(resp):
            try:
                body = await resp.body()
                click_responses.append({
                    "url": resp.url, "status": resp.status,
                    "len": len(body),
                    "body": body[:500].decode("utf-8-sig", errors="replace")
                })
            except Exception:
                click_responses.append({"url": resp.url, "status": resp.status})

        page.on("request", on_req)
        page.on("response", on_resp)

        await page.evaluate("""
            () => {
                const btn = document.getElementById('export-activity-list-submit');
                btn.classList.remove('disabled');
                btn.click();
            }
        """)
        await page.wait_for_timeout(8000)

        print(f"Requests after Export click ({len(click_requests)}):")
        for r in click_requests:
            print(f"  {r}")
        print(f"\nResponses after Export click ({len(click_responses)}):")
        for r in click_responses:
            print(f"  {r['status']}  {r['url']}")
            if r.get("len", 0) < 5000 and r.get("body"):
                print(f"    body: {r['body'][:300]!r}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
