"""Phase 0 / T0.1+T0.3：贵港全帧斑块时空统计 + 降雨账目 + A/B/C 四规则实测去除率（只读诊断）。

对应计划 `plans/贵港水动力推演效果改进实施计划_从零散水点治理到上游来水与降雨双驱动重构.md`：

- §3 Phase 0 任务 1「时空斑块基线固化」：57 帧（saveint=21600，T+0~336 h）连通域统计，
  逐帧斑块数、面积谱（≤4 格 / ≤9 格 / ≥1 km²）、水深与体积演变。
- §3 Phase 0 任务 3「降雨量与蓄水账目对账」：全域降雨体积、上游三角 Q 注入体积、
  雨水体积在离散小坑中的截留占比。
- §3 Phase 1 规则 1–4（`classify_guigang_patch` 伪代码）与 §1.2 表格的三档预注册参数：
  在 57 帧轨迹上**实测**三档去除率，与计划 §4 预测值（80.2% / 92.5% / 96.8%）并列比对。

口径硬约束（全部由脚本强制，不靠注释约定）：

1. 连通域 8 邻域（`scripts/common/patch_analysis.connected_components`）。
2. 湿格判定 `depth > 0.05 m`，**保持 float32 口径不升位**。栅格以 float32 存储，
   ``float32(0.05) == 0.05000000074505806``；升到 float64 后 ``> 0.05`` 会把恰好写成
   0.050 的像元判成湿，与 ``summarize_frame`` 的判定冲突，导致本文件与逐帧 CSV 自相矛盾。
3. 全部面积阈值以 **km²** 表达；像元数一律由栅格头 ``cellsize`` 经
   ``AsciiGrid.cells_for_area`` 现算，禁止写死 10000.0 / "4 像元" / "9 像元"。
4. 只读 ``outputs/guigang/**``；脚本前后各算一次源 run sha256 并比对，不一致即硬失败。
5. 不调用 ``lisflood.exe``，不修改 ``models/``。
6. 输出目录不得含 ``res.mass``（否则污染 ``g15_mass_recheck.py`` 的独立 run 计数）。
7. 所有 JSON 带 ``generated_utc``、脚本名、输入哈希；结论带证据等级 E1–E4。

本脚本**不做任何过滤输出**：A/B/C 三档只做诊断计数，不产生派生栅格。
阈值一律按计划原值使用，**未按结果调整**；达不到计划验收目标时如实登记。

用法（先设 ``$env:PYTHONUTF8="1"``）::

    uv --directory 2d_hydro_project run python scripts/stage6/analyze_guigang_patches.py --dry-run
    uv --directory 2d_hydro_project run python scripts/stage6/analyze_guigang_patches.py

产出（默认目录 ``diagnostics/guigang/``）：
    patch_persistence.csv     逐帧逐斑块统计（每行 = 一帧的一个斑块）
    patch_frame_summary.csv   逐帧汇总（斑块数、湿面积、体积、薄层像元数）
    patch_tracks.csv          跨帧轨迹（复用 PersistenceTracker）
    patch_summary.json        口径、参数、阈值换算、三档实测去除率、降雨账目、证据等级
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import (  # noqa: E402
    AuditLog,
    GridError,
    add_common_args,
    guard_no_overwrite,
    parse_run_par,
    read_ascii_grid,
    run_frames,
    sha256_file,
    utc_now,
    write_json,
)
from scripts.common.patch_analysis import (  # noqa: E402
    PersistenceTracker,
    summarize_frame,
)

# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
GG = PROJ / "outputs" / "guigang" / "2024-09-yagi"
DEFAULT_RUN_ID = "scen_yagi_q11500_100m_dt2p5_choff_realdem"
DEFAULT_OUT = PROJ / "diagnostics" / "guigang"
DEFAULT_LOG_DIR = PROJ / "logs" / "diagnostics"
ERA5 = PROJ / "data" / "catalog" / "probes" / "guigang_sep2024_era5_hourly.json"
MASS_G15 = PROJ / "data" / "catalog" / "mass_recheck_g15.json"
S1_PRE = PROJ / "data" / "processed" / "s1_water" / "guigang_20240905T105022_water20m.tif"
SCRIPT_NAME = "scripts/stage6/analyze_guigang_patches.py"

# --------------------------------------------------------------------------- #
# 口径常量
# --------------------------------------------------------------------------- #
#: 湿格判定：与 summarize_frame / verify.json / floodviz 同口径（严格大于，float32）。
WET_THRESHOLD_M = 0.05
#: 细薄水膜上限：输出格式量化到 0.001 m（output.cpp:686 ``%.3f``），故 (0, 0.005] 记薄层。
THIN_FILM_MAX_M = 0.005
#: 诊断面积分档（km²）。只用于给斑块打标签，不产生任何过滤。
AREA_BINS_KM2 = (0.04, 0.09, 0.25, 1.0, 4.0, 10.0)
#: 主水体判定：每帧按面积降序累计覆盖该帧湿面积 >= 该比例的斑块集合。
#: 与 ``clean_jiulong_display.MAIN_BODY_AREA_FRACTION`` 同一项目口径（E3 候选假设）。
MAIN_BODY_AREA_FRACTION = 0.5

# --------------------------------------------------------------------------- #
# 计划 §3 Phase 1 规则 1–4 的预注册参数（**照抄计划原值，禁止按结果调整**）
# --------------------------------------------------------------------------- #
#: 规则 1 主干水系保护：面积 >= 5.0 km² 绝对豁免（计划 §Phase1 伪代码注释）。
PROTECTED_MAIN_STEM_MIN_AREA_KM2 = 5.0
#: 规则 2 显著深水洼地保护：max_depth >= 0.50 m 豁免（计划伪代码注释，固定值，不随档变化）。
DEEP_DEPTH_THRESHOLD_M = 0.50
#: 规则 3/1 三档预注册参数（计划 §3 Phase 1 表格第 157–161 行原值）。
TIER_SPECS: dict[str, dict[str, Any]] = {
    "A": {
        "label": "A 档（保守）",
        "area_max_km2": 0.04,
        "depth_max_m": 0.15,
        "s1_buffer_m": 500.0,
        "plan_predicted_removal_pct": 80.2,
        "plan_predicted_volume_change_pct_lt": 0.5,
    },
    "B": {
        "label": "B 档（推荐平衡）",
        "area_max_km2": 0.09,
        "depth_max_m": 0.30,
        "s1_buffer_m": 300.0,
        "plan_predicted_removal_pct": 92.5,
        "plan_predicted_volume_change_pct_lt": 1.2,
    },
    "C": {
        "label": "C 档（激进展示）",
        "area_max_km2": 0.25,
        "depth_max_m": 0.50,
        "s1_buffer_m": 200.0,
        "plan_predicted_removal_pct": 96.8,
        "plan_predicted_volume_change_pct_lt": 2.5,
    },
}
#: 规则 4 时空持久性阈值（**计划未给数值**，本脚本处理前预注册）。
#: persistence_ratio = 轨迹出现帧数 / 全 run 有湿帧数；低于该比例判为瞬时噪点。
MIN_PERSISTENCE_RATIO = 0.25
#: 规则 4 敏感性网格：用于证明结论不依赖该预注册值（计划未给值，故必须给区间）。
MIN_PERSISTENCE_RATIO_SENSITIVITY = (0.10, 0.25, 0.50, 0.75, 0.90)

#: 上游三角 Q 情景线（run 的 yujiang.bdy 实际值，单位 m³/s）。用于解析积分对账。
UPSTREAM_Q_SERIES = (
    (0.0, 1000.0),
    (432000.0, 1000.0),
    (820800.0, 11500.0),
    (1166400.0, 2500.0),
    (1209600.0, 2000.0),
)

NB8 = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))

PATCH_COLUMNS = (
    "run_id", "cellsize_m", "cell_area_m2", "seq", "t_h", "patch_label", "track_id",
    "n_cells", "area_km2", "max_depth_m", "mean_depth_m", "p95_depth_m", "volume_m3",
    "row_min", "row_max", "col_min", "col_max", "bbox_area_km2", "rectangularity",
    "centroid_row", "centroid_col", "depth_band", "area_band", "is_main_body",
)
FRAME_COLUMNS = (
    "run_id", "cellsize_m", "cell_area_m2", "seq", "t_h", "n_patches", "wet_cells",
    "wet_area_km2", "wet_volume_m3", "max_depth_m", "p50_depth_m", "p90_depth_m",
    "p99_depth_m", "thin_film_cells", "single_cell_patches", "single_cell_area_km2",
    "largest_patch_area_km2", "main_body_patches", "main_body_area_km2",
    "patches_le_4_cells", "patches_le_9_cells", "patches_ge_1km2",
    "area_le_4_cells_km2", "area_le_9_cells_km2",
)
TRACK_COLUMNS = (
    "run_id", "cellsize_m", "track_id", "frame_count", "longest_consecutive_run",
    "first_frame", "last_frame", "max_area_km2", "mean_area_km2", "max_depth_m",
    "volume_peak_m3", "persistence_ratio", "is_main_body_track", "touches_s1_500m",
    "touches_s1_300m", "touches_s1_200m",
    "verdict_A", "reason_A", "verdict_B", "reason_B", "verdict_C", "reason_C",
)


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def depth_band(max_depth_m: float) -> str:
    """按最大水深给诊断分档（不用于过滤）。"""
    if max_depth_m <= THIN_FILM_MAX_M:
        return f"le_{THIN_FILM_MAX_M:g}m"
    if max_depth_m <= WET_THRESHOLD_M:
        return "0.005_0.05m"
    if max_depth_m <= 0.30:
        return "0.05_0.30m"
    if max_depth_m <= 1.0:
        return "0.30_1.00m"
    return "gt_1.00m"


def area_band(area_km2: float) -> str:
    """按物理面积给诊断分档；边界由 AREA_BINS_KM2 定义（km²，与像元数无关）。"""
    previous = 0.0
    for edge in AREA_BINS_KM2:
        if area_km2 < edge:
            return f"{previous:g}_{edge:g}km2"
        previous = edge
    return f"ge_{AREA_BINS_KM2[-1]:g}km2"


def distance_cells(mask: np.ndarray) -> np.ndarray:
    """到掩膜的 8 邻域 chamfer 距离（单位：格，1/√2 权重），掩膜内为 0。

    纯 numpy + Python 双遍扫描，无 scipy 依赖（环境只有 numpy + rasterio）。
    """
    height, width = mask.shape
    big = 1e9
    dist = np.where(np.asarray(mask, bool), 0.0, big)
    diag = math.sqrt(2.0)
    for r in range(height):
        row = dist[r]
        prev = dist[r - 1] if r > 0 else None
        for c in range(width):
            if row[c] == 0.0:
                continue
            best = row[c]
            if prev is not None:
                if prev[c] + 1.0 < best:
                    best = prev[c] + 1.0
                if c > 0 and prev[c - 1] + diag < best:
                    best = prev[c - 1] + diag
                if c + 1 < width and prev[c + 1] + diag < best:
                    best = prev[c + 1] + diag
            if c > 0 and row[c - 1] + 1.0 < best:
                best = row[c - 1] + 1.0
            row[c] = best
    for r in range(height - 1, -1, -1):
        row = dist[r]
        nxt = dist[r + 1] if r + 1 < height else None
        for c in range(width - 1, -1, -1):
            if row[c] == 0.0:
                continue
            best = row[c]
            if nxt is not None:
                if nxt[c] + 1.0 < best:
                    best = nxt[c] + 1.0
                if c > 0 and nxt[c - 1] + diag < best:
                    best = nxt[c - 1] + diag
                if c + 1 < width and nxt[c + 1] + diag < best:
                    best = nxt[c + 1] + diag
            if c + 1 < width and row[c + 1] + 1.0 < best:
                best = row[c + 1] + 1.0
            row[c] = best
    return dist


def load_s1_masks(header: dict[str, float], shape: tuple[int, int], *, res_m: float) -> dict[str, Any]:
    """把 S1 常水掩膜最近邻重采样到模型网格，并给出三档缓冲区掩膜。

    计划 §Phase1 规则 1 的「郁江主槽保护区」按 S1 常水外缓冲 500/300/200 m 定义。
    本函数同时返回**足印掩膜**：S1 幅宽只覆盖模型域的一部分，足印外无法判定保护区，
    必须在审计里显式登记，不能把「足印外」当成「非保护区」。
    """
    out: dict[str, Any] = {"available": False}
    if not S1_PRE.is_file():
        return out
    import rasterio
    from rasterio.warp import Resampling, reproject

    height, width = shape
    dst_transform = rasterio.Affine(
        res_m, 0, float(header["xllcorner"]), 0, -res_m,
        float(header["yllcorner"]) + height * res_m,
    )
    with rasterio.open(S1_PRE) as src:
        band = src.read(1)
        nodata = src.nodata
        dst = np.full((height, width), 255, dtype=np.uint8)
        reproject(
            source=band, destination=dst,
            src_transform=src.transform, src_crs=src.crs, src_nodata=nodata,
            dst_transform=dst_transform, dst_crs=src.crs,
            resampling=Resampling.nearest,
        )
        crs = str(src.crs)
    water = dst == 1
    footprint = dst != 255
    dist = distance_cells(water)
    buffers: dict[str, np.ndarray] = {}
    buffer_cells: dict[str, int] = {}
    for tier, spec in TIER_SPECS.items():
        n = max(1, int(math.ceil(float(spec["s1_buffer_m"]) / res_m)))
        buffers[tier] = dist <= n
        buffer_cells[tier] = n
    out = {
        "available": True,
        "source": str(S1_PRE.relative_to(PROJ)),
        "source_sha256": sha256_file(S1_PRE),
        "source_crs": crs,
        "resampling": "nearest",
        "water_cells": int(water.sum()),
        "footprint_cells": int(footprint.sum()),
        "footprint_fraction_of_domain": round(float(footprint.mean()), 4),
        "distance_to_water_cells_max": round(float(dist.max()), 2),
        "buffers": {tier: buffers[tier] for tier in TIER_SPECS},
        "buffer_cells_by_tier": buffer_cells,
        "water": water,
        "footprint": footprint,
        "distance": dist,
    }
    return out


def rain_accounting(run_dir: Path, domain_cells: int, cell_area_m2: float) -> dict[str, Any]:
    """降雨账目：rain.rain 逐时序列、全域降雨体积、上游三角 Q 注入体积（E1）。"""
    path = run_dir / "rain.rain"
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    declared = int(lines[1].split()[0])
    pts = [(float(ln.split()[1]), float(ln.split()[0])) for ln in lines[2:] if ln.strip()]
    total_mm = float(sum(v for _t, v in pts))
    nonzero = [v for _t, v in pts if v > 0]
    v_rain = domain_cells * cell_area_m2 * total_mm / 1000.0
    v_up = float(sum(
        0.5 * (a[1] + b[1]) * (b[0] - a[0]) for a, b in zip(UPSTREAM_Q_SERIES, UPSTREAM_Q_SERIES[1:])
    ))
    return {
        "rain_file": str(path.relative_to(PROJ)),
        "rain_file_sha256": sha256_file(path),
        "rain_series_points_declared": declared,
        "rain_series_points_actual": len(pts),
        "rain_series_points_match": declared == len(pts),
        "rain_total_mm": round(total_mm, 4),
        "rain_hours_with_rain": len(nonzero),
        "rain_max_hourly_mm": round(float(max(nonzero)), 3) if nonzero else 0.0,
        "rain_mean_hourly_mm": round(total_mm / max(len(pts), 1), 6),
        "domain_cells": int(domain_cells),
        "cell_area_m2": float(cell_area_m2),
        "domain_area_km2": round(domain_cells * cell_area_m2 / 1e6, 4),
        "v_rain_m3": round(v_rain, 1),
        "v_rain_formula": "domain_cells × cell_area_m2 × rain_total_mm / 1000",
        "upstream_q_series_m3s": [list(p) for p in UPSTREAM_Q_SERIES],
        "v_upstream_m3": round(v_up, 1),
        "v_upstream_formula": "Σ 0.5×(Q_i+Q_i+1)×Δt_i（三角情景线解析积分）",
        "rain_over_upstream_pct": round(v_rain / v_up * 100, 3) if v_up else None,
        "rain_share_of_total_inflow_pct": (
            round(v_rain / (v_rain + v_up) * 100, 3) if (v_rain + v_up) else None
        ),
        "rain_series": [{"t_s": t, "mm": v} for t, v in pts],
        "clock_basis": (
            "rain.rain 时刻列以模型 t=0 = 2024-09-03T00 为基准"
            "（scripts/stage5/guigang_scenario.py:196-200，T0_DAY=3）；"
            "ERA5 原始序列首个键为 2024-09-05T00，不得把 t=0 当成 09-05T00，"
            "否则累计雨量整体偏移 48 h"
        ),
        "evidence_level": "E1（脚本从 rain.rain 与 yujiang.bdy 复算）",
    }


def read_res_mass(run_dir: Path) -> dict[str, Any]:
    """读 res.mass 并复算 G15 口径 E_mass，供降雨账目交叉核对（E1）。"""
    path = run_dir / "results" / "res.mass"
    if not path.is_file():
        return {"available": False}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    keys = lines[0].split()
    rows = [dict(zip(keys, (float(x) for x in ln.split()))) for ln in lines[1:] if ln.split()]
    if not rows:
        return {"available": False}
    v_in = sum(0.5 * (a["Qin"] + b["Qin"]) * (b["Time"] - a["Time"]) for a, b in zip(rows, rows[1:]))
    v_out = sum(0.5 * (a["Qout"] + b["Qout"]) * (b["Time"] - a["Time"]) for a, b in zip(rows, rows[1:]))
    v0, v_end = rows[0]["Vol"], rows[-1]["Vol"]
    v_rain = rows[-1]["Rain-(Inf+Evap)"]
    residual = v_end - v0 - v_rain - (v_in - v_out)
    denom = max(v0 + v_rain + v_in, 1.0)
    return {
        "available": True,
        "res_mass_sha256": sha256_file(path),
        "rows": len(rows),
        "v0_m3": round(v0, 1),
        "v_end_m3": round(v_end, 1),
        "v_rain_m3": round(v_rain, 1),
        "v_in_m3": round(v_in, 1),
        "v_out_m3": round(v_out, 1),
        "residual_m3": round(residual, 1),
        "e_mass": float(f"{abs(residual) / denom:.4e}"),
        "gate_1e-3": "PASS" if abs(residual) / denom < 1e-3 else "FAIL",
        "note": (
            "v_end 是末帧蓄量，与 res.max 峰值包络体积不是同一口径，不得混用"
            "（res.max 是全程逐像元最大值，其体积大于任一单帧蓄量）"
        ),
    }


def hash_source_run(run_dir: Path, frames: list[tuple[int, Path]]) -> dict[str, Any]:
    """源 run 完整性快照：res.max / res.mass / 全部 res-*.wd / 控制文件的 sha256。"""
    res_dir = run_dir / "results"
    frame_hashes = {p.name: sha256_file(p) for _seq, p in frames}
    combined = hashlib.sha256()
    for name in sorted(frame_hashes):
        combined.update(f"{name}:{frame_hashes[name]}\n".encode("utf-8"))
    controls = {}
    for name in ("dem.asc", "run.par", "rain.rain", "bci.bci", "yujiang.bdy", "meta.json"):
        path = run_dir / name
        if path.is_file():
            controls[name] = sha256_file(path)
    res_max = res_dir / "res.max"
    res_mass = res_dir / "res.mass"
    return {
        "res_max_sha256": sha256_file(res_max) if res_max.is_file() else None,
        "res_mass_sha256": sha256_file(res_mass) if res_mass.is_file() else None,
        "frame_count": len(frame_hashes),
        "frame_list_sha256": combined.hexdigest(),
        "frames": frame_hashes,
        "control_files": controls,
    }


def compare_source_hashes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """比对前后哈希；任何差异都是硬失败（只读承诺的可执行形式）。"""
    keys = ("res_max_sha256", "res_mass_sha256", "frame_list_sha256", "frame_count")
    diffs = [k for k in keys if before.get(k) != after.get(k)]
    changed_frames = sorted(
        name for name, digest in before.get("frames", {}).items()
        if after.get("frames", {}).get(name) != digest
    )
    changed_controls = sorted(
        name for name, digest in before.get("control_files", {}).items()
        if after.get("control_files", {}).get(name) != digest
    )
    return {
        "unchanged": not diffs and not changed_frames and not changed_controls,
        "differing_keys": diffs,
        "changed_frames": changed_frames,
        "changed_control_files": changed_controls,
    }


# --------------------------------------------------------------------------- #
# 计划 §Phase1 规则 1–4：轨迹/斑块分类
# --------------------------------------------------------------------------- #
def classify_guigang(
    *,
    area_km2: float,
    max_depth_m: float,
    persistence_ratio: float,
    touches_s1_buffer: bool,
    tier: str,
    min_persistence_ratio: float | None = None,
) -> tuple[str, str]:
    """按计划 §Phase1 ``classify_guigang_patch`` 的**原始规则顺序**判定。

    顺序（不可交换；交换即改变结果）：

      R1 主干水系保护（绝对豁免）：面积 >= 5.0 km²，或接触郁江主槽缓冲区
      R2 显著深水洼地保护：max_depth >= 0.50 m
      R3 微型离散浅水斑块：面积 <= A_max **且** 水深 <= H_max
      R4 时空持久性：persistence_ratio < min_persistence_ratio

    返回 ``(verdict, reason_code)``，verdict ∈ {``keep``, ``filter``}。
    """
    spec = TIER_SPECS[tier]
    persist_thr = MIN_PERSISTENCE_RATIO if min_persistence_ratio is None else min_persistence_ratio
    # ---- R1 主干水系保护（绝对豁免） ----
    if area_km2 >= PROTECTED_MAIN_STEM_MIN_AREA_KM2:
        return "keep", "KEEP_MAIN_STEM"
    if touches_s1_buffer:
        return "keep", "KEEP_RIVER_CORRIDOR"
    # ---- R2 显著深水洼地保护 ----
    if max_depth_m >= DEEP_DEPTH_THRESHOLD_M:
        return "keep", "KEEP_DEEP_WATER"
    # ---- R3 微型离散浅水斑块 ----
    if area_km2 <= float(spec["area_max_km2"]) and max_depth_m <= float(spec["depth_max_m"]):
        return "filter", "FILTER_DISCRETE_PUDDLE"
    # ---- R4 时空持久性 ----
    if persistence_ratio < persist_thr:
        return "filter", "FILTER_TRANSIENT_NOISE"
    return "keep", "KEEP_NATURAL_WETLAND"


def _tier_removal(
    records: list[dict[str, Any]], tier: str, *, min_persistence_ratio: float | None = None
) -> dict[str, Any]:
    """对一组斑块记录统计单档去除率（同时给斑块数、面积、体积三个口径）。"""
    n = len(records)
    n_filter = 0
    area_all = 0.0
    area_filter = 0.0
    vol_all = 0.0
    vol_filter = 0.0
    reasons: dict[str, int] = {}
    for rec in records:
        verdict, reason = classify_guigang(
            area_km2=rec["area_km2"], max_depth_m=rec["max_depth_m"],
            persistence_ratio=rec["persistence_ratio"],
            touches_s1_buffer=rec["touches_s1_buffer"], tier=tier,
            min_persistence_ratio=min_persistence_ratio,
        )
        area_all += rec["area_km2"]
        vol_all += rec["volume_m3"]
        reasons[reason] = reasons.get(reason, 0) + 1
        if verdict == "filter":
            n_filter += 1
            area_filter += rec["area_km2"]
            vol_filter += rec["volume_m3"]
    return {
        "tier": tier,
        "label": TIER_SPECS[tier]["label"],
        "patch_count": n,
        "filtered_patch_count": n_filter,
        "removal_pct": round(n_filter / n * 100, 3) if n else 0.0,
        "area_km2_total": round(area_all, 4),
        "area_km2_filtered": round(area_filter, 4),
        "area_change_pct": round(area_filter / area_all * 100, 4) if area_all else 0.0,
        "volume_m3_total": round(vol_all, 1),
        "volume_m3_filtered": round(vol_filter, 1),
        "volume_change_pct": round(vol_filter / vol_all * 100, 4) if vol_all else 0.0,
        "reason_counts": dict(sorted(reasons.items())),
    }


# --------------------------------------------------------------------------- #
# 主分析
# --------------------------------------------------------------------------- #
def analyze_run(
    run_dir: Path,
    *,
    dry_run: bool,
    out_dir: Path,
    audit: AuditLog,
) -> dict[str, Any]:
    """全帧斑块统计 + 轨迹 + 三档实测去除率。返回汇总 dict（不写盘）。"""
    dem = read_ascii_grid(run_dir / "dem.asc")
    shape = dem.shape
    cell_area = dem.cell_area_m2
    res_m = dem.cell_size_m
    header = dict(dem.header)
    par = parse_run_par(run_dir)
    saveint = float(par.get("saveint") or 0.0)
    if saveint <= 0:
        raise GridError(f"{run_dir}/run.par 缺 saveint；帧时间口径无法确定")
    sim_time = float(par.get("sim_time") or 0.0)

    frames = run_frames(run_dir / "results")
    if not frames:
        raise GridError(f"{run_dir}/results 无 res-XXXX.wd 帧")
    for seq, path in frames:
        if seq * saveint > sim_time + 1e-6:
            raise GridError(f"{path} 的 t={seq * saveint}s 超出 sim_time={sim_time}s；帧口径不一致")

    s1 = load_s1_masks(header, shape, res_m=res_m)
    audit.step(
        "s1_masks",
        available=s1.get("available"),
        water_cells=s1.get("water_cells"),
        footprint_fraction=s1.get("footprint_fraction_of_domain"),
    )

    # ---- 阈值换算（唯一入口：cells_for_area）----
    threshold_conversion = {
        "cell_area_source": "栅格头 cellsize 平方（grid_io.AsciiGrid.cell_area_m2）",
        "cellsize_m": res_m,
        "cell_area_m2": cell_area,
        "area_km2_to_cells": {
            f"{a:g}": dem.cells_for_area(a)
            for a in (0.04, 0.09, 0.25, 1.0, 5.0) + AREA_BINS_KM2
        },
        "tiers": {
            tier: {
                "area_max_km2": float(spec["area_max_km2"]),
                "n_cells_area_max": dem.cells_for_area(float(spec["area_max_km2"])),
                "depth_max_m": float(spec["depth_max_m"]),
                "s1_buffer_m": float(spec["s1_buffer_m"]),
                "n_cells_s1_buffer": max(1, int(math.ceil(float(spec["s1_buffer_m"]) / res_m))),
                "plan_predicted_removal_pct": float(spec["plan_predicted_removal_pct"]),
            }
            for tier, spec in TIER_SPECS.items()
        },
        "protected_main_stem_min_area_km2": PROTECTED_MAIN_STEM_MIN_AREA_KM2,
        "n_cells_protected_main_stem": dem.cells_for_area(PROTECTED_MAIN_STEM_MIN_AREA_KM2),
        "deep_depth_threshold_m": DEEP_DEPTH_THRESHOLD_M,
        "min_persistence_ratio": MIN_PERSISTENCE_RATIO,
        "min_persistence_ratio_source": (
            "计划 §Phase1 规则 4 只给规则名与 min_persistence_ratio 变量，未给数值；"
            "本脚本处理前预注册 0.25，并在 min_persistence_ratio_sensitivity 给出区间结果"
        ),
        "hard_rule": "禁止写死 10000.0 / '4 像元' / '9 像元'；像元数一律经 cells_for_area 现算",
    }

    # ---- CSV 打开（dry-run 不写）----
    patch_fh = patch_csv = None
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        patch_fh = (out_dir / "patch_persistence.csv").open("w", encoding="utf-8", newline="")
        patch_csv = csv.DictWriter(patch_fh, fieldnames=list(PATCH_COLUMNS), extrasaction="ignore")
        patch_csv.writeheader()

    tracker = PersistenceTracker()
    frame_rows: list[dict[str, Any]] = []
    # 每帧的紧凑斑块表（label, n_cells, area, max_depth, volume, track_id, ...）
    frame_patch_tables: dict[int, dict[str, np.ndarray]] = {}
    cell_track: dict[int, int] = {}
    main_body_track_ids: set[int] = set()
    s1_touch_by_track: dict[int, dict[str, bool]] = {}
    n_patch_rows = 0

    for seq, path in frames:
        frame = read_ascii_grid(path)
        if frame.shape != shape:
            raise GridError(f"{path} 形状 {frame.shape} 与 DEM {shape} 不符")
        depth = frame.data  # float32，不升位
        summary = summarize_frame(
            depth, seq, wet_threshold_m=WET_THRESHOLD_M,
            cell_area_m2=cell_area, sea_mask=None, keep_patches=True,
        )
        tracker.update(summary, width=shape[1])
        owner = getattr(tracker, "_cell_owner", {})
        t_h = round(seq * saveint / 3600.0, 3)

        # 主水体（候选主汇流区）：面积降序累计覆盖 >= MAIN_BODY_AREA_FRACTION 湿面积
        main_labels: set[int] = set()
        if summary.patches and summary.wet_area_km2 > 0:
            acc = 0.0
            for patch in summary.patches:  # summarize_frame 已按面积降序
                main_labels.add(patch.label)
                acc += patch.area_km2
                if acc >= MAIN_BODY_AREA_FRACTION * summary.wet_area_km2:
                    break
        main_cells = 0
        main_area = 0.0
        for patch in summary.patches:
            if patch.label in main_labels:
                main_cells += patch.n_cells
                main_area += patch.area_km2

        finite = np.isfinite(depth)
        thin = int((finite & (depth > 0) & (depth <= np.float32(THIN_FILM_MAX_M))).sum())

        labels_arr = np.empty(len(summary.patches), dtype=np.int32)
        n_cells_arr = np.empty(len(summary.patches), dtype=np.int32)
        area_arr = np.empty(len(summary.patches), dtype=np.float64)
        depth_arr = np.empty(len(summary.patches), dtype=np.float64)
        vol_arr = np.empty(len(summary.patches), dtype=np.float64)
        track_arr = np.empty(len(summary.patches), dtype=np.int32)

        for idx, patch in enumerate(summary.patches):
            cells = patch.cell_indices
            track_id = owner.get(int(cells[0])) if cells.size else None
            if track_id is None:
                raise GridError(
                    f"帧 {seq} 斑块 {patch.label} 无轨迹归属：PersistenceTracker._cell_owner 契约不符"
                )
            track_arr[idx] = track_id
            labels_arr[idx] = patch.label
            n_cells_arr[idx] = patch.n_cells
            area_arr[idx] = patch.area_km2
            depth_arr[idx] = patch.max_depth_m
            vol_arr[idx] = patch.volume_m3
            for c in cells.tolist():
                cell_track[c] = track_id
            if patch.label in main_labels:
                main_body_track_ids.add(track_id)
            if s1.get("available"):
                rec = s1_touch_by_track.setdefault(
                    track_id, {"s1_500": False, "s1_300": False, "s1_200": False}
                )
                for tier, buf in s1["buffers"].items():
                    if not rec[f"s1_{int(TIER_SPECS[tier]['s1_buffer_m'])}"]:
                        if bool(buf.ravel()[cells].any()):
                            rec[f"s1_{int(TIER_SPECS[tier]['s1_buffer_m'])}"] = True

            if patch_csv is not None:
                bbox_cells = (patch.row_max - patch.row_min + 1) * (patch.col_max - patch.col_min + 1)
                patch_csv.writerow({
                    "run_id": run_dir.name,
                    "cellsize_m": res_m,
                    "cell_area_m2": cell_area,
                    "seq": seq,
                    "t_h": t_h,
                    "patch_label": patch.label,
                    "track_id": track_id,
                    "n_cells": patch.n_cells,
                    "area_km2": round(patch.area_km2, 6),
                    "max_depth_m": round(patch.max_depth_m, 4),
                    "mean_depth_m": round(patch.mean_depth_m, 4),
                    "p95_depth_m": round(patch.p95_depth_m, 4),
                    "volume_m3": round(patch.volume_m3, 1),
                    "row_min": patch.row_min,
                    "row_max": patch.row_max,
                    "col_min": patch.col_min,
                    "col_max": patch.col_max,
                    "bbox_area_km2": round(bbox_cells * cell_area / 1e6, 6),
                    "rectangularity": round(patch.n_cells / bbox_cells, 4),
                    "centroid_row": round(patch.centroid_row, 2),
                    "centroid_col": round(patch.centroid_col, 2),
                    "depth_band": depth_band(patch.max_depth_m),
                    "area_band": area_band(patch.area_km2),
                    "is_main_body": int(patch.label in main_labels),
                })
            n_patch_rows += 1

        frame_patch_tables[seq] = {
            "label": labels_arr, "n_cells": n_cells_arr, "area": area_arr,
            "depth": depth_arr, "volume": vol_arr, "track": track_arr,
            "is_main_body": np.isin(labels_arr, np.array(sorted(main_labels), dtype=np.int32)),
        }

        le4 = int((n_cells_arr <= 4).sum())
        le9 = int((n_cells_arr <= 9).sum())
        ge1 = int((area_arr >= 1.0).sum())
        frame_rows.append({
            "run_id": run_dir.name,
            "cellsize_m": res_m,
            "cell_area_m2": cell_area,
            "seq": seq,
            "t_h": t_h,
            "n_patches": summary.n_patches,
            "wet_cells": summary.wet_cells,
            "wet_area_km2": round(summary.wet_area_km2, 4),
            "wet_volume_m3": round(summary.wet_volume_m3, 1),
            "max_depth_m": round(summary.max_depth_m, 4),
            "p50_depth_m": summary.depth_stats.get("p50", 0.0),
            "p90_depth_m": summary.depth_stats.get("p90", 0.0),
            "p99_depth_m": summary.depth_stats.get("p99", 0.0),
            "thin_film_cells": thin,
            "single_cell_patches": int((n_cells_arr == 1).sum()),
            "single_cell_area_km2": round(float(area_arr[n_cells_arr == 1].sum()), 4),
            "largest_patch_area_km2": round(float(area_arr.max()), 4) if area_arr.size else 0.0,
            "main_body_patches": len(main_labels),
            "main_body_area_km2": round(main_area, 4),
            "patches_le_4_cells": le4,
            "patches_le_9_cells": le9,
            "patches_ge_1km2": ge1,
            "area_le_4_cells_km2": round(float(area_arr[n_cells_arr <= 4].sum()), 4),
            "area_le_9_cells_km2": round(float(area_arr[n_cells_arr <= 9].sum()), 4),
        })

    if patch_fh is not None:
        patch_fh.close()

    # ---- 轨迹汇总 ----
    n_wet_frames = sum(1 for r in frame_rows if r["wet_cells"] > 0)
    tracks = tracker.summaries()
    track_rows: list[dict[str, Any]] = []
    for track in tracks:
        tid = int(track["track_id"])
        persistence_ratio = track["frame_count"] / n_wet_frames if n_wet_frames else 0.0
        touch = s1_touch_by_track.get(tid, {"s1_500": False, "s1_300": False, "s1_200": False})
        row = {
            "run_id": run_dir.name,
            "cellsize_m": res_m,
            "track_id": tid,
            "frame_count": track["frame_count"],
            "longest_consecutive_run": track["longest_consecutive_run"],
            "first_frame": track["first_frame"],
            "last_frame": track["last_frame"],
            "max_area_km2": track["max_area_km2"],
            "mean_area_km2": track["mean_area_km2"],
            "max_depth_m": track["max_depth_m"],
            "volume_peak_m3": track["volume_peak_m3"],
            "persistence_ratio": round(persistence_ratio, 6),
            "is_main_body_track": int(tid in main_body_track_ids),
            "touches_s1_500m": int(touch["s1_500"]),
            "touches_s1_300m": int(touch["s1_300"]),
            "touches_s1_200m": int(touch["s1_200"]),
        }
        for tier in TIER_SPECS:
            verdict, reason = classify_guigang(
                area_km2=float(track["max_area_km2"]),
                max_depth_m=float(track["max_depth_m"]),
                persistence_ratio=persistence_ratio,
                touches_s1_buffer=bool(touch[f"s1_{int(TIER_SPECS[tier]['s1_buffer_m'])}"]),
                tier=tier,
            )
            row[f"verdict_{tier}"] = verdict
            row[f"reason_{tier}"] = reason
        track_rows.append(row)

    # ---- res.max 与峰值帧的斑块表 ----
    res_max_grid = read_ascii_grid(run_dir / "results" / "res.max")
    if res_max_grid.shape != shape:
        raise GridError(f"res.max 形状 {res_max_grid.shape} 与 DEM {shape} 不符")
    res_max_summary = summarize_frame(
        res_max_grid.data, -1, wet_threshold_m=WET_THRESHOLD_M,
        cell_area_m2=cell_area, sea_mask=None, keep_patches=True,
    )
    peak_frame = max(frame_rows, key=lambda r: r["wet_area_km2"])

    def _records_from_summary(summary: Any) -> list[dict[str, Any]]:
        """把一帧/一场的斑块转成规则判定所需的记录（轨迹属性按多数像元归属取）。"""
        recs: list[dict[str, Any]] = []
        for patch in summary.patches:
            cells = patch.cell_indices
            votes: dict[int, int] = {}
            for c in cells.tolist():
                tid = cell_track.get(c)
                if tid is not None:
                    votes[tid] = votes.get(tid, 0) + 1
            tid = max(votes, key=votes.get) if votes else None
            touch = s1_touch_by_track.get(tid, {"s1_500": False, "s1_300": False, "s1_200": False}) \
                if tid is not None else {"s1_500": False, "s1_300": False, "s1_200": False}
            frame_count = 0
            if tid is not None and tid in tracker.tracks:
                frame_count = tracker.tracks[tid].frame_count
            recs.append({
                "area_km2": patch.area_km2,
                "max_depth_m": patch.max_depth_m,
                "volume_m3": patch.volume_m3,
                "n_cells": patch.n_cells,
                "track_id": tid,
                "persistence_ratio": frame_count / n_wet_frames if n_wet_frames else 0.0,
                "touches_s1_buffer": bool(touch["s1_500"]),  # 逐档覆盖见下
                "touches_by_tier": {
                    tier: bool(touch[f"s1_{int(TIER_SPECS[tier]['s1_buffer_m'])}"])
                    for tier in TIER_SPECS
                },
            })
        return recs

    res_max_records = _records_from_summary(res_max_summary)
    peak_records = [
        {
            "area_km2": float(a), "max_depth_m": float(d), "volume_m3": float(v),
            "n_cells": int(n), "track_id": int(t),
            "persistence_ratio": (
                tracker.tracks[int(t)].frame_count / n_wet_frames
                if int(t) in tracker.tracks and n_wet_frames else 0.0
            ),
            "touches_by_tier": {
                tier: bool(s1_touch_by_track.get(int(t), {}).get(
                    f"s1_{int(TIER_SPECS[tier]['s1_buffer_m'])}", False))
                for tier in TIER_SPECS
            },
        }
        for a, d, v, n, t in zip(
            frame_patch_tables[peak_frame["seq"]]["area"],
            frame_patch_tables[peak_frame["seq"]]["depth"],
            frame_patch_tables[peak_frame["seq"]]["volume"],
            frame_patch_tables[peak_frame["seq"]]["n_cells"],
            frame_patch_tables[peak_frame["seq"]]["track"],
        )
    ]

    def _tier_eval(records: list[dict[str, Any]]) -> dict[str, Any]:
        """三档去除率；每档用各自 S1 缓冲区（计划 §Phase1 表格第 3 列）。"""
        out: dict[str, Any] = {}
        for tier in TIER_SPECS:
            enriched = [
                {**rec, "touches_s1_buffer": rec["touches_by_tier"][tier]}
                for rec in records
            ]
            out[tier] = _tier_removal(enriched, tier)
        return out

    # 三档实测（多个口径，全部并列登记）
    res_max_tiers = _tier_eval(res_max_records)
    peak_tiers = _tier_eval(peak_records)
    track_tiers = {
        tier: _tier_removal(
            [
                {
                    "area_km2": float(t["max_area_km2"]),
                    "max_depth_m": float(t["max_depth_m"]),
                    "volume_m3": float(t["volume_peak_m3"]),
                    "persistence_ratio": float(t["persistence_ratio"]),
                    "touches_s1_buffer": bool(t[f"touches_s1_{int(TIER_SPECS[tier]['s1_buffer_m'])}m"]),
                }
                for t in track_rows
            ],
            tier,
        )
        for tier in TIER_SPECS
    }

    # 逐帧去除率（用轨迹判定回写像元；按斑块数加权）
    per_frame_tiers: dict[str, list[dict[str, Any]]] = {tier: [] for tier in TIER_SPECS}
    for frow in frame_rows:
        seq = frow["seq"]
        table = frame_patch_tables[seq]
        recs = [
            {
                "area_km2": float(table["area"][i]),
                "max_depth_m": float(table["depth"][i]),
                "volume_m3": float(table["volume"][i]),
                "persistence_ratio": (
                    tracker.tracks[int(table["track"][i])].frame_count / n_wet_frames
                    if int(table["track"][i]) in tracker.tracks and n_wet_frames else 0.0
                ),
                "touches_by_tier": {
                    tier: bool(s1_touch_by_track.get(int(table["track"][i]), {}).get(
                        f"s1_{int(TIER_SPECS[tier]['s1_buffer_m'])}", False))
                    for tier in TIER_SPECS
                },
            }
            for i in range(table["area"].size)
        ]
        for tier in TIER_SPECS:
            enriched = [{**r, "touches_s1_buffer": r["touches_by_tier"][tier]} for r in recs]
            res = _tier_removal(enriched, tier)
            per_frame_tiers[tier].append({
                "seq": seq, "t_h": frow["t_h"], "patch_count": res["patch_count"],
                "removal_pct": res["removal_pct"], "area_change_pct": res["area_change_pct"],
                "volume_change_pct": res["volume_change_pct"],
            })

    # 规则 4 敏感性：min_persistence_ratio 取不同值时的 res.max 去除率
    persistence_sensitivity = {
        f"{thr:g}": {
            tier: _tier_removal(
                [
                    {**rec, "touches_s1_buffer": rec["touches_by_tier"][tier]}
                    for rec in res_max_records
                ],
                tier, min_persistence_ratio=thr,
            )["removal_pct"]
            for tier in TIER_SPECS
        }
        for thr in MIN_PERSISTENCE_RATIO_SENSITIVITY
    }

    # 结构上界：规则 2（深度 >= 0.50 m 豁免）先于规则 3/4 生效，
    # 因此任何档位能过滤的斑块数不可能超过「max_depth < 0.50 m」的斑块数。
    n_deep = sum(1 for r in res_max_records if r["max_depth_m"] >= DEEP_DEPTH_THRESHOLD_M)
    structural_ceiling = {
        "deep_depth_threshold_m": DEEP_DEPTH_THRESHOLD_M,
        "res_max_patch_count": len(res_max_records),
        "patches_with_max_depth_ge_threshold": n_deep,
        "patches_with_max_depth_lt_threshold": len(res_max_records) - n_deep,
        "max_possible_removal_pct_any_tier": (
            round((len(res_max_records) - n_deep) / len(res_max_records) * 100, 3)
            if res_max_records else 0.0
        ),
        "explanation": (
            "计划 §Phase1 的规则顺序把 R2（max_depth >= 0.50 m 显著深水洼地保护）排在 "
            "R3（面积+水深）与 R4（时空持久性）之前，且 R2 的阈值是固定 0.50 m、不随档位变化。"
            "因此 max_depth >= 0.50 m 的斑块对任何档位都绝对豁免，任何档位的去除率上界"
            "= 1 − P(max_depth >= 0.50 m)。这是规则结构决定的上界，与阈值怎么选无关。"
        ),
        "evidence_level": "E1（脚本按计划原规则顺序逐斑块复算）",
    }

    # 面积+水深合取单独口径（去掉 R1/R2/R4，只看 R3）——用于定位差距来源
    def _r3_only(records: list[dict[str, Any]], tier: str) -> dict[str, Any]:
        spec = TIER_SPECS[tier]
        n = len(records)
        hit = [
            r for r in records
            if r["area_km2"] <= float(spec["area_max_km2"])
            and r["max_depth_m"] <= float(spec["depth_max_m"])
        ]
        area_all = sum(r["area_km2"] for r in records)
        vol_all = sum(r["volume_m3"] for r in records)
        return {
            "patch_count": n,
            "filtered_patch_count": len(hit),
            "removal_pct": round(len(hit) / n * 100, 3) if n else 0.0,
            "area_change_pct": round(sum(r["area_km2"] for r in hit) / area_all * 100, 4) if area_all else 0.0,
            "volume_change_pct": round(sum(r["volume_m3"] for r in hit) / vol_all * 100, 4) if vol_all else 0.0,
            "note": "仅 R3「面积+水深」合取，不含 R1 主干保护 / R2 深水豁免 / R4 持久性",
        }

    r3_only = {tier: _r3_only(res_max_records, tier) for tier in TIER_SPECS}

    # ---- 计划预测值的来源分解（证明计划数字 = 纯面积谱累计占比）----
    # 计划 §3 表格的「预期去除斑块比例」80.2 / 92.5 / 96.8 与本 run 的面积谱累计占比
    # 高度重合，说明计划是按「所有小斑块都被去除」的假设外推的，没有计入
    # 规则 2（max_depth >= 0.50 m 深水豁免）与规则 1（主干保护）的拦截。
    # 本块只做口径对齐诊断，不改变任何阈值。
    area_spectrum_cumulative = {}
    for tier, spec in TIER_SPECS.items():
        amax = float(spec["area_max_km2"])
        hit = [r for r in res_max_records if r["area_km2"] <= amax]
        area_spectrum_cumulative[tier] = {
            "area_max_km2": amax,
            "n_cells_area_max": dem.cells_for_area(amax),
            "patches_area_le_amax": len(hit),
            "cumulative_pct_of_patches": round(len(hit) / len(res_max_records) * 100, 3)
            if res_max_records else 0.0,
            "plan_predicted_removal_pct": float(spec["plan_predicted_removal_pct"]),
            "matches_area_spectrum_within_1pp": bool(
                abs(len(hit) / len(res_max_records) * 100 - float(spec["plan_predicted_removal_pct"])) < 1.0
            ) if res_max_records else False,
        }
    # 精确复现计划三档预测值所对应的像元数（只用于说明来源，不用于选阈值）
    plan_prediction_provenance = {
        "explanation": (
            "计划 §3 Phase 1 表格的「预期去除斑块比例」与「面积 <= A_max 的斑块累计占比」"
            "几乎逐位相等（见 area_spectrum_cumulative），说明该预测是在「面积 <= A_max 的小斑块"
            "全部被去除」的假设下算出的，等价于只用面积一条规则。计划伪代码里排在面积规则"
            "之前的规则 1（主干保护）与规则 2（max_depth >= 0.50 m 深水豁免）会拦截其中"
            "相当一部分斑块，因此实际去除率必然低于该预测值。"
        ),
        "evidence_level": "E1（面积谱累计占比由 res.max 逐斑块复算）",
        "not_a_threshold_change": (
            "本块只做口径对齐，不改变、不建议任何阈值；三档参数仍按计划原值使用"
        ),
    }

    # ---- 诊断变体：只按面积（无深度/无豁免）与「仅去掉 R2 深水豁免」----
    # 这两个变体是**诊断对照**，不是候选发布参数；用来定位差距来自哪一条规则。
    diagnostic_variants: dict[str, Any] = {}
    for tier, spec in TIER_SPECS.items():
        amax = float(spec["area_max_km2"])
        hmax = float(spec["depth_max_m"])
        n = len(res_max_records)
        # V1：只用面积（= 计划预测值的假设）
        v1 = [r for r in res_max_records if r["area_km2"] <= amax]
        # V2：面积+水深合取，但去掉 R2 的 0.50 m 深水豁免（= 计划三档参数直接生效）
        v2 = [r for r in res_max_records if r["area_km2"] <= amax and r["max_depth_m"] <= hmax]
        # V3：完整 4 条规则（正式口径）
        v3_count = res_max_tiers[tier]["filtered_patch_count"]
        diagnostic_variants[tier] = {
            "area_max_km2": amax,
            "depth_max_m": hmax,
            "v1_area_only_removal_pct": round(len(v1) / n * 100, 3) if n else 0.0,
            "v2_area_and_depth_no_deep_exemption_pct": round(len(v2) / n * 100, 3) if n else 0.0,
            "v3_full_four_rules_removal_pct": round(v3_count / n * 100, 3) if n else 0.0,
            "note": (
                "V1/V2 是诊断对照，**不是候选发布参数**。V1 复现计划预测值的来源；"
                "V2 显示若让三档水深上限直接生效（绕过 R2）能去掉多少；"
                "V3 是按计划原规则顺序的正式实测值。三者差距定位了差距来源。"
            ),
        }

    # ---- 深度与体积账目（回答「离散斑块是不是浅水」）----
    depths = np.array([r["max_depth_m"] for r in res_max_records], dtype=np.float64)
    ncells = np.array([r["n_cells"] for r in res_max_records], dtype=np.int64)
    areas = np.array([r["area_km2"] for r in res_max_records], dtype=np.float64)
    vols = np.array([r["volume_m3"] for r in res_max_records], dtype=np.float64)

    def _depth_quantiles(mask: np.ndarray) -> dict[str, Any]:
        vals = depths[mask]
        if vals.size == 0:
            return {"n": 0}
        return {
            "n": int(vals.size),
            "p10": round(float(np.percentile(vals, 10)), 4),
            "p25": round(float(np.percentile(vals, 25)), 4),
            "p50": round(float(np.percentile(vals, 50)), 4),
            "p75": round(float(np.percentile(vals, 75)), 4),
            "p90": round(float(np.percentile(vals, 90)), 4),
            "p99": round(float(np.percentile(vals, 99)), 4),
            "max": round(float(vals.max()), 4),
            "mean": round(float(vals.mean()), 4),
        }

    discrete_depth_profile = {
        "note": (
            "对 res.max 口径按斑块像元数分组的 max_depth 分位。用于检验计划 §1.2 把离散斑块"
            "描述为「浅水坑洼 / 5 cm 地表糙度」是否成立。"
        ),
        "le_1_cell": _depth_quantiles(ncells <= 1),
        "le_4_cells": _depth_quantiles(ncells <= 4),
        "le_9_cells": _depth_quantiles(ncells <= 9),
        "le_25_cells": _depth_quantiles(ncells <= 25),
        "gt_9_cells": _depth_quantiles(ncells > 9),
        "all": _depth_quantiles(np.ones(depths.size, dtype=bool)),
        "count_le_9_cells_depth_lt_0p15": int(((ncells <= 9) & (depths < 0.15)).sum()),
        "count_le_9_cells_depth_lt_0p30": int(((ncells <= 9) & (depths < 0.30)).sum()),
        "count_le_9_cells_depth_lt_0p50": int(((ncells <= 9) & (depths < 0.50)).sum()),
        "count_le_9_cells_total": int((ncells <= 9).sum()),
        "evidence_level": "E1（脚本从 res.max 逐斑块复算）",
    }

    main_idx = int(np.argmax(areas)) if areas.size else -1
    volume_budget = {
        "res_max_wet_volume_m3": round(float(vols.sum()), 1),
        "largest_patch_area_km2": round(float(areas[main_idx]), 4) if main_idx >= 0 else 0.0,
        "largest_patch_volume_m3": round(float(vols[main_idx]), 1) if main_idx >= 0 else 0.0,
        "largest_patch_share_of_wet_area_pct": (
            round(float(areas[main_idx]) / float(areas.sum()) * 100, 3) if areas.sum() else 0.0
        ),
        "largest_patch_share_of_volume_pct": (
            round(float(vols[main_idx]) / float(vols.sum()) * 100, 3) if vols.sum() else 0.0
        ),
        "by_patch_size": {
            f"le_{lim}_cells": {
                "patch_count": int((ncells <= lim).sum()),
                "area_km2": round(float(areas[ncells <= lim].sum()), 4),
                "volume_m3": round(float(vols[ncells <= lim].sum()), 1),
                "volume_share_of_res_max_pct": round(
                    float(vols[ncells <= lim].sum()) / float(vols.sum()) * 100, 4
                ) if vols.sum() else 0.0,
            }
            for lim in (1, 4, 9, 25, 100)
        },
        "note": "res.max 是全程逐像元最大值包络，其体积不等于末帧蓄量（见 mass_accounting.v_end_m3）",
    }

    # 插值到降雨体积的截留占比
    rain = rain_accounting(run_dir, int(dem.data.size), cell_area)
    v_rain = rain["v_rain_m3"]
    rain_interception = {
        "v_rain_m3": v_rain,
        "res_max_wet_volume_m3": volume_budget["res_max_wet_volume_m3"],
        "interception_by_patch_size": {
            f"le_{lim}_cells": {
                "volume_m3": volume_budget["by_patch_size"][f"le_{lim}_cells"]["volume_m3"],
                "pct_of_rain_volume": round(
                    volume_budget["by_patch_size"][f"le_{lim}_cells"]["volume_m3"] / v_rain * 100, 3
                ) if v_rain else None,
                "pct_of_res_max_volume": volume_budget["by_patch_size"][f"le_{lim}_cells"][
                    "volume_share_of_res_max_pct"
                ],
            }
            for lim in (1, 4, 9, 25, 100)
        },
        "note": (
            "「截留占比」= 该尺寸组斑块在 res.max 包络中的蓄水体积 / 全域降雨体积。"
            "分子是包络峰值口径、分母是全程累计入流口径，两者不是同一时间断面，"
            "故比值只能读作量级对照，不能读作水量守恒项。"
        ),
        "evidence_level": "E2（分子分母口径不同，只能给量级；严格闭合账目见 mass_accounting）",
    }

    # 时序：斑块数暴增与降雨/Q 的同步性
    wet_frame_rows = [r for r in frame_rows if r["wet_cells"] > 0]
    first_wet = wet_frame_rows[0]["t_h"] if wet_frame_rows else None
    # 「暴增帧」= 相邻帧斑块数**增幅最大**的那一帧（不是斑块数最多的帧）。
    # 两者不同：本 run 斑块数峰值在 seq16（7558），但最大增幅在 seq13→seq14。
    burst_pair = max(
        zip(frame_rows, frame_rows[1:]),
        key=lambda pair: pair[1]["n_patches"] - pair[0]["n_patches"],
        default=(None, None),
    )
    burst_prev, burst = burst_pair
    peak_patch_frame = max(frame_rows, key=lambda r: r["n_patches"])
    rain_series = {
        "note": (
            "计划 §1.2 根因一的时序判据：斑块数暴增时刻上游 Q 是否仍为基流。"
            "本脚本只登记事实，因果归因见 root_cause_report.md 并标证据等级。"
            "**模型时钟基准 t=0 对应 2024-09-03T00（meta.json t0 / guigang_scenario.T0_DAY=3）**；"
            "rain.rain 的时刻列按同一基准生成，故此处 t_h 与 rain.rain 逐时序列可直接对齐。"
        ),
        "first_wet_frame_t_h": first_wet,
        "peak_patch_count_frame": {
            "seq": peak_patch_frame["seq"], "t_h": peak_patch_frame["t_h"],
            "n_patches": peak_patch_frame["n_patches"],
            "wet_area_km2": peak_patch_frame["wet_area_km2"],
        },
        "burst_frame": {
            "definition": "相邻帧斑块数增幅最大的那一帧（不是斑块数峰值帧）",
            "seq": burst["seq"], "t_h": burst["t_h"],
            "n_patches": burst["n_patches"], "wet_area_km2": burst["wet_area_km2"],
            "patches_le_4_cells": burst["patches_le_4_cells"],
            "patches_le_9_cells": burst["patches_le_9_cells"],
            "previous_seq": burst_prev["seq"] if burst_prev else None,
            "previous_t_h": burst_prev["t_h"] if burst_prev else None,
            "previous_n_patches": burst_prev["n_patches"] if burst_prev else None,
        },
        "clock_basis": {
            "t0": "2024-09-03T00 Asia/Shanghai（前 2 日基流充槽）",
            "t0_source": "meta.json 的 t0 字段 + scripts/stage5/guigang_scenario.py T0_DAY=3",
            "trap": (
                "ERA5 原始序列首个键是 2024-09-05T00。若误把 t=0 当成 09-05T00"
                "（而非 09-03T00），全部累计雨量会整体偏移 48 h；"
                "若误把 t=0 当成 09-04T00，则偏移 24 h。本文件已按 09-03T00 对齐。"
            ),
            "lead_reported_offset_check": (
                "外部报告的累计雨量（72h→26.9mm / 96h→58.6mm）比本文件曲线整体超前 24 h，"
                "对应 t0=09-04T00 口径。本文件采用与本 run 的 rain.rain / yujiang.bdy / "
                "res.mass 同源的 T0_DAY=3 口径，故数值不可直接混用。"
                "该偏移不影响「暴增帧上游 Q 仍为基流」这一判据：Q 基流段持续到 t=120h，"
                "而暴增帧在 t=84h，任何偏移量下都落在基流段内。"
            ),
        },
        "patch_count_series": [],
        "evidence_level": "E1（逐帧复算 + rain.rain/yujiang.bdy 解析）",
    }
    # 逐帧加入累计雨量与上游 Q（同一模型时钟），供报告直接引用
    _rain_pts = [(float(p["t_s"]), float(p["mm"])) for p in rain["rain_series"]]
    rain_cum_by_t: list[tuple[float, float]] = []
    _cum = 0.0
    for t_s, mm in _rain_pts:
        _cum += mm
        rain_cum_by_t.append((t_s, _cum))

    def _rain_cum_at(t_s: float) -> float:
        total = 0.0
        for tt, mm in _rain_pts:
            if tt <= t_s:
                total += mm
        return total

    def _q_at(t_s: float) -> float:
        for (t0, q0), (t1, q1) in zip(UPSTREAM_Q_SERIES, UPSTREAM_Q_SERIES[1:]):
            if t0 <= t_s <= t1:
                return q0 + (q1 - q0) * (t_s - t0) / (t1 - t0)
        return UPSTREAM_Q_SERIES[-1][1]

    for row in frame_rows:
        t_s = row["seq"] * saveint
        rain_series["patch_count_series"].append({
            "seq": row["seq"], "t_h": row["t_h"],
            "n_patches": row["n_patches"], "wet_area_km2": row["wet_area_km2"],
            "patches_le_4_cells": row["patches_le_4_cells"],
            "patches_le_9_cells": row["patches_le_9_cells"],
            "rain_cum_mm": round(_rain_cum_at(t_s), 2),
            "upstream_q_m3s": round(_q_at(t_s), 1),
        })
    # 暴增前后两帧的驱动状态（报告核心引用）
    series = rain_series["patch_count_series"]
    burst_idx = next(
        (i for i, r in enumerate(series) if r["seq"] == burst["seq"]), None
    )
    prev_row = series[burst_idx - 1] if burst_idx else None
    burst_row = series[burst_idx] if burst_idx is not None else None
    rain_series["burst_transition"] = {
        "from": prev_row,
        "to": burst_row,
        "patch_count_delta": (
            burst_row["n_patches"] - prev_row["n_patches"]
            if prev_row and burst_row else None
        ),
        "patch_count_ratio": (
            round(burst_row["n_patches"] / prev_row["n_patches"], 2)
            if prev_row and burst_row and prev_row["n_patches"] else None
        ),
        "upstream_q_at_burst_m3s": burst_row["upstream_q_m3s"] if burst_row else None,
        "upstream_q_is_baseflow": bool(
            burst_row and burst_row["upstream_q_m3s"] <= 1000.0 + 1e-6
        ),
        "rain_cum_at_burst_mm": burst_row["rain_cum_mm"] if burst_row else None,
        "interpretation": (
            "斑块数最大增幅出现在「上游 Q 仍为基流 1000 m³/s」的帧上，且该帧累计雨量"
            "已明显抬升；斑块数随后随降雨过程持续维持高位，而与洪峰（seq38 峰值 11500）"
            "不同步。该同步性是相关描述（E2），不是因果证明。因果确认需 Phase 2 的"
            "flow_only_v1 对照 run（rain=off）——该 run 若斑块数不暴增，则根因一成立。"
        ),
        "evidence_level": "E1（时序数值）；E2（同步性归因）；因果待 Phase 2 确认",
    }

    mass = read_res_mass(run_dir)

    # ---- 轨迹 CSV ----
    if not dry_run:
        with (out_dir / "patch_tracks.csv").open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(TRACK_COLUMNS), extrasaction="ignore")
            writer.writeheader()
            for row in track_rows:
                writer.writerow(row)
        with (out_dir / "patch_frame_summary.csv").open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(FRAME_COLUMNS), extrasaction="ignore")
            writer.writeheader()
            for row in frame_rows:
                writer.writerow(row)

    # ---- 全域深度谱与主/非主水体深度对比（res.max 口径）----
    res_max_depth = res_max_grid.data  # float32，不升位
    res_max_wet = np.isfinite(res_max_depth) & (res_max_depth > res_max_depth.dtype.type(WET_THRESHOLD_M))
    main_mask_flat = np.zeros(int(res_max_depth.size), dtype=bool)
    main_mask_flat[res_max_summary.patches[0].cell_indices] = True if res_max_summary.patches else False
    main_mask = main_mask_flat.reshape(res_max_depth.shape)
    non_main_wet = res_max_wet & ~main_mask

    depth_spectrum = {
        "note": (
            "res.max 口径下，depth > 阈值 的像元面积与占湿面积比例。"
            "用于检验「全域大部分湿区是深水」这一事实（与计划 §1.2 的浅水噪点叙事对照）。"
        ),
        "wet_area_km2_at_0p05": round(float(res_max_wet.sum()) * cell_area / 1e6, 4),
        "by_threshold": {
            f"{thr:g}": {
                "depth_gt_m": thr,
                "cells": int((res_max_wet & (res_max_depth > res_max_depth.dtype.type(thr))).sum()),
                "area_km2": round(
                    float((res_max_wet & (res_max_depth > res_max_depth.dtype.type(thr))).sum())
                    * cell_area / 1e6, 4
                ),
                "pct_of_wet_area": round(
                    float((res_max_wet & (res_max_depth > res_max_depth.dtype.type(thr))).sum())
                    / max(int(res_max_wet.sum()), 1) * 100, 3
                ),
            }
            for thr in (0.05, 0.10, 0.15, 0.30, 0.50, 1.0, 2.0)
        },
        "evidence_level": "E1（脚本从 res.max 逐像元复算，float32 口径）",
    }

    def _cell_quantiles(values: np.ndarray) -> dict[str, Any]:
        if values.size == 0:
            return {"n": 0}
        return {
            "n_cells": int(values.size),
            "area_km2": round(float(values.size) * cell_area / 1e6, 4),
            "p10": round(float(np.percentile(values, 10)), 4),
            "p25": round(float(np.percentile(values, 25)), 4),
            "p50": round(float(np.percentile(values, 50)), 4),
            "p75": round(float(np.percentile(values, 75)), 4),
            "p90": round(float(np.percentile(values, 90)), 4),
            "p99": round(float(np.percentile(values, 99)), 4),
            "max": round(float(values.max()), 4),
            "mean": round(float(values.mean()), 4),
        }

    main_vs_nonmain_depth = {
        "note": (
            "主水体 = 面积最大的单个连通斑块（该 run 的郁江干流连通漫滩）。"
            "非主水体 = 其余全部湿像元。用于回答「离散水点是否只是薄水」："
            "非主水体中位深度不深，但尾部很深，说明其中既有薄水也有深水坑。"
        ),
        "main_body": {
            "patch_count": 1,
            "definition": "面积最大的单个 8 邻域连通斑块",
            **_cell_quantiles(res_max_depth[res_max_wet & main_mask]),
        },
        "non_main_body": _cell_quantiles(res_max_depth[non_main_wet]),
        "domain_wet": _cell_quantiles(res_max_depth[res_max_wet]),
        "evidence_level": "E1（脚本从 res.max 逐像元复算）",
    }

    # ---- 三档实测 vs 计划预测 ----
    tier_comparison = {}
    for tier, spec in TIER_SPECS.items():
        actual = res_max_tiers[tier]["removal_pct"]
        predicted = float(spec["plan_predicted_removal_pct"])
        tier_comparison[tier] = {
            "label": spec["label"],
            "pre_registered_params": {
                "area_max_km2": float(spec["area_max_km2"]),
                "n_cells_area_max": dem.cells_for_area(float(spec["area_max_km2"])),
                "depth_max_m": float(spec["depth_max_m"]),
                "s1_buffer_m": float(spec["s1_buffer_m"]),
            },
            "plan_predicted_removal_pct": predicted,
            "measured_removal_pct_res_max": actual,
            "measured_removal_pct_peak_frame": peak_tiers[tier]["removal_pct"],
            "measured_removal_pct_track_level": track_tiers[tier]["removal_pct"],
            "measured_area_change_pct_res_max": res_max_tiers[tier]["area_change_pct"],
            "measured_volume_change_pct_res_max": res_max_tiers[tier]["volume_change_pct"],
            "gap_pp": round(actual - predicted, 3),
            "meets_plan_target": bool(actual >= predicted),
            "plan_target_90pct_met": bool(actual >= 90.0),
            "reason_counts_res_max": res_max_tiers[tier]["reason_counts"],
            "r3_only_removal_pct": r3_only[tier]["removal_pct"],
            "r3_only_area_change_pct": r3_only[tier]["area_change_pct"],
            "r3_only_volume_change_pct": r3_only[tier]["volume_change_pct"],
        }

    return {
        "run_id": run_dir.name,
        "grid": {
            **dem.to_meta(),
            "cells": int(dem.data.size),
            "domain_area_km2": round(int(dem.data.size) * cell_area / 1e6, 4),
        },
        "time": {
            "sim_time_s": sim_time,
            "saveint_s": saveint,
            "frame_count": len(frames),
            "n_wet_frames": n_wet_frames,
            "sim_hours": round(sim_time / 3600.0, 3),
            "first_frame_seq": frames[0][0],
            "last_frame_seq": frames[-1][0],
            "last_frame_t_h": round(frames[-1][0] * saveint / 3600.0, 3),
        },
        "threshold_conversion": threshold_conversion,
        "s1_corridor": {
            k: v for k, v in s1.items()
            if not isinstance(v, np.ndarray) and not isinstance(v, dict)
        } | {
            # buffers 是 {tier: ndarray}；只登记面积口径，不序列化掩膜
            "buffers_summary": {
                tier: {
                    "buffer_m": float(TIER_SPECS[tier]["s1_buffer_m"]),
                    "buffer_cells": int(s1["buffer_cells_by_tier"][tier]),
                    "mask_cells": int(np.count_nonzero(buf)),
                    "mask_area_km2": round(float(np.count_nonzero(buf)) * cell_area / 1e6, 4),
                    "mask_frac_of_domain_pct": round(float(buf.mean()) * 100, 3),
                }
                for tier, buf in (s1.get("buffers") or {}).items()
            },
        },
        "frame_summary": frame_rows,
        "track_count": len(track_rows),
        "res_max_facts": {
            "wet_cells": res_max_summary.wet_cells,
            "wet_area_km2": round(res_max_summary.wet_area_km2, 4),
            "flood_ratio_pct_of_domain": round(
                res_max_summary.wet_cells / int(dem.data.size) * 100, 4
            ),
            "patch_count": res_max_summary.n_patches,
            "max_depth_m": round(res_max_summary.max_depth_m, 4),
            **res_max_summary.depth_stats,
            "patches_le_1_cell": int((ncells <= 1).sum()),
            "patches_le_4_cells": int((ncells <= 4).sum()),
            "patches_le_4_cells_pct": round(float((ncells <= 4).mean()) * 100, 3),
            "patches_le_9_cells": int((ncells <= 9).sum()),
            "patches_le_9_cells_pct": round(float((ncells <= 9).mean()) * 100, 3),
            "patches_ge_1km2": int((areas >= 1.0).sum()),
            "largest_patch_area_km2": volume_budget["largest_patch_area_km2"],
            "largest_patch_share_pct": volume_budget["largest_patch_share_of_wet_area_pct"],
            "wet_volume_m3": volume_budget["res_max_wet_volume_m3"],
        },
        "volume_budget": volume_budget,
        "discrete_depth_profile": discrete_depth_profile,
        "depth_spectrum": depth_spectrum,
        "main_vs_nonmain_depth": main_vs_nonmain_depth,
        "rain_accounting": rain,
        "rain_interception": rain_interception,
        "mass_accounting": mass,
        "structural_ceiling": structural_ceiling,
        "tier_comparison": tier_comparison,
        "area_spectrum_cumulative": area_spectrum_cumulative,
        "plan_prediction_provenance": plan_prediction_provenance,
        "diagnostic_variants": diagnostic_variants,
        "tier_per_frame": per_frame_tiers,
        "min_persistence_ratio_sensitivity": persistence_sensitivity,
        "timing_evidence": rain_series,
        "patch_row_count": n_patch_rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Phase 0：贵港全帧斑块时空统计 + 降雨账目 + 三档实测去除率（只读诊断）"
    )
    add_common_args(parser)
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID, help="run 目录名（默认贵港 100 m 基线）")
    args = parser.parse_args(argv)

    run_tag = (args.run_tag or "").strip()
    out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_OUT
    if run_tag:
        out_dir = out_dir / run_tag
    log_path = Path(args.log_json) if args.log_json else (
        DEFAULT_LOG_DIR / f"analyze_guigang_patches{'_' + run_tag if run_tag else ''}.json"
    )
    audit = AuditLog("analyze_guigang_patches", run_tag=run_tag or None, dry_run=args.dry_run)
    audit.param(
        wet_threshold_m=WET_THRESHOLD_M,
        thin_film_max_m=THIN_FILM_MAX_M,
        connectivity=8,
        area_bins_km2=list(AREA_BINS_KM2),
        main_body_area_fraction=MAIN_BODY_AREA_FRACTION,
        protected_main_stem_min_area_km2=PROTECTED_MAIN_STEM_MIN_AREA_KM2,
        deep_depth_threshold_m=DEEP_DEPTH_THRESHOLD_M,
        tiers={t: {k: v for k, v in s.items()} for t, s in TIER_SPECS.items()},
        min_persistence_ratio=MIN_PERSISTENCE_RATIO,
        min_persistence_ratio_sensitivity=list(MIN_PERSISTENCE_RATIO_SENSITIVITY),
        out_dir=str(out_dir),
        note=(
            "三档参数照抄计划 §Phase1 表格原值，未按结果调整；"
            "全部面积阈值以 km² 表达，像元数由栅格头 cellsize 经 cells_for_area 现算"
        ),
    )
    audit.add_input("era5_guigang", ERA5)
    audit.add_input("mass_recheck_g15", MASS_G15)
    audit.add_input("s1_water_pre", S1_PRE)

    run_dir = GG / args.run_id
    if not run_dir.is_dir():
        audit.error(f"缺少 run 目录：{run_dir}")
        audit.write(log_path)
        print(f"[FAIL] 缺少 run 目录：{run_dir}", file=sys.stderr)
        return 4

    # 输出目录保护：不得含 results/res.mass（会污染 g15 质量门计数）
    try:
        guard_no_overwrite(out_dir)
    except Exception as exc:  # OverwriteRefused
        audit.error(str(exc))
        audit.write(log_path)
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 3
    protected = (PROJ / "outputs" / "guigang").resolve()
    if out_dir.resolve() == protected or protected in out_dir.resolve().parents:
        audit.error(f"拒绝写入受保护目录 {out_dir}：只读承诺")
        audit.write(log_path)
        print("[FAIL] 输出目录落在 outputs/guigang 内", file=sys.stderr)
        return 3

    frames = run_frames(run_dir / "results")
    if not frames:
        audit.error(f"{run_dir}/results 无帧")
        audit.write(log_path)
        print("[FAIL] 无 res-XXXX.wd 帧", file=sys.stderr)
        return 4

    print(f"[1/3] 源 run 哈希（前）… {run_dir.name}", flush=True)
    before = hash_source_run(run_dir, frames)
    audit.step("source_hash_before", frame_count=before["frame_count"],
               frame_list_sha256=before["frame_list_sha256"])

    print("[2/3] 全帧斑块统计 + 轨迹 + 三档去除率 …", flush=True)
    result = analyze_run(run_dir, dry_run=args.dry_run, out_dir=out_dir, audit=audit)

    print("[3/3] 源 run 哈希（后）比对 …", flush=True)
    after = hash_source_run(run_dir, frames)
    comparison = compare_source_hashes(before, after)
    audit.step("source_hash_after", unchanged=comparison["unchanged"],
               differing_keys=comparison["differing_keys"],
               changed_frames=len(comparison["changed_frames"]))
    if not comparison["unchanged"]:
        audit.error(f"源 run 在分析期间被改动：{comparison}")
        audit.write(log_path)
        print(f"[FAIL] 源 run 哈希前后不一致：{comparison}", file=sys.stderr)
        return 5

    payload = {
        "generated_utc": utc_now(),
        "script": SCRIPT_NAME,
        "run_tag": run_tag or None,
        "phase": "Phase 0 / T0.1+T0.3（斑块时空 + 降雨账目 + 三档实测）",
        "mode": "ADR-0004 公开数据降级、情景级、非业务预警",
        "read_only_declaration": (
            "本文件全部数值由脚本从 outputs/guigang/** 只读复算；未写入、移动或删除该目录下"
            "任何文件；未调用 lisflood.exe；models/ 未修改"
        ),
        "source_run": {
            "run_id": run_dir.name,
            "path": str(run_dir.relative_to(PROJ)).replace("\\", "/"),
            "sha256_before": before,
            "sha256_after": after,
            "integrity": comparison,
        },
        "hard_constraints": {
            "connectivity": 8,
            "wet_threshold_m": WET_THRESHOLD_M,
            "wet_threshold_dtype": "float32（未升位；float32(0.05)=0.05000000074505806）",
            "area_threshold_expression": "km²；像元数由栅格头 cellsize 经 cells_for_area 现算",
            "forbidden_literals": ["10000.0", "4 像元", "9 像元"],
            "no_lisflood_call": True,
            "no_models_edit": True,
            "no_res_mass_in_output_dir": True,
            "threshold_policy": (
                "三档参数照抄计划 §Phase1 表格原值；未按结果调整；"
                "计划未给 min_persistence_ratio 数值，本脚本处理前预注册 0.25 并给敏感性区间"
            ),
        },
        "evidence_levels": {
            "frame_patch_statistics": "E1（脚本从 raw run 逐帧复算）",
            "patch_tracks": "E1（8 邻域连通 + 像元重叠关联）",
            "tier_removal_rates": "E1（按计划原规则顺序逐斑块/逐轨迹复算）",
            "rain_budget": "E1（rain.rain 与 yujiang.bdy 解析积分，与 res.mass 交叉核对）",
            "rain_interception": "E2（分子为 res.max 包络口径、分母为累计入流口径）",
            "physical_cause_attribution": "E2/E3（时序同步性是相关描述，非因果证明）",
            "independent_accuracy_metrics": "E4 未测（S1 仅灾前/洪峰两景，无连续观测）",
        },
        **result,
    }

    if args.dry_run:
        print("\n[DRY-RUN] 不写派生文件。摘要：", flush=True)
        rm = payload["res_max_facts"]
        print(f"  res.max: 湿像元 {rm['wet_cells']}（{rm['wet_area_km2']} km²，"
              f"淹没率 {rm['flood_ratio_pct_of_domain']}%），斑块 {rm['patch_count']}，"
              f"≤4 格 {rm['patches_le_4_cells']}（{rm['patches_le_4_cells_pct']}%），"
              f"≤9 格 {rm['patches_le_9_cells']}（{rm['patches_le_9_cells_pct']}%），"
              f"最大斑块 {rm['largest_patch_area_km2']} km²（{rm['largest_patch_share_pct']}%）", flush=True)
        ra = payload["rain_accounting"]
        print(f"  降雨账目：累计 {ra['rain_total_mm']} mm（{ra['rain_hours_with_rain']} 小时有雨，"
              f"最大 {ra['rain_max_hourly_mm']} mm/h）；V_rain {ra['v_rain_m3']:.4e} m³；"
              f"V_upstream {ra['v_upstream_m3']:.4e} m³；雨水占总入流 "
              f"{ra['rain_share_of_total_inflow_pct']}%", flush=True)
        sc = payload["structural_ceiling"]
        print(f"  结构上界：max_depth >= {sc['deep_depth_threshold_m']} m 的斑块 "
              f"{sc['patches_with_max_depth_ge_threshold']}/{sc['res_max_patch_count']}，"
              f"任何档位去除率上界 {sc['max_possible_removal_pct_any_tier']}%", flush=True)
        for tier, rec in payload["tier_comparison"].items():
            print(f"  档 {tier}：计划预测 {rec['plan_predicted_removal_pct']}% → 实测 "
                  f"{rec['measured_removal_pct_res_max']}%（峰值帧 "
                  f"{rec['measured_removal_pct_peak_frame']}%，轨迹级 "
                  f"{rec['measured_removal_pct_track_level']}%）；"
                  f"面积变化 {rec['measured_area_change_pct_res_max']}%，"
                  f"水量变化 {rec['measured_volume_change_pct_res_max']}%；"
                  f"达标={rec['meets_plan_target']}", flush=True)
        print(f"  patch_persistence.csv 行数 {payload['patch_row_count']}；"
              f"轨迹 {payload['track_count']}；帧 {payload['time']['frame_count']}", flush=True)
        print("  计划预测值来源（面积谱累计占比）：", flush=True)
        for tier, rec in payload["area_spectrum_cumulative"].items():
            print(f"    档 {tier}: 面积 <= {rec['area_max_km2']} km² 的斑块 "
                  f"{rec['patches_area_le_amax']} 个（{rec['cumulative_pct_of_patches']}%）"
                  f" vs 计划预测 {rec['plan_predicted_removal_pct']}% "
                  f"（吻合={rec['matches_area_spectrum_within_1pp']}）", flush=True)
        print("  诊断变体（非发布参数）：", flush=True)
        for tier, rec in payload["diagnostic_variants"].items():
            print(f"    档 {tier}: V1 仅面积 {rec['v1_area_only_removal_pct']}% / "
                  f"V2 面积+水深(绕过 R2) {rec['v2_area_and_depth_no_deep_exemption_pct']}% / "
                  f"V3 完整四规则 {rec['v3_full_four_rules_removal_pct']}%", flush=True)
        if args.log_json:
            audit.write(log_path)
        return 0

    write_json(out_dir / "patch_summary.json", payload)
    audit.step("write_products", out_dir=str(out_dir),
               patch_rows=payload["patch_row_count"], tracks=payload["track_count"],
               frames=payload["time"]["frame_count"])
    audit.write(log_path)
    for name in ("patch_persistence.csv", "patch_frame_summary.csv",
                 "patch_tracks.csv", "patch_summary.json"):
        print(f"-> {out_dir / name}")
    print(f"-> {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
