"""临时探针（qa）：file://+flag 下地图是否真的画出水深层（PNG image source 是否加载成功），
以及在线 Esri 底图在 file:// 源下能否使用。仅读发布包，不改代码。
"""

from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = (ROOT / "outputs" / "web_bundle" / "v2-20261027").resolve()
FILE_URL = BUNDLE.as_uri() + "/index.html"

CANVAS_STATS = """() => {
  const c = document.querySelector('#map canvas');
  if (!c) return null;
  const tmp = document.createElement('canvas');
  tmp.width = c.width; tmp.height = c.height;
  const ctx = tmp.getContext('2d');
  ctx.drawImage(c, 0, 0);
  const d = ctx.getImageData(0, 0, tmp.width, tmp.height).data;
  let painted = 0, colors = new Set();
  for (let i = 0; i < d.length; i += 4) {
    if (d[i + 3] > 0) { painted += 1; if (colors.size < 40) colors.add(d[i]+','+d[i+1]+','+d[i+2]); }
  }
  return {w: tmp.width, h: tmp.height, painted, distinct: colors.size};
}"""


def main() -> int:
    out: dict = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="msedge", args=["--allow-file-access-from-files"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 900}, locale="zh-CN")
        page = ctx.new_page()
        reqs: list[str] = []
        page.on("request", lambda r: reqs.append(r.url))
        page.goto(FILE_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_function("() => window.__fvDebug && window.__fvDebug.state().frameId !== null", timeout=60000)
        # 跳到湿帧
        page.eval_on_selector(
            "#frame-slider", "el => { el.value = '10'; el.dispatchEvent(new Event('input', {bubbles:true})); }"
        )
        page.wait_for_function("() => window.__fvDebug.state().index === 10", timeout=60000)
        page.wait_for_timeout(3000)
        flood_on = page.evaluate(CANVAS_STATS)
        # 关掉水层看差异
        page.click("#layer-cb-flood")
        page.wait_for_timeout(1500)
        flood_off = page.evaluate(CANVAS_STATS)
        page.click("#layer-cb-flood")
        page.wait_for_timeout(500)

        png_requests = [u for u in reqs if u.endswith(".png")]
        bin_requests = [u for u in reqs if u.endswith(".bin")]
        out["file_with_flag"] = {
            "flood_on": flood_on,
            "flood_off": flood_off,
            "painted_delta": (flood_on or {}).get("painted", 0) - (flood_off or {}).get("painted", 0),
            "png_requests": len(png_requests),
            "png_sample": [u.rsplit("/", 1)[-1] for u in png_requests[:6]],
            "bin_requests": len(bin_requests),
        }
        page.screenshot(path=str(ROOT / "outputs" / "qa" / "evidence" / "11-file-protocol-map.png"))

        # 在线底图（file:// 源 + https tiles）
        page.click("#basemap-toggle")
        page.wait_for_timeout(6000)
        warn = (page.text_content("#basemap-warn") or "").strip()
        tile_reqs = [u for u in reqs if "arcgisonline.com" in u]
        tile_status: dict[str, int] = {}

        def on_resp(resp):
            if "arcgisonline.com" in resp.url:
                tile_status[resp.url.rsplit("/", 1)[-1][:24]] = resp.status

        page.on("response", on_resp)
        page.wait_for_timeout(4000)
        out["esri_basemap_under_file"] = {
            "warn": warn[:200],
            "tile_requests": len(tile_reqs),
            "tile_status_sample": dict(list(tile_status.items())[:5]),
            "attribution": (page.text_content("#status-attribution") or "").strip()[:160],
        }
        out["js_errors"] = page.evaluate("() => window.__fvErrors || []")[:5]
        browser.close()
    print(json.dumps(out, ensure_ascii=False, indent=1)[:2600])
    path = ROOT / "outputs" / "qa" / "file_protocol_map.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
