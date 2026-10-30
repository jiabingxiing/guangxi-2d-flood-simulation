"""阶段3：宁明 2026-08 紫檀事件 情景回算（降级模式，ADR-0004）。

管线：裁切走廊 DEM -> 生成 .rain（ERA5 逐时，均匀）与上游 Q 情景 .bci/.bdy -> par -> 运行
     -> 验证（与 8/24 新增淹没掩膜的 CSI/POD/FAR/F1 + 县城站水位抬升对照）。
用法：uv --directory . run python scripts/stage3/ningming_scenario.py prep|run|verify|all
情景假设（必须随成果标注）：
  - 上游明江入流为三角情景线（峰值 Q_PEAK 可配），无实测 Q(t)；
  - 下游与南北侧 FREE 出流（走廊域，山体边缘无流动到达）；
  - 降雨取 ERA5 县城锚点逐时（空间均匀）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
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
sys.path.insert(0, str(PROJ))
from scripts.common.run_manifest import write_run_manifest_best_effort  # noqa: E402

LISFLOOD = PROJ / "models" / "lisflood-fp-8.1" / "LISFLOOD-FP-trunk" / "lisflood.exe"
DEM_FILLED = PROJ / "data" / "interim" / "ningming" / "dem_filled_12p5m_32648.tif"
W24 = PROJ / "data" / "interim" / "ningming" / "water_new_0824.tif"  # 实为 8/24 新增淹没层
ERA5 = PROJ / "data" / "catalog" / "probes" / "ningming_aug2026_era5.json"

# 走廊域（WGS84 [w,s,e,n]）与网格
AOI = (106.96, 22.06, 107.20, 22.26)
RES = float(sys.argv[sys.argv.index("--res") + 1]) if "--res" in sys.argv else 100.0
Q_PEAK = float(sys.argv[sys.argv.index("--qpeak") + 1]) if "--qpeak" in sys.argv else 4000.0
FRIC = float(sys.argv[sys.argv.index("--fric") + 1]) if "--fric" in sys.argv else 0.035
RAIN_SCALE = float(sys.argv[sys.argv.index("--rain-scale") + 1]) if "--rain-scale" in sys.argv else 1.0
# 空间 Manning 网格（可选）。给定后写 manning.asc 并在 run.par 增加 manningfile；
# 本构建 pars.cpp:234 / input.cpp:914 LoadManningsn / fp_acc.cpp:58 实测支持（G18 修正）。
MANNING_TIF = Path(sys.argv[sys.argv.index("--manningfile") + 1]) if "--manningfile" in sys.argv else None
# 标量入渗（m/s，可选）。源码核验：infilfile 的分布式网格虽被 LoadDistInfil 读取，
# 但 FPInfiltration 只用 Parptr->InfilRate（infevap.cpp:28），分布式在本构建不生效。
INFIL = float(sys.argv[sys.argv.index("--infiltration") + 1]) if "--infiltration" in sys.argv else 0.0
# 河槽处理（2026-10-28 起默认 off）：
#   off       不做任何地形修改；dem.asc = dem_filled 重采样结果，用于验证真实地形出流性
#   watermask 以 8/6 常水水体为河槽位置；只取最大连通组件，不跨组件跳接、不水平延伸
#   centerline 读外部中心线；缺文件直接退出，不回退到其它模式
# 历史 run 一律为旧逻辑（贪心跳接 + 同一行延伸至西缘），审计判 FAIL（dem_channel_audit.json）。
CHANNEL = sys.argv[sys.argv.index("--channel") + 1] if "--channel" in sys.argv else "off"
if CHANNEL not in ("off", "watermask", "centerline"):
    raise SystemExit(f"--channel 只能是 off|watermask|centerline，实得 {CHANNEL!r}")
CHANNEL_FILE = Path(sys.argv[sys.argv.index("--channel-file") + 1]) if "--channel-file" in sys.argv else None
if CHANNEL == "centerline" and CHANNEL_FILE is None:
    raise SystemExit("--channel centerline 必须同时给 --channel-file（GeoJSON LineString）")
# 事件窗（Asia/Shanghai）：2026-08-21 00:00 ~ 08-25 12:00
SIM_SECONDS = 4.5 * 86400
DT_DEFAULT = 1.5 if RES <= 60 else (1.5 if RES <= 110 else 3.0)
# Phase B1：时间步敏感性可显式覆盖（--dt），默认沿用按分辨率的固定步长
DT = float(sys.argv[sys.argv.index("--dt") + 1]) if "--dt" in sys.argv else DT_DEFAULT
RUN_TAG = os.environ.get("RUN_TAG", "").strip()
RUN_ID = f"scen_q{int(Q_PEAK)}_n{int(FRIC * 1000)}_{int(RES)}m" + (
    f"_r{int(RAIN_SCALE * 100)}" if abs(RAIN_SCALE - 1.0) > 1e-9 else "") + (
    "_nsp" if MANNING_TIF is not None else "") + (
    f"_dt{str(DT).replace('.', 'p')}" if abs(DT - DT_DEFAULT) > 1e-9 else "") + (
    f"_inf{INFIL:.0e}".replace("e-0", "e-").replace("+", "") if INFIL > 0 else "") + (
    f"_ch{CHANNEL}" if CHANNEL != "watermask" else "") + (
    f"_{RUN_TAG}" if RUN_TAG else "")
RUN_DIR = PROJ / "outputs" / "ningming" / "2026-08-zitan" / RUN_ID

STATION = ("宁明县城", 107.0719, 22.1403)  # 站基面：7月 109.24 / 8月 123.43，抬升 14.19 m
RISE_OBS = 14.19


def tf_32648():
    return Transformer.from_crs(4326, 32648, always_xy=True)


def _largest_component(mask: np.ndarray) -> np.ndarray:
    """返回 mask 中最大 4-连通组件（布尔同形状）。"""
    from collections import deque

    seen = np.zeros(mask.shape, dtype=bool)
    best = np.zeros(mask.shape, dtype=bool)
    best_n = 0
    rows, cols = mask.shape
    for r0, c0 in zip(*np.nonzero(mask)):
        if seen[r0, c0]:
            continue
        comp = np.zeros(mask.shape, dtype=bool)
        queue = deque([(int(r0), int(c0))])
        seen[r0, c0] = True
        n = 0
        while queue:
            r, c = queue.popleft()
            comp[r, c] = True
            n += 1
            for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nr, nc = r + dr, c + dc
                if 0 <= nr < rows and 0 <= nc < cols and mask[nr, nc] and not seen[nr, nc]:
                    seen[nr, nc] = True
                    queue.append((nr, nc))
        if n > best_n:
            best_n, best = n, comp
    return best


def _read_centerline(path: Path, dem: np.ndarray,
                     bounds: tuple[float, float, float, float],
                     res: float) -> tuple[dict[int, int], dict[int, float], dict]:
    """读 GeoJSON LineString（EPSG:32648 米制），转成「列 -> 中心行 / 河床高程」。

    河床高程：沿线地表减去固定下切深度（默认 6 m，需在 meta 中登记）。
    """
    import json as _json

    if path is None or not path.is_file():
        raise SystemExit(f"centerline 模式缺少中心线文件：{path}")
    doc = _json.loads(path.read_text(encoding="utf-8"))
    geoms = [doc] if doc.get("type") == "Feature" else doc.get("features", [])
    coords: list[list[float]] = []
    for feat in geoms:
        geom = feat.get("geometry", feat) if isinstance(feat, dict) else {}
        if geom.get("type") != "LineString":
            continue
        coords.extend(geom.get("coordinates", []))
    if not coords:
        raise SystemExit(f"中心线文件无 LineString 坐标：{path}")

    xll, yll, xr, yt = bounds
    h, w = dem.shape
    depth = 6.0
    center_by_col: dict[int, int] = {}
    bed_by_col: dict[int, float] = {}
    for x, y in coords:
        c = int(round((float(x) - xll) / res))
        r = int(round((yt - float(y)) / res))
        if 0 <= r < h and 0 <= c < w:
            center_by_col[c] = r
    if not center_by_col:
        raise SystemExit(f"中心线全部落在网格外：{path}")
    for c, r in center_by_col.items():
        bed_by_col[c] = float(dem[r, c]) - depth
    return center_by_col, bed_by_col, {
        "centerline_file": str(path), "carve_depth_m": depth,
        "note": "centerline 模式按地表减固定深度生成河床，属情景假设，需断面资料复核",
    }


def _write_edit_rasters(run_dir: Path, edit_mask: np.ndarray, diff: np.ndarray,
                        bounds: tuple[float, float, float, float], res: float) -> None:
    """写地形修改掩膜与差异栅格（EPSG:32648），供可视化和审计使用。"""
    transform = rasterio.Affine(res, 0, bounds[0], 0, -res, bounds[3])
    base = {"driver": "GTiff", "height": edit_mask.shape[0], "width": edit_mask.shape[1],
            "count": 1, "crs": "EPSG:32648", "transform": transform, "compress": "lzw"}
    with rasterio.open(run_dir / "dem_edit_mask.tif", "w", dtype="uint8",
                       nodata=None, **base) as dst:
        dst.write(edit_mask.astype("uint8"), 1)
    with rasterio.open(run_dir / "dem_edit_diff.tif", "w", dtype="float32",
                       nodata=0.0, **base) as dst:
        dst.write(np.where(edit_mask, diff, 0.0).astype("float32"), 1)


def _audit_gate(run_dir: Path) -> dict | None:
    """对刚写好的 dem.asc 跑刻蚀审计，返回 gate 字段。"""
    try:
        sys.path.insert(0, str(PROJ / "scripts" / "stage3"))
        from dem_channel_audit import audit_run
    except Exception as exc:  # 审计不可用时不静默放行刻蚀
        raise SystemExit(f"无法加载 dem_channel_audit（{type(exc).__name__}: {exc}）；"
                         "刻蚀模式必须能审计") from exc
    rec = audit_run(run_dir)
    return (rec or {}).get("gate")



def prep() -> None:
    try:
        from scripts.common.run_manifest import prepare_run_dir
        prepare_run_dir(RUN_DIR)
    except FileExistsError:
        raise SystemExit(f"{RUN_DIR} 已有 res.mass；终稿 §10 禁止覆盖。请设置 RUN_TAG 使用新目录。") from None
    tf = tf_32648()
    corners = [tf.transform(x, y) for x, y in [(AOI[0], AOI[1]), (AOI[2], AOI[3])]]
    bounds = (min(c[0] for c in corners), min(c[1] for c in corners),
              max(c[0] for c in corners), max(c[1] for c in corners))
    with rasterio.open(DEM_FILLED) as src:
        with WarpedVRT(src, crs="EPSG:32648", resampling=Resampling.bilinear,
                       width=int((bounds[2] - bounds[0]) / RES), height=int((bounds[3] - bounds[1]) / RES),
                       transform=rasterio.Affine(RES, 0, bounds[0], 0, -RES, bounds[3])) as vrt:
            dem = vrt.read(1).astype("float32")
            out_transform = vrt.transform
    dem[dem == -9999.0] = 0.0
    h, w = dem.shape

    # --- 河槽处理（--channel，默认 off）---
    # 历史实现（≤2026-10-27）用贪心跨组件跳接 + 同一行延伸至西缘生成河槽；
    # 审计 dem_channel_audit.json 判 FAIL：距常水 >250 m 占 64%、最大下切 244 m、逐列常值。
    # 现改为显式三模式，默认不改动任何地形。
    exit_row = None
    channel_meta: dict = {"mode": CHANNEL, "edited_cells": 0}
    edit_mask = np.zeros(dem.shape, dtype=bool)
    dem_pre_edit = dem.copy()

    def _carve(center_by_col: dict[int, int], bed_by_col: dict[int, float], half_width: int) -> int:
        """按「列 -> 中心行 + 河床高程」下切；只在目标低于地表时修改。"""
        n = 0
        for c, r in center_by_col.items():
            if c not in bed_by_col:
                continue
            bed_c = bed_by_col[c]
            rows_c = np.arange(max(0, r - half_width), min(dem.shape[0], r + half_width + 1))
            if not rows_c.size:
                continue
            mask_c = rows_c[dem[rows_c, c] > bed_c]
            if mask_c.size:
                dem[mask_c, c] = bed_c
                edit_mask[mask_c, c] = True
                n += int(mask_c.size)
        return n

    if CHANNEL == "watermask":
        from rasterio.warp import reproject as _rp0
        with rasterio.open(PROJ / "data" / "interim" / "ningming" / "water_0806.tif") as ds0:
            frac0 = np.zeros(dem.shape, dtype="float32")
            tgt0 = rasterio.Affine(RES, 0, bounds[0], 0, -RES, bounds[3])
            _rp0((ds0.read(1) == 1).astype("float32"), frac0,
                 src_transform=ds0.transform, src_crs=ds0.crs,
                 dst_transform=tgt0, dst_crs="EPSG:32648", resampling=Resampling.average)
        wet = frac0 > 0.2
        if wet.any():
            comp = _largest_component(wet)  # 只取最大连通组件，不跨组件跳接
            cols = np.where(comp.any(axis=0))[0]
            c_e, c_w = int(cols.max()), int(cols.min())
            bed_east = (float(np.percentile(dem[comp[:, c_e], c_e], 25))
                        if comp[:, c_e].any() else float(dem[comp].min()))
            bed_west = bed_east - 4.5
            center_by_col: dict[int, int] = {}
            bed_by_col: dict[int, float] = {}
            for c in range(c_w, c_e + 1):
                rows_c = np.where(comp[:, c])[0]
                if not rows_c.size:
                    continue
                center_by_col[c] = int(np.median(rows_c))
                frac = (c_e - c) / max(c_e - c_w, 1)
                bed_by_col[c] = bed_east - frac * (bed_east - bed_west)
            channel_meta.update({
                "wet_cells": int(wet.sum()), "component_cells": int(comp.sum()),
                "col_range": [c_w, c_e],
                "bed_up_m": round(bed_east, 2), "bed_down_m": round(bed_west, 2),
                "note": "只取最大连通组件；不跨组件跳接、不水平延伸至西缘",
            })
            channel_meta["edited_cells"] = _carve(center_by_col, bed_by_col, 1)
            if center_by_col:
                exit_row = center_by_col[min(center_by_col)]
    elif CHANNEL == "centerline":
        center_by_col, bed_by_col, meta_cl = _read_centerline(CHANNEL_FILE, dem, bounds, RES)
        channel_meta.update(meta_cl)
        channel_meta["edited_cells"] = _carve(center_by_col, bed_by_col, 1)
        if center_by_col:
            exit_row = center_by_col[min(center_by_col)]
    else:
        channel_meta["note"] = "off：不做地形修改；dem.asc 等于 dem_filled 重采样结果"

    print(f"河槽模式 {CHANNEL}: 修改 {channel_meta['edited_cells']} 像元"
          + (f"，西缘出口行 {exit_row}" if exit_row is not None else ""))

    if CHANNEL != "off":
        np.savetxt(RUN_DIR / "dem_pre_carve.diff.txt",
                   np.where(edit_mask, dem - dem_pre_edit, 0.0),
                   fmt="%.2f", delimiter="\t",
                   header=f"ncols {w}\nnrows {h}\nxllcorner {bounds[0]:.1f}\nyllcorner {bounds[1]:.1f}\n"
                          f"cellsize {RES:.1f}\nNODATA_value -9999.0", comments="")
        _write_edit_rasters(RUN_DIR, edit_mask, dem - dem_pre_edit, bounds, RES)
    np.savetxt(RUN_DIR / "dem.asc", dem, fmt="%.3f", delimiter="\t",
               header=f"ncols         {w}\nnrows         {h}\nxllcorner     {bounds[0]:.1f}\n"
                      f"yllcorner     {bounds[1]:.1f}\ncellsize      {RES:.1f}\nNODATA_value  -9999.0",
               comments="")

    if CHANNEL != "off":
        gate = _audit_gate(RUN_DIR)
        channel_meta["audit_gate"] = gate
        if gate and gate.get("status") == "FAIL":
            for stale in ("dem.asc", "dem_pre_carve.diff.txt",
                          "dem_edit_mask.tif", "dem_edit_diff.tif"):
                (RUN_DIR / stale).unlink(missing_ok=True)
            raise SystemExit(
                f"河槽审计 FAIL，拒绝 prep：{RUN_DIR}\n  "
                + "\n  ".join(gate.get("reasons") or ["（无原因字段）"])
                + "\n  修正 --channel 参数或提供可核验中心线后再试。"
            )

    # --- 空间 Manning（可选）：把 WorldCover 先验 n 网格重投影到本 run 网格（最近邻，
    # n 为类别常数，不做插值），写 6 行头 ESRI ASC（input.cpp LoadManningsn 逐行读取）---
    manning_meta = None
    if MANNING_TIF is not None:
        from rasterio.warp import reproject as _rp_n
        nan_grid = np.full(dem.shape, np.nan, dtype="float32")
        with rasterio.open(MANNING_TIF) as dsn:
            _rp_n(dsn.read(1), nan_grid, src_transform=dsn.transform, src_crs=dsn.crs,
                  dst_transform=rasterio.Affine(RES, 0, bounds[0], 0, -RES, bounds[3]),
                  dst_crs="EPSG:32648", src_nodata=dsn.nodata, dst_nodata=np.nan,
                  resampling=Resampling.nearest)
        miss = ~np.isfinite(nan_grid)
        nan_grid[miss] = FRIC  # 覆盖不到的像元回落 fpfric（=对比基准值），登记数量
        np.savetxt(RUN_DIR / "manning.asc", nan_grid, fmt="%.4f", delimiter="\t",
                   header=f"ncols         {w}\nnrows         {h}\nxllcorner     {bounds[0]:.1f}\n"
                          f"yllcorner     {bounds[1]:.1f}\ncellsize      {RES:.1f}\nNODATA_value  -9999.0",
                   comments="")
        manning_meta = {
            "source": str(MANNING_TIF),
            "resampling": "nearest（类别常数，不插值）",
            "grid": [int(w), int(h)], "res_m": RES,
            "n_mean": round(float(nan_grid.mean()), 4),
            "n_min": round(float(nan_grid.min()), 4),
            "n_max": round(float(nan_grid.max()), 4),
            "n_p10": round(float(np.percentile(nan_grid, 10)), 4),
            "n_p50": round(float(np.percentile(nan_grid, 50)), 4),
            "n_p90": round(float(np.percentile(nan_grid, 90)), 4),
            "uncovered_cells_fallback_fpfric": int(miss.sum()),
            "fpfric_used_as_fallback": FRIC,
        }
        print(f"空间 Manning: n 均值 {manning_meta['n_mean']}，范围 "
              f"[{manning_meta['n_min']}, {manning_meta['n_max']}]，"
              f"未覆盖回落 {int(miss.sum())} 像元")

    # 上游点源：用 8/6 常水期水体（真实河道）在域内最东侧的湿单元定位，
    # 避免误选喀斯特封闭洼地（域内 82-92m 低带为岩溶洼地，非河槽，见 meta 注记）
    W06 = PROJ / "data" / "interim" / "ningming" / "water_0806.tif"
    from rasterio.warp import reproject as _rp
    with rasterio.open(W06) as ds:
        w06 = np.zeros(dem.shape, dtype="uint8")
        tgt = rasterio.Affine(RES, 0, bounds[0], 0, -RES, bounds[3])
        _rp(ds.read(1), w06, src_transform=ds.transform, src_crs=ds.crs,
            dst_transform=tgt, dst_crs="EPSG:32648", resampling=Resampling.nearest)
    wet = w06 == 1
    if wet.any():
        cols_with_wet = np.where(wet.any(axis=0))[0]
        c_east = int(cols_with_wet.max())
        rows_wet = np.where(wet[:, c_east])[0]
        src_row = int(rows_wet[np.argmin(dem[rows_wet, c_east])])
        src_col = c_east
    else:  # 兜底：东缘最低
        edge = dem[:, -6:]
        r, c = np.unravel_index(np.argmin(edge), edge.shape)
        src_row, src_col = int(r), int(w - 6 + c)
    sx, sy = out_transform * (src_col + 0.5, src_row + 0.5)
    print(f"上游点源: 行{src_row} 列{src_col} 高程{dem[src_row, src_col]:.1f}m 32648=({sx:.0f},{sy:.0f})")

    # .bci：P 行按手册填「地图坐标（easting/northing）」——非行列号（首版错误致水源落在山坡）
    # 西缘出流。G14 真根因（input.cpp 解析器实测）：bci 边界区间是地图坐标（米），不是单元序号
    # ——小数值被钳位到域角且交换后区间为空 -> 零单元赋值 -> 零出流。修复：超大坐标（钳位=全边段）。
    # 另：FREE 无坡度时只用边缘两格局部水面坡度（boundary.cpp），漫积水面近水平 -> 仍需显式坡度。
    BIG = 99999999
    (RUN_DIR / "bci.bci").write_text(
        f"P\t{sx:.1f}\t{sy:.1f}\tQVAR\tmingjiang\n"
        f"W\t-{BIG}\t{BIG}\tFREE\t0.0002\n"
        f"N\t-{BIG}\t{BIG}\tFREE\nS\t-{BIG}\t{BIG}\tFREE\nE\t-{BIG}\t{BIG}\tFREE\n",
        encoding="ascii", newline="\n")

    # 上游 Q 情景（三角线 + 基流）：08-22 00 起涨 -> 08-24 20 峰 -> 08-25 12 落至基流
    # 点源标度律（B6_bdy_units 受控试验，2026-10-24）：实际 m³/s = 文件值 × 网格边长
    # ——故文件值 = 目标流量 / RES
    qbase, t_rise0, t_peak, t_end = 80.0, 86400.0, 3 * 86400 + 20 * 3600, SIM_SECONDS
    scale = RES  # 目标流量(米3/秒) -> 文件值
    qs = [qbase / scale, qbase / scale, Q_PEAK / scale, 500.0 / scale]
    ts = [0.0, t_rise0, t_peak, t_end]
    (RUN_DIR / "mingjiang.bdy").write_text(
        "# scenario upstream hydrograph (triangular, NO observed Q(t)); file_value = Q_m3s / cellsize\n"
        f"mingjiang\n{len(ts)} seconds\n" +
        "".join(f"{q:.4f}\t{t:.0f}\n" for q, t in zip(qs, ts)), encoding="ascii", newline="\n")

    # .rain：ERA5 县城锚点逐时（8/21 00 ~ 8/25 12），均匀面雨
    era5 = json.loads(ERA5.read_text(encoding="utf-8"))
    pts = [(0.0, 0.0)]
    for day in ("2026-08-21", "2026-08-22", "2026-08-23", "2026-08-24", "2026-08-25"):
        if day not in era5["daily_mm"]:
            continue
        for hr in range(24):
            t = (int(day[-2:]) - 21) * 86400 + hr * 3600
            if t > SIM_SECONDS:
                break
            mmh = era5["daily_mm"][day] / 24.0  # 日均匀化（降级口径，登记）
            pts.append((mmh, float(t)))
    pts.append((0.0, SIM_SECONDS))
    pts = sorted({t: v for v, t in pts}.items())
    pts = [(t, v * RAIN_SCALE) for t, v in pts]  # 水量维度敏感性：ERA5 均匀雨整体折减
    assert all(b[0] > a[0] for a, b in zip(pts, pts[1:])), "rain 时刻必须严格递增"
    (RUN_DIR / "rain.rain").write_text(
        "# ERA5 county-anchor hourly-averaged uniform rain (degraded)\n"
        f"{len(pts)} seconds\n" +
        "".join(f"{v:.4f}\t{t:.0f}\n" for t, v in pts), encoding="ascii", newline="\n")

    (RUN_DIR / "run.par").write_text(
        f"DEMfile\tdem.asc\nbcifile\tbci.bci\nbdyfile\tmingjiang.bdy\nrainfall\train.rain\n"
        f"resroot\tres\ndirroot\tresults\nsim_time\t{SIM_SECONDS:.0f}\n"
        f"initial_tstep\t{DT}\nmassint\t3600\nsaveint\t21600\n"
        f"fpfric\t{FRIC}\n" + ("manningfile\tmanning.asc\n" if MANNING_TIF is not None else "") +
        (f"infiltration\t{INFIL:.3e}\n" if INFIL > 0 else "") +
        "acceleration\n", encoding="ascii", newline="\n")
    meta = {"run_id": RUN_ID, "aoi": AOI, "res": RES, "dt": DT, "q_peak": Q_PEAK, "fpfric": FRIC,
            "rain_scale": RAIN_SCALE,
            "manning_spatial": manning_meta,
            "infiltration_m_s": INFIL,
            "infiltration_note": ("标量全域入渗（仅非河道单元；ChanMask 全 -1 时=全域），"
                                  "喀斯特有界损失代理，非分布式 .inf，非实测入渗率") if INFIL > 0 else None,
            "channel": channel_meta,
            "point_source_32648": [sx, sy],
            "point_source_elev_m": round(float(dem[src_row, src_col]), 1),
            "point_source_method": "8/6 常水期水体（河道）最东湿单元",
            "dem_channel_note": (
                "off 模式：未做任何河槽刻蚀，dem.asc 等于 dem_filled 重采样结果；"
                "用于验证真实地形能否把上游来水送到出流边界"
                if CHANNEL == "off" else
                "河槽为情景假设，非实测断面；审计结果见 run 目录 audit 记录与 "
                "data/catalog/dem_channel_audit.json"),
            "sim_seconds": SIM_SECONDS,
            "scenarios_note": "上游三角Q情景+ERA5均匀雨+FREE边界；情景级结论（ADR-0004）",
            "generated_utc": datetime.now(timezone.utc).isoformat()}
    (RUN_DIR / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print("prep 完成 ->", RUN_DIR)


def run() -> dict:
    import psutil
    proc = subprocess.Popen([str(LISFLOOD), "run.par"], cwd=RUN_DIR,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    peak = {"mb": 0.0}
    p = psutil.Process(proc.pid)

    def poll():
        while proc.poll() is None:
            try:
                peak["mb"] = max(peak["mb"], p.memory_info().rss / 1e6)
            except Exception:
                break
            time.sleep(0.2)

    threading.Thread(target=poll, daemon=True).start()
    started_utc = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()
    out, _ = proc.communicate(timeout=24 * 3600)
    wall = time.monotonic() - t0
    (RUN_DIR / "run.log").write_text(out, encoding="utf-8", errors="replace")
    rec = {"exit": proc.returncode, "wall_s": round(wall, 1), "peak_rss_mb": round(peak["mb"], 1)}
    write_run_manifest_best_effort(
        RUN_DIR, peak_rss_mb=rec["peak_rss_mb"], started_utc=started_utc,
        warnings=["运行后清单；verify 完成后更新质量门。"],
    )
    print(json.dumps(rec), "（日志 run.log）")
    return rec


def _read_asc(p: Path):
    with open(p) as f:
        hdr = [next(f) for _ in range(6)]
    return np.loadtxt(p, skiprows=6), {l.split()[0]: float(l.split()[1]) for l in hdr}


def verify() -> None:
    res_dir = RUN_DIR / "results"
    rmax, hdr = _read_asc(res_dir / "res.max")
    cell = hdr["cellsize"]
    xll, yll = hdr["xllcorner"], hdr["yllcorner"]

    # 8/24 观测新增淹没 -> 本域网格（最近邻重投影）
    from rasterio.transform import Affine as A
    from rasterio.warp import reproject
    with rasterio.open(W24) as ds:
        tgt = A(cell, 0, xll, 0, -cell, yll + rmax.shape[0] * cell)
        obs = np.zeros_like(rmax, dtype="uint8")
        mask_all = ds.read(1)
        reproject(mask_all, obs, src_transform=ds.transform, src_crs=ds.crs,
                  dst_transform=tgt, dst_crs="EPSG:32648",
                  resampling=Resampling.nearest)

    sim_wet = rmax > 0.05  # 5cm 淹没阈值（候选之一，终稿 8.1 敏感性另做）
    obs_wet = obs == 1
    valid = obs_wet | sim_wet  # 观测仅覆盖县界内走廊；域内均有效
    H = int((sim_wet & obs_wet).sum())
    FA = int((sim_wet & ~obs_wet).sum())
    M = int((~sim_wet & obs_wet).sum())
    csi = H / max(H + FA + M, 1)
    pod = H / max(H + M, 1)
    far = FA / max(H + FA, 1)
    f1 = 2 * H / max(2 * H + FA + M, 1)
    area_sim = sim_wet.sum() * cell * cell / 1e6
    area_obs = obs_wet.sum() * cell * cell / 1e6

    # 县城站水位抬升：取最后时刻 .wd + DEM
    wds = sorted(res_dir.glob("res-*.wd"))
    wd_last, _ = _read_asc(wds[-1])
    # 站点像元
    tf2 = Transformer.from_crs(4326, 32648, always_xy=True)
    sx, sy = tf2.transform(STATION[1], STATION[2])
    col = int((sx - xll) / cell)
    row = int((yll + rmax.shape[0] * cell - sy) / cell)
    # 站点邻域最大水深 -> 水位抬升近似（含基流通道水深）
    r0, r1 = max(0, row - 2), min(rmax.shape[0], row + 3)
    c0, c1 = max(0, col - 2), min(rmax.shape[1], col + 3)
    wd_station = float(wd_last[r0:r1, c0:c1].max())
    # mass：终稿 13.2 口径 E_mass = |V_end−V_start−V_rain−∫(Qin−Qout)dt| / max(V_start+V_rain+∫Qin dt, V_floor)
    mass_lines = (res_dir / "res.mass").read_text().splitlines()
    keys = mass_lines[0].split()
    rows = [{k: float(v) for k, v in zip(keys, ln.split())} for ln in mass_lines[1:] if ln.split()]
    v_in = 0.0
    v_out = 0.0
    for a, b in zip(rows, rows[1:]):
        dt = b["Time"] - a["Time"]
        v_in += 0.5 * (a["Qin"] + b["Qin"]) * dt
        v_out += 0.5 * (a["Qout"] + b["Qout"]) * dt
    v_rain = rows[-1]["Rain-(Inf+Evap)"]
    residual = abs(rows[-1]["Vol"] - rows[0]["Vol"] - v_rain - (v_in - v_out))
    denom = max(rows[0]["Vol"] + v_rain + v_in, 1.0)
    e_mass = residual / denom
    report = {
        "run_id": RUN_ID, "threshold_m": 0.05,
        "H": H, "FA": FA, "M": M,
        "csi": round(csi, 3), "pod": round(pod, 3), "far": round(far, 3), "f1": round(f1, 3),
        "area_sim_km2": round(area_sim, 2), "area_obs_km2": round(area_obs, 2),
        "area_bias": round(area_sim / max(area_obs, 1e-9), 2),
        "station_wd_final_m": round(wd_station, 2),
        "station_wd_center_m": round(float(wd_last[row, col]), 3),
        "station_radius_cells": 2,
        "obs_rise_station_m": RISE_OBS,
        "e_mass_132": float(f"{e_mass:.3e}"),
        "v_in_integrated_m3": round(v_in, 1), "v_out_integrated_m3": round(v_out, 1),
        "v_rain_m3": round(v_rain, 1),
        "final_vol_m3": round(rows[-1]["Vol"], 1),
        "qout_final": rows[-1]["Qout"],
        "metric_semantics": "旧口径：res.max/最后帧，仅最大范围；时刻切片验证见 ningming_ensemble.py",
        "note": "水位抬升对照需考虑通道基流水深与站基面差；情景级结论",
    }
    out = RUN_DIR / "verify.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_run_manifest_best_effort(
        RUN_DIR,
        mass_gate={"status": "PASS" if e_mass <= 1e-3 else "FAIL", "e_mass": float(f"{e_mass:.3e}")},
        warnings=["验证口径：正式观测时刻切片由 ningming_ensemble.py 生成。"],
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("prep", "all"):
        prep()
    if cmd in ("run", "all"):
        run()
    if cmd in ("verify", "all"):
        verify()


if __name__ == "__main__":
    main()
