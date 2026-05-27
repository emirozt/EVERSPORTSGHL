#!/usr/bin/env python3
"""Find the Eversports scheduler export API by searching all lazy-loaded JS chunks."""
import asyncio, json, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

BASE_URL = "https://app.eversportsmanager.com"

def _load_cookies():
    for line in (Path(__file__).parent.parent / ".env").read_text().splitlines():
        if line.startswith("EVERSPORTS_TEST_COOKIE_JSON="):
            raw = line.split("=", 1)[1]
            break
    _FIELDS = {"name","value","domain","path","expires","httpOnly","secure","sameSite"}
    _SS = {"strict":"Strict","lax":"Lax","none":"None","no_restriction":"None"}
    out = []
    for c in json.loads(raw):
        cl = {k:v for k,v in c.items() if k in _FIELDS}
        if "expires" not in cl and "expirationDate" in c:
            cl["expires"] = c["expirationDate"]
        if isinstance(cl.get("sameSite"), str):
            cl["sameSite"] = _SS.get(cl["sameSite"].lower(), cl["sameSite"])
        out.append(cl)
    return out

async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        await context.add_cookies(_load_cookies())

        # Collect ALL JS URLs loaded on the scheduler page
        all_js_urls: list[str] = []
        def on_req(req):
            if ".js" in req.url and req.resource_type == "script":
                all_js_urls.append(req.url)

        page = await context.new_page()
        page.on("request", on_req)

        await page.goto(
            f"{BASE_URL}/admin/Yneu3U/scheduler/overview?modal=export-class-list&export=active",
            timeout=30_000, wait_until="networkidle"
        )
        await page.wait_for_timeout(3000)

        print(f"All JS files loaded ({len(all_js_urls)}):")
        for u in all_js_urls:
            print(f"  {u}")

        # Search all of them for export handler
        terms = [
            "export-activity-list-submit",
            "export-date-start",
            "checkExport",
            "missingInformation",
            "export-download-link",
            "exportDateStart",
            "/admin-event-export",
            "/event-export",
            "eventExport",
            "export_start",
            "exportFrom",
        ]

        for js_url in all_js_urls:
            try:
                resp = await context.request.get(js_url, timeout=15_000)
                if resp.status != 200:
                    continue
                src = (await resp.body()).decode("utf-8", errors="replace")
                found_terms = [t for t in terms if t in src]
                if found_terms:
                    print(f"\n=== {js_url.split('/')[-1]} ({len(src):,} bytes) — terms: {found_terms}")
                    for term in found_terms:
                        idx = src.find(term)
                        snippet = src[max(0, idx-300):idx+500]
                        print(f"\n  -- {term!r} --\n{snippet}\n")
            except Exception as exc:
                print(f"  {js_url}: {exc}")

        # Also: read the body data attributes (the dateformat comes from body data attrs)
        body_data = await page.evaluate("""
            () => {
                const b = document.body;
                const attrs = {};
                for (const a of b.attributes) {
                    if (a.name.startsWith('data-')) attrs[a.name] = a.value;
                }
                return attrs;
            }
        """)
        print(f"\nBody data attributes: {json.dumps(body_data, indent=2)}")

        await browser.close()

asyncio.run(main())
