"""临时探针（qa）：用截图差分证明 file://+flag 下地图真的画出了水层 PNG。

MapLibre 用 WebGL 画布，`drawImage` 取不到像素（未开 preserveDrawingBuffer），
因此改用「水层开/关两次截图 → 像素差」判断绘制是否发生。仅读发布包。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = (ROOT / "outputs" / "web_bundle" / "v2-20261027").resolve()
FILE_URL = BUNDLE.as_uri() + "/index.html"
HTTP_URL = "http://127.0.0.1:8792/index.html"
SHOT_DIR = ROOT / "outputs" / "qa" / "evidence"


def shot(page, name: str) -> Path:
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SHOT_DIR / name
    page.locator("#map").screenshot(path=str(path))
    return path


def diff(a: Path, b: Path) -> dict:
    ia = np.asarray(Image.open(a).convert("RGB"), dtype=np.int16)
    ib = np.asarray(Image.open(b).convert("RGB"), dtype=np.int16)
    if ia.shape != ib.shape:
        return {"shape_mismatch": [ia.shape, ib.shape]}
    delta = np.abs(ia - ib).sum(axis=2)
    changed = int((delta > 12).sum())
    return {
        "pixels": int(delta.size),
        "changed_pixels": changed,
        "changed_ratio": round(changed / delta.size, 4),
        "max_delta": int(delta.max()),
    }


def run(page, label: str, url: str) -> dict:
    pngs: list[str] = []
    tiles: list[tuple[str, int]] = []
    page.on("request", lambda r: pngs.append(r.url.rsplit("/", 1)[-1]) if r.url.endswith(".png") else None)
    page.on(
        "response",
        lambda r: tiles.append((r.url.rsplit("/", 1)[-1][:22], r.status)) if "arcgisonline.com" in r.url else None,
    )
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_function(
        "() => window.__fvDebug && window.__fvDebug.state().frameId !== null", timeout=90000
    )
    page.eval_on_selector(
        "#frame-slider", "el => { el.value = '10'; el.dispatchEvent(new Event('input', {bubbles:true})); }"
    )
    page.wait_for_function("() => window.__fvDebug.state().index === 10", timeout=90000)
    page.wait_for_timeout(3500)

    on = shot(page, f"map-{label}-flood-on.png")
    page.click("#layer-cb-flood")
    page.wait_for_timeout(1800)
    off = shot(page, f"map-{label}-flood-off.png")
    page.click("#layer-cb-flood")
    page.wait_for_timeout(600)

    # 最大包络
    page.click("#btn-max")
    page.wait_for_function("() => window.__fvDebug.state().maxMode === true", timeout=60000)
    page.wait_for_timeout(2500)
    env = shot(page, f"map-{label}-max-envelope.png")
    page.click("#btn-max")
    page.wait_for_timeout(800)

    # 在线底图
    page.click("#basemap-toggle")
    page.wait_for_timeout(7000)
    warn = (page.text_content("#basemap-warn") or "").strip()
    with_tiles = shot(page, f"map-{label}-basemap.png")
    attribution = (page.text_content("#status-attribution") or "").strip()

    return {
        "label": label,
        "url": url,
        "flood_on_vs_off": diff(on, off),
        "flood_on_vs_envelope": diff(on, env),
        "flood_on_vs_basemap": diff(on, with_tiles),
        "frame_pngs": sorted({n for n in pngs if n.startswith("f0010")}),
        "png_total": len(pngs),
        "basemap_warn": warn[:160],
        "basemap_tile_responses": tiles[:6],
        "basemap_tile_count": len(tiles),
        "attribution": attribution[:120],
        "js_errors": page.evaluate("() => window.__fvErrors || []")[:3],
    }


def main() -> int:
    out: dict = {}
    with sync_playwright() as p:
        for label, channel, args, url in (
            ("file-flag", "msedge", ["--allow-file-access-from-files"], FILE_URL),
            ("http", None, None, HTTP_URL),
        ):
            if channel:
                browser = p.chromium.launch(headless=True, channel=channel, args=args)
            else:
                browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(viewport={"width": 1600, "height": 900}, locale="zh-CN")
            page = ctx.new_page()
            try:
                out[label] = run(page, label, url)
            except Exception as exc:  # noqa: BLE001
                out[label] = {"error": str(exc)[:300]}
            browser.close()
    print(json.dumps(out, ensure_ascii=False, indent=1)[:3500])
    path = ROOT / "outputs" / "qa" / "file_protocol_paint.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
