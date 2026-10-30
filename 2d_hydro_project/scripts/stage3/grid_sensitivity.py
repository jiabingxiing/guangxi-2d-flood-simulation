"""Phase B1：案例级网格/时间步敏感性（终稿 §11 第 7 项）。

对三案例各跑一档"改网格"与一档"改时间步"，与原基线对比：
  宁明  100 m/dt1.5（基线，已有） vs 50 m/dt1.5 vs 100 m/dt3.0
  久隆  200 m/dt5（基线，已有）   vs 100 m/dt5  vs 200 m/dt2.5
  贵港  200 m/dt5（基线，已有）   vs 100 m/dt5  vs 200 m/dt2.5
指标（统一口径，全部由 results/res.max + res.mass 现算，不依赖各案例 verify 字段差异）：
  湿面积（wd>0.05，久隆剔除海面像元）、全域最大水深、站址像元水深、
  站址 200 m 半径内最大水深（分辨率公平）、E_mass、墙钟耗时。

用法：
  uv run python scripts/stage3/grid_sensitivity.py --case jiulong --res 100
  uv run python scripts/stage3/grid_sensitivity.py --case jiulong --dt 2.5
  uv run python scripts/stage3/grid_sensitivity.py --report      # 只重算汇总与文档
输出：data/catalog/sensitivity.json + docs/stage3_sensitivity.md

注意：30 m 全域由 Phase B5 补跑（贵港，河槽宽按米折算保持物理一致；--tag b5）；
5 m 局部仍由 ADR-0005 推迟。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from pyproj import Transformer

PROJ = Path(__file__).resolve().parents[2]
OUT_JSON = PROJ / "data" / "catalog" / "sensitivity.json"
OUT_MD = PROJ / "docs" / "stage3_sensitivity.md"

CASES = {  # noqa: 保持原结构；--tag 用于区分批次目录后缀（默认 b1）
    "jiulong": {
        "script": PROJ / "scripts" / "stage3" / "jiulong_scenarios.py",
        "scen": "free", "epsg": 32649, "station": (108.7161, 22.0638),
        "dir": PROJ / "outputs" / "jiulong" / "2024-09-yagi",
        "name": lambda res, dt: f"scen_free_{int(res)}m" + (
            f"_dt{str(dt).replace('.', 'p')}" if abs(dt - 5.0) > 1e-9 else ""),
        "baseline": lambda: "scen_free_200m",
        "prefix": "scen_free_",
        "sea_mask": "sea_mask.npy",
        "note": "雨驱动 5 天事件；无实测 Q(t)；站址 500kV 站坐标待业务确认（G12 邻域）",
    },
    "guigang": {
        "script": PROJ / "scripts" / "stage5" / "guigang_scenario.py",
        "scen": "q11500", "epsg": 32649, "station": (109.60, 23.11),
        "dir": PROJ / "outputs" / "guigang" / "2024-09-yagi",
        "name": lambda res, dt: f"scen_yagi_q11500_{int(res)}m" + (
            f"_dt{str(dt).replace('.', 'p')}" if abs(dt - 5.0) > 1e-9 else ""),
        "baseline": lambda: "scen_yagi_q11500_200m",
        "prefix": "scen_yagi_q11500_",
        "sea_mask": None,
        "note": "上游三角 Q 情景（峰 11500 m³/s）；河槽为直线参数化（宽度按米：200m=600m、"
                "100m_b1 旧口径 300m、100m_wfix≈700m、30m_b5≈630m）；垂直基准未确认（G7）",
    },
    "ningming": {
        "script": PROJ / "scripts" / "stage3" / "ningming_scenario.py",
        "scen": "q6000", "epsg": 32648, "station": (107.0719, 22.1403),
        "dir": PROJ / "outputs" / "ningming" / "2026-08-zitan",
        "name": lambda res, dt: f"scen_q6000_n100_{int(res)}m" + (
            f"_dt{str(dt).replace('.', 'p')}" if abs(dt - (1.5 if res <= 60 else 1.5)) > 1e-9 else ""),
        "baseline": lambda: "scen_q6000_n100_100m",
        "prefix": "scen_q6000_n100_",
        "sea_mask": None,
        "note": "上游三角 Q 情景（峰 6000 m³/s）；喀斯特损失未建模（B3 仅标量代理）",
    },
}


def read_asc(path: Path) -> tuple[np.ndarray, dict]:
    with open(path) as f:
        hdr = {}
        for _ in range(6):
            k, *v = next(f).split()
            hdr[k.lower()] = float(v[0]) if v else None
        arr = np.loadtxt(f)
    return arr, hdr


def e_mass(res_dir: Path) -> float:
    lines = (res_dir / "res.mass").read_text().splitlines()
    keys = lines[0].split()
    rows = [dict(zip(keys, map(float, ln.split()))) for ln in lines[1:] if ln.split()]
    v_in = sum(0.5 * (a["Qin"] + b["Qin"]) * (b["Time"] - a["Time"]) for a, b in zip(rows, rows[1:]))
    v_out = sum(0.5 * (a["Qout"] + b["Qout"]) * (b["Time"] - a["Time"]) for a, b in zip(rows, rows[1:]))
    resid = rows[-1]["Vol"] - rows[0]["Vol"] - rows[-1]["Rain-(Inf+Evap)"] - (v_in - v_out)
    return float(f"{abs(resid) / max(rows[0]['Vol'] + rows[-1]['Rain-(Inf+Evap)'] + v_in, 1.0):.3e}")


def metrics(case: str, run_dir: Path) -> dict:
    cfg = CASES[case]
    res_dir = run_dir / "results"
    rmax, hdr = read_asc(res_dir / "res.max")
    cell = hdr["cellsize"]
    mask = np.zeros(rmax.shape, dtype=bool)
    if cfg["sea_mask"] and (run_dir / cfg["sea_mask"]).exists():
        mask = np.load(run_dir / cfg["sea_mask"])
    wet = (rmax > 0.05) & ~mask
    tf = Transformer.from_crs(4326, cfg["epsg"], always_xy=True)
    sx, sy = tf.transform(*cfg["station"])
    col = int((sx - hdr["xllcorner"]) / cell)
    row = int((hdr["yllcorner"] + rmax.shape[0] * cell - sy) / cell)
    rad = max(1, int(round(200.0 / cell)))
    r0, r1 = max(0, row - rad), min(rmax.shape[0], row + rad + 1)
    c0, c1 = max(0, col - rad), min(rmax.shape[1], col + rad + 1)
    meta = {}
    mp = run_dir / "meta.json"
    if mp.exists():
        meta = json.loads(mp.read_text(encoding="utf-8"))
    return {
        "run_dir": run_dir.name, "res_m": cell, "dt_s": meta.get("dt"),
        "grid": [int(rmax.shape[1]), int(rmax.shape[0])],
        "wet_area_km2": round(float(wet.sum()) * cell * cell / 1e6, 2),
        "max_wd_m": round(float(rmax[~mask].max()), 2),
        "station_depth_center_m": round(float(rmax[row, col]), 3),
        "station_depth_r200m_m": round(float(rmax[r0:r1, c0:c1].max()), 3),
        "station_radius_cells": rad,
        "e_mass": e_mass(res_dir),
        "frames": len(sorted(res_dir.glob("res-*.wd"))),
    }


def run_case(case: str, res: float, dt: float, tag: str = "b1") -> dict:
    cfg = CASES[case]
    env = dict(os.environ)
    env["RUN_TAG"] = tag  # 敏感性跑统一进 _<tag> 目录，避免触碰首轮基线
    run_dir = cfg["dir"] / (cfg["name"](res, dt) + f"_{tag}")
    args = [str(cfg["script"])]
    if case == "ningming":
        args += ["prep"] + ([] if "--qpeak" in sys.argv else ["--qpeak", "6000"]) + \
                ([] if "--fric" in sys.argv else ["--fric", "0.10"])
    elif case == "jiulong":
        args += ["prep", cfg["scen"]]
    else:
        args += ["prep"]
    args += ["--res", str(res), "--dt", str(dt)]
    t0 = time.monotonic()
    for cmd in ("prep", "run"):
        a = list(args)
        a[1] = cmd
        r = subprocess.run([sys.executable] + a, cwd=str(PROJ), env=env)
        if r.returncode != 0:
            raise SystemExit(f"{case} {cmd} 失败 exit={r.returncode}")
    wall = time.monotonic() - t0
    rec = metrics(case, run_dir)
    rec["wall_s"] = round(wall, 1)
    return rec


def report() -> dict:
    out = {}
    for case, cfg in CASES.items():
        rows = {}
        base = cfg["dir"] / cfg["baseline"]()
        if (base / "results" / "res.max").exists():
            rows[base.name] = metrics(case, base)
        for d in sorted(cfg["dir"].iterdir()):
            if not d.is_dir() or d.name in rows:
                continue
            if "_v1" in d.name or "_repro" in d.name or "_interrupted" in d.name or "_sv" in d.name:
                continue  # 作废变体、重跑复验、被中断、输出间隔变体不进入敏感性表
            if "_inf" in d.name or "_nsp" in d.name or re.search(r"_r\d", d.name):
                continue  # 入渗/空间糙率/雨量折减属其它 Phase，不进本表
            if (d / "results" / "res.max").exists() and d.name.startswith(cfg["prefix"]):
                rows[d.name] = metrics(case, d)
        out[case] = {"note": cfg["note"], "runs": rows}
    return out


def write_outputs(payload: dict) -> None:
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 网格/时间步敏感性（Phase B1，终稿 §11 第 7 项）", "",
             f"生成：{payload['generated_utc']}　｜　情景级结论", "",
             "统一指标口径：`res.max`（wd>0.05）湿面积、全域最大水深（久隆剔除海面像元）、"
             "站址像元水深、站址 200 m 半径内最大水深（按分辨率折算格数）、`res.mass` 的 E_mass。", ""]
    for case, blk in payload["cases"].items():
        lines += [f"## {case}", "", f"- {blk['note']}", "",
                  "| run | 网格 | dt s | 湿面积 km² | 最大水深 m | 站址像元 m | 站址 r200 m | E_mass |",
                  "|---|---|---|---|---|---|---|---|"]
        for k, v in blk["runs"].items():
            lines.append(f"| {k} | {v['grid'][0]}×{v['grid'][1]} @ {v['res_m']:.0f} m "
                         f"| {v['dt_s']} | {v['wet_area_km2']} | {v['max_wd_m']} "
                         f"| {v['station_depth_center_m']} | {v['station_depth_r200m_m']} "
                         f"| {v['e_mass']} |")
        # 相对基线变化（基线=各案例首轮目录）
        base_name = next(iter(blk["runs"]))
        base = blk["runs"][base_name]
        lines += ["", f"相对基线（{base_name}）的变化：", ""]
        for k, v in blk["runs"].items():
            if k == base_name:
                continue
            da = (v["wet_area_km2"] / base["wet_area_km2"] - 1) * 100
            dm = (v["max_wd_m"] / base["max_wd_m"] - 1) * 100 if base["max_wd_m"] else float("nan")
            lines.append(f"- {k}：湿面积 {da:+.1f}%、最大水深 {dm:+.1f}%、"
                         f"站址像元 {base['station_depth_center_m']}→{v['station_depth_center_m']} m、"
                         f"站址 r200 {base['station_depth_r200m_m']}→{v['station_depth_r200m_m']} m。")
        lines.append("")
    lines += ["## 判读", "",
              "- 本表只回答“结果对网格/时间步有多敏感”，不改变任何门的通过状态。",
              "- 贵港 30 m 全域由 Phase B5 补跑（河槽宽按米参数化 ≈600 m，跨分辨率物理一致；"
              "100m_b1 为旧 3 格宽=300 m 口径，100m_wfix 为等宽对照 ≈700 m）——"
              "比较网格效应须用同宽度行对照；5 m 站区局部仍由 ADR-0005 推迟。",
              "- 站址 r200 m 在不同分辨率下格数不同，比较的是 200 m 物理半径内的最大值，"
              "不得当作资产级点值。",
              "- 站址**像元**值随分辨率变化最大（亚像元地形采样的必然结果），"
              "跨分辨率比较必须用 r200 m 口径；资产级结论仍需 5 m 局部模型（ADR-0005）。", ""]
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = sys.argv[1:]
    payload = {"generated_utc": datetime.now(timezone.utc).isoformat()}
    if "--report" not in args:
        case = args[args.index("--case") + 1]
        res = float(args[args.index("--res") + 1]) if "--res" in args else 200.0
        dt = float(args[args.index("--dt") + 1]) if "--dt" in args else (
            1.5 if case == "ningming" and res <= 60 else 5.0)
        tag = args[args.index("--tag") + 1] if "--tag" in args else "b1"
        rec = run_case(case, res, dt, tag)
        print(json.dumps(rec, ensure_ascii=False), flush=True)
        prev = json.loads(OUT_JSON.read_text(encoding="utf-8")) if OUT_JSON.exists() else {}
        prev.setdefault("cases", {}).setdefault(case, {}).setdefault("runs", {})[rec["run_dir"]] = rec
        prev["generated_utc"] = datetime.now(timezone.utc).isoformat()
        OUT_JSON.write_text(json.dumps(prev, ensure_ascii=False, indent=2), encoding="utf-8")
    payload["cases"] = report()
    write_outputs(payload)
    print(f"-> {OUT_JSON}\n-> {OUT_MD}", flush=True)


if __name__ == "__main__":
    main()
