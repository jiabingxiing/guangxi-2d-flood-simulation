"""阶段1：宁明站水位与 DEM 垂直基准关系筛查（粗估级，不写入 datum_offset_m）。

用法：uv --directory . run python scripts/stage1/wse_screening.py
方法：每站取缓冲半径内 DEM 最小高程（近似河床/岸坡脚），
     计算站基面水位与 DEM 高程之差；跨站离散度用于判断系统性 vs 站点差异。
限制：DEM 河床高程受 12.5m 分辨率与产品精度限制，结果仅作筛查，不作为转换参数。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer

PROJ = Path(__file__).resolve().parents[2]
DEM_PATH = PROJ / "data" / "interim" / "ningming" / "dem_12p5m_32648.tif"

STATIONS = [
    # 站名, 河, 经度, 纬度, 2026年7月水位(m, 站基面), 2026年8月水位(m, 站基面)
    ("宁明县城", "明江", 107.0719, 22.1403, 109.24, 123.43),
    ("板祝", "派连河", 107.059332, 22.093138, 109.70, 123.66),
    ("宁明", "明江", 107.2, 22.116667, 110.69, 124.55),
    ("那堪", "明江", 107.6, 22.1, 126.30, 133.20),
]
BUFFER_M = 500.0


def main() -> None:
    tf = Transformer.from_crs(4326, 32648, always_xy=True)
    rows = []
    with rasterio.open(DEM_PATH) as ds:
        for name, river, lon, lat, jul, aug in STATIONS:
            x, y = tf.transform(lon, lat)
            row, col = ds.index(x, y)
            r = int(BUFFER_M / abs(ds.transform.e))
            r0, r1 = max(0, row - r), min(ds.height, row + r + 1)
            c0, c1 = max(0, col - r), min(ds.width, col + r + 1)
            if r1 <= r0 or c1 <= c0:
                rows.append({"站": name, "河": river, "error": f"站位于 DEM 裁切范围外/边缘 (row={row},col={col} vs {ds.width}x{ds.height})"})
                continue
            win = ds.read(1, window=rasterio.windows.Window(c0, r0, c1 - c0, r1 - r0))
            valid = win[(win != -9999.0) & (win != 0)]  # 0 为疑似 NoData，剔除
            if valid.size == 0:
                rows.append({"站": name, "河": river, "error": "缓冲区无有效 DEM 值"})
                continue
            dmin, dp50 = float(valid.min()), float(np.percentile(valid, 50))
            rows.append({
                "站": name, "河": river,
                "dem_min_500m": round(dmin, 1), "dem_p50_500m": round(dp50, 1),
                "level_jul": jul, "level_aug": aug,
                "rise_jul_aug_m": round(aug - jul, 2),
                "offset_aug_minus_demmin": round(aug - dmin, 1),
                "offset_jul_minus_demmin": round(jul - dmin, 1),
            })
    offs = [r["offset_aug_minus_demmin"] for r in rows if "offset_aug_minus_demmin" in r]
    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "method": "站基面水位 − 缓冲500m内DEM最小值；含河床深度与基准差的混合效应",
        "stations": rows,
        "offset_aug_stats": {
            "min": min(offs), "max": max(offs), "spread": round(max(offs) - min(offs), 1),
            "mean": round(float(np.mean(offs)), 1),
        },
        "conclusion": "离散度为站间河床深度差异与基准差之和的混合；跨站均值仅作筛查参考，禁止直接作 datum_offset_m",
    }
    out = PROJ / "data" / "catalog" / "ningming_wse_screening.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    print("offset_aug stats:", report["offset_aug_stats"])
    print(f"-> {out}")


if __name__ == "__main__":
    main()
