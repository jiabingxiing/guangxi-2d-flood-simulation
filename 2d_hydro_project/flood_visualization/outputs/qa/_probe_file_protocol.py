"""临时探针（qa）：评估「双击 index.html（file://）」是否可用。

对比：
  A. file:// 默认参数（等同双击 Edge/Chrome）
  B. file:// + --allow-file-access-from-files
  C. http://127.0.0.1 基线（已知可用）
  D. 真实 Edge（channel=msedge，若已安装）

不修改任何代码/包；只读发布包 v2-20261027。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "outputs" / "web_bundle" / "v2-20261027"
FILE_URL = BUNDLE.as_uri() + "/index.html"
HTTP_URL = "http://127.0.0.1:8792/index.html"


def probe(p, label: str, url: str, *, args=None, channel=None) -> dict:
    result = {"label": label, "url": url, "channel": channel, "args": args or []}
    try:
        kwargs = {"headless": True}
        if channel:
            kwargs["channel"] = channel
        if args:
            kwargs["args"] = args
        browser = p.chromium.launch(**kwargs)
    except Exception as exc:  # noqa: BLE001
        result["launch_error"] = str(exc)[:400]
        print(f"[{label}] LAUNCH FAIL: {str(exc)[:200]}")
        return result

    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="zh-CN")
    page = ctx.new_page()
    console: list[str] = []
    pageerr: list[str] = []
    failures: list[str] = []
    page.on("console", lambda m: console.append(f"{m.type}: {m.text}"[:300]) if m.type == "error" else None)
    page.on("pageerror", lambda e: pageerr.append(str(e)[:300]))
    page.on("requestfailed", lambda r: failures.append(f"{r.url[:120]} :: {r.failure}"[:250]))
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(6000)
        state = None
        try:
            state = page.evaluate("() => (window.__fvDebug && window.__fvDebug.state()) || null")
        except Exception as exc:  # noqa: BLE001
            result["eval_error"] = str(exc)[:200]
        result.update(
            {
                "title": page.title(),
                "status_bundle": (page.text_content("#status-bundle") or "").strip()[:160],
                "state": {
                    "caseId": (state or {}).get("caseId"),
                    "frameId": (state or {}).get("frameId"),
                    "frameCount": (state or {}).get("frameCount"),
                }
                if state
                else None,
                "js_errors": page.evaluate("() => window.__fvErrors || []")[:5] if state is not None else None,
                "console_errors": console[:8],
                "page_errors": pageerr[:5],
                "request_failed": failures[:8],
                "failed_count": len(failures),
                "notice_hidden": page.eval_on_selector("#notice-bar", "el => el.hidden")
                if page.query_selector("#notice-bar")
                else None,
            }
        )
    except Exception as exc:  # noqa: BLE001
        result["goto_error"] = str(exc)[:400]
    finally:
        browser.close()
    print(f"\n===== {label} =====")
    print("goto_error:", result.get("goto_error"))
    print("launch_error:", result.get("launch_error"))
    print("state:", result.get("state"))
    print("status-bundle:", result.get("status_bundle"))
    print("console_errors:", result.get("console_errors"))
    print("page_errors:", result.get("page_errors"))
    print("failed_count:", result.get("failed_count"))
    for item in (result.get("request_failed") or [])[:4]:
        print("  failed:", item)
    return result


def main() -> int:
    if not (BUNDLE / "index.html").is_file():
        print(f"缺少发布包：{BUNDLE}")
        return 2
    report = []
    with sync_playwright() as p:
        report.append(probe(p, "A_file_default", FILE_URL))
        report.append(probe(p, "B_file_allow", FILE_URL, args=["--allow-file-access-from-files"]))
        report.append(probe(p, "C_http_baseline", HTTP_URL))
        report.append(probe(p, "D_edge_file_default", FILE_URL, channel="msedge"))
        report.append(probe(p, "E_edge_http", HTTP_URL, channel="msedge"))
        report.append(
            probe(p, "F_edge_file_allow", FILE_URL, channel="msedge", args=["--allow-file-access-from-files"])
        )
    out = ROOT / "outputs" / "qa" / "file_protocol_probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告：{out}")
    _ = sys
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
