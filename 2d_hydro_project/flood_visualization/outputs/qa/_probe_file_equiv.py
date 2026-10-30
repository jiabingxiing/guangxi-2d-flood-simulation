"""临时探针（qa）：file:// 两种模式的交互等价性与失败现场取证。

P1. file:// + --allow-file-access-from-files：切案例、拖帧、点查、面积曲线、控制台/4xx
P2. file:// 默认（等同双击）：抓 notice 文案与截图，确认用户看到的是明确错误而非白屏
"""

from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "outputs" / "web_bundle" / "v2-20261027"
FILE_URL = BUNDLE.as_uri() + "/index.html"
OUT = ROOT / "outputs" / "qa" / "evidence"
OUT.mkdir(parents=True, exist_ok=True)


def main() -> int:
    report: dict = {}
    with sync_playwright() as p:
        # ---------- P2 失败现场（默认参数 = 双击） ----------
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 900}, locale="zh-CN")
        page = ctx.new_page()
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text[:200]) if m.type == "error" else None)
        page.goto(FILE_URL, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3000)
        report["default_file"] = {
            "status_bundle": (page.text_content("#status-bundle") or "").strip(),
            "notice_hidden": page.eval_on_selector("#notice-bar", "el => el.hidden"),
            "notice": (page.text_content("#notice-bar") or "").strip()[:300],
            "query_body": (page.text_content("#query-body") or "").strip()[:200],
            "frame_readout": (page.text_content("#frame-readout") or "").strip(),
            "console_error_count": len(errors),
            "console_sample": errors[:3],
            "body_text_head": (page.text_content("body") or "").strip()[:300],
        }
        page.screenshot(path=str(OUT / "09-file-protocol-failure.png"))
        browser.close()

        # ---------- P1 带 flag 的等价性 ----------
        browser = p.chromium.launch(headless=True, args=["--allow-file-access-from-files"])
        ctx = browser.new_context(viewport={"width": 1920, "height": 1080}, locale="zh-CN")
        page = ctx.new_page()
        console_errors: list[str] = []
        local_4xx: list[str] = []
        page.on("console", lambda m: console_errors.append(m.text[:200]) if m.type == "error" else None)
        page.on(
            "response",
            lambda r: local_4xx.append(f"{r.status} {r.url[:100]}")
            if r.url.startswith("file://") and r.status >= 400
            else None,
        )
        page.goto(FILE_URL, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_function(
            "() => window.__fvDebug && window.__fvDebug.state().frameId !== null", timeout=30000
        )
        page.wait_for_timeout(1200)

        def state():
            return page.evaluate("() => window.__fvDebug.state()")

        initial = state()
        # 拖帧到 10
        page.eval_on_selector(
            "#frame-slider",
            "el => { el.value = '10'; el.dispatchEvent(new Event('input', {bubbles:true})); }",
        )
        page.wait_for_function("() => window.__fvDebug.state().index === 10", timeout=30000)
        after_scrub = {
            "index": state()["index"],
            "frameId": state()["frameId"],
            "time_relative": (page.text_content("#time-relative") or "").strip(),
            "frame_readout": (page.text_content("#frame-readout") or "").strip(),
        }
        # 点查
        payload = page.evaluate("() => window.__fvDebug.query(108.72, 22.06)")
        # 图表
        painted = page.evaluate(
            """() => { const c=document.getElementById('area-chart'); const d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;
                       let n=0; for (let i=3;i<d.length;i+=4) if (d[i]>0) n+=1; return n; }"""
        )
        # 切贵港
        page.click(".case-tab[data-case-id='guigang']")
        page.wait_for_function("() => window.__fvDebug.state().caseId === 'guigang'", timeout=60000)
        page.wait_for_timeout(2500)
        guigang = {
            "caseId": state()["caseId"],
            "frameCount": state()["frameCount"],
            "time_absolute": (page.text_content("#time-absolute") or "").strip(),
            "dem_available": state()["layers"]["dem"]["available"],
            "sar_available": state()["layers"]["sar"]["available"],
        }
        page.screenshot(path=str(OUT / "10-file-protocol-with-flag.png"))
        report["file_with_flag"] = {
            "initial": {
                "caseId": initial["caseId"],
                "frameCount": initial["frameCount"],
                "frameId": initial["frameId"],
            },
            "after_scrub": after_scrub,
            "query": json.loads(json.dumps(payload, ensure_ascii=False))[:1] if False else payload,
            "chart_painted_pixels": painted,
            "guigang": guigang,
            "console_errors": console_errors[:5],
            "local_4xx": local_4xx[:5],
            "js_errors": page.evaluate("() => window.__fvErrors || []")[:5],
        }
        browser.close()

    out = ROOT / "outputs" / "qa" / "file_protocol_equiv.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1)[:4000])
    print(f"\n报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
