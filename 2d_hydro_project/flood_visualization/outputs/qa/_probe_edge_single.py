"""临时探针（qa）：Edge 单实例复用是否吞掉 --allow-file-access-from-files。

步骤：
1) 用持久化 profile P 起一个 headed Edge（模拟"用户已开着 Edge"）；
2) 另起进程 `msedge --allow-file-access-from-files --user-data-dir=P <file url>`；
3) 检查 (1) 里的所有页面，看是否新增页面、该页面能否 fetch catalog.json。

结论用途：决定 file:// 方案是否必须带独立 --user-data-dir。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = (ROOT / "outputs" / "web_bundle" / "v2-20261027").resolve()
FILE_URL = BUNDLE.as_uri() + "/index.html"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"


def probe_page(page) -> dict:
    try:
        return {
            "url": page.url[:120],
            "title": page.title()[:80],
            "ready": bool(page.evaluate("() => !!(window.__fvDebug && window.__fvDebug.state().frameId)")),
            "bundle": (page.text_content("#status-bundle") or "").strip()[:120],
        }
    except Exception as exc:  # noqa: BLE001
        return {"url": getattr(page, "url", "?")[:120], "error": str(exc)[:160]}


def main() -> int:
    out: dict = {}
    profile = Path(tempfile.mkdtemp(prefix="fv_edge_shared_"))
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile), headless=False, channel="msedge", viewport={"width": 1280, "height": 800}
        )
        first = ctx.pages[0] if ctx.pages else ctx.new_page()
        first.goto("about:blank")
        out["instance_A_pages_before"] = [probe_page(page) for page in ctx.pages]

        proc = subprocess.run(
            [EDGE, "--allow-file-access-from-files", f"--user-data-dir={profile}", FILE_URL],
            capture_output=True,
            text=True,
            timeout=90,
        )
        time.sleep(6)
        pages = list(ctx.pages)
        out["instance_B"] = {
            "exit": proc.returncode,
            "stdout": (proc.stdout or "")[-200:],
            "stderr": (proc.stderr or "")[-200:],
        }
        out["instance_A_pages_after"] = [probe_page(page) for page in pages]
        out["handoff_new_page_loaded"] = any(
            (probe_page(page).get("ready") is True) for page in pages
        )
        ctx.close()
        subprocess.run(["taskkill", "/IM", "msedge.exe", "/F"], capture_output=True, text=True)

    path = ROOT / "outputs" / "qa" / "edge_single_instance.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1)[:2000])
    print(f"\n报告：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
