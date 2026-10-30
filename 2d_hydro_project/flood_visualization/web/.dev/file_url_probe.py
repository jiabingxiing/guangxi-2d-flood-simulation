import json
from playwright.sync_api import sync_playwright

url = "file:///D:/NWKYYSJZX/2026%E5%B9%B4%E5%B7%A5%E4%BD%9C/1023%20%E4%B9%85%E9%9A%86%20%E8%B4%B5%E6%B8%AF%20%E5%AE%81%E6%98%8E%20%E4%BA%8C%E7%BB%B4%E6%B0%B4%E5%8A%A8%E5%8A%9B%E6%A8%A1%E6%8B%9F/2d_hydro_project/flood_visualization/outputs/web_bundle/v2-20261027/index.html"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    page = b.new_context(viewport={"width": 1600, "height": 900}).new_page()
    msgs = []
    page.on("console", lambda m: msgs.append((m.type, m.text[:200])))
    page.on("pageerror", lambda e: msgs.append(("pageerror", str(e)[:200])))
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    state = page.evaluate("() => ({ ready: typeof window.__fvReady, dbg: !!window.__fvDebug, notice: (document.getElementById('notice-bar')||{}).textContent || '', title: (document.getElementById('frame-readout')||{}).textContent || '' })")
    print("STATE:", json.dumps(state, ensure_ascii=False))
    print("CONSOLE:", json.dumps(msgs[:8], ensure_ascii=False))
    b.close()
