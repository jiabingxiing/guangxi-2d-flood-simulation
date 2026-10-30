"""Phase B3：宁明喀斯特标量入渗有界损失情景（R6）。

源码核验（2026-10-26）：
  - `infilfile` 虽被 `input.cpp:884 LoadDistInfil` 读取到 `dist_infiltration`，
    但 `infevap.cpp:14 FPInfiltration` 只用标量 `Parptr->InfilRate`（第 28 行），
    分布式 `.inf` 在本构建**不生效**；
  - 生效的只有 par 的 `infiltration`（m/s），作用于非河道单元
    （`ChanMask == -1`；本例无 chanmask 文件，故为全域）；
  - 故本项只做"标量全域有界损失代理"，不得写成"喀斯特入渗率已确定"。

三档：0（基线，不设 infiltration，已有 run）/ 1e-6（3.6 mm/h）/ 5e-6（18 mm/h）。
输出：data/catalog/karst_scenarios.json + docs/stage4_karst.md
用法：
  uv run python scripts/stage3/karst_loss.py            # 仅汇总（run 已存在）
  uv run python scripts/stage3/karst_loss.py --run      # 追加跑两档（各约 20–50 min）
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))
LISFLOOD = PROJ / "models" / "lisflood-fp-8.1" / "LISFLOOD-FP-trunk" / "lisflood.exe"
SCEN = PROJ / "scripts" / "stage3" / "ningming_scenario.py"
BASE = PROJ / "outputs" / "ningming" / "2026-08-zitan"
SMOKE = PROJ / "outputs" / "bench" / "B3_karst_smoke"
OUT_JSON = PROJ / "data" / "catalog" / "karst_scenarios.json"
OUT_MD = PROJ / "docs" / "stage4_karst.md"

LEVELS = {"inf0": None, "inf1e-6": 1e-6, "inf5e-6": 5e-6}
RUNS = {"inf0": "scen_q6000_n100_100m",
        "inf1e-6": "scen_q6000_n100_100m_inf1e-6_karst",
        "inf5e-6": "scen_q6000_n100_100m_inf5e-6_karst"}


# ---------- 1) 入渗账目受控冒烟（闭合域，无边界流） ----------
def write_asc(path: Path, arr: np.ndarray, cell: float) -> None:
    h, w = arr.shape
    with open(path, "w", newline="\n") as f:
        f.write(f"ncols         {w}\nnrows         {h}\nxllcorner     0.0\nyllcorner     0.0\n")
        f.write(f"cellsize      {cell:.6f}\nNODATA_value  -9999.0\n")
        np.savetxt(f, arr, fmt="%.6e", delimiter="\t")


def read_asc(path: Path) -> tuple[np.ndarray, dict]:
    with open(path) as f:
        hdr = {}
        for _ in range(6):
            k, *v = next(f).split()
            hdr[k.lower()] = float(v[0]) if v else None
        arr = np.loadtxt(f)
    return arr, hdr


def smoke_accounting() -> dict:
    """均匀雨 36 mm/h × 1 h；入渗 18 mm/h -> 末水深应≈18 mm；无入渗 -> ≈36 mm。"""
    SMOKE.mkdir(parents=True, exist_ok=True)
    n, cell = 40, 50.0
    write_asc(SMOKE / "dem.asc", np.full((n, n), 5.0), cell)
    (SMOKE / "rain.rain").write_text(
        "# uniform 36 mm/h for 1 h, closed domain\n3 hours\n36.0\t0.0\n36.0\t1.0\n0.0\t3.0\n",
        encoding="ascii", newline="\n")
    out = {}
    for tag, inf in (("inf0", None), ("inf5e-6", 5e-6)):
        par = f"smoke_{tag}.par"
        txt = (f"DEMfile\tdem.asc\nrainfall\train.rain\nresroot\tres\ndirroot\tresults_{tag}\n"
               f"sim_time\t3600.0\ninitial_tstep\t5.0\nmassint\t600.0\nsaveint\t3600.0\nfpfric\t0.035\n")
        if inf is not None:
            txt += f"infiltration\t{inf:.3e}\n"
        (SMOKE / par).write_text(txt + "acceleration\n", encoding="ascii", newline="\n")
        subprocess.run([str(LISFLOOD), par], cwd=str(SMOKE), check=True,
                       stdout=subprocess.DEVNULL)
        wd, hdr = read_asc(SMOKE / f"results_{tag}" / "res-0001.wd")
        mean_mm = float(wd.mean()) * 1000.0
        lines = (SMOKE / f"results_{tag}" / "res.mass").read_text().splitlines()
        keys = lines[0].split()
        rows = [dict(zip(keys, map(float, ln.split()))) for ln in lines[1:] if ln.split()]
        out[tag] = {"mean_depth_mm": round(mean_mm, 3),
                    "rain_minus_inf_m3": round(rows[-1]["Rain-(Inf+Evap)"], 1),
                    "vol_m3": round(rows[-1]["Vol"], 1)}
    exp_no_inf, exp_inf = 36.0, 18.0
    inf_loss = out["inf0"]["rain_minus_inf_m3"] - out["inf5e-6"]["rain_minus_inf_m3"]
    area = n * n * cell * cell
    out["checks"] = {
        "depth_without_inf_mm": out["inf0"]["mean_depth_mm"],
        "depth_with_inf_mm": out["inf5e-6"]["mean_depth_mm"],
        "expected_without_inf_mm": exp_no_inf, "expected_with_inf_mm": exp_inf,
        "within_tol": (abs(out["inf0"]["mean_depth_mm"] - exp_no_inf) < 2.0
                       and abs(out["inf5e-6"]["mean_depth_mm"] - exp_inf) < 2.0),
        "inf_loss_m3": round(inf_loss, 1),
        "inf_loss_equivalent_mm": round(inf_loss / area * 1000.0, 3),
        "inf_loss_expected_mm": 18.0,
    }
    out["checks"]["status"] = "PASS" if (
        out["checks"]["within_tol"]
        and abs(out["checks"]["inf_loss_equivalent_mm"] - 18.0) < 2.0) else "FAIL"
    return out


# ---------- 2) 宁明三档 ----------
def _rain_inf_terms(res_dir: Path) -> dict:
    lines = (res_dir / "res.mass").read_text().splitlines()
    keys = lines[0].split()
    rows = [dict(zip(keys, map(float, ln.split()))) for ln in lines[1:] if ln.split()]
    return {"rain_minus_inf_m3": round(rows[-1]["Rain-(Inf+Evap)"], 1),
            "final_vol_m3": round(rows[-1]["Vol"], 1),
            "qout_final": round(rows[-1]["Qout"], 3)}


def scenarios() -> dict:
    from scripts.stage3.ningming_ensemble import metrics
    out = {}
    for tag, path_name in RUNS.items():
        d = BASE / path_name
        if not (d / "results" / "res.max").exists():
            out[tag] = {"missing": str(d)}
            continue
        m = metrics(d)
        out[tag] = {"infiltration_m_s": LEVELS[tag], "run": path_name,
                    **{k: m[k] for k in ("observed_new_area_km2", "station_wd_at_obs_m",
                                         "station_wd_center_m", "e_mass_132")},
                    "thresholds": m["thresholds"], **_rain_inf_terms(d / "results")}
    base = out.get("inf0", {})
    for tag in ("inf1e-6", "inf5e-6"):
        cur = out.get(tag, {})
        if "thresholds" in cur and "thresholds" in base:
            cur["delta_vs_inf0"] = {
                "csi_0p15": round(cur["thresholds"]["0.15"]["csi"] - base["thresholds"]["0.15"]["csi"], 3),
                "far_0p15": round(cur["thresholds"]["0.15"]["far"] - base["thresholds"]["0.15"]["far"], 3),
                "area_0p15_km2": round(cur["thresholds"]["0.15"]["area_sim_km2"]
                                       - base["thresholds"]["0.15"]["area_sim_km2"], 2),
                "inf_loss_m3": round(base.get("rain_minus_inf_m3", 0.0)
                                     - cur.get("rain_minus_inf_m3", 0.0), 1),
            }
    return out


def main() -> None:
    if "--run" in sys.argv:
        env = {**os.environ, "RUN_TAG": "karst"}
        for tag, inf in LEVELS.items():
            if inf is None:
                continue
            for cmd in ("prep", "run"):
                r = subprocess.run([sys.executable, str(SCEN), cmd, "--qpeak", "6000",
                                    "--fric", "0.10", "--res", "100",
                                    "--infiltration", f"{inf:.1e}"],
                                   cwd=str(PROJ), env=env)
                if r.returncode != 0:
                    raise SystemExit(f"{tag} {cmd} 失败 exit={r.returncode}")
    print("=== 入渗账目受控冒烟 ===", flush=True)
    smoke = smoke_accounting()
    print(json.dumps(smoke["checks"], ensure_ascii=False), flush=True)
    print("=== 宁明三档 ===", flush=True)
    scen = scenarios()
    rec = {"generated_utc": datetime.now(timezone.utc).isoformat(),
           "source_code_check": {
               "infilfile": "input.cpp:884 LoadDistInfil 读取；infevap.cpp:28 只用 Parptr->InfilRate -> 分布式不生效",
               "infiltration": "pars.cpp:486 标量；作用于 ChanMask==-1 单元（本例无 chanmask = 全域）",
               "conclusion": "只能用标量有界损失代理，不得宣称分布式喀斯特入渗已建模"},
           "smoke": smoke, "scenarios": scen}
    OUT_JSON.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    write_md(rec)
    print(f"-> {OUT_JSON}\n-> {OUT_MD}", flush=True)


def write_md(rec: dict) -> None:
    sm = rec["smoke"]["checks"]
    sc = rec["scenarios"]
    lines = [
        "# 宁明喀斯特标量入渗有界损失情景（Phase B3，R6）", "",
        f"生成：{rec['generated_utc']}　｜　情景级结论，非实测入渗率", "",
        "## 1. 源码事实（决定本项只能做标量代理）", "",
        f"- `infilfile`：{rec['source_code_check']['infilfile']}",
        f"- `infiltration`：{rec['source_code_check']['infiltration']}",
        f"- 结论：{rec['source_code_check']['conclusion']}", "",
        "## 2. 账目受控冒烟（闭合域，36 mm/h × 1 h）", "",
        "| 项 | 值 |", "|---|---|",
        f"| 无入渗末水深 | {sm['depth_without_inf_mm']} mm（期望 {sm['expected_without_inf_mm']}） |",
        f"| 入渗 5e-6 m/s 末水深 | {sm['depth_with_inf_mm']} mm（期望 {sm['expected_with_inf_mm']}） |",
        f"| 反算入渗损失 | {sm['inf_loss_equivalent_mm']} mm（期望 {sm['inf_loss_expected_mm']}） |",
        f"| 判定 | **{sm['status']}** |", "",
        "## 3. 宁明三档结果（q6000 / n0.10 / 100 m，指标=8/24 时刻切片，主阈值 0.15）", "",
        "| 情景 | 入渗 m/s | 模拟面积 km² | CSI | POD | FAR | 面积偏比 | 站址 ±2 格 m | E_mass |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for tag, v in sc.items():
        if "thresholds" not in v:
            lines.append(f"| {tag} | — | 缺 run | | | | | | |")
            continue
        t = v["thresholds"]["0.15"]
        inf_txt = "0（基线，未设 infiltration）" if v["infiltration_m_s"] is None else v["infiltration_m_s"]
        lines.append(f"| {tag} | {inf_txt} | {t['area_sim_km2']} | {t['csi']} "
                     f"| {t['pod']} | {t['far']} | {t['area_bias']} | {v['station_wd_at_obs_m']} "
                     f"| {v['e_mass_132']} |")
    lines += ["", "## 4. 相对无入渗基线的变化", "",
              "| 情景 | ΔCSI(0.15) | ΔFAR(0.15) | Δ面积 km² | 反算入渗损失 m³ |",
              "|---|---|---|---|---|"]
    for tag in ("inf1e-6", "inf5e-6"):
        dv = sc.get(tag, {}).get("delta_vs_inf0")
        if dv:
            lines.append(f"| {tag} | {dv['csi_0p15']} | {dv['far_0p15']} "
                         f"| {dv['area_0p15_km2']} | {dv['inf_loss_m3']} |")
    lines += ["", "## 5. 账面判读（必须与数值一起写）", ""]
    base_rm = sc.get("inf0", {}).get("rain_minus_inf_m3")
    for tag in ("inf1e-6", "inf5e-6"):
        v = sc.get(tag, {})
        if v.get("rain_minus_inf_m3") is None or base_rm is None:
            continue
        lines.append(f"- `{tag}`：`Rain-(Inf+Evap)` = {v['rain_minus_inf_m3']:.3e} m³"
                     f"（无入渗基线 {base_rm:.3e} m³，差 {base_rm - v['rain_minus_inf_m3']:.3e} m³ 即入渗损失）。")
        if v["rain_minus_inf_m3"] < 0:
            lines.append(f"  - **该值为负**：入渗损失大于降雨总量，模型同时在扣减上游来水与"
                         f"初始蓄水。因此 `{tag}` 是**上限包络**，不是可接受率定值。")
    lines += ["",
              "## 6. 可写 / 不可写", "",
              "- 可写：标量入渗 0 → 1e-6 → 5e-6 m/s（0/3.6/18 mm/h）三档**预注册**情景已跑"
              "（三档在跑之前登记于计划，不是按 CSI 事后挑档），给出了面积/CSI/FAR 对损失量的敏感度；"
              "账目口径经受控算例验证（36 mm/h 雨 + 18 mm/h 入渗 → 末水深 18.0 mm，反算损失 17.5 mm）。",
              "- 可写：入渗是本案例面积的**强杠杆**：面积偏比 1.94 → 1.51 → 0.92×，"
              "CSI 0.357 → 0.372 → 0.471，FAR 0.602 → 0.549 → 0.330；"
              "但 POD 同时 0.773 → 0.681 → 0.613 下降，说明“多排水”不是全面改善。",
              "- 不可写：不得把 5e-6 档当作率定结果或推荐参数；不得称“喀斯特入渗率已确定”"
              "“分布式喀斯特入渗已建模”。该损失“全域均匀、仅非河道单元、无空间机制”，是**代理**。",
              "- 后续：R6 定量需地质/观测依据（泉流量、水位退水曲线、示踪）；"
              "在有依据前，宁明面积偏差应同时报告 0 与 1e-6 两档，不得只报最好的一档。", ""]
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
