"""2D 模拟网格虚拟水尺水面高程时程提取模块。

在 LISFLOOD-FP 输出目录中读取 `run.par`、`dem.asc` 与全序列 `res-*.wd`，
按站点经纬度投影定位网格行列（同时支持原始坐标与河道中心线就近吸附），
提取连续水面高程序列 WSE_sim(t) 与水深序列 h_sim(t)。
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
from pyproj import Transformer

from hydro_refine.core.paths import resolve_legacy
from scripts.common.grid_io import (
    parse_run_par,
    read_ascii_grid,
    run_frames,
)

WET_THRESHOLD_M = 0.05


def _load_centerline_xy(geojson_path: Path, epsg: int) -> np.ndarray | None:
    """加载河道中心线并投影至指定 UTM 坐标系 (N, 2)。"""
    if not geojson_path.is_file():
        return None
    doc = json.loads(geojson_path.read_text(encoding="utf-8"))
    features = [doc] if doc.get("type") == "Feature" else doc.get("features", [])
    tf = Transformer.from_crs(4326, epsg, always_xy=True)
    pts: list[tuple[float, float]] = []
    for feat in features:
        geom = feat.get("geometry", feat)
        gtype = geom.get("type")
        coords = geom.get("coordinates", [])
        if gtype == "LineString":
            for lon, lat in coords:
                pts.append(tf.transform(float(lon), float(lat)))
        elif gtype == "MultiLineString":
            for line in coords:
                for lon, lat in line:
                    pts.append(tf.transform(float(lon), float(lat)))
    return np.asarray(pts, dtype=np.float64) if pts else None


def _snap_to_centerline(
    x: float, y: float, cl_xy: np.ndarray | None, max_snap_m: float
) -> tuple[float, float, float]:
    """将坐标吸附至最近河道节点（若距离 <= max_snap_m），返回 (nx, ny, dist_m)。"""
    if cl_xy is None or len(cl_xy) == 0:
        return x, y, 0.0
    d2 = (cl_xy[:, 0] - x) ** 2 + (cl_xy[:, 1] - y) ** 2
    idx = int(np.argmin(d2))
    dist = math.sqrt(float(d2[idx]))
    if dist <= max_snap_m:
        return float(cl_xy[idx, 0]), float(cl_xy[idx, 1]), dist
    return x, y, dist


def _sample_window_wse(
    dem_sub: np.ndarray,
    wd_sub: np.ndarray,
    center_rc_in_sub: tuple[int, int],
    *,
    mode: str = "wet_max",
    wet_thr: float = WET_THRESHOLD_M,
) -> tuple[float | None, float | None, int]:
    """在窗口内按指定口径采样水面高程与水深，返回 (wse_m, depth_m, wet_cells)。"""
    wet = np.isfinite(wd_sub) & np.isfinite(dem_sub) & (wd_sub > wet_thr)
    n_wet = int(wet.sum())
    if n_wet == 0:
        return None, None, 0

    if mode == "center":
        rr, cc = center_rc_in_sub
        if not bool(wet[rr, cc]):
            return None, None, n_wet
        d = float(wd_sub[rr, cc])
        return round(float(dem_sub[rr, cc]) + d, 3), round(d, 3), n_wet

    if mode == "thalweg_wet":
        # 取窗口内湿格中 DEM 最低的深泓像元，避免岸坡干扰
        dem_masked = np.where(wet, dem_sub, np.inf)
        flat_idx = int(np.argmin(dem_masked))
        rr, cc = divmod(flat_idx, dem_sub.shape[1])
        d = float(wd_sub[rr, cc])
        return round(float(dem_sub[rr, cc]) + d, 3), round(d, 3), n_wet

    # 默认 wet_max（与 guigang_scenario.py::verify() 与 validate_guigang_s1.py 逐行同口径）
    wse = float((dem_sub[wet] + wd_sub[wet]).max())
    d_max = float(wd_sub[wet].max())
    return round(wse, 3), round(d_max, 3), n_wet


def extract_run_virtual_gauges(
    run_dir: Path,
    stations: list[dict[str, Any]],
    *,
    epsg: int,
    half_window: int = 3,
    mode: str = "wet_max",
    wet_thr: float = WET_THRESHOLD_M,
    frame_step: int = 1,
) -> dict[str, Any]:
    """从单个 LISFLOOD-FP run 目录提取所有域内站点的连续模拟水位序列。

    自动从 `run.par` 读取 `saveint` 与从 `dem.asc` 头读取 `cellsize`，绝不写死。
    """
    dem_p = run_dir / "dem.asc"
    res_dir = run_dir / "results"
    if not dem_p.is_file() or not res_dir.is_dir():
        return {"run_dir": str(run_dir), "available": False, "reason": "missing_dem_or_results"}

    dem_grid = read_ascii_grid(dem_p, nodata_to_nan=True)
    hdr = dem_grid.header
    dem = dem_grid.data
    nrows, ncols = dem_grid.shape
    cell = dem_grid.cell_size_m
    xll = float(hdr["xllcorner"])
    yll = float(hdr["yllcorner"])

    par = parse_run_par(run_dir)
    saveint_s = float(par.get("saveint", 21600.0))
    frames = run_frames(res_dir)
    if frame_step > 1:
        frames = frames[::frame_step]
    if not frames:
        return {"run_dir": str(run_dir), "available": False, "reason": "no_wd_frames"}

    tf = Transformer.from_crs(4326, epsg, always_xy=True)

    # 预计算各站点位置索引（含原始坐标与河道中心线吸附坐标）
    probes: list[dict[str, Any]] = []
    for st in stations:
        if not st.get("in_model_domain", True) or st.get("exclude_from_validation", False):
            continue
        lon, lat = float(st["lon"]), float(st["lat"])
        x_raw, y_raw = tf.transform(lon, lat)
        targets = [("raw_anchor", x_raw, y_raw, 0.0)]

        cl_rel = st.get("snap_centerline")
        if cl_rel:
            try:
                cl_p = resolve_legacy(cl_rel)
                cl_xy = _load_centerline_xy(cl_p, epsg)
                xs, ys, snap_dist = _snap_to_centerline(
                    x_raw, y_raw, cl_xy, float(st.get("max_snap_m", 6000.0))
                )
                if snap_dist > 1.0 and (abs(xs - x_raw) > 1.0 or abs(ys - y_raw) > 1.0):
                    targets.append(("snapped_channel", xs, ys, round(snap_dist, 1)))
            except Exception:
                pass

        for p_kind, px, py, sdist in targets:
            col = int((px - xll) / cell)
            row = int((yll + nrows * cell - py) / cell)
            if not (0 <= row < nrows and 0 <= col < ncols):
                continue
            r0, r1 = max(0, row - half_window), min(nrows, row + half_window + 1)
            c0, c1 = max(0, col - half_window), min(ncols, col + half_window + 1)
            probes.append({
                "key": f"{st['station_id']}__{p_kind}",
                "station_id": st["station_id"],
                "station_name": st["name"],
                "probe_kind": p_kind,
                "snap_dist_m": sdist,
                "row": row,
                "col": col,
                "slice": (r0, r1, c0, c1),
                "center_in_sub": (row - r0, col - c0),
                "ground_dem_m": (
                    None if not np.isfinite(dem[row, col]) else round(float(dem[row, col]), 3)
                ),
                "t_h": [],
                "wse_m": [],
                "depth_m": [],
                "wet_cells": [],
            })

    if not probes:
        return {"run_dir": str(run_dir), "available": False, "reason": "no_stations_in_domain"}

    for seq, wd_p in frames:
        wd_grid = read_ascii_grid(wd_p, nodata_to_nan=True, expected_shape=(nrows, ncols))
        wd = wd_grid.data
        t_h = round(seq * saveint_s / 3600.0, 2)
        for pr in probes:
            r0, r1, c0, c1 = pr["slice"]
            wse, depth, n_wet = _sample_window_wse(
                dem[r0:r1, c0:c1],
                wd[r0:r1, c0:c1],
                pr["center_in_sub"],
                mode=mode,
                wet_thr=wet_thr,
            )
            pr["t_h"].append(t_h)
            pr["wse_m"].append(wse)
            pr["depth_m"].append(depth)
            pr["wet_cells"].append(n_wet)

    # 清理内部切片字段
    for pr in probes:
        pr.pop("slice", None)
        pr.pop("center_in_sub", None)

    return {
        "run_dir": str(run_dir),
        "available": True,
        "cellsize_m": cell,
        "saveint_s": saveint_s,
        "n_frames": len(frames),
        "sampling_mode": mode,
        "window_cells": 2 * half_window + 1,
        "probes": {pr["key"]: pr for pr in probes},
    }
