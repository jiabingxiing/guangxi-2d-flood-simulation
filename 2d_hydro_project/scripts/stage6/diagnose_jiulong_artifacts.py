"""Phase 0 / T0+T1：久隆事实冻结、DEM 异常分类与分辨率对照（只读诊断）。

对应计划（`plans/久隆水动力推演效果改进实施计划_从伪影诊断到全链条治理.md`）：

- §5 Phase 0 任务 1–2、5–8：固定 200 m 生产基线与 100 m 敏感性基线；复核栅格元数据、
  实际单元面积、输出时间与事件窗口；叠合 BFS/负值处理区；复核 12.5→100/200 m 重采样
  对坡度、局部坑洼、D8 和出口的影响；退水期逐项对账；形成分辨率决策单输入。
- §1.4 / §3.4：200 m 是生产基线，100 m 仅为敏感性试验；跨分辨率比较必须使用**同一物理
  面积阈值**（km²），单元面积一律从栅格头 `cellsize` 读取，禁止写死 0.04 km²。
- §2.4：所有结论必须带证据等级 E1–E4。

本脚本**只读** `outputs/jiulong/**`，不写入、不移动、不删除其中任何文件；不调用
`lisflood.exe`。全部派生产物写入 `diagnostics/jiulong/`。

用法（先设 `$env:PYTHONUTF8="1"`）::

    uv --directory 2d_hydro_project run python scripts/stage6/diagnose_jiulong_artifacts.py --dry-run
    uv --directory 2d_hydro_project run python scripts/stage6/diagnose_jiulong_artifacts.py --run-tag phase0-v1

产出（默认目录 `diagnostics/jiulong/`）：
    baseline_snapshot.json      run 清单、栅格元数据、单元面积、时间口径、输入哈希、
                                质量门状态、DEM 分类图例、退水期逐项对账
    dem_artifact_map.tif        DEM 异常分类栅格（5 波段，见 CLASS_LEGEND）
    resolution_comparison.json  200 m vs 100 m 同物理面积阈值对照 + 地形水文派生量

`--dry-run` 只计算并打印，不写任何派生文件（含审计 JSON）；如需在试算时留痕，
显式给出 `--log-json`。
"""
from __future__ import annotations

import argparse
import heapq
import json
import sys
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import (  # noqa: E402
    AsciiGrid,
    AuditLog,
    GridError,
    add_common_args,
    guard_no_overwrite,
    load_sea_mask,
    parse_run_par,
    read_ascii_grid,
    run_frames,
    sha256_file,
    utc_now,
    write_json,
)
from scripts.common.patch_analysis import summarize_frame  # noqa: E402

JIU = PROJ / "outputs" / "jiulong" / "2024-09-yagi"
DEM12 = PROJ / "data" / "interim" / "jiulong" / "dem_12p5m_32649.tif"
S1_WATER = PROJ / "data" / "processed" / "s1_water"
MASS_G15 = PROJ / "data" / "catalog" / "mass_recheck_g15.json"
SCEN_SUMMARY = PROJ / "data" / "catalog" / "jiulong_scenarios_summary.json"
RECESSION_S1 = PROJ / "data" / "catalog" / "jiulong_recession_s1.json"
DEFAULT_OUT = PROJ / "diagnostics" / "jiulong"
DEFAULT_LOG_DIR = PROJ / "logs" / "diagnostics"
MANIFEST = PROJ / "data" / "catalog" / "jiulong_improvement_manifest.json"

# 与 scripts/stage3/jiulong_scenarios.py 的 AOI 完全一致（该模块 import 时会解析 sys.argv，
# 不能直接 import；此处复制常量并在下方用栅格头逐项校验，不一致即报错，不静默降级）。
AOI = (108.55, 21.92, 108.90, 22.20)

# 湿地判定阈值：与 verify.json / jiulong_recession_s1.py 同口径（depth > 0.05 m）。
WET_THRESHOLD_M = 0.05
# 物理面积阈值（km²）。计划 §1.4 / §3.3：**只用物理面积**，像元数由栅格头换算。
AREA_THRESHOLDS_KM2 = (0.04, 0.16, 0.36, 1.0, 4.0)
# 平地（精确常值连通区）最小物理面积；像元数按 cellsize 换算，不写死。
PLATEAU_MIN_KM2 = 1.0
# 细薄水膜上限：输出格式量化到 0.001 m（output.cpp:686 `%.3f`），故 (0, 0.005] 视作薄层。
THIN_FILM_MAX_M = 0.005
# 站址邻域参考半径（米）。仅邻域参考，不是资产级水深判据（计划 §1.2 站址行）。
ANCHOR_LONLAT = (108.7161, 22.0638)
RADII_M = (1000.0, 2000.0)

CLASS_LEGEND = {
    0: "UNCHANGED_VALID",
    1: "SEA_CLAMPED_ZERO",
    2: "NEGATIVE_BFS_FILLED",
    3: "SOURCE_NODATA_FILLED",
    4: "UNRESOLVED_NEGATIVE",
    5: "NODATA_IN_MODEL_DEM",
}
NB8 = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def reconstruct_raw_dem(res_m: float, shape: tuple[int, int], header: dict[str, float]) -> np.ndarray:
    """按 jiulong_scenarios.prep() 的完全相同口径重建「海面钳零/BFS 填补之前」的地形。

    做法：12.5 m 源 DEM（EPSG:32649）经 WarpedVRT 双线性重采样到 AOI 的
    `int((b2-b0)/res) × int((b3-b1)/res)` 网格。若与 run 内 `dem.asc` 的
    shape / 原点 / cellsize 不一致，直接抛 GridError（口径必须唯一）。
    """
    import rasterio
    from pyproj import Transformer
    from rasterio.enums import Resampling
    from rasterio.vrt import WarpedVRT

    tf = Transformer.from_crs(4326, 32649, always_xy=True)
    corners = [tf.transform(x, y) for x, y in [(AOI[0], AOI[1]), (AOI[2], AOI[3])]]
    bounds = (
        min(c[0] for c in corners), min(c[1] for c in corners),
        max(c[0] for c in corners), max(c[1] for c in corners),
    )
    width = int((bounds[2] - bounds[0]) / res_m)
    height = int((bounds[3] - bounds[1]) / res_m)
    if (height, width) != tuple(shape):
        raise GridError(
            f"重建网格 {height}×{width} 与 run dem.asc {tuple(shape)} 不符；"
            "AOI/RES 口径已漂移，禁止继续比较"
        )
    # dem.asc 头由 prep() 以 %.1f 写出（jiulong_scenarios.py:226-229），原点精度只有 0.05 m，
    # 故原点容差取 0.1 m（半量化步长），cellsize 容差取 1e-6。
    for key, expected, tol in (
        ("xllcorner", bounds[0], 0.1),
        ("yllcorner", bounds[1], 0.1),
        ("cellsize", res_m, 1e-6),
    ):
        got = float(header[key])
        if abs(got - expected) > tol:
            raise GridError(f"dem.asc 头 {key}={got} 与重建值 {expected:.4f} 不符（容差 {tol}）")
    transform = rasterio.Affine(res_m, 0, bounds[0], 0, -res_m, bounds[3])
    with rasterio.open(DEM12) as src:
        with WarpedVRT(
            src, crs="EPSG:32649", resampling=Resampling.bilinear,
            width=width, height=height, transform=transform,
        ) as vrt:
            raw = vrt.read(1).astype("float32")
    return raw


def constant_plateaus(arr: np.ndarray, tol: float = 1e-6) -> tuple[np.ndarray, np.ndarray]:
    """标记「精确常值连通区」（8 邻域）。

    这是「规则几何色块」的地形侧来源：海面钳零与 BFS 填补都会把大片像元压到同一常数，
    在雨驱动平地上形成完全均匀的薄层水面。返回 (labels, sizes_per_label)，
    sizes[0] 恒为 0（背景不参与）。
    """
    height, width = arr.shape
    labels = np.zeros((height, width), dtype=np.int32)
    label = 0
    for r0 in range(height):
        for c0 in range(width):
            if labels[r0, c0]:
                continue
            value = float(arr[r0, c0])
            label += 1
            labels[r0, c0] = label
            queue = deque([(r0, c0)])
            while queue:
                cr, cc = queue.popleft()
                for dr, dc in NB8:
                    nr, nc = cr + dr, cc + dc
                    if 0 <= nr < height and 0 <= nc < width and not labels[nr, nc]:
                        if abs(float(arr[nr, nc]) - value) <= tol:
                            labels[nr, nc] = label
                            queue.append((nr, nc))
    sizes = np.bincount(labels.ravel(), minlength=label + 1).astype(np.int64)
    sizes[0] = 0
    return labels, sizes


def flat_neighbourhood(dem: np.ndarray, tol: float = 1e-6) -> np.ndarray:
    """像元的 8 邻域是否全部与自身等高（无任何坡度可驱动 D8 流向）。"""
    height, width = dem.shape
    pad = np.pad(dem.astype(np.float64), 1, mode="edge")
    flat = np.ones((height, width), dtype=bool)
    for dr, dc in NB8:
        nb = pad[1 + dr:1 + dr + height, 1 + dc:1 + dc + width]
        flat &= np.abs(nb - dem) <= tol
    return flat


def pit_mask(dem: np.ndarray, *, include_border: bool = True) -> np.ndarray:
    """局部洼坑（8 邻域内不高于任一邻居）。

    `include_border=False` 时排除最外一圈像元：边界像元用 edge padding 填充后天然
    满足「不高于邻居」，会把边界算成洼坑并虚增计数。诊断洼坑应使用 interior 口径。
    """
    height, width = dem.shape
    pad = np.pad(dem.astype(np.float64), 1, mode="edge")
    pits = np.ones((height, width), dtype=bool)
    for dr, dc in NB8:
        nb = pad[1 + dr:1 + dr + height, 1 + dc:1 + dc + width]
        pits &= dem <= nb
    if not include_border:
        pits[0, :] = False
        pits[-1, :] = False
        pits[:, 0] = False
        pits[:, -1] = False
    return pits


def priority_flood(dem: np.ndarray) -> np.ndarray:
    """Priority-Flood 填洼（纯 numpy + heapq，环境无 scipy/pysheds）。

    只用于**诊断**地形水文派生量（计划 §3.2：DEM_HYDRO 才允许填洼，且不得直接替代
    DEM_HYDRAULIC）。边界像元为种子，按高程升序扩散。
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


def read_mass(run_dir: Path) -> tuple[list[dict[str, float]], dict[str, float]]:
    """读 res.mass 并按 G15 口径复算 E_mass（E1 可复现账目）。"""
    path = Path(run_dir) / "results" / "res.mass"
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    keys = lines[0].split()
    rows = [dict(zip(keys, (float(x) for x in ln.split()))) for ln in lines[1:] if ln.split()]
    if not rows:
        raise GridError(f"{path} 无数据行")
    v_in = sum(0.5 * (a["Qin"] + b["Qin"]) * (b["Time"] - a["Time"]) for a, b in zip(rows, rows[1:]))
    v_out = sum(0.5 * (a["Qout"] + b["Qout"]) * (b["Time"] - a["Time"]) for a, b in zip(rows, rows[1:]))
    v0, v_end = rows[0]["Vol"], rows[-1]["Vol"]
    v_rain = rows[-1]["Rain-(Inf+Evap)"]
    residual = v_end - v0 - v_rain - (v_in - v_out)
    denom = max(v0 + v_rain + v_in, 1.0)
    return rows, {
        "v0_m3": v0,
        "v_end_m3": v_end,
        "v_rain_m3": v_rain,
        "v_in_m3": v_in,
        "v_out_m3": v_out,
        "residual_m3": residual,
        "e_mass": float(f"{abs(residual) / denom:.4e}"),
    }


def boundary_control(run_dir: Path) -> dict[str, Any]:
    """解析 bci.bci 的四边边界类型（出口口径）。"""
    path = Path(run_dir) / "bci.bci"
    edges: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[0] in ("N", "S", "E", "W"):
                edges[parts[0]] = parts[3]
    kinds = sorted(set(edges.values()))
    return {
        "edges": edges,
        "free_edge_count": sum(1 for v in edges.values() if v.startswith("FREE")),
        "kinds": kinds,
        "note": "FREE 四边均可能出流；Qout 是四边之和（boundary.cpp:1330-1378），不是仅南缘",
    }


# --------------------------------------------------------------------------- #
# run 清单
# --------------------------------------------------------------------------- #
def classify_run_role(name: str) -> tuple[str, str]:
    """按目录名给出角色与是否可入诊断主表。"""
    if "_v1invalid" in name or "_v1_polyfitbed" in name:
        return "excluded_variant", "作废的 v1 DEM/河槽变体，不参与诊断与质量门"
    if name.endswith("_cleaned"):
        return "non_physical_derived", (
            "后处理滤杂产物（clean_audit.json 声明 ARR 2019 / UK EA 规则），"
            "不是物理模拟结果，本 Phase 只作对照，不得称为物理结果"
        )
    if name == "scen_free_200m_sv6h":
        return "production_baseline_200m", "200 m 生产基线（21 帧，saveint=21600）"
    if name == "scen_free_200m_sv6h_recession":
        return "production_recession_200m", "200 m 退水延长情景（53 帧，sim=1123200）"
    if name == "scen_free_100m_dt2p5_sv6h_realdem":
        return "sensitivity_100m", "100 m 敏感性试验（21 帧，dt=2.5 s）"
    if name == "scen_free_200m":
        return "single_frame_baseline_200m", "200 m 单帧基线（saveint=sim_time）"
    if name.startswith("scen_z"):
        return "boundary_scenario_200m", "下游边界情景（HFIX/HVAR）"
    return "other", "未归类的 run 目录"


REQUIRED_RUN_FILES = ("dem.asc", "run.par", "meta.json", "sea_mask.npy", "bci.bci", "rain.rain")


def scan_runs() -> list[dict[str, Any]]:
    """扫描全部久隆 run 目录，记录角色、完整性与关键元数据。"""
    g15 = {}
    if MASS_G15.is_file():
        doc = json.loads(MASS_G15.read_text(encoding="utf-8"))
        g15 = doc.get("runs", {})
    records: list[dict[str, Any]] = []
    if not JIU.is_dir():
        return records
    for run_dir in sorted(p for p in JIU.iterdir() if p.is_dir()):
        name = run_dir.name
        role, role_note = classify_run_role(name)
        missing = [f for f in REQUIRED_RUN_FILES if not (run_dir / f).is_file()]
        if not (run_dir / "run.log").is_file():
            missing.append("run.log")
        mass_path = run_dir / "results" / "res.mass"
        if not mass_path.is_file():
            missing.append("results/res.mass")
        meta = {}
        meta_path = run_dir / "meta.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                meta = {}
        par = parse_run_par(run_dir)
        frames = run_frames(run_dir / "results")
        verify = {}
        verify_path = run_dir / "verify.json"
        if verify_path.is_file():
            try:
                verify = json.loads(verify_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                verify = {}
        rel = f"outputs/jiulong/2024-09-yagi/{name}"
        gate = g15.get(rel, {})
        eligible = role not in ("excluded_variant",) and not missing
        records.append({
            "run_id": name,
            "path": rel,
            "role": role,
            "role_note": role_note,
            "eligible_for_diagnosis": eligible,
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
                "sea_px": meta.get("sea_px"),
                "s1_water_px": meta.get("s1_water_px"),
                "filled_px": meta.get("filled_px"),
                "unfilled_px": meta.get("unfilled_px"),
                "dem_v2": meta.get("dem_v2"),
            },
            "verify_json": {
                "wet_area_km2": verify.get("wet_area_km2"),
                "final_vol_m3": verify.get("final_vol_m3"),
                "e_mass_132": verify.get("e_mass_132"),
            },
            "mass_gate_g15": {
                "gate": gate.get("gate_1e-3"),
                "e_mass": gate.get("e_mass_132"),
                "cause": gate.get("cause"),
                "excluded_from_pass_count": gate.get("excluded_from_pass_count"),
            },
            "boundary": boundary_control(run_dir),
        })
    return records


# --------------------------------------------------------------------------- #
# 单 run 事实：栅格、时间、DEM 分类
# --------------------------------------------------------------------------- #
def _overlap_with_max_frame(
    run_dir: Path,
    grid: AsciiGrid,
    sea: np.ndarray,
    edited: np.ndarray,
    big_plateau: np.ndarray,
    flat8: np.ndarray,
    negatives: np.ndarray,
) -> dict[str, Any]:
    """把 DEM 处理区与 res.max 湿区叠合（计划 §5 Phase 0 任务 4）。

    只做面积交叠统计，不做因果断言；因果归因写在 root_cause_report.md 并标证据等级。
    """
    path = run_dir / "results" / "res.max"
    if not path.is_file():
        return {"available": False, "reason": "缺少 results/res.max"}
    frame = read_ascii_grid(path)
    if frame.shape != grid.shape:
        raise GridError(f"{path} 形状 {frame.shape} 与 DEM {grid.shape} 不符")
    depth = frame.data
    cell_area = grid.cell_area_m2
    wet = np.isfinite(depth) & (depth > WET_THRESHOLD_M) & ~sea
    thin = np.isfinite(depth) & (depth > 0) & (depth <= THIN_FILM_MAX_M) & ~sea

    def pct(mask: np.ndarray) -> float:
        return round(float(mask.sum()) / max(int(wet.sum()), 1) * 100, 2)

    def area_km2(mask: np.ndarray) -> float:
        return round(float(mask.sum()) * cell_area / 1e6, 4)

    return {
        "available": True,
        "wet_threshold_m": WET_THRESHOLD_M,
        "wet_cells": int(wet.sum()),
        "wet_area_km2": area_km2(wet),
        "edited_share_of_wet_pct": pct(wet & edited),
        "edited_wet_area_km2": area_km2(wet & edited),
        "plateau_share_of_wet_pct": pct(wet & big_plateau),
        "plateau_wet_area_km2": area_km2(wet & big_plateau),
        "flat8_share_of_wet_pct": pct(wet & flat8),
        "flat8_wet_area_km2": area_km2(wet & flat8),
        "negative_filled_share_of_wet_pct": pct(wet & negatives),
        "thin_film_cells": int(thin.sum()),
        "thin_film_area_km2": area_km2(thin),
        "thin_film_share_of_domain_pct": round(
            float(thin.sum()) / max(int((~sea).sum()), 1) * 100, 2
        ),
        "note": (
            "交叠率是相关描述，不是因果证明；同一像元可同时属于多个处理层，"
            "各百分比不可相加"
        ),
    }


def run_facts(run_dir: Path) -> dict[str, Any]:
    """单 run 的栅格元数据、时间口径与 DEM 分类统计。"""
    grid = read_ascii_grid(run_dir / "dem.asc")
    header = dict(grid.header)
    res_m = grid.cell_size_m
    sea = load_sea_mask(run_dir, grid.shape)
    if sea is None:
        raise GridError(f"{run_dir} 缺 sea_mask.npy；该 run 不参与诊断主表")
    raw = reconstruct_raw_dem(res_m, grid.shape, header)
    model = grid.data

    sea_cells = int(sea.sum())
    source_nodata = (raw == np.float32(-9999.0)) & ~sea
    negatives = (raw < 0) & ~sea & ~source_nodata
    # dem.asc 由 prep() 以 %.3f 写出（jiulong_scenarios.py:226-229），即高程量化到 0.001 m，
    # 未改写像元与重建 raw 的差最大 0.0005 m。容差必须取量化步长（1e-3）：
    # 用 1e-6 会把舍入差当改写（实测虚报 3.5 倍，200 m 由 313.6 km² 虚增到 1103.6 km²）。
    edit_tol_m = 1e-3
    diff = np.abs(model.astype(np.float64) - raw.astype(np.float64))
    changed = diff > edit_tol_m
    # 容差敏感性：供报告说明「改写面积」对容差的依赖（E1）
    edit_tolerance_sensitivity = {
        f"{tol:g}": int((diff > tol).sum()) for tol in (0.0, 1e-6, 5e-4, 1e-3, 1e-2)
    }

    classes = np.zeros(grid.shape, dtype=np.uint16)
    classes[changed] = 2
    classes[sea] = 1
    classes[source_nodata] = 3
    classes[model < -1e-6] = 4
    classes[model == np.float32(-9999.0)] = 5

    plateau_labels, plateau_sizes = constant_plateaus(model.astype(np.float64))
    min_plateau_cells = max(1, int(np.ceil(PLATEAU_MIN_KM2 * 1e6 / grid.cell_area_m2)))
    big_plateau = np.isin(plateau_labels, np.flatnonzero(plateau_sizes >= min_plateau_cells))
    flat8 = flat_neighbourhood(model.astype(np.float64))

    par = parse_run_par(run_dir)
    saveint = float(par.get("saveint") or 0.0)
    sim = float(par.get("sim_time") or 0.0)
    frames = run_frames(run_dir / "results")
    timeline = [
        {"seq": seq, "t_s": seq * saveint, "t_h": round(seq * saveint / 3600.0, 3)}
        for seq, _ in frames
    ]
    # 重建校验：meta.json 声明的 filled_px + sea_px 必须等于本脚本重算的改写像元数。
    meta = {}
    meta_path = run_dir / "meta.json"
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    declared = None
    if meta.get("filled_px") is not None and meta.get("sea_px") is not None:
        declared = int(meta["filled_px"]) + int(meta["sea_px"])
    reconstruction_ok = (declared is None) or (declared == int(changed.sum()))

    cells = int(model.size)
    cells_area = cells * grid.cell_area_m2 / 1e6
    return {
        "run_id": run_dir.name,
        "grid": {
            **grid.to_meta(),
            "cells": cells,
            "domain_area_km2": round(cells * grid.cell_area_m2 / 1e6, 4),
            "cell_area_m2_from_header": grid.cell_area_m2,
            "area_0p04_km2_in_cells": grid.cells_for_area(0.04),
            "area_0p36_km2_in_cells": grid.cells_for_area(0.36),
        },
        "time": {
            "sim_time_s": sim,
            "saveint_s": saveint,
            "sim_hours": round(sim / 3600.0, 3),
            "frame_count": len(frames),
            "timeline": timeline,
            "beyond_sim_end": [t["seq"] for t in timeline if sim and t["t_s"] > sim + 1e-6],
        },
        "dem_classification": {
            "min_plateau_area_km2": PLATEAU_MIN_KM2,
            "min_plateau_cells": min_plateau_cells,
            "cells": {
                "total": cells,
                "unchanged_valid": int((classes == 0).sum()),
                "sea_clamped_zero": sea_cells,
                "negative_bfs_filled": int((classes == 2).sum()),
                "source_nodata_filled": int((classes == 3).sum()),
                "unresolved_negative": int((classes == 4).sum()),
                "nodata_in_model_dem": int((classes == 5).sum()),
            },
            "area_km2": {
                "sea_clamped_zero": round(sea_cells * grid.cell_area_m2 / 1e6, 4),
                "negative_bfs_filled": round(int((classes == 2).sum()) * grid.cell_area_m2 / 1e6, 4),
                "raw_negative_outside_sea": round(int(negatives.sum()) * grid.cell_area_m2 / 1e6, 4),
                "raw_negative_including_sea": round(
                    int((raw < 0).sum()) * grid.cell_area_m2 / 1e6, 4
                ),
                "edited_total_including_sea": round(int(changed.sum()) * grid.cell_area_m2 / 1e6, 4),
                "domain_total": round(cells_area, 4),
            },
            "raw_dem": {
                "negative_cells": int(negatives.sum()),
                "negative_frac_of_domain": round(float(negatives.mean()), 6),
                "min_m": float(np.min(raw)),
                "p50_m": float(np.median(raw)),
                "p95_m": float(np.percentile(raw, 95)),
                "max_m": float(np.max(raw)),
            },
            "model_dem": {
                "min_m": float(np.min(model)),
                "zero_cells": int((np.abs(model) < 1e-6).sum()),
                "p50_m": float(np.median(model)),
                "p95_m": float(np.percentile(model, 95)),
                "max_m": float(np.max(model)),
            },
            "plateaus": {
                "region_count": int(plateau_sizes.size - 1),
                "regions_at_or_above_min": int((plateau_sizes >= min_plateau_cells).sum()),
                "cells_at_or_above_min": int(plateau_sizes[plateau_sizes >= min_plateau_cells].sum()),
                "area_at_or_above_min_km2": round(
                    float(plateau_sizes[plateau_sizes >= min_plateau_cells].sum()) * grid.cell_area_m2 / 1e6, 4
                ),
                "largest_region_cells": int(plateau_sizes.max()) if plateau_sizes.size > 1 else 0,
                "largest_region_area_km2": round(
                    float(plateau_sizes.max()) * grid.cell_area_m2 / 1e6, 4
                ) if plateau_sizes.size > 1 else 0.0,
                "largest_region_elevation_m": (
                    float(model[plateau_labels == int(np.argmax(plateau_sizes))][0])
                    if plateau_sizes.size > 1 else None
                ),
            },
            "flat_neighbourhood_cells": int(flat8.sum()),
            "flat_neighbourhood_area_km2": round(
                int(flat8.sum()) * grid.cell_area_m2 / 1e6, 4
            ),
            "max_frame_overlap": _overlap_with_max_frame(
                run_dir, grid, sea, changed, big_plateau, flat8, negatives
            ),
            "reconstruction_check": {
                "declared_filled_plus_sea_px": declared,
                "recomputed_edited_px": int(changed.sum()),
                "edit_tolerance_m": edit_tol_m,
                "ok": reconstruction_ok,
                "note": (
                    "meta.json filled_px+sea_px 必须等于重算的改写像元数，否则口径漂移；"
                    "容差取 dem.asc 的 %.3f 输出量化步长 0.001 m"
                ),
            },
            "edit_tolerance_sensitivity_cells": edit_tolerance_sensitivity,
        },
        "artifact_layers": {
            "classes": classes,
            "plateau_sizes": plateau_sizes,
            "plateau_labels": plateau_labels,
            "big_plateau": big_plateau,
            "flat8": flat8,
            "sea": sea,
        },
    }


def write_artifact_map(
    path: Path, facts: dict[str, Any], max_frame_depth: np.ndarray | None
) -> dict[str, Any]:
    """写 dem_artifact_map.tif（5 波段 uint16）并返回波段说明。"""
    import rasterio
    from rasterio.transform import from_origin

    layers = facts["artifact_layers"]
    grid = facts["grid"]
    res_m = grid["cellsize_m"]
    classes = layers["classes"].astype(np.uint16)
    sizes = layers["plateau_sizes"]
    labels = layers["plateau_labels"]
    min_cells = facts["dem_classification"]["min_plateau_cells"]
    big = np.isin(labels, np.flatnonzero(sizes >= min_cells))
    plateau_cells = np.where(big, sizes[labels], 0)
    plateau_cells = np.clip(plateau_cells, 0, 65535).astype(np.uint16)
    flat8 = layers["flat8"].astype(np.uint16)
    if max_frame_depth is None:
        thin = np.zeros(classes.shape, dtype=np.uint16)
        quant = np.zeros(classes.shape, dtype=np.uint16)
    else:
        thin = ((max_frame_depth > 0) & (max_frame_depth <= THIN_FILM_MAX_M)).astype(np.uint16)
        quant = np.clip(np.round(np.clip(max_frame_depth, 0, 65.535) * 1000.0), 0, 65535).astype(np.uint16)

    height, width = classes.shape
    transform = from_origin(
        float(grid["xllcorner"]), float(grid["yllcorner"]) + height * res_m, res_m, res_m
    )
    bands = (classes, plateau_cells, flat8, thin, quant)
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path, "w", driver="GTiff", height=height, width=width, count=len(bands),
        dtype="uint16", crs="EPSG:32649", transform=transform, compress="lzw",
    ) as dst:
        for idx, band in enumerate(bands, start=1):
            dst.write(band, idx)
        dst.set_band_description(1, "dem_class_code")
        dst.set_band_description(2, "constant_plateau_cells")
        dst.set_band_description(3, "flat_neighbourhood")
        dst.set_band_description(4, "thin_film_le_5mm")
        dst.set_band_description(5, "max_depth_mm_quantised")
    return {
        "path": str(path),
        "crs": "EPSG:32649",
        "shape": [height, width],
        "cellsize_m": res_m,
        "bands": {
            "1": "dem_class_code（取值见 class_legend）",
            "2": "constant_plateau_cells（该像元所在精确常值连通区的像元数；0=低于阈值）",
            "3": "flat_neighbourhood（8 邻域全部等高=1）",
            "4": f"thin_film_le_5mm（res.max 水深在 (0, {THIN_FILM_MAX_M}] m）",
            "5": "max_depth_mm_quantised（res.max 水深×1000 取整，用于显示 0.001 m 量化台阶）",
        },
        "class_legend": {str(k): v for k, v in CLASS_LEGEND.items()},
    }


# --------------------------------------------------------------------------- #
# 退水期逐项对账
# --------------------------------------------------------------------------- #
def recession_accounting(run_dir: Path) -> dict[str, Any]:
    """退水期水量/湿面积/降雨/出口/边界逐项对账（计划 §5 Phase 0 任务 7）。"""
    grid = read_ascii_grid(run_dir / "dem.asc")
    sea = load_sea_mask(run_dir, grid.shape)
    cell_area = grid.cell_area_m2
    par = parse_run_par(run_dir)
    saveint = float(par.get("saveint"))
    rows, gate = read_mass(run_dir)
    frames = run_frames(run_dir / "results")

    series = []
    for seq, path in frames:
        frame = read_ascii_grid(path)
        if frame.shape != grid.shape:
            raise GridError(f"{path} 形状 {frame.shape} 与 DEM {grid.shape} 不符")
        wet = np.isfinite(frame.data) & (frame.data > WET_THRESHOLD_M) & ~sea
        volume = float(np.sum(np.where(wet, frame.data, 0.0))) * cell_area
        series.append({
            "seq": seq,
            "t_h": round(seq * saveint / 3600.0, 3),
            "wet_cells": int(wet.sum()),
            "wet_area_km2": round(int(wet.sum()) * cell_area / 1e6, 4),
            "wet_volume_m3": round(volume, 1),
            "max_depth_m": round(float(frame.data[wet].max()), 4) if wet.any() else 0.0,
            "thin_film_cells": int(((frame.data > 0) & (frame.data <= THIN_FILM_MAX_M)).sum()),
        })
    by_seq = {rec["seq"]: rec for rec in series}

    # 09-10T00 = 5 天窗末；09-17 过境帧取 S1 对照帧（与 jiulong_recession_s1.py 同口径）
    seq_0910 = int(round(5 * 86400 / saveint))
    t_overpass = (17 - 5) * 86400 + 18 * 3600 + 50 * 60 + 23
    seq_overpass = min((s for s, _ in frames), key=lambda s: abs(s * saveint - t_overpass), default=None)

    rain_cum = {r["Time"]: r["Rain-(Inf+Evap)"] for r in rows}
    row_by_time = {r["Time"]: r for r in rows}
    t_0910 = seq_0910 * saveint

    def pick(time_s: float) -> dict[str, float] | None:
        return row_by_time.get(round(time_s, 3))

    r_0910, r_end = pick(t_0910), rows[-1]
    window = {
        "t_start_h": round(t_0910 / 3600.0, 3),
        "t_end_h": round(rows[-1]["Time"] / 3600.0, 3),
        "rain_m3": round(float(r_end["Rain-(Inf+Evap)"] - r_0910["Rain-(Inf+Evap)"]), 1) if r_0910 else None,
        "volume_start_m3": round(float(r_0910["Vol"]), 1) if r_0910 else None,
        "volume_end_m3": round(float(r_end["Vol"]), 1),
        "volume_change_m3": round(float(r_end["Vol"] - r_0910["Vol"]), 1) if r_0910 else None,
        "qin_integral_m3": round(float(gate["v_in_m3"]), 1),
        "qout_integral_m3": round(float(gate["v_out_m3"]), 1),
    }
    # 窗口内 Qout 积分
    if r_0910:
        idx = rows.index(r_0910)
        sub = rows[idx:]
        v_out_window = sum(
            0.5 * (a["Qout"] + b["Qout"]) * (b["Time"] - a["Time"]) for a, b in zip(sub, sub[1:])
        )
        window["qout_integral_window_m3"] = round(float(v_out_window), 1)
        window["qout_window_over_rain"] = (
            round(float(v_out_window) / float(window["rain_m3"]), 4) if window["rain_m3"] else None
        )

    s1 = {}
    if RECESSION_S1.is_file():
        doc = json.loads(RECESSION_S1.read_text(encoding="utf-8"))
        s1 = {
            "source": "data/catalog/jiulong_recession_s1.json（复用，未重算）",
            "s1_water_km2_0905_nosea": doc.get("s1_water_km2_0905_prevent_in_aoi_nosea"),
            "s1_water_km2_0917_nosea": doc.get("s1_water_km2_0917_in_aoi_nosea"),
            "model_wet_km2_0910T00": doc.get("model_wet_km2_0910T00"),
            "model_wet_km2_at_overpass": doc.get("model_wet_km2_at_overpass_frame"),
            "model_area_change_0910_to_overpass_km2": doc.get("model_area_change_0910_to_overpass_km2"),
            "csi": (doc.get("metrics_model_vs_s1_0917") or {}).get("csi"),
            "pod": (doc.get("metrics_model_vs_s1_0917") or {}).get("pod"),
            "far": (doc.get("metrics_model_vs_s1_0917") or {}).get("far"),
            "verdict": doc.get("verdict"),
        }
    s1_change = None
    if s1.get("s1_water_km2_0917_nosea") is not None and s1.get("s1_water_km2_0905_nosea") is not None:
        s1_change = round(float(s1["s1_water_km2_0917_nosea"]) - float(s1["s1_water_km2_0905_nosea"]), 4)
        s1["s1_area_change_0905_to_0917_km2"] = s1_change

    model_change = None
    if seq_0910 in by_seq and seq_overpass in by_seq:
        model_change = round(
            by_seq[seq_overpass]["wet_area_km2"] - by_seq[seq_0910]["wet_area_km2"], 4
        )

    return {
        "run_id": run_dir.name,
        "wet_threshold_m": WET_THRESHOLD_M,
        "frame_series": series,
        "mass_gate_recomputed": gate,
        "window": window,
        "boundary": boundary_control(run_dir),
        "comparison_0910_to_overpass": {
            "seq_0910": seq_0910,
            "seq_overpass": seq_overpass,
            "model_wet_km2_0910": by_seq.get(seq_0910, {}).get("wet_area_km2"),
            "model_wet_km2_overpass": by_seq.get(seq_overpass, {}).get("wet_area_km2"),
            "model_area_change_km2": model_change,
            "s1_area_change_km2": s1_change,
            "direction_agreement": (
                None if (model_change is None or s1_change is None)
                else bool(np.sign(model_change) == np.sign(s1_change))
            ),
        },
        "s1_reference": s1,
        "caveats": [
            "S1 仅灾前（09-05）与退水后（09-17）两景，无洪峰过境；退水结论为 WEAK（计划 §1.2 独立观测行）",
            "模型未编码常水（河/塘/海），湿面积为总水体口径，含全部降雨驱动积水",
            "海面像元已从湿面积与体积统计中剔除（sea_mask.npy）",
            "Qin 全程为 0（无干流注入），出流全部来自边界，四边均 FREE",
        ],
    }


# --------------------------------------------------------------------------- #
# 分辨率对照
# --------------------------------------------------------------------------- #
def frame_max_depth(run_dir: Path, name: str) -> np.ndarray:
    grid = read_ascii_grid(Path(run_dir) / "results" / name)
    return grid.data


def resolution_comparison(
    run_200m: Path, run_100m: Path
) -> dict[str, Any]:
    """200 m vs 100 m 在同一物理面积阈值下的对照（计划 §3.4）。"""
    import rasterio
    from pyproj import Transformer

    out: dict[str, Any] = {
        "policy": (
            "200 m 是生产基线，100 m 仅为敏感性试验（计划 §1.4）；"
            "跨分辨率比较只允许同一物理面积阈值（km²），不得只比像元数或斑块个数"
        ),
        "wet_threshold_m": WET_THRESHOLD_M,
        "area_thresholds_km2": list(AREA_THRESHOLDS_KM2),
        "runs": {},
    }
    for res_m, run_dir in ((200.0, run_200m), (100.0, run_100m)):
        grid = read_ascii_grid(run_dir / "dem.asc")
        sea = load_sea_mask(run_dir, grid.shape)
        cell_area = grid.cell_area_m2
        max_grid = read_ascii_grid(run_dir / "results" / "res.max")
        depth = max_grid.data
        wet = np.isfinite(depth) & (depth > WET_THRESHOLD_M) & ~sea
        summary = summarize_frame(
            depth, -1, wet_threshold_m=WET_THRESHOLD_M, cell_area_m2=cell_area, sea_mask=sea
        )
        # 同一物理面积阈值下的斑块数与面积
        by_threshold = {}
        for area_km2 in AREA_THRESHOLDS_KM2:
            n_cells = grid.cells_for_area(area_km2)
            kept = [p for p in summary.patches if p.area_km2 >= area_km2 - 1e-12]
            by_threshold[f"{area_km2:g}"] = {
                "area_threshold_km2": area_km2,
                "cells_threshold_on_this_grid": n_cells,
                "patch_count": len(kept),
                "area_km2": round(sum(p.area_km2 for p in kept), 4),
                "volume_m3": round(sum(p.volume_m3 for p in kept), 1),
                "area_share_pct": round(
                    sum(p.area_km2 for p in kept) / summary.wet_area_km2 * 100, 2
                ) if summary.wet_area_km2 else None,
            }
        # 站址邻域：统一物理半径内的最大/分位水深
        header = dict(grid.header)
        tf = Transformer.from_crs(4326, 32649, always_xy=True)
        ax, ay = tf.transform(ANCHOR_LONLAT[0], ANCHOR_LONLAT[1])
        col = int((ax - float(header["xllcorner"])) / res_m)
        row = int((float(header["yllcorner"]) + grid.shape[0] * res_m - ay) / res_m)
        rows_idx = np.arange(grid.shape[0])[:, None]
        cols_idx = np.arange(grid.shape[1])[None, :]
        distance = np.hypot((rows_idx - row) * res_m, (cols_idx - col) * res_m)
        radius = {}
        for radius_m in RADII_M:
            inside = distance <= radius_m
            values = depth[inside]
            radius[f"{radius_m:g}"] = {
                "radius_m": radius_m,
                "cells": int(inside.sum()),
                "max_depth_m": round(float(values.max()), 4),
                "p95_depth_m": round(float(np.percentile(values, 95)), 4),
                "mean_depth_m": round(float(values.mean()), 4),
                "wet_fraction": round(float((values > WET_THRESHOLD_M).mean()), 4),
                "note": "邻域参考值，不是资产级水深判据",
            }
        # 地形水文派生量：坡度、坑洼、Priority-Flood、D8、出口
        dem_model = grid.data.astype(np.float64)
        gy, gx = np.gradient(dem_model, res_m)
        slope = np.hypot(gx, gy)
        pits = pit_mask(dem_model, include_border=False)
        filled = priority_flood(dem_model)
        rise = filled - dem_model
        receiver = d8_receiver(filled)
        acc = flow_accumulation(filled, receiver)
        flat_after_fill = flat_neighbourhood(filled)
        terrain = {
            "slope": {
                "p50": round(float(np.median(slope)), 6),
                "p95": round(float(np.percentile(slope, 95)), 6),
                "max": round(float(slope.max()), 6),
                "frac_below_0p0005": round(float((slope < 5e-4).mean()), 6),
            },
            "pits": {
                "count_interior": int(pits.sum()),
                "area_km2_interior": round(int(pits.sum()) * cell_area / 1e6, 4),
                "frac_of_domain": round(float(pits.mean()), 6),
                "note": "只统计内部像元（排除最外一圈）；边界像元按 edge padding 天然判为洼坑，会虚增计数",
            },
            "priority_flood": {
                "changed_cells": int((rise > 1e-9).sum()),
                "changed_area_km2": round(int((rise > 1e-9).sum()) * cell_area / 1e6, 4),
                "fill_volume_m3": round(float(rise.sum()) * cell_area, 1),
                "max_rise_m": round(float(rise.max()), 4),
                "flat_cells_after_fill": int(flat_after_fill.sum()),
                "flat_area_after_fill_km2": round(int(flat_after_fill.sum()) * cell_area / 1e6, 4),
                "note": (
                    "Priority-Flood 只在本脚本内作诊断派生，用于 DEM_HYDRO 候选评估；"
                    "填洼后仍有平坦像元，平坦面与水力连通性必须单独检查（计划 §2.3）"
                ),
            },
            "d8": {
                "max_accumulation_cells": int(acc.max()),
                "sink_cells_no_downstream": int((receiver < 0).sum()),
                "sink_share_of_domain_pct": round(float((receiver < 0).mean()) * 100, 2),
                "evidence_level": "E4（出口位置未测）",
                "note": (
                    "D8 只是候选线索，不是真实沟渠（计划 §2.2）。本域 DEM 存在大片精确常值平地，"
                    "D8 在这些像元上没有唯一下游（sink 占比见上），**因此本脚本不给出任何"
                    "出口位置或按边出流占比结论**——出口判定标 E4 未测，须在 Phase 2 用"
                    "DEM_HYDRO 的平坦面路由与 Phase 4 的边界/排水审计单独完成。"
                    "模型实际出流由四边 FREE 边界共同承担（见 boundary_control），"
                    "只能用 res.mass 的 Qout 账目计量"
                ),
            },
            "dem_source_sha256": sha256_file(DEM12),
        }
        out["runs"][f"{int(res_m)}m"] = {
            "run_id": run_dir.name,
            "role": classify_run_role(run_dir.name)[0],
            "grid": {
                **grid.to_meta(),
                "cells": int(grid.data.size),
                "domain_area_km2": round(int(grid.data.size) * cell_area / 1e6, 4),
            },
            "res_max": {
                "wet_cells": summary.wet_cells,
                "wet_area_km2": round(summary.wet_area_km2, 4),
                "wet_volume_m3_above_threshold": round(summary.wet_volume_m3, 1),
                "max_depth_m": round(summary.max_depth_m, 4),
                **summary.depth_stats,
                "patch_count": summary.n_patches,
                "single_cell_patches": sum(1 for p in summary.patches if p.n_cells == 1),
                "single_cell_patch_area_km2": round(
                    sum(p.area_km2 for p in summary.patches if p.n_cells == 1), 4
                ),
            },
            "by_physical_area_threshold": by_threshold,
            "anchor_radius": {"anchor": ANCHOR_LONLAT, "radius": radius},
            "terrain_hydraulics": terrain,
        }
    # 跨分辨率对照结论（只给同一物理阈值的比值）
    cmp: dict[str, Any] = {"wet_area_ratio_100m_over_200m": None, "by_threshold": {}}
    r200, r100 = out["runs"]["200m"], out["runs"]["100m"]
    if r200["res_max"]["wet_area_km2"]:
        cmp["wet_area_ratio_100m_over_200m"] = round(
            r100["res_max"]["wet_area_km2"] / r200["res_max"]["wet_area_km2"], 4
        )
    cmp["wet_area_delta_km2"] = round(
        r100["res_max"]["wet_area_km2"] - r200["res_max"]["wet_area_km2"], 4
    )
    for key in r200["by_physical_area_threshold"]:
        a = r200["by_physical_area_threshold"][key]
        b = r100["by_physical_area_threshold"][key]
        cmp["by_threshold"][key] = {
            "area_threshold_km2": a["area_threshold_km2"],
            "cells_200m": a["cells_threshold_on_this_grid"],
            "cells_100m": b["cells_threshold_on_this_grid"],
            "patch_count_200m": a["patch_count"],
            "patch_count_100m": b["patch_count"],
            "patch_count_ratio_100m_over_200m": (
                round(b["patch_count"] / a["patch_count"], 4) if a["patch_count"] else None
            ),
            "area_km2_200m": a["area_km2"],
            "area_km2_100m": b["area_km2"],
            "area_ratio_100m_over_200m": round(b["area_km2"] / a["area_km2"], 4) if a["area_km2"] else None,
        }
    out["cross_resolution"] = cmp
    out["decision_input"] = {
        "recommended_option": "A",
        "option_a": "按 200 m 生产基线治理；展示阈值按 200 m 换算；100 m 仅作敏感性参照（计划 §1.4 推荐）",
        "option_b": "批准 100 m 或更细重跑后治理；须先完成重跑、质量门与分辨率收敛",
        "status": "已冻结：用户于 2026-10-31 明确选择方案 A（200 m 为生产治理基线）",
        "status_basis": (
            "用户决定，非脚本推断。ADR-0010 决策项 2 按方案 A 定版；"
            "Phase 1 的 A/B/C 展示阈值据此按 200 m 口径冻结。"
        ),
        "option_b_authorization": (
            "用户同日批准 100 m 完整物理重跑，作为独立升级路径；"
            "重跑完成并通过质量门与分辨率收敛前，100 m 仍不得升级为生产基线"
            "（ADR-0010 决策项 2 保持有效）。"
        ),
        "decided_utc": "2026-10-31",
        "evidence_level": "E1（本文件全部数值由脚本从 raw run 复算）",
    }
    return out


# --------------------------------------------------------------------------- #
# 改进 manifest（输入 / 代码 / 参数）
# --------------------------------------------------------------------------- #
CODE_TARGETS = (
    "scripts/stage6/diagnose_jiulong_artifacts.py",
    "scripts/stage6/analyze_patch_persistence.py",
    "scripts/common/grid_io.py",
    "scripts/common/patch_analysis.py",
    "scripts/stage3/jiulong_scenarios.py",
    "scripts/stage3/g15_mass_recheck.py",
    "scripts/stage3/jiulong_recession_s1.py",
    "models/lisflood-fp-8.1/LISFLOOD-FP-trunk/output.cpp",
    "models/lisflood-fp-8.1/LISFLOOD-FP-trunk/boundary.cpp",
    "pyproject.toml",
)


def build_manifest(baseline: dict[str, Any], comparison: dict[str, Any]) -> dict[str, Any]:
    """Phase 0 改进 manifest：输入哈希、代码哈希、参数、产物、证据等级、阻塞项。"""
    inputs: dict[str, Any] = {}
    for key, rec in baseline.get("input_hashes", {}).items():
        inputs[key] = rec
    for key, path in (
        ("data/catalog/mass_recheck_g15.json", MASS_G15),
        ("data/catalog/jiulong_scenarios_summary.json", SCEN_SUMMARY),
        ("data/catalog/jiulong_recession_s1.json", RECESSION_S1),
        ("data/catalog/input_manifest.json", PROJ / "data" / "catalog" / "input_manifest.json"),
    ):
        if path.is_file():
            inputs[key] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
    code: dict[str, Any] = {}
    for rel in CODE_TARGETS:
        path = PROJ / rel
        code[rel] = (
            {"sha256": sha256_file(path), "bytes": path.stat().st_size} if path.is_file()
            else {"missing": True}
        )
    artifacts: dict[str, Any] = {}
    for name in (
        "baseline_snapshot.json", "dem_artifact_map.tif", "resolution_comparison.json",
        "patch_persistence.csv", "patch_tracks.csv", "patch_frame_summary.csv",
        "patch_summary.json", "root_cause_report.md",
    ):
        path = DEFAULT_OUT / name
        artifacts[f"diagnostics/jiulong/{name}"] = (
            {"sha256": sha256_file(path), "bytes": path.stat().st_size} if path.is_file()
            else {"missing": True}
        )
    return {
        "generated_utc": utc_now(),
        "script": "scripts/stage6/diagnose_jiulong_artifacts.py（build_manifest）",
        "phase": "Phase 0 / T0+T1（事实冻结 + 根因诊断）",
        "mode": "ADR-0004 公开数据降级、情景级、非业务预警",
        "read_only_declaration": (
            "本清单登记的全部 run 输入均只读；outputs/jiulong/** 未被写入、移动或删除；"
            "未调用 lisflood.exe；models/ 未修改"
        ),
        "resolution_policy": baseline["resolution_policy"],
        "parameters": {
            "wet_threshold_m": WET_THRESHOLD_M,
            "thin_film_max_m": THIN_FILM_MAX_M,
            "output_precision_m": 0.001,
            "area_thresholds_km2": list(AREA_THRESHOLDS_KM2),
            "plateau_min_km2": PLATEAU_MIN_KM2,
            "radii_m": list(RADII_M),
            "anchor_lonlat": list(ANCHOR_LONLAT),
            "cell_area_source": "栅格头 cellsize 平方；逐 run 记录；禁止写死 0.04 km²",
            "thresholds_frozen": False,
            "threshold_freeze_blocker": "方案 A/B 未确认（计划 §5 Phase 0 门禁 / §9.3 第 1 项）",
        },
        "baseline_runs": {
            "production_200m": "outputs/jiulong/2024-09-yagi/scen_free_200m_sv6h",
            "production_recession_200m": "outputs/jiulong/2024-09-yagi/scen_free_200m_sv6h_recession",
            "sensitivity_100m": "outputs/jiulong/2024-09-yagi/scen_free_100m_dt2p5_sv6h_realdem",
            "non_physical_derived": "outputs/jiulong/2024-09-yagi/scen_free_100m_dt2p5_sv6h_realdem_cleaned",
            "excluded": [
                "scen_free_200m_v1invalid", "scen_z0_200m_v1invalid",
                "scen_z05_200m_v1invalid", "scen_z10_200m_v1invalid",
            ],
        },
        "key_facts": {
            "production_wet_area_km2": comparison["runs"]["200m"]["res_max"]["wet_area_km2"],
            "sensitivity_wet_area_km2": comparison["runs"]["100m"]["res_max"]["wet_area_km2"],
            "wet_area_ratio_100m_over_200m": comparison["cross_resolution"]["wet_area_ratio_100m_over_200m"],
            "max_depth_200m_m": comparison["runs"]["200m"]["res_max"]["max_depth_m"],
            "max_depth_100m_m": comparison["runs"]["100m"]["res_max"]["max_depth_m"],
            "cell_area_200m_m2": comparison["runs"]["200m"]["grid"]["cell_area_m2"],
            "cell_area_100m_m2": comparison["runs"]["100m"]["grid"]["cell_area_m2"],
            "dem_edited_area_km2_200m": baseline["grid_and_time_facts"][
                "scen_free_200m_sv6h"]["dem_classification"]["area_km2"]["edited_total_including_sea"],
            "sea_clamped_area_km2_200m": baseline["grid_and_time_facts"][
                "scen_free_200m_sv6h"]["dem_classification"]["area_km2"]["sea_clamped_zero"],
            "recession_model_area_change_km2": baseline["recession_accounting"][
                "comparison_0910_to_overpass"]["model_area_change_km2"],
            "recession_s1_area_change_km2": baseline["recession_accounting"][
                "comparison_0910_to_overpass"]["s1_area_change_km2"],
            "recession_qout_over_rain": baseline["recession_accounting"]["window"][
                "qout_window_over_rain"],
            "quality_gate": {
                "PASS": ["scen_free_200m_sv6h", "scen_free_200m_sv6h_recession",
                         "scen_free_100m_dt2p5_sv6h_realdem", "scen_z0_200m"],
                "FAIL_EXPLAINED": ["scen_z05_200m", "scen_z10_200m", "scen_ztide_200m"],
                "note": "FAIL-EXPLAINED 不计通过（ADR-0006 / 计划 §7.1）；不得表述为全量通过",
            },
        },
        "evidence_levels": {
            "run_inventory_and_grid_metadata": "E1",
            "dem_classification_and_reconstruction": "E1",
            "patch_statistics_and_tracks": "E1",
            "resolution_comparison": "E1",
            "recession_mass_accounting": "E1",
            "geometric_artifact_causal_link": "E2",
            "isolated_patch_physical_cause": "E3",
            "outlet_position_and_per_edge_outflow": "E4 未测",
            "continuous_depth_and_asset_level": "E4 未测",
            "independent_validation_metrics": "E4 未测",
        },
        "inputs": inputs,
        "code": code,
        "artifacts": artifacts,
        "open_blockers": [
            "方案 A/B 未确认：展示阈值不得冻结（计划 §5 Phase 0 门禁）",
            "四层 DEM 未生成（Phase 2 / task-3）",
            "DEM_HYDRO 平坦面路由未实现：出口位置判定 E4 未测",
            "降雨损失与入渗敏感性未做（Phase 3 / task-4）",
            "排水网络、潮位与站址资料未授权（Phase 4 / 计划 §9.3）",
            "久隆独立验证仅有灾前/退水后两景 S1：CSI/POD/FAR 为 WEAK，NSE/R² 未测",
            "12.5 m 原生网格未重算：lisflood.exe 禁止调用",
        ],
        "known_doc_discrepancies": [
            {
                "id": "C1",
                "doc": "计划 §执行摘要「负值 DEM 约 319.5 km²」",
                "measured": "200 m 网格改写合计 313.60 km²；负值不含海 312.52 km²；12.5 m AOI 内 341.69 km²",
                "action": "请 Lead 标注口径；本 Phase 不改计划",
            },
            {
                "id": "C2",
                "doc": "计划 §执行摘要「S1 常水仅约 11 km²」",
                "measured": "200 m 网格 S1 常水 18.48 km²，其中判为海 1.08 km²",
                "action": "区分常水总量与判海量",
            },
            {
                "id": "C3",
                "doc": "计划 §1.2「下游边界 FREE、Z=0、Z=0.5、Z=1.0」",
                "measured": "N/W/E/S 四边全部 FREE；Qout 为四边之和（boundary.cpp:1330-1378）",
                "action": "更正边界口径描述",
            },
            {
                "id": "C7",
                "doc": "scripts/stage3/jiulong_recession_s1.py:97 硬编码 0.04 km²",
                "measured": "该常量只对 200 m 成立；本 Phase 脚本一律从栅格头读 cellsize",
                "action": "Phase 5 建议改为从栅格头读单元面积",
            },
            {
                "id": "C8",
                "doc": "scen_free_100m_..._cleaned/clean_audit.json 声明 ARR 2019 / UK EA 规则",
                "measured": "计划 §2.3 禁止把 ARR 2019 阈值写成强制标准；100 m 不是生产层",
                "action": "该产物只能作对照，不得称物理结果",
            },
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Phase 0：久隆事实冻结、DEM 异常分类与分辨率对照（只读诊断）"
    )
    add_common_args(parser)
    parser.add_argument(
        "--skip-terrain", action="store_true",
        help="跳过 Priority-Flood / D8 地形派生量（较慢，约 10-30 s）",
    )
    args = parser.parse_args(argv)

    run_tag = (args.run_tag or "").strip()
    out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_OUT
    if run_tag:
        out_dir = out_dir / run_tag
    log_path = Path(args.log_json) if args.log_json else (
        DEFAULT_LOG_DIR / f"diagnose_jiulong_artifacts{'_' + run_tag if run_tag else ''}.json"
    )
    audit = AuditLog("diagnose_jiulong_artifacts", run_tag=run_tag or None, dry_run=args.dry_run)
    audit.param(
        wet_threshold_m=WET_THRESHOLD_M,
        area_thresholds_km2=list(AREA_THRESHOLDS_KM2),
        plateau_min_km2=PLATEAU_MIN_KM2,
        thin_film_max_m=THIN_FILM_MAX_M,
        radii_m=list(RADII_M),
        out_dir=str(out_dir),
        note="全部面积阈值以 km² 表达；像元数由栅格头 cellsize 换算，不写死",
    )
    for key, path in (("dem_12p5m", DEM12), ("mass_recheck_g15", MASS_G15)):
        audit.add_input(key, path)

    if not JIU.is_dir():
        audit.error(f"缺少久隆输出根目录：{JIU}")
        audit.write(log_path)
        print(f"[FAIL] 缺少 {JIU}", file=sys.stderr)
        return 2

    # 覆盖保护：输出目录不得含 results/res.mass（终稿 §10）
    try:
        guard_no_overwrite(out_dir)
    except Exception as exc:  # OverwriteRefused
        audit.error(str(exc))
        audit.write(log_path)
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 3

    run_200m = JIU / "scen_free_200m_sv6h"
    run_100m = JIU / "scen_free_100m_dt2p5_sv6h_realdem"
    run_recession = JIU / "scen_free_200m_sv6h_recession"
    for required in (run_200m, run_100m, run_recession):
        if not required.is_dir():
            audit.error(f"缺少必需 run 目录：{required}")
            audit.write(log_path)
            print(f"[FAIL] 缺少必需 run 目录：{required}", file=sys.stderr)
            return 4

    print("[1/4] 扫描 run 清单 …", flush=True)
    runs = scan_runs()
    audit.step("scan_runs", run_count=len(runs),
               eligible=sum(1 for r in runs if r["eligible_for_diagnosis"]))

    print("[2/4] 复核栅格/时间/DEM 分类 …", flush=True)
    facts = {}
    for run_dir in (run_200m, run_100m, run_recession):
        record = run_facts(run_dir)
        facts[run_dir.name] = record
        check = record["dem_classification"]["reconstruction_check"]
        audit.step("run_facts", run_id=run_dir.name, frames=record["time"]["frame_count"],
                   reconstruction_ok=check["ok"], declared=check["declared_filled_plus_sea_px"],
                   recomputed=check["recomputed_edited_px"])
        if not check["ok"]:
            audit.warn(
                f"{run_dir.name} 重建校验不一致：declared={check['declared_filled_plus_sea_px']} "
                f"recomputed={check['recomputed_edited_px']}"
            )
        print(f"   {run_dir.name}: {record['grid']['cells']} 格 @ {record['grid']['cellsize_m']} m，"
              f"{record['time']['frame_count']} 帧，重建校验 {check['ok']}", flush=True)

    print("[3/4] 退水期逐项对账 …", flush=True)
    recession = recession_accounting(run_recession)
    audit.step("recession_accounting", **{
        k: v for k, v in recession["comparison_0910_to_overpass"].items()
        if k in ("model_area_change_km2", "s1_area_change_km2", "direction_agreement")
    })

    print("[4/4] 分辨率对照（同物理面积阈值）…", flush=True)
    comparison = resolution_comparison(run_200m, run_100m)
    if args.skip_terrain:
        for key in ("200m", "100m"):
            comparison["runs"][key].pop("terrain_hydraulics", None)
        audit.warn("--skip-terrain：已跳过 Priority-Flood / D8 派生量")
    audit.step("resolution_comparison",
               wet_200m=comparison["runs"]["200m"]["res_max"]["wet_area_km2"],
               wet_100m=comparison["runs"]["100m"]["res_max"]["wet_area_km2"])

    # 汇总产物
    baseline = {
        "generated_utc": utc_now(),
        "script": "scripts/stage6/diagnose_jiulong_artifacts.py",
        "run_tag": run_tag or None,
        "phase": "Phase 0 / T0（事实冻结）",
        "mode": "ADR-0004 公开数据降级、情景级、非业务预警",
        "read_only_declaration": (
            "本文件全部数值由脚本从 outputs/jiulong/** 只读复算；"
            "未写入、移动或删除该目录下任何文件；未调用 lisflood.exe"
        ),
        "resolution_policy": {
            "production_baseline": "200 m（scen_free_200m_sv6h）",
            "sensitivity_only": "100 m（scen_free_100m_dt2p5_sv6h_realdem）",
            "rule": "跨分辨率比较必须使用同一物理面积阈值（km²），不得只比像元数或斑块个数（计划 §3.4）",
            "cell_area_source": "栅格头 cellsize 平方，逐 run 记录，禁止写死 0.04 km²",
        },
        "event_window": {
            "event": "2024-09 摩羯（久隆）",
            "rain_source": "ERA5 锚点（22.0638N, 108.7161E）逐时，非站点实测雨量",
            "note": "久隆/贵港为 2024-09 摩羯，宁明为 2026-08 紫檀；不共用绝对时间轴",
        },
        "runs": runs,
        "grid_and_time_facts": {
            name: {
                "grid": rec["grid"],
                "time": rec["time"],
                "dem_classification": {
                    k: v for k, v in rec["dem_classification"].items()
                },
            }
            for name, rec in facts.items()
        },
        "input_hashes": {},
        "quality_gate_summary": {
            "source": "data/catalog/mass_recheck_g15.json（复用，未重算全表）",
            "policy": "PASS<=1e-3；FAIL-EXPLAINED 不计通过（ADR-0006 / 计划 §7.1）",
            "jiulong_runs": {
                rec["path"]: rec["mass_gate_g15"] for rec in runs
                if rec["mass_gate_g15"]["gate"] is not None
            },
            "note": "不得表述为『质量门全量通过』；z05/z10/ztide 为 FAIL-EXPLAINED",
        },
        "recession_accounting": recession,
        "artifacts": {},
    }
    for name, rec in facts.items():
        for fname in ("dem.asc", "run.par", "rain.rain", "bci.bci", "sea_mask.npy"):
            path = JIU / name / fname
            if path.is_file():
                baseline["input_hashes"][f"outputs/jiulong/2024-09-yagi/{name}/{fname}"] = {
                    "sha256": sha256_file(path), "bytes": path.stat().st_size,
                }
        for fname in ("results/res.max", "results/res.mass"):
            path = JIU / name / fname
            if path.is_file():
                baseline["input_hashes"][f"outputs/jiulong/2024-09-yagi/{name}/{fname}"] = {
                    "sha256": sha256_file(path), "bytes": path.stat().st_size,
                }
    baseline["input_hashes"]["data/interim/jiulong/dem_12p5m_32649.tif"] = {
        "sha256": sha256_file(DEM12), "bytes": DEM12.stat().st_size,
    }
    for name in ("jiulong_20240905T105022_water20m.tif", "jiulong_20240917T105023_water20m.tif"):
        path = S1_WATER / name
        if path.is_file():
            baseline["input_hashes"][f"data/processed/s1_water/{name}"] = {
                "sha256": sha256_file(path), "bytes": path.stat().st_size,
            }

    max_200m = frame_max_depth(run_200m, "res.max")
    map_info = {
        "200m": write_artifact_map(out_dir / "dem_artifact_map.tif", facts[run_200m.name], max_200m)
        if not args.dry_run else {
            "path": str(out_dir / "dem_artifact_map.tif"),
            "dry_run_not_written": True,
            "class_legend": {str(k): v for k, v in CLASS_LEGEND.items()},
        }
    }
    baseline["artifacts"]["dem_artifact_map"] = map_info["200m"]
    baseline["artifacts"]["dem_artifact_map_note"] = (
        "栅格以 200 m 生产基线为准（生产口径）；100 m 分类统计见 grid_and_time_facts，"
        "不另出栅格以避免与生产层混淆"
    )

    if args.dry_run:
        print("\n[DRY-RUN] 不写派生文件。摘要：", flush=True)
        for name, rec in facts.items():
            cls = rec["dem_classification"]
            print(f"  {name}: 单元面积 {rec['grid']['cell_area_m2_from_header']} m²；"
                  f"海钳零 {cls['cells']['sea_clamped_zero']} 格 / "
                  f"负值填补 {cls['cells']['negative_bfs_filled']} 格 / "
                  f"精确常值平地(≥{cls['min_plateau_cells']}格) "
                  f"{cls['plateaus']['area_at_or_above_min_km2']} km²", flush=True)
        print(f"  200 m 湿面积 {comparison['runs']['200m']['res_max']['wet_area_km2']} km²；"
              f"100 m 湿面积 {comparison['runs']['100m']['res_max']['wet_area_km2']} km²；"
              f"比值 {comparison['cross_resolution']['wet_area_ratio_100m_over_200m']}", flush=True)
        print(f"  退水：模型 09-10→过境 "
              f"{recession['comparison_0910_to_overpass']['model_area_change_km2']} km²；"
              f"S1 {recession['comparison_0910_to_overpass']['s1_area_change_km2']} km²", flush=True)
        print(json.dumps({"dry_run": True, "out_dir": str(out_dir)}, ensure_ascii=False))
        audit.write(log_path) if args.log_json else None
        return 0

    audit.step("write_products", out_dir=str(out_dir))
    write_json(out_dir / "baseline_snapshot.json", baseline)
    write_json(out_dir / "resolution_comparison.json", comparison)
    manifest = build_manifest(baseline, comparison)
    write_json(MANIFEST, manifest)
    audit.add_input_dir("outputs_jiulong_yagi", JIU, "*")
    audit.write(log_path)
    print(f"\n-> {out_dir / 'baseline_snapshot.json'}")
    print(f"-> {out_dir / 'dem_artifact_map.tif'}")
    print(f"-> {out_dir / 'resolution_comparison.json'}")
    print(f"-> {MANIFEST}")
    print(f"-> {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
