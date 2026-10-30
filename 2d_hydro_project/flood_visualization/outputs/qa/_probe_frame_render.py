"""临时探针（qa）：久隆 f0000（全干）vs f0010（198 km²）地图截图差分。

目的：判断"逐帧水层是否真的贴图"，并排除点击开关未生效的干扰（同时读 __fvDebug 状态）。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
SHOT = ROOT / "outputs" / "qa" / "evidence"
HTTP_URL = "http://127.0.0.1:8792/index.html"


def shot(page, name: str) -> Path:
    SHOT.mkdir(parents=True, exist_ok=True)
    path = SHOT / name
    page.locator("#map").screenshot(path=str(path))
    return path


def diff(a: Path, b: Path) -> dict:
    ia = np.asarray(Image.open(a).convert("RGB"), dtype=np.int16)
    ib = np.asarray(Image.open(b).convert("RGB"), dtype=np.int16)
    delta = np.abs(ia - ib).sum(axis=2)
    changed = int((delta > 12).sum())
    return {"changed": changed, "ratio": round(changed / delta.size, 4), "max": int(delta.max())}


def main() -> int:
    out: dict = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 900}, locale="zh-CN")
        page = ctx.new_page()
        pngs: list[str] = []
        page.on("request", lambda r: pngs.append(r.url.rsplit("/", 1)[-1]) if r.url.endswith(".png") else None)
        page.goto(HTTP_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_function("() => window.__fvDebug && window.__fvDebug.state().frameId !== null", timeout=60000)
        page.wait_for_timeout(2500)

        s0 = page.evaluate("() => window.__fvDebug.state()")
        img_f0 = shot(page, "dbg-f0000.png")

        page.eval_on_selector(
            "#frame-slider", "el => { el.value = '10'; el.dispatchEvent(new Event('input', {bubbles:true})); }"
        )
        page.wait_for_function("() => window.__fvDebug.state().index === 10", timeout=60000)
        page.wait_for_timeout(4000)
        s10 = page.evaluate("() => window.__fvDebug.state()")
        img_f10 = shot(page, "dbg-f0010.png")

        # 用 JS 直接隐藏水层（绕开可能的点击问题）
        page.evaluate("() => { document.getElementById('layer-cb-flood').click(); }")
        page.wait_for_timeout(2500)
        s_hidden = page.evaluate("() => window.__fvDebug.state()")
        img_hidden = shot(page, "dbg-f0010-flood-hidden.png")

        out["state_f0000"] = {k: s0["layers"]["flood"][k] for k in ("status", "available", "visible", "opacity")}
        out["state_f0010"] = {k: s10["layers"]["flood"][k] for k in ("status", "available", "visible", "opacity")}
        out["state_after_hide"] = {k: s_hidden["layers"]["flood"][k] for k in ("status", "available", "visible", "opacity")}
        out["wetPixels"] = {"f0000": s0["wetPixels"], "f0010": s10["wetPixels"]}
        out["map_title"] = (page.text_content("#map-title") or "").strip()
        out["subtitle"] = (page.text_content("#map-subtitle") or "").strip()
        out["diff_f0000_vs_f0010"] = diff(img_f0, img_f10)
        out["diff_f0010_vs_hidden"] = diff(img_f10, img_hidden)
        out["frame_pngs"] = sorted({n for n in pngs if n.startswith("f0010")})
        out["frame_pngs_dry"] = sorted({n for n in pngs if n.startswith("f0000")})
        out["js_errors"] = page.evaluate("() => window.__fvErrors || []")[:5]
        out["cache_stats"] = s10.get("cache")
        browser.close()
    print(json.dumps(out, ensure_ascii=False, indent=1))
    path = ROOT / "outputs" / "qa" / "frame_render_probe.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
