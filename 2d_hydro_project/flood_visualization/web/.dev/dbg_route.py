from playwright.sync_api import sync_playwright
URL = "http://127.0.0.1:8789/"
URL_ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/10/447/821"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    for pattern in ["**/arcgisonline.com/**", "**/arcgisonline.com/*", "**/*arcgisonline*", "**/*"]:
        ctx = b.new_context()
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded")
        seen = []
        ctx.route(pattern, lambda route: (seen.append(route.request.url), route.abort()))
        page.wait_for_timeout(200)
        ok = page.evaluate("""async (u) => {
            try { await fetch(u, {mode:'no-cors'}); return 'fetch-ok'; }
            catch (e) { return 'fetch-blocked:' + e.name; }
        }""", URL_ESRI)
        print("%-24s handler_calls=%d  result=%s" % (pattern, len(seen), ok))
        ctx.close()
    b.close()
