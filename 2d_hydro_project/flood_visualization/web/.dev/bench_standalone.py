import json, time
from playwright.sync_api import sync_playwright
URL = "http://127.0.0.1:8796/"
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(viewport={"width": 1920, "height": 1080})
    page = ctx.new_page()
    errs, net_fail = [], []
    page.on("console", lambda m: errs.append(m.text[:160]) if m.type == "error" and "Failed to load resource" not in m.text else None)
    page.on("pageerror", lambda e: errs.append("pageerror:" + str(e)[:160]))
    page.on("requestfailed", lambda r: net_fail.append(r.url[:120]))
    t0 = time.perf_counter()
    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_function("() => window.__fvDebug && window.__fvDebug.state().frameId !== null", timeout=180000)
    t_first = time.perf_counter() - t0
    # 播放 10 帧 @4fps
    page.evaluate("() => { const b=[...document.querySelectorAll('.speed-btn')].find(x=>x.dataset.speed==='4'); b.click(); }")
    page.click("#btn-play"); page.wait_for_timeout(4000); page.click("#btn-play")
    st = page.evaluate("() => window.__fvDebug.state()")
    # 切贵港（大 parts）
    t1 = time.perf_counter()
    page.click(".case-tab[data-case-id='guigang']")
    page.wait_for_function("() => window.__fvDebug.state().caseId==='guigang' && window.__fvDebug.state().frameId!==null", timeout=180000)
    t_guigang = time.perf_counter() - t1
    st2 = page.evaluate("() => window.__fvDebug.state()")
    print(json.dumps({"first_frame_s": round(t_first,2), "jiulong_frame": st["frameId"], "jiulong_index": st["index"],
                      "guigang_switch_s": round(t_guigang,2), "guigang_frame": st2["frameId"],
                      "cache": st2["cache"], "errors": errs[:5], "net_fail": net_fail[:5]}, ensure_ascii=False, indent=1))
    b.close()
