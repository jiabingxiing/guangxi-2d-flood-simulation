import json, time
from playwright.sync_api import sync_playwright
URL = "http://127.0.0.1:8789/"
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(viewport={"width": 1920, "height": 1080})
    page = ctx.new_page()
    errs = []
    page.on("console", lambda m: errs.append(m.text[:150]) if m.type == "error" and "Failed to load resource" not in m.text else None)
    page.on("pageerror", lambda e: errs.append("pageerror:" + str(e)[:150]))
    t0 = time.perf_counter()
    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_function("() => window.__fvDebug && window.__fvDebug.state().frameId !== null", timeout=180000)
    t_first = time.perf_counter() - t0
    page.wait_for_timeout(400)
    st = page.evaluate("() => window.__fvDebug.state()")
    t1 = time.perf_counter()
    page.click(".case-tab[data-case-id='guigang']")
    page.wait_for_function("() => window.__fvDebug.state().caseId==='guigang' && window.__fvDebug.state().frameId!==null", timeout=180000)
    t_g = time.perf_counter() - t1
    st2 = page.evaluate("() => window.__fvDebug.state()")
    print(json.dumps({"cold_start_to_first_frame_s": round(t_first,2), "jiulong_frame": st["frameId"],
        "guigang_switch_s": round(t_g,2), "guigang_frames": st2["frameCount"],
        "cache": st2["cache"], "console_errors": errs[:5]}, ensure_ascii=False, indent=1))
    b.close()
