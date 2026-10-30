"""阶段0：盘点本地 DEM（五省区）与外部栅格的元数据。

用法：uv --directory . run python scripts/stage0/inspect_dem.py <栅格路径> [<栅格路径> ...]
输出：控制台 + data/catalog/dem_inventory.json
只读元数据与统计金字塔/抽样，不全量读像素。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import rasterio

PROJ_ROOT = Path(__file__).resolve().parents[2]
OUT_PATH = PROJ_ROOT / "data" / "catalog" / "dem_inventory.json"


def inspect(path: str) -> dict:
    p = Path(path)
    rec: dict = {
        "path": str(p),
        "exists": p.exists(),
        "size_mb": round(p.stat().st_size / 1e6, 1) if p.exists() else None,
    }
    if not p.exists():
        rec["error"] = "file not found"
        return rec
    with rasterio.open(p) as ds:
        rec.update(
            driver=ds.driver,
            width=ds.width,
            height=ds.height,
            count=ds.count,
            dtype=str(ds.dtypes[0]),
            crs=str(ds.crs),
            resolution=[abs(ds.transform.a), abs(ds.transform.e)],
            bounds=list(ds.bounds),
            nodata=ds.nodata,
            tags=ds.tags(),
        )
        if ds.crs is None or not ds.crs.is_projected:
            rec["unit_warning"] = "CRS 缺失或非投影（经纬度）——不得直接当米制网格使用"
        # 抽样统计（第一个波段，粗抽 512x512 网格）
        try:
            import numpy as np

            rows = np.linspace(0, ds.height - 1, min(512, ds.height)).astype(int)
            cols = np.linspace(0, ds.width - 1, min(512, ds.width)).astype(int)
            vals = []
            for r in rows[:: max(1, len(rows) // 64)]:
                window_vals = ds.read(1, window=rasterio.windows.Window(
                    int(cols[0]), int(r), max(1, int(cols[-1]) - int(cols[0])), 1))
                vals.append(window_vals.ravel())
            arr = np.concatenate(vals)
            valid = arr[np.isfinite(arr)]
            if ds.nodata is not None:
                valid = valid[valid != ds.nodata]
            if valid.size:
                rec["sample_stats"] = {
                    "n": int(valid.size),
                    "min": float(np.nanmin(valid)),
                    "p5": float(np.nanpercentile(valid, 5)),
                    "median": float(np.nanmedian(valid)),
                    "p95": float(np.nanpercentile(valid, 95)),
                    "max": float(np.nanmax(valid)),
                }
        except Exception as exc:  # 统计失败不影响元数据盘点
            rec["sample_stats_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    return rec


def main() -> None:
    paths = sys.argv[1:]
    if not paths:
        print("用法: inspect_dem.py <栅格路径> [...]")
        raise SystemExit(2)
    results = [inspect(p) for p in paths]
    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "script": "scripts/stage0/inspect_dem.py",
        "rasters": results,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    for rec in results:
        name = Path(rec["path"]).name
        print(
            f"{name}: {rec.get('width')}x{rec.get('height')} crs={rec.get('crs')} "
            f"res={rec.get('resolution')} nodata={rec.get('nodata')} "
            f"bounds={[round(b, 4) for b in rec.get('bounds', [])] if rec.get('bounds') else None} "
            f"sizeMB={rec.get('size_mb')}"
        )
        if rec.get("sample_stats"):
            s = rec["sample_stats"]
            print(f"    elev min/med/max = {s['min']:.1f} / {s['median']:.1f} / {s['max']:.1f}")
        for key in ("unit_warning", "error", "sample_stats_error"):
            if key in rec:
                print(f"    !! {key}: {rec[key]}")
    print(f"-> {OUT_PATH}")


if __name__ == "__main__":
    main()
