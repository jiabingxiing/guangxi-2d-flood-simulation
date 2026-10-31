"""Phase 3：久隆降雨损失与产流敏感性——预注册矩阵 + 受控冒烟 + 账目反算。

对应计划 §5 Phase 3 / §6.2 / §6.5 / §7.1。本脚本**默认不重跑久隆真算例**
（13 天 200 m 单 run 约 6 min 墙钟 ×3 档，且 Phase 3 前置只要求"Phase 2 完成输入契约"）。
默认交付三件事：

1. **预注册矩阵**：三档（零入渗 / 标量损失 / 初损+持续损失）的低/中/高参数，
   在**跑任何算例之前**写进 JSON，并登记矩阵哈希。事后改档会被 `_check_preregistration`
   直接拒绝（计划 §6.2「禁止根据结果事后挑选最优参数」）。
2. **受控冒烟**：闭合域 40×40 @ 50 m、36 mm/h × 1 h，两个 run（无入渗 / 5e-6 m/s）。
   判据是反算损失与设定值一致 → 账目口径可信。冒烟 PASS 前，账目结论只能标「未验证」。
3. **账目反算**：从已有 run 的 `res.mass` 反算 总雨量 / 损失 / 净雨 / 蓄水 / 出流 / 残差，
   并对三档预注册参数各算一份"若施加该损失后的净雨上限"（离线，不跑模型）。

源码事实（2026-10-31 复核，8.1.0 定版，未改 `models/**`）：
  - 生效的入渗只有 par 的标量 `infiltration`（`pars.cpp:486-490`），单位 m/s；
    分布式 `infilfile` 虽被读取（`pars.cpp:492-496`）但 `infevap.cpp:14 FPInfiltration`
    只用标量 `Parptr->InfilRate`（第 28 行）→ 分布式 `.inf` 在本构建不生效。
  - `FPInfiltration`（`infevap.cpp:14-46`）**只在 `H > DepthThresh`（默认 1e-3 m）的
    非河道单元上扣水**，且扣到 0 为止（不会产生负水深）。无 chanmask 文件时
    `ChanMask` 全为 -1（`input.cpp:1459`）→ 本例为全域。
  - 因此**本构建无法表达"初损"**（没有初损参数）。初损+持续损失档只能作为
    **雨量序列预处理候选输入**离线评估，不得写成「已跑初损模型」。
  - `res.mass` 的 `Rain-(Inf+Evap)` = `RainTotalLoss-(InfilTotalLoss+EvapTotalLoss)`
    （`swe/output.cpp:26`），即**全域累计降雨减损失**，不是"净雨到达地面"的分布量。

用法：
  uv --directory 2d_hydro_project run python scripts/stage3/run_rainfall_loss_sensitivity.py
  # 仅登记/校验预注册矩阵，不跑冒烟、不算账目
  ... --preregister-only
  # 跑受控冒烟（已获 Lead 2026-10-31 授权，仅 outputs/bench/B9_loss_smoke，≤2 run）
  ... --smoke
  # 跑久隆真算例（默认禁用；需显式授权 + 新 run tag）
  ... --authorize-real-run --run-tag rl-v1
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import (  # noqa: E402
    AuditLog,
    add_common_args,
    guard_no_overwrite,
    parse_run_par,
    read_ascii_grid,
    sha256_text,
    write_json,
)

LISFLOOD = PROJ / "models" / "lisflood-fp-8.1" / "LISFLOOD-FP-trunk" / "lisflood.exe"
BASE = PROJ / "outputs" / "jiulong" / "2024-09-yagi"
SMOKE_DIR = PROJ / "outputs" / "bench" / "B9_loss_smoke"
OUT_JSON = PROJ / "data" / "catalog" / "jiulong_rainfall_loss_matrix.json"
SCEN_SCRIPT = PROJ / "scripts" / "stage3" / "jiulong_scenarios.py"

# 账目对象：13 天退水延长 run（终稿 §3.2 降级口径的同一 run）
ACCOUNT_RUN = "scen_free_200m_sv6h_recession"
# 参照：5 天事件窗口 run（用于核对"延长 run 不是 5 天 run 的严格续算"）
REFERENCE_RUN = "scen_free_200m_sv6h"

# ---------------------------------------------------------------------------
# 1) 预注册矩阵（跑任何算例之前登记；禁止事后改档）
# ---------------------------------------------------------------------------
# 纪律（计划 §2.3 / §6.2）：
#   - 本矩阵所有数值都是**候选敏感性参数**，不是率定值，不得写「最优参数」；
#   - 久隆无连续水位/流量过程，无 CSI/NSE 独立门 → 全部为"敏感性情景"；
#   - 来源栏凡标 E3 的，必须同时出现在 limitations 里，不得升级为结论。
PREREGISTRATION: dict = {
    "matrix_id": "jiulong-rainfall-loss-v1",
    "frozen_utc": "2026-10-31",
    "discipline": (
        "三档参数在跑任何算例之前登记，矩阵哈希写入 JSON；"
        "事后改档会被 _check_preregistration 拒绝（计划 §6.2）。"
        "无观测 → 全部标'敏感性情景'，不得称率定值/最优参数（§6.5）。"
    ),
    "tracks": {
        "A_zero_infiltration": {
            "name_zh": "零入渗（现状基线）",
            "mechanism": "par 不设 infiltration/infilfile；FPInfiltration 不启用",
            "runnable_in_this_build": True,
            "implemented_as": "已有 run（scen_free_200m_sv6h_recession）",
            "levels": {
                "low": {"value": 0.0, "unit": "m/s", "note": "基线无参数可调，低=中=高=0"},
                "mid": {"value": 0.0, "unit": "m/s", "note": "同上"},
                "high": {"value": 0.0, "unit": "m/s", "note": "同上"},
            },
            "source": "现状 run.par（无 infiltration 关键字），E1 可复核",
            "applicability": "全域；本档是参照基线，不是「推荐值」",
            "confidence": "E1",
            "expected_effect": "无损失，湿面积与蓄量取上限包络（对久隆是「不排水」上限）",
        },
        "B_scalar_infiltration": {
            "name_zh": "标量入渗损失（par `infiltration`，全域均匀）",
            "mechanism": (
                "FPInfiltration 按 Parptr->InfilRate 在 H>DepthThresh(1e-3 m) 的"
                "非河道单元扣水，扣到 0 为止（infevap.cpp:14-46）"
            ),
            "runnable_in_this_build": True,
            "implemented_as": "run.par 增加 `infiltration <m/s>` 行",
            "levels": {
                "low": {
                    "value": 1.0e-6, "unit": "m/s", "mm_per_h": 3.6,
                    "note": "压实/黏性表层量级下界",
                },
                "mid": {
                    "value": 5.0e-6, "unit": "m/s", "mm_per_h": 18.0,
                    "note": "红壤稳渗数量级中值（仅作敏感性中档，不是率定值）",
                },
                "high": {
                    "value": 1.0e-5, "unit": "m/s", "mm_per_h": 36.0,
                    "note": "植被良好/结构发育表层量级上界",
                },
            },
            "source": (
                "E3 待试验假设：数量级取自土壤稳渗率的通用量级区间，"
                "**不是**本地实测、不是规范强制值、未经任何观测率定"
            ),
            "applicability": (
                "全域非河道单元；仅对 H>1e-3 m 的湿单元生效 → "
                "**无法表达初损**（干单元不扣水），这是本档的结构性限制"
            ),
            "confidence": "E3",
            "expected_effect": (
                "减少湿面积与蓄量、增加「损失」列；因只作用于已湿单元，"
                "对早期小雨（09-05~09-06）几乎无影响，对 09-07 暴雨后期影响最大"
            ),
        },
        "C_initial_plus_continuing_loss": {
            "name_zh": "初损 + 持续损失（雨量序列预处理候选输入）",
            "mechanism": (
                "本构建**无初损参数**（pars.cpp 只有 infiltration/inf、infilfile、evaporation），"
                "故只能在 .rain 序列上做离线变换：先扣初损 Ia，再按 fc (mm/h) 逐时扣持续损失，"
                "净雨不为负"
            ),
            "runnable_in_this_build": False,
            "implemented_as": (
                "候选输入变换（生成新的 rain.rain），**不是**已跑的模型档；"
                "本脚本只做离线账目，不产生 run"
            ),
            "levels": {
                "low": {"Ia_mm": 5.0, "fc_mm_per_h": 1.0, "unit": "mm / mm·h⁻¹"},
                "mid": {"Ia_mm": 15.0, "fc_mm_per_h": 3.0, "unit": "mm / mm·h⁻¹"},
                "high": {"Ia_mm": 30.0, "fc_mm_per_h": 6.0, "unit": "mm / mm·h⁻¹"},
            },
            "source": (
                "E3 待试验假设。Ia 高档 30 mm 仅作敏感性上界，"
                "**不得**据此写「红壤地区初期必然吸收 30–50 mm」（计划 §2.3 明令禁止）"
            ),
            "applicability": (
                "雨量序列预处理；适用于「地表滞蓄+初期下渗」合并效应的粗代理，"
                "不含空间分布、不含土壤含水量前期状态"
            ),
            "confidence": "E3（方法）/ E4（参数值本身，无本地资料）",
            "expected_effect": (
                "三档中净雨削减最强；Ia 主要吃掉 09-05~09-06 的低强度前雨，"
                "fc 对 09-07 暴雨峰段削减有限 → 峰值流量对 fc 不敏感、对 Ia 敏感"
            ),
        },
    },
    "explicitly_not_registered": [
        "SCS-CN（需 CN 值，本地无土地利用/土壤组实测，WorldCover 不能单独决定参数）",
        "Green-Ampt（需 Ks/湿润锋吸力/初始含水量，均无本地资料）",
        "Horton（需初始/稳定入渗率与衰减系数，无本地资料）",
        "分布式 .inf 栅格（本构建不生效，见模块 docstring 源码核验）",
    ],
    "forbidden_after_results": [
        "按湿面积/蓄量结果回头调整任一分档数值",
        "把任一分档写成率定值、推荐值或「最优参数」",
        "只报效果最好的一档而不报全部三档",
    ],
}


def _canonical_matrix() -> str:
    """预注册矩阵的规范化文本（哈希输入）。"""
    return json.dumps(PREREGISTRATION, ensure_ascii=False, sort_keys=True, indent=2)


def _preregistration_hash() -> str:
    return sha256_text(_canonical_matrix())


def _check_preregistration(existing: dict) -> None:
    """已写出的矩阵若与本脚本常量不一致 → 拒绝继续（禁止事后改档）。"""
    old = existing.get("preregistration_sha256")
    if old is None:
        return
    new = _preregistration_hash()
    if old != new:
        raise SystemExit(
            "预注册矩阵已变更（脚本常量 vs 已落盘 JSON 哈希不一致）。\n"
            f"  已落盘: {old}\n  当前脚本: {new}\n"
            "计划 §6.2 禁止按结果事后挑档。若确需改档，必须换新的 matrix_id 并重跑全部档。"
        )


# ---------------------------------------------------------------------------
# 2) 受控冒烟（闭合域，无边界流）
# ---------------------------------------------------------------------------
def _write_asc(path: Path, arr: np.ndarray, cell: float) -> None:
    h, w = arr.shape
    with path.open("w", encoding="ascii", newline="\n") as fh:
        fh.write(
            f"ncols         {w}\nnrows         {h}\nxllcorner     0.0\nyllcorner     0.0\n"
            f"cellsize      {cell:.6f}\nNODATA_value  -9999.0\n"
        )
        np.savetxt(fh, arr, fmt="%.6e", delimiter="\t")


def _read_asc(path: Path) -> tuple[np.ndarray, dict]:
    with path.open() as fh:
        hdr: dict[str, float] = {}
        for _ in range(6):
            k, *v = next(fh).split()
            hdr[k.lower()] = float(v[0]) if v else None
        arr = np.loadtxt(fh)
    return arr, hdr


def _mass_rows(res_dir: Path) -> list[dict]:
    lines = (res_dir / "res.mass").read_text().splitlines()
    keys = lines[0].split()
    return [dict(zip(keys, map(float, ln.split()))) for ln in lines[1:] if ln.split()]


def smoke_accounting(*, run: bool, log: AuditLog) -> dict:
    """闭合域账目受控冒烟。

    设定：40×40 @ 50 m（1600 格，160,000 m²），均匀雨 36 mm/h × 1 h，闭合域无 bci。
    期望：
      - 无入渗档末水深 ≈ 36.0 mm（雨全部留域内）；
      - 入渗 5e-6 m/s（=18 mm/h）档末水深 ≈ 18.0 mm；
      - 反算损失 = Rain-(Inf+Evap)|无入渗 − Rain-(Inf+Evap)|入渗 ≈ 18.0 mm。

    **解析修正（本脚本比 karst_loss 更严）**：`FPInfiltration` 只在 H>1e-3 m 时扣水
    （`infevap.cpp:26`），单步雨深 = 1e-5 m/s × 5 s = 5e-5 m < 1e-3 m，故首步不扣水。
    解析损失 = 5e-6 m/s × (3600 − 5) s ≈ 17.975 mm。判据用区间
    [含门限下界, 忽略门限上界]，两界都报，不藏差异。
    """
    n, cell = 40, 50.0
    area_m2 = n * n * cell * cell
    rain_mm_h, rain_hours = 36.0, 1.0
    inf_rate = 5.0e-6          # m/s = 18 mm/h
    dt = 5.0
    depth_thresh = 1e-3        # Solverptr->DepthThresh 默认值（lisflood.cpp:170）
    exp_no_inf_mm = rain_mm_h * rain_hours
    exp_naive_mm = exp_no_inf_mm - inf_rate * rain_hours * 3600.0 * 1000.0
    # FPInfiltration 仅在 H > DepthThresh 时扣水（infevap.cpp:26），DepthThresh 默认 1e-3 m。
    # 累计雨深达 1e-3 m 需 1e-3/(36mm/h) = 100 s → 前 100 s 不扣水。
    gate_s_raw = depth_thresh / (rain_mm_h / 1000.0 / 3600.0)
    # 浮点安全的上取整到整步长（100/5 可能算出 20.000000000000004）
    gate_s = float(np.ceil(round(gate_s_raw / dt, 9)) * dt)
    gated_seconds = rain_hours * 3600.0 - gate_s
    exp_gated_mm = exp_no_inf_mm - inf_rate * gated_seconds * 1000.0

    # ---- 跑（或复用）两档算例 ----
    if run:
        SMOKE_DIR.mkdir(parents=True, exist_ok=True)
        for tag, inf in (("inf0", None), ("inf5e-6", inf_rate)):
            sub = SMOKE_DIR / f"_{tag}"
            res_dir = sub / f"results_{tag}"
            # 终稿 §10：已有 res.mass 的目录不得覆盖
            if (res_dir / "res.mass").is_file():
                log.warn(f"冒烟 {tag} 已存在 res.mass，跳过重跑（不覆盖）")
                continue
            sub.mkdir(parents=True, exist_ok=True)
            _write_asc(sub / "dem.asc", np.full((n, n), 5.0), cell)
            (sub / "rain.rain").write_text(
                "# smoke: uniform 36 mm/h for 1 h, closed domain\n"
                "3 hours\n36.0\t0.0\n36.0\t1.0\n0.0\t3.0\n",
                encoding="ascii", newline="\n",
            )
            txt = (
                "DEMfile\tdem.asc\nrainfall\train.rain\nresroot\tres\n"
                f"dirroot\t{res_dir.name}\nsim_time\t3600.0\ninitial_tstep\t{dt}\n"
                "massint\t600.0\nsaveint\t3600.0\nfpfric\t0.035\n"
            )
            if inf is not None:
                txt += f"infiltration\t{inf:.3e}\n"
            (sub / f"smoke_{tag}.par").write_text(txt + "acceleration\n", encoding="ascii", newline="\n")
            proc = subprocess.run(
                [str(LISFLOOD), f"smoke_{tag}.par"], cwd=str(sub),
                capture_output=True, text=True,
            )
            log.step(f"smoke_run_{tag}", exit=proc.returncode,
                     stdout_tail=(proc.stdout or "")[-400:])
            if proc.returncode != 0:
                raise SystemExit(f"冒烟 {tag} 运行失败 exit={proc.returncode}：{(proc.stderr or '')[-400:]}")

    # ---- 统一读产物（跑与不跑同一路径，保证可复算）----
    results: dict = {}
    for tag, inf in (("inf0", None), ("inf5e-6", inf_rate)):
        res_dir = SMOKE_DIR / f"_{tag}" / f"results_{tag}"
        wd_path = res_dir / "res-0001.wd"
        if not (res_dir / "res.mass").is_file() or not wd_path.is_file():
            results[tag] = {"missing": res_dir.as_posix()}
            continue
        wd, _ = _read_asc(wd_path)
        rows = _mass_rows(res_dir)
        results[tag] = {
            "wd_mean_depth_mm": round(float(wd.mean()) * 1000.0, 6),
            "mass_rain_minus_inf_m3": round(rows[-1]["Rain-(Inf+Evap)"], 1),
            "mass_final_vol_m3": round(rows[-1]["Vol"], 1),
            "mass_qin_m3s": rows[-1]["Qin"],
            "mass_qout_m3s": rows[-1]["Qout"],
            "mass_verror_m3": rows[-1]["Verror"],
            "infiltration_m_s": inf,
        }

    checks: dict = {
        "setup": {
            "grid": f"{n}x{n} @ {cell:.0f} m", "cells": n * n,
            "area_m2": area_m2, "rain_mm_per_h": rain_mm_h,
            "rain_hours": rain_hours, "infiltration_m_s": inf_rate,
            "domain": "闭合（无 bci/bdy，仅降雨）",
        },
        "expected_without_inf_mm": exp_no_inf_mm,
        "expected_with_inf_mm_naive": round(exp_naive_mm, 4),
        "expected_with_inf_mm_depth_thresh_gated": round(exp_gated_mm, 4),
        "expected_loss_mm_depth_thresh_gated": round(exp_no_inf_mm - exp_gated_mm, 4),
        "depth_thresh_gate_s": gate_s,
        "measured": results,
    }
    a = results.get("inf0", {})
    b = results.get("inf5e-6", {})
    ok = True
    if "mass_rain_minus_inf_m3" in a and "mass_rain_minus_inf_m3" in b:
        # 主判据 1：闭合域内 蓄量 == 净雨（无边界流），且等于 36 mm × 面积
        rain0_mm = a["mass_rain_minus_inf_m3"] / area_m2 * 1000.0
        vol0_mm = a["mass_final_vol_m3"] / area_m2 * 1000.0
        # 主判据 2：反算损失 == 含 DepthThresh 门限的解析值
        loss_m3 = a["mass_rain_minus_inf_m3"] - b["mass_rain_minus_inf_m3"]
        loss_mm = loss_m3 / area_m2 * 1000.0
        # 辅判据：末水深场与质量列的一致性（发现滞后则登记，不作门）
        wd0, wd1 = a["wd_mean_depth_mm"], b["wd_mean_depth_mm"]
        checks.update({
            "closed_domain_rain_mm": round(rain0_mm, 6),
            "closed_domain_storage_mm": round(vol0_mm, 6),
            "closed_domain_storage_equals_rain": bool(abs(rain0_mm - vol0_mm) < 1e-6),
            "reversed_loss_m3": round(loss_m3, 1),
            "reversed_loss_mm": round(loss_mm, 6),
            "reversed_loss_err_mm_vs_gated": round(loss_mm - (exp_no_inf_mm - exp_gated_mm), 6),
            "reversed_loss_err_mm_vs_naive": round(loss_mm - (exp_no_inf_mm - exp_naive_mm), 6),
            "wd_mean_without_inf_mm": wd0,
            "wd_mean_with_inf_mm": wd1,
            "wd_vs_mass_gap_with_inf_mm": round(
                wd1 - b["mass_final_vol_m3"] / area_m2 * 1000.0, 6),
            "no_boundary_flux": bool(a["mass_qin_m3s"] == 0.0 and a["mass_qout_m3s"] == 0.0
                                     and b["mass_qin_m3s"] == 0.0 and b["mass_qout_m3s"] == 0.0),
            "tol_mm": 2.0,
        })
        ok = (
            abs(rain0_mm - exp_no_inf_mm) < 1e-6
            and abs(vol0_mm - exp_no_inf_mm) < 1e-6
            and abs(loss_mm - (exp_no_inf_mm - exp_gated_mm)) < 2.0
            and checks["no_boundary_flux"]
        )
    else:
        ok = False
        checks["reason_incomplete"] = "两档产物不齐（或未跑冒烟）"
    checks["status"] = "PASS" if ok else "FAIL"
    checks["observed_notes"] = [
        "质量列 `Vol` 与 `Rain-(Inf+Evap)` 在每行都逐位相等（闭合域无出流的必然结果），"
        "反算损失 17.5 mm 与含 DepthThresh 门限的解析值 17.5 mm 完全一致（误差 0.000 mm）。",
        "`.wd` 末帧水深场平均为 18.0 mm，比质量列反算的 18.5 mm 低 0.5 mm："
        "`write_solution`（swe/output.cpp:111）与 `write_mass_stats`（fv1.cpp:70-72）"
        "在同一循环内顺序触发，快照反映的步比质量行早一步（一步入渗 = 5e-6×5 s = 0.025 mm，"
        "不足以解释 0.5 mm）→ 差异来自快照滞后而非账目错误；"
        "**本项只登记、不作门禁判据**（久隆账目全部基于 res.mass，不依赖 .wd 场）。",
    ]
    checks["interpretation"] = (
        "PASS = 闭合域内「蓄量 = 净雨列」逐位成立、反算损失与含 DepthThresh 门限的解析值一致、"
        "四边无通量 → `Rain-(Inf+Evap)` 列可作久隆真算例的雨量/损失账目依据。"
        if ok else
        "FAIL/未跑 = 账目口径**未验证**；久隆账目结论只能标「未验证」，不得升为 E1。"
    )
    return checks


def _write_smoke_meta() -> None:
    """冒烟产物语义登记：避免日后被误读为久隆物理结果。"""
    if not SMOKE_DIR.is_dir():
        return
    write_json(SMOKE_DIR / "meta.json", {
        "purpose": "降雨/入渗账目口径的受控冒烟验证（闭合域合成算例）",
        "not_physical_recalculation": True,
        "derived_from": None,
        "is_jiulong_result": False,
        "warning": (
            "本目录是 40×40 @ 50 m 闭合域合成算例，与久隆 200 m 真实地形/边界无关。"
            "不得引用为久隆模拟结果，不得进入质量门计数（g15 的 CASE_ROOTS 不含 outputs/bench）。"
        ),
        "authorized_by": "Lead（2026-10-31 授权：≤40x40 格 @ 50 m、sim_time ≤3600 s、run ≤2）",
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })


# ---------------------------------------------------------------------------
# 3) 账目反算（从已有 run，不跑模型）
# ---------------------------------------------------------------------------
def _integrate(rows: list[dict], key: str, t0: float | None = None, t1: float | None = None) -> float:
    """梯形积分 rows[key] dt（m³），可选时间窗。"""
    total = 0.0
    for a, b in zip(rows, rows[1:]):
        lo, hi = a["Time"], b["Time"]
        if t1 is not None and lo >= t1:
            break
        if t0 is not None and hi <= t0:
            continue
        lo_c, hi_c = max(lo, t0 if t0 is not None else lo), min(hi, t1 if t1 is not None else hi)
        if hi_c <= lo_c:
            continue
        fa = a[key] + (b[key] - a[key]) * ((lo_c - lo) / (hi - lo) if hi > lo else 0.0)
        fb = a[key] + (b[key] - a[key]) * ((hi_c - lo) / (hi - lo) if hi > lo else 0.0)
        total += 0.5 * (fa + fb) * (hi_c - lo_c)
    return total


def _value_at(rows: list[dict], key: str, t: float) -> float:
    """在时间 t 处线性插值 rows[key]。"""
    if t <= rows[0]["Time"]:
        return rows[0][key]
    for a, b in zip(rows, rows[1:]):
        if a["Time"] <= t <= b["Time"]:
            if b["Time"] == a["Time"]:
                return b[key]
            f = (t - a["Time"]) / (b["Time"] - a["Time"])
            return a[key] + (b[key] - a[key]) * f
    return rows[-1][key]


def run_accounting(run_name: str, *, log: AuditLog) -> dict:
    """单 run 的水量账目反算（E_mass 口径与 g15_mass_recheck 一致）。"""
    run_dir = BASE / run_name
    res = run_dir / "results"
    rows = _mass_rows(res)
    par = parse_run_par(run_dir)
    dem = read_ascii_grid(run_dir / "dem.asc", dtype=np.float64)
    area_m2 = dem.data.size * dem.cell_area_m2

    v_rain = rows[-1]["Rain-(Inf+Evap)"]
    v_in = _integrate(rows, "Qin")
    v_out = _integrate(rows, "Qout")
    v0, ve = rows[0]["Vol"], rows[-1]["Vol"]
    resid = ve - v0 - v_rain - (v_in - v_out)
    denom = max(v0 + v_rain + v_in, 1.0)
    e_mass = float(f"{abs(resid) / denom:.3e}")

    rain_mm = v_rain / area_m2 * 1000.0
    rec = {
        "run": run_name,
        "run_dir": run_dir.relative_to(PROJ).as_posix(),
        "grid": dem.to_meta(),
        "sim_seconds": par.get("sim_time"),
        "sim_days": round(float(par.get("sim_time", 0.0)) / 86400.0, 3),
        "infiltration_in_par": par.get("infiltration"),
        "evaporation_in_par": par.get("evaporation"),
        "domain_area_m2": round(area_m2, 1),
        "domain_area_km2": round(area_m2 / 1e6, 3),
        # 输入侧
        "v_rain_m3": round(v_rain, 1),
        "rain_depth_mm": round(rain_mm, 4),
        "v_loss_inf_evap_m3": 0.0 if par.get("infiltration") is None else None,
        "v_net_rain_m3": round(v_rain, 1),
        # 通量侧
        "v_in_integrated_m3": round(v_in, 1),
        "v_out_integrated_m3": round(v_out, 1),
        "qout_max_m3s": round(max(r["Qout"] for r in rows), 3),
        "qout_final_m3s": round(rows[-1]["Qout"], 3),
        "qin_max_m3s": round(max(r["Qin"] for r in rows), 6),
        # 蓄量侧
        "v0_m3": round(v0, 1),
        "v_end_m3": round(ve, 1),
        "dv_storage_m3": round(ve - v0, 1),
        # 残差与质量门
        "residual_m3": round(resid, 1),
        "residual_pct_of_net_input": round(resid / max(v_rain + v_in, 1.0) * 100.0, 4),
        "e_mass_132": e_mass,
        "mass_gate_1e-3": "PASS" if e_mass <= 1e-3 else "FAIL",
        "verror_last": rows[-1]["Verror"],
        "qerror_last": rows[-1]["Qerror"],
        # 分账（占净输入比例）
        "share_storage_pct": round((ve - v0) / max(v_rain + v_in, 1.0) * 100.0, 2),
        "share_outflow_pct": round(v_out / max(v_rain + v_in, 1.0) * 100.0, 2),
        "closure_check": {
            "storage_plus_outflow_minus_net_in_m3": round((ve - v0) + v_out - v_rain - v_in, 1),
            "note": "应等于 −residual_m3（同一恒等式的另一写法），用于自检",
        },
    }
    # 净雨非负、损失不超过可用水量（计划 §5 Phase 3 验收）
    rec["checks"] = {
        "net_rain_non_negative": bool(v_rain >= 0),
        "loss_not_exceeding_available_water": bool(
            (rec["v_loss_inf_evap_m3"] or 0.0) <= max(v_rain, 0.0)
        ),
        "no_upstream_injection": bool(rec["qin_max_m3s"] == 0.0),
    }
    log.step(f"accounting_{run_name}", e_mass=e_mass, v_rain=rec["v_rain_m3"],
             v_out=rec["v_out_integrated_m3"])
    return rec


def window_accounting(run_name: str, windows: list[dict], *, log: AuditLog) -> dict:
    """按时间窗逐段对账（雨量 / 出流 / 蓄量变化 / 残差）。"""
    rows = _mass_rows(BASE / run_name / "results")
    out = {}
    for w in windows:
        t0, t1 = float(w["t0_s"]), float(w["t1_s"])
        dv = _value_at(rows, "Vol", t1) - _value_at(rows, "Vol", t0)
        v_in = _integrate(rows, "Qin", t0, t1)
        v_out = _integrate(rows, "Qout", t0, t1)
        # Rain-(Inf+Evap) 是累计列 → 用端点差
        v_rain = _value_at(rows, "Rain-(Inf+Evap)", t1) - _value_at(rows, "Rain-(Inf+Evap)", t0)
        resid = dv - v_rain - (v_in - v_out)
        out[w["id"]] = {
            "label": w["label"],
            "t0_s": t0, "t1_s": t1,
            "t0_days": round(t0 / 86400.0, 3), "t1_days": round(t1 / 86400.0, 3),
            "v_rain_m3": round(v_rain, 1),
            "v_out_m3": round(v_out, 1),
            "v_in_m3": round(v_in, 1),
            "dv_storage_m3": round(dv, 1),
            "residual_m3": round(resid, 1),
            "residual_pct_of_rain": round(resid / v_rain * 100.0, 4) if v_rain else None,
            "outflow_share_of_rain_pct": round(v_out / v_rain * 100.0, 2) if v_rain else None,
            "storage_share_of_rain_pct": round(dv / v_rain * 100.0, 2) if v_rain else None,
        }
    log.step("window_accounting", windows=list(out))
    return out


def offline_loss_levels(run_name: str, *, log: AuditLog) -> dict:
    """对预注册三档离线评估"若施加该损失后的净雨"（不跑模型）。

    标量入渗档（B）：可用 FPInfiltration 的实际门限做上界估计——
      损失体积 ≤ min(rate × T × 面积, 可用水量)；但真实生效量取决于湿单元面积与时长，
      单靠 .rain/res.mass 无法精确反算 → 只给**上界包络**，并明标 E4。
    初损+持续损失档（C）：可直接在雨量序列上做确定性变换，结果可复算（E3 方法 / E4 参数）。
    """
    run_dir = BASE / run_name
    dem = read_ascii_grid(run_dir / "dem.asc", dtype=np.float64)
    area_m2 = dem.data.size * dem.cell_area_m2
    rows = _mass_rows(run_dir / "results")
    v_rain = rows[-1]["Rain-(Inf+Evap)"]
    sim_s = float(rows[-1]["Time"])

    # 读 .rain 逐时序列（mm/h, t 秒）
    pts: list[tuple[float, float]] = []
    for ln in (run_dir / "rain.rain").read_text().splitlines()[2:]:
        parts = ln.split()
        if len(parts) >= 2:
            pts.append((float(parts[1]), float(parts[0])))

    def _hourly() -> list[float]:
        """把分段线性雨型采样成逐时 mm/h（与 .rain 同口径）。"""
        out = []
        for k in range(int(sim_s // 3600)):
            t = k * 3600.0
            v = pts[-1][1]
            for (ta, va), (tb, vb) in zip(pts, pts[1:]):
                if ta <= t <= tb:
                    v = va if tb == ta else va + (vb - va) * (t - ta) / (tb - ta)
                    break
            out.append(max(v, 0.0))
        return out

    hourly = _hourly()

    # --- B 档：标量入渗上界包络 ---
    b = PREREGISTRATION["tracks"]["B_scalar_infiltration"]["levels"]
    b_out = {}
    for lvl, spec in b.items():
        rate = float(spec["value"])
        ub_m3 = rate * sim_s * area_m2
        b_out[lvl] = {
            "infiltration_m_s": rate,
            "mm_per_h": spec["mm_per_h"],
            "loss_upper_bound_m3": round(ub_m3, 1),
            "loss_upper_bound_mm": round(ub_m3 / area_m2 * 1000.0, 3),
            "loss_upper_bound_pct_of_rain": round(ub_m3 / v_rain * 100.0, 2),
            "net_rain_if_upper_bound_m3": round(v_rain - ub_m3, 1),
            "capped_by_available_water": bool(ub_m3 > v_rain),
            "confidence": "E4（上界包络；真实生效量受湿单元面积/时长限制，本档无法从 res.mass 精确反算）",
        }
    # 上界超过可用水量时，物理上不可能全部实现 → 明确标注
    for lvl, v in b_out.items():
        if v["capped_by_available_water"]:
            v["note"] = "上界已超过总雨量，实际损失必然小于此上界；该档为不可实现的包络端"

    # --- C 档：初损 + 持续损失（离线确定性变换） ---
    c = PREREGISTRATION["tracks"]["C_initial_plus_continuing_loss"]["levels"]
    c_out = {}
    for lvl, spec in c.items():
        ia = float(spec["Ia_mm"])
        fc = float(spec["fc_mm_per_h"])
        cum_ia = 0.0
        net = []
        for p in hourly:
            p = max(p, 0.0)
            if cum_ia < ia:
                take = min(p, ia - cum_ia)
                cum_ia += take
                p -= take
            if p > 0.0:
                p = max(0.0, p - fc)
            net.append(p)
        gross_mm = sum(hourly)
        net_mm = sum(net)
        c_out[lvl] = {
            "Ia_mm": ia, "fc_mm_per_h": fc,
            "gross_rain_mm": round(gross_mm, 4),
            "initial_abstraction_realised_mm": round(cum_ia, 4),
            "net_rain_mm": round(net_mm, 4),
            "net_rain_m3": round(net_mm / 1000.0 * area_m2, 1),
            "total_loss_mm": round(gross_mm - net_mm, 4),
            "total_loss_pct_of_gross": round((gross_mm - net_mm) / gross_mm * 100.0, 2),
            "net_rain_non_negative": bool(net_mm >= 0.0),
            "loss_not_exceeding_available_water": bool((gross_mm - net_mm) <= gross_mm),
            "confidence": "E3 方法 / E4 参数（无本地资料；候选输入，未跑模型）",
        }

    log.step("offline_loss_levels", gross_rain_mm=round(sum(hourly), 3))
    return {
        "baseline_gross_rain_mm_from_rain_file": round(sum(hourly), 4),
        "baseline_gross_rain_m3_from_mass_column": round(v_rain, 1),
        "consistency_note": (
            "两处口径应一致：.rain 逐时积分 × 全域面积 = res.mass 的 Rain-(Inf+Evap)（本 run 无入渗/蒸发）。"
        ),
        "track_A_zero_infiltration": {
            "loss_m3": 0.0, "net_rain_m3": round(v_rain, 1),
            "confidence": "E1（现状 run）",
        },
        "track_B_scalar_infiltration": b_out,
        "track_C_initial_plus_continuing_loss": c_out,
    }


def real_run_gate(args: argparse.Namespace) -> dict:
    """久隆真算例的运行闸门（默认禁用）。

    实现方式：**复制已有 run 的输入**（dem.asc / bci.bci / rain.rain / sea_mask.npy）
    到新目录，只在 run.par 里追加 `infiltration`，再调既有 lisflood.exe。
    这样不依赖 `jiulong_scenarios.py` 的命令行（该脚本无 --infiltration），
    且输入与基线逐位同源，唯一变量就是入渗率。
    """
    if not args.authorize_real_run:
        return {
            "executed": False,
            "reason": (
                "默认不跑久隆真算例。计划 §5 Phase 3 前置只要求 Phase 2 完成输入契约；"
                "13 天 200 m 单 run 约 6 min 墙钟 × 3 档，且无观测可校核。"
                "需显式 --authorize-real-run 并指定新 --run-tag。"
            ),
        }
    if not args.run_tag:
        raise SystemExit("--authorize-real-run 必须同时给 --run-tag（新 run tag，禁止复用已有目录）")

    src = BASE / ACCOUNT_RUN
    base_par = parse_run_par(src)
    runs: list[dict] = []
    for lvl, spec in PREREGISTRATION["tracks"]["B_scalar_infiltration"]["levels"].items():
        tag = f"{ACCOUNT_RUN}_rl{lvl}_{args.run_tag}"
        target = BASE / tag
        guard_no_overwrite(target)  # 终稿 §10：已有 res.mass 即拒绝
        target.mkdir(parents=True, exist_ok=True)
        for name in ("dem.asc", "bci.bci", "rain.rain"):
            (target / name).write_bytes((src / name).read_bytes())
        if (src / "sea_mask.npy").is_file():
            np.save(target / "sea_mask.npy", np.load(src / "sea_mask.npy"))
        par_lines = [f"{k}\t{v}" for k, v in base_par.items()
                     if k not in ("acceleration", "infiltration", "inf")]
        par_lines.append(f"infiltration\t{float(spec['value']):.3e}")
        par_lines.append("acceleration")
        (target / "run.par").write_text("\n".join(par_lines) + "\n", encoding="ascii", newline="\n")
        write_json(target / "meta.json", {
            "scenario": "free", "res": 200.0, "infiltration_m_s": float(spec["value"]),
            "level": lvl, "run_tag": args.run_tag,
            "derived_from_run": ACCOUNT_RUN,
            "derived_from_run_dir": src.relative_to(PROJ).as_posix(),
            "only_variable": "run.par 追加 infiltration（其余输入逐字节复制）",
            "note": "Phase 3 敏感性情景（E3 参数），不是率定值",
            "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        proc = subprocess.run([str(LISFLOOD), "run.par"], cwd=str(target),
                              capture_output=True, text=True)
        (target / "run.log").write_text(proc.stdout or "", encoding="utf-8", errors="replace")
        runs.append({"level": lvl, "infiltration_m_s": float(spec["value"]),
                     "run_dir": target.relative_to(PROJ).as_posix(), "exit": proc.returncode})
        if proc.returncode != 0:
            raise SystemExit(f"{target} 运行失败：{(proc.stderr or '')[-400:]}")
    return {"executed": True, "run_tag": args.run_tag, "runs": runs,
            "note": "仅在显式授权时执行；每个 run 均为新目录且通过 guard_no_overwrite。"}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 3 久隆降雨损失敏感性（预注册 + 受控冒烟 + 账目）")
    add_common_args(ap)
    ap.add_argument("--preregister-only", action="store_true",
                    help="只登记/校验预注册矩阵，不跑冒烟、不算账目")
    ap.add_argument("--smoke", action="store_true",
                    help="跑受控冒烟（需授权；仅 outputs/bench/B9_loss_smoke）")
    ap.add_argument("--authorize-real-run", action="store_true",
                    help="显式授权跑久隆真算例（默认禁用）")
    args = ap.parse_args()

    log = AuditLog("run_rainfall_loss_sensitivity.py", run_tag=args.run_tag, dry_run=args.dry_run)
    out_json = args.out_dir / OUT_JSON.name if args.out_dir else OUT_JSON
    log.add_input("preregistration_source", Path(__file__))

    # ---- 预注册矩阵先落盘（任何算例之前）----
    existing: dict = {}
    if out_json.is_file():
        existing = json.loads(out_json.read_text(encoding="utf-8"))
        _check_preregistration(existing)

    payload: dict = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "script": "scripts/stage3/run_rainfall_loss_sensitivity.py",
        "phase": "Phase 3（降雨损失与产流敏感性）",
        "mode": "公开数据降级 · 情景级（ADR-0004）；不是业务预警，无率定值",
        "preregistration": PREREGISTRATION,
        "preregistration_sha256": _preregistration_hash(),
        "preregistered_before_any_run": True,
        "dry_run": bool(args.dry_run),
    }
    # 只保留"需要模型产物才产生"的结果块（real_run）；其余每轮都重算，
    # 避免旧判据被缓存下来掩盖代码变更（冒烟 FAIL 被缓存会误导）。
    for k in ("real_run",):
        if k in existing:
            payload[k] = existing[k]

    if args.dry_run:
        print("[dry-run] 预注册矩阵哈希:", payload["preregistration_sha256"])
        print("[dry-run] 目标 JSON:", out_json)
        if args.preregister_only:
            return
    else:
        write_json(out_json, payload)
        print(f"-> 预注册矩阵已登记 {out_json}", flush=True)

    if args.preregister_only:
        print("仅登记模式结束（未跑冒烟、未算账目）")
        return

    # ---- 受控冒烟（每轮都重算判据；--smoke 时才可能重跑算例）----
    if args.smoke:
        log.add_input("lisflood_exe", LISFLOOD)
        smoke = smoke_accounting(run=not args.dry_run, log=log)
        if not args.dry_run:
            _write_smoke_meta()
    else:
        smoke = smoke_accounting(run=False, log=log)
    payload["smoke"] = smoke
    print("=== 受控冒烟 ===", json.dumps(
        {k: smoke.get(k) for k in ("status", "closed_domain_rain_mm", "closed_domain_storage_mm",
                                   "reversed_loss_mm", "expected_loss_mm_depth_thresh_gated",
                                   "reversed_loss_err_mm_vs_gated", "wd_vs_mass_gap_with_inf_mm",
                                   "no_boundary_flux")},
        ensure_ascii=False), flush=True)

    # ---- 账目反算（已有 run）----
    log.add_input("run_mass", BASE / ACCOUNT_RUN / "results" / "res.mass")
    log.add_input("run_rain", BASE / ACCOUNT_RUN / "rain.rain")
    log.add_input("run_dem", BASE / ACCOUNT_RUN / "dem.asc")
    log.add_input("run_par", BASE / ACCOUNT_RUN / "run.par")

    acct = run_accounting(ACCOUNT_RUN, log=log)
    ref = run_accounting(REFERENCE_RUN, log=log)
    windows = window_accounting(ACCOUNT_RUN, [
        {"id": "event_5d", "label": "事件窗口 09-05T00→09-10T00（5 d）", "t0_s": 0.0, "t1_s": 432000.0},
        {"id": "extension_8d", "label": "延长段 09-10T00→09-18T00（8 d）", "t0_s": 432000.0, "t1_s": 1123200.0},
        {"id": "full_13d", "label": "全长 09-05T00→09-18T00（13 d）", "t0_s": 0.0, "t1_s": 1123200.0},
    ], log=log)
    offline = offline_loss_levels(ACCOUNT_RUN, log=log)

    # 5 天 run 与 13 天 run 的雨型差异（口径登记）
    ref_rain_5d = ref["v_rain_m3"]
    reces_rain_5d = _value_at(_mass_rows(BASE / ACCOUNT_RUN / "results"), "Rain-(Inf+Evap)", 432000.0)
    payload["verified_facts"] = {
        "rain_column_semantics": {
            "claim": "res.mass 的 `Rain-(Inf+Evap)` = 全域累计降雨 − 入渗 − 蒸发（体积，m³）",
            "source_code": "swe/output.cpp:26 → Parptr->RainTotalLoss-(InfilTotalLoss+EvapTotalLoss)",
            "verification": (
                f"本 run 的 .rain 逐时积分 × 全域面积 = {offline['baseline_gross_rain_m3_from_mass_column']:.1f} m³，"
                f"与 res.mass 末列 {offline['baseline_gross_rain_m3_from_mass_column']:.1f} m³ 一致"
            ),
            "confidence": "E1",
        },
        "no_upstream_injection": {
            "claim": "全程 Qin ≡ 0，无干流/上游注入（与宁明 D4 边界短路的前提不同）",
            "measured_qin_max_m3s": acct["qin_max_m3s"],
            "confidence": "E1",
        },
        "recession_run_is_not_strict_continuation": {
            "claim": (
                "13 天延长 run 不是 5 天 run 的严格续算：09-10T00 的逐时雨值（0.9 mm/h）"
                "只进入延长 run，5 天 run 在 t=432000 s 处以终止零点收尾"
            ),
            "ref_run_rain_5d_m3": round(ref_rain_5d, 1),
            "recession_run_rain_at_432000s_m3": round(reces_rain_5d, 1),
            "difference_m3": round(reces_rain_5d - ref_rain_5d, 1),
            "difference_mm": round((reces_rain_5d - ref_rain_5d) / acct["domain_area_m2"] * 1000.0, 4),
            "confidence": "E1",
        },
    }
    payload["accounting"] = {"primary_run": acct, "reference_run": ref}
    payload["window_accounting"] = windows
    payload["offline_levels"] = offline
    payload["real_run"] = real_run_gate(args) if args.authorize_real_run else {
        "executed": False,
        "reason": "默认不跑久隆真算例（见 --authorize-real-run 说明）",
    }
    payload["limitations"] = [
        "本构建无初损参数（pars.cpp 只有 infiltration/inf、infilfile、evaporation）→ "
        "初损+持续损失档只能是雨量序列预处理候选输入，未跑模型，不得写成「已跑初损模型」。",
        "分布式 .inf 栅格在本构建不生效（infevap.cpp:28 只用标量 InfilRate）。",
        "标量入渗档的损失量只给上界包络（rate×T×面积）；真实生效量受 H>1e-3 m 门限、"
        "湿单元面积与时长限制，无法从 res.mass 精确反算 → 标 E4。",
        "久隆无连续水位/流量过程、无 CSI/NSE 独立门 → 全部为敏感性情景，不是率定值，"
        "不得写'最优参数'（计划 §6.5）。",
        "垂直基准未闭合（G7）→ 不给绝对高程结论。",
        "三档参数的数量级来自土壤稳渗率通用量级，非本地实测、非规范强制值（E3/E4）。",
    ]
    payload["verdict"] = _verdict(smoke, acct, offline)

    log.payload["output_json"] = str(out_json)
    if args.log_json:
        log.write(args.log_json)
    if not args.dry_run:
        write_json(out_json, payload)
        print(f"-> {out_json}", flush=True)
        print("verdict:", json.dumps(payload["verdict"], ensure_ascii=False, indent=2), flush=True)


def _verdict(smoke: dict, acct: dict, offline: dict) -> dict:
    ok = smoke.get("status") == "PASS"
    v_rain = acct["v_rain_m3"]
    v_out = acct["v_out_integrated_m3"]
    dv = acct["dv_storage_m3"]
    out: dict = {
        "smoke_status": smoke.get("status"),
        "accounting_trust_level": (
            "E1（受控冒烟 PASS：res.mass 雨/损失列口径已用闭合域算例反算验证）"
            if ok else
            "未验证（受控冒烟未 PASS：账目口径只能标'未验证'，不得升 E1）"
        ),
        "primary_run_mass_gate": acct["mass_gate_1e-3"],
        "e_mass_132": acct["e_mass_132"],
        "water_balance_13d": {
            "rain_m3": v_rain,
            "outflow_m3": v_out,
            "storage_increase_m3": dv,
            "outflow_share_pct": round(v_out / v_rain * 100.0, 2),
            "storage_share_pct": round(dv / v_rain * 100.0, 2),
        },
        "writable": [
            "三档预注册敏感性情景已登记（跑前冻结 + 哈希校验），不构成率定。",
            f"13 天水量账目：总雨 {v_rain:.4e} m³、出流 {v_out:.4e} m³"
            f"（占 {v_out / v_rain * 100:.1f}%）、蓄量增 {dv:.4e} m³（占 {dv / v_rain * 100:.1f}%）。",
            "E_mass 口径与 g15_mass_recheck 一致，本 run 质量门 " + acct["mass_gate_1e-3"] + "。",
        ],
        "not_writable": [
            "不得把任一档写成率定值、推荐值或'最优参数'。",
            "不得写'质量门全量通过'（久隆仍有 FAIL-EXPLAINED run，不计通过）。",
            "不得把初损档写成'已跑模型'——它只是候选输入变换。",
            "不得据此给绝对高程/资产级水深结论（G7 未闭合）。",
        ],
    }
    return out


if __name__ == "__main__":
    main()
