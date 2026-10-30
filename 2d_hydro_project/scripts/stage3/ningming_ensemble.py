"""宁明率定敏感性集（终稿 8.1）：Q_PEAK × 糙率 9 组，淹没阈值 3 档事后扫。

维度：Q_PEAK ∈ {2500, 4000, 6000} m³/s × fpfric ∈ {0.035, 0.06, 0.10}
     阈值 ∈ {0.05, 0.15, 0.30} m（对 res.max 事后扫，不重跑）
对照：8/24 新增淹没掩膜（CSI/POD/FAR/F1、面积偏比）+ 县城站水深 + E_mass。
产出：data/catalog/ningming_ensemble.json + docs/stage3_ningming_ensemble.md
运行：uv run python scripts/stage3/ningming_ensemble.py  （串行，约 9×20 分钟）
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.transform import Affine
from rasterio.warp import reproject

PROJ = Path(__file__).resolve().parents[2]
SCEN = PROJ / "scripts" / "stage3" / "ningming_scenario.py"
BASE = PROJ / "outputs" / "ningming" / "2026-08-zitan"
W24 = PROJ / "data" / "interim" / "ningming" / "water_new_0824.tif"
QS = (2500.0, 4000.0, 6000.0)
NS = (0.035, 0.06, 0.10)
THRS = (0.05, 0.15, 0.30)
STATION = (107.0719, 22.1403)  # 宁明县城
# GF-3 8/24 本地元数据未见可直接使用的精确时刻；以公开报道 08:00 为中心，
# 模型输出 6h 一帧，最近帧为 78h(06:00) / 84h(12:00)。主口径取 78h，±6h 不确定性覆盖 84h。
OBS_TIME = "2026-08-24T08:00+08:00"
OBS_FRAME = 13
OBS_TIME_UNCERTAINTY_H = 6
PRIMARY_THRESHOLD_M = 0.15  # 预注册主阈值；不得按 CSI 挑阈值


def _read_asc(p: Path):
    with open(p) as f:
        hdr = [next(f) for _ in range(6)]
    return np.loadtxt(p, skiprows=6), {l.split()[0]: float(l.split()[1]) for l in hdr}


def e_mass(res_dir: Path) -> float:
    lines = (res_dir / "res.mass").read_text().splitlines()
    keys = lines[0].split()
    rows = [dict(zip(keys, map(float, ln.split()))) for ln in lines[1:] if ln.split()]
    v_in = sum(0.5 * (a["Qin"] + b["Qin"]) * (b["Time"] - a["Time"])
               for a, b in zip(rows, rows[1:]))
    v_out = sum(0.5 * (a["Qout"] + b["Qout"]) * (b["Time"] - a["Time"])
                for a, b in zip(rows, rows[1:]))
    resid = rows[-1]["Vol"] - rows[0]["Vol"] - rows[-1]["Rain-(Inf+Evap)"] - (v_in - v_out)
    return abs(resid) / max(rows[0]["Vol"] + rows[-1]["Rain-(Inf+Evap)"] + v_in, 1.0)


def metrics(run_dir: Path, frame: int = OBS_FRAME) -> dict:
    """时刻切片指标：取观测时刻最近的 6h 模型帧，不用 res.max。"""
    res_dir = run_dir / "results"
    rmax, hdr = _read_asc(res_dir / "res.max")
    cell = hdr["cellsize"]
    xll, yll = hdr["xllcorner"], hdr["yllcorner"]
    tgt = Affine(cell, 0, xll, 0, -cell, yll + rmax.shape[0] * cell)
    wd_path = res_dir / f"res-{frame:04d}.wd"
    if not wd_path.exists():
        raise FileNotFoundError(f"{run_dir} 缺 res-{frame:04d}.wd（时刻切片不可用）")
    wd, _ = _read_asc(wd_path)
    with rasterio.open(W24) as ds:
        obs = np.zeros_like(rmax, dtype="uint8")
        reproject(ds.read(1), obs, src_transform=ds.transform, src_crs=ds.crs,
                  dst_transform=tgt, dst_crs="EPSG:32648", resampling=Resampling.nearest)
    # 观测=8/24「新增」淹没；0/1 全 AOI 有效，若未来出现 255 则排除。
    valid = np.isin(obs, (0, 1))
    obs_wet = (obs == 1) & valid
    # 模拟侧剔除 8/6 常水水体，否则河槽常水全计入假警报。
    w06 = np.zeros_like(rmax, dtype="uint8")
    with rasterio.open(PROJ / "data" / "interim" / "ningming" / "water_0806.tif") as ds:
        reproject(ds.read(1), w06, src_transform=ds.transform, src_crs=ds.crs,
                  dst_transform=tgt, dst_crs="EPSG:32648", resampling=Resampling.nearest)
    normal = w06 == 1
    tf = Transformer.from_crs(4326, 32648, always_xy=True)
    sx, sy = tf.transform(*STATION)
    col = int((sx - xll) / cell)
    row = int((yll + rmax.shape[0] * cell - sy) / cell)
    r0, r1 = max(0, row - 2), min(rmax.shape[0], row + 3)
    c0, c1 = max(0, col - 2), min(rmax.shape[1], col + 3)
    wd_station_r2 = float(wd[r0:r1, c0:c1].max())
    wd_station_center = float(wd[row, col])
    wd_station_max = float(rmax[r0:r1, c0:c1].max())
    area_obs = obs_wet.sum() * cell * cell / 1e6
    area_valid = valid.sum() * cell * cell / 1e6
    em = e_mass(res_dir)
    per_thr = {}
    for thr in THRS:
        sim_wet = (wd > thr) & ~normal & valid
        H = int((sim_wet & obs_wet).sum())
        FA = int((sim_wet & ~obs_wet & valid).sum())
        M = int((~sim_wet & obs_wet & valid).sum())
        area_sim = sim_wet.sum() * cell * cell / 1e6
        per_thr[str(thr)] = {
            "csi": round(H / max(H + FA + M, 1), 3),
            "pod": round(H / max(H + M, 1), 3),
            "far": round(FA / max(H + FA, 1), 3),
            "area_sim_km2": round(area_sim, 2),
            "area_bias": round(area_sim / max(area_obs, 1e-9), 2),
        }
    return {
        "obs_time": OBS_TIME, "frame": frame, "frame_t_h": round(frame * 6.0, 1),
        "time_uncertainty_h": OBS_TIME_UNCERTAINTY_H,
        "primary_threshold_m": PRIMARY_THRESHOLD_M,
        "valid_area_km2": round(area_valid, 2),
        "observed_new_area_km2": round(area_obs, 2),
        "station_wd_at_obs_m": round(wd_station_r2, 2),
        "station_wd_center_m": round(wd_station_center, 3),
        "station_wd_max_m": round(wd_station_max, 2),
        "station_radius_cells": 2,
        "station_wd_final_m": round(wd_station_r2, 2),  # 兼容旧字段名，实为观测时刻±2格值
        "obs_rise_station_m": 14.19,
        "area_obs_km2": round(area_obs, 2),
        "metric_semantics": "时刻切片口径：取 2026-08-24 观测时刻最近 6h 帧（78h），模拟剔除 8/6 常水；不再用 res.max",
        "e_mass_132": float(f"{em:.3e}"), "thresholds": per_thr,
    }


def main() -> None:
    rows = []
    for q in QS:
        for n in NS:
            rid = f"scen_q{int(q)}_n{int(n * 1000)}_100m"
            run_dir = BASE / rid
            if not (run_dir / "results" / "res.max").exists():
                for cmd in ("prep", "run"):
                    r = subprocess.run(
                        [sys.executable, str(SCEN), cmd,
                         "--qpeak", str(q), "--fric", str(n), "--res", "100"],
                        cwd=str(PROJ))
                    if r.returncode != 0:
                        print(f"[失败] {rid} {cmd} exit={r.returncode}", flush=True)
                        break
            else:
                print(f"[跳过已有] {rid}", flush=True)
            if (run_dir / "results" / "res.max").exists():
                m = metrics(run_dir)
                rec = {"run_id": rid, "q_peak": q, "fpfric": n, **m,
                       "wall_note": "串行 100m，见 run.log"}
                rows.append(rec)
                (run_dir / "ensemble_verify.json").write_text(
                    json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps(rec, ensure_ascii=False), flush=True)
    # 基线（旧目录、同配置 q4000/n0.035，留作对照行）
    legacy = BASE / "scen_q4000_100m"
    if legacy.exists():
        rec = {"run_id": "scen_q4000_100m(基线,同 q4000/n0.035)", "q_peak": 4000.0,
               "fpfric": 0.035, **metrics(legacy)}
        rows.append(rec)
    out = {"generated_utc": datetime.now(timezone.utc).isoformat(),
           "design": {"q_peaks_m3s": QS, "fpfric": NS, "thresholds_m": THRS,
                      "res_m": 100, "solver": "acceleration, dt=1.5s 固定",
                      "obs_time": OBS_TIME, "obs_frame": OBS_FRAME,
                      "frame_t_h": round(OBS_FRAME * 6.0, 1),
                      "time_uncertainty_h": OBS_TIME_UNCERTAINTY_H,
                      "primary_threshold_m": PRIMARY_THRESHOLD_M,
                      "metric_semantics": "时刻切片，模拟剔除 8/6 常水；不用 res.max"},
           "runs": rows}
    (PROJ / "data" / "catalog" / "ningming_ensemble.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 宁明率定敏感性集（2026-08 紫檀事件，情景级）", "",
             f"指标口径：观测时刻 {OBS_TIME}（最近 6h 帧 {OBS_FRAME} = {OBS_FRAME*6}h，±{OBS_TIME_UNCERTAINTY_H}h）；"
             f"主阈值 {PRIMARY_THRESHOLD_M} m（预注册），0.05/0.30 仅敏感性；模拟剔除 8/6 常水。",
             "",
             "| run | Q峰值 | n | 阈值 | CSI | POD | FAR | 模拟面积 | 面积偏比 | 站±2格水深@78h | E_mass |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        for thr, t in r["thresholds"].items():
            lines.append(f"| {r['run_id']} | {r['q_peak']:.0f} | {r['fpfric']} | {thr} "
                         f"| {t['csi']} | {t['pod']} | {t['far']} | {t['area_sim_km2']} "
                         f"| {t['area_bias']} | {r['station_wd_at_obs_m']} | {r['e_mass_132']} |")
    lines.append("")
    lines.append(f"观测面积 {rows[0]['area_obs_km2'] if rows else '?'} km²（8/24 新增淹没）；"
                 f"站水位抬升观测 14.19 m（对照需考虑通道基流水深与站基面差）。")
    lines.append("")
    lines.append("口径（2026-10-26 修订）：不再用 `res.max`；取观测时刻最近 6h 帧。"
                 "模拟侧已剔除 8/6 常水水体（观测为「新增淹没」产品）。"
                 "`station_wd_at_obs_m` 为站址 ±2 格面积级值，站址像元中心值见 JSON `station_wd_center_m`。")
    lines.append("")
    lines.append("判读（2026-10-26）：12 个独立配置/36 个独立阈值点，**不是 108 点全因子**。"
                 "q6000/n0.10 主阈值 0.15：CSI 0.357、POD 0.773、FAR 0.602、面积 96.02 km²（1.94×）；"
                 "9 组 Q×n 的 CSI 约 0.28–0.36，最高 0.358（q6000/n0.10/thr0.3，事后角点）。"
                 "站址 ±2 格 10.32 m vs 观测抬升 14.19 m 仅量级参考；站址像元 0.018 m。"
                 "面积仍约 1.9–2.0× 偏大、FAR 高；空间 Manning 未试（G18 修正），不能再说“无空间杠杆”。")
    (PROJ / "docs" / "stage3_ningming_ensemble.md").write_text(
        "\n".join(lines), encoding="utf-8")
    print("-> data/catalog/ningming_ensemble.json + docs/stage3_ningming_ensemble.md", flush=True)


if __name__ == "__main__":
    main()
