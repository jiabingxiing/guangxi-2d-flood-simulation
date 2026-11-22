"""阶段6：贵港情景 Sentinel-1 2024-09-12 洪峰景验证（只读）。

用途
----
对基线 `scen_yagi_q11500_100m_dt2p5_choff_realdem`、三个 Phase 2+4 新情景
（`flow_only_v1` / `dualloss_v1` / `rainloss_high_v1`）与五个 ADR-0012 水力约束
河槽重构情景（`chhyd_d10m` ~ `chhyd_d19m`）分别计算 S1 重叠区 POD / FAR / CSI
与足迹内模拟面积，并判定「FAR 压降 > 20%、CSI 有效提升」的方向性。
缺失 run 自动记 `available=false`，不阻塞其余情景。

口径来源（不得重写）
--------------------
全部比对逻辑逐行复用 `scripts/stage5/guigang_scenario.py::verify()` 的
S1 段（该函数第 511–539 行）：

  - 时刻：`res-0038.wd`（t_model_h = 228.0，即 2024-09-12T12 模型时刻，
    对应 10:42 卫星过境）；
  - 湿格判据：`wd > 0.05`（float32 口径）；
  - S1 值域：0=非水 / 1=水 / 255=nodata；观测水 = `(obs == 1) & (cover == 1)`；
  - 足迹 `valid = (cover == 1)`；**足迹外无观测，不计假警报**；
  - 重投影：`rasterio.warp.reproject` + `Resampling.nearest` 到模型 100 m 网格
    （`tgt` 由 `res.max` 的 6 行头构造，与 verify() 完全一致）；
  - 面积一律 `count * cellsize**2 / 1e6`，cellsize 从栅格头读取，不写死。

纪律
----
- 只读 `outputs/guigang/**` 与 `data/processed/s1_water/**`；不写任何 run 目录。
- 不调用 lisflood.exe，不改 `models/`。
- 本脚本只做诊断，不是闸门：S1 固定阈值 -17 dB，风浪/淹没植被偏保守（G17 降级），
  且足迹仅覆盖模型域约 20.6%。任何 CSI/POD/FAR 都不得写成模型精度结论。

用法
----
    $env:PYTHONUTF8 = "1"
    uv --directory 2d_hydro_project run python scripts/stage6/validate_guigang_s1.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))
from scripts.common.grid_io import (  # noqa: E402
    read_ascii_header,
    sha256_file,
    utc_now,
    write_json,
)

RUNS_ROOT = PROJ / "outputs" / "guigang" / "2024-09-yagi"
S1W_FLD = PROJ / "data" / "processed" / "s1_water" / "guigang_20240912T104222_water20m.tif"
OUT_JSON = PROJ / "data" / "catalog" / "guigang_s1_validation.json"

# 与 verify() 完全一致的常量
T_MODEL_H = 228.0
SCENE = "2024-09-12T10:42"
WET_THRESHOLD_M = 0.05
FRAME_FILE = "res-0038.wd"

# 基线（verify.json 实测值）—— 仅作核对，不作为结果来源
BASELINE_REFERENCE = {
    "csi": 0.076,
    "pod": 0.652,
    "far": 0.921,
    "area_sim_km2": 630.14,
    "area_sim_in_footprint_km2": 92.63,
    "area_obs_km2_in_footprint": 11.29,
    "valid_footprint_km2": 510.95,
}

SCENARIOS = (
    ("baseline_realdem", "scen_yagi_q11500_100m_dt2p5_choff_realdem", "on（毛雨 135.50 mm，无损失）"),
    ("flow_only_v1", "scen_yagi_q11500_100m_dt2p5_choff_flow_only_v1", "off（无降雨输入）"),
    ("dualloss_v1", "scen_yagi_q11500_100m_dt2p5_choff_dualloss_v1",
     "loss_medium（Ia=30 mm, fc=2.0 mm/h → 净雨 45.40 mm）"),
    ("rainloss_high_v1", "scen_yagi_q11500_100m_dt2p5_choff_rainloss_high_v1",
     "loss_high（Ia=50 mm, fc=3.0 mm/h → 净雨 36.10 mm）"),
    # ADR-0012 水力约束河槽重构（rain=on 与基线同源；唯一变量 = 河槽几何 + manningfile 主槽 n）
    ("chhyd_d10", "scen_yagi_q11500_100m_dt2p5_chhyd_d10",
     "on（同基线雨）+ hydraulic D=10 m（浅槽，α≈0.38）"),
    ("chhyd_d12", "scen_yagi_q11500_100m_dt2p5_chhyd_d12",
     "on（同基线雨）+ hydraulic D=12 m（中浅槽，α≈0.51）"),
    ("chhyd_d14", "scen_yagi_q11500_100m_dt2p5_chhyd_d14",
     "on（同基线雨）+ hydraulic D=14 m（基准候选，α≈0.64）"),
    ("chhyd_d16", "scen_yagi_q11500_100m_dt2p5_chhyd_d16",
     "on（同基线雨）+ hydraulic D=16 m（深槽，α≈0.78）"),
    ("chhyd_d19", "scen_yagi_q11500_100m_dt2p5_chhyd_d19",
     "on（同基线雨）+ hydraulic D=19 m（主槽极限输水，α≈0.99）"),
    # ADR-0013 组合实验（河槽 × 净雨双变量，归因靠 2×2 因子差分）
    ("chhyd_d14_r2_lossmed", "scen_yagi_q11500_100m_dt2p5_chhyd_d14_r2_lossmed",
     "loss_medium（45.40 mm）+ hydraulic d14（补全 2×2）"),
    ("chhyd_d14_r2_ga", "scen_yagi_q11500_100m_dt2p5_chhyd_d14_r2_ga",
     "ga（GA-Mid 物理净雨 38.87 mm 模型窗）+ hydraulic d14（主方案）"),
    ("chhyd_d10_r2_norain", "scen_yagi_q11500_100m_dt2p5_chhyd_d10_r2_norain",
     "off（无降雨）+ hydraulic d10（补全无雨 2×2）"),
    # ADR-0014 排水连通治理（诊断探针：DEM 推导沟 / 求解器入渗，均为情景假设）
    ("chhyd_d14_r3_infil2", "scen_yagi_q11500_100m_dt2p5_chhyd_d14_r3_infil2",
     "on（毛雨）+ hydraulic d14 + 求解器入渗 2.0 mm/h（垂直排水）"),
    ("chhyd_d14_r3_drA", "scen_yagi_q11500_100m_dt2p5_chhyd_d14_r3_drA",
     "on（毛雨）+ hydraulic d14_drA（DEM 推导沟 A 档，水平排水）"),
    ("chhyd_d14_r3_combo", "scen_yagi_q11500_100m_dt2p5_chhyd_d14_r3_combo",
     "on（毛雨）+ hydraulic d14_drA + 求解器入渗 2.0 mm/h（叠加）"),
    # ADR-0014 第四轮（天花板与密度敏感性，延续诊断探针定位）
    ("chhyd_d14_r4_infil3", "scen_yagi_q11500_100m_dt2p5_chhyd_d14_r4_infil3",
     "on（毛雨）+ hydraulic d14 + 求解器入渗 3.0 mm/h（垂直天花板）"),
    ("chhyd_d14_r4_drB", "scen_yagi_q11500_100m_dt2p5_chhyd_d14_r4_drB",
     "on（毛雨）+ hydraulic d14_drB（DEM 推导沟 B 档 27 条/402 km）"),
    ("chhyd_d14_r4_drB_infil3", "scen_yagi_q11500_100m_dt2p5_chhyd_d14_r4_drB_infil3",
     "on（毛雨）+ hydraulic d14_drB + 求解器入渗 3.0 mm/h（双轨天花板）"),
)

# ---- 站点水位诊断口径（逐项复用 verify()，见 guigang_scenario.py 第 486-509、555-576 行）----
ANCHOR_LONLAT = (109.60, 23.11)  # 贵港市区郁江段（guigang_scenario.ANCHOR）
ANCHOR_LABEL = "贵港市区郁江段"
ANCHOR_HALF_WINDOW = 3           # verify() 用 row±3 / col±3（7×7 窗口）
GAUGE_0912_M = 43.09             # 贵港站 09-12T14 洪峰水位（站基面）
GAUGE_0914_M = 44.00             # 贵港站 09-14T05 水位
T_GAUGE_0912_H = 228.0           # 对应 res-0038.wd（模型 09-12T12），非 09-12T14
T_GAUGE_0914_H = 270.0

# 计划 §4 验收项（原文：「FAR 压降 > 20%」「CSI 有效提升」）
PLAN_FAR_REDUCTION_TARGET_PCT = 20.0


def _read_asc(p: Path) -> np.ndarray:
    return np.loadtxt(p, skiprows=6, dtype=np.float32, ndmin=2)


def _wse_context(run_dir: Path, grid_shape: tuple[int, int], cell: float,
                 xll: float, yll: float, key_seqs: tuple[int, ...] = (38, 45)) -> dict:
    """复用 verify() 的锚点 WSE 诊断口径（7×7 窗口内湿格的最大水面高程）。

    数据来源分两层，避免重算 228 个 ASCII 帧：
      1. `verify.json::wse_gauge_context` —— 由 `verify()` 本身产出，是本文件的权威值；
      2. 本函数对关键帧（默认 seq 38 = t228h、seq 45 = t270h）独立复算，
         用于证明 `verify.json` 里的口径可被复现（窗口、阈值、wse 定义均照抄 verify()）。
    """
    from pyproj import Transformer

    tf = Transformer.from_crs(4326, 32649, always_xy=True)
    ax, ay = tf.transform(ANCHOR_LONLAT[0], ANCHOR_LONLAT[1])
    col = int((ax - xll) / cell)
    row = int((yll + grid_shape[0] * cell - ay) / cell)
    dem = _read_asc(run_dir / "dem.asc")
    nrows, ncols = grid_shape
    r0, r1 = max(0, row - ANCHOR_HALF_WINDOW), min(nrows, row + ANCHOR_HALF_WINDOW + 1)
    c0, c1 = max(0, col - ANCHOR_HALF_WINDOW), min(ncols, col + ANCHOR_HALF_WINDOW + 1)
    dem_sub = dem[r0:r1, c0:c1]

    recomputed = {}
    for seq in key_seqs:
        wd_p = run_dir / "results" / f"res-{seq:04d}.wd"
        if not wd_p.is_file():
            recomputed[f"res-{seq:04d}"] = {"available": False}
            continue
        wd = _read_asc(wd_p)
        wd_sub = wd[r0:r1, c0:c1]
        wet = wd_sub > WET_THRESHOLD_M
        recomputed[f"res-{seq:04d}"] = {
            "available": True,
            "t_h": round(seq * 6.0, 1),
            "wse_center_m": round(float(dem[row, col] + wd[row, col]), 2)
            if bool(wet[row - r0, col - c0]) else None,
            "wse_wet_max_m": round(float((dem_sub[wet] + wd_sub[wet]).max()), 2)
            if bool(wet.any()) else None,
            "wet_cells": int(wet.sum()),
        }

    verify_json = run_dir / "verify.json"
    recorded = {}
    if verify_json.is_file():
        recorded = json.loads(verify_json.read_text(encoding="utf-8")).get("wse_gauge_context", {})

    # 逐项核对：verify.json 记录值 vs 本次独立复算值
    checks = []
    for label, t_h in (("0912T14", T_GAUGE_0912_H), ("0914T05", T_GAUGE_0914_H)):
        seq = int(t_h / 6.0)
        rec = recomputed.get(f"res-{seq:04d}", {})
        old = recorded.get(label, {})
        if rec.get("available") and old:
            checks.append({
                "label": label,
                "seq": seq,
                "verify_json_model_wet_wse_m": old.get("model_wet_wse_m"),
                "recomputed_model_wet_wse_m": rec.get("wse_wet_max_m"),
                "match": old.get("model_wet_wse_m") == rec.get("wse_wet_max_m"),
            })

    return {
        "anchor": ANCHOR_LABEL,
        "anchor_lonlat": list(ANCHOR_LONLAT),
        "anchor_row_col": [row, col],
        "window": f"{2 * ANCHOR_HALF_WINDOW + 1}x{2 * ANCHOR_HALF_WINDOW + 1}（verify() 原口径）",
        "wse_definition": "窗口内湿格 (dem + wd) 的最大值；wet = wd > 0.05 m",
        "recorded_in_verify_json": recorded,
        "recomputed_key_frames": recomputed,
        "recompute_checks": checks,
        "recompute_all_match": all(c["match"] for c in checks) if checks else None,
        "note": (
            "recorded_in_verify_json 由 verify() 本身产出，是权威值；"
            "recomputed_key_frames 是本文件按同一口径独立复算的核对样本。"
        ),
    }


def _s1_masks(shape: tuple[int, int], transform, cell: float):
    """按 verify() 原样把 S1 重投影到模型网格，返回 (obsb, valid)。"""
    obs = np.zeros(shape, dtype="uint8")
    cover = np.zeros(shape, dtype="uint8")
    with rasterio.open(S1W_FLD) as ds:
        reproject(
            ds.read(1), obs,
            src_transform=ds.transform, src_crs=ds.crs,
            dst_transform=transform, dst_crs="EPSG:32649",
            resampling=Resampling.nearest,
        )
        reproject(
            (ds.read(1) != 255).astype("uint8"), cover,
            src_transform=ds.transform, src_crs=ds.crs,
            dst_transform=transform, dst_crs="EPSG:32649",
            resampling=Resampling.nearest,
        )
    obsb = (obs == 1) & (cover == 1)
    valid = (cover == 1)
    return obsb, valid


def main() -> None:
    if not S1W_FLD.is_file():
        raise SystemExit(f"S1 水体栅格缺失：{S1W_FLD}")

    # 网格以基线 res.max 的栅格头为准（四情景同网格，逐像元可比）
    ref_run = RUNS_ROOT / SCENARIOS[0][1]
    hdr = read_ascii_header(ref_run / "results" / "res.max")
    cell = float(hdr["cellsize"])
    nrows, ncols = int(hdr["nrows"]), int(hdr["ncols"])
    xll, yll = float(hdr["xllcorner"]), float(hdr["yllcorner"])
    cell_area_m2 = cell * cell
    # 与 verify() 同一构造：Affine(cell, 0, xll, 0, -cell, yll + nrows*cell)
    transform = rasterio.Affine(cell, 0, xll, 0, -cell, yll + nrows * cell)

    obsb, valid = _s1_masks((nrows, ncols), transform, cell)
    area = lambda mask: round(float(mask.sum()) * cell_area_m2 / 1e6, 2)  # noqa: E731

    s1_footprint = {
        "source": str(S1W_FLD.relative_to(PROJ)).replace("\\", "/"),
        "source_sha256": sha256_file(S1W_FLD),
        "scene": SCENE,
        "t_model_h": T_MODEL_H,
        "frame_file": FRAME_FILE,
        "obs_water_cells": int(obsb.sum()),
        "valid_footprint_cells": int(valid.sum()),
        "area_obs_km2_in_footprint": area(obsb),
        "valid_footprint_km2": area(valid),
        "footprint_frac_of_domain": round(float(valid.mean()), 3),
        "threshold_note": "S1 固定阈值 -17 dB；风浪/淹没植被偏保守（G17 降级）",
    }

    per_scenario: dict[str, dict] = {}
    gauge_per_scenario: dict[str, dict] = {}
    for tag, run_id, rain_mode in SCENARIOS:
        run_dir = RUNS_ROOT / run_id
        wd_path = run_dir / "results" / FRAME_FILE
        rmax_path = run_dir / "results" / "res.max"
        if not wd_path.is_file():
            per_scenario[tag] = {
                "run_id": run_id,
                "rain_mode": rain_mode,
                "available": False,
                "error": f"缺少 {FRAME_FILE}",
            }
            continue
        wd = _read_asc(wd_path)
        if wd.shape != (nrows, ncols):
            raise SystemExit(f"{run_id} 的 {FRAME_FILE} 形状 {wd.shape} 与网格 {(nrows, ncols)} 不符")

        sim_wet = wd > WET_THRESHOLD_M
        H = int((sim_wet & obsb).sum())          # 命中
        FA = int((sim_wet & ~obsb & valid).sum())  # 足迹内虚警
        M = int((~sim_wet & obsb).sum())          # 漏报
        per_scenario[tag] = {
            "run_id": run_id,
            "rain_mode": rain_mode,
            "available": True,
            "t_model_h": T_MODEL_H,
            "scene": SCENE,
            "hits_cells": H,
            "false_alarm_cells": FA,
            "misses_cells": M,
            "csi": round(H / max(H + FA + M, 1), 3),
            "pod": round(H / max(H + M, 1), 3),
            "far": round(FA / max(H + FA, 1), 3),
            "area_sim_km2": area(sim_wet),
            "area_sim_in_footprint_km2": area(sim_wet & valid),
            "area_obs_km2_in_footprint": area(obsb),
            "valid_footprint_km2": area(valid),
            "footprint_frac_of_domain": round(float(valid.mean()), 3),
            "wet_cells_frame": int(sim_wet.sum()),
            "wd_sha256": sha256_file(wd_path),
            "res_max_sha256": sha256_file(rmax_path),
        }
        # 第二部分：站点水位诊断（G7 降级口径，仅诊断不作闸门）
        gauge_per_scenario[tag] = _wse_context(run_dir, (nrows, ncols), cell, xll, yll)

    # ---- 与基线对比，并逐条判定计划 §4 验收项 ----
    base = per_scenario.get("baseline_realdem", {})
    comparison: list[dict] = []
    if base.get("available"):
        for tag, _run_id, _mode in SCENARIOS:
            rec = per_scenario[tag]
            if not rec.get("available"):
                continue
            row = {
                "scenario": tag,
                "csi": rec["csi"], "pod": rec["pod"], "far": rec["far"],
                "area_sim_in_footprint_km2": rec["area_sim_in_footprint_km2"],
                "delta_csi": round(rec["csi"] - base["csi"], 3),
                "delta_pod": round(rec["pod"] - base["pod"], 3),
                "delta_far": round(rec["far"] - base["far"], 3),
                "far_change_pct": round(
                    (rec["far"] - base["far"]) / base["far"] * 100.0, 1
                ) if base["far"] else None,
                "area_in_footprint_change_pct": round(
                    (rec["area_sim_in_footprint_km2"] - base["area_sim_in_footprint_km2"])
                    / base["area_sim_in_footprint_km2"] * 100.0, 1
                ) if base["area_sim_in_footprint_km2"] else None,
            }
            row["far_reduction_pct"] = (
                round(-row["far_change_pct"], 1) if row["far_change_pct"] is not None else None
            )
            row["far_target_met"] = bool(
                row["far_reduction_pct"] is not None
                and row["far_reduction_pct"] > PLAN_FAR_REDUCTION_TARGET_PCT
            )
            row["csi_improved"] = bool(row["delta_csi"] > 0)
            comparison.append(row)

    # 逐情景判定（基线自身不参与「达成」判定）
    acceptance: dict[str, dict] = {}
    for row in comparison:
        tag = row["scenario"]
        if tag == "baseline_realdem":
            acceptance[tag] = {
                "is_reference": True,
                "far_reduction_target_met": None,
                "csi_improved": None,
                "verdict": "参照基线，不参与验收判定",
            }
            continue
        far_ok = row["far_target_met"]
        csi_ok = row["csi_improved"]
        if far_ok and csi_ok:
            verdict = "FAR 压降 >20% 且 CSI 提升：两项均达成"
        elif far_ok and not csi_ok:
            verdict = "FAR 压降 >20% 达成；CSI 未提升，CSI 项未达成"
        elif (not far_ok) and csi_ok:
            verdict = "CSI 提升达成；FAR 压降未达 >20%，FAR 项未达成"
        else:
            verdict = "两项均未达成"
        acceptance[tag] = {
            "is_reference": False,
            "far_reduction_pct": row["far_reduction_pct"],
            "far_reduction_target_pct": PLAN_FAR_REDUCTION_TARGET_PCT,
            "far_reduction_target_met": far_ok,
            "delta_csi": row["delta_csi"],
            "csi_improved": csi_ok,
            "verdict": verdict,
        }

    # ---- 基线自校验（与 verify.json 的 s1_0912_check 对照） ----
    baseline_selfcheck = None
    if base.get("available"):
        diffs = {}
        for key, ref in BASELINE_REFERENCE.items():
            got = base[key]
            diffs[key] = {
                "computed": got,
                "verify_json_reference": ref,
                "abs_diff": round(abs(float(got) - float(ref)), 4),
                "match": abs(float(got) - float(ref)) <= 0.011,
            }
        baseline_selfcheck = {
            "compared_against": "outputs/guigang/2024-09-yagi/scen_yagi_q11500_100m_dt2p5_choff_realdem/verify.json::s1_0912_check",
            "tolerance": "|abs_diff| <= 0.011（round 到 3/2 位小数的舍入容差）",
            "fields": diffs,
            "all_match": all(v["match"] for v in diffs.values()),
        }

    payload = {
        "script": "scripts/stage6/validate_guigang_s1.py",
        "generated_utc": utc_now(),
        "read_only": True,
        "not_a_gate": True,
        "purpose": (
            "对贵港基线 + 三个 Phase 2+4 新情景计算 S1 09-12 洪峰景重叠区 POD/FAR/CSI，"
            "如实判定计划 §4「FAR 压降 >20%、CSI 有效提升」是否达成"
        ),
        "method": {
            "reused_from": "scripts/stage5/guigang_scenario.py::verify()（S1 段，逐行复用口径）",
            "t_model_h": T_MODEL_H,
            "scene": SCENE,
            "frame_file": FRAME_FILE,
            "wet_threshold_m": WET_THRESHOLD_M,
            "connectivity": "不适用（单帧像元级比对，不做连通域）",
            "reprojection": "rasterio.warp.reproject + Resampling.nearest → EPSG:32649 模型 100 m 网格",
            "footprint_rule": "只在 S1 足迹内比对（valid = cover==1）；足迹外无观测，不计假警报",
            "area_rule": "面积比较必须用 area_sim_in_footprint_km2，不得用全域 area_sim_km2",
            "cell_area_source": "栅格头 cellsize**2（100 m → 10,000 m²），未写死",
            "cellsize_m": cell,
            "cell_area_m2": cell_area_m2,
            "ncols": ncols,
            "nrows": nrows,
        },
        "s1_footprint": s1_footprint,
        "scenarios": per_scenario,
        "comparison_table": comparison,
        "gauge_diagnostic": {
            "plan_reference": (
                "计划 §Phase5 第 2 条：与贵港站 09-12T14 洪峰水位（43.09 m）比对，"
                "记录垂直基准差异（G7 降级口径）"
            ),
            "gauge_0912_m": GAUGE_0912_M,
            "gauge_0914_m": GAUGE_0914_M,
            "diagnostic_only": True,
            "not_a_gate": True,
            "per_scenario": gauge_per_scenario,
            "shared_caveats": [
                "G7 垂直基准未闭合：站点水位为站基面，模型 WSE 为 DEM 高程基准，二者差值不是 datum_offset_m。",
                "两个时点的 apparent offset 彼此不一致，故不得用作率定值、不得用作同峰对齐依据。",
                "本项仅诊断，不作闸门；未参与任何验收判定。",
                "模型 WSE 取锚点 7×7 窗口内湿格最大值，属邻域包络口径，不是站点单点水位。",
            ],
        },
        "plan_acceptance": {
            "plan_reference": (
                "plans/贵港水动力推演效果改进实施计划_从零散水点治理到上游来水与降雨双驱动重构.md "
                "§4「独立卫星观测检验」行：FAR 压降 > 20%，CSI 有效提升"
            ),
            "far_reduction_target_pct": PLAN_FAR_REDUCTION_TARGET_PCT,
            "per_scenario": acceptance,
            "note": (
                "「CSI 有效提升」计划未给数值门槛，本文件按 delta_csi > 0 判定，"
                "并在 caveats 说明该判定阈值属本项目自定，非计划原文"
            ),
        },
        "baseline_selfcheck": baseline_selfcheck,
        "caveats": [
            "S1 为单景固定阈值 -17 dB 二值水体，风浪与淹没植被致漏判偏多（G17 降级）。",
            "足迹仅覆盖模型域约 20.6%，足迹外无观测；本文件所有 POD/FAR/CSI 只代表该足迹内。",
            "CSI/POD/FAR 是重叠度指标，不是水深或水位精度指标；不得据此声称模型预测正确。",
            "POD 上升与 FAR 下降可同时发生（只要模拟水体向观测水体收缩），故必须与面积一并读。",
            "「CSI 有效提升」计划未给数值门槛，本文件采用 delta_csi > 0；该阈值属本项目自定。",
            "本文件不是闸门：不得把任何一档 CSI 数值写成验收通过或精度证明。",
        ],
    }
    write_json(OUT_JSON, payload)

    # 控制台摘要（便于复核）
    print(f"out: {OUT_JSON}")
    print(f"footprint: {s1_footprint['footprint_frac_of_domain']:.3f} of domain, "
          f"obs {s1_footprint['area_obs_km2_in_footprint']} km2, "
          f"valid {s1_footprint['valid_footprint_km2']} km2")
    print(f"{'scenario':<20}{'CSI':>7}{'POD':>7}{'FAR':>7}{'area_fp':>10}{'dCSI':>8}{'FARred%':>9}")
    for row in comparison:
        print(f"{row['scenario']:<20}{row['csi']:>7.3f}{row['pod']:>7.3f}{row['far']:>7.3f}"
              f"{row['area_sim_in_footprint_km2']:>10.2f}{row['delta_csi']:>8.3f}"
              f"{(row['far_reduction_pct'] or 0):>9.1f}")
    if baseline_selfcheck:
        print(f"baseline selfcheck all_match = {baseline_selfcheck['all_match']}")
    print("---- gauge diagnostic (G7, 仅诊断) ----")
    for tag in ("baseline_realdem", "flow_only_v1", "dualloss_v1", "rainloss_high_v1"):
        g = gauge_per_scenario.get(tag, {})
        rec = (g.get("recorded_in_verify_json") or {}).get("0912T14", {})
        print(f"{tag:<20} 0912T14 model_wet_wse={rec.get('model_wet_wse_m')} m "
              f"gauge={rec.get('gauge_m')} m diff={rec.get('diff_gauge_minus_model_m')} m "
              f"recompute_match={g.get('recompute_all_match')}")


if __name__ == "__main__":
    main()
