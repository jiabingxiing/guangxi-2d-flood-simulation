"""阶段2 基准 B8：结构连通性定量（终稿 §11 第 6 项）。

两部分：
  B8a 合成堤防四组（本构建 8.1.0 生产可执行文件）
    open    无结构（连通基准）
    wall    DEM 加高 3 m 全封闭堤防（阻断）
    notch   堤防 + 5 行低槛开口（+0.5 m）（节制通过）
    weir    无 DEM 堤防，改用 weirfile 沿全宽布一层堰（堰顶 = 地面 + 1.5 m）
  检查：越堤水量、上下游水深、首次过水时间、流向、E_mass。
  B8b T016c 桥洞/堰官方算例（8.1.0 生产构建 vs 仓库自带 5.7.0 参考可执行文件）
    记录 6 个 stage 的深度过程与最大/末值差异；判断 8.1.0 桥涵模块是否与参考一致。

用法：uv run python scripts/stage2/structure_benchmark.py
输出：outputs/bench/B8_levee/summary.json、outputs/bench/B8_T016c*/、
      data/catalog/benchmarks.json 的 B8 键
说明：本项只证明模型“具备”结构连通性表达能力，不代表真实堤防/桥涵/站门已建模
      （真实结构资料仍缺，R1）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import psutil

PROJ = Path(__file__).resolve().parents[2]
LISFLOOD = PROJ / "models" / "lisflood-fp-8.1" / "LISFLOOD-FP-trunk" / "lisflood.exe"
TRUNK = PROJ / "models" / "lisflood-fp-8.1" / "LISFLOOD-FP-trunk"
T016C_SRC = TRUNK / "testing" / "T016c_Bridge_orifice_test"
T016C_REF_EXE = T016C_SRC / "lisflood.exe"  # 仓库自带参考可执行文件（5.7.0）
BENCH = PROJ / "outputs" / "bench"
OUT_JSON = PROJ / "data" / "catalog" / "benchmarks.json"

# --- 合成算例几何（东高西低缓坡，全部单元底面倾斜 3 m/1.2 km）---
NROW, NCOL = 60, 60
CELL = 20.0
Z_WEST, Z_EAST = 2.0, 5.0
BARRIER_COL = 30
LEVEE_H = 3.0
NOTCH_ROWS = (28, 29, 30, 31, 32)
NOTCH_H = 0.5
WEIR_CREST_ABOVE_GROUND = 1.5
Q_IN_M3S = 30.0
SIM_S = 6 * 3600.0
DT = 1.0


def write_asc(path: Path, arr: np.ndarray, cell: float = CELL, xll: float = 0.0,
              yll: float = 0.0, nodata: float = -9999.0) -> None:
    h, w = arr.shape
    with open(path, "w", newline="\n") as f:
        f.write(f"ncols         {w}\nnrows         {h}\n")
        f.write(f"xllcorner     {xll:.6f}\nyllcorner     {yll:.6f}\n")
        f.write(f"cellsize      {cell:.6f}\nNODATA_value  {nodata:.6f}\n")
        np.savetxt(f, arr, fmt="%.6e", delimiter="\t")


def read_asc(path: Path) -> tuple[np.ndarray, dict]:
    with open(path) as f:
        hdr = {}
        for _ in range(6):
            k, *v = next(f).split()
            hdr[k.lower()] = float(v[0]) if v else None
        arr = np.loadtxt(f)
    return arr, hdr


def run_lisflood(case_dir: Path, par_name: str, exe: Path = LISFLOOD,
                 timeout_s: int = 3600) -> dict:
    proc = subprocess.Popen([str(exe), par_name], cwd=case_dir,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    peak = {"rss_mb": 0.0}
    p = psutil.Process(proc.pid)

    def poll():
        while proc.poll() is None:
            try:
                peak["rss_mb"] = max(peak["rss_mb"], p.memory_info().rss / 1e6)
            except Exception:
                break
            time.sleep(0.05)

    threading.Thread(target=poll, daemon=True).start()
    t0 = time.monotonic()
    out, _ = proc.communicate(timeout=timeout_s)
    wall = time.monotonic() - t0
    (case_dir / "run.log").write_text(out, encoding="utf-8", errors="replace")
    return {"exit": proc.returncode, "wall_s": round(wall, 2), "peak_rss_mb": round(peak["rss_mb"], 1)}


def parse_mass(mass_path: Path) -> list[dict]:
    lines = mass_path.read_text().splitlines()
    keys = lines[0].split()
    return [{k: float(v) for k, v in zip(keys, ln.split())}
            for ln in lines[1:] if len(ln.split()) == len(keys)]


def e_mass(rows: list[dict]) -> float:
    v_in = sum(0.5 * (a["Qin"] + b["Qin"]) * (b["Time"] - a["Time"]) for a, b in zip(rows, rows[1:]))
    v_out = sum(0.5 * (a["Qout"] + b["Qout"]) * (b["Time"] - a["Time"]) for a, b in zip(rows, rows[1:]))
    resid = rows[-1]["Vol"] - rows[0]["Vol"] - rows[-1]["Rain-(Inf+Evap)"] - (v_in - v_out)
    return abs(resid) / max(rows[0]["Vol"] + rows[-1]["Rain-(Inf+Evap)"] + v_in, 1.0)


def par_text(**kv) -> str:
    return "".join(f"{k}\t{v}\n" for k, v in kv.items())


def _base_dem() -> np.ndarray:
    """东高西低单一坡面：col 0（西）2 m → col 59（东）5 m，行向不变。"""
    ramp = np.linspace(Z_WEST, Z_EAST, NCOL)
    return np.tile(ramp, (NROW, 1))


def _write_case_inputs(case: Path, mode: str) -> dict:
    """写 geometry/边界/par，返回该 case 的说明字段。"""
    case.mkdir(parents=True, exist_ok=True)
    dem = _base_dem()
    weir_file = None
    if mode in ("wall", "notch"):
        dem[:, BARRIER_COL] += LEVEE_H
        if mode == "notch":
            dem[NOTCH_ROWS[0]:NOTCH_ROWS[-1] + 1, BARRIER_COL] = (
                _base_dem()[NOTCH_ROWS[0]:NOTCH_ROWS[-1] + 1, BARRIER_COL] + NOTCH_H)
    write_asc(case / "dem.asc", dem)

    # 边界：东缘中心点源入流；西缘 FREE 出流（显式坡度，见 G14 修复）；南北封闭
    xll, yll = 0.0, 0.0
    tly = yll + NROW * CELL
    sx = xll + (NCOL - 1.5) * CELL    # 东缘内一列
    sy = yll + (NROW // 2 + 0.5) * CELL
    BIG = 99999999
    (case / "bci.bci").write_text(
        f"P\t{sx:.1f}\t{sy:.1f}\tQVAR\tinflow\n"
        f"W\t-{BIG}\t{BIG}\tFREE\t0.0005\n",
        encoding="ascii", newline="\n")
    # 点源单位律（B6 实测）：实际 m³/s = 文件值 × 网格边长
    q_file = Q_IN_M3S / CELL
    (case / "inflow.bdy").write_text(
        f"# constant inflow scenario; file_value = Q_m3s / cellsize\n"
        f"inflow\n2 seconds\n{q_file:.6f}\t0.0\n{q_file:.6f}\t{SIM_S:.0f}\n",
        encoding="ascii", newline="\n")

    if mode == "weir":
        # 沿全宽（每行一条）在 col BARRIER_COL-1|BARRIER_COL 面布堰：
        # tag E + x 取 col BARRIER_COL 中心 -> Weir_Identx[xi] 对应面 (xi-1, xi)
        # 见 input.cpp:183 与 weir_flow.cpp:61
        lines = [f"{NROW}"]
        for r in range(NROW):
            crest = _base_dem()[r, BARRIER_COL] + WEIR_CREST_ABOVE_GROUND
            wc_x = xll + (BARRIER_COL + 0.5) * CELL
            wc_y = tly - (r + 0.5) * CELL
            # x y tag Cd hc m width
            lines.append(f"{wc_x:.1f} {wc_y:.1f} E 0.60 {crest:.3f} 1.50 {CELL:.1f}")
        weir_file = "levee.weir"
        (case / weir_file).write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")

    par = dict(DEMfile="dem.asc", bcifile="bci.bci", bdyfile="inflow.bdy",
               resroot="res", dirroot="results", sim_time=f"{SIM_S:.0f}",
               initial_tstep=f"{DT}", massint="300.0", saveint="3600.0",
               fpfric="0.035")
    if weir_file:
        par["weirfile"] = weir_file
    (case / "run.par").write_text(par_text(**par) + "acceleration\n",
                                  encoding="ascii", newline="\n")
    return {"mode": mode, "weir_file": weir_file,
            "q_in_m3s_target": Q_IN_M3S, "q_file_value": round(q_file, 4),
            "barrier_col": BARRIER_COL, "levee_h_m": LEVEE_H if mode != "open" else 0.0,
            "notch_h_m": NOTCH_H if mode == "notch" else None,
            "notch_rows": list(NOTCH_ROWS) if mode == "notch" else None,
            "weir_crest_above_ground_m": WEIR_CREST_ABOVE_GROUND if mode == "weir" else None}


def _analyse(case: Path) -> dict:
    res = case / "results"
    rows = parse_mass(res / "res.mass")
    wds = sorted(res.glob("res-*.wd"))
    cell = CELL
    area = cell * cell
    first_wet_west = None
    series = []
    for f in wds:
        t_h = int(f.stem.split("-")[1])  # saveint=3600 -> 帧号=小时
        wd, _ = read_asc(f)
        west = wd[:, :BARRIER_COL]
        east = wd[:, BARRIER_COL + 1:]
        v_west = float(np.clip(west, 0, None).sum()) * area
        series.append({"t_h": t_h, "vol_west_m3": round(v_west, 1),
                       "max_wd_west_m": round(float(west.max()), 3),
                       "max_wd_east_m": round(float(east.max()), 3),
                       "wet_cells_west": int((west > 0.05).sum())})
        if first_wet_west is None and (west > 0.05).any():
            first_wet_west = t_h
    wd_end = series[-1] if series else {}
    qin = rows[0]["Qin"] if rows else None
    v_in = sum(0.5 * (a["Qin"] + b["Qin"]) * (b["Time"] - a["Time"]) for a, b in zip(rows, rows[1:])) if rows else 0.0
    v_out = sum(0.5 * (a["Qout"] + b["Qout"]) * (b["Time"] - a["Time"]) for a, b in zip(rows, rows[1:])) if rows else 0.0
    return {
        "frames": len(wds),
        "qin_m3s_effective": round(qin, 3) if qin is not None else None,
        "qin_scale_vs_file_value": round(qin / (Q_IN_M3S / CELL), 3) if qin else None,
        "v_in_integrated_m3": round(v_in, 1),
        "v_out_integrated_m3": round(v_out, 1),  # 西缘累计出流：越堤连通性的直接指标
        "vol_west_end_m3": wd_end.get("vol_west_m3"),
        "max_wd_west_m": max((s["max_wd_west_m"] for s in series), default=None),
        "max_wd_east_m": max((s["max_wd_east_m"] for s in series), default=None),
        "first_wet_west_h": first_wet_west,
        "e_mass": float(f"{e_mass(rows):.3e}") if rows else None,
        "qout_final": round(rows[-1]["Qout"], 3) if rows else None,
        "series": series,
    }


def b8a_levee(base: Path) -> dict:
    out = {"case": "B8a 合成堤防连通性（60×60@20 m，东高西低，Q=30 m³/s 恒定 6 h）",
           "runs": {}}
    for mode in ("open", "wall", "notch", "weir"):
        case = base / "B8_levee" / mode
        if case.exists():
            shutil.rmtree(case)
        info = _write_case_inputs(case, mode)
        run = run_lisflood(case, "run.par")
        rec = {**info, **run, **_analyse(case)}
        out["runs"][mode] = rec
        print(f"  {mode}: exit={run['exit']} wall={run['wall_s']}s "
              f"V_west={rec['vol_west_end_m3']} E_mass={rec['e_mass']}", flush=True)
    r = out["runs"]
    v_open = r["open"]["vol_west_end_m3"] or 0.0
    v_wall = r["wall"]["vol_west_end_m3"] or 0.0
    v_notch = r["notch"]["vol_west_end_m3"] or 0.0
    v_weir = r["weir"]["vol_west_end_m3"] or 0.0
    o_open = r["open"]["v_out_integrated_m3"] or 0.0
    out["ratios"] = {
        "wall_over_open": round(v_wall / max(v_open, 1e-9), 4),
        "notch_over_open": round(v_notch / max(v_open, 1e-9), 4),
        "weir_over_open": round(v_weir / max(v_open, 1e-9), 4),
        "wall_out_over_open_out": round((r["wall"]["v_out_integrated_m3"] or 0.0) / max(o_open, 1e-9), 4),
        "notch_out_over_open_out": round((r["notch"]["v_out_integrated_m3"] or 0.0) / max(o_open, 1e-9), 4),
        "weir_out_over_open_out": round((r["weir"]["v_out_integrated_m3"] or 0.0) / max(o_open, 1e-9), 4),
    }
    checks = {
        "wall_blocks": out["ratios"]["wall_over_open"] < 0.01,
        "notch_passes": out["ratios"]["notch_over_open"] > 0.01,
        "notch_less_than_open": out["ratios"]["notch_over_open"] < 1.0,
        "weir_passes": out["ratios"]["weir_over_open"] > 0.01,
        "weir_less_than_open": out["ratios"]["weir_over_open"] < 1.0,
        "wall_no_wet_west": (r["wall"]["first_wet_west_h"] is None),
        "wall_no_outflow": out["ratios"]["wall_out_over_open_out"] < 0.01,
        "e_mass_ok": all(r[m]["e_mass"] is not None and r[m]["e_mass"] < 1e-3
                         for m in ("open", "wall", "notch", "weir")),
    }
    out["checks"] = checks
    all_ok = all(checks.values())
    out["status"] = "PASS" if all_ok else "FAIL"
    out["note"] = ("结构连通性可由 weirfile 与 DEM 加高两种方式表达；通过率=全部检查项。"
                   "不覆盖真实堤防/桥涵/站门资料（R1）。")
    return out


def _stage_series(stage_path: Path) -> tuple[list[str], np.ndarray]:
    """解析 res.stage：'Time; stages 1 to 6' 之后的数值块，列数=1+stage 数。"""
    lines = stage_path.read_text(errors="replace").splitlines()
    idx = next(i for i, ln in enumerate(lines) if ln.strip().startswith("Time;"))
    nstage = int(lines[idx].split("stages 1 to")[1].split()[0]) if "stages 1 to" in lines[idx] else None
    data = []
    for ln in lines[idx + 1:]:
        vals = ln.split()
        if not vals:
            continue
        try:
            nums = [float(v) for v in vals]
        except ValueError:
            continue
        if nstage is not None and len(nums) != nstage + 1:
            continue
        data.append(nums)
    arr = np.array(data) if data else np.zeros((0, (nstage or 0) + 1))
    names = [f"stage{i}" for i in range(1, arr.shape[1])]
    return names, arr


def b8b_t016c(base: Path) -> dict:
    """T016c 桥洞算例：本构建 8.1.0 vs 仓库参考 5.7.0。"""
    out = {"case": "B8b T016c 桥洞/堰官方算例（SGC 通道 + bridge weir）",
           "builds": {}, "reference_note": ""}
    cases = {"chan": "MT_SGC_chan.par", "orifice": "MT_SGC_orifice.par"}
    inputs = ["DEM_MT.asc", "width.asc", "inflow.bci", "inflow_SGC2.bdy.txt",
              "locations.stage", "bridge.weir"] + list(cases.values())
    for tag, exe in (("v81_production", LISFLOOD), ("v57_reference", T016C_REF_EXE)):
        case_dir = base / ("B8_T016c" if tag == "v81_production" else "B8_T016c_ref")
        case_dir.mkdir(parents=True, exist_ok=True)
        for f in inputs:
            shutil.copy2(T016C_SRC / f, case_dir / f)
        if tag == "v57_reference":
            shutil.copy2(exe, case_dir / "lisflood_ref.exe")
            exe = case_dir / "lisflood_ref.exe"
        rec = {}
        for key, par in cases.items():
            run = run_lisflood(case_dir, par, exe=exe)
            stage_path = case_dir / ("results_SGC_chan" if key == "chan" else "results_SGC_Orifice") / "res.stage"
            names, arr = _stage_series(stage_path)
            rec[key] = {
                **run,
                "stage_count": len(names),
                "stage_max_m": [round(float(x), 3) for x in arr[:, 1:].max(axis=0)],
                "stage_final_m": [round(float(x), 3) for x in arr[-1, 1:]],
                "t_end_s": round(float(arr[-1, 0]), 1),
            }
        out["builds"][tag] = rec
        print(f"  T016c {tag}: chan exit={rec['chan']['exit']} orifice exit={rec['orifice']['exit']}",
              flush=True)
    v81 = out["builds"]["v81_production"]
    v57 = out["builds"]["v57_reference"]
    d_chan = [round(a - b, 3) for a, b in zip(v81["chan"]["stage_max_m"], v57["chan"]["stage_max_m"])]
    d_orif = [round(a - b, 3) for a, b in zip(v81["orifice"]["stage_max_m"], v57["orifice"]["stage_max_m"])]
    out["stage_max_diff_m_81_minus_57"] = {"chan": d_chan, "orifice": d_orif}
    out["max_abs_diff_m"] = max(max(abs(x) for x in d_chan), max(abs(x) for x in d_orif))
    # 连通性判据：桥洞组下游侧（stage1..3，x=99750/74750/50250）是否过水
    wet57 = [x > 0.05 for x in v57["orifice"]["stage_max_m"][:3]]
    wet81 = [x > 0.05 for x in v81["orifice"]["stage_max_m"][:3]]
    out["orifice_downstream_wet"] = {"v81": wet81, "v57": wet57}
    reproducible = out["max_abs_diff_m"] < 0.01
    out["status"] = "PASS" if (reproducible and wet81 == wet57) else "DIFFERS_FROM_REFERENCE"
    out["reference_note"] = (
        "仓库自带 T016c 参考结果由 bundled LISFLOOD-FP 5.7.0 生成；本构建 8.1.0 复跑结果与参考不一致"
        if not reproducible else "本构建复现仓库参考结果")
    out["gate"] = ("桥涵能力仅在有参考可执行文件（5.7.0）时通过；生产构建 8.1.0 的 SGC+bridge "
                   "路径未获独立验证，且与参考存在差异 -> 结构能力不宣称已验证")
    return out


def main() -> None:
    base = BENCH
    base.mkdir(parents=True, exist_ok=True)
    print("=== B8a 合成堤防 ===", flush=True)
    b8a = b8a_levee(base)
    print("=== B8b T016c ===", flush=True)
    b8b = b8b_t016c(base)
    summary = {"generated_utc": datetime.now(timezone.utc).isoformat(),
               "lisflood_build": "8.1.0 MinGW GCC16.2 -O3 -fopenmp -static",
               "reference_build": "5.7.0（仓库自带 testing/T016c 可执行文件）",
               "B8a": b8a, "B8b": b8b}
    (base / "B8_levee" / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    prev = {}
    if OUT_JSON.exists():
        try:
            prev = json.loads(OUT_JSON.read_text(encoding="utf-8"))
        except Exception:
            prev = {}
    prev["generated_utc"] = datetime.now(timezone.utc).isoformat()
    prev["B8"] = {
        "case": "B8 结构连通性定量（合成堤防四组 + T016c 桥洞对照）",
        "status": "PASS_PARTIAL" if (b8a["status"] == "PASS" and b8b["status"] != "PASS") else
                  ("FAIL" if b8a["status"] != "PASS" else "PASS"),
        "B8a_status": b8a["status"], "B8b_status": b8b["status"],
        "B8a_ratios": b8a["ratios"], "B8a_checks": b8a["checks"],
        "B8b_max_abs_diff_m": b8b["max_abs_diff_m"],
        "B8b_orifice_downstream_wet": b8b["orifice_downstream_wet"],
        "evidence": ["outputs/bench/B8_levee/summary.json", "outputs/bench/B8_T016c/",
                     "outputs/bench/B8_T016c_ref/"],
        "gate": "合成堤防证明 2D 阻断/开口/堰三类连通性可表达；T016c 提示生产构建与参考构建差异，"
                "真实结构（堤防/桥涵/站门）仍未建模（R1）",
        "note": b8a["note"],
    }
    OUT_JSON.write_text(json.dumps(prev, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"B8a": b8a["status"], "B8a_ratios": b8a["ratios"],
                      "B8b": b8b["status"], "B8b_max_abs_diff_m": b8b["max_abs_diff_m"]},
                     ensure_ascii=False), flush=True)
    print(f"-> {OUT_JSON}")


if __name__ == "__main__":
    main()
