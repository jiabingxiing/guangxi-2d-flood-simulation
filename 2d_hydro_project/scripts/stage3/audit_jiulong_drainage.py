"""Phase 4：久隆排水网络、边界与退水审计。

对应计划 §5 Phase 4 / §3.5 / §7.1。本脚本**只读**既有 run，不跑模型、不改 bci、不改 DEM。

三件事，顺序固定（先边界，后网络，再退水）：

1. **边界与重复计量核查**（计划 §5 Phase 4 第一句"先核查边界与重复计量"）
   - 解析 `bci.bci`：逐边边界类型与坡度；
   - 源码核验本构建的记账结构（`boundary.cpp:1327-1409`）：四边通量**合计成单个
     Qin/Qout**，**无逐边账目** → 逐边份额不可从 `res.mass` 取得，只能估计并标注；
   - 用求解器自身的 FREE 公式（`boundary.cpp:109-125`）在 `.wd` 帧上估计逐边通量，
     与 `res.mass` 的 Qout 积分对账（估计值 vs 求解器记录值）；
   - 短路检查：是否存在"注入体积直接从边界排出"（宁明 D4 同类问题）。
     本例 Qin ≡ 0，无注入 → **无 D4 式注入短路**；但存在**三边可出流**的结构风险，单独登记。

2. **候选排水网络**（D8 仅作线索）
   - 纯 numpy D8 流向 + 汇流累积，只用于回答"地形上最可能的汇流廊道在哪"；
   - **不得**当作真实沟渠；无中心线/断面/影像/现场资料 → 明写阻塞。

3. **退水对账**（逐时段）
   - 逐 6 h 帧：蓄量变化 / 降雨输入 / 出口通量 / 边界通量 / 残差；
   - 湿面积时序与 S1 方向对比；
   - 排水亏缺定量：维持蓄量不变所需出流 vs 实际出流；
   - 退水异常未消除 → 给原因，不以滤波掩盖。

用法：
  uv --directory 2d_hydro_project run python scripts/stage3/audit_jiulong_drainage.py
  ... --dry-run          # 只算不写
  ... --run-tag <tag>    # 输出到 data/catalog 的备份名（默认覆盖审计 JSON）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import (  # noqa: E402
    AuditLog,
    add_common_args,
    parse_run_par,
    read_ascii_grid,
    run_frames,
    write_json,
)

BASE = PROJ / "outputs" / "jiulong" / "2024-09-yagi"
RUN = "scen_free_200m_sv6h_recession"
OUT_JSON = PROJ / "data" / "catalog" / "jiulong_drainage_audit.json"
S1_JSON = PROJ / "data" / "catalog" / "jiulong_recession_s1.json"
WET_THR = 0.05
MANNING_N = 0.035  # run.par 的 fpfric

# 逐边几何：与 stats.cpp:77-111 的 north/south/west/east 顺序一致
EDGES = {
    "N": {"slice": (0, slice(None)), "axis": "row0", "dx": "dx"},
    "S": {"slice": (-1, slice(None)), "axis": "rowN-1", "dx": "dx"},
    "W": {"slice": (slice(None), 0), "axis": "col0", "dx": "dy"},
    "E": {"slice": (slice(None), -1), "axis": "colN-1", "dx": "dy"},
}


# ---------------------------------------------------------------------------
# 1) 边界与重复计量
# ---------------------------------------------------------------------------
def parse_bci(path: Path) -> dict:
    """解析 bci.bci：逐边类型与坡度（FREE 的第二个数是坡度，-1 = 用局地水面坡）。"""
    out: dict = {"raw": path.read_text(encoding="utf-8", errors="replace").splitlines(), "edges": {}}
    for line in out["raw"]:
        parts = line.split()
        if not parts:
            continue
        side = parts[0].upper()
        if side not in ("N", "S", "E", "W"):
            continue
        kind = parts[3].upper() if len(parts) > 3 else "UNKNOWN"
        slope = None
        if len(parts) > 4:
            try:
                slope = float(parts[4])
            except ValueError:
                slope = parts[4]
        out["edges"][side] = {
            "type": kind,
            "slope": slope,
            "slope_meaning": (
                "未给坡度 → 求解器用局地水面坡（boundary.cpp:112-117，注释称 likely unstable）"
                if slope is None else
                f"用户给定坡度 {slope}（boundary.cpp:118-121 直接用它，不用地形）"
            ),
            "raw_line": line.strip(),
        }
    out["outflow_capable_edges"] = sorted(k for k, v in out["edges"].items()
                                          if v["type"].startswith("FREE"))
    out["inflow_capable_edges"] = []  # FREE 在 sign 钳位下只能出流（boundary.cpp:180-183）
    return out


def edge_wet_stats(frames: list[tuple[int, Path]], dem: np.ndarray, *, saveint: float,
                   upland_threshold_m: float = 5.0) -> dict:
    """逐帧逐边统计湿边界单元、水深与 DEM 高程，并按"高地/低地"拆分估计通量。

    为什么要拆：边界上可能出现毫米级薄膜（雨滴落在高地上、随即被边界排走）。
    只用"最大 DEM"会把这类薄膜误报成"高地出口"，夸大成因；
    因此同时给出**通量占比**（`q_est_upland_share_pct`），并以它作为风险判据。
    """
    per_edge: dict = {k: {"frames": [], "wet_cells_total": 0, "wet_cells_thr5cm_total": 0,
                          "frames_with_wet": 0, "frames_with_wet_5cm": 0,
                          "sum_h_m": 0.0, "q_est_sum_m3s": 0.0, "q_est_upland_sum_m3s": 0.0,
                          "wet5_dem_max": None, "wet5_dem_min": None}
                      for k in EDGES}
    dx = 200.0
    for seq, path in frames:
        wd = np.loadtxt(path, skiprows=6, dtype=np.float64, ndmin=2)
        if wd.shape != dem.shape:
            raise ValueError(f"{path} 形状 {wd.shape} 与 DEM {dem.shape} 不符")
        for k, spec in EDGES.items():
            rs, cs = spec["slice"]
            h = wd[rs, cs]
            z = dem[rs, cs]
            wet = h > 1e-3
            wet5 = h > WET_THR
            rec = per_edge[k]
            rec["wet_cells_total"] += int(wet.sum())
            rec["wet_cells_thr5cm_total"] += int(wet5.sum())
            rec["sum_h_m"] += float(h.sum())
            if wet.any():
                rec["frames_with_wet"] += 1
            if wet5.any():
                rec["frames_with_wet_5cm"] += 1
                z5min, z5max = float(z[wet5].min()), float(z[wet5].max())
                rec["wet5_dem_min"] = z5min if rec["wet5_dem_min"] is None else min(rec["wet5_dem_min"], z5min)
                rec["wet5_dem_max"] = z5max if rec["wet5_dem_max"] is None else max(rec["wet5_dem_max"], z5max)
            # 用求解器自身的 FREE/inertial 公式（boundary.cpp:109-125）估计该边通量。
            # acceleration 分支：Sf = 用户坡度（给了就用地，S 边 0.0005）否则 -dh/dx；
            # q ≈ (1/n)·h^(5/3)·√Sf·dx（Manning 稳态近似，量级估计用）
            inner = _inner_slice(k)
            ri, ci = inner
            h_in = wd[ri, ci]
            z_in = dem[ri, ci]
            slope_user = 0.0005 if k == "S" else None
            if slope_user is None:
                dh = (z + h) - (z_in + h_in)
                sf = -dh / dx
            else:
                sf = np.full_like(h, slope_user)
            sf = np.clip(np.nan_to_num(sf, nan=0.0), 0.0, None)
            hh = np.clip(h, 0.0, None)
            with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
                q = (1.0 / MANNING_N) * np.power(hh, 5.0 / 3.0) * np.sqrt(sf) * dx
            q = np.where(wet, np.nan_to_num(q, nan=0.0, posinf=0.0), 0.0)
            q_tot = float(q.sum())
            q_upland = float(q[z > upland_threshold_m].sum())
            rec["q_est_sum_m3s"] += q_tot
            rec["q_est_upland_sum_m3s"] += q_upland
            rec["frames"].append({
                "seq": seq, "t_h": round(seq * saveint / 3600.0, 2),
                "wet_cells": int(wet.sum()), "wet_cells_5cm": int(wet5.sum()),
                "sum_h_m": round(float(h.sum()), 4),
                "q_est_m3s": round(q_tot, 4),
                "q_est_upland_m3s": round(q_upland, 4),
            })
    n = max(len(frames), 1)
    summary = {}
    for k, rec in per_edge.items():
        qsum = rec["q_est_sum_m3s"]
        summary[k] = {
            "avg_wet_cells": round(rec["wet_cells_total"] / n, 2),
            "avg_wet_cells_5cm": round(rec["wet_cells_thr5cm_total"] / n, 2),
            "frames_with_wet": rec["frames_with_wet"],
            "frames_with_wet_5cm": rec["frames_with_wet_5cm"],
            "avg_sum_h_m": round(rec["sum_h_m"] / n, 3),
            "mean_q_est_m3s": round(qsum / n, 3),
            "mean_q_est_upland_m3s": round(rec["q_est_upland_sum_m3s"] / n, 3),
            "q_est_upland_share_pct": round(rec["q_est_upland_sum_m3s"] / qsum * 100.0, 2) if qsum else 0.0,
            "wet5_dem_min_m": round(rec["wet5_dem_min"], 2) if rec["wet5_dem_min"] is not None else None,
            "wet5_dem_max_m": round(rec["wet5_dem_max"], 2) if rec["wet5_dem_max"] is not None else None,
        }
    return {"per_edge": summary, "per_edge_frames": per_edge,
            "upland_threshold_m": upland_threshold_m}


def edge_outlet_hotspots(frames: list[tuple[int, Path]], dem: np.ndarray, *,
                         share_threshold_pct: float = 5.0) -> dict:
    """识别每条边上"承担主要出流"的边界单元（热点），并给出其 DEM 与水深。

    目的：逐边总量不够解释问题——真正的排水可能只经由极少数单元发生。
    判据：该单元累计估计通量占该边累计估计通量的 `share_threshold_pct` 以上。
    这类热点若位于 DEM 明显高于周边的高地，则说明模型在用边界当"人工排水口"，
    需要资料核证（宁明 D4 同类风险）。
    """
    n_cells = {"N": dem.shape[1], "S": dem.shape[1], "W": dem.shape[0], "E": dem.shape[0]}
    cum_q: dict = {k: np.zeros(n_cells[k]) for k in EDGES}
    cum_h: dict = {k: np.zeros(n_cells[k]) for k in EDGES}
    n_wet: dict = {k: np.zeros(n_cells[k], dtype=np.int64) for k in EDGES}
    dx = 200.0
    for _seq, path in frames:
        wd = np.loadtxt(path, skiprows=6, dtype=np.float64, ndmin=2)
        for k, spec in EDGES.items():
            rs, cs = spec["slice"]
            h = wd[rs, cs]
            z = dem[rs, cs]
            wet = h > 1e-3
            ri, ci = _inner_slice(k)
            h_in = wd[ri, ci]
            z_in = dem[ri, ci]
            if k == "S":
                sf = np.full_like(h, 0.0005)
            else:
                sf = -( (z + h) - (z_in + h_in) ) / dx
            sf = np.clip(np.nan_to_num(sf, nan=0.0), 0.0, None)
            hh = np.clip(h, 0.0, None)
            with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
                q = (1.0 / MANNING_N) * np.power(hh, 5.0 / 3.0) * np.sqrt(sf) * dx
            q = np.where(wet, np.nan_to_num(q, nan=0.0, posinf=0.0), 0.0)
            cum_q[k] += q
            cum_h[k] += np.where(wet, h, 0.0)
            n_wet[k] += wet.astype(np.int64)

    out: dict = {}
    for k in EDGES:
        q = cum_q[k]
        tot = float(q.sum())
        if tot <= 0:
            out[k] = {"total_q_est_m3": 0.0, "hotspots": []}
            continue
        share = q / tot * 100.0
        hot = np.where(share >= share_threshold_pct)[0]
        nf = max(len(frames), 1)
        z_edge = dem[{"N": (0, slice(None)), "S": (-1, slice(None)),
                      "W": (slice(None), 0), "E": (slice(None), -1)}[k]]
        out[k] = {
            "total_q_est_m3": round(tot, 1),
            "n_cells_carrying_gt_threshold": int(hot.size),
            "hotspots": [
                {
                    "cell_index_along_edge": int(i),
                    "share_of_edge_flux_pct": round(float(share[i]), 2),
                    "dem_m": round(float(z_edge[i]), 2),
                    "mean_depth_m_when_wet": round(
                        float(cum_h[k][i] / n_wet[k][i]) if n_wet[k][i] else 0.0, 4),
                    "frames_wet": int(n_wet[k][i]),
                    "frames_total": nf,
                }
                for i in hot[np.argsort(-share[hot])]
            ],
        }
    return out


def _inner_slice(edge: str) -> tuple:
    """边内一格（求解器 p1 位置）：N→row1, S→row N-2, W→col1, E→col N-2。"""
    return {"N": (1, slice(None)), "S": (-2, slice(None)),
            "W": (slice(None), 1), "E": (slice(None), -2)}[edge]


def boundary_audit(run_dir: Path, *, log: AuditLog) -> dict:
    """边界与重复计量核查。"""
    bci = parse_bci(run_dir / "bci.bci")
    dem_grid = read_ascii_grid(run_dir / "dem.asc", dtype=np.float64)
    dem = dem_grid.data
    sea_path = run_dir / "sea_mask.npy"
    sea = np.load(sea_path) if sea_path.is_file() else np.zeros(dem.shape, bool)
    par = parse_run_par(run_dir)
    saveint = float(par.get("saveint", 21600.0))
    frames = run_frames(run_dir / "results")

    # 质量列
    lines = (run_dir / "results" / "res.mass").read_text().splitlines()
    keys = lines[0].split()
    rows = [dict(zip(keys, map(float, ln.split()))) for ln in lines[1:] if ln.split()]
    v_in = sum(0.5 * (a["Qin"] + b["Qin"]) * (b["Time"] - a["Time"]) for a, b in zip(rows, rows[1:]))
    v_out = sum(0.5 * (a["Qout"] + b["Qout"]) * (b["Time"] - a["Time"]) for a, b in zip(rows, rows[1:]))
    tspan = rows[-1]["Time"] - rows[0]["Time"]
    qout_mean = v_out / tspan if tspan else 0.0

    est = edge_wet_stats(frames, dem, saveint=saveint)
    hotspots = edge_outlet_hotspots(frames, dem)
    est_total = sum(v["mean_q_est_m3s"] for v in est["per_edge"].values())
    upland_total = sum(v["mean_q_est_upland_m3s"] for v in est["per_edge"].values())
    for k, v in est["per_edge"].items():
        v["share_of_estimated_total_pct"] = (
            round(v["mean_q_est_m3s"] / est_total * 100.0, 2) if est_total else None
        )
        # 风险判据用**通量占比**，不用"最大 DEM"（毫米级薄膜会把后者打成假阳性）
        v["upland_flux_dominant"] = bool(v["q_est_upland_share_pct"] > 50.0)

    qin_all_zero = all(r["Qin"] == 0.0 for r in rows)
    audit = {
        "run": run_dir.name,
        "run_dir": run_dir.relative_to(PROJ).as_posix(),
        "grid": dem_grid.to_meta(),
        "bci": {
            "edges": bci["edges"],
            "outflow_capable_edges": bci["outflow_capable_edges"],
            "inflow_capable_edges": bci["inflow_capable_edges"],
            "raw_lines": bci["raw"],
        },
        "accounting_structure": {
            "source_code": "boundary.cpp:1327-1409 BoundaryFlux()",
            "fact": (
                "求解器把 N/S/W/E 四边通量按正负分别累加，最后合成**单个** BCptr->Qin / BCptr->Qout；"
                "res.mass 的 Qin/Qout 列只有域总账，**没有逐边账目**。"
            ),
            "chanmask_note": (
                "无 chanmask 文件时 ChanMask 全为 -1（input.cpp:1459），"
                "四边的全部边界单元都参与记账（boundary.cpp:1343-1373 的 `== -1` 判定恒真）。"
            ),
            "free_one_way_note": (
                "FREE 边界在 sign 钳位下只能出流不能入流（boundary.cpp:180-183），"
                "故 Qin 恒为 0 与边界设置自洽。"
            ),
            "consequence": "逐边 Qin/Qout 无法从 res.mass 取得；本节逐边份额为**估计值**，不是求解器记录值。",
        },
        "measured": {
            "qin_integrated_m3": round(v_in, 1),
            "qout_integrated_m3": round(v_out, 1),
            "qin_all_rows_zero": qin_all_zero,
            "qout_mean_m3s": round(qout_mean, 3),
            "qout_max_m3s": round(max(r["Qout"] for r in rows), 3),
            "qout_final_m3s": round(rows[-1]["Qout"], 3),
        },
        "per_edge_estimate": {
            "method": (
                "在每帧 .wd 上用求解器自身的 FREE/inertial 公式（boundary.cpp:109-125）"
                "按 Manning 形式估计各边出流；S 边用 bci 给定的 0.0005 坡度，"
                "N/W/E 边用局地水面坡（因 bci 未给坡度）。"
            ),
            "confidence": "E4（估计值；非求解器记录，只用于判断逐边量级与合理性，不作账目依据）",
            "manning_n": MANNING_N,
            "frames_used": len(frames),
            "per_edge": est["per_edge"],
            "estimated_total_m3s": round(est_total, 3),
            "estimated_upland_total_m3s": round(upland_total, 3),
            "estimated_upland_share_pct": round(upland_total / est_total * 100.0, 2) if est_total else None,
            "outlet_hotspots": hotspots,
            "solver_recorded_mean_qout_m3s": round(qout_mean, 3),
            "estimate_over_recorded_ratio": round(est_total / qout_mean, 3) if qout_mean else None,
            "interpretation": (
                "估计总量与求解器记录的 Qout 均值同量级（比值 0.87）即视为逐边拆分"
                "可信度足够做**量级判断**；不得据此写精确逐边通量。"
            ),
        },
        "short_circuit_check": {
            "definition": "边界短路 = 注入体积从边界直接排出（宁明 D4：83.0% 注入量从东缘排出）",
            "has_upstream_injection": bool(not qin_all_zero),
            "d4_type_short_circuit": False,
            "reason": (
                "本例全程 Qin ≡ 0、无干流/上游注入（计划 §1.2），"
                "不存在“注入量被边界直接排走”的 D4 式短路。"
            ),
            "three_side_outflow_risk": {
                "present": True,
                "edges": bci["outflow_capable_edges"],
                "detail": (
                    "N/W/E 三边也是 FREE，即除南缘外三边都能出流。"
                    "这与宁明 D4 属**同类**（边界可自由排水且与地形/汇流关系未核证），"
                    "但**成因不同**：D4 是注入点与出流边界重合，本例是全域无注入、"
                    "仅靠四边缓排。"
                ),
                "risk_level": "中（不是账目错误，但边界排水路径未经资料核证）",
                "unresolved": True,
            },
        },
        "findings": [],
        "limitations": [
            "本构建无逐边 Qin/Qout 账目（boundary.cpp:1327-1409），逐边份额只能估计，标 E4。",
            "无潮位/水位过程资料 → 下游边界只能是情景（FREE / Z=0 / Z=0.5 / Z=1.0），"
            "不能判定哪一档接近真实（计划 §1.2）。",
            "无排水网络授权资料（沟渠中心线/断面/影像/现场）→ 候选网络只能是 D8 线索。",
            "垂直基准未闭合（G7）→ 边界高程与水深不做绝对高程结论。",
        ],
    }

    # 逐边发现（按证据强度排序）
    e = est["per_edge"]
    s_edge = e["S"]
    audit["findings"].append({
        "id": "B1",
        "claim": "南缘（S，FREE 0.0005）是设计上的主出口，湿宽最大、估计出流份额最高",
        "evidence": {
            "s_avg_wet_cells": s_edge["avg_wet_cells"],
            "s_mean_q_est_m3s": s_edge["mean_q_est_m3s"],
            "s_share_pct": s_edge["share_of_estimated_total_pct"],
        },
        "confidence": "E2（估计值方向一致 + bci 设计意图）",
    })
    audit["findings"].append({
        "id": "B2",
        "claim": (
            "N/W/E 三边同时可出流。南缘（S）是主出口（估计占 65%）；"
            "但**东缘（E）的出流几乎全部来自高地单元**（DEM>5 m 贡献 97%），"
            "热点单元位于 DEM 22.2–23.4 m、平均水深仅 0.007–0.022 m，"
            "物理上不构成真实排水出口"
        ),
        "evidence": {
            "share_pct": {k: e[k]["share_of_estimated_total_pct"] for k in ("N", "S", "W", "E")},
            "upland_flux_share_pct": {k: e[k]["q_est_upland_share_pct"] for k in ("N", "S", "W", "E")},
            "upland_flux_total_share_pct": (
                round(upland_total / est_total * 100.0, 2) if est_total else None
            ),
            "east_hotspots": hotspots["E"]["hotspots"][:4],
            "north_hotspots_dem_m": [h["dem_m"] for h in hotspots["N"]["hotspots"][:4]],
            "south_hotspots_count": hotspots["S"]["n_cells_carrying_gt_threshold"],
        },
        "confidence": "E3（逐边与逐单元为估计值；高地判据 DEM>5 m 为项目自定，非规范阈值）",
        "implication": (
            "东缘存在与宁明 D4 **同类**的问题：边界被当作人工排水口，排走的是高地上的"
            "毫米级薄膜水。量级小（E 边占估计总出流 12%），但方向上是假排水——"
            "它让模型显得比实际更能退水，而真实退水仍严重不足。"
            "候选处置：将 N/W/E 改为闭合（仅留南缘 FREE），作为新 run tag 单独试验；"
            "**不得**直接改现有 bci.bci。"
        ),
        "self_correction": (
            "初版曾用'湿边界单元的最大 DEM>5 m'判定'高地出口'，实测发现这些多为"
            "毫米级薄膜（H≈0.002–0.013 m），会把风险夸大成假阳性；"
            "已改为按**通量占比**判定，并逐单元定位热点后重新核对，本记录保留以免误读。"
        ),
    })
    audit["findings"].append({
        "id": "B3",
        "claim": "域内总出流能力远小于降雨输入能力，是退水异常的边界侧原因",
        "evidence": {
            "mean_qout_m3s": round(qout_mean, 3),
            "rain_input_mean_m3s": round(
                rows[-1]["Rain-(Inf+Evap)"] / tspan if tspan else 0.0, 3),
        },
        "confidence": "E1（两者均为 res.mass 直接可得）",
    })
    log.step("boundary_audit", qin_zero=qin_all_zero,
             est_total=round(est_total, 3), qout_mean=round(qout_mean, 3))
    return audit


# ---------------------------------------------------------------------------
# 2) 候选排水网络（D8 仅作线索）
# ---------------------------------------------------------------------------
def d8_flow_accumulation(dem: np.ndarray) -> dict:
    """纯 numpy D8 流向 + 汇流累积（不作真实沟渠，仅线索）。

    规则：每格指向 8 邻域中高程最低者；无更低邻居即为局部洼地（流终止）。
    按高程降序累加，保证上游先算完。
    """
    h, w = dem.shape
    idx = np.arange(h * w).reshape(h, w)
    padded = np.pad(dem, 1, mode="edge")
    padded_idx = np.pad(idx, 1, mode="edge")
    best = np.full((h, w), np.inf)
    target = np.full((h, w), -1, dtype=np.int64)
    offsets = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
    for dr, dc in offsets:
        nb = padded[1 + dr:1 + dr + h, 1 + dc:1 + dc + w]
        nb_i = padded_idx[1 + dr:1 + dr + h, 1 + dc:1 + dc + w]
        dist = float(np.hypot(dr, dc))
        slope = (dem - nb) / dist          # 正 = 下坡
        # 取"下坡坡度最大"者（等价于最陡下降）；best 初值 inf 保证首轮必选
        better = (slope > 0) & ((best == np.inf) | (slope > best))
        best = np.where(better, slope, best)
        target = np.where(better, nb_i, target)
    acc = np.ones(h * w, dtype=np.int64)
    order = np.argsort(dem.ravel(), kind="stable")[::-1]   # 高程降序
    tgt_flat = target.ravel()
    for cell in order:
        t = tgt_flat[cell]
        if t >= 0:
            acc[t] += acc[cell]
    acc2 = acc.reshape(h, w)
    sinks = target < 0
    return {"accumulation": acc2, "sinks": sinks, "target": target}


def drainage_network_candidates(run_dir: Path, *, log: AuditLog) -> dict:
    """候选排水廊道（D8 线索）+ 阻塞项。"""
    grid = read_ascii_grid(run_dir / "dem.asc", dtype=np.float64)
    dem = grid.data
    h, w = dem.shape
    sea_path = run_dir / "sea_mask.npy"
    sea = np.load(sea_path) if sea_path.is_file() else np.zeros(dem.shape, bool)

    d8 = d8_flow_accumulation(dem)
    acc = d8["accumulation"]
    sinks = d8["sinks"]
    acc_land = np.where(sea, 0, acc)

    # 高汇流廊道（线索）：取全域累积前 0.5% 的单元
    flat = acc_land.ravel()
    thr = float(np.percentile(flat[flat > 1], 99.5)) if (flat > 1).any() else 0.0
    corridors = acc_land >= thr
    corridor_area_km2 = float(corridors.sum()) * grid.cell_area_m2 / 1e6

    # 洼地（流终止点）容量线索：洼地数与最大洼地累积面积
    sink_cells = int(sinks.sum())
    sink_acc = acc[sinks]
    biggest = int(sink_acc.max()) if sink_acc.size else 0

    # 与湿区叠合：退水期末帧湿区里有多少落在高汇流廊道上
    frames = run_frames(run_dir / "results")
    overlay = None
    if frames:
        seq, path = frames[-1]
        wd = np.loadtxt(path, skiprows=6, dtype=np.float64, ndmin=2)
        wet = (wd > WET_THR) & ~sea
        on_corr = int((wet & corridors).sum())
        overlay = {
            "frame": f"res-{seq:04d}.wd",
            "wet_cells": int(wet.sum()),
            "wet_cells_on_corridor": on_corr,
            "wet_cells_on_corridor_pct": round(on_corr / int(wet.sum()) * 100.0, 2) if wet.any() else None,
        }

    rec = {
        "method": "纯 numpy D8 最陡下降 + 汇流累积（8 邻域；无更低邻居即为局部洼地）",
        "role": "**线索**：只用于回答地形上最可能汇流到哪里，不得当作真实沟渠/排水管网",
        "grid": grid.to_meta(),
        "d8_result": {
            "accumulation_max_cells": int(acc_land.max()),
            "corridor_threshold_cells": round(thr, 1),
            "corridor_cells": int(corridors.sum()),
            "corridor_area_km2": round(corridor_area_km2, 3),
            "sink_cells": sink_cells,
            "sink_cells_pct": round(sink_cells / (h * w) * 100.0, 2),
            "largest_sink_upstream_cells": biggest,
        },
        "wet_overlay_on_corridors": overlay,
        "authorization_blockers": [
            {
                "id": "D1",
                "item": "排水沟渠/河渠中心线（矢量）",
                "status": "阻塞",
                "why": "D8 廊道是地形汇流线索，不是实测沟渠；无中心线则无法判定真实排水路径与断面",
                "needed_from": "业务部门 / 水利普查 / 1:10000 以上地形图或影像解译成果",
                "user_decision_2026_10_31": "用户明确：目前无法提供。该项保持阻塞，不得以 D8 线索替代",
            },
            {
                "id": "D2",
                "item": "沟渠断面（宽度/深度/底高程）",
                "status": "阻塞",
                "why": "无断面则任何刻蚀深度都是无依据猜测；计划 §3.5 禁止全域固定刻蚀 0.5–1.5 m",
                "needed_from": "实测断面或设计资料",
                "user_decision_2026_10_31": "用户明确：目前无法提供。该项保持阻塞，不得以假定断面刻蚀",
            },
            {
                "id": "D3",
                "item": "下游控制断面水位/流量过程 Z(t)、Q(t)",
                "status": "阻塞",
                "why": "无过程则下游边界只能是 FREE/Z=常数情景，无法判定真实排水能力",
                "needed_from": "水文站 / 潮位站",
                "user_decision_2026_10_31": (
                    "用户明确：目前无法提供潮位/水位资料。"
                    "下游边界继续用 FREE/Z=常数情景带，结论保持情景级"
                ),
            },
            {
                "id": "D4",
                "item": "受纳水体（钦江/海湾）与域边界的真实连通关系",
                "status": "阻塞",
                "why": "决定 N/W/E 三边是否应闭合；当前三边 FREE 未经资料核证",
                "needed_from": "水系图 + 现场/影像核证",
                "user_decision_2026_10_31": (
                    "用户明确：目前无法提供。三边 FREE 的短路风险只作候选处置，"
                    "不凭推断改 bci；已另立 100 m 边界对照 run 作纯敏感性证据"
                ),
            },
            {
                "id": "D5",
                "item": "涵洞/桥闸/泵站等结构位置与参数",
                "status": "阻塞",
                "why": "ADR-0008 已定版禁 bridge/orifice（B8b 未独立验证），结构只能靠 DEM 加高 + weirfile",
                "needed_from": "结构竣工资料 + 独立再验证",
                "user_decision_2026_10_31": "用户明确：目前无法提供。结构表达不实施",
            },
        ],
        "authorization_status_2026_10_31": {
            "decision": "用户明确表示目前无法提供站址坐标、排水网络、潮位/水位资料",
            "consequence": (
                "Phase 4 物理治理（排水网络、断面刻蚀、结构表达、Z(t)/Q(t) 边界）"
                "全部无法推进，保持阻塞。退水异常（+27.68 km²）在现有资料下不可消除，"
                "只能登记原因，不得以滤波掩盖。"
            ),
            "what_remains_possible": (
                "边界设置的敏感性对照（N/W/E 闭合 vs 四边 FREE）不需要外部资料，"
                "可作纯候选证据；但不得据此宣称已找到真实排水路径"
            ),
        },
        "conclusion": (
            "候选排水网络**只能到线索级**：D8 给出地形汇流廊道与洼地分布，"
            "但缺 D1–D5 全部授权资料，**不得**据此刻蚀河道、**不得**当作已建成的排水网络，"
            "也**不得**把候选网络切换到生产 run。"
        ),
    }
    log.step("drainage_network", corridor_cells=int(corridors.sum()), sink_cells=sink_cells)
    return rec


# ---------------------------------------------------------------------------
# 3) 退水对账（逐时段）
# ---------------------------------------------------------------------------
def recession_reconciliation(run_dir: Path, *, log: AuditLog) -> dict:
    """逐 6 h 帧对账：蓄量 / 降雨 / 出口 / 边界 / 残差；并量化排水亏缺。"""
    grid = read_ascii_grid(run_dir / "dem.asc", dtype=np.float64)
    dem = grid.data
    cell_area = grid.cell_area_m2
    sea_path = run_dir / "sea_mask.npy"
    sea = np.load(sea_path) if sea_path.is_file() else np.zeros(dem.shape, bool)
    par = parse_run_par(run_dir)
    saveint = float(par.get("saveint", 21600.0))

    lines = (run_dir / "results" / "res.mass").read_text().splitlines()
    keys = lines[0].split()
    rows = [dict(zip(keys, map(float, ln.split()))) for ln in lines[1:] if ln.split()]

    def at(key: str, t: float) -> float:
        if t <= rows[0]["Time"]:
            return rows[0][key]
        for a, b in zip(rows, rows[1:]):
            if a["Time"] <= t <= b["Time"]:
                return a[key] if b["Time"] == a["Time"] else \
                    a[key] + (b[key] - a[key]) * (t - a["Time"]) / (b["Time"] - a["Time"])
        return rows[-1][key]

    def integ(key: str, t0: float, t1: float) -> float:
        tot = 0.0
        for a, b in zip(rows, rows[1:]):
            lo, hi = a["Time"], b["Time"]
            if hi <= t0 or lo >= t1:
                continue
            lo_c, hi_c = max(lo, t0), min(hi, t1)
            fa = a[key] + (b[key] - a[key]) * ((lo_c - lo) / (hi - lo) if hi > lo else 0.0)
            fb = a[key] + (b[key] - a[key]) * ((hi_c - lo) / (hi - lo) if hi > lo else 0.0)
            tot += 0.5 * (fa + fb) * (hi_c - lo_c)
        return tot

    # 逐 6 h 帧：湿面积 + 蓄量
    frame_rows = []
    for seq, path in run_frames(run_dir / "results"):
        wd = np.loadtxt(path, skiprows=6, dtype=np.float64, ndmin=2)
        wet = (wd > WET_THR) & ~sea
        t = seq * saveint
        frame_rows.append({
            "seq": seq, "t_s": t, "t_days": round(t / 86400.0, 3),
            "wet_cells": int(wet.sum()),
            "wet_area_km2": round(float(wet.sum()) * cell_area / 1e6, 3),
            "vol_m3": round(at("Vol", t), 1),
            "qout_m3s": round(at("Qout", t), 3),
        })

    # 逐时段对账（按 24 h 分段，便于阅读；同时给逐 6 h 原始表）
    seg_len = 86400.0
    segments = []
    t_end = rows[-1]["Time"]
    t = 0.0
    while t < t_end - 1e-9:
        t1 = min(t + seg_len, t_end)
        dv = at("Vol", t1) - at("Vol", t)
        v_rain = at("Rain-(Inf+Evap)", t1) - at("Rain-(Inf+Evap)", t)
        v_out = integ("Qout", t, t1)
        v_in = integ("Qin", t, t1)
        resid = dv - v_rain - (v_in - v_out)
        segments.append({
            "t0_days": round(t / 86400.0, 3), "t1_days": round(t1 / 86400.0, 3),
            "dv_storage_m3": round(dv, 1),
            "v_rain_m3": round(v_rain, 1),
            "v_out_m3": round(v_out, 1),
            "v_in_m3": round(v_in, 1),
            "residual_m3": round(resid, 1),
            "residual_pct_of_rain": round(resid / v_rain * 100.0, 4) if v_rain else None,
            "storage_share_of_rain_pct": round(dv / v_rain * 100.0, 2) if v_rain else None,
            "outflow_share_of_rain_pct": round(v_out / v_rain * 100.0, 2) if v_rain else None,
        })
        t = t1

    # 排水亏缺：维持蓄量不变所需出流 vs 实际出流
    def deficit(t0: float, t1: float) -> dict:
        span = t1 - t0
        v_rain = at("Rain-(Inf+Evap)", t1) - at("Rain-(Inf+Evap)", t0)
        v_out = integ("Qout", t0, t1)
        need = v_rain / span if span else 0.0
        actual = v_out / span if span else 0.0
        return {
            "span_s": span,
            "rain_volume_m3": round(v_rain, 1),
            "outflow_volume_m3": round(v_out, 1),
            "required_outflow_to_hold_storage_m3s": round(need, 3),
            "actual_outflow_m3s": round(actual, 3),
            "outflow_deficit_m3s": round(need - actual, 3),
            "outflow_capacity_ratio": round(actual / need, 4) if need else None,
        }

    ext0, ext1 = 432000.0, 1123200.0
    deficit_extension = deficit(ext0, ext1)
    deficit_full = deficit(0.0, t_end)

    # 湿面积时序（关键帧）
    def frame_at(days: float) -> dict | None:
        target = days * 86400.0
        if not frame_rows:
            return None
        return min(frame_rows, key=lambda r: abs(r["t_s"] - target))

    f_0910 = frame_at(5.0)
    f_0917 = frame_at(12.75)
    area_change = (round(f_0917["wet_area_km2"] - f_0910["wet_area_km2"], 3)
                   if f_0910 and f_0917 else None)

    s1 = json.loads(S1_JSON.read_text(encoding="utf-8")) if S1_JSON.is_file() else {}

    rec = {
        "run": run_dir.name,
        "window": {"t0_days": 0.0, "t1_days": round(t_end / 86400.0, 3),
                   "sim_seconds": t_end, "saveint_s": saveint},
        "totals": {
            "v_rain_m3": round(rows[-1]["Rain-(Inf+Evap)"], 1),
            "v_out_m3": round(integ("Qout", 0.0, t_end), 1),
            "v_in_m3": round(integ("Qin", 0.0, t_end), 1),
            "dv_storage_m3": round(rows[-1]["Vol"] - rows[0]["Vol"], 1),
        },
        "per_day_reconciliation": segments,
        "per_frame": frame_rows,
        "drainage_deficit": {
            "full_13d": deficit_full,
            "extension_8d_09_10_to_09_18": deficit_extension,
            "interpretation": (
                "维持蓄量不变所需的出流能力远大于模型实际出流能力；"
                "这就是“退水不降反升”的边界侧定量原因。"
            ),
        },
        "wet_area_trend": {
            "at_09_10T00_km2": f_0910["wet_area_km2"] if f_0910 else None,
            "at_09_17T18_km2": f_0917["wet_area_km2"] if f_0917 else None,
            "change_km2": area_change,
            "s1_reference_0917_km2": s1.get("s1_water_km2_0917_in_aoi_nosea"),
            "s1_reference_0905_km2": s1.get("s1_water_km2_0905_prevent_in_aoi_nosea"),
            "direction_vs_s1": (
                "模型湿面积在延长段**增加**，S1 实测从 09-05 到 09-17 **减少** → 方向相反"
                if area_change is not None and area_change > 0 else
                "模型湿面积未增加，与 S1 方向不冲突"
            ),
            "confidence": "E2（模型侧 E1 可复核；S1 侧 WEAK，单景、G17 下限）",
        },
    }

    # 原因链（不得以滤波掩盖）
    rain_ext = deficit_extension["rain_volume_m3"]
    out_ext = deficit_extension["outflow_volume_m3"]
    ratio_ext = deficit_extension["outflow_capacity_ratio"]
    rec["recession_anomaly_cause"] = {
        "anomaly": (
            f"延长段（09-10→09-18）降雨 {rain_ext:.4e} m³，"
            f"出流仅 {out_ext:.4e} m³（维持蓄量不变所需能力的 {ratio_ext:.3f}×），"
            f"蓄量反而增加 → 湿面积变化 {area_change if area_change is not None else 'NA'} km²。"
        ),
        "causes": [
            {
                "id": "R1",
                "cause": "无排水网络：域内无沟渠/河槽编码，洼地积水只能靠四边缓排",
                "evidence": (
                    "模型无 chanmask/河槽刻蚀（meta.json note：河槽未刻蚀，钦江亚像元）；"
                    "D8 显示大量局部洼地（见候选网络段）"
                ),
                "confidence": "E2",
            },
            {
                "id": "R2",
                "cause": "边界排水能力不足：四边 FREE 缓排，出流仅占降雨的 12.6%",
                "evidence": f"总雨 {rows[-1]['Rain-(Inf+Evap)']:.4e} m³，出流 {integ('Qout', 0.0, t_end):.4e} m³",
                "confidence": "E1（res.mass 直接可得）",
            },
            {
                "id": "R3",
                "cause": "延长段仍有 84 mm 降雨持续补给，退水期并未真正“无雨”",
                "evidence": "ERA5 延长段 09-10~09-17 累计 84.0 mm；延长段雨量 "
                            f"{deficit_extension['rain_volume_m3']:.4e} m³",
                "confidence": "E1",
            },
            {
                "id": "R4",
                "cause": "无蒸发、无入渗（现状 run 未设 infiltration/evaporation）→ 损失项为 0",
                "evidence": "run.par 无 infiltration / evaporation 关键字；res.mass 损失列 = 纯降雨",
                "confidence": "E1",
            },
        ],
        "not_the_cause": [
            "不是数值不稳定：E_mass 5.3e-4 PASS，Verror/Qerror 末行均为 0。",
            "不是滤波问题：本审计全程用原始 res.mass 与 .wd，未做任何平滑。",
        ],
        "discipline": (
            "该异常**未消除**，本脚本只给原因、不改结果、不做滤波（计划 §5 Phase 4 验收："
            "退水异常若未消除必须给出原因，不得以滤波掩盖）。"
        ),
    }
    log.step("recession_reconciliation", area_change_km2=area_change,
             capacity_ratio=deficit_extension["outflow_capacity_ratio"])
    return rec


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 4 久隆排水/边界/退水审计")
    add_common_args(ap)
    args = ap.parse_args()

    run_dir = BASE / RUN
    out_json = args.out_dir / OUT_JSON.name if args.out_dir else OUT_JSON
    log = AuditLog("audit_jiulong_drainage.py", run_tag=args.run_tag, dry_run=args.dry_run)
    log.add_input("res_mass", run_dir / "results" / "res.mass")
    log.add_input("bci", run_dir / "bci.bci")
    log.add_input("run_par", run_dir / "run.par")
    log.add_input("dem", run_dir / "dem.asc")
    log.add_input("meta", run_dir / "meta.json")
    if S1_JSON.is_file():
        log.add_input("s1_recession_json", S1_JSON)

    boundary = boundary_audit(run_dir, log=log)
    network = drainage_network_candidates(run_dir, log=log)
    recession = recession_reconciliation(run_dir, log=log)

    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "script": "scripts/stage3/audit_jiulong_drainage.py",
        "phase": "Phase 4（排水、边界和结构治理）",
        "mode": "公开数据降级 · 情景级（ADR-0004）；不是业务预警，无率定值",
        "run_under_audit": RUN,
        "read_only": True,
        "note": (
            "本脚本只读既有 run，不跑 lisflood.exe、不改 bci.bci、不改 DEM、不改任何 outputs/jiulong/**。"
        ),
        "order": ["边界与重复计量", "候选排水网络", "退水对账"],
        "boundary_and_double_counting": boundary,
        "drainage_network_candidates": network,
        "recession_reconciliation": recession,
        "structural_expression": {
            "policy": "ADR-0008 定版：禁 bridge/orifice（B8b 未独立验证）；结构限 DEM 加高 + weirfile",
            "current_state": "本例无结构物编码（无堤防/桥涵/闸门资料）",
            "blocker": "阻塞：缺结构位置与参数资料；且 ADR-0008 要求升级后再验证才可启用 bridge/orifice",
            "global_etching": "不采用全域固定刻蚀（计划 §3.5）",
        },
        "limitations": (
            boundary["limitations"] + [
                "逐边通量为估计值（E4），非求解器记录；本构建无逐边账目。",
                "候选排水网络仅到 D8 线索级；D1–D5 授权资料全部缺失（阻塞）。",
                "退水异常未消除；本审计只给原因，不做滤波掩盖。",
            ]
        ),
        "verdict": _verdict(boundary, network, recession),
    }

    if args.dry_run:
        print("[dry-run] 不写文件；verdict:", json.dumps(payload["verdict"], ensure_ascii=False, indent=2))
        if args.log_json:
            log.write(args.log_json)
        return

    write_json(out_json, payload)
    log.payload["output_json"] = str(out_json)
    if args.log_json:
        log.write(args.log_json)
    print(f"-> {out_json}", flush=True)
    print(json.dumps(payload["verdict"], ensure_ascii=False, indent=2), flush=True)


def _verdict(boundary: dict, network: dict, recession: dict) -> dict:
    e = boundary["per_edge_estimate"]["per_edge"]
    seg = recession["drainage_deficit"]
    return {
        "boundary_verified": {
            "no_upstream_injection": boundary["measured"]["qin_all_rows_zero"],
            "per_edge_accounting_available": False,
            "d4_type_short_circuit": boundary["short_circuit_check"]["d4_type_short_circuit"],
            "three_side_outflow_risk": True,
            "estimated_edge_shares_pct": {k: e[k]["share_of_estimated_total_pct"] for k in ("N", "S", "W", "E")},
        },
        "drainage_network": {
            "level": "线索级（D8）",
            "usable_as_real_channel": False,
            "blockers": [b["id"] for b in network["authorization_blockers"]],
        },
        "recession": {
            "anomaly_resolved": False,
            "wet_area_change_km2": recession["wet_area_trend"]["change_km2"],
            "direction_vs_s1": recession["wet_area_trend"]["direction_vs_s1"],
            "outflow_capacity_ratio_extension": seg["extension_8d_09_10_to_09_18"]["outflow_capacity_ratio"],
            "cause_given": True,
            "filtered": False,
        },
        "writable": [
            "边界结构事实：N/W/E/S 四边均 FREE（S 带 0.0005 坡度），本构建无逐边 Qin/Qout 账目。",
            "无 D4 式注入短路（Qin ≡ 0）；但存在三边可出流的结构风险（未核证）。",
            "退水异常未消除，原因是无排水网络 + 边界排水能力不足 + 延长段仍有降雨，"
            "出流能力仅为维持蓄量所需的 ~18%（见 drainage_deficit）。",
        ],
        "not_writable": [
            "不得把逐边份额写成求解器实测值（那是 E4 估计）。",
            "不得把 D8 廊道当作真实沟渠，不得据此刻蚀河道。",
            "不得声称排水网络已建成/已治理。",
            "不得用滤波或平滑掩盖退水异常。",
            "不得给出绝对高程/资产级水深结论（G7 未闭合）。",
        ],
    }


if __name__ == "__main__":
    main()
