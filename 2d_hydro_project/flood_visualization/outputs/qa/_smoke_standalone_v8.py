"""交付冒烟：直接服务 standalone/ 目录，用 headless Chromium 验证新前端。

只读，不改发布包；截图另存 evidence/12-style-v8.png，不覆盖既有证据。
"""
from __future__ import annotations

import functools
import http.server
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
STANDALONE = ROOT / "standalone" / "广西二维洪涝可视化_双击打开"
EVIDENCE = ROOT / "outputs" / "qa" / "evidence"


def main() -> int:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(STANDALONE))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/index.html"
    errors: list[str] = []
    results: list[tuple[str, bool, str]] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        results.append((name, bool(cond), detail))

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--force-device-scale-factor=1"])
            ctx = browser.new_context(viewport={"width": 1920, "height": 1080}, locale="zh-CN", device_scale_factor=1)
            page = ctx.new_page()
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" and "Failed to load resource" not in m.text else None)
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_function(
                "() => window.__fvReady && window.__fvDebug && window.__fvDebug.state().frameId !== null",
                timeout=60000,
            )
            page.wait_for_timeout(600)

            # 门户已完全移除
            check("portal-overlay removed", page.locator("#portal-overlay").count() == 0)
            check("btn-portal-toggle removed", page.locator("#btn-portal-toggle").count() == 0)
            check("mode pills removed", page.locator("#mode-fast, #mode-research, .dock-card, .portal-basin-card").count() == 0)
            check("brand-slogan removed", page.locator(".brand-slogan").count() == 0)

            # 工作台结构完整
            check("project title", "「驭洪」洪涝推演系统" in (page.locator("#project-title").text_content() or ""))
            check("3 case tabs", page.locator(".case-tab[data-case-id]").count() == 3, str(page.locator(".case-tab[data-case-id]").count()))
            check("layer list rendered", page.locator(".layer-item[data-layer]").count() >= 6, str(page.locator(".layer-item[data-layer]").count()))
            check("legend rendered", len((page.locator("#legend-body").text_content() or "").strip()) > 20)
            check("quality panel rendered", "质量守恒门" in (page.locator("#quality-body").text_content() or ""))
            check("chart note fact text", "不是资产安全等级" in (page.locator("#chart-note").text_content() or ""))
            check("origin note fact text", "候选依据" in (page.locator("#origin-note").text_content() or ""))
            check("status bundle v8", "v8-20261102" in (page.locator("#status-bundle").text_content() or ""))
            check("map canvas", page.evaluate("() => !!(window.__fvDebug && window.__fvDebug.hasMap())"))
            check("no glass blur", page.evaluate("() => getComputedStyle(document.getElementById('app-header')).backdropFilter === 'none'"))

            # 事实纪律 + 事实锚点配色（单主色）
            accent = page.evaluate("() => getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()")
            check("single accent token", accent == "#0369a1", accent)

            # 切贵港：v8 贵港 7 个 run（1 默认 + 6 新增层），三案例合计 11
            page.click(".case-tab[data-case-id='guigang']")
            page.wait_for_function(
                "() => { const s=window.__fvDebug.state(); return s.caseId==='guigang' && s.frameId!==null; }",
                timeout=90000,
            )
            run_count = page.locator("#run-select option").count()
            check("guigang 7 runs (v8 data)", run_count == 7, str(run_count))

            # 播放/逐帧/包络核心交互（切案例后等首帧提交稳定，再点逐帧）
            page.wait_for_function(
                "() => { const s=window.__fvDebug.state(); return s.index===0 && s.playing===false; }",
                timeout=30000,
            )
            page.wait_for_timeout(400)
            before_idx = page.evaluate("() => window.__fvDebug.state().index")
            page.click("#btn-next")
            page.wait_for_function(
                f"() => window.__fvDebug.state().index > {before_idx}", timeout=30000
            )
            check("next frame works", page.evaluate("() => window.__fvDebug.state().index") > before_idx)
            page.click("#btn-max")
            page.wait_for_timeout(300)
            check("max envelope title", "全程最大水深包络" in (page.locator("#map-title").text_content() or ""))
            page.click("#btn-max")
            page.wait_for_timeout(200)

            EVIDENCE.mkdir(parents=True, exist_ok=True)
            shot = EVIDENCE / "12-style-v8.png"
            page.screenshot(path=str(shot), full_page=False)
            check("screenshot saved", shot.is_file(), str(shot))

            browser.close()
    finally:
        server.shutdown()
        server.server_close()

    print(f"URL={url}")
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' :: ' + detail) if detail else ''}")
    print(f"JS_ERRORS={len(errors)}")
    for e in errors:
        print(f"  ! {e}")
    failed = [r for r in results if not r[1]]
    print(f"SUMMARY: {len(results) - len(failed)}/{len(results)} passed; js_errors={len(errors)}")
    return 1 if failed or errors else 0


if __name__ == "__main__":
    sys.exit(main())
