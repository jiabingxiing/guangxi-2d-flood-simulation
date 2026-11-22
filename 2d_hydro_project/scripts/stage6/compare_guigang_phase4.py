"""Phase 2+4：三个贵港物理情景与基线的逐帧斑块对照。

口径与基线诊断完全一致（不得另立一套）：
  - 湿格判定 depth > 0.05 m（float32）
  - 8 邻域连通域（scripts.common.patch_analysis.connected_components）
  - 单元面积从栅格头读取（cellsize**2），禁写死
  - 帧号 → 时刻：t = seq * saveint，saveint 从 run.par 读取（默认 21600）

输出：data/catalog/guigang_phase4_scenarios.json + 控制台摘要。

用法：
  uv --directory 2d_hydro_project run python scripts/stage6/compare_guigang_phase4.py
  # 只比某一帧
  ... --frames 18,56
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import (  # noqa: E402
    parse_run_par,
    read_ascii_grid,
    run_frames,
    sha256_file,
    utc_now,
    write_json,
)
from scripts.common.patch_analysis import connected_components  # noqa: E402

BASE = PROJ / "outputs" / "guigang" / "2024-09-yagi"
OUT_JSON = PROJ / "data" / "catalog" / "guigang_phase4_scenarios.json"

WET_THR = 0.05
BASELINE = "scen_yagi_q11500_100m_dt2p5_choff_realdem"
RUNS = {
    "baseline_realdem": BASELINE,
    "flow_only_v1": "scen_yagi_q11500_100m_dt2p5_choff_flow_only_v1",
    "dualloss_v1": "scen_yagi_q11500_100m_dt2p5_choff_dualloss_v1",
    "rainloss_high_v1": "scen_yagi_q11500_100m_dt2p5_choff_rainloss_high_v1",
}
# 关键判读时刻（Lead 指定）：t=108h（斑块暴增、Q 仍基流）、t=336h（末帧/洪峰后）
KEY_HOURS = (36.0, 72.0, 108.0, 144.0, 216.0, 336.0)


def frame_metrics(wd: np.ndarray, cell_area_m2: float) -> dict:
    """单帧斑块统计（与基线诊断同口径）。"""
    wet = np.isfinite(wd) & (wd > WET_THR)
    wet_cells = int(wet.sum())
    labels, n = connected_components(wet, connectivity=8)
    if n:
        sizes = np.bincount(labels.ravel())[1:]
        max_cells = int(sizes.max())
        n_le4 = int((sizes <= 4).sum())
        n_le9 = int((sizes <= 9).sum())
    else:
        sizes = np.empty(0, np.int64)
        max_cells = n_le4 = n_le9 = 0
    return {
        "wet_cells": wet_cells,
        "wet_area_km2": round(wet_cells * cell_area_m2 / 1e6, 2),
        "n_patches": int(n),
        "max_patch_cells": max_cells,
        "max_patch_km2": round(max_cells * cell_area_m2 / 1e6, 2),
        "max_patch_share_pct": round(100.0 * max_cells / wet_cells, 2) if wet_cells else 0.0,
        "n_le4_cells": n_le4,
        "n_le9_cells": n_le9,
        "n_le4_pct": round(100.0 * n_le4 / n, 2) if n else 0.0,
        "n_le9_pct": round(100.0 * n_le9 / n, 2) if n else 0.0,
        "max_depth_m": round(float(wd[wet].max()), 2) if wet_cells else 0.0,
        "wet_volume_m3": round(float(wd[wet].astype(np.float64).sum()) * cell_area_m2, 1)
        if wet_cells else 0.0,
    }


def mass_summary(run_dir: Path) -> dict:
    """res.mass 账目（E_mass 口径与 g15_mass_recheck 完全一致）。"""
    path = run_dir / "results" / "res.mass"
    if not path.is_file():
        return {}
    lines = path.read_text().splitlines()
    keys = lines[0].split()
    rows = [dict(zip(keys, map(float, ln.split()))) for ln in lines[1:] if ln.split()]
    v_in = sum(0.5 * (a["Qin"] + b["Qin"]) * (b["Time"] - a["Time"])
               for a, b in zip(rows, rows[1:]))
    v_out = sum(0.5 * (a["Qout"] + b["Qout"]) * (b["Time"] - a["Time"])
                for a, b in zip(rows, rows[1:]))
    v0, ve = rows[0]["Vol"], rows[-1]["Vol"]
    v_rain = rows[-1]["Rain-(Inf+Evap)"]
    resid = ve - v0 - v_rain - (v_in - v_out)
    e_mass = abs(resid) / max(v0 + v_rain + v_in, 1.0)
    return {
        "v0_m3": round(v0, 1), "v_end_m3": round(ve, 1),
        "v_rain_m3": round(v_rain, 1),
        "v_in_integrated_m3": round(v_in, 1),
        "v_out_integrated_m3": round(v_out, 1),
        "residual_m3": round(resid, 1),
        "e_mass_132": float(f"{e_mass:.3e}"),
        "gate_1e-3": "PASS" if e_mass <= 1e-3 else "FAIL",
        "qin_max_m3s": round(max(r["Qin"] for r in rows), 3),
        "qout_max_m3s": round(max(r["Qout"] for r in rows), 3),
        "n_mass_rows": len(rows),
        "t_end_s": rows[-1]["Time"],
    }


def analyse(tag: str, dirname: str, frames_wanted: set[int] | None) -> dict:
    run_dir = BASE / dirname
    if not run_dir.is_dir():
        return {"run": dirname, "missing": True}
    par = parse_run_par(run_dir)
    saveint = float(par.get("saveint", 21600.0))
    meta_p = run_dir / "meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.is_file() else {}
    rain = meta.get("rain", {})

    frames = run_frames(run_dir / "results")
    if not frames:
        return {"run": dirname, "no_frames": True, "meta_rain": rain}

    rmax_p = run_dir / "results" / "res.max"
    rmax = read_ascii_grid(rmax_p) if rmax_p.is_file() else None
    if rmax is not None:
        cell_area = rmax.cell_area_m2
        shape = rmax.shape
        grid = rmax.to_meta()
        max_m = frame_metrics(rmax.data, cell_area)
    else:
        # 仍在运行中：res.max 由求解器结束时写出。逐帧序列照样可算（.wd 已落地），
        # 但 res.max 汇总缺失——如实登记 in_progress，不伪造最终指标。
        probe = read_ascii_grid(frames[0][1])
        cell_area = probe.cell_area_m2
        shape = probe.shape
        grid = probe.to_meta()
        max_m = None

    series = []
    for seq, path in frames:
        if frames_wanted is not None and seq not in frames_wanted:
            continue
        wd = read_ascii_grid(path, expected_shape=shape)
        m = frame_metrics(wd.data, cell_area)
        m["seq"] = seq
        m["t_h"] = round(seq * saveint / 3600.0, 1)
        series.append(m)

    rec = {
        "run": dirname,
        "run_tag": tag,
        "grid": grid,
        "saveint_s": saveint,
        "n_frames": len(frames),
        "meta_rain": rain,
        "meta_scenario": meta.get("scenario"),
        "run_par": {k: v for k, v in par.items() if k != "acceleration"},
        "res_max": max_m,
        "mass": mass_summary(run_dir),
        "run_log_tail": (run_dir / "run.log").read_text(encoding="utf-8",
                                                        errors="replace")[-400:]
        if (run_dir / "run.log").is_file() else None,
        "series": series,
    }
    if rmax is not None:
        rec["res_max_sha256"] = sha256_file(rmax_p)
    else:
        rec["in_progress"] = True
        rec["note"] = ("results/res.max 尚未生成（run 未结束）；series 为已完成帧，"
                       "res_max/mass 不可用")
    return rec


def main() -> None:
    ap = argparse.ArgumentParser(description="贵港 Phase 2+4 情景与基线对照")
    ap.add_argument("--frames", default=None,
                    help="逗号分隔帧号，只统计这些帧（默认全部）")
    ap.add_argument("--out", type=Path, default=OUT_JSON)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    wanted = None
    if args.frames:
        wanted = {int(x) for x in args.frames.split(",") if x.strip()}

    out: dict = {"generated_utc": utc_now(),
                 "script": "scripts/stage6/compare_guigang_phase4.py",
                 "wet_threshold_m": WET_THR,
                 "connectivity": 8,
                 "key_hours": list(KEY_HOURS),
                 "runs": {}}
    for tag, dirname in RUNS.items():
        print(f"[{tag}] {dirname} ...", flush=True)
        out["runs"][tag] = analyse(tag, dirname, wanted)

    # 关键帧横向对照表
    key_rows: list[dict] = []
    for t_h in KEY_HOURS:
        row: dict = {"t_h": t_h}
        for tag, rec in out["runs"].items():
            if "series" not in rec:
                continue
            hit = next((s for s in rec["series"] if abs(s["t_h"] - t_h) < 1e-6), None)
            if hit:
                row[tag] = {k: hit[k] for k in
                            ("seq", "wet_cells", "wet_area_km2", "n_patches",
                             "max_patch_km2", "n_le9_cells", "max_depth_m")}
        key_rows.append(row)
    out["key_frame_comparison"] = key_rows

    print("\n=== res.max 汇总 ===")
    hdr = f"{'run':20s} {'wet_cells':>10s} {'wet_km2':>9s} {'patches':>8s} " \
          f"{'max_patch_km2':>13s} {'E_mass':>10s} {'gate':>5s}"
    print(hdr)
    for tag, rec in out["runs"].items():
        if rec.get("res_max") is None:
            state = "进行中" if rec.get("in_progress") else "缺失/未完成"
            n = len(rec.get("series", []))
            print(f"{tag:20s} ({state}, {rec.get('n_frames', 0)} 帧已写出, 本次统计 {n} 帧)")
            continue
        m = rec["res_max"]
        ms = rec.get("mass", {})
        print(f"{tag:20s} {m['wet_cells']:10d} {m['wet_area_km2']:9.2f} "
              f"{m['n_patches']:8d} {m['max_patch_km2']:13.2f} "
              f"{ms.get('e_mass_132', 0):10.3e} {ms.get('gate_1e-3', '?'):>5s}")

    print("\n=== 关键帧对照 ===")
    for row in key_rows:
        print(f"\nt = {row['t_h']:.0f} h")
        for tag in RUNS:
            if tag in row:
                d = row[tag]
                print(f"  {tag:20s} seq={d['seq']:3d} wet={d['wet_cells']:7d} "
                      f"({d['wet_area_km2']:8.2f} km2) patches={d['n_patches']:6d} "
                      f"max_patch={d['max_patch_km2']:8.2f} le9={d['n_le9_cells']:6d} "
                      f"maxdepth={d['max_depth_m']:.2f}")

    if not args.dry_run:
        write_json(args.out, out)
        print(f"\n-> {args.out}")


if __name__ == "__main__":
    main()
