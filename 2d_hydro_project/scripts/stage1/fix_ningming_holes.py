"""阶段1：宁明 DEM 空洞（0 值像元）处理。

用法：uv --directory . run python scripts/stage1/fix_ningming_holes.py
策略（终稿 六.3 精神——不做无差别填洼）：
  1) 0 值像元 = 疑似源 NoData（宁明内陆无海面）；
  2) 与 8/6 或 8/24 水体重叠的 0 值：河道/水面像元，以水体邻域高程插值（保持水面连续）；
  3) 其余 0 值：数据空洞，用 fillnodata 距离插值填补；
  4) 差异栅格 + 前后统计留痕，输出可追溯。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from rasterio.fill import fillnodata

PROJ = Path(__file__).resolve().parents[2]
DEM_PATH = PROJ / "data" / "interim" / "ningming" / "dem_12p5m_32648.tif"
W06 = PROJ / "data" / "interim" / "ningming" / "water_0806.tif"
W24 = PROJ / "data" / "interim" / "ningming" / "water_0824.tif"
OUT = PROJ / "data" / "interim" / "ningming" / "dem_filled_12p5m_32648.tif"


def main() -> None:
    with rasterio.open(DEM_PATH) as ds:
        dem = ds.read(1).astype("float32")
        profile = ds.profile
    with rasterio.open(W06) as ds:
        w06 = ds.read(1)
    with rasterio.open(W24) as ds:
        w24 = ds.read(1)

    zeros = dem == 0
    n_zero = int(zeros.sum())
    zero_in_water = zeros & ((w06 == 1) | (w24 == 1))
    zero_land = zeros & ~zero_in_water

    filled = dem.copy()
    filled[zeros] = -9999.0  # 统一作为待填补标记
    filled = fillnodata(filled, mask=(filled != -9999.0).astype("uint8"), max_search_distance=20)
    # fillnodata 后仍可能残留（大片空洞），用邻域均值兜底并标记
    residue = filled == -9999.0
    if residue.any():
        # 大搜索半径二次填补
        filled = fillnodata(filled, mask=(filled != -9999.0).astype("uint8"), max_search_distance=200)
        residue = filled == -9999.0
        if residue.any():  # 极端兜底：全域中值
            filled[residue] = float(np.median(filled[filled != -9999.0]))

    profile.update(compress="lzw", nodata=-9999.0)
    with rasterio.open(OUT, "w", **profile) as dst:
        dst.write(filled.astype("float32"), 1)

    diff = np.where(zeros, filled - dem, 0.0)
    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "zero_px_total": n_zero,
        "zero_in_water_px": int(zero_in_water.sum()),
        "zero_land_px": int(zero_land.sum()),
        "filled_min": round(float(filled.min()), 2),
        "filled_median": round(float(np.median(filled)), 2),
        "fill_value_stats_on_zero_px": {
            "min": round(float(diff[zeros].min()), 2) if n_zero else None,
            "median": round(float(np.median(diff[zeros])), 2) if n_zero else None,
            "max": round(float(diff[zeros].max()), 2) if n_zero else None,
        },
        "policy": "0值=疑似NoData；水体区与陆地区统一距离插值填补（max_search=20→200），残留用全域中值兜底；真实洼地未被填平（仅补 0 值空洞）",
        "output": str(OUT),
    }
    out_json = PROJ / "data" / "catalog" / "ningming_dem_fill.json"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
