"""阶段3：久隆 2024-09 摩羯事件 13 天退水段物理求解器重跑（Green-Ampt 净雨驱动）。

背景与目的：
  久隆原基线 run (scen_free_200m_sv6h_recession) 采用 0 损失毛雨驱动（总雨量 242.5 mm），
  导致 09-10 至 09-17 退水段 84 mm 降雨全量转为地表积水，模拟湿面积由 233.08 km² 反常暴增至
  260.76 km²（+27.68 km²），而实测 Sentinel-1 卫星（09-17T10:50 UTC）水体仅 12.2 km²，
  退水方向完全相反，产生严重退水假象（CSI 仅 0.018，FAR 高达 0.982）。
  
  本脚本应用新构建的降雨-产流损失与土壤水文预处理器（Green-Ampt GA-Mid 物理下渗模型），
  生成扣除土壤非饱和蓄水与稳渗损失后的净雨序列（总净雨 109.7 mm，削减 54.8% 产流水量，
  退水段 84 mm 毛雨削减 75.2% 仅余 20.9 mm 净雨），
  驱动二维浅水动力学物理求解器（LISFLOOD-FP 8.1.0 double，acceleration 求解器），
  重跑 13 天完整物理水动力推演，验证产流水量虚高治理与退水反常纠偏成效。

用法：
  uv --directory 2d_hydro_project run python scripts/stage3/run_jiulong_ga_rerun.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import guard_no_overwrite, read_ascii_grid, write_json  # noqa: E402
from scripts.common.run_manifest import write_run_manifest_best_effort  # noqa: E402

LISFLOOD = PROJ / "models" / "lisflood-fp-8.1" / "LISFLOOD-FP-trunk" / "lisflood.exe"
BASE_RUN = PROJ / "outputs" / "jiulong" / "2024-09-yagi" / "scen_free_200m_sv6h_recession"
DT = float(sys.argv[sys.argv.index("--dt") + 1]) if "--dt" in sys.argv else 2.5
dt_tag = f"_dt{str(DT).replace('.', 'p')}" if abs(DT - 5.0) > 1e-9 else ""
TARGET_DIR = PROJ / "outputs" / "jiulong" / "2024-09-yagi" / f"scen_free_200m{dt_tag}_sv6h_recession_ga_v1"
PREPROCESSOR_RAIN = PROJ / "outputs" / "preprocessor" / "jiulong" / "rainfall_net_green_ampt.rain"
S1_DIR = PROJ / "data" / "processed" / "s1_water"
OUT_CATALOG_JSON = PROJ / "data" / "catalog" / f"jiulong_ga_v1{dt_tag}_s1_validation.json"

SIM_SECONDS = 1123200.0  # 13 days
SAVEINT = 21600.0        # 6 hours
RES = 200.0
WET_THR = 0.05
S1_FRAC_THR = 0.2


def prepare_run() -> None:
    print(f"=== 1. 准备物理重跑目录: {TARGET_DIR.name} ===")
    guard_no_overwrite(TARGET_DIR)
    TARGET_DIR.mkdir(parents=True, exist_ok=True)

    # 1. 复制基础地形与边界配置（严格保持与基线一致，控制唯一变量为净雨驱动）
    for fname in ["dem.asc", "bci.bci", "sea_mask.npy"]:
        src = BASE_RUN / fname
        dst = TARGET_DIR / fname
        if src.is_file():
            shutil.copy2(src, dst)
            print(f"  已复制 {fname} (大小: {src.stat().st_size / 1024:.1f} KB)")
        else:
            raise FileNotFoundError(f"基线必需文件缺失: {src}")

    # 2. 准备降雨文件 rain.rain（转换为 seconds 格式）
    if not PREPROCESSOR_RAIN.is_file():
        raise FileNotFoundError(f"预处理器净雨文件不存在: {PREPROCESSOR_RAIN}")

    lines = PREPROCESSOR_RAIN.read_text(encoding="utf-8").splitlines()
    # 提取时间与强度
    pts: list[tuple[float, float]] = []
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("#") or "hours" in ln or "seconds" in ln:
            continue
        parts = ln.split()
        if len(parts) >= 2:
            rate_mm_h = float(parts[0])
            t_val = float(parts[1])
            # 如果是 hours，转为 seconds
            if t_val <= 312.0:
                t_sec = t_val * 3600.0
            else:
                t_sec = t_val
            pts.append((rate_mm_h, t_sec))

    pts.sort(key=lambda x: x[1])
    # 确保起点为 0，终点覆盖 SIM_SECONDS
    if pts[0][1] > 0.0:
        pts.insert(0, (0.0, 0.0))
    if pts[-1][1] < SIM_SECONDS:
        pts.append((0.0, SIM_SECONDS))

    rain_content = [
        "# Net rainfall driven by Green-Ampt infiltration preprocessor (GA-Mid, South China Red Soil)",
        f"{len(pts)} seconds",
    ]
    for r, t in pts:
        rain_content.append(f"{r:.4f}\t{t:.0f}")

    (TARGET_DIR / "rain.rain").write_text("\n".join(rain_content) + "\n", encoding="ascii")
    print(f"  已生成 rain.rain: {len(pts)} 个时间点, 持续时间 {pts[-1][1] / 86400:.1f} 天")

    # 3. 写入 run.par
    par_content = (
        "DEMfile\tdem.asc\n"
        "bcifile\tbci.bci\n"
        "rainfall\train.rain\n"
        "resroot\tres\n"
        "dirroot\tresults\n"
        f"sim_time\t{SIM_SECONDS:.0f}\n"
        f"initial_tstep\t{DT}\n"
        "massint\t3600\n"
        f"saveint\t{SAVEINT:.0f}\n"
        "fpfric\t0.035\n"
        "acceleration\n"
    )
    (TARGET_DIR / "run.par").write_text(par_content, encoding="ascii")
    print("  已生成 run.par")

    # 4. 写入 meta.json
    meta = {
        "scenario": "free",
        "run_tag": "ga_v1",
        "res": RES,
        "dt": DT,
        "sim_seconds": SIM_SECONDS,
        "saveint": SAVEINT,
        "solver": "acceleration",
        "boundary_mode": "allfree",
        "inflow_model": "rainfall_only",
        "rainfall_preprocessing": {
            "model": "Green-Ampt (GA-Mid)",
            "soil_type": "South China Red Soil (clay loam / loam)",
            "K_sat_mm_h": 2.58,
            "theta_s": 0.460,
            "theta_i": 0.280,
            "suction_psi_mm": 216.0,
            "gross_rain_mm": 242.50,
            "net_rain_mm": 109.70,
            "loss_rain_mm": 132.80,
            "runoff_coefficient": 0.452,
            "recession_gross_mm": 84.0,
            "recession_net_mm": 20.85,
        },
        "purpose": "久隆2024-09摩羯退水段物理求解器重跑：测试 Green-Ampt 净雨驱动对退水反常（+27.68 km²）的物理纠偏与 S1 弱验证改善",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
    }
    (TARGET_DIR / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print("  已写入 meta.json")


def execute_solver() -> dict:
    import psutil

    print(f"=== 2. 启动 LISFLOOD-FP 8.1 物理求解器 ===")
    print(f"  工作目录: {TARGET_DIR}")
    print(f"  求解器路径: {LISFLOOD}")
    
    t0 = time.monotonic()
    proc = subprocess.Popen(
        [str(LISFLOOD), "run.par"],
        cwd=TARGET_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    peak = {"mb": 0.0}
    p = psutil.Process(proc.pid)

    def poll():
        while proc.poll() is None:
            try:
                peak["mb"] = max(peak["mb"], p.memory_info().rss / 1e6)
            except Exception:
                break
            time.sleep(0.5)

    poll_thread = threading.Thread(target=poll, daemon=True)
    poll_thread.start()

    started_utc = datetime.now(timezone.utc).isoformat()
    stdout_lines: list[str] = []
    
    last_print = time.monotonic()
    while True:
        line = proc.stdout.readline()
        if not line and proc.poll() is not None:
            break
        if line:
            stdout_lines.append(line)
            now = time.monotonic()
            if now - last_print > 15.0 or "Time" in line or "Total" in line:
                print(f"  [LISFLOOD {now - t0:.1f}s] {line.strip()}")
                last_print = now

    proc.wait()
    wall_s = round(time.monotonic() - t0, 1)
    out_text = "".join(stdout_lines)
    (TARGET_DIR / "run.log").write_text(out_text, encoding="utf-8", errors="replace")

    print(f"  LISFLOOD 完成! 返回码: {proc.returncode}, 耗时: {wall_s}s ({wall_s / 60:.1f} min), 内存峰值: {peak['mb']:.1f} MB")
    if proc.returncode != 0:
        raise RuntimeError(f"LISFLOOD 异常退出，返回码: {proc.returncode}")

    write_run_manifest_best_effort(
        TARGET_DIR,
        peak_rss_mb=round(peak["mb"], 1),
        started_utc=started_utc,
        warnings=[],
    )
    return {"exit": proc.returncode, "wall_s": wall_s, "peak_mb": round(peak["mb"], 1)}


def read_asc(path: Path) -> tuple[np.ndarray, dict]:
    with open(path) as f:
        hdr = {}
        for _ in range(6):
            k, *v = next(f).split()
            hdr[k.lower()] = float(v[0]) if v else None
        arr = np.loadtxt(f)
    return arr, hdr


def compute_mass_balance(run_dir: Path) -> dict:
    mass_file = run_dir / "results" / "res.mass"
    if not mass_file.is_file():
        raise FileNotFoundError(f"缺少 res.mass: {mass_file}")
    lines = mass_file.read_text().splitlines()
    keys = lines[0].split()
    rows = [dict(zip(keys, map(float, ln.split()))) for ln in lines[1:] if ln.split()]
    v_in = sum(0.5 * (a["Qin"] + b["Qin"]) * (b["Time"] - a["Time"]) for a, b in zip(rows, rows[1:]))
    v_out = sum(0.5 * (a["Qout"] + b["Qout"]) * (b["Time"] - a["Time"]) for a, b in zip(rows, rows[1:]))
    v0 = rows[0]["Vol"]
    ve = rows[-1]["Vol"]
    v_rain = rows[-1]["Rain-(Inf+Evap)"]
    resid = ve - v0 - v_rain - (v_in - v_out)
    denom = max(v0 + v_rain + v_in, 1.0)
    e_m = abs(resid) / denom
    return {
        "v0_m3": v0,
        "ve_m3": ve,
        "v_rain_m3": v_rain,
        "v_in_m3": v_in,
        "v_out_m3": v_out,
        "residual_m3": resid,
        "e_mass": e_m,
        "gate_pass": bool(e_m <= 1e-3),
    }


def s1_to_grid(tif: Path, shape, transform) -> tuple[np.ndarray, np.ndarray]:
    frac = np.zeros(shape, dtype="float32")
    cover = np.zeros(shape, dtype="float32")
    with rasterio.open(tif) as ds:
        reproject((ds.read(1) == 1).astype("float32"), frac,
                  src_transform=ds.transform, src_crs=ds.crs,
                  dst_transform=transform, dst_crs="EPSG:32649",
                  resampling=Resampling.average, src_nodata=255.0)
        reproject((ds.read(1) != 255).astype("float32"), cover,
                  src_transform=ds.transform, src_crs=ds.crs,
                  dst_transform=transform, dst_crs="EPSG:32649",
                  resampling=Resampling.average, src_nodata=0.0)
    return frac, cover


def frame_wet(run_dir: Path, idx: int, sea: np.ndarray) -> tuple[np.ndarray, float]:
    p = run_dir / "results" / f"res-{idx:04d}.wd"
    wd, _ = read_asc(p)
    wet = (wd > WET_THR) & ~sea
    return wet, float(wet.sum()) * (RES ** 2) / 1e6


def compute_metrics(m_wet: np.ndarray, s1_wat: np.ndarray, valid: np.ndarray) -> dict:
    m = m_wet & valid
    o = s1_wat & valid
    h = int((m & o).sum())
    fa = int((m & ~o).sum())
    ms = int((~m & o).sum())
    csi = h / (h + fa + ms) if (h + fa + ms) else 0.0
    pod = h / (h + ms) if (h + ms) else 0.0
    far = fa / (h + fa) if (h + fa) else 0.0
    return {
        "hit_cells": h,
        "false_alarm_cells": fa,
        "miss_cells": ms,
        "csi": round(csi, 4),
        "pod": round(pod, 4),
        "far": round(far, 4),
        "model_wet_km2": round(float(m.sum()) * (RES ** 2) / 1e6, 2),
        "s1_water_km2": round(float(o.sum()) * (RES ** 2) / 1e6, 2),
    }


def analyze_and_validate() -> dict:
    print("=== 3. 运行后质检与 S1 卫星弱验证对比 ===")
    
    # 质量门检查
    mb_new = compute_mass_balance(TARGET_DIR)
    mb_base = compute_mass_balance(BASE_RUN)
    print(f"  [质量门 G15 检查]")
    print(f"    新 Run E_mass: {mb_new['e_mass']:.3e} (PASS: {mb_new['gate_pass']})")
    print(f"    基线 E_mass:   {mb_base['e_mass']:.3e} (PASS: {mb_base['gate_pass']})")
    print(f"    净雨入流量: 新 Run = {mb_new['v_rain_m3'] / 1e6:.2f} Mm³, 基线 = {mb_base['v_rain_m3'] / 1e6:.2f} Mm³ (削减 {(1.0 - mb_new['v_rain_m3'] / mb_base['v_rain_m3']) * 100:.1f}%)")

    rmax_new, hdr = read_asc(TARGET_DIR / "results" / "res.max")
    rmax_base, _ = read_asc(BASE_RUN / "results" / "res.max")
    shape = rmax_new.shape
    transform = rasterio.Affine(RES, 0, hdr["xllcorner"], 0, -RES, hdr["yllcorner"] + shape[0] * RES)
    sea = np.load(TARGET_DIR / "sea_mask.npy")

    # 帧索引：09-10T00 (seq 20) 与 09-17 S1 过境 (seq 51)
    idx_0910 = int(round(5 * 86400 / SAVEINT))  # 20
    t_target = (17 - 5) * 86400 + 18 * 3600 + 50 * 60 + 23
    frames = sorted(int(p.stem.split("-")[1]) for p in (TARGET_DIR / "results").glob("res-*.wd"))
    idx_s1 = min(frames, key=lambda i: abs(i * SAVEINT - t_target))  # 51

    # 1. 退水期面积变化 (09-10T00 -> 09-17 过境帧)
    wet_0910_new, area_0910_new = frame_wet(TARGET_DIR, idx_0910, sea)
    wet_s1_new, area_s1_new = frame_wet(TARGET_DIR, idx_s1, sea)
    delta_area_new = area_s1_new - area_0910_new

    wet_0910_base, area_0910_base = frame_wet(BASE_RUN, idx_0910, sea)
    wet_s1_base, area_s1_base = frame_wet(BASE_RUN, idx_s1, sea)
    delta_area_base = area_s1_base - area_0910_base

    print(f"\n  [退水段 (09-10T00 至 09-17 S1过境) 湿面积动态]")
    print(f"    基线 (零损失): 09-10={area_0910_base:.2f} km² -> 09-17={area_s1_base:.2f} km², 变化 = {delta_area_base:+.2f} km² (反常增加!)")
    print(f"    新 Run (GA-Mid): 09-10={area_0910_new:.2f} km² -> 09-17={area_s1_new:.2f} km², 变化 = {delta_area_new:+.2f} km²")

    # 2. S1 09-17 水体对照
    s1_tif_17 = S1_DIR / "jiulong_20240917T105023_water20m.tif"
    s1_tif_05 = S1_DIR / "jiulong_20240905T105022_water20m.tif"
    frac17, cov17 = s1_to_grid(s1_tif_17, shape, transform)
    frac05, cov05 = s1_to_grid(s1_tif_05, shape, transform)
    valid = (cov17 > 0.5) & ~sea
    s1_w17 = (frac17 > S1_FRAC_THR) & valid
    s1_w05 = (frac05 > S1_FRAC_THR) & (cov05 > 0.5) & ~sea

    m_new = compute_metrics(wet_s1_new, s1_w17, valid)
    m_base = compute_metrics(wet_s1_base, s1_w17, valid)

    # 3. 最大水深与峰值淹没对比
    wet_max_new = (rmax_new > WET_THR) & ~sea
    wet_max_base = (rmax_base > WET_THR) & ~sea
    area_max_new = wet_max_new.sum() * (RES ** 2) / 1e6
    area_max_base = wet_max_base.sum() * (RES ** 2) / 1e6

    print(f"\n  [S1 09-17 弱验证指标对比 (S1实测水体={m_base['s1_water_km2']} km²)]")
    print(f"    指标      | 基线 (零损失)       | 新 Run (GA-Mid)     | 变化")
    print(f"    ----------+--------------------+--------------------+----------")
    print(f"    模拟湿面积| {m_base['model_wet_km2']:8.2f} km²        | {m_new['model_wet_km2']:8.2f} km²        | {m_new['model_wet_km2'] - m_base['model_wet_km2']:+8.2f} km² ({(m_new['model_wet_km2'] / m_base['model_wet_km2'] - 1.0) * 100:+.1f}%)")
    print(f"    命中像元  | {m_base['hit_cells']:8d} 格         | {m_new['hit_cells']:8d} 格         | {m_new['hit_cells'] - m_base['hit_cells']:+8d} 格")
    print(f"    假警报像元| {m_base['false_alarm_cells']:8d} 格         | {m_new['false_alarm_cells']:8d} 格         | {m_new['false_alarm_cells'] - m_base['false_alarm_cells']:+8d} 格 (压降 {(1.0 - m_new['false_alarm_cells'] / m_base['false_alarm_cells']) * 100:.1f}%)")
    print(f"    漏报像元  | {m_base['miss_cells']:8d} 格         | {m_new['miss_cells']:8d} 格         | {m_new['miss_cells'] - m_base['miss_cells']:+8d} 格")
    print(f"    POD 命中率| {m_base['pod']:8.3f}             | {m_new['pod']:8.3f}             | {m_new['pod'] - m_base['pod']:+8.3f}")
    print(f"    FAR 误报率| {m_base['far']:8.3f}             | {m_new['far']:8.3f}             | {m_new['far'] - m_base['far']:+8.3f}")
    print(f"    CSI 临界成功| {m_base['csi']:8.3f}           | {m_new['csi']:8.3f}           | {m_new['csi'] - m_base['csi']:+8.3f}")
    print(f"    全周期峰值面积| {area_max_base:8.2f} km²      | {area_max_new:8.2f} km²      | {area_max_new - area_max_base:+8.2f} km²")

    validation_summary = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "baseline_run": BASE_RUN.name,
        "new_run": TARGET_DIR.name,
        "rainfall_driver": "Green-Ampt GA-Mid",
        "mass_balance": {
            "baseline": mb_base,
            "new_run": mb_new,
        },
        "recession_area_dynamics_km2": {
            "baseline_0910": round(area_0910_base, 2),
            "baseline_0917": round(area_s1_base, 2),
            "baseline_change": round(delta_area_base, 2),
            "new_0910": round(area_0910_new, 2),
            "new_0917": round(area_s1_new, 2),
            "new_change": round(delta_area_new, 2),
            "reversal_of_anomaly": bool(delta_area_new < delta_area_base),
        },
        "s1_0917_validation": {
            "s1_obs_water_km2": m_base["s1_water_km2"],
            "baseline": m_base,
            "new_run": m_new,
            "metrics_delta": {
                "d_model_wet_km2": round(m_new["model_wet_km2"] - m_base["model_wet_km2"], 2),
                "false_alarm_reduction_pct": round((1.0 - m_new["false_alarm_cells"] / m_base["false_alarm_cells"]) * 100, 2),
                "d_pod": round(m_new["pod"] - m_base["pod"], 4),
                "d_far": round(m_new["far"] - m_base["far"], 4),
                "d_csi": round(m_new["csi"] - m_base["csi"], 4),
            },
        },
        "max_extent_km2": {
            "baseline": round(area_max_base, 2),
            "new_run": round(area_max_new, 2),
            "reduction_km2": round(area_max_base - area_max_new, 2),
        },
    }
    OUT_CATALOG_JSON.write_text(json.dumps(validation_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  已更新验证指标报告: {OUT_CATALOG_JSON}")
    return validation_summary


def main() -> None:
    t_start = time.monotonic()
    print("======================================================================")
    print("=== 钦州久隆 2024-09 摩羯事件 物理求解器 Green-Ampt 净雨重跑与验证 ===")
    print("======================================================================")
    
    # 1. 准备物理运行输入
    prepare_run()

    # 2. 运行 LISFLOOD-FP
    execute_solver()

    # 3. 分析与验证对比
    summary = analyze_and_validate()

    total_time = round(time.monotonic() - t_start, 1)
    print("======================================================================")
    print(f"=== 重跑全部完成! 总耗时: {total_time}s ({total_time / 60:.1f} min) ===")
    print("======================================================================")


if __name__ == "__main__":
    main()
