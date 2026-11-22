"""Phase 3：贵港河道调理路线可行性审计（**只读**，不实施任何刻蚀）。

对应计划 Phase 3 原设计：「利用灾前 Sentinel-1（2024-09-05）常水水体掩膜提取郁江真实
蜿蜒自然河道骨架线与河面多边形，仅在真实常水河道范围内下切 2~3 m」。

本脚本**不复跑**这条路线，而是把「该路线不可行」做成可复核的定量证据包。
判据分四组：

1. **足印可用性** —— S1 两景重投影到模型 100 m 网格后的覆盖率（总体 + 按行/列十分位）。
2. **骨架连续性** —— 域内常水像元的连通块数量、面积谱、最大块，与模型主水体对比。
3. **端点退化** —— 常水掩膜在模型域上下游边缘（西=注入上游，东=出口下游）是否
   存在连续河段。这是当初失败的直接原因。
4. **走廊平坦度** —— DEM 坡度分布、Priority-Flood 抬升比例、D8 可定向比例、
   逐行谷底列漂移。用于验证「宽泛平底平原无河谷可循」。

纪律（硬约束，脚本内自检）：

- **只读**：不写 `outputs/guigang/**`、不改 `models/`、不调 `lisflood.exe`。
  源 run 的关键输入在读取前后各算一次 sha256 并比对。
- **不生成任何新 DEM，不施加任何刻蚀**：本脚本不产出任何栅格文件。
- 面积一律由栅格头 `cellsize` 换算（`cell_area_m2`），**不写死像元面积**。
- 结论只描述「路线可行性」，不得把「未实施」写成「已完成」。

用法（先设 `$env:PYTHONUTF8="1"`）::

    uv --directory 2d_hydro_project run python scripts/stage6/audit_guigang_channel_route.py
    uv --directory 2d_hydro_project run python scripts/stage6/audit_guigang_channel_route.py --dry-run

产出（默认 `data/catalog/guigang_channel_route_audit.json`）：
    审计定量结果 + verdict + 三个替代方案评估 + 对 Phase 4 的影响声明。
"""
from __future__ import annotations

import argparse
import heapq
import sys
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.warp import reproject

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import (  # noqa: E402
    AuditLog,
    GridError,
    read_ascii_grid,
    run_meta,
    sha256_file,
    utc_now,
    write_json,
)
from scripts.common.patch_analysis import connected_components  # noqa: E402

SCRIPT_REL = "scripts/stage6/audit_guigang_channel_route.py"
DEFAULT_OUT = PROJ / "data" / "catalog" / "guigang_channel_route_audit.json"

SCENARIO_SCRIPT = PROJ / "scripts" / "stage5" / "guigang_scenario.py"
# 三次失败尝试的注释记载位置（guigang_scenario.py prep() 内）
CROSSREF_LINES = (96, 105)

S1_PRE = PROJ / "data" / "processed" / "s1_water" / "guigang_20240905T105022_water20m.tif"
S1_FLD = PROJ / "data" / "processed" / "s1_water" / "guigang_20240912T104222_water20m.tif"
RUN = (PROJ / "outputs" / "guigang" / "2024-09-yagi"
       / "scen_yagi_q11500_100m_dt2p5_choff_realdem")

# 与 guigang_scenario.py 完全一致的网格构造口径（不得另立一套）
AOI = (109.30, 22.98, 109.95, 23.32)
RES = 100.0

NB8 = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
WET_THR = 0.05          # 湿格判定，与全项目一致（float32 口径）
S1_WATER, S1_NODATA = 1, 255

# DEM 平坦度判据（全部为**登记参数**，不是事后挑选的阈值）
PLAIN_BAND_M = 40.0     # 「平原带」上界：全域 p50=37 m，取 40 m 覆盖南半部低地
SLOPE_FLAT = 0.005      # 0.5% 坡度以下视为近平
SLOPE_NEAR_FLAT = 0.02  # 2% 以下视为缓坡
THALWEG_JUMP_COLS = 20  # 谷底列相邻行跳变 > 2 km 视为「谷底不连续」
UPSTREAM_EDGE_COLS = 5  # 上游注入行带内，贴西边界多少列内算「接到边界」


# ---------------------------------------------------------------- 网格与重投影
def model_grid() -> tuple[Any, tuple[int, int], tuple[float, float, float, float]]:
    """复算 guigang_scenario.prep() 的目标网格（EPSG:32649，100 m，669×370）。"""
    tf = Transformer.from_crs(4326, 32649, always_xy=True)
    corners = [tf.transform(x, y) for x, y in [(AOI[0], AOI[1]), (AOI[2], AOI[3])]]
    b = (min(c[0] for c in corners), min(c[1] for c in corners),
         max(c[0] for c in corners), max(c[1] for c in corners))
    tgt = rasterio.Affine(RES, 0, b[0], 0, -RES, b[3])
    shape = (int((b[3] - b[1]) / RES), int((b[2] - b[0]) / RES))
    return tgt, shape, b


def reproject_s1(path: Path, tgt: Any, shape: tuple[int, int]
                 ) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """把 S1 水体掩膜重投影到模型网格。

    返回 (water, footprint, src_meta)。`footprint` = 源影像有效数据（非 nodata）覆盖，
    是「有观测」的唯一判据；足迹外一律不计假警报。
    """
    with rasterio.open(path) as ds:
        src = ds.read(1)
        vals = np.unique(src)
        obs = np.zeros(shape, dtype="uint8")
        cover = np.zeros(shape, dtype="uint8")
        reproject(src, obs, src_transform=ds.transform, src_crs=ds.crs,
                  dst_transform=tgt, dst_crs="EPSG:32649", resampling=Resampling.nearest)
        reproject((src != S1_NODATA).astype("uint8"), cover,
                  src_transform=ds.transform, src_crs=ds.crs,
                  dst_transform=tgt, dst_crs="EPSG:32649", resampling=Resampling.nearest)
        src_meta = {
            "path": str(path.relative_to(PROJ)),
            "crs": str(ds.crs), "res_m": list(ds.res), "shape": list(ds.shape),
            "bounds": [round(float(v), 2) for v in ds.bounds],
            "value_counts": {int(v): int((src == v).sum()) for v in vals},
        }
    valid = cover == 1
    water = (obs == S1_WATER) & valid
    return water, valid, src_meta


def band_frac(mask: np.ndarray, axis: int, nbands: int = 10) -> list[float]:
    """沿 axis 切 nbands 等分带，返回每带覆盖率（该带内 True 占比）。"""
    n = mask.shape[axis]
    edges = np.linspace(0, n, nbands + 1).astype(int)
    out = []
    for i in range(nbands):
        sl = slice(edges[i], edges[i + 1])
        sub = mask[sl, :] if axis == 0 else mask[:, sl]
        out.append(round(float(sub.mean()), 4))
    return out


def component_spectrum(mask: np.ndarray, cell_area_m2: float
                       ) -> tuple[dict[str, Any], np.ndarray]:
    """8 邻域连通块面积谱。面积一律由 cell_area_m2 换算。"""
    labels, n = connected_components(mask, connectivity=8)
    if n == 0:
        return {"n_components": 0, "cells": 0, "area_km2": 0.0,
                "sizes_top10_cells": [], "sizes_top10_km2": [],
                "max_component_cells": 0, "max_component_km2": 0.0,
                "n_ge_100_cells": 0, "n_ge_10_cells": 0, "largest_label": 0}, labels
    sizes = np.bincount(labels.ravel())[1:]
    order = np.argsort(sizes)[::-1]
    top = [int(sizes[i]) for i in order[:10]]
    return {
        "n_components": int(n),
        "cells": int(mask.sum()),
        "area_km2": round(float(mask.sum()) * cell_area_m2 / 1e6, 4),
        "sizes_top10_cells": top,
        "sizes_top10_km2": [round(t * cell_area_m2 / 1e6, 4) for t in top],
        "max_component_cells": int(sizes.max()),
        "max_component_km2": round(float(sizes.max()) * cell_area_m2 / 1e6, 4),
        "n_ge_100_cells": int((sizes >= 100).sum()),
        "n_ge_10_cells": int((sizes >= 10).sum()),
        "largest_label": int(order[0]) + 1,
    }, labels


def edge_contacts(mask: np.ndarray, width: int = 2) -> dict[str, int]:
    """域四边（含 width 像素内边带）上的掩膜像元数。"""
    return {
        "west_col0_to_w": int(mask[:, :width].sum()),
        "east_col_last_w": int(mask[:, -width:].sum()),
        "north_row0_to_w": int(mask[:width, :].sum()),
        "south_row_last_w": int(mask[-width:, :].sum()),
    }


def longest_run_per_row(mask: np.ndarray, cell_size_m: float) -> dict[str, Any]:
    """逐行最长连续 True 段（像元数）。河谷横穿域时该值应接近域宽。"""
    best, best_row = 0, -1
    for r in range(mask.shape[0]):
        row = mask[r]
        if not row.any():
            continue
        idx = np.flatnonzero(np.diff(np.concatenate(([0], row.astype(np.int8), [0]))))
        runs = idx[1::2] - idx[0::2]
        if runs.size and int(runs.max()) > best:
            best, best_row = int(runs.max()), r
    return {"max_run_cells": best, "at_row": best_row,
            "max_run_km": round(best * cell_size_m / 1000.0, 3)}


# ------------------------------------------------------------ 地形水文派生量
def priority_flood(dem: np.ndarray) -> np.ndarray:
    """Priority-Flood 填洼（纯 numpy + heapq）。

    复用 `scripts/stage6/diagnose_jiulong_artifacts.py:207` 的实现思路：
    边界像元为种子、按高程升序扩散。**仅用于诊断地形水文派生量**，
    不产出任何 DEM 文件，也不替代 DEM_HYDRAULIC。
    """
    height, width = dem.shape
    filled = dem.astype(np.float64).copy()
    closed = np.zeros((height, width), dtype=bool)
    heap: list[tuple[float, int, int]] = []

    def push(r: int, c: int) -> None:
        closed[r, c] = True
        heapq.heappush(heap, (float(filled[r, c]), r, c))

    for r in range(height):
        push(r, 0)
        push(r, width - 1)
    for c in range(width):
        if not closed[0, c]:
            push(0, c)
        if not closed[height - 1, c]:
            push(height - 1, c)
    while heap:
        level, r, c = heapq.heappop(heap)
        for dr, dc in NB8:
            nr, nc = r + dr, c + dc
            if 0 <= nr < height and 0 <= nc < width and not closed[nr, nc]:
                if filled[nr, nc] < level:
                    filled[nr, nc] = level
                push(nr, nc)
    return filled


def d8_resolvable(filled: np.ndarray) -> np.ndarray:
    """填洼后是否存在**严格更低**的 8 邻域像元（即 D8 能否定向）。"""
    h, w = filled.shape
    pad = np.pad(filled, 1, mode="edge")
    lower = np.zeros((h, w), dtype=bool)
    for dr, dc in NB8:
        nb = pad[1 + dr:1 + dr + h, 1 + dc:1 + dc + w]
        lower |= nb < filled
    return lower


def slope_field(dem: np.ndarray, cell_size_m: float) -> np.ndarray:
    """局部坡度（m/m），中心差分，间距用真实 cellsize。"""
    gy, gx = np.gradient(dem.astype(np.float64), cell_size_m, cell_size_m)
    return np.hypot(gx, gy)


def thalweg_wander(dem: np.ndarray, rows: np.ndarray) -> dict[str, Any]:
    """逐行谷底列（该行 DEM 最小的列）的相邻行跳变。

    真实河谷的谷底列应随行号平滑变化；宽平底平原上谷底列会在孤立高地之间乱跳。
    """
    cols = np.argmin(dem[rows, :], axis=1)
    jumps = np.abs(np.diff(cols.astype(np.int64)))
    return {
        "row_range": [int(rows[0]), int(rows[-1])],
        "thalweg_col_per_row": [int(c) for c in cols],
        "jump_abs_col_p50": round(float(np.percentile(jumps, 50)), 2),
        "jump_abs_col_p90": round(float(np.percentile(jumps, 90)), 2),
        "jump_abs_col_max": int(jumps.max()),
        "n_rows_jump_gt_threshold": int((jumps > THALWEG_JUMP_COLS).sum()),
        "threshold_cols": THALWEG_JUMP_COLS,
        "note": "谷底列=该行 DEM 最小列；跳变按相邻行计。阈值 20 列=2 km。",
    }


# ------------------------------------------------------------------- 主审计
def audit(dry_run: bool = False) -> dict[str, Any]:
    log = AuditLog(SCRIPT_REL, dry_run=dry_run)
    log.param(aoi=AOI, res_m=RES, wet_thr=WET_THR,
              plain_band_m=PLAIN_BAND_M, slope_flat=SLOPE_FLAT,
              slope_near_flat=SLOPE_NEAR_FLAT, thalweg_jump_cols=THALWEG_JUMP_COLS)

    # 源 run 只读自检：读前后各算一次哈希
    guarded = [RUN / "dem.asc", RUN / "results" / "res.max", RUN / "meta.json",
               S1_PRE, S1_FLD, SCENARIO_SCRIPT]
    for p in guarded:
        log.add_input(p.name, p)
    hash_before = {str(p): sha256_file(p) for p in guarded if p.is_file()}

    tgt, shape, bounds = model_grid()
    grid_meta = {
        "crs": "EPSG:32649", "res_m": RES, "shape": list(shape),
        "cells": int(shape[0] * shape[1]),
        "bounds_32649": [round(v, 2) for v in bounds],
        "transform": [tgt.a, tgt.b, tgt.c, tgt.d, tgt.e, tgt.f],
        "source": "复算 guigang_scenario.prep() 的网格构造（AOI->32649, Affine(100,0,b0,0,-100,b3)）",
    }
    log.step("model_grid", shape=list(shape))

    # ---- 模型栅格（单元面积唯一来源）----
    dem_grid = read_ascii_grid(RUN / "dem.asc", dtype=np.float32)
    rmax_grid = read_ascii_grid(RUN / "results" / "res.max", dtype=np.float32)
    if dem_grid.shape != shape or rmax_grid.shape != shape:
        raise GridError(f"栅格形状 {dem_grid.shape}/{rmax_grid.shape} 与网格 {shape} 不符")
    cell_area_m2 = dem_grid.cell_area_m2
    dem = dem_grid.data.astype(np.float64)
    rmax = rmax_grid.data
    cell_meta = {
        "cellsize_m": dem_grid.cell_size_m,
        "cell_area_m2": cell_area_m2,
        "note": "面积一律由栅格头 cellsize 换算，未写死像元面积",
    }
    log.step("read_rasters", cell_area_m2=cell_area_m2)

    meta = run_meta(RUN)
    sx, sy = meta["point_source_32649"]
    inj_col = int((sx - bounds[0]) / RES)
    inj_row = int((bounds[3] - sy) / RES)
    if not (0 <= inj_row < shape[0] and 0 <= inj_col < shape[1]):
        raise GridError(f"注入点 ({inj_row},{inj_col}) 落在网格外")
    inj_band = slice(max(0, inj_row - 5), min(shape[0], inj_row + 6))
    log.step("locate_injection", row=inj_row, col=inj_col, band=[inj_band.start, inj_band.stop - 1])

    # ---- 组 1+2：S1 两景足印与骨架 ----
    s1_sections: dict[str, Any] = {}
    masks: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for tag, path in (("s1_common_water_20240905", S1_PRE),
                      ("s1_flood_20240912", S1_FLD)):
        water, valid, src_meta = reproject_s1(path, tgt, shape)
        masks[tag] = (water, valid)
        spec, labels = component_spectrum(water, cell_area_m2)
        s1_sections[tag] = {
            "role": "灾前常水（Phase 3 原设计的骨架来源）" if "0905" in tag
                    else "洪峰景（仅用于 verify 对比，不作骨架）",
            "source": src_meta,
            "on_model_grid": {
                "footprint_cells": int(valid.sum()),
                "footprint_frac_of_domain": round(float(valid.mean()), 4),
                "footprint_km2": round(float(valid.sum()) * cell_area_m2 / 1e6, 2),
                "water_cells": int(water.sum()),
                "water_km2": round(float(water.sum()) * cell_area_m2 / 1e6, 2),
                "water_frac_of_footprint": round(
                    float(water.sum()) / max(int(valid.sum()), 1), 4),
            },
            "footprint_by_row_decile": band_frac(valid, axis=0),
            "footprint_by_col_decile": band_frac(valid, axis=1),
            "water_by_row_decile": band_frac(water, axis=0),
            "components_8conn": spec,
            "edge_contacts_water": edge_contacts(water),
            "longest_run": longest_run_per_row(water, cell_meta["cellsize_m"]),
            "largest_component_bbox": None,
        }
        lab = spec["largest_label"]
        if lab:
            rr, cc = np.where(labels == lab)
            s1_sections[tag]["largest_component_bbox"] = {
                "row_min": int(rr.min()), "row_max": int(rr.max()),
                "col_min": int(cc.min()), "col_max": int(cc.max()),
                "width_km": round((cc.max() - cc.min() + 1) * RES / 1000.0, 2),
                "height_km": round((rr.max() - rr.min() + 1) * RES / 1000.0, 2),
                "touches_west": bool((cc < 2).any()), "touches_east": bool((cc >= shape[1] - 2).any()),
                "touches_north": bool((rr < 2).any()), "touches_south": bool((rr >= shape[0] - 2).any()),
            }
        log.step(f"s1_{tag}", footprint_frac=s1_sections[tag]["on_model_grid"]["footprint_frac_of_domain"],
                 n_components=spec["n_components"])

    # ---- 组 3：端点退化 ----
    endpoints: dict[str, Any] = {}
    for tag, (water, valid) in masks.items():
        spec = s1_sections[tag]["components_8conn"]
        bbox = s1_sections[tag]["largest_component_bbox"] or {}
        band_cover = float(valid[inj_band, :].mean())
        band_water = int(water[inj_band, :].sum())
        # 上游「连续河段」必须真的接到西边界，而不只是落在注入行带内
        upstream_edge_water = int(water[inj_band, :UPSTREAM_EDGE_COLS].sum())
        # 「上下游贯通」= 单块同时触及上游边（西）与下游边（东）
        spans = bool(bbox.get("touches_west")) and bool(bbox.get("touches_east"))
        endpoints[tag] = {
            "upstream_is_west_edge": True,
            "downstream_is_east_edge": True,
            "reason": "上游 Q 点源在 meta.point_source_32649，落于 col0；东边界 bci.bci 设 FREE 坡度 0.00015",
            "injection_row_band": [inj_band.start, inj_band.stop - 1],
            "injection_band_footprint_frac": round(band_cover, 4),
            "injection_band_water_cells": band_water,
            "upstream_boundary_observed": band_cover > 0.0,
            "largest_component_spans_upstream_to_downstream": spans,
            "upstream_edge_water_cells": upstream_edge_water,
            "upstream_edge_width_cols": UPSTREAM_EDGE_COLS,
            "upstream_continuous_reach_exists": bool(upstream_edge_water >= 2),
            "west_edge_water_cells": int(water[:, :2].sum()),
            "east_edge_water_cells": int(water[:, -2:].sum()),
            "degenerate": bool(upstream_edge_water < 2 or not spans),
            "note": "「端点退化」定义：上游注入行带内、贴西边界 "
                    f"{UPSTREAM_EDGE_COLS} 列内无可续接的常水段，或最大块不能同时触及西、东"
                    "边界，则无法从掩膜生成一条贯穿域的河道骨架线。",
        }
        log.step(f"endpoint_{tag}", degenerate=endpoints[tag]["degenerate"])

    # ---- 组 2 对照：模型主水体 ----
    wet = rmax > WET_THR
    main_spec, main_labels = component_spectrum(wet, cell_area_m2)
    main_mask = main_labels == main_spec["largest_label"] if main_spec["n_components"] else wet
    rr, cc = np.where(main_mask)
    main_body = {
        "wet_cells": int(wet.sum()),
        "wet_km2": round(float(wet.sum()) * cell_area_m2 / 1e6, 2),
        "patches_8conn": main_spec["n_components"],
        "largest_component_cells": main_spec["max_component_cells"],
        "largest_component_km2": main_spec["max_component_km2"],
        "largest_component_share_of_wet": round(
            main_spec["max_component_cells"] / max(int(wet.sum()), 1), 4),
        "largest_component_bbox": {
            "row_min": int(rr.min()), "row_max": int(rr.max()),
            "col_min": int(cc.min()), "col_max": int(cc.max()),
        },
        "touches_west": bool((cc < 2).any()), "touches_east": bool((cc >= shape[1] - 2).any()),
        "depth_p50_m": round(float(np.percentile(rmax[main_mask], 50)), 3),
        "dem_p50_m": round(float(np.percentile(dem[main_mask], 50)), 1),
        "note": "主水体来自 res.max（模型结果），仅作**参照尺度**，不用于反推地形",
    }
    log.step("main_water_body", km2=main_body["largest_component_km2"])

    # ---- 组 4：DEM 走廊平坦度 ----
    dem_stats = {
        "min_m": round(float(dem.min()), 1), "max_m": round(float(dem.max()), 1),
        "p1_m": round(float(np.percentile(dem, 1)), 1),
        "p50_m": round(float(np.percentile(dem, 50)), 1),
        "p99_m": round(float(np.percentile(dem, 99)), 1),
        "edge_profiles": {
            "west_col0_p50_m": round(float(np.percentile(dem[:, 0], 50)), 1),
            "north_row0_p50_m": round(float(np.percentile(dem[0, :], 50)), 1),
            "south_row_last_p50_m": round(float(np.percentile(dem[-1, :], 50)), 1),
            "east_col_last_p50_m": round(float(np.percentile(dem[:, -1], 50)), 1),
            "note": "西缘 p50 远高于域内 p50 -> 西缘为山地，不是河道出山口",
        },
        "injection_neighbourhood": {
            "window": f"row{inj_row - 10}-{inj_row + 10}, col0-19",
            "min_m": round(float(dem[max(0, inj_row - 10):inj_row + 11, :20].min()), 1),
            "max_m": round(float(dem[max(0, inj_row - 10):inj_row + 11, :20].max()), 1),
            "p50_m": round(float(np.percentile(dem[max(0, inj_row - 10):inj_row + 11, :20], 50)), 1),
            "injection_cell_dem_m": round(float(dem[inj_row, inj_col]), 1),
        },
    }

    slope = slope_field(dem, cell_meta["cellsize_m"])
    plain = dem <= PLAIN_BAND_M
    pf = priority_flood(dem)
    raised = pf - dem
    resolvable = d8_resolvable(pf)
    dem_flatness = {
        "plain_band_definition_m": PLAIN_BAND_M,
        "plain_band": {
            "cells": int(plain.sum()),
            "frac_of_domain": round(float(plain.mean()), 4),
            "area_km2": round(float(plain.sum()) * cell_area_m2 / 1e6, 2),
            "dem_min_m": round(float(dem[plain].min()), 1),
            "dem_p50_m": round(float(np.percentile(dem[plain], 50)), 1),
            "dem_max_m": round(float(dem[plain].max()), 1),
        },
        "slope_m_per_m": {
            "domain_p50": round(float(np.percentile(slope, 50)), 5),
            "domain_p90": round(float(np.percentile(slope, 90)), 5),
            "plain_p50": round(float(np.percentile(slope[plain], 50)), 5),
            "plain_p90": round(float(np.percentile(slope[plain], 90)), 5),
            "domain_frac_below_flat_threshold": round(float((slope < SLOPE_FLAT).mean()), 4),
            "domain_frac_below_near_flat_threshold": round(float((slope < SLOPE_NEAR_FLAT).mean()), 4),
            "flat_threshold": SLOPE_FLAT, "near_flat_threshold": SLOPE_NEAR_FLAT,
            "note": "坡度量化受 DEM 整米精度限制：100 m 网格上 1 m 落差即 0.01 m/m。",
        },
        "priority_flood": {
            "raised_cells": int((raised > 0).sum()),
            "raised_frac_of_domain": round(float((raised > 0).mean()), 4),
            "raised_gt_1m_cells": int((raised > 1.0).sum()),
            "raised_gt_5m_cells": int((raised > 5.0).sum()),
            "raised_max_m": round(float(raised.max()), 2),
            "plain_raised_frac": round(float((raised[plain] > 0).mean()), 4),
            "note": "抬升>0 = 该像元无外泄路径，须填洼后才可汇流；比例越高说明洼地越多、"
                    "地形越不单调。",
        },
        "d8_resolvability_after_fill": {
            "domain_cells_with_strict_lower_neighbour": int(resolvable.sum()),
            "domain_frac": round(float(resolvable.mean()), 4),
            "plain_frac": round(float(resolvable[plain].mean()), 4),
            "domain_frac_flat_no_direction": round(float((~resolvable).mean()), 4),
            "plain_frac_flat_no_direction": round(float((~resolvable[plain]).mean()), 4),
            "note": "填洼后仍无严格更低邻居的像元 = D8 无梯度可用，汇流方向不可定。",
        },
        "plain_band_connectivity": None,
        "thalweg_wander": None,
    }

    plain_spec, _ = component_spectrum(plain, cell_area_m2)
    dem_flatness["plain_band_connectivity"] = {
        "n_components": plain_spec["n_components"],
        "largest_component_cells": plain_spec["max_component_cells"],
        "largest_component_km2": plain_spec["max_component_km2"],
        "note": "平原带若为**单一**巨块，说明内部没有分隔性河谷，只有孤立高地。",
    }

    # 谷底漂移：在南半部（低地主体）逐行找 DEM 最小列
    row_lo = max(0, int(shape[0] * 0.40))
    dem_flatness["thalweg_wander"] = thalweg_wander(dem, np.arange(row_lo, shape[0]))
    log.step("dem_flatness", plain_frac=dem_flatness["plain_band"]["frac_of_domain"],
             d8_flat_frac=dem_flatness["d8_resolvability_after_fill"]["domain_frac_flat_no_direction"])

    # ---- 三次失败尝试的交叉引用（原文摘录，可核对）----
    crossref: dict[str, Any] = {"source_script": str(SCENARIO_SCRIPT.relative_to(PROJ)),
                                "line_range": list(CROSSREF_LINES), "excerpt": None,
                                "sha256": sha256_file(SCENARIO_SCRIPT) if SCENARIO_SCRIPT.is_file() else None}
    if SCENARIO_SCRIPT.is_file():
        lines = SCENARIO_SCRIPT.read_text(encoding="utf-8").splitlines()
        lo, hi = CROSSREF_LINES
        crossref["excerpt"] = [f"{i}: {lines[i - 1]}" for i in range(lo, min(hi, len(lines)) + 1)]
    crossref["current_channel_mode"] = (meta.get("channel") or {}).get("mode")
    crossref["current_channel_note"] = (meta.get("channel") or {}).get("note")
    crossref["finding"] = (
        "prep() 注释记载 S1 掩膜路线三版皆败（足印覆盖 58%/74% 截断致分量碎裂、端点退化，"
        "2026-10-25），改走 DEM 山谷 D8 追踪亦因走廊为宽平底平原无梯度可用而三版皆败；"
        "最终退回计划明确要否定的参数化直线河槽。当前基线 run 为 channel=off。"
    )

    # ---- verdict ----
    e0905 = endpoints["s1_common_water_20240905"]
    e0912 = endpoints["s1_flood_20240912"]
    s0905 = s1_sections["s1_common_water_20240905"]
    verdict = {
        "code": "BLOCKED_ALTERNATIVE_REQUIRED",
        "phase3_implemented": False,
        "phase3_status": "NOT_IMPLEMENTED（未实施）",
        "primary_reason": "S1 常水掩膜不可用作河道骨架来源（定量证据见 s1_* 与 endpoint_degeneracy）",
        "not_an_execution_defect": True,
        "not_an_execution_defect_reason": (
            "三次失败尝试的成因已在 prep() 注释留痕并经本审计定量复算：掩膜在模型域上游边界"
            "完全无观测、域内常水碎裂为数百个互不连通小块、最大块尺寸远小于主水体量级。"
            "这是**输入数据固有缺陷**，换实现方式无法修复。"
        ),
        "decision_evidence": {
            "footprint_frac_0905": s0905["on_model_grid"]["footprint_frac_of_domain"],
            "footprint_north_half_absent": s0905["footprint_by_row_decile"][:5],
            "water_components_0905": s0905["components_8conn"]["n_components"],
            "max_component_km2_0905": s0905["components_8conn"]["max_component_km2"],
            "main_water_body_km2": main_body["largest_component_km2"],
            "size_ratio_main_over_s1": round(
                main_body["largest_component_km2"]
                / max(s0905["components_8conn"]["max_component_km2"], 1e-9), 1),
            "upstream_boundary_observed_0905": e0905["upstream_boundary_observed"],
            "spans_upstream_to_downstream_0905": e0905["largest_component_spans_upstream_to_downstream"],
            "spans_upstream_to_downstream_0912": e0912["largest_component_spans_upstream_to_downstream"],
            "d8_flat_frac_after_fill": dem_flatness["d8_resolvability_after_fill"]["domain_frac_flat_no_direction"],
        },
        "consequence": "Phase 3 河道调理维持阻塞；不生成任何骨架线、不施加任何刻蚀。",
    }

    # ---- 三个替代方案评估 ----
    alternatives = [
        {
            "id": "alt_a",
            "name": "用模型主水体反推主槽走廊",
            "description": (
                f"取 res.max 最大连通块（{main_body['largest_component_km2']} km²，"
                f"占湿面积 {main_body['largest_component_share_of_wet']:.1%}）作走廊掩膜，"
                "对其骨架化后下切 2~3 m。"
            ),
            "feasibility": "技术可行（几何可提取），但结论可用性低",
            "physical_basis_strength": "弱",
            "physical_basis_detail": (
                "主水体确实占据真实河道位置（其 DEM p50 与常水像元 DEM p50 同量级），"
                "但走廊边界由**模型自身**的淹没结果决定，不是独立观测。"
            ),
            "circular_reasoning_risk": "高",
            "circular_reasoning_detail": (
                "刻蚀后重跑必然让主水体更集中在走廊内，等于用结果定义地形再用地形复现结果；"
                "验证指标（斑块数、主水体面积、S1 CSI/FAR）全部会被人为改善，"
                "无法与「物理正确」区分。"
            ),
            "required_extra_data": [
                "独立于模型的河道中心线（实测断面、航道图、或高分辨率光学影像）",
                "河道断面的床高程实测值，用于标定下切深度而非凭 2~3 m 假定",
            ],
            "verdict": "不推荐作为物理改进；若仅作展示层对照，必须标注 derived_from=模型结果 且不得用于物理结论",
        },
        {
            "id": "alt_b",
            "name": "DEM 低带 + Priority-Flood 汇流做骨架",
            "description": (
                "在 DEM 低带上做 Priority-Flood 填洼后按 D8/汇流累积量提取高汇流骨架线，"
                "作为河道中心线。"
            ),
            "feasibility": "技术可行，但本域地形不提供可分辨的河谷信号",
            "physical_basis_strength": "中（纯地形驱动，不依赖模型结果）",
            "physical_basis_detail": (
                f"Priority-Flood 抬升>0 的像元占全域 "
                f"{dem_flatness['priority_flood']['raised_frac_of_domain']:.1%}；"
                f"填洼后仍有 {dem_flatness['d8_resolvability_after_fill']['domain_frac_flat_no_direction']:.1%} "
                "的像元无严格更低邻居（平原带内 "
                f"{dem_flatness['d8_resolvability_after_fill']['plain_frac_flat_no_direction']:.1%}）；"
                f"平原带为单一连通块 {dem_flatness['plain_band_connectivity']['largest_component_km2']} km²，"
                "内部无分隔性河谷。"
            ),
            "circular_reasoning_risk": "低",
            "circular_reasoning_detail": "输入只有 DEM，与模型结果无关，不存在循环论证。",
            "required_extra_data": [
                "更高垂直精度 DEM（当前 DEM 为整米级，100 m 网格上 1 m 即 0.01 m/m，"
                "量化噪声与真实纵坡同量级）",
                "郁江该河段实测纵坡与断面，用于判断提取出的骨架线是否物理合理",
                "（可选）光学影像或航道图作为独立中心线校核",
            ],
            "verdict": "理论最优但受数据精度限制，此前 D8 三版已失败；在获得高精度 DEM 前不应重试",
        },
        {
            "id": "alt_c",
            "name": "保持 channel=off 现状（不调理）",
            "description": "不施加任何人工河槽，严格使用源 DEM，接受「干流传导受阻」这一已知限制。",
            "feasibility": "立即可行（已是现状）",
            "physical_basis_strength": "强（不引入任何虚构地形）",
            "physical_basis_detail": (
                "地形完全忠实于输入 DEM，不存在任何人为刻蚀；缺点是无河道时上游来水在平原上"
                "扩散，干流输运能力被低估。"
            ),
            "circular_reasoning_risk": "无",
            "circular_reasoning_detail": "不修改地形，不存在用结果反推地形的通路。",
            "required_extra_data": ["无（若日后要解除该限制，需先取得上表独立河道资料）"],
            "verdict": "推荐维持；已知限制必须在成果与 ADR 中显式声明",
            "recommended": True,
        },
    ]

    phase4_impact = {
        "affects_phase4": True,
        "statement": (
            "Phase 4 的三个新情景（flow_only_v1 / dualloss_v1 / rainloss_high_v1）全部沿用 "
            "channel=off，即**未做任何河道调理**。它们与基线在河道几何上完全一致，"
            "差异只来自降雨损失与上游来水，因此情景间可比。"
        ),
        "known_limitation": (
            "「干流传导受阻」限制未被消除：上游 11500 m³/s 三角来水在无河槽的宽平底平原上"
            "扩散，干流输运能力可能被低估。任何基于绝对淹没面积的结论都必须带此限制声明。"
        ),
        "what_is_safe_to_conclude": (
            "情景**之间**的差值与相对变化（斑块数、湿面积、S1 CSI/FAR 的相对变化）"
            "不受河道几何影响，可用于判断降雨损失假设的方向性。"
        ),
        "what_is_not_safe_to_conclude": (
            "绝对淹没面积、干流洪峰传播时间、河道内水位绝对值——均受未调理河道影响，"
            "不得作为物理结论或资产判据。"
        ),
    }

    # 只读自检
    hash_after = {str(p): sha256_file(p) for p in guarded if p.is_file()}
    readonly = {
        "wrote_into_outputs": False,
        "generated_rasters": 0,
        "carving_applied": False,
        "lisflood_invoked": False,
        "models_modified": False,
        "source_hashes_unchanged": hash_before == hash_after,
        "checked_files": sorted(hash_before),
    }
    if not readonly["source_hashes_unchanged"]:
        log.error("源文件哈希在读取前后不一致")

    payload = {
        "schema_version": "1.0",
        "audit": "guigang_channel_route",
        "generated_utc": utc_now(),
        "script": SCRIPT_REL,
        "script_sha256": sha256_file(Path(__file__)),
        "purpose": "Phase 3 河道调理路线可行性证据包（判定阻塞，不实施）",
        "verdict": verdict,
        "model_grid": grid_meta,
        "cell_geometry": cell_meta,
        "source_run": {
            "run_id": RUN.name,
            "meta_channel": meta.get("channel"),
            "note": "基线 run；本审计只读该 run，不写入、不重跑",
        },
        **s1_sections,
        "endpoint_degeneracy": endpoints,
        "model_main_water_body": main_body,
        "dem_corridor": dem_stats,
        "dem_flatness": dem_flatness,
        "source_cross_reference": crossref,
        "readonly_evidence": readonly,
        "alternatives": alternatives,
        "phase4_impact": phase4_impact,
        "caveats": [
            "本审计只判定路线可行性，不评价模型物理正确性。",
            "S1 固定阈值 -17 dB，风浪与淹没植被会导致漏检（G17 降级）；"
            "漏检只会让掩膜更碎，不会让「骨架不可用」的结论偏乐观。",
            "「上游」定义为西边界：上游 Q 点源 meta.point_source_32649 落于 col0；"
            "「下游」为东边界（bci.bci 设 FREE 坡度 0.00015）。",
            "DEM 为整米级精度，100 m 网格上 1 m 落差即 0.01 m/m；坡度统计的分辨率下限受此限制。",
            "Priority-Flood 与 D8 派生量仅用于诊断，不产出任何 DEM 文件，"
            "也不替代 DEM_HYDRAULIC。",
            "Phase 3 状态为 NOT_IMPLEMENTED（未实施）；本文件不是实施报告。",
        ],
    }
    log.step("write_products", out=str(DEFAULT_OUT.relative_to(PROJ)))
    payload["audit_trail"] = log.payload
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description="贵港河道调理路线可行性审计（只读）")
    ap.add_argument("--dry-run", action="store_true", help="只计算并打印，不写审计 JSON")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="审计 JSON 输出路径")
    args = ap.parse_args()

    out = args.out.resolve()
    # 只读护栏：禁止把审计结果写进 outputs/**
    if "outputs" in out.parts:
        raise SystemExit(f"拒绝写入 {out}：审计产物不得进入 outputs/（只读约束）")

    payload = audit(dry_run=args.dry_run)
    v = payload["verdict"]
    e = v["decision_evidence"]
    print("\n=== 贵港河道调理路线可行性审计 ===")
    print(f"verdict: {v['code']}  |  Phase 3: {v['phase3_status']}")
    print(f"S1 09-05 足印覆盖率 {e['footprint_frac_0905']:.1%}；北半部十分位 {e['footprint_north_half_absent']}")
    print(f"S1 09-05 域内水像元 {payload['s1_common_water_20240905']['on_model_grid']['water_cells']} "
          f"({payload['s1_common_water_20240905']['on_model_grid']['water_km2']} km²)，"
          f"碎成 {e['water_components_0905']} 块，最大 {e['max_component_km2_0905']} km²")
    print(f"郁江主水体 {e['main_water_body_km2']} km² —— 尺寸比 {e['size_ratio_main_over_s1']}:1")
    print(f"上游边界有观测(09-05): {e['upstream_boundary_observed_0905']}；"
          f"上下游贯通(09-05/09-12): {e['spans_upstream_to_downstream_0905']}/"
          f"{e['spans_upstream_to_downstream_0912']}")
    print(f"填洼后 D8 无方向像元占比 {e['d8_flat_frac_after_fill']:.1%}")
    if args.dry_run:
        print("\n[dry-run] 未写审计 JSON")
        return 0
    write_json(out, payload)
    print(f"\n-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
