"""阶段1：获取 ESA WorldCover 2021 v200 瓦片并统计三案例土地覆盖。

用法：uv --directory . run python scripts/stage1/fetch_worldcover.py
瓦片（3°x3°，10m）：N21E105 覆盖宁明，N21E108 覆盖久隆+贵港。
输出：data/raw/worldcover/*.tif + data/catalog/landcover_stats.json
许可：CC BY 4.0（以官网当前条款为准）——保留署名。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
import requests
import yaml
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.windows import from_bounds
from pyproj import Transformer

PROJ = Path(__file__).resolve().parents[2]
RAW = PROJ / "data" / "raw" / "worldcover"
TILES = {
    "N21E105": "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/ESA_WorldCover_10m_2021_v200_N21E105_Map.tif",
    "N21E108": "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/ESA_WorldCover_10m_2021_v200_N21E108_Map.tif",
}
CLASS_NAMES = {
    10: "树地 Tree cover", 20: "灌木 Shrubland", 30: "草地 Grassland",
    40: "耕地 Cropland", 50: "建筑 Built-up", 60: "裸地/稀疏植被",
    70: "雪冰", 80: "水体", 90: "草本湿地", 95: "红树林", 100: "苔藓地衣",
}


def download() -> dict[str, Path]:
    RAW.mkdir(parents=True, exist_ok=True)
    paths = {}
    for tile, url in TILES.items():
        dest = RAW / f"{tile}.tif"
        if dest.exists() and dest.stat().st_size > 10_000_000:
            paths[tile] = dest
            continue
        session = requests.Session()
        session.trust_env = True  # S3 eu-central-1 走代理
        for attempt in range(1, 4):
            try:
                print(f"下载 {url} 第{attempt}次", flush=True)
                r = session.get(url, stream=True, timeout=(15, 120))
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0))
                done = 0
                with open(dest, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        fh.write(chunk)
                        done += len(chunk)
                        if done % (64 << 20) < (1 << 20):
                            print(f"  {done/1e6:6.1f} MB", flush=True)
                if total and done != total:
                    raise IOError(f"{done}/{total}")
                paths[tile] = dest
                break
            except Exception as exc:  # noqa: BLE001
                print("  失败:", type(exc).__name__, str(exc)[:100], flush=True)
                dest.unlink(missing_ok=True)
        else:
            raise SystemExit(f"瓦片 {tile} 下载失败")
    return paths


def case_stats(paths: dict[str, Path]) -> dict:
    cases = {}
    for case in ("jiulong", "ningming", "guigang"):
        cfg = yaml.safe_load((PROJ / "configs" / "cases" / f"{case}.yaml").read_text(encoding="utf-8"))
        w, s, e, n = cfg["aoi"]["provisional_bbox_wgs84"]
        epsg = cfg["crs"]["epsg"]
        tf = Transformer.from_crs(4326, epsg, always_xy=True)
        corners = [tf.transform(x, y) for x, y in [(w, s), (e, s), (e, n), (w, n)]]
        bounds = (min(c[0] for c in corners), min(c[1] for c in corners),
                  max(c[0] for c in corners), max(c[1] for c in corners))
        # 选覆盖瓦片：按 AOI 中心经度
        clon = (w + e) / 2
        tile = "N21E105" if clon < 108 else "N21E108"
        with rasterio.open(paths[tile]) as src:
            with WarpedVRT(src, crs=f"EPSG:{epsg}", resampling=Resampling.nearest) as vrt:
                win = from_bounds(*bounds, transform=vrt.transform)
                arr = vrt.read(1, window=win).astype("int16")
        vals, counts = np.unique(arr, return_counts=True)
        total = int(counts.sum())
        dist = {CLASS_NAMES.get(int(v), f"未知类{int(v)}"): {
            "code": int(v), "px": int(c),
            "pct": round(float(c) / total * 100, 2)} for v, c in zip(vals, counts)}
        cases[case] = {"tile": tile, "epsg": epsg, "distribution": dist,
                       "unknown_codes": [int(v) for v in vals if int(v) not in CLASS_NAMES]}
    return cases


def main() -> None:
    paths = download()
    stats = case_stats(paths)
    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "product": "ESA WorldCover 2021 v200 (10m), CC BY 4.0",
        "tiles": {k: str(v) for k, v in paths.items()},
        "cases": stats,
    }
    out = PROJ / "data" / "catalog" / "landcover_stats.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for case, s in stats.items():
        top = sorted(s["distribution"].items(), key=lambda kv: -kv[1]["pct"])[:5]
        print(f"{case} ({s['tile']}): " + "; ".join(f"{k} {v['pct']}%" for k, v in top))
        if s["unknown_codes"]:
            print(f"  !! 未知类别码: {s['unknown_codes']}")
    print(f"-> {out}")


if __name__ == "__main__":
    main()
