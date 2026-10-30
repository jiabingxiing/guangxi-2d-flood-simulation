import re
from playwright.sync_api import sync_playwright
URL = "http://127.0.0.1:8789/"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(viewport={"width": 1600, "height": 900})
    page = ctx.new_page()
    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_function("() => window.__fvDebug && window.__fvDebug.state().frameId !== null", timeout=120000)
    page.wait_for_timeout(500)
    ctx.route(re.compile(r"arcgisonline\.com"), lambda route: route.abort())
    page.click("#basemap-toggle")
    page.wait_for_timeout(6000)
    print("warn visible:", page.is_visible("#basemap-warn"), "|", (page.text_content("#basemap-warn") or "")[:150])
    print("retry visible:", page.is_visible("#basemap-retry"))
    print("notice:", (page.text_content("#notice-bar") or "")[:220])
    print("attribution:", (page.text_content("#status-attribution") or "")[:100])
    print("flood layer still visible:", page.evaluate("() => window.__fvDebug.state().layers.flood.visible"))
    page.screenshot(path="D:/NWKYYSJZX/2026年工作/1023 久隆 贵港 宁明 二维水动力模拟/2d_hydro_project/flood_visualization/web/.dev/screenshots/dbg-basemap-fail.png")
    b.close()
