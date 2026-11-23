"""久隆河道—洪泛区交换与退水对账（R2a/R2/R3/R4，只读诊断，不重跑）。

审计项
1. **∫Qin 对账**：res.mass 梯形 ∫Qin（parse_res_mass 口径） vs QVAR 边界理论积分。
   理论积分**优先读 run 目录内 `inflow.bdy` 副本**（字节级组装拷贝，provenance 精确；
   2026-11-22 实测 data/interim 源文件在执行期间被重排，run 局部副本与源不一致），
   缺失才回退 data/interim 源并告警。源码口径：`boundary.cpp` QVAR5 每条边界边
   `q = 文件值 × dx`；`input.cpp` N 缘 `BCi1=(int)((start−xll)/dx)`、
   `BCi2=(int)((finish−xll)/dx)−1`，bci 段展开 n_edges 条边（本实验 3 格宽 → ×3）：
   - `int_qin_bdy_segment_m3` = trapz(值×dx×n_edges)（源码口径，**1% 门对照项**）；
   - `int_qin_bdy_design_m3` = trapz(值×dx)（文件值=Q_m3s/cellsize 单格设计，仅参照）。
   截断到模拟窗（SIM_SECONDS）后梯形积分。
2. **入口积水扩散**：r2a（无刻槽对照） vs r2（SGC），前 3 帧湿格（depth>0.05）
   自入口格 [0,156] 的 8 邻域 BFS 最远距离（格×200 m；只在湿格内传播）。
3. **洪泛区交换证据**：河道影响区（dilate1(sgc_width>0)，全 run 同一几何）之外
   湿面积在峰值帧 vs 末帧的差（负值 = 洪泛区水退出/收缩）。
4. **来水体积 vs 蓄水**：∫Qin（delivered） vs ΔV=V_end−V0−V_rain（stored）；
   差值 ≈ ∫Qout，只记数不作门。

证据等级：情景级（E3）；质量门 PASS ≠ 预测正确（ADR-0006/0010）。
产出 `diagnostics/jiulong/coupling/coupling_audit.json`；审计
`logs/diagnostics/audit_jiulong_coupling.json`。缺 run 跳过（status="missing"）；
已有报告拒绝覆盖，除非 --run-tag（tag 子目录）。

用法（先设 `$env:PYTHONUTF8="1"`）::

    uv --directory 2d_hydro_project run python scripts/stage6/audit_jiulong_coupling.py --dry-run
"""
from __future__ import annotations

import json
import sys
from argparse import ArgumentParser
from collections import deque
from pathlib import Path

import numpy as np

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import (  # noqa: E402
    AuditLog,
    GridError,
    add_common_args,
    load_sea_mask,
    read_ascii_header,
    sha256_file,
    utc_now,
    write_json,
)
from scripts.common.paths import rel_posix  # noqa: E402
from scripts.stage3.run_jiulong_dual_forcing import SIM_SECONDS, parse_res_mass  # noqa: E402
from scripts.stage6.compare_jiulong_scenarios import (  # noqa: E402
    CHANNEL_WIDTH_ASC,
    ENTRY_CELL,
    EVENT_DIR,
    RUN_NAMES,
    WET_THRESHOLD_M,
    dilate1,
    guard_report_target,
    load_channel_mask,
    load_frame_masks,
    rel_or_name,
    resolve_out_dir,
)

INFLOW_BDY = PROJ / "data/interim/jiulong/hydrology/qinjiang_event_5d.bdy"
DEFAULT_OUT_DIR = PROJ / "diagnostics/jiulong/coupling"
DEFAULT_LOG = PROJ / "logs/diagnostics/audit_jiulong_coupling.json"
AUDIT_KEYS = ("r2a", "r2", "r3", "r4")  # 含来水边界的 run（r2a 为无刻槽护栏对照）
QVAR_CELL_M = 200.0
REL_DEV_GATE = 0.01

_NEI8 = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))


def parse_qvar_bdy(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """QVAR bdy → (times_s, 文件值)。值×cellsize = 单格设计 Q_m3s（值在前、时间在后）。"""
    lines = [ln.strip() for ln
             in Path(path).read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    if len(lines) < 4:
        raise GridError(f"{path} 行数不足，不是有效 bdy")
    n_declared = int(lines[2].split()[0])
    times: list[float] = []
    vals: list[float] = []
    for ln in lines[3:3 + n_declared]:
        parts = ln.split()
        if len(parts) < 2:
            continue
        vals.append(float(parts[0]))  # 值在前、时间在后
        times.append(float(parts[1]))
    if not times:
        raise GridError(f"{path} 无数据行")
    return np.asarray(times), np.asarray(vals)


def qvar_n_edges(bci_path: Path, xll: float, cellsize_m: float) -> int | None:
    """bci QVAR 段展开的边界边数（input.cpp：BCi1=(int)((start−xll)/dx)，BCi2 同式 −1）。"""
    if not bci_path.is_file():
        return None
    for ln in bci_path.read_text(encoding="ascii", errors="replace").splitlines():
        parts = ln.split()
        if len(parts) >= 4 and parts[0].upper() == "N" and parts[3].upper() == "QVAR":
            start, finish = float(parts[1]), float(parts[2])
            i1 = int((start - xll) / cellsize_m)
            i2 = int((finish - xll) / cellsize_m) - 1
            return max(i2 - i1 + 1, 0)
    return None


def trapz_clip(times: np.ndarray, values: np.ndarray, t_max: float | None = None) -> float:
    """梯形积分；t_max 截断（越界末点按全序列线性内插补到 t_max）。"""
    t_all = np.asarray(times, dtype=float)
    v_all = np.asarray(values, dtype=float)
    t, v = t_all, v_all
    if t_max is not None:
        keep = t_all <= t_max
        t, v = t_all[keep], v_all[keep]
        if t.size and t[-1] < t_max:
            t = np.append(t, t_max)
            v = np.append(v, float(np.interp(t_max, t_all, v_all)))
    if t.size < 2:
        return 0.0
    trapezoid = getattr(np, "trapezoid", None) or np.trapz  # numpy 2.x 改名兼容
    return float(trapezoid(v, t))


def wet_bfs_max_distance(
    wet: np.ndarray, entry: tuple[int, int]
) -> tuple[np.ndarray, int, bool]:
    """入口格 8 邻域 BFS（只在湿格内传播）→ (距离格数矩阵, 最远格数, 入口是否湿)。"""
    wet = np.asarray(wet, dtype=bool)
    h, w = wet.shape
    dist = np.full((h, w), -1, dtype=np.int32)
    r0, c0 = entry
    if not (0 <= r0 < h and 0 <= c0 < w) or not wet[r0, c0]:
        return dist, 0, False
    dist[r0, c0] = 0
    max_d = 0
    queue: deque[tuple[int, int]] = deque([(r0, c0)])
    while queue:
        r, c = queue.popleft()
        for dr, dc in _NEI8:
            nr, nc = r + dr, c + dc
            if 0 <= nr < h and 0 <= nc < w and wet[nr, nc] and dist[nr, nc] < 0:
                dist[nr, nc] = dist[r, c] + 1
                max_d = max(max_d, int(dist[nr, nc]))
                queue.append((nr, nc))
    return dist, max_d, True


def load_mass(run_dir: Path) -> dict | None:
    """verify.json 的 mass 优先，缺失回退 parse_res_mass（同口径）。"""
    vpath = run_dir / "verify.json"
    if vpath.is_file():
        try:
            mass = json.loads(vpath.read_text(encoding="utf-8")).get("mass")
            if mass:
                return mass
        except json.JSONDecodeError:
            pass
    mass_path = run_dir / "results" / "res.mass"
    if mass_path.is_file():
        try:
            return parse_res_mass(mass_path)
        except (RuntimeError, OSError):
            return None
    return None


def audit_run(key: str, channel_dil: np.ndarray | None, cell_m: float,
              fallback_bdy: tuple[np.ndarray, np.ndarray] | None,
              xll: float) -> dict:
    """单 run 耦合审计（∫Qin 对账 / 入口扩散 / 交换 / 体积对账）。"""
    run_dir = EVENT_DIR / RUN_NAMES[key]
    rec: dict = {"run": key, "name": RUN_NAMES[key], "run_dir": rel_posix(run_dir), "status": "ok"}
    if not run_dir.is_dir() or not (run_dir / "results" / "res.max").is_file():
        rec["status"] = "missing"
        return rec
    frames = load_frame_masks(run_dir, None)
    if not frames:
        rec["status"] = "missing"
        rec["error"] = "无 res-*.wd 帧"
        return rec
    shape = frames[0][1].shape
    try:
        sea = load_sea_mask(run_dir, shape)
        if sea is not None:
            frames = [(s, w & ~sea) for s, w in frames]
    except GridError as exc:
        rec["sea_mask_error"] = str(exc)
    rec["n_frames"] = len(frames)

    mass = load_mass(run_dir)
    int_qin = float(mass.get("v_in_integrated_m3") or 0.0) if mass else None
    rec["qin_reconciliation"] = {"int_qin_res_mass_m3": None if int_qin is None else round(int_qin, 3)}
    if int_qin is None:
        rec["qin_reconciliation"]["note"] = "res.mass 缺失，未对账"

    # ∫Qin 理论值：优先 run 目录内 bdy 副本（provenance 精确），回退 data/interim 源
    local_bdy = run_dir / "inflow.bdy"
    bdy_src = None
    if local_bdy.is_file():
        bdy_src = ("run_local", local_bdy)
    elif fallback_bdy is not None:
        bdy_src = ("interim_fallback", INFLOW_BDY)
    if bdy_src is not None and int_qin is not None:
        try:
            times, vals = parse_qvar_bdy(bdy_src[1])
            n_edges = qvar_n_edges(run_dir / "bci.bci", xll, cell_m)
            q_design = vals * cell_m
            int_design = trapz_clip(times, q_design, t_max=SIM_SECONDS)
            rec["qin_reconciliation"].update({
                "bdy_source": bdy_src[0],
                "bdy_path": rel_posix(bdy_src[1]),
                "n_edges_qvar": n_edges,
                "int_qin_bdy_design_m3": round(int_design, 3),
            })
            if n_edges:
                int_seg = trapz_clip(times, q_design * n_edges, t_max=SIM_SECONDS)
                rel_dev = abs(int_qin - int_seg) / max(int_seg, 1.0)
                rec["qin_reconciliation"].update({
                    "int_qin_bdy_segment_m3": round(int_seg, 3),
                    "rel_dev_vs_segment": round(rel_dev, 6),
                    "gate_1pct": "PASS" if rel_dev <= REL_DEV_GATE else "FAIL",
                    "empirical_multiplier_vs_design": round(int_qin / max(int_design, 1.0), 4),
                })
            else:
                rec["qin_reconciliation"]["gate_1pct"] = None
                rec["qin_reconciliation"]["note"] = "bci 无 QVAR 段，segment 口径不可用"
        except (GridError, OSError) as exc:
            rec["qin_reconciliation"]["error"] = str(exc)

    spreads = []

    spreads = []
    for seq, wet in frames[:3]:
        _, max_cells, reached = wet_bfs_max_distance(wet, ENTRY_CELL)
        spreads.append({"seq": seq, "entry_wet": reached,
                        "max_dist_cells": int(max_cells),
                        "max_dist_m": round(max_cells * cell_m, 1)})
    rec["entry_spread_first3"] = spreads
    rec["entry_spread_max_m"] = max((s["max_dist_m"] for s in spreads), default=None)

    if channel_dil is not None and channel_dil.shape == shape:
        cell_area = cell_m * cell_m

        def outside_km2(wet: np.ndarray) -> float:
            return round(float((wet & ~channel_dil).sum()) * cell_area / 1e6, 4)

        total = [int(w.sum()) for _, w in frames]
        peak_i = int(np.argmax(total))
        end_i = len(frames) - 1
        rec["floodplain_exchange"] = {
            "region": "dilate1(sgc_width>0) 之外（全 run 同一几何，r2a 作对照参照区）",
            "peak_seq": frames[peak_i][0],
            "outside_wet_km2_peak": outside_km2(frames[peak_i][1]),
            "end_seq": frames[end_i][0],
            "outside_wet_km2_end": outside_km2(frames[end_i][1]),
            "delta_km2_end_minus_peak": round(
                outside_km2(frames[end_i][1]) - outside_km2(frames[peak_i][1]), 4),
        }

    if mass:
        v0 = float(mass.get("v0_m3") or 0.0)
        ve = float(mass.get("v_end_m3") or 0.0)
        vr = float(mass.get("v_rain_m3") or 0.0)
        delivered = float(mass.get("v_in_integrated_m3") or 0.0)
        stored = ve - v0 - vr
        rec["volume_check"] = {
            "delivered_int_qin_m3": round(delivered, 3),
            "stored_delta_v_m3": round(stored, 3),
            "residual_m3": round(delivered - stored, 3),
            "stored_frac_of_delivered": round(stored / delivered, 4) if delivered > 0 else None,
            "note": "residual ≈ ∫Qout（含南缘 FREE 出流），只记数不作门",
        }
    return rec


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__)
    add_common_args(parser)
    args = parser.parse_args(argv)
    log = AuditLog("audit_jiulong_coupling", run_tag=args.run_tag, dry_run=args.dry_run)
    log.param(audit_keys=AUDIT_KEYS, entry_cell=list(ENTRY_CELL))
    log.add_input("qvar_bdy", INFLOW_BDY)

    bdy: tuple[np.ndarray, np.ndarray] | None = None
    try:
        bdy = parse_qvar_bdy(INFLOW_BDY)
        log.step("bdy_parsed", n_points=int(bdy[0].size),
                 t_span_s=[float(bdy[0][0]), float(bdy[0][-1])])
    except (GridError, OSError) as exc:
        log.warn(f"data/interim QVAR bdy 不可读（各 run 若无局部副本则 ∫Qin 对账跳过）：{exc}")

    cell_m = QVAR_CELL_M
    xll = 0.0
    channel_dil: np.ndarray | None = None
    try:
        hdr = read_ascii_header(CHANNEL_WIDTH_ASC)
        cell_m = float(hdr["cellsize"])
        xll = float(hdr["xllcorner"])
        channel_dil = dilate1(load_channel_mask())
    except (GridError, OSError) as exc:
        log.warn(f"河道掩膜不可读（cellsize 回退 {cell_m} m），交换项跳过：{exc}")
    log.param(cellsize_m=cell_m, xllcorner=xll)

    runs = {key: audit_run(key, channel_dil, cell_m, bdy, xll) for key in AUDIT_KEYS}
    for key, rec in runs.items():
        if rec["status"] != "ok":
            log.warn(f"{key} 缺失（status={rec['status']}），跳过")
    log.step("audited", available=[k for k, v in runs.items() if v["status"] == "ok"])

    payload = {
        "script": "scripts/stage6/audit_jiulong_coupling.py",
        "generated_utc": utc_now(),
        "purpose": "河道—洪泛区交换与退水对账（R2a/R2/R3/R4，只读诊断）",
        "evidence_level": "E3（情景级模拟诊断，非实测；质量门 PASS ≠ 预测正确）",
        "not_independent_validation": True,
        "method": {
            "qin_reconciliation": "res.mass 梯形 ∫Qin vs run 局部 inflow.bdy 理论积分；"
                                  "源码口径 Q=值×dx×n_edges（boundary.cpp QVAR5 每边 ×dx，"
                                  "input.cpp BCi1..BCi2 段展开），门 ≤1% 对照 segment 口径；"
                                  "design 口径（值×dx 单格，任务书参照）并列记录",
            "entry_spread": f"前 3 帧 wet(depth>{WET_THRESHOLD_M}) 自入口 {list(ENTRY_CELL)} 的 "
                            "8 邻域 BFS 最远距离（只在湿格内传播）",
            "floodplain_exchange": "dilate1(sgc_width>0) 之外湿面积：峰值帧 vs 末帧",
            "volume_check": "∫Qin vs (V_end − V0 − V_rain)；residual ≈ ∫Qout",
        },
        "bdy": {
            "source": rel_posix(INFLOW_BDY),
            "sha256": sha256_file(INFLOW_BDY) if INFLOW_BDY.is_file() else None,
            "n_points": int(bdy[0].size) if bdy is not None else None,
            "t_span_s": [float(bdy[0][0]), float(bdy[0][-1])] if bdy is not None else None,
            "provenance_note": "理论积分优先用 run 目录内 inflow.bdy 副本；data/interim 源文件"
                               "曾在执行期间重排（2026-11-22），run 副本与源可能不一致",
        },
        "runs": runs,
        "entry_spread_comparison": {
            "r2a_vs_r2": "无刻槽对照 vs SGC：前 3 帧入口湿距最大值对比",
            "r2a_max_m": runs.get("r2a", {}).get("entry_spread_max_m"),
            "r2_max_m": runs.get("r2", {}).get("entry_spread_max_m"),
        },
        "caveats": [
            "∫Qin 对账门 ≤1% 只验证边界驱动被模型如量摄入，不代表淹没结果正确。",
            "对账以 run 局部 inflow.bdy 为准；若 run 间局部副本版本不同，其 ∫Qin 差异含驱动力差异，"
            "不全是地形/河道效应。",
            "入口扩散只在湿格连通体内传播；入口格未湿时记 entry_wet=false、距离 0。",
            "洪泛区交换用同一河道参照区几何（含无刻槽 r2a），仅作 run 间可比证据。",
            "本审计不是独立验证；S1 仅灾前/退水两景趋势参照（ADR-0010）。",
        ],
    }

    out_dir = resolve_out_dir(args.out_dir, args.run_tag, default=DEFAULT_OUT_DIR)
    out_json = out_dir / "coupling_audit.json"
    if not args.dry_run:
        try:
            guard_report_target(out_json)
        except FileExistsError as exc:
            log.error(str(exc))
            print(f"FAIL: {exc}")
            return 2
        write_json(out_json, payload)
        log.step("written", outputs=[rel_or_name(out_json)])
        print(f"out: {out_json}")
    else:
        print("dry-run: 未写审计 JSON")
    for key, rec in runs.items():
        q = rec.get("qin_reconciliation", {})
        print(f"{key:<4} status={rec['status']} intQin={q.get('int_qin_res_mass_m3')} "
              f"seg={q.get('int_qin_bdy_segment_m3')} rel_dev={q.get('rel_dev_vs_segment')} "
              f"gate={q.get('gate_1pct')} spread_max_m={rec.get('entry_spread_max_m')}")

    tag = f".{args.run_tag}" if args.run_tag else ""
    log_path = args.log_json or (DEFAULT_LOG.parent / f"{DEFAULT_LOG.stem}{tag}.json")
    log.write(log_path)
    print(f"log: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
