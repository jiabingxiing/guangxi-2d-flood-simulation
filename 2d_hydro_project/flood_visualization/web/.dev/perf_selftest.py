"""性能与缓存自测（Playwright）：冷启动、切帧时延、播放稳定性、内存增长。

用法：
  .venv/Scripts/python.exe web/.dev/perf_selftest.py --url http://127.0.0.1:8788/
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OUT = Path(__file__).resolve().parent / "screenshots"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8788/")
    ap.add_argument("--case", default="guigang", help="压力测试案例（默认贵港 2×2 parts / 57 帧）")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    report: dict = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1920, "height": 1080})
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "Failed to load resource" not in m.text else None)

        # 冷启动计时：从 goto 到首帧提交
        t0 = page.evaluate("() => performance.now()") if False else 0
        page.goto(args.url, wait_until="domcontentloaded")
        nav_start = page.evaluate("() => performance.now()")
        page.wait_for_function("() => window.__fvDebug && window.__fvDebug.state().frameId !== null", timeout=60000)
        ready_ms = page.evaluate("() => performance.now()") - nav_start
        report["cold_start_ms"] = round(ready_ms)

        # 切到贵港（较大 parts）
        page.click(".case-tab[data-case-id='%s']" % args.case)
        page.wait_for_function(
            "c => window.__fvDebug.state().caseId === c && window.__fvDebug.state().frameId !== null",
            arg=args.case,
            timeout=60000,
        )
        page.wait_for_timeout(1200)

        # 逐帧（预取命中）时延
        timings = []
        for _ in range(10):
            idx0 = page.evaluate("() => window.__fvDebug.state().index")
            t = page.evaluate("() => performance.now()")
            page.click("#btn-next")
            page.wait_for_function("prev => window.__fvDebug.state().index !== prev", arg=idx0, timeout=20000)
            timings.append(page.evaluate("() => performance.now()") - t)
        report["frame_step_ms"] = {
            "n": len(timings),
            "median": round(statistics.median(timings)),
            "p95": round(sorted(timings)[max(0, int(len(timings) * 0.95) - 1)]),
            "max": round(max(timings)),
        }

        # 播放稳定性：4 帧/秒跑 5 秒，统计实际推进帧数
        page.evaluate("() => { const b=[...document.querySelectorAll('.speed-btn')].find(x=>x.dataset.speed==='4'); b.click(); }")
        idx_start = page.evaluate("() => window.__fvDebug.state().index")
        page.click("#btn-play")
        page.wait_for_timeout(5000)
        idx_end = page.evaluate("() => window.__fvDebug.state().index")
        page.click("#btn-play")
        report["playback_5s_frames_advanced"] = idx_end - idx_start
        report["playback_expected_at_4fps"] = 20

        # 点查时延（同一像元重复 20 次，含缓存命中）
        page.evaluate(
            """() => {
                const el = document.getElementById('frame-slider');
                el.value = '10';
                el.dispatchEvent(new Event('input', {bubbles:true}));
            }"""
        )
        page.wait_for_timeout(1500)
        box = page.eval_on_selector("#map", "el => { const r = el.getBoundingClientRect(); return {x:r.x+r.width/2, y:r.y+r.height/2}; }")
        qtimings = []
        for _ in range(20):
            t = page.evaluate("() => performance.now()")
            page.mouse.click(box["x"], box["y"])
            page.wait_for_function("() => !document.querySelector('#query-body .hint') || !/查询中/.test(document.querySelector('#query-body').textContent)", timeout=15000)
            qtimings.append(page.evaluate("() => performance.now()") - t)
        report["point_query_ms"] = {
            "n": len(qtimings),
            "median": round(statistics.median(qtimings)),
            "p95": round(sorted(qtimings)[max(0, int(len(qtimings) * 0.95) - 1)]),
        }

        # 缓存状态
        st = page.evaluate("() => window.__fvDebug.state()")
        report["cache"] = st["cache"]

        # 内存（JS heap）
        metrics = page.evaluate("() => performance.memory ? {used: performance.memory.usedJSHeapSize, total: performance.memory.totalJSHeapSize} : null")
        report["js_heap_mb"] = round(metrics["used"] / 1048576, 1) if metrics else None

        # 页面缩放/布局检查：无横向滚动
        overflow = page.evaluate("() => ({sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth})")
        report["layout_no_hscroll"] = overflow["sw"] <= overflow["cw"]
        report["layout"] = overflow

        report["console_errors"] = errors
        (OUT / "perf-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=1))
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
