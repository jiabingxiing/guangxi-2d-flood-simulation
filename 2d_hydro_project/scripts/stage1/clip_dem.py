"""阶段1：从整省 DEM 裁切案例 AOI 并重投影到案例 CRS。

用法：uv --directory . run python scripts/stage1/clip_dem.py <case>
输出：data/interim/<case>/dem_<res>m_<epsg>.tif + data/catalog/dem_clip_<case>.json（含哈希与 QA 统计）
规则（终稿 六章）：重投影显式、双线性重采样、QA 统计与哈希留痕；NoData 处理单独登记。
"""
from __future__ import annotations

import hashlib
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

PROJ_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ_ROOT))
from scripts.common.paths import dem_source  # noqa: E402  可移植：源 DEM 由 paths 解析


def main() -> None:
    case = sys.argv[1]
    cfg_path = PROJ_ROOT / "configs" / "cases" / f"{case}.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    w, s, e, n = cfg["aoi"]["provisional_bbox_wgs84"]
    target_epsg = cfg["crs"]["epsg"]
    res = float(cfg["terrain"]["primary"]["resolution_m"])

    tf = Transformer.from_crs(4326, target_epsg, always_xy=True)
    corners = [tf.transform(x, y) for x, y in [(w, s), (e, s), (e, n), (w, n)]]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    bounds = (min(xs), min(ys), max(xs), max(ys))

    dem_path = dem_source()
    with rasterio.open(dem_path) as src:
        with WarpedVRT(src, crs=f"EPSG:{target_epsg}", resampling=Resampling.bilinear) as vrt:
            # AOI 外扩 2 km 缓冲，避免边界重采样伪影
            pad = 2000.0
            wb = (bounds[0] - pad, bounds[1] - pad, bounds[2] + pad, bounds[3] + pad)
            window = from_bounds(*wb, transform=vrt.transform)
            data = vrt.read(1, window=window, out_shape=(
                max(1, int((wb[3] - wb[1]) / res)), max(1, int((wb[2] - wb[0]) / res))),
                resampling=Resampling.bilinear).astype("float32")
            out_transform = vrt.window_transform(window) * rasterio.Affine.scale(
                window.width / data.shape[1], window.height / data.shape[0])

    out_dir = PROJ_ROOT / "data" / "interim" / case
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"dem_{str(res).replace('.', 'p')}m_{target_epsg}.tif"
    profile = {
        "driver": "GTiff", "height": data.shape[0], "width": data.shape[1],
        "count": 1, "dtype": "float32", "crs": f"EPSG:{target_epsg}",
        "transform": out_transform, "compress": "lzw", "nodata": -9999.0,
    }
    # 源 NoData=0：0 值在海岸带可能是有效高程，先按原值带入并统计，QA 后再定掩膜策略
    data = np.where(np.isfinite(data), data, -9999.0)
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(data, 1)

    valid = data[data != -9999.0]
    rec = {
        "case": case,
        "source": str(dem_path),
        "target_crs": f"EPSG:{target_epsg}",
        "resolution_m": res,
        "aoi_buffer_m": pad,
        "grid": [int(data.shape[1]), int(data.shape[0])],
        "bounds_target_crs": [round(v, 1) for v in wb],
        "sha256": hashlib.sha256(out_path.read_bytes()).hexdigest(),
        "size_mb": round(out_path.stat().st_size / 1e6, 1),
        "qa": {
            "valid_px": int(valid.size),
            "elev_min": round(float(valid.min()), 2),
            "elev_p50": round(float(np.percentile(valid, 50)), 2),
            "elev_max": round(float(valid.max()), 2),
            "zero_px": int((data == 0).sum()),
            "neg_px": int(((data < 0) & (data != -9999.0)).sum()),
            "nodata_px": int((data == -9999.0).sum()),
            "note": "源 NoData=0 未做掩膜（海岸 0 m 冲突，QA 后再定）；负值多位于海面/河道水域",
        },
        "generated_utc": datetime.now(timezone.utc).isoformat(),
    }
    cat = PROJ_ROOT / "data" / "catalog" / f"dem_clip_{case}.json"
    cat.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{case}: {data.shape[1]}x{data.shape[0]} @{res}m EPSG:{target_epsg} -> {out_path}")
    print(f"  高程 min/p50/max = {rec['qa']['elev_min']}/{rec['qa']['elev_p50']}/{rec['qa']['elev_max']} m；"
          f"0值像元 {rec['qa']['zero_px']}，负值 {rec['qa']['neg_px']}，NoData {rec['qa']['nodata_px']}")
    print(f"  目录 -> {cat}")


if __name__ == "__main__":
    main()
