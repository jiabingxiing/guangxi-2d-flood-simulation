"""阶段 3/6 交付产品包：最大水深 / 到达时间 / 历时 / 峰值时刻 GeoTIFF + 站址摘要。

口径（降级模式，随产品标注）：
- 水深 = res.max（模拟全程最大）；
- 到达 = 第一个 wd>0.05m 的 6h 快照时刻（不是 res.maxtm）；单快照算例记 not_computable；
- 峰值时刻 = res.maxtm（最大水深发生时刻）；
- 历时 = res.totaltm（LISFLOOD 累计湿时，小时）；
- 面积 = area_gt05_km2 为全历时最大淹没范围（包络）；peak_area_km2 为峰时单帧面积；
- 站址值分列：cell（站址像元）、r2（±2 格面积级，主口径）；summary 的 depth_max_site_m 保留为 r2 别名。
产出：outputs/products/<site>/*.tif + products_summary.json
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer

PROJ = Path(__file__).resolve().parents[2]
OUT = PROJ / "outputs" / "products"
WET_THR = 0.05
SITE_RADIUS_CELLS = 2

RUNS = {
    "ningming": {"dir": PROJ / "outputs/ningming/2026-08-zitan/scen_q6000_n100_100m",
                 "epsg": 32648, "site": ("宁明县城站", 107.0719, 22.1403), "saveint_h": 6.0},
    "jiulong": {"dir": PROJ / "outputs/jiulong/2024-09-yagi/scen_free_200m_sv6h",
                "epsg": 32649, "site": ("久隆500kV站", 108.7161, 22.0638), "saveint_h": 6.0,
                "sea_mask": PROJ / "outputs/jiulong/2024-09-yagi/scen_free_200m_sv6h/sea_mask.npy"},
    "guigang": {"dir": PROJ / "outputs/guigang/2024-09-yagi/scen_yagi_q11500_200m",
                "epsg": 32649, "site": ("贵港市区锚点", 109.60, 23.11), "saveint_h": 6.0},
}


def read_asc(p: Path):
    with open(p) as f:
        hdr = [next(f) for _ in range(6)]
    h = {l.split()[0].lower(): float(l.split()[1]) for l in hdr}
    return np.loadtxt(p, skiprows=6), h


def site_rowcol(hdr: dict, lon: float, lat: float, epsg: int) -> tuple[int, int]:
    tf = Transformer.from_crs(4326, epsg, always_xy=True)
    x, y = tf.transform(lon, lat)
    col = int((x - hdr["xllcorner"]) / hdr["cellsize"])
    row = int((hdr["yllcorner"] + hdr.get("nrows", 0) * hdr["cellsize"] - y) / hdr["cellsize"])
    return row, col


def window(arr: np.ndarray, row: int, col: int, rad: int = SITE_RADIUS_CELLS):
    r0, r1 = max(0, row - rad), min(arr.shape[0], row + rad + 1)
    c0, c1 = max(0, col - rad), min(arr.shape[1], col + rad + 1)
    return arr[r0:r1, c0:c1], (r0, c0)


def site_stats(arr: np.ndarray, hdr: dict, lon: float, lat: float, epsg: int,
               rad: int = SITE_RADIUS_CELLS) -> dict:
    row, col = site_rowcol(hdr, lon, lat, epsg)
    sub, (r0, c0) = window(arr, row, col, rad)
    ri, ci = np.unravel_index(np.nanargmax(sub), sub.shape)
    return {
        "center": float(arr[row, col]),
        "r2_max": float(sub[ri, ci]),
        "argmax_row": int(r0 + ri), "argmax_col": int(c0 + ci),
        "offset_cells": [int(r0 + ri - row), int(c0 + ci - col)],
        "center_row": int(row), "center_col": int(col),
    }


def write_tif(p: Path, arr: np.ndarray, hdr: dict, epsg: int):
    cell = hdr["cellsize"]
    prof = {"driver": "GTiff", "height": arr.shape[0], "width": arr.shape[1], "count": 1,
            "dtype": "float32", "crs": f"EPSG:{epsg}",
            "transform": rasterio.Affine(cell, 0, hdr["xllcorner"], 0, -cell,
                                         hdr["yllcorner"] + arr.shape[0] * cell),
            "compress": "deflate", "nodata": -9999.0}
    with rasterio.open(p, "w", **prof) as dst:
        dst.write(np.where(np.isfinite(arr), arr, -9999).astype("float32"), 1)


def snapshot_paths(res: Path) -> list[tuple[int, Path]]:
    out = []
    for p in sorted(res.glob("res-*.wd")):
        try:
            out.append((int(p.stem.split("-")[1]), p))
        except ValueError:
            continue
    return out


def first_wet_array(frames: list[tuple[int, Path]], shape: tuple[int, int],
                    saveint_h: float | None, thr: float = WET_THR):
    if saveint_h is None or len(frames) < 2:
        return None
    arrival = np.full(shape, -1.0, dtype="float64")
    for idx, p in frames:
        wd, _ = read_asc(p)
        newly = (wd > thr) & (arrival < 0)
        arrival[newly] = idx * saveint_h
    return arrival


def max_snapshot_area(frames: list[tuple[int, Path]], cell: float, thr: float = WET_THR) -> float:
    best = 0.0
    for _, p in frames:
        wd, _ = read_asc(p)
        best = max(best, float((wd > thr).sum()) * cell * cell / 1e6)
    return best


def main() -> None:
    summary = {"generated_utc": datetime.now(timezone.utc).isoformat(),
               "note": "情景级产品；到达=首次 wd>0.05 的 6h 快照；峰值时刻=res.maxtm；"
                       "历时=res.totaltm；站址值分 cell/r2（r2 为主口径，面积级）；"
                       "area_gt05_km2=全历时最大范围包络，peak_area_km2=峰时单帧",
               "wet_threshold_m": WET_THR, "site_radius_cells": SITE_RADIUS_CELLS,
               "sites": {}}
    for site, cfg in RUNS.items():
        res = cfg["dir"] / "results"
        d = OUT / site
        d.mkdir(parents=True, exist_ok=True)
        rmax, hdr = read_asc(res / "res.max")
        cell = hdr["cellsize"]
        sea = None
        if cfg.get("sea_mask") and cfg["sea_mask"].exists():
            sea = np.load(cfg["sea_mask"])
            if sea.shape != rmax.shape:
                sea = None
        rmax_use = np.where(sea, np.nan, rmax) if sea is not None else rmax
        stats = site_stats(rmax_use, hdr, *cfg["site"][1:], cfg["epsg"])
        rec = {
            "site_name": cfg["site"][0], "run": cfg["dir"].name,
            "depth_max_site_cell_m": round(stats["center"], 3),
            "depth_max_site_r2_m": round(stats["r2_max"], 3),
            "depth_max_site_m": round(stats["r2_max"], 3),  # 兼容旧字段；主口径 r2
            "site_r2_offset_cells": stats["offset_cells"],
            "area_gt05_km2": round(float((rmax_use > WET_THR).sum()) * cell * cell / 1e6, 1),
            "sea_masked": sea is not None,
        }
        write_tif(d / "depth_max_m.tif", rmax_use, hdr, cfg["epsg"])

        frames = snapshot_paths(res)
        rec["peak_area_km2"] = round(max_snapshot_area(frames, cell), 1)
        rec["n_snapshots"] = len(frames)

        mt = res / "res.maxtm"
        if mt.exists():
            arr, _ = read_asc(mt)  # LISFLOOD maxtm 单位=小时
            arr = np.where(rmax_use > WET_THR, arr, np.nan)
            write_tif(d / "peak_time_h.tif", arr, hdr, cfg["epsg"])
            rec["peak_time_site_h"] = round(float(arr[stats["argmax_row"], stats["argmax_col"]]), 1)
            rec["peak_time_method"] = "res.maxtm at site r2 argmax cell"

        tt = res / "res.totaltm"
        if tt.exists():
            arr, _ = read_asc(tt)  # 累计湿时，小时
            dur = np.where(rmax_use > WET_THR, arr, np.nan)
            write_tif(d / "duration_h.tif", dur, hdr, cfg["epsg"])
            rec["duration_site_h"] = round(float(dur[stats["argmax_row"], stats["argmax_col"]]), 1)
            rec["duration_method"] = "res.totaltm (hours)"

        arrival = first_wet_array(frames, rmax.shape, cfg["saveint_h"])
        if arrival is not None:
            write_tif(d / "arrival_h.tif", np.where(arrival >= 0, arrival, np.nan), hdr, cfg["epsg"])
            r0 = stats["argmax_row"]
            c0 = stats["argmax_col"]
            # 站址 ±2 格内首次淹没时刻
            sub = arrival[max(0, r0 - SITE_RADIUS_CELLS):r0 + SITE_RADIUS_CELLS + 1,
                          max(0, c0 - SITE_RADIUS_CELLS):c0 + SITE_RADIUS_CELLS + 1]
            pos = sub[sub >= 0]
            rec["arrival_site_h"] = round(float(pos.min()), 1) if pos.size else None
            rec["arrival_method"] = "first snapshot wd>0.05"
            rec["arrival_resolution_h"] = cfg["saveint_h"]
        else:
            rec["arrival_site_h"] = None
            rec["arrival_method"] = "not_computable_single_snapshot"
            rec["arrival_resolution_h"] = None
            stale = d / "arrival_h.tif"
            if stale.exists():
                stale.unlink()  # 避免旧 maxtm 伪到达文件残留

        summary["sites"][site] = rec
        print(site, json.dumps(rec, ensure_ascii=False))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "products_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("->", OUT)


if __name__ == "__main__":
    main()
