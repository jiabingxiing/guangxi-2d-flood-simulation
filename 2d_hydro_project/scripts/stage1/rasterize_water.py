"""阶段1：宁明 GF-3 水体矢量 -> 模型网格掩膜（8/6 与 8/24 两期）。

用法：uv --directory . run python scripts/stage1/rasterize_water.py
输出：data/interim/ningming/water_0806.tif、water_0824.tif、water_new_0824.tif + catalog json
口径说明：观测为「总水体」；新增淹没 = 8/24 有水 且 8/6 无水（终稿 8.1 的"新增漫滩"口径）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
import shapefile
from pyproj import Transformer
from rasterio.features import rasterize

PROJ = Path(__file__).resolve().parents[2]
SHP_DIR = PROJ / "data" / "raw" / "ningming_validation" / "淹没深度验证数据-崇左宁明县"
DEM_PATH = PROJ / "data" / "interim" / "ningming" / "dem_12p5m_32648.tif"
OUT_DIR = PROJ / "data" / "interim" / "ningming"

LAYERS = {
    "water_0806": "GF3_8月6日水体_宁明县",
    "water_0824": "GF3_8月24日水体_宁明县",
}

TF = Transformer.from_crs(4326, 32648, always_xy=True)


def _tx_pts(pts):
    xs, ys = TF.transform([p[0] for p in pts], [p[1] for p in pts])
    return [(x, y) for x, y in zip(xs, ys)]


def geom_iter(shp_path: str):
    """读取 shp 并把 WGS84 坐标重投影到 EPSG:32648（栅格网格 CRS）。"""
    r = shapefile.Reader(shp_path)
    n = 0
    for sr in r.iterShapeRecords():
        g = sr.shape.__geo_interface__
        if g["type"] == "Polygon":
            g = {**g, "coordinates": [_tx_pts(ring) for ring in g["coordinates"]]}
        elif g["type"] == "MultiPolygon":
            g = {**g, "coordinates": [[_tx_pts(ring) for ring in poly] for poly in g["coordinates"]]}
        n += 1
        yield g
    print(f"  {Path(shp_path).name}: {n} 个要素已重投影")


def main() -> None:
    with rasterio.open(DEM_PATH) as ds:
        transform = ds.transform
        crs = ds.crs
        h, w = ds.height, ds.width
        cell_area = abs(transform.a * transform.e)

    masks = {}
    for key, layer in LAYERS.items():
        geoms = list(geom_iter(str(SHP_DIR / layer)))
        arr = rasterize(((g, 1) for g in geoms), out_shape=(h, w), transform=transform,
                        fill=0, dtype="uint8", all_touched=False)
        masks[key] = arr
        out = OUT_DIR / f"{key}.tif"
        with rasterio.open(out, "w", driver="GTiff", height=h, width=w, count=1,
                           dtype="uint8", crs=crs, transform=transform, compress="lzw") as dst:
            dst.write(arr, 1)
        print(f"{key}: 湿像元 {int(arr.sum())} ({arr.sum()*cell_area/1e6:.2f} km2) -> {out}")

    new = ((masks["water_0824"] == 1) & (masks["water_0806"] == 0)).astype("uint8")
    persist = ((masks["water_0824"] == 1) & (masks["water_0806"] == 1)).astype("uint8")
    receded = ((masks["water_0824"] == 0) & (masks["water_0806"] == 1)).astype("uint8")
    with rasterio.open(OUT_DIR / "water_new_0824.tif", "w", driver="GTiff", height=h, width=w,
                       count=1, dtype="uint8", crs=crs, transform=transform, compress="lzw") as dst:
        dst.write(new, 1)

    # DEM 空洞（0 值像元）与水体的重叠：判别空洞性质
    with rasterio.open(DEM_PATH) as ds:
        dem = ds.read(1)
    zeros = dem == 0
    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "grid": [w, h], "cell_m": 12.5,
        "water_0806_km2": round(float(masks["water_0806"].sum()) * cell_area / 1e6, 2),
        "water_0824_km2": round(float(masks["water_0824"].sum()) * cell_area / 1e6, 2),
        "new_inundation_km2": round(float(new.sum()) * cell_area / 1e6, 2),
        "persistent_water_km2": round(float(persist.sum()) * cell_area / 1e6, 2),
        "receded_km2": round(float(receded.sum()) * cell_area / 1e6, 2),
        "dem_zero_px_total": int(zeros.sum()),
        "dem_zero_in_0824_water": int((zeros & (masks["water_0824"] == 1)).sum()),
        "dem_zero_in_never_water": int((zeros & (masks["water_0824"] == 0) & (masks["water_0806"] == 0)).sum()),
        "note": "矢量属性面积合计 130.14 km2 为图斑面积；栅格化后按 12.5m 网格统计",
    }
    out_json = PROJ / "data" / "catalog" / "ningming_water_masks.json"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"-> {out_json}")


if __name__ == "__main__":
    main()
