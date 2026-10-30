"""宁明换维度收敛试验（任务 1，2026-10-25）：水量维度。

背景：敏感性 9 组的 FAR 地板 ~0.59 不随 Q/糙率/阈值动（结构性过量）。
本构建 LISFLOOD-FP 8.1 无糙率网格文件（仅均匀 fpfric）、雨文件仅全域均匀序列
——空间化两条路不可实现（登记）。落地为水量维度：
  雨量折减 ×{0.85, 0.7, 0.5} @ q6000/n0.10（敏感性角点最优）
  = ERA5 均匀雨高估 + 喀斯特地下排泄（R6）的联合等效代理。
目标：检验 FAR 地板是否水量驱动；若 CSI 显著升 → 定位驱动因子并量化所需等效损失率。
口径：与敏感性集一致（新增淹没：模拟剔除 8/6 常水后比对 8/24 新增淹没）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ningming_ensemble import THRS, metrics  # noqa: E402  同目录复用（新增淹没口径）

SCEN = PROJ / "scripts" / "stage3" / "ningming_scenario.py"
BASE = PROJ / "outputs" / "ningming" / "2026-08-zitan"
Q, N = 6000.0, 0.10
SCALES = (0.85, 0.7, 0.5)


def main() -> None:
    rows = []
    for sc in SCALES:
        rid = f"scen_q{int(Q)}_n{int(N * 1000)}_100m_r{int(sc * 100)}"
        run_dir = BASE / rid
        if not (run_dir / "results" / "res.max").exists():
            for cmd in ("prep", "run"):
                r = subprocess.run(
                    [sys.executable, str(SCEN), cmd, "--qpeak", str(Q),
                     "--fric", str(N), "--res", "100", "--rain-scale", str(sc)],
                    cwd=str(PROJ))
                if r.returncode != 0:
                    print(f"[失败] {rid} {cmd}", flush=True)
                    break
        if (run_dir / "results" / "res.max").exists():
            m = metrics(run_dir)
            rec = {"run_id": rid, "q_peak": Q, "fpfric": N, "rain_scale": sc, **m}
            rows.append(rec)
            (run_dir / "convergence_verify.json").write_text(
                json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(rec, ensure_ascii=False), flush=True)
    # 参照行：角点无折减（敏感性集已有）
    ref = BASE / "scen_q6000_n100_100m"
    if (ref / "results" / "res.max").exists():
        rows.append({"run_id": "scen_q6000_n100_100m(参照 r100)", "q_peak": Q,
                     "fpfric": N, "rain_scale": 1.0, **metrics(ref)})
    out = {"generated_utc": datetime.now(timezone.utc).isoformat(),
           "design": {"q_peak": Q, "fpfric": N, "rain_scales": SCALES, "thresholds_m": THRS,
                      "semantics": "新增淹没口径（模拟剔除 8/6 常水）",
                      "rationale": "水量维度=ERA5均匀雨高估+喀斯特地下排泄(R6)联合代理；"
                                   "空间化(糙率网格/分布雨)在本 LISFLOOD 8.1 构建不可实现(pars.cpp 仅均匀 fpfric)"},
           "runs": rows}
    (PROJ / "data" / "catalog" / "ningming_convergence.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 宁明换维度收敛试验（水量维度 @ q6000/n0.10）", "",
             "指标口径：观测时刻 2026-08-24 最近 6h 帧（78h）；主阈值 0.15 m；模拟剔除 8/6 常水。",
             "",
             "| run | 雨量折减 | 阈值 | CSI | POD | FAR | 模拟面积 | 面积偏比 | 站±2格水深@78h | E_mass |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        for thr, t in r["thresholds"].items():
            lines.append(f"| {r['run_id']} | ×{r['rain_scale']} | {thr} | {t['csi']} | {t['pod']} "
                         f"| {t['far']} | {t['area_sim_km2']} | {t['area_bias']} "
                         f"| {r['station_wd_at_obs_m']} | {r['e_mass_132']} |")
    lines += ["", "观测 49.5 km²（8/24 新增淹没）；口径=时刻切片、模拟剔除 8/6 常水。",
              "判读目标：FAR 是否随折减显著下降（水量驱动）及所需等效损失率。",
              "",
              "判读（2026-10-26）：雨量 ×0.5 时 thr0.15 的 CSI 0.361、FAR 0.582、面积 85.98 km²；"
              "相对 ×1.0 的 0.357/0.602/96.02，面积仅 −10%，FAR 仅 −0.02。"
              "水量折减不是 FAR 地板的主要驱动；但空间 Manning 未试（G18 修正），"
              "不能把“空间型态失配、无空间杠杆”当作已证结论。"]
    (PROJ / "docs" / "stage3_ningming_convergence.md").write_text(
        "\n".join(lines), encoding="utf-8")
    print("-> data/catalog/ningming_convergence.json + docs/stage3_ningming_convergence.md", flush=True)


if __name__ == "__main__":
    main()
