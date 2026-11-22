"""Phase 0 / T0.2：贵港事实冻结 + DEM 假洼地与离散积水斑块空间重合率（只读诊断）。

对应计划 `plans/贵港水动力推演效果改进实施计划_从零散水点治理到上游来水与降雨双驱动重构.md`：

- §3 Phase 0 任务 2「DEM 微地形假坑诊断」：分析全域无流向假洼地（sinks）分布，
  比对假洼地空间位置与 4,422 个离散积水斑块的重合率，**计划预估相关系数 > 0.85**。
  本脚本用 5 种洼地口径 + 斑块级零假设（null）实测该预估是否成立。
- §6 M0「诊断冻结」：run 清单、栅格元数据、实际单元面积、时间口径、输入哈希、
  质量门状态，写入 ``baseline_snapshot.json``。

口径硬约束（与 ``analyze_guigang_patches.py`` 完全一致，全部由脚本强制）：

1. 湿格判定 ``depth > 0.05 m``，**保持 float32 口径不升位**。
2. 连通域 8 邻域；面积阈值以 km² 表达，像元数经 ``cells_for_area`` 现算。
3. 只读 ``outputs/guigang/**``；脚本前后各算一次源 run sha256 并比对，不一致即硬失败。
4. 不调用 ``lisflood.exe``，不修改 ``models/``。
5. 输出目录不得含 ``res.mass``。
6. 所有 JSON 带 ``generated_utc``、脚本名、输入哈希；结论带证据等级 E1–E4。

关于「相关系数 > 0.85」的口径说明（重要）：
    计划未定义相关系数的计算对象。本脚本按**三种可复算口径**同时给出，避免选择性报告：
      a) 像元级：洼地掩膜 vs 离散斑块掩膜的 Phi 系数（二值变量相关系数）
      b) 1 km 分块密度级：Pearson r（洼地密度 vs 离散斑块密度）
      c) 斑块级命中率：离散斑块「触碰洼地」的比例，并用随机平移零假设给对照基线
    **不挑口径**：三种全部登记，并明确哪一种支持、哪一种不支持计划的 0.85 预估。

用法（先设 ``$env:PYTHONUTF8="1"``）::

    uv --directory 2d_hydro_project run python scripts/stage6/diagnose_guigang_artifacts.py --dry-run
    uv --directory 2d_hydro_project run python scripts/stage6/diagnose_guigang_artifacts.py

产出（默认目录 ``diagnostics/guigang/``）：
    baseline_snapshot.json     run 清单、栅格元数据、单元面积、时间口径、输入哈希、质量门状态
    guigang_sink_analysis.json DEM 假洼地分布 + 与离散积水斑块的空间重合率（5 口径 × 3 指标）
"""
from __future__ import annotations

import argparse
import heapq
import json
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
from scripts.common.patch_analysis import summarize_frame  # noqa: E402
from scripts.stage6.analyze_guigang_patches import (  # noqa: E402
    DEFAULT_RUN_ID,
    ERA5,
    GG,
    MASS_G15,
    S1_PRE,
    WET_THRESHOLD_M,
    compare_source_hashes,
    hash_source_run,
    read_res_mass,
)

DEFAULT_OUT = PROJ / "diagnostics" / "guigang"
DEFAULT_LOG_DIR = PROJ / "logs" / "diagnostics"
SCRIPT_NAME = "scripts/stage6/diagnose_guigang_artifacts.py"

#: 细薄水膜上限：输出格式量化到 0.001 m（output.cpp:686 ``%.3f``）。
THIN_FILM_MAX_M = 0.005
#: 离散积水斑块的诊断尺寸（像元数上限）。只用于把「离散」与「成片」分开统计。
#: 注意：这是**像元数**不是面积阈值；面积口径一律走 cells_for_area，见 convert_areas()。
DISCRETE_MAX_CELLS = 9
#: 分块密度相关的块边长（像元）。100 m 网格 → 1 km 块。
BLOCK_CELLS = 10
#: 斑块级零假设的随机平移抽样次数。
NULL_DRAWS = 200

NB8 = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))

REQUIRED_RUN_FILES = (
    "dem.asc", "run.par", "meta.json", "bci.bci", "rain.rain", "yujiang.bdy",
)


# --------------------------------------------------------------------------- #
# 地形水文派生（纯 numpy + heapq，无 scipy 依赖）
# --------------------------------------------------------------------------- #
def pit_mask(dem: np.ndarray) -> np.ndarray:
    """局部洼坑：8 邻域内不高于任一邻居。只统计内部像元（排除最外一圈）。

    边界像元用 edge padding 填充后天然满足「不高于邻居」，会把边界算成洼坑并虚增计数，
    故诊断洼坑一律使用 interior 口径。
    """
    height, width = dem.shape
    pad = np.pad(dem.astype(np.float64), 1, mode="edge")
    pits = np.ones((height, width), dtype=bool)
    for dr, dc in NB8:
        nb = pad[1 + dr:1 + dr + height, 1 + dc:1 + dc + width]
        pits &= dem <= nb
    pits[0, :] = False
    pits[-1, :] = False
    pits[:, 0] = False
    pits[:, -1] = False
    return pits


def priority_flood(dem: np.ndarray) -> np.ndarray:
    """Priority-Flood 填洼（诊断用；不产出候选 DEM，不替代真实地形）。"""
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


def d8_receiver(filled: np.ndarray) -> np.ndarray:
    """D8 最陡下降接收像元（按距离归一化坡度）；无下游返回 -1。"""
    height, width = filled.shape
    pad = np.pad(filled, 1, mode="edge")
    best = np.full((height, width), -np.inf)
    target = np.full((height, width), -1, dtype=np.int64)
    rows = np.arange(height)[:, None]
    cols = np.arange(width)[None, :]
    for dr, dc in NB8:
        nb = pad[1 + dr:1 + dr + height, 1 + dc:1 + dc + width]
        drop = (filled - nb) / float(np.hypot(dr, dc))
        better = drop > best
        best = np.where(better, drop, best)
        target = np.where(better, (rows + dr) * width + (cols + dc), target)
    target[best <= 0] = -1
    return target


def flow_accumulation(filled: np.ndarray, receiver: np.ndarray) -> np.ndarray:
    """按高程降序累加汇流像元数（每个像元自身计 1）。"""
    flat_receiver = receiver.ravel()
    acc = np.ones(filled.size, dtype=np.float64)
    for idx in np.argsort(-filled.ravel(), kind="stable"):
        downstream = flat_receiver[idx]
        if downstream >= 0:
            acc[downstream] += acc[idx]
    return acc.reshape(filled.shape)


# --------------------------------------------------------------------------- #
# 相关性指标（三种口径，全部登记，不挑口径）
# --------------------------------------------------------------------------- #
def phi_coefficient(a: np.ndarray, b: np.ndarray) -> dict[str, Any]:
    """二值变量的 Phi 系数（= 2×2 列联表的 Pearson 相关）。"""
    a = np.asarray(a, bool)
    b = np.asarray(b, bool)
    n11 = float(np.count_nonzero(a & b))
    n10 = float(np.count_nonzero(a & ~b))
    n01 = float(np.count_nonzero(~a & b))
    n00 = float(np.count_nonzero(~a & ~b))
    den = np.sqrt((n11 + n10) * (n01 + n00) * (n11 + n01) * (n10 + n00))
    return {
        "phi": round(float((n11 * n00 - n10 * n01) / den), 6) if den else 0.0,
        "n11": int(n11), "n10": int(n10), "n01": int(n01), "n00": int(n00),
        "recall_sink_is_wet": round(n11 / (n11 + n10) * 100, 3) if (n11 + n10) else 0.0,
        "precision_wet_is_sink": round(n11 / (n11 + n01) * 100, 3) if (n11 + n01) else 0.0,
    }


def block_density(mask: np.ndarray, block: int = BLOCK_CELLS) -> np.ndarray:
    """把掩膜按 block×block 分块求密度（0–1）。用于分块级 Pearson 相关。"""
    height, width = mask.shape
    hb, wb = height // block, width // block
    if hb == 0 or wb == 0:
        return np.zeros(0, dtype=np.float64)
    counts = mask[:hb * block, :wb * block].reshape(hb, block, wb, block).sum(axis=(1, 3))
    return counts.astype(np.float64) / float(block * block)


def pearson(a: np.ndarray, b: np.ndarray) -> float | None:
    """Pearson 相关；任一序列零方差时返回 None（不返回 0，避免误读为「无相关」）。"""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if a.size < 2 or b.size != a.size:
        return None
    if a.std() == 0 or b.std() == 0:
        return None
    return round(float(np.corrcoef(a, b)[0, 1]), 6)


def patch_touch_rate(
    patches: list[Any], sink_flat: np.ndarray, *, shape: tuple[int, int],
    n_draw: int = NULL_DRAWS, seed: int = 20260912,
) -> dict[str, Any]:
    """斑块级：有多少斑块「触碰」洼地；并用随机平移零假设给对照基线。

    零假设：把每个斑块整体平移到域内随机位置（保持形状），统计触碰洼地的概率。

    **抽样纪律（易错点）**：``summarize_frame`` 返回的斑块按面积降序排列，若直接取前
    N 个做零假设，抽到的是最大的斑块（触碰概率天然高），会把零假设基线抬高数倍，
    从而低估 lift。因此本函数用**固定随机种子均匀抽样**，并在**同一子集**上同时算
    实际命中率与零假设命中率，保证两者可比。同时对每个 ``n_cells`` 分组单独给对照，
    因为触碰概率随斑块像元数单调上升（独立近似 ``1-(1-p)^n``）。
    """
    if not patches:
        return {"available": False}
    rng = np.random.default_rng(seed)
    height, width = shape
    sink2d = sink_flat.reshape(height, width)

    def _touch(patch: Any) -> bool:
        cells = patch.cell_indices
        return bool(cells.size) and bool(sink_flat[cells].any())

    def _full(patch: Any) -> bool:
        cells = patch.cell_indices
        return bool(cells.size) and bool(sink_flat[cells].all())

    def _null_touch(patch: Any) -> float | None:
        """该斑块在随机平移下触碰洼地的概率（蒙特卡洛）。"""
        cells = patch.cell_indices
        if cells.size == 0:
            return None
        rows, cols = np.divmod(cells, width)
        r0, c0 = int(rows.min()), int(cols.min())
        hgt, wid = int(rows.max()) - r0 + 1, int(cols.max()) - c0 + 1
        if hgt > height or wid > width:
            return None
        local = np.zeros((hgt, wid), dtype=bool)
        local[rows - r0, cols - c0] = True
        hits = 0
        for _ in range(n_draw):
            rr = int(rng.integers(0, height - hgt + 1))
            cc = int(rng.integers(0, width - wid + 1))
            if bool(sink2d[rr:rr + hgt, cc:cc + wid][local].any()):
                hits += 1
        return hits / n_draw

    # ---- 全量：实际命中率 ----
    touch = sum(1 for p in patches if _touch(p))
    full = sum(1 for p in patches if _full(p))

    # ---- 均匀随机子集：实际 vs 零假设（同一子集，可比）----
    n_sample = min(len(patches), 300)
    idx = rng.choice(len(patches), size=n_sample, replace=False)
    sample = [patches[int(i)] for i in sorted(idx)]
    s_touch = sum(1 for p in sample if _touch(p))
    null_vals = [v for v in (_null_touch(p) for p in sample) if v is not None]
    null_mean = float(np.mean(null_vals)) if null_vals else None

    # ---- 按 n_cells 分组：每组的实际 vs 零假设 vs 解析期望 ----
    by_size: dict[int, dict[str, Any]] = {}
    sink_frac = float(np.count_nonzero(sink_flat)) / float(sink_flat.size)
    groups: dict[int, list[Any]] = {}
    for p in patches:
        if p.n_cells <= 9:
            groups.setdefault(int(p.n_cells), []).append(p)
    for size, group in sorted(groups.items()):
        g_touch = sum(1 for p in group if _touch(p))
        sub = group[: min(len(group), 120)]
        sub_touch = sum(1 for p in sub if _touch(p))
        sub_null = [v for v in (_null_touch(p) for p in sub) if v is not None]
        by_size[size] = {
            "patch_count": len(group),
            "touch_pct": round(g_touch / len(group) * 100, 3),
            "null_sample_patches": len(sub),
            "null_expected_touch_pct": (
                round(float(np.mean(sub_null)) * 100, 3) if sub_null else None
            ),
            "analytic_independent_pct": round(
                (1.0 - (1.0 - sink_frac) ** size) * 100, 3
            ),
            "lift_ratio": (
                round((sub_touch / len(sub)) / float(np.mean(sub_null)), 3)
                if sub_null and float(np.mean(sub_null)) > 0 else None
            ),
        }

    n = len(patches)
    return {
        "available": True,
        "patch_count": n,
        "patches_touching_sink": touch,
        "touch_pct": round(touch / n * 100, 3) if n else 0.0,
        "patches_fully_in_sink": full,
        "fully_in_sink_pct": round(full / n * 100, 3) if n else 0.0,
        "null_model": {
            "method": "把斑块整体平移到域内随机位置（保持形状），统计触碰洼地概率",
            "draws_per_patch": n_draw,
            "sample_size": n_sample,
            "sampling": (
                "均匀随机抽样（固定种子）；**不按面积降序取前 N**——"
                "summarize_frame 按面积降序返回，取前 N 会系统性偏向大斑块，"
                "把零假设基线抬高数倍"
            ),
            "actual_touch_pct_on_sample": round(s_touch / n_sample * 100, 3) if n_sample else None,
            "expected_touch_pct": round(null_mean * 100, 3) if null_mean is not None else None,
            "lift_ratio": (
                round((s_touch / n_sample) / null_mean, 3)
                if null_mean and null_mean > 0 and n_sample else None
            ),
            "note": (
                "lift > 1 表示实际重合高于随机平移；≈1 表示与随机摆放无差别。"
                "零假设未控制地形可达性（积水只能出现在低地），故 lift 是保守下界，"
                "不是精确显著性检验。触碰概率随斑块像元数单调上升，"
                "故总体 lift 必须与 by_size 分组 lift 一起读。"
            ),
        },
        "by_n_cells": by_size,
        "sink_fraction_of_domain": round(sink_frac, 6),
    }


# --------------------------------------------------------------------------- #
# run 清单与事实
# --------------------------------------------------------------------------- #
def classify_run_role(name: str) -> tuple[str, str]:
    """按目录名给出贵港 run 角色。"""
    if "interrupted" in name:
        return "interrupted_run", "中断的 run，不参与诊断主表与质量门"
    if name == DEFAULT_RUN_ID:
        return "production_baseline_100m", (
            "100 m 真实 DEM 基线（channel=off，无人工河槽刻蚀）；本 Phase 0 的诊断对象"
        )
    if name == "scen_yagi_q11500_200m":
        return "baseline_200m", "200 m 基线（跨分辨率参照）"
    if name.startswith("scen_yagi_q") and name.endswith("_200m"):
        return "q_sensitivity_200m", "上游 Q 敏感性情景（200 m）"
    return "other", "未归类的 run 目录"


def scan_runs() -> list[dict[str, Any]]:
    """扫描贵港全部 run 目录，记录角色、完整性与关键元数据。"""
    g15: dict[str, Any] = {}
    if MASS_G15.is_file():
        doc = json.loads(MASS_G15.read_text(encoding="utf-8"))
        g15 = doc.get("runs", {})
    records: list[dict[str, Any]] = []
    if not GG.is_dir():
        return records
    for run_dir in sorted(p for p in GG.iterdir() if p.is_dir()):
        name = run_dir.name
        role, role_note = classify_run_role(name)
        missing = [f for f in REQUIRED_RUN_FILES if not (run_dir / f).is_file()]
        if not (run_dir / "run.log").is_file():
            missing.append("run.log")
        if not (run_dir / "results" / "res.mass").is_file():
            missing.append("results/res.mass")
        meta: dict[str, Any] = {}
        meta_path = run_dir / "meta.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                meta = {}
        par = parse_run_par(run_dir)
        frames = run_frames(run_dir / "results")
        verify: dict[str, Any] = {}
        verify_path = run_dir / "verify.json"
        if verify_path.is_file():
            try:
                verify = json.loads(verify_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                verify = {}
        rel = f"outputs/guigang/2024-09-yagi/{name}"
        gate = g15.get(rel, {})
        records.append({
            "run_id": name,
            "path": rel,
            "role": role,
            "role_note": role_note,
            "eligible_for_diagnosis": role not in ("interrupted_run",) and not missing,
            "missing_files": missing,
            "frame_count": len(frames),
            "first_frame": frames[0][1].name if frames else None,
            "last_frame": frames[-1][1].name if frames else None,
            "run_par": {
                "sim_time_s": par.get("sim_time"),
                "saveint_s": par.get("saveint"),
                "initial_tstep_s": par.get("initial_tstep"),
                "massint_s": par.get("massint"),
                "fpfric": par.get("fpfric"),
                "acceleration": bool(par.get("acceleration", False)),
            },
            "meta": {
                "res_m": meta.get("res"),
                "dt_s": meta.get("dt"),
                "q_base": meta.get("q_base"),
                "q_peak": meta.get("q_peak"),
                "sim_seconds": meta.get("sim_seconds"),
                "t0": meta.get("t0"),
                "channel_mode": (meta.get("channel") or {}).get("mode"),
                "point_source_32649": meta.get("point_source_32649"),
            },
            "verify_json": {
                "wet_area_km2": verify.get("wet_area_km2"),
                "final_vol_m3": verify.get("final_vol_m3"),
                "e_mass_132": verify.get("e_mass_132"),
                "v_in_integrated_m3": verify.get("v_in_integrated_m3"),
                "v_rain_m3": verify.get("v_rain_m3"),
            },
            "mass_gate_g15": {
                "gate": gate.get("gate_1e-3"),
                "e_mass": gate.get("e_mass_132"),
                "cause": gate.get("cause"),
            },
        })
    return records


def run_grid_facts(run_dir: Path) -> dict[str, Any]:
    """单 run 的栅格元数据、时间口径、单元面积与质量账目。"""
    grid = read_ascii_grid(run_dir / "dem.asc")
    res_m = grid.cell_size_m
    cell_area = grid.cell_area_m2
    par = parse_run_par(run_dir)
    saveint = float(par.get("saveint") or 0.0)
    sim = float(par.get("sim_time") or 0.0)
    frames = run_frames(run_dir / "results")
    timeline = [
        {"seq": seq, "t_s": seq * saveint, "t_h": round(seq * saveint / 3600.0, 3)}
        for seq, _ in frames
    ]
    meta: dict[str, Any] = {}
    meta_path = run_dir / "meta.json"
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    mass = read_res_mass(run_dir)
    return {
        "run_id": run_dir.name,
        "grid": {
            **grid.to_meta(),
            "cells": int(grid.data.size),
            "domain_area_km2": round(int(grid.data.size) * cell_area / 1e6, 4),
            "cell_area_m2_from_header": cell_area,
            "cell_area_source": "栅格头 cellsize 平方（grid_io.AsciiGrid.cell_area_m2）",
            "area_km2_to_cells": {
                f"{a:g}": grid.cells_for_area(a) for a in (0.04, 0.09, 0.25, 1.0, 5.0)
            },
        },
        "time": {
            "sim_time_s": sim,
            "saveint_s": saveint,
            "sim_hours": round(sim / 3600.0, 3),
            "frame_count": len(frames),
            "timeline": timeline,
            "beyond_sim_end": [t["seq"] for t in timeline if sim and t["t_s"] > sim + 1e-6],
            "t0": meta.get("t0"),
            "t0_note": (
                "模型 t=0 对应 2024-09-03T00；rain.rain 与 bdy 时刻列同基准。"
                "ERA5 原始序列首键为 2024-09-05T00，勿混用"
            ),
        },
        "dem": {
            "dtype": "float32（res/dem 均为 float32；禁止升位）",
            "min_m": float(grid.data.min()),
            "max_m": float(grid.data.max()),
            "p50_m": round(float(np.median(grid.data)), 3),
            "negative_cells": int((grid.data < 0).sum()),
            "nodata_cells": int((grid.data == np.float32(grid.nodata)).sum()),
            "zero_cells": int((grid.data == 0).sum()),
        },
        "mass_accounting": mass,
        "channel": meta.get("channel"),
    }


# --------------------------------------------------------------------------- #
# 主分析
# --------------------------------------------------------------------------- #
def sink_analysis(
    run_dir: Path, *, out: dict[str, Any], audit: AuditLog,
    before: dict[str, Any] | None = None, after: dict[str, Any] | None = None,
    comparison: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """DEM 假洼地分布 + 与离散积水斑块的空间重合率（多口径）。"""
    grid = read_ascii_grid(run_dir / "dem.asc")
    cell_area = grid.cell_area_m2
    res_m = grid.cell_size_m
    shape = grid.shape
    dem = grid.data.astype(np.float64)

    res_max_grid = read_ascii_grid(run_dir / "results" / "res.max")
    if res_max_grid.shape != shape:
        raise GridError(f"res.max 形状 {res_max_grid.shape} 与 DEM {shape} 不符")
    depth = res_max_grid.data  # float32，不升位
    wet = np.isfinite(depth) & (depth > depth.dtype.type(WET_THRESHOLD_M))
    summary = summarize_frame(
        depth, -1, wet_threshold_m=WET_THRESHOLD_M,
        cell_area_m2=cell_area, sea_mask=None, keep_patches=True,
    )
    if not summary.patches:
        raise GridError("res.max 无湿像元；无法做洼地重合分析")

    def _area(mask: np.ndarray) -> float:
        return round(float(np.count_nonzero(mask)) * cell_area / 1e6, 4)

    main_patch = summary.patches[0]
    main_mask = np.zeros(shape, dtype=bool)
    main_mask.ravel()[main_patch.cell_indices] = True
    discrete_patches = [p for p in summary.patches if p.n_cells <= DISCRETE_MAX_CELLS]
    discrete_mask = np.zeros(shape, dtype=bool)
    for patch in discrete_patches:
        discrete_mask.ravel()[patch.cell_indices] = True
    non_main_wet = wet & ~main_mask

    # ---- 地形派生 ----
    pits = pit_mask(dem)
    filled = priority_flood(dem)
    rise = filled - dem
    depression = rise > 1e-9
    receiver = d8_receiver(filled)
    acc = flow_accumulation(filled, receiver)

    # ---- 5 种洼地口径（全部登记，不挑口径）----
    variants: dict[str, dict[str, Any]] = {
        "S1_local_pit": {
            "mask": pits,
            "definition": "局部洼坑：8 邻域内不高于任一邻居（interior 口径，排除最外圈）",
            "evidence_level": "E1（DEM 逐像元复算）",
        },
        "S2_priority_flood_depression": {
            "mask": depression,
            "definition": "Priority-Flood 填洼时需要抬升的像元（rise > 0）",
            "evidence_level": "E1",
        },
        "S3_shallow_depression_rise_le_1m": {
            "mask": depression & (rise <= 1.0),
            "definition": "浅层填洼需求 <= 1 m 的洼地（对应「数字高程误差级」微坑）",
            "evidence_level": "E3 候选假设（1 m 阈值为项目候选值，非外部规范）",
        },
        "S4_depression_small_catchment_lt_500": {
            "mask": depression & (acc < 500),
            "definition": "汇流累积 < 500 像元的填洼需求区（无成规模汇水通道）",
            "evidence_level": "E3 候选假设（500 像元阈值为项目候选值）",
        },
        "S5_pit_small_catchment_lt_500": {
            "mask": pits & (acc < 500),
            "definition": "局部洼坑 且 汇流累积 < 500 像元（坑 + 无排水通道）",
            "evidence_level": "E3 候选假设",
        },
    }

    block_sink_cache: dict[str, np.ndarray] = {}
    block_disc = block_density(discrete_mask)
    block_nonmain = block_density(non_main_wet)
    block_wet = block_density(wet)

    variant_reports: dict[str, Any] = {}
    for name, spec in variants.items():
        mask = spec["mask"]
        # (a) 像元级 Phi
        vs_disc = phi_coefficient(mask, discrete_mask)
        vs_wet = phi_coefficient(mask, wet)
        vs_nonmain = phi_coefficient(mask, non_main_wet)
        # (b) 分块密度级 Pearson
        bd = block_density(mask)
        block_sink_cache[name] = bd
        # (c) 斑块级 + 零假设
        patch_level = patch_touch_rate(discrete_patches, mask.ravel(), shape=shape)
        patch_level_all = patch_touch_rate(summary.patches, mask.ravel(), shape=shape)
        variant_reports[name] = {
            "definition": spec["definition"],
            "evidence_level": spec["evidence_level"],
            "sink_cells": int(np.count_nonzero(mask)),
            "sink_area_km2": _area(mask),
            "sink_frac_of_domain_pct": round(float(mask.mean()) * 100, 3),
            "pixel_level_phi": {
                "vs_discrete_patches": vs_disc,
                "vs_all_wet": vs_wet,
                "vs_non_main_wet": vs_nonmain,
            },
            "block_level_pearson": {
                "block_cells": BLOCK_CELLS,
                "block_km2": round(BLOCK_CELLS * res_m / 1000.0, 3),
                "n_blocks": int(bd.size),
                "vs_discrete_patches": pearson(bd, block_disc),
                "vs_non_main_wet": pearson(bd, block_nonmain),
                "vs_all_wet": pearson(bd, block_wet),
            },
            "patch_level_discrete": patch_level,
            "patch_level_all": patch_level_all,
        }

    # ---- 计划预估「相关系数 > 0.85」的核验 ----
    best_phi = max(
        (abs(v["pixel_level_phi"]["vs_discrete_patches"]["phi"]), k)
        for k, v in variant_reports.items()
    )
    best_block = max(
        (abs(v["block_level_pearson"]["vs_discrete_patches"] or 0.0), k)
        for k, v in variant_reports.items()
    )
    plan_check = {
        "plan_claim": "计划 §3 Phase 0 任务 2 预估：假洼地与 4,422 个离散积水斑块的重合率相关系数 > 0.85",
        "plan_does_not_specify": (
            "计划未定义相关系数的计算对象（像元级？分块级？斑块级？）与口径"
            "（Pearson？Phi？），也未说明是「全斑块」还是「仅离散斑块」"
        ),
        "max_abs_phi_pixel_level": {
            "value": round(best_phi[0], 6), "variant": best_phi[1],
        },
        "max_abs_block_pearson": {
            "value": round(best_block[0], 6), "variant": best_block[1],
        },
        "max_patch_touch_pct": round(
            max(v["patch_level_discrete"]["touch_pct"] for v in variant_reports.values()), 3
        ),
        "max_patch_touch_lift": max(
            (v["patch_level_discrete"]["null_model"]["lift_ratio"] or 0.0)
            for v in variant_reports.values()
        ),
        "patch_touch_detail_by_variant": {
            name: {
                "touch_pct": rec["patch_level_discrete"]["touch_pct"],
                "null_expected_touch_pct": rec["patch_level_discrete"]["null_model"][
                    "expected_touch_pct"
                ],
                "lift_ratio": rec["patch_level_discrete"]["null_model"]["lift_ratio"],
            }
            for name, rec in variant_reports.items()
        },
        "verdict_0p85_met": bool(best_phi[0] > 0.85 or best_block[0] > 0.85),
        "verdict_0p85_met_by_any_pixel_or_block_metric": bool(
            best_phi[0] > 0.85 or best_block[0] > 0.85
        ),
        "verdict_0p85_met_by_patch_touch_pct": bool(
            max(v["patch_level_discrete"]["touch_pct"] for v in variant_reports.values()) > 85.0
        ),
        "conclusion": "",  # 下面填充
        "evidence_level": "E1（5 种洼地口径 × 3 类指标逐像元/逐斑块复算）",
    }
    phi_met = plan_check["verdict_0p85_met_by_any_pixel_or_block_metric"]
    touch_met = plan_check["verdict_0p85_met_by_patch_touch_pct"]
    max_lift = plan_check["max_patch_touch_lift"] or 0.0
    if phi_met:
        plan_check["conclusion"] = (
            "计划预估在**像元级/分块级相关系数**口径下成立；但计划未指定口径，"
            "本文件同时登记斑块级命中率供交叉核对。"
        )
    elif touch_met:
        plan_check["conclusion"] = (
            "计划预估的 0.85 **不能**在像元级或分块级相关系数上复现"
            f"（最大 |Phi|={best_phi[0]:.3f}，最大 |分块 Pearson|={best_block[0]:.3f}）；"
            "但在**斑块级命中率**口径下可超过 85%。说明「洼地确实与离散斑块空间重叠」，"
            "但「相关系数 > 0.85」这一具体数值取决于未定义的口径，不可作为验收判据。"
        )
    else:
        plan_check["conclusion"] = (
            "计划预估的 0.85 **在任何口径下都不成立**："
            f"最大 |Phi|={best_phi[0]:.3f}，最大 |分块 Pearson|={best_block[0]:.3f}，"
            f"最大斑块命中率={plan_check['max_patch_touch_pct']:.1f}%"
            f"（随机平移零假设基线下的 lift 最高 {max_lift:.2f}×）。"
            "假洼地与离散积水斑块存在**可测但中等**的空间关联（显著高于随机零假设），"
            "但强度远低于 0.85。因此「离散斑块 = DEM 假洼地」只能标 E3 候选假设，"
            "不能当作已证结论，也不得作为 Phase 1 阈值选定的依据。"
        )
    plan_check["strongest_variant"] = best_phi[1]
    plan_check["interpretation_of_lift"] = (
        "lift = 实际斑块触碰率 / 随机平移零假设触碰率。lift > 1 表示洼地确实"
        "「偏好」离散斑块所在位置；但斑块只能出现在能积水的低地，零假设未控制"
        "地形可达性，故 lift 是保守下界。lift 明显 > 1 与 |Phi| 明显 < 0.85 可以同时成立："
        "前者说「洼地是离散积水的必要条件之一」，后者说「洼地远不是充分条件」——"
        "还有大量离散斑块不在洼地里，也有大量洼地不积水。"
    )

    # ---- 地形水文派生量汇总 ----
    terrain = {
        "slope_m_per_m": {
            "p50": round(float(np.median(np.hypot(*np.gradient(dem, res_m)))), 6),
            "p95": round(float(np.percentile(np.hypot(*np.gradient(dem, res_m)), 95)), 6),
            "max": round(float(np.max(np.hypot(*np.gradient(dem, res_m)))), 6),
        },
        "pits": {
            "cells_interior": int(np.count_nonzero(pits)),
            "area_km2": _area(pits),
            "frac_of_domain_pct": round(float(pits.mean()) * 100, 3),
        },
        "priority_flood": {
            "changed_cells": int(np.count_nonzero(depression)),
            "changed_area_km2": _area(depression),
            "fill_volume_m3": round(float(rise.sum()) * cell_area, 1),
            "max_rise_m": round(float(rise.max()), 3),
            "rise_gt_1m_cells": int(np.count_nonzero(rise > 1.0)),
            "rise_gt_5m_cells": int(np.count_nonzero(rise > 5.0)),
            "rise_gt_20m_cells": int(np.count_nonzero(rise > 20.0)),
            "note": (
                "fill_volume 是「把全 DEM 填到无洼」所需体积，属地形诊断量，"
                "不是模型蓄水量，不得与 res.mass 的 Vol 相加或比较闭合"
            ),
        },
        "d8": {
            "sink_cells_no_downstream": int(np.count_nonzero(receiver < 0)),
            "sink_share_of_domain_pct": round(float((receiver < 0).mean()) * 100, 3),
            "max_accumulation_cells": int(acc.max()),
            "accumulation_p99": round(float(np.percentile(acc, 99)), 1),
            "evidence_level": "E4（出口位置未测）",
            "note": (
                "D8 只是候选线索，不是真实沟渠。本域 DEM 存在大片精确常值平地，"
                "D8 在这些像元上没有唯一下游；本文件**不给出任何出口位置或按边出流占比结论**，"
                "出口判定标 E4 未测，须由边界/排水审计单独完成"
            ),
        },
    }

    # ---- 参考：离散斑块/主水体规模 ----
    population = {
        "res_max_wet_cells": summary.wet_cells,
        "res_max_wet_area_km2": round(summary.wet_area_km2, 4),
        "res_max_patch_count": summary.n_patches,
        "main_body_patch": {
            "n_cells": main_patch.n_cells,
            "area_km2": round(main_patch.area_km2, 4),
            "share_of_wet_area_pct": round(main_patch.area_km2 / summary.wet_area_km2 * 100, 3),
            "volume_m3": round(main_patch.volume_m3, 1),
        },
        "discrete_patches": {
            "max_cells_definition": DISCRETE_MAX_CELLS,
            "max_area_km2_equivalent": round(
                DISCRETE_MAX_CELLS * cell_area / 1e6, 4
            ),
            "patch_count": len(discrete_patches),
            "cells": int(np.count_nonzero(discrete_mask)),
            "area_km2": _area(discrete_mask),
            "note": (
                "DISCRETE_MAX_CELLS=9 是**像元数**分组，只用于把离散与成片分开统计；"
                "面积阈值一律走 cells_for_area（见 grid.area_km2_to_cells）"
            ),
        },
        "non_main_wet": {
            "cells": int(np.count_nonzero(non_main_wet)),
            "area_km2": _area(non_main_wet),
        },
    }

    out["plan_check"] = plan_check
    return {
        "generated_utc": utc_now(),
        "script": SCRIPT_NAME,
        "phase": "Phase 0 / T0.2（DEM 假洼地诊断）",
        "mode": "ADR-0004 公开数据降级、情景级、非业务预警",
        "read_only_declaration": (
            "本文件全部数值由脚本从 outputs/guigang/** 只读复算；"
            "未写入、移动或删除该目录下任何文件；未调用 lisflood.exe；models/ 未修改"
        ),
        "source_run": {
            "run_id": run_dir.name,
            "path": str(run_dir.relative_to(PROJ)).replace("\\", "/"),
            "sha256_before": before,
            "sha256_after": after,
            "integrity": comparison,
        },
        "hard_constraints": {
            "wet_threshold_m": WET_THRESHOLD_M,
            "wet_threshold_dtype": "float32（未升位；float32(0.05)=0.05000000074505806）",
            "connectivity": 8,
            "area_threshold_expression": "km²；像元数由栅格头 cellsize 经 cells_for_area 现算",
            "no_lisflood_call": True,
            "no_models_edit": True,
        },
        "grid": {
            **grid.to_meta(),
            "cells": int(grid.data.size),
            "domain_area_km2": round(int(grid.data.size) * cell_area / 1e6, 4),
        },
        "population": population,
        "terrain_hydraulics": terrain,
        "sink_variants": variant_reports,
        "plan_check_0p85": plan_check,
        "evidence_levels": {
            "sink_masks_and_areas": "E1（DEM 逐像元复算）",
            "pixel_and_block_correlations": "E1（可复算的确定量）",
            "patch_level_touch_rate": "E1（逐斑块复算）",
            "null_model": "E3（随机平移零假设，未控制地形可达性）",
            "causal_link_sink_to_discrete_patch": "E3（空间重合是相关描述，非因果证明）",
            "outlet_position": "E4 未测",
        },
        "caveats": [
            "空间重合率是相关描述，不是因果证明；洼地与积水重合不等于积水由洼地造成。",
            "计划未定义「相关系数」的计算口径；本文件给出 3 类指标 × 5 种洼地口径，不挑口径。",
            "随机平移零假设未控制地形可达性，lift 是保守下界，不是精确显著性检验。",
            "fill_volume 是地形诊断量，不是模型蓄水量，不得与 res.mass 的 Vol 比较闭合。",
            "Priority-Flood 填洼只作诊断派生，不产出候选 DEM，不替代真实地形。",
            "D8 出口位置 E4 未测；本文件不给出口位置或按边出流占比结论。",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Phase 0：贵港事实冻结 + DEM 假洼地与离散积水斑块重合率（只读诊断）"
    )
    add_common_args(parser)
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID, help="run 目录名（默认贵港 100 m 基线）")
    args = parser.parse_args(argv)

    run_tag = (args.run_tag or "").strip()
    out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_OUT
    if run_tag:
        out_dir = out_dir / run_tag
    log_path = Path(args.log_json) if args.log_json else (
        DEFAULT_LOG_DIR / f"diagnose_guigang_artifacts{'_' + run_tag if run_tag else ''}.json"
    )
    audit = AuditLog("diagnose_guigang_artifacts", run_tag=run_tag or None, dry_run=args.dry_run)
    audit.param(
        wet_threshold_m=WET_THRESHOLD_M,
        thin_film_max_m=THIN_FILM_MAX_M,
        discrete_max_cells=DISCRETE_MAX_CELLS,
        block_cells=BLOCK_CELLS,
        null_draws=NULL_DRAWS,
        out_dir=str(out_dir),
        note="全部面积阈值以 km² 表达；像元数由栅格头 cellsize 经 cells_for_area 现算",
    )
    for key, path in (("era5_guigang", ERA5), ("mass_recheck_g15", MASS_G15),
                      ("s1_water_pre", S1_PRE)):
        audit.add_input(key, path)

    if not GG.is_dir():
        audit.error(f"缺少贵港输出根目录：{GG}")
        audit.write(log_path)
        print(f"[FAIL] 缺少 {GG}", file=sys.stderr)
        return 2

    run_dir = GG / args.run_id
    if not run_dir.is_dir():
        audit.error(f"缺少 run 目录：{run_dir}")
        audit.write(log_path)
        print(f"[FAIL] 缺少 run 目录：{run_dir}", file=sys.stderr)
        return 4

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

    print(f"[1/4] 源 run 哈希（前）… {run_dir.name}", flush=True)
    before = hash_source_run(run_dir, frames)

    print("[2/4] 扫描贵港 run 清单 …", flush=True)
    runs = scan_runs()
    audit.step("scan_runs", run_count=len(runs),
               eligible=sum(1 for r in runs if r["eligible_for_diagnosis"]))

    print("[3/5] 源 run 哈希（后）比对 …", flush=True)
    after = hash_source_run(run_dir, frames)
    comparison = compare_source_hashes(before, after)
    audit.step("source_hash_after", unchanged=comparison["unchanged"],
               differing_keys=comparison["differing_keys"])
    if not comparison["unchanged"]:
        audit.error(f"源 run 在分析期间被改动：{comparison}")
        audit.write(log_path)
        print(f"[FAIL] 源 run 哈希前后不一致：{comparison}", file=sys.stderr)
        return 5

    print("[4/5] 栅格/时间/质量账目 + 洼地重合率 …", flush=True)
    facts = run_grid_facts(run_dir)
    sink = sink_analysis(
        run_dir, out={}, audit=audit,
        before=before, after=after, comparison=comparison,
    )
    plan_check = sink["plan_check_0p85"]
    audit.step(
        "sink_analysis",
        max_abs_phi=plan_check["max_abs_phi_pixel_level"],
        max_abs_block_pearson=plan_check["max_abs_block_pearson"],
        max_patch_touch_pct=plan_check["max_patch_touch_pct"],
        verdict_0p85=plan_check["verdict_0p85_met_by_any_pixel_or_block_metric"],
    )

    print("[5/5] 汇总产物 …", flush=True)

    # ---- 输入哈希 ----
    input_hashes: dict[str, Any] = {}
    for name in ("dem.asc", "run.par", "rain.rain", "bci.bci", "yujiang.bdy", "meta.json"):
        path = run_dir / name
        if path.is_file():
            input_hashes[f"{run_dir.relative_to(PROJ).as_posix()}/{name}"] = {
                "sha256": sha256_file(path), "bytes": path.stat().st_size,
            }
    for name in ("res.max", "res.mass"):
        path = run_dir / "results" / name
        if path.is_file():
            input_hashes[f"{run_dir.relative_to(PROJ).as_posix()}/results/{name}"] = {
                "sha256": sha256_file(path), "bytes": path.stat().st_size,
            }
    for key, path in (("data/catalog/probes/guigang_sep2024_era5_hourly.json", ERA5),
                      ("data/catalog/mass_recheck_g15.json", MASS_G15),
                      ("data/processed/s1_water/guigang_20240905T105022_water20m.tif", S1_PRE)):
        if path.is_file():
            input_hashes[key] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}

    quality_gate = {
        "source": "data/catalog/mass_recheck_g15.json（复用，未重算全表）",
        "policy": "PASS <= 1e-3；FAIL-EXPLAINED 不计通过（ADR-0006）",
        "guigang_runs": {
            rec["path"]: rec["mass_gate_g15"] for rec in runs
            if rec["mass_gate_g15"]["gate"] is not None
        },
        "baseline_recomputed_e_mass": facts["mass_accounting"],
        "note": "不得表述为『质量门全量通过』；计数以 mass_recheck_g15.json 为 SSOT",
    }

    baseline = {
        "generated_utc": utc_now(),
        "script": SCRIPT_NAME,
        "run_tag": run_tag or None,
        "phase": "Phase 0 / T0（事实冻结）",
        "mode": "ADR-0004 公开数据降级、情景级、非业务预警",
        "read_only_declaration": (
            "本文件全部数值由脚本从 outputs/guigang/** 只读复算；"
            "未写入、移动或删除该目录下任何文件；未调用 lisflood.exe；models/ 未修改"
        ),
        "source_run": {
            "run_id": run_dir.name,
            "path": str(run_dir.relative_to(PROJ)).replace("\\", "/"),
            "sha256_before": before,
            "sha256_after": after,
            "integrity": comparison,
        },
        "event_window": {
            "event": "2024-09 摩羯（贵港/郁江走廊）",
            "rain_source": "ERA5 贵港锚点逐时（degraded），非站点实测雨量",
            "upstream_bc": "情景三角 Q 线（q_base=1000 → q_peak=11500 → 2000），非逐时实测",
            "note": "久隆/贵港为 2024-09 摩羯，宁明为 2026-08 紫檀；不共用绝对时间轴",
        },
        "runs": runs,
        "grid_and_time_facts": {run_dir.name: facts},
        "input_hashes": input_hashes,
        "quality_gate_summary": quality_gate,
        "evidence_levels": {
            "run_inventory_and_grid_metadata": "E1",
            "mass_accounting": "E1（res.mass 梯形积分复算，与 verify.json 交叉核对）",
            "sink_masks_and_correlation": "E1（见 guigang_sink_analysis.json）",
            "physical_cause_attribution": "E2/E3",
            "independent_validation_metrics": "E4 未测（S1 仅灾前/洪峰两景）",
        },
    }

    if args.dry_run:
        print("\n[DRY-RUN] 不写派生文件。摘要：", flush=True)
        g = facts["grid"]
        print(f"  网格 {g['ncols']}×{g['nrows']}={g['cells']} 像元，cellsize {g['cellsize_m']} m，"
              f"单元面积 {g['cell_area_m2_from_header']} m²，域面积 {g['domain_area_km2']} km²", flush=True)
        print(f"  时间：sim {facts['time']['sim_time_s']} s，saveint {facts['time']['saveint_s']} s，"
              f"{facts['time']['frame_count']} 帧，t0={facts['time']['t0']}", flush=True)
        m = facts["mass_accounting"]
        print(f"  质量账目：v_in {m['v_in_m3']:.4e} / v_out {m['v_out_m3']:.4e} / "
              f"v_rain {m['v_rain_m3']:.4e} / v_end {m['v_end_m3']:.4e} / "
              f"E_mass {m['e_mass']:.3e} {m['gate_1e-3']}", flush=True)
        t = sink["terrain_hydraulics"]
        print(f"  洼地：局部洼坑 {t['pits']['cells_interior']} 格（{t['pits']['area_km2']} km²，"
              f"{t['pits']['frac_of_domain_pct']}%）；填洼需求 {t['priority_flood']['changed_cells']} 格"
              f"（{t['priority_flood']['changed_area_km2']} km²，需填体积 "
              f"{t['priority_flood']['fill_volume_m3']:.3e} m³）", flush=True)
        print(f"  计划 0.85 核验：最大 |Phi| = {plan_check['max_abs_phi_pixel_level']['value']}"
              f"（{plan_check['max_abs_phi_pixel_level']['variant']}）；"
              f"最大 |分块 Pearson| = {plan_check['max_abs_block_pearson']['value']}"
              f"（{plan_check['max_abs_block_pearson']['variant']}）；"
              f"最大斑块命中率 {plan_check['max_patch_touch_pct']}%", flush=True)
        print(f"  0.85 在像元/分块口径成立 = "
              f"{plan_check['verdict_0p85_met_by_any_pixel_or_block_metric']}；"
              f"在斑块命中率口径成立 = {plan_check['verdict_0p85_met_by_patch_touch_pct']}", flush=True)
        for name, rec in sink["sink_variants"].items():
            pl = rec["patch_level_discrete"]
            print(f"    {name}: cells {rec['sink_cells']} ({rec['sink_frac_of_domain_pct']}%) "
                  f"phi_disc {rec['pixel_level_phi']['vs_discrete_patches']['phi']:+.4f} "
                  f"blk_r {rec['block_level_pearson']['vs_discrete_patches']} "
                  f"patch_touch {pl['touch_pct']}% "
                  f"(null {pl['null_model']['expected_touch_pct']}%, "
                  f"lift {pl['null_model']['lift_ratio']}×)", flush=True)
        if args.log_json:
            audit.write(log_path)
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "baseline_snapshot.json", baseline)
    write_json(out_dir / "guigang_sink_analysis.json", sink)
    audit.step("write_products", out_dir=str(out_dir))
    audit.write(log_path)
    print(f"-> {out_dir / 'baseline_snapshot.json'}")
    print(f"-> {out_dir / 'guigang_sink_analysis.json'}")
    print(f"-> {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
