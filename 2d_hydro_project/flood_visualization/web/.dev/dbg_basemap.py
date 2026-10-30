import json
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8789/"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(viewport={"width": 1600, "height": 900})
    page = ctx.new_page()
    reqs = []
    page.on("request", lambda r: reqs.append(r.url) if "arcgisonline" in r.url else None)
    page.on("requestfailed", lambda r: reqs.append("FAILED:" + r.url) if "arcgisonline" in r.url else None)
    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_function("() => window.__fvDebug && window.__fvDebug.state().frameId !== null", timeout=120000)
    page.wait_for_timeout(600)
    print("basemap toggle checked before:", page.eval_on_selector("#basemap-toggle", "el => el.checked"))
    ctx.route("**/arcgisonline.com/**", lambda route: route.abort())
    page.click("#basemap-toggle")
    page.wait_for_timeout(5000)
    print("toggle checked after:", page.eval_on_selector("#basemap-toggle", "el => el.checked"))
    print("warn visible:", page.is_visible("#basemap-warn"), "| text:", (page.text_content("#basemap-warn") or "")[:120])
    print("retry visible:", page.is_visible("#basemap-retry"))
    print("notice:", (page.text_content("#notice-bar") or "")[:200])
    print("attribution:", (page.text_content("#status-attribution") or "")[:120])
    print("arcgis requests:", reqs[:6])
    b.close()
