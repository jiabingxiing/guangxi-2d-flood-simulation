"""临时探针（qa）：对任一本地 URL 做功能等价性检查（用于比较 file:// / PS server / python server）。

用法：python _probe_any_url.py http://127.0.0.1:8793/index.html [label]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8793/index.html"
    label = sys.argv[2] if len(sys.argv) > 2 else "probe"
    report: dict = {"label": label, "url": url}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1920, "height": 1080}, locale="zh-CN")
        page = ctx.new_page()
        console: list[str] = []
        pageerr: list[str] = []
        statuses: dict[str, int] = {}
        page.on("console", lambda m: console.append(m.text[:200]) if m.type == "error" else None)
        page.on("pageerror", lambda e: pageerr.append(str(e)[:200]))
        page.on("response", lambda r: statuses.setdefault(r.url.rsplit("/", 1)[-1][:40], r.status))
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_function(
                "() => window.__fvDebug && window.__fvDebug.state().frameId !== null", timeout=30000
            )
            state = page.evaluate("() => window.__fvDebug.state()")
            page.eval_on_selector(
                "#frame-slider",
                "el => { el.value = '10'; el.dispatchEvent(new Event('input', {bubbles:true})); }",
            )
            page.wait_for_function("() => window.__fvDebug.state().index === 10", timeout=30000)
            query = page.evaluate("() => window.__fvDebug.query(108.72, 22.06)")
            chart = page.evaluate(
                """() => { const c=document.getElementById('area-chart'); const d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;
                           let n=0; for (let i=3;i<d.length;i+=4) if (d[i]>0) n+=1; return n; }"""
            )
            page.click(".case-tab[data-case-id='guigang']")
            page.wait_for_function("() => window.__fvDebug.state().caseId === 'guigang'", timeout=90000)
            page.wait_for_timeout(2500)
            gg = page.evaluate("() => window.__fvDebug.state()")
            report.update(
                {
                    "ok": True,
                    "jiulong_frames": state["frameCount"],
                    "scrub_frameId": "f0010",
                    "query_state": query.get("state"),
                    "query_value": query.get("value"),
                    "chart_painted": chart,
                    "guigang_frames": gg["frameCount"],
                    "guigang_time_abs": (page.text_content("#time-absolute") or "").strip(),
                    "status_bundle": (page.text_content("#status-bundle") or "").strip(),
                }
            )
        except Exception as exc:  # noqa: BLE001
            report.update(
                {
                    "ok": False,
                    "error": str(exc)[:300],
                    "status_bundle": (page.text_content("#status-bundle") or "").strip(),
                    "notice": (page.text_content("#notice-bar") or "").strip()[:300],
                }
            )
        report.update(
            {
                "console_errors": console[:5],
                "page_errors": pageerr[:5],
                "sample_statuses": dict(list(statuses.items())[:10]),
                "js_errors": page.evaluate("() => window.__fvErrors || []")[:5],
            }
        )
        browser.close()
    print(json.dumps(report, ensure_ascii=False, indent=1))
    out = ROOT / "outputs" / "qa" / f"probe_{label}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
