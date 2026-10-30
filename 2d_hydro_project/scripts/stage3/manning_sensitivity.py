"""Phase B0：空间 Manning 敏感性（修正 G18）。

G18 修正事实（源码核验，2026-10-26）：
  - `manningfile` 受支持：pars.cpp:234 解析；input.cpp:914 LoadManningsn 读 6 行头 ASC；
    fp_acc.cpp:58/154、boundary.cpp:105/225/289 实际使用 Arrptr->Manningsn；
  - 项目此前从未跑过空间糙率（data/interim/*/mannings_12p5m_*.tif 生成后未用）；
  - 故“宁明面积偏大 = 空间型态失配、无空间杠杆”缺一条关键试验，本脚本补此试验。

三部分：
  1) 机制等价性冒烟（便宜且硬）：均匀 manning.asc == fpfric 时结果应逐像元相同；
     改 n 后结果应显著不同 -> 证明文件真的被使用，而不只是被解析。
  2) 宁明主试验：q6000/n0.10/100 m，均匀 fpfric=0.10 vs WorldCover 空间 n；
     指标用 ningming_ensemble.metrics（8/24 时刻切片、主阈值 0.15、剔除 8/6 常水）。
  3) 结论落盘：data/catalog/manning_sensitivity.json + docs/stage3_manning.md

用法：
  uv run python scripts/stage3/manning_sensitivity.py          # 冒烟 + 对比（不跑主模型）
  uv run python scripts/stage3/manning_sensitivity.py --run    # 额外跑主试验（约 20–30 min）
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import psutil

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))
LISFLOOD = PROJ / "models" / "lisflood-fp-8.1" / "LISFLOOD-FP-trunk" / "lisflood.exe"
SCEN = PROJ / "scripts" / "stage3" / "ningming_scenario.py"
BASE = PROJ / "outputs" / "ningming" / "2026-08-zitan"
MANNING_TIF = PROJ / "data" / "interim" / "ningming" / "mannings_12p5m_32648.tif"
SMOKE = PROJ / "outputs" / "bench" / "B0_manning_smoke"
OUT_JSON = PROJ / "data" / "catalog" / "manning_sensitivity.json"
OUT_MD = PROJ / "docs" / "stage3_manning.md"

UNIFORM_RUN = "scen_q6000_n100_100m"          # 均匀 fpfric=0.10 基准（已存在）
SPATIAL_RUN = "scen_q6000_n100_100m_nsp_b0"   # 空间 n（RUN_TAG=b0）


def write_asc(path: Path, arr: np.ndarray, cell: float) -> None:
    h, w = arr.shape
    with open(path, "w", newline="\n") as f:
        f.write(f"ncols         {w}\nnrows         {h}\n")
        f.write("xllcorner     0.000000\nyllcorner     0.000000\n")
        f.write(f"cellsize      {cell:.6f}\nNODATA_value  -9999.000000\n")
        np.savetxt(f, arr, fmt="%.6e", delimiter="\t")


def read_asc(path: Path) -> tuple[np.ndarray, dict]:
    with open(path) as f:
        hdr = {}
        for _ in range(6):
            k, *v = next(f).split()
            hdr[k.lower()] = float(v[0]) if v else None
        arr = np.loadtxt(f)
    return arr, hdr


def run_lisflood(case: Path, par: str) -> dict:
    proc = subprocess.Popen([str(LISFLOOD), par], cwd=case,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    peak = {"mb": 0.0}
    p = psutil.Process(proc.pid)

    def poll():
        while proc.poll() is None:
            try:
                peak["mb"] = max(peak["mb"], p.memory_info().rss / 1e6)
            except Exception:
                break
            time.sleep(0.05)

    threading.Thread(target=poll, daemon=True).start()
    t0 = time.monotonic()
    out, _ = proc.communicate(timeout=3600)
    wall = time.monotonic() - t0
    (case / "run.log").write_text(out, encoding="utf-8", errors="replace")
    return {"exit": proc.returncode, "wall_s": round(wall, 2), "peak_rss_mb": round(peak["mb"], 1)}


def smoke_equivalence() -> dict:
    """均匀 manning.asc == fpfric -> 结果一致；n 改小 -> 结果不同。"""
    SMOKE.mkdir(parents=True, exist_ok=True)
    n, cell = 40, 50.0
    dem = np.tile(np.linspace(10.0, 13.0, n), (n, 1))
    write_asc(SMOKE / "dem.asc", dem, cell)
    BIG = 99999999
    sx, sy = (n - 1.5) * cell, (n // 2 + 0.5) * cell
    (SMOKE / "bci.bci").write_text(
        f"P\t{sx:.1f}\t{sy:.1f}\tQVAR\tinflow\nW\t-{BIG}\t{BIG}\tFREE\t0.0005\n",
        encoding="ascii", newline="\n")
    q_file = 20.0 / cell  # B6 单位律：文件值 = Q / 格边长
    (SMOKE / "inflow.bdy").write_text(
        f"# constant inflow; file_value = Q_m3s / cellsize\n"
        f"inflow\n2 seconds\n{q_file:.6f}\t0.0\n{q_file:.6f}\t7200.0\n",
        encoding="ascii", newline="\n")
    for tag, nval in (("eq", 0.10), ("finer", 0.05)):
        write_asc(SMOKE / f"manning_{tag}.asc", np.full((n, n), nval), cell)
    out = {}
    for tag in ("nofile", "eq", "finer"):
        par = f"smoke_{tag}.par"
        txt = (f"DEMfile\tdem.asc\nbcifile\tbci.bci\nbdyfile\tinflow.bdy\n"
               f"resroot\tres\ndirroot\tresults_{tag}\nsim_time\t7200.0\n"
               f"initial_tstep\t1.0\nmassint\t600.0\nsaveint\t7200.0\nfpfric\t0.1\n")
        if tag != "nofile":
            txt += f"manningfile\tmanning_{tag}.asc\n"
        (SMOKE / par).write_text(txt + "acceleration\n", encoding="ascii", newline="\n")
        rec = run_lisflood(SMOKE, par)
        res = SMOKE / f"results_{tag}"
        wds = sorted(res.glob("res-*.wd"))
        wd, hdr = read_asc(wds[-1])
        out[tag] = {**rec, "mean_wd_m": round(float(wd.mean()), 6),
                    "vol_m3": round(float(wd.sum()) * hdr["cellsize"] ** 2, 1),
                    "max_wd_m": round(float(wd.max()), 4), "_wd": wd}
    d_eq = float(np.abs(out["eq"]["_wd"] - out["nofile"]["_wd"]).max())
    d_finer = float(np.abs(out["finer"]["_wd"] - out["nofile"]["_wd"]).max())
    for k in out:
        out[k].pop("_wd")
    verdict = {
        "uniform_manning_equals_fpfric": d_eq < 1e-9,
        "changed_n_changes_result": d_finer > 1e-3,
        "max_abs_diff_uniform_vs_fpfric_m": d_eq,
        "max_abs_diff_n005_vs_n010_m": round(d_finer, 6),
        "vol_ratio_n005_over_n010": round(out["finer"]["vol_m3"] / max(out["nofile"]["vol_m3"], 1e-9), 4),
    }
    verdict["status"] = "PASS" if (verdict["uniform_manning_equals_fpfric"]
                                   and verdict["changed_n_changes_result"]) else "FAIL"
    return {"cases": out, "checks": verdict,
            "note": "均匀 manning.asc 与 fpfric 同值时应逐像元一致；n 减半后必须出现差异；"
                    "两者同时成立才证明 manningfile 生效（不只是被解析）。"}


def _metrics(run_dir: Path) -> dict:
    from scripts.stage3.ningming_ensemble import metrics
    return metrics(run_dir)


def compare() -> dict:
    uni, spa = BASE / UNIFORM_RUN, BASE / SPATIAL_RUN
    if not (uni / "results" / "res.max").exists():
        raise SystemExit(f"缺均匀基准 run: {uni}")
    if not (spa / "results" / "res.max").exists():
        raise SystemExit(f"缺空间 n run: {spa}（先跑 --run）")
    m_uni, m_spa = _metrics(uni), _metrics(spa)
    meta_spa = json.loads((spa / "meta.json").read_text(encoding="utf-8"))
    rows = {}
    for thr in m_uni["thresholds"]:
        u, s = m_uni["thresholds"][thr], m_spa["thresholds"][thr]
        rows[thr] = {
            "uniform": u, "spatial": s,
            "delta": {k: round(s[k] - u[k], 3) for k in ("csi", "pod", "far", "area_sim_km2")},
            "area_bias_delta": round(s["area_bias"] - u["area_bias"], 3),
        }
    primary = rows["0.15"] if "0.15" in rows else list(rows.values())[1]
    return {
        "design": {
            "case": "ningming 2026-08-zitan", "q_peak": 6000.0, "res_m": 100,
            "uniform_baseline": {"run": UNIFORM_RUN, "fpfric": 0.10},
            "spatial": {"run": SPATIAL_RUN, "manning_source": str(MANNING_TIF),
                        "mapping": "WorldCover 先验中值（make_roughness.py）",
                        "grid_stats": meta_spa.get("manning_spatial")},
            "metric_semantics": "8/24 观测时刻 78h 切片；主阈值 0.15 预注册；模拟剔除 8/6 常水",
        },
        "uniform": m_uni, "spatial": m_spa, "by_threshold": rows,
        "primary_threshold_0p15": primary,
        "conclusion_key": (
            "空间 Manning 使主阈值 CSI 变化 %+0.3f、面积变化 %+.2f km²（%+0.2f×）、"
            "FAR 变化 %+0.3f" % (primary["delta"]["csi"], primary["delta"]["area_sim_km2"],
                                 primary["area_bias_delta"], primary["delta"]["far"])),
    }


def main() -> None:
    do_run = "--run" in sys.argv
    print("=== 1) manningfile 机制等价性冒烟 ===", flush=True)
    smoke = smoke_equivalence()
    print(json.dumps(smoke["checks"], ensure_ascii=False), flush=True)

    if do_run:
        print("=== 2) 宁明空间 Manning 主试验 prep+run ===", flush=True)
        env_tag = "b0"
        for cmd in ("prep", "run"):
            r = subprocess.run([sys.executable, str(SCEN), cmd, "--qpeak", "6000",
                                "--fric", "0.10", "--res", "100",
                                "--manningfile", str(MANNING_TIF)],
                               cwd=str(PROJ), env={**__import__("os").environ, "RUN_TAG": env_tag})
            if r.returncode != 0:
                raise SystemExit(f"{cmd} 失败 exit={r.returncode}")

    print("=== 3) 均匀 vs 空间 Manning 对比 ===", flush=True)
    comp = compare()
    rec = {"generated_utc": datetime.now(timezone.utc).isoformat(),
           "smoke_equivalence": smoke, "comparison": comp}
    OUT_JSON.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    write_md(rec)
    print(comp["conclusion_key"].encode("ascii", "replace").decode("ascii"), flush=True)
    print(f"-> {OUT_JSON}\n-> {OUT_MD}", flush=True)


def write_md(rec: dict) -> None:
    sm = rec["smoke_equivalence"]["checks"]
    c = rec["comparison"]
    m_uni, m_spa = c["uniform"], c["spatial"]
    gs = c["design"]["spatial"]["grid_stats"] or {}
    lines = [
        "# 宁明空间 Manning 敏感性（Phase B0，修正 G18）", "",
        f"生成：{rec['generated_utc']}　｜　情景级结论，非业务判据", "",
        "## 1. 源码核验（先纠正 G18）", "",
        "- `manningfile` **受支持**：`pars.cpp:234` 解析 → `input.cpp:914 LoadManningsn`（6 行头 ASC）"
        "→ `fp_acc.cpp:58/154`、`boundary.cpp:105/225/289` 使用 `Arrptr->Manningsn`。",
        "- 因此“本构建无空间糙率、无空间杠杆”的说法作废；正确说法是**项目从未跑过空间糙率**。",
        "- 另：`infilfile` 虽被 `LoadDistInfil` 读取，但 `FPInfiltration` 只用标量 `Parptr->InfilRate`"
        "（`infevap.cpp:28`），分布式入渗在本构建不生效（见 `docs/stage4_karst.md`）。", "",
        "## 2. 机制等价性冒烟（证明文件真的被用）", "",
        "| 检查 | 结果 |", "|---|---|",
        f"| 均匀 `manning.asc`=0.10 与 `fpfric`=0.10 结果一致 | "
        f"{'PASS' if sm['uniform_manning_equals_fpfric'] else 'FAIL'}"
        f"（最大逐像元差 {sm['max_abs_diff_uniform_vs_fpfric_m']:.2e} m） |",
        f"| n 改为 0.05 后结果改变 | "
        f"{'PASS' if sm['changed_n_changes_result'] else 'FAIL'}"
        f"（最大差 {sm['max_abs_diff_n005_vs_n010_m']} m，体积比 {sm['vol_ratio_n005_over_n010']}） |",
        f"| 冒烟总判定 | **{rec['smoke_equivalence']['checks']['status']}** |", "",
        "## 3. 主试验设计", "",
        f"- 案例 {c['design']['case']}，Q峰值 {c['design']['q_peak']:.0f} m³/s，100 m，dt 1.5 s。",
        f"- 均匀基准：`{c['design']['uniform_baseline']['run']}`，`fpfric`="
        f"{c['design']['uniform_baseline']['fpfric']}。",
        f"- 空间 n：`{c['design']['spatial']['run']}`，来自 WorldCover 先验中值网格"
        f"（重投影到 100 m 网格，最近邻，不插值）。",
        f"- 空间 n 统计：均值 {gs.get('n_mean')}，范围 [{gs.get('n_min')}, {gs.get('n_max')}]，"
        f"p10/p50/p90 = {gs.get('n_p10')}/{gs.get('n_p50')}/{gs.get('n_p90')}，"
        f"未覆盖回落 {gs.get('uncovered_cells_fallback_fpfric')} 像元。",
        f"- 指标口径：{c['design']['metric_semantics']}。", "",
        "## 4. 结果（三阈值全表）", "",
        "| 阈值 m | 配置 | CSI | POD | FAR | 模拟面积 km² | 面积偏比 |",
        "|---|---|---|---|---|---|---|",
    ]
    for thr, row in c["by_threshold"].items():
        for tag, key in (("均匀 n=0.10", "uniform"), ("空间 n", "spatial")):
            v = row[key]
            lines.append(f"| {thr} | {tag} | {v['csi']} | {v['pod']} | {v['far']} "
                         f"| {v['area_sim_km2']} | {v['area_bias']} |")
    lines += [
        "", "## 5. 站址与质量守恒", "",
        "| 项 | 均匀 | 空间 |", "|---|---|---|",
        f"| 站址像元水深@78h m | {m_uni['station_wd_center_m']} | {m_spa['station_wd_center_m']} |",
        f"| 站址 ±2 格水深@78h m | {m_uni['station_wd_at_obs_m']} | {m_spa['station_wd_at_obs_m']} |",
        f"| E_mass | {m_uni['e_mass_132']} | {m_spa['e_mass_132']} |",
        f"| 观测新增面积 km² | {m_uni['observed_new_area_km2']} | {m_spa['observed_new_area_km2']} |",
        "", "## 6. 结论（可写/不可写）", "",
        f"- 主阈值 0.15：{c['conclusion_key']}。",
        "- 可写：空间 Manning 已实测一档，给出了面积偏大对空间糙率的敏感度；"
        "“无空间杠杆”表述作废。",
        "- 不可写：不得据此宣称面积偏差主因已确定；单事件、单档、单网格，"
        "仍需 50 m 网格与授权断面/入渗资料才能定性。",
        "- 与 `gaps_risks.md` G18 同步：G18 由“无空间糙率”改为“空间糙率已试、敏感性见本文”。",
        "",
    ]
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
