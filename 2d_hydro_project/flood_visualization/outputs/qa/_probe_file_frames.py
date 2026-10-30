"""临时探针（qa）：file://+flag 下逐帧贴图是否真的生效（f0000 全干 vs f0010 198 km²）。"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = (ROOT / "outputs" / "web_bundle" / "v2-20261027").resolve()
FILE_URL = BUNDLE.as_uri() + "/index.html"
SHOT = ROOT / "outputs" / "qa" / "evidence"


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
        browser = p.chromium.launch(headless=True, channel="msedge", args=["--allow-file-access-from-files"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 900}, locale="zh-CN")
        page = ctx.new_page()
        pngs: list[str] = []
        page.on("request", lambda r: pngs.append(r.url.rsplit("/", 1)[-1]) if r.url.endswith(".png") else None)
        page.goto(FILE_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_function("() => window.__fvDebug && window.__fvDebug.state().frameId !== null", timeout=90000)
        page.wait_for_timeout(2500)
        dry = shot(page, "file-flag-f0000-dry.png")
        page.eval_on_selector(
            "#frame-slider", "el => { el.value = '10'; el.dispatchEvent(new Event('input', {bubbles:true})); }"
        )
        page.wait_for_function("() => window.__fvDebug.state().index === 10", timeout=90000)
        page.wait_for_timeout(4000)
        wet = shot(page, "file-flag-f0010-wet.png")
        state = page.evaluate("() => window.__fvDebug.state()")
        page.evaluate("() => { document.getElementById('layer-cb-flood').click(); }")
        page.wait_for_timeout(2500)
        hidden = shot(page, "file-flag-f0010-hidden.png")
        out.update(
            {
                "wetPixels": {"f0000": 0, "f0010": state["wetPixels"]},
                "subtitle": (page.text_content("#map-subtitle") or "").strip(),
                "diff_dry_vs_wet": diff(dry, wet),
                "diff_wet_vs_hidden": diff(wet, hidden),
                "pngs": sorted(set(pngs)),
                "js_errors": page.evaluate("() => window.__fvErrors || []")[:5],
            }
        )
        browser.close()
    print(json.dumps(out, ensure_ascii=False, indent=1))
    path = ROOT / "outputs" / "qa" / "file_protocol_frames.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
