"""临时探针（qa）：Edge 双击可行性的两种通道对比。

1) file:// 默认（= 双击 index.html）
2) file:// + --allow-file-access-from-files（启动器带参数）
3) file:// + flag + 独立 user-data-dir（launch_persistent_context，模拟"专用配置"）
4) http://127.0.0.1 基线
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = (ROOT / "outputs" / "web_bundle" / "v2-20261027").resolve()
FILE_URL = BUNDLE.as_uri() + "/index.html"
HTTP_URL = "http://127.0.0.1:8792/index.html"


def collect(page) -> dict:
    return {
        "bundle": (page.text_content("#status-bundle") or "").strip(),
        "notice": (page.text_content("#notice-bar") or "").strip()[:160],
        "js_errors": page.evaluate("() => window.__fvErrors || []")[:3],
    }


def probe(p, label: str, url: str, *, channel: str | None = None, args: list[str] | None = None) -> dict:
    entry: dict = {"label": label, "url": url, "channel": channel, "args": args or []}
    try:
        if channel:
            browser = p.chromium.launch(headless=True, channel=channel, args=args or [])
            ctx = browser.new_context(viewport={"width": 1920, "height": 1080}, locale="zh-CN")
            closer = browser.close
        else:
            browser = p.chromium.launch(headless=True, args=args or [])
            ctx = browser.new_context(viewport={"width": 1920, "height": 1080}, locale="zh-CN")
            closer = browser.close
    except Exception as exc:  # noqa: BLE001
        entry["launch_error"] = str(exc)[:200]
        return entry
    page = ctx.new_page()
    errs: list[str] = []
    page.on("pageerror", lambda e: errs.append(str(e)[:150]))
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    ready = False
    try:
        page.wait_for_function(
            "() => window.__fvDebug && window.__fvDebug.state().frameId !== null", timeout=60000
        )
        ready = True
    except Exception:  # noqa: BLE001
        pass
    state = page.evaluate("() => (window.__fvDebug && window.__fvDebug.state()) || null")
    entry.update(
        {
            "ready": ready,
            "caseId": (state or {}).get("caseId"),
            "frames": (state or {}).get("frameCount"),
            "page_errors": errs[:3],
            **collect(page),
        }
    )
    closer()
    return entry


def probe_persistent(p, label: str, url: str) -> dict:
    entry: dict = {"label": label, "url": url}
    profile = tempfile.mkdtemp(prefix="fv_edge_profile_")
    try:
        ctx = p.chromium.launch_persistent_context(
            profile,
            headless=True,
            channel="msedge",
            args=["--allow-file-access-from-files"],
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )
    except Exception as exc:  # noqa: BLE001
        entry["launch_error"] = str(exc)[:300]
        return entry
    page = ctx.new_page()
    errs: list[str] = []
    page.on("pageerror", lambda e: errs.append(str(e)[:150]))
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    ready = False
    try:
        page.wait_for_function(
            "() => window.__fvDebug && window.__fvDebug.state().frameId !== null", timeout=60000
        )
        ready = True
    except Exception:  # noqa: BLE001
        pass
    state = page.evaluate("() => (window.__fvDebug && window.__fvDebug.state()) || null")
    entry.update(
        {
            "ready": ready,
            "caseId": (state or {}).get("caseId"),
            "frames": (state or {}).get("frameCount"),
            "page_errors": errs[:3],
            **collect(page),
        }
    )
    ctx.close()
    return entry


def main() -> int:
    rows = []
    with sync_playwright() as p:
        rows.append(probe(p, "edge_file_default", FILE_URL, channel="msedge"))
        rows.append(probe(p, "edge_file_flag", FILE_URL, channel="msedge", args=["--allow-file-access-from-files"]))
        rows.append(probe_persistent(p, "edge_file_flag_persistent_profile", FILE_URL))
        rows.append(probe(p, "edge_http_baseline", HTTP_URL, channel="msedge"))
        rows.append(probe(p, "chromium_file_default", FILE_URL))
        rows.append(probe(p, "chromium_file_flag", FILE_URL, args=["--allow-file-access-from-files"]))
    out = ROOT / "outputs" / "qa" / "file_protocol_matrix.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    for row in rows:
        print(f"{row['label']:36s} ready={row.get('ready')} case={row.get('caseId')} frames={row.get('frames')} "
              f"err={row.get('launch_error','')[:40]} js={len(row.get('js_errors') or [])} page={len(row.get('page_errors') or [])}")
        if not row.get("ready"):
            print(f"    bundle={row.get('bundle')!r} notice={row.get('notice')!r}")
    print(f"\n报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
