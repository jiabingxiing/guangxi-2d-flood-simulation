"""阶段3：WorldCover -> Manning n 糙率网格（终稿 九章：先验区间中值，非率定值）。

用法：uv --directory . run python scripts/stage3/make_roughness.py <case> [<case> ...]
输出：data/interim/<case>/mannings_<res>m_<epsg>.tif + data/catalog/roughness_<case>.json
映射（先验中值，m-s 制）：树地 0.10 | 灌木 0.08 | 草地 0.035 | 耕地 0.05 | 建筑 0.15（ADR-0005
高糙率/障碍近似）| 裸地 0.025 | 水体 0.03 | 湿地 0.07 | 红树林 0.13 | 未知/NoData 0.05（登记，不静默）。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
import yaml
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.windows import from_bounds

PROJ = Path(__file__).resolve().parents[2]
WC = {"N21E105": PROJ / "data" / "raw" / "worldcover" / "N21E105.tif",
      "N21E108": PROJ / "data" / "raw" / "worldcover" / "N21E108.tif"}

N_MAP = {10: 0.10, 20: 0.08, 30: 0.035, 40: 0.05, 50: 0.15,
         60: 0.025, 70: 0.03, 80: 0.03, 90: 0.07, 95: 0.13, 100: 0.07}
N_DEFAULT = 0.05  # 未知类/NoData —— 记录比例，不静默套用


def main() -> None:
    for case in sys.argv[1:]:
        cfg = yaml.safe_load((PROJ / "configs" / "cases" / f"{case}.yaml").read_text(encoding="utf-8"))
        w, s, e, n = cfg["aoi"]["provisional_bbox_wgs84"]
        epsg = cfg["crs"]["epsg"]
        res = float(cfg["terrain"]["primary"]["resolution_m"])
        tile = WC["N21E105" if (w + e) / 2 < 108 else "N21E108"]
        tf = Transformer.from_crs(4326, epsg, always_xy=True)
        corners = [tf.transform(x, y) for x, y in [(w, s), (e, s), (e, n), (w, n)]]
        bounds = (min(c[0] for c in corners), min(c[1] for c in corners),
                  max(c[0] for c in corners), max(c[1] for c in corners))
        with rasterio.open(tile) as src:
            with WarpedVRT(src, crs=f"EPSG:{epsg}", resampling=Resampling.nearest) as vrt:
                win = from_bounds(*bounds, transform=vrt.transform)
                win = win.round_offsets().round_lengths()
                lc = vrt.read(1, window=win).astype("int16")
                out_transform = vrt.window_transform(win)
        n_grid = np.vectorize(lambda c: N_MAP.get(int(c), N_DEFAULT))(lc).astype("float32")
        out_dir = PROJ / "data" / "interim" / case
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"mannings_{str(res).replace('.', 'p')}m_{epsg}.tif"
        with rasterio.open(out, "w", driver="GTiff", height=n_grid.shape[0], width=n_grid.shape[1],
                           count=1, dtype="float32", crs=f"EPSG:{epsg}", transform=out_transform,
                           compress="lzw", nodata=0.0) as dst:
            dst.write(n_grid, 1)
        unknown = ~np.isin(lc, list(N_MAP.keys()))
        rec = {
            "case": case, "tile": tile.name, "res_m": res, "epsg": epsg,
            "grid": [int(n_grid.shape[1]), int(n_grid.shape[0])],
            "n_area_weighted_mean": round(float(n_grid.mean()), 4),
            "unknown_or_nodata_frac": round(float(unknown.mean()), 6),
            "mapping": {str(k): v for k, v in N_MAP.items()},
            "default_for_unknown": N_DEFAULT,
            "note": "先验中值（终稿 九章）；率定在阶段3/4 分阶段进行（先边界/河槽，后糙率）",
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "output": str(out),
        }
        (PROJ / "data" / "catalog" / f"roughness_{case}.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{case}: {rec['grid']} 面积加权 n≈{rec['n_area_weighted_mean']} "
              f"未知类占比 {rec['unknown_or_nodata_frac']*100:.3f}% -> {out}")


if __name__ == "__main__":
    main()
