"""调试脚本：测量帧提交时延、缺帧提示、缓存状态。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8788/"
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_context(viewport={"width": 1920, "height": 1080}).new_page()
        logs = []
        page.on("console", lambda m: logs.append((m.type, m.text)) if m.type == "error" else None)
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_function("() => window.__fvDebug && window.__fvDebug.state().frameId !== null", timeout=30000)
        page.wait_for_timeout(500)
        print("initial:", json.dumps(page.evaluate("() => window.__fvDebug.state()")["cache"], ensure_ascii=False))

        # 逐帧提交耗时（点击下一帧 → 等到 index 变化）
        timings = []
        for _ in range(6):
            t0 = page.evaluate("() => performance.now()")
            idx0 = page.evaluate("() => window.__fvDebug.state().index")
            page.click("#btn-next")
            page.wait_for_function(
                "prev => window.__fvDebug.state().index !== prev", arg=idx0, timeout=15000
            )
            t1 = page.evaluate("() => performance.now()")
            timings.append(round(t1 - t0))
        print("frame commit ms:", timings)

        # 缺帧：跳到未预取的帧并 404
        page.evaluate("""() => { const el=document.getElementById('frame-slider'); el.value='1'; el.dispatchEvent(new Event('input',{bubbles:true})); }""")
        page.wait_for_timeout(1500)
        print("at index:", page.evaluate("() => window.__fvDebug.state().index"))
        page.route("**/*.png", lambda route: route.fulfill(status=404, body="missing") if "/f0010_r" in route.request.url or "/f0010." in route.request.url else route.continue_())
        page.evaluate("""() => { const el=document.getElementById('frame-slider'); el.value='10'; el.dispatchEvent(new Event('input',{bubbles:true})); }""")
        page.wait_for_timeout(3000)
        st = page.evaluate("() => window.__fvDebug.state()")
        print("after 404: index:", st["index"], "lastError:", page.evaluate("() => window.__fvDebug.state().errors"))
        print("notice:", (page.text_content("#notice-bar") or "")[:300])
        print("console errors:", logs[:6])
        page.screenshot(path=str(Path(__file__).parent / "screenshots" / "dbg-missing.png"))
        b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
