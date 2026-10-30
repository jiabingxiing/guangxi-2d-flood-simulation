"""阶段5：贵港 2024-09 摩羯郁江洪水 情景回算（降级模式，ADR-0004）。

管线：走廊 DEM（本域实测无负值垃圾，G16 仅沿海平原）-> S1 09-05 常水河道定位
     -> 参数化河槽刻蚀 S2 -> 上游郁江三角 Q 情景（公开洪峰证据锚定）+ ERA5 均匀雨
     -> 运行 -> 验证（S1 09-12 洪峰景直接对比 + 市区锚点 WSE 诊断对照 43.09m）。
情景假设（成果必须标注）：
  - 上游 Q(t) 为三角情景线（基流 1000 -> 峰 11500 m3/s @09-12T12 -> 2500），
    锚定公开报道：南宁站 09-12T19:30 峰 11900 m3/s、贵港站 09-12T14 43.09m/10500m3/s；
  - 降雨 ERA5 市区锚点逐时均匀（降级）；
  - 下游东缘 FREE 带坡度；垂直基准未确认（G7）：站水位 43.09m 仅作诊断对照，不作闸门。
用法：uv run python scripts/stage5/guigang_scenario.py [all|prep|run|verify]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.warp import reproject

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))
from scripts.common.run_manifest import write_run_manifest_best_effort  # noqa: E402

LISFLOOD = PROJ / "models" / "lisflood-fp-8.1" / "LISFLOOD-FP-trunk" / "lisflood.exe"
RAW = Path(r"D:\NWKYYSJZX\2026年工作\0731 五省区DEM\广西\广西.tif")
S1W_PRE = PROJ / "data" / "processed" / "s1_water" / "guigang_20240905T105022_water20m.tif"
S1W_FLD = PROJ / "data" / "processed" / "s1_water" / "guigang_20240912T104222_water20m.tif"
SIG2 = PROJ / "data" / "processed" / "s1_water" / "guigang_20240912T104222_sigma20m.tif"
SIG1 = PROJ / "data" / "processed" / "s1_water" / "guigang_20240905T105022_sigma20m.tif"
ERA5 = PROJ / "data" / "catalog" / "probes" / "guigang_sep2024_era5_hourly.json"

AOI = (109.30, 22.98, 109.95, 23.32)
# Phase B1：网格/时间步敏感性（终稿 §11 第 7 项）。默认值=首轮基线 200 m / dt 5 s。
RES = float(sys.argv[sys.argv.index("--res") + 1]) if "--res" in sys.argv else 200.0
DT = float(sys.argv[sys.argv.index("--dt") + 1]) if "--dt" in sys.argv else 5.0
SIM_SECONDS = 14 * 86400  # 09-03T00 ~ 09-17T00（前 2 天基流充河槽）
T0_DAY = 3  # 起算日（9 月 3 日）
Q_BASE = 1000.0
Q_PEAK = float(sys.argv[sys.argv.index("--qpeak") + 1]) if "--qpeak" in sys.argv else 11500.0
CHANNEL = sys.argv[sys.argv.index("--channel") + 1] if "--channel" in sys.argv else "off"
# 河槽宽按米参数化（终稿 六.5 情景意图「宽 600m」）：半宽格数随分辨率折算，
# 200m 下 = 3 格（r-1..r+2），与首轮基线逐位一致；网格变细时保持物理宽度不变（B5 网格收敛前提）。
CHANNEL_WIDTH_M = float(sys.argv[sys.argv.index("--channel-width") + 1]) if "--channel-width" in sys.argv else 600.0
CH_HALF = max(1, int(round(CHANNEL_WIDTH_M / RES)) // 2)
ANCHOR = ("贵港市区郁江段", 109.60, 23.11)
ANCHOR_WSE_DIAG = 43.09  # 贵港站 09-12T14 水位（站基面，垂直基准未确认 G7，仅诊断）
GAUGE_0912_M = 43.09
GAUGE_0914_M = 44.00
T_GAUGE_0912_H = 228.0
T_GAUGE_0914_H = 270.0
RUN_TAG = os.environ.get("RUN_TAG", "").strip()
ch_tag = f"_ch{CHANNEL}" if CHANNEL != "linear" else ""
RUN_DIR = PROJ / "outputs" / "guigang" / "2024-09-yagi" / (
    f"scen_yagi_q{int(Q_PEAK)}_{int(RES)}m"
    + (f"_dt{str(DT).replace('.', 'p')}" if abs(DT - 5.0) > 1e-9 else "")
    + ch_tag
    + (f"_{RUN_TAG}" if RUN_TAG else ""))


def _t(day: int, hh: int = 0) -> float:
    return (day - T0_DAY) * 86400.0 + hh * 3600.0


def prep() -> None:
    try:
        from scripts.common.run_manifest import prepare_run_dir
        prepare_run_dir(RUN_DIR)
    except FileExistsError:
        raise SystemExit(f"{RUN_DIR} 已有 res.mass；终稿 §10 禁止覆盖。请设置 RUN_TAG 使用新目录。") from None
    tf = Transformer.from_crs(4326, 32649, always_xy=True)
    corners = [tf.transform(x, y) for x, y in [(AOI[0], AOI[1]), (AOI[2], AOI[3])]]
    b = (min(c[0] for c in corners), min(c[1] for c in corners),
         max(c[0] for c in corners), max(c[1] for c in corners))
    tgt = rasterio.Affine(RES, 0, b[0], 0, -RES, b[3])
    shape = (int((b[3] - b[1]) / RES), int((b[2] - b[0]) / RES))
    with rasterio.open(RAW) as src:
        with WarpedVRT(src, crs="EPSG:32649", resampling=Resampling.bilinear,
                       width=shape[1], height=shape[0], transform=tgt) as v:
            dem = v.read(1).astype("float32")
    h, w = dem.shape
    dem[dem == -9999.0] = np.nanmax(dem[dem != -9999.0])
    assert not (dem < 0).any(), "贵港走廊出现负值（G16 预排查为 0，若触发须复核）"
    dem_pre_edit = dem.copy()  # 河槽刻蚀前的原始地形（用于地形修改留痕）

    # S1 09-05 常水水体 -> 河道定位
    # 河道定位：D8 山谷追踪（锚点吸附谷底，上下游各走）。S1 掩膜路线三版皆败
    # （足印覆盖 58%/74% 截断致分量碎裂、端点退化，2026-10-25），改用 DEM 山谷本身；
    # S1 掩膜仅用于验证（verify），不再参与河槽几何
    # 走廊 DEM 实测为宽平底平原（平滑面低带 16~27m 遍布全域，200m 下无河谷可循；
    # 城区江段未被 S1 检出，D8 无梯度可走，2026-10-25 三版追踪皆败）——按终稿 六.5
    # 参数化河槽：过锚点沿郁江过境走向直线河槽，宽 3 格（600m），
    # 床 = 沿线地表 polyfit 线性 - 12m。几何为情景假设，成果必须标注。
    # 走向取 100°（东偏南 ~10°）：低带实测在南半部（行 130~154），70° 走向北漂进山（首版 bed 98m 已废弃）
    # 另：城区平原 DEM ~21m vs 贵港站洪峰 43.09m -> 垂直基准差约 -20m（G7 实证，对照仅诊断）
    arow, acol = rasterio.transform.rowcol(tgt, *tf.transform(ANCHOR[1], ANCHOR[2]))
    arow, acol = int(arow), int(acol)
    dc, dr = np.sin(np.radians(100.0)), -np.cos(np.radians(100.0))  # 东分量->列，北分量->行（行向下为北减）
    ts = np.arange(-2 * max(h, w), 2 * max(h, w) + 1, 0.5)
    rr = np.round(arow + ts * dr).astype(int)
    cc = np.round(acol + ts * dc).astype(int)
    inb = (rr >= 0) & (rr < h) & (cc >= 0) & (cc < w)
    path = list(dict.fromkeys((int(r), int(c)) for r, c in zip(rr[inb], cc[inb])))  # 保序去重
    print(f"河槽(参数化直线): {len(path)} 节点，过锚点(行{arow},列{acol})，走向 100°/280°，"
          f"宽 {2 * CH_HALF + 1} 格（≈{(2 * CH_HALF + 1) * RES:.0f}m，目标 {CHANNEL_WIDTH_M:.0f}m）")
    seen = set()
    path = [p for p in path if not (p in seen or seen.add(p))]  # 去重保序

    carved = 0
    if CHANNEL != "off":
        print(f"河槽(参数化直线): {len(path)} 节点，过锚点(行{arow},列{acol})，走向 100°/280°，"
              f"宽 {2 * CH_HALF + 1} 格（≈{(2 * CH_HALF + 1) * RES:.0f}m，目标 {CHANNEL_WIDTH_M:.0f}m）")
        # 河槽纵坡 v2（2026-10-25 二修）：v1 polyfit 拷贝了沿线地形 30m 落差（真郁江该河段 ~8m），
        # 中段岸深被拉薄 -> 11500m3/s 沿线漫溢（首轮湿 487km²）。
        # v2：床 = 沿线局部地表（±1 带最小值，15 节点滑动均值）- 12m，再向下游累计取小保证单调
        # —— 岸深处处 12m，纵坡跟随地形但不会平均化拉薄中段岸深。
        surf = []
        for r, c in path:
            r0, r1 = max(0, r - CH_HALF), min(h, r + CH_HALF + 1)
            c0, c1 = max(0, c - CH_HALF), min(w, c + CH_HALF + 1)
            surf.append(float(dem[r0:r1, c0:c1].min()))
        surf = np.array(surf)
        ker = np.ones(15) / 15.0
        surf_s = np.convolve(np.pad(surf, 7, mode="edge"), ker, mode="valid")
        bed_line = surf_s - 12.0
        bed_line = np.minimum.accumulate(bed_line)  # 向下游累计取小（单调不升）
        for i, (r, c) in enumerate(path):
            r0, r1 = max(0, r - CH_HALF), min(h, r + CH_HALF + 1)
            c0, c1 = max(0, c - CH_HALF), min(w, c + CH_HALF + 1)
            bed_c = float(bed_line[i])
            sub = dem[r0:r1, c0:c1]
            loc = sub > bed_c + 1.5
            if loc.any():
                sub[loc] = bed_c
                dem[r0:r1, c0:c1] = sub
                carved += int(loc.sum())
        bed_w, bed_e = float(bed_line[0]), float(bed_line[-1])
        if not (0 < bed_w - bed_e <= 60 and 0 < bed_e < 80):
            raise SystemExit(f"河槽底坡不合理：bed_up={bed_w:.1f} bed_dn={bed_e:.1f}（主分量选错或 DEM 异常）")
        print(f"河槽: 路径 {len(path)} 节点（起点(行{path[0][0]},列{path[0][1]})->终点(行{path[-1][0]},列{path[-1][1]})），"
              f"刻蚀 {carved} 格，底 上游{bed_w:.1f}->下游{bed_e:.1f} m")
    else:
        bed_w = float(dem[path[0][0], path[0][1]])
        bed_e = float(dem[path[-1][0], path[-1][1]])
        print(f"河槽[off模式]: 未施加任何人工河槽刻蚀与虚假下切。路径起点底高={bed_w:.1f}m，终点={bed_e:.1f}m")

    # 地形修改留痕（可视化「地形修改证据」层与审计读取）
    edit_mask = np.abs(dem - dem_pre_edit) > 1e-6
    if edit_mask.any():
        np.savetxt(RUN_DIR / "dem_pre_carve.diff.txt",
                   np.where(edit_mask, dem - dem_pre_edit, 0.0),
                   fmt="%.2f", delimiter="\t",
                   header=f"ncols {w}\nnrows {h}\nxllcorner {b[0]:.1f}\nyllcorner {b[1]:.1f}\n"
                          f"cellsize {RES:.1f}\nNODATA_value -9999.0", comments="")
        base = {"driver": "GTiff", "height": h, "width": w, "count": 1,
                "crs": "EPSG:32649", "transform": tgt, "compress": "lzw"}
        with rasterio.open(RUN_DIR / "dem_edit_mask.tif", "w", dtype="uint8", **base) as dst:
            dst.write(edit_mask.astype("uint8"), 1)
        with rasterio.open(RUN_DIR / "dem_edit_diff.tif", "w", dtype="float32",
                           nodata=0.0, **base) as dst:
            dst.write(np.where(edit_mask, dem - dem_pre_edit, 0.0).astype("float32"), 1)
    sx, sy = tgt * (path[0][1] + 0.5, path[0][0] + 0.5)  # 路径起点（上游）= 点源
    np.savetxt(RUN_DIR / "dem.asc", dem, fmt="%.3f", delimiter="\t",
               header=f"ncols         {w}\nnrows         {h}\nxllcorner     {b[0]:.1f}\n"
                      f"yllcorner     {b[1]:.1f}\ncellsize      {RES:.1f}\nNODATA_value  -9999.0",
               comments="")

    BIG = 99999999
    (RUN_DIR / "bci.bci").write_text(
        f"P\t{sx:.1f}\t{sy:.1f}\tQVAR\tyujiang\n"
        f"E\t-{BIG}\t{BIG}\tFREE\t0.00015\n"
        f"W\t-{BIG}\t{BIG}\tFREE\t0.0001\n"
        f"N\t-{BIG}\t{BIG}\tFREE\nS\t-{BIG}\t{BIG}\tFREE\n", encoding="ascii", newline="\n")

    # 上游三角 Q（标度律 B6：文件值 = Q/网格边长）
    scale = RES
    ts_q = [(0.0, Q_BASE), (_t(8), Q_BASE), (_t(12, 12), Q_PEAK), (_t(16, 12), 2500.0),
            (SIM_SECONDS, 2000.0)]
    (RUN_DIR / "yujiang.bdy").write_text(
        "# scenario upstream hydrograph (triangular, public-peak anchored); file_value = Q_m3s / cellsize\n"
        f"yujiang\n{len(ts_q)} seconds\n" +
        "".join(f"{q / scale:.4f}\t{t:.0f}\n" for t, q in ts_q), encoding="ascii", newline="\n")

    hourly = json.loads(ERA5.read_text(encoding="utf-8"))["hourly_mm"]
    pts = [(0.0, 0.0)]
    for tkey in sorted(hourly):
        day, hh = tkey.split("T")
        t = (int(day[-2:]) - T0_DAY) * 86400 + int(hh[:2]) * 3600
        if 0 < t <= SIM_SECONDS:
            pts.append((hourly[tkey], float(t)))
    pts.append((0.0, SIM_SECONDS))
    pts = sorted({t: v for v, t in pts}.items())  # 去重：末日T00与末点重号会致时刻非递增（实测 exit -1）
    assert all(b[0] > a[0] for a, b in zip(pts, pts[1:])), "rain 时刻必须严格递增"
    (RUN_DIR / "rain.rain").write_text(
        "# ERA5 guigang anchor hourly uniform rain (degraded)\n"
        f"{len(pts)} seconds\n" +
        "".join(f"{v:.4f}\t{t:.0f}\n" for t, v in pts), encoding="ascii", newline="\n")

    (RUN_DIR / "run.par").write_text(
        f"DEMfile\tdem.asc\nbcifile\tbci.bci\nbdyfile\tyujiang.bdy\nrainfall\train.rain\n"
        f"resroot\tres\ndirroot\tresults\nsim_time\t{SIM_SECONDS:.0f}\n"
        f"initial_tstep\t{DT}\nmassint\t3600\nsaveint\t21600\n"
        f"fpfric\t0.035\nacceleration\n", encoding="ascii", newline="\n")
    if CHANNEL == "off":
        ch_meta = {
            "mode": "off",
            "carved_px": 0,
            "path_nodes": len(path),
            "note": "off: 真实DEM未施加任何人工河槽刻蚀与虚假下切，严格采用源地形",
        }
    else:
        ch_meta = {
            "mode": "linear",
            "path_nodes": len(path), "carved_px": carved,
            "bed_up_m": round(bed_w, 1), "bed_dn_m": round(bed_e, 1),
            "depth_m": 12.0, "width_cells": 2 * CH_HALF + 1,
            "width_actual_m": round((2 * CH_HALF + 1) * RES), "width_target_m": CHANNEL_WIDTH_M,
            "method_version": "v3",
            "method": "参数化直线河槽（过市区锚点、走向100°/280°、宽按米参数化≈600m、床=局部地表−12m + 下游累计取小）——走廊为宽平底平原无河谷可循，纯情景假设；v3 起宽度按米跨分辨率一致（200m 下与 v2 逐位同）",
        }
    meta = {"run_id": RUN_DIR.name, "aoi": AOI, "res": RES, "dt": DT,
            "q_base": Q_BASE, "q_peak": Q_PEAK, "sim_seconds": SIM_SECONDS,
            "t0": "2024-09-03T00 Asia/Shanghai（前2日基流充槽）",
            "point_source_32649": [round(sx, 1), round(sy, 1)],
            "channel": ch_meta,
            "evidence": "南宁站 09-12T19:30 峰 11900m3/s；贵港站 09-12T14 43.09m/10500m3/s（公开转引）",
            "note": "上游 Q 为情景三角线（无逐时实测）；WSE 与站水位对照受 G7 垂直基准未确认限制，仅诊断；情景级结论",
            "generated_utc": datetime.now(timezone.utc).isoformat()}
    (RUN_DIR / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print("prep 完成 ->", RUN_DIR)


def run() -> dict:
    import psutil
    proc = subprocess.Popen([str(LISFLOOD), "run.par"], cwd=RUN_DIR,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    peak = {"mb": 0.0}
    p = psutil.Process(proc.pid)

    def poll():
        while proc.poll() is None:
            try:
                peak["mb"] = max(peak["mb"], p.memory_info().rss / 1e6)
            except Exception:
                break
            time.sleep(0.2)

    threading.Thread(target=poll, daemon=True).start()
    started_utc = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()
    out, _ = proc.communicate(timeout=24 * 3600)
    wall = time.monotonic() - t0
    (RUN_DIR / "run.log").write_text(out, encoding="utf-8", errors="replace")
    rec = {"exit": proc.returncode, "wall_s": round(wall, 1), "peak_rss_mb": round(peak["mb"], 1)}
    write_run_manifest_best_effort(
        RUN_DIR, peak_rss_mb=rec["peak_rss_mb"], started_utc=started_utc,
        warnings=["运行后清单；verify 完成后更新质量门。"],
    )
    print(json.dumps(rec), "（日志 run.log）")
    return rec


def _read_asc(p: Path):
    with open(p) as f:
        hdr = [next(f) for _ in range(6)]
    return np.loadtxt(p, skiprows=6), {l.split()[0]: float(l.split()[1]) for l in hdr}


def verify() -> None:
    res = RUN_DIR / "results"
    rmax, hdr = _read_asc(res / "res.max")
    cell = hdr["cellsize"]
    xll, yll = hdr["xllcorner"], hdr["yllcorner"]
    tgt = rasterio.Affine(cell, 0, xll, 0, -cell, yll + rmax.shape[0] * cell)

    # 锚点 WSE 序列（诊断，G7）
    tf = Transformer.from_crs(4326, 32649, always_xy=True)
    ax, ay = tf.transform(ANCHOR[1], ANCHOR[2])
    col = int((ax - xll) / cell)
    row = int((yll + rmax.shape[0] * cell - ay) / cell)
    dem_arr, _ = _read_asc(RUN_DIR / "dem.asc")
    wet_thr = 0.05
    wse_series = []
    for wd_p in sorted(res.glob("res-*.wd")):
        idx = int(wd_p.stem.split("-")[1])
        wd, _ = _read_asc(wd_p)
        r0, r1 = max(0, row - 3), min(rmax.shape[0], row + 4)
        c0, c1 = max(0, col - 3), min(rmax.shape[1], col + 4)
        wd_sub = wd[r0:r1, c0:c1]
        dem_sub = dem_arr[r0:r1, c0:c1]
        wet = wd_sub > wet_thr
        wse_series.append({
            "t_h": round(idx * 6.0, 1),
            "wse_center_m": round(float(dem_arr[row, col] + wd[row, col]), 2)
            if bool(wet[row - r0, col - c0]) else None,
            "wse_wet_max_m": round(float((dem_sub[wet] + wd_sub[wet]).max()), 2)
            if bool(wet.any()) else None,
            "wet_cells": int(wet.sum()),
        })

    # S1 09-12 洪峰景对比（t=228h=09-12T12 最近景 10:42）
    wd_f, _ = _read_asc(res / "res-0038.wd") if (res / "res-0038.wd").exists() else (None, None)
    s1_res = None
    if wd_f is not None:
        sim_wet = wd_f > 0.05
        obs = np.zeros(rmax.shape, dtype="uint8")
        cover = np.zeros(rmax.shape, dtype="uint8")
        with rasterio.open(S1W_FLD) as ds:
            reproject(ds.read(1), obs, src_transform=ds.transform, src_crs=ds.crs,
                      dst_transform=tgt, dst_crs="EPSG:32649", resampling=Resampling.nearest)
            reproject((ds.read(1) != 255).astype("uint8"), cover,
                      src_transform=ds.transform, src_crs=ds.crs,
                      dst_transform=tgt, dst_crs="EPSG:32649", resampling=Resampling.nearest)
        obsb = (obs == 1) & (cover == 1)
        valid = (cover == 1)  # 足迹内才算有效（足迹外无观测，不计假警报）
        H = int((sim_wet & obsb).sum())
        FA = int((sim_wet & ~obsb & valid).sum())
        M = int((~sim_wet & obsb).sum())
        s1_res = {
            "t_model_h": 228.0, "scene": "2024-09-12T10:42",
            "csi": round(H / max(H + FA + M, 1), 3), "pod": round(H / max(H + M, 1), 3),
            "far": round(FA / max(H + FA, 1), 3),
            "area_sim_km2": round(sim_wet.sum() * cell * cell / 1e6, 2),
            "area_sim_in_footprint_km2": round((sim_wet & valid).sum() * cell * cell / 1e6, 2),
            "area_obs_km2_in_footprint": round(obsb.sum() * cell * cell / 1e6, 2),
            "valid_footprint_km2": round(valid.sum() * cell * cell / 1e6, 2),
            "footprint_frac_of_domain": round(float(valid.mean()), 3),
            "note": "S1 固定阈值-17dB，风浪/淹没植被偏保守（G17）；仅足迹内比对；面积比较须用 area_sim_in_footprint",
        }
    mass_lines = (res / "res.mass").read_text().splitlines()
    keys = mass_lines[0].split()
    rows = [{k: float(v) for k, v in zip(keys, ln.split())} for ln in mass_lines[1:] if ln.split()]
    v_in = sum(0.5 * (a["Qin"] + bb["Qin"]) * (bb["Time"] - a["Time"]) for a, bb in zip(rows, rows[1:]))
    v_out = sum(0.5 * (a["Qout"] + bb["Qout"]) * (bb["Time"] - a["Time"]) for a, bb in zip(rows, rows[1:]))
    v_rain = rows[-1]["Rain-(Inf+Evap)"]
    resid = rows[-1]["Vol"] - rows[0]["Vol"] - v_rain - (v_in - v_out)
    e_mass = abs(resid) / max(rows[0]["Vol"] + v_rain + v_in, 1.0)
    wet_peak_vals = [x["wse_wet_max_m"] for x in wse_series if x["wse_wet_max_m"] is not None]
    wse_peak = max(wet_peak_vals, default=None)
    peak_row = max((x for x in wse_series if x["wse_wet_max_m"] is not None),
                   key=lambda x: x["wse_wet_max_m"], default=None)
    def at_time(t_h: float):
        return next((x for x in wse_series if abs(x["t_h"] - t_h) < 1e-6), None)

    def gauge_rec(t_h: float, gauge_m: float):
        row = at_time(t_h)
        model_wse = row["wse_wet_max_m"] if row else None
        return {
            "t_h": t_h, "gauge_m": gauge_m, "model_wet_wse_m": model_wse,
            "diff_gauge_minus_model_m": round(gauge_m - model_wse, 2)
            if model_wse is not None else None,
        }

    gauge_0912 = gauge_rec(T_GAUGE_0912_H, GAUGE_0912_M)
    gauge_0914 = gauge_rec(T_GAUGE_0914_H, GAUGE_0914_M)
    report = {
        "run_id": RUN_DIR.name,
        "anchor": ANCHOR[0], "wse_peak_m": wse_peak,
        "wse_peak_time_h": peak_row["t_h"] if peak_row else None,
        "wse_gauge_context": {
            "0912T14": gauge_0912,
            "0914T05": gauge_0914,
            "caveat": "G7 垂直基准未确认；两个时点/三个 Q 情景的 apparent offset 不一致，"
                      "不得当作 datum_offset_m、率定值或同峰对齐依据",
            "diagnostic_only": True,
        },
        "s1_0912_check": s1_res,
        "e_mass_132": float(f"{e_mass:.3e}"),
        "v_in_integrated_m3": round(v_in, 1), "v_out_integrated_m3": round(v_out, 1),
        "v_rain_m3": round(v_rain, 1), "final_vol_m3": round(rows[-1]["Vol"], 1),
        "qout_final": rows[-1]["Qout"],
        "wse_series": wse_series,
        "note": "上游三角Q情景+ERA5均匀雨；情景级结论（ADR-0004）",
    }
    out = RUN_DIR / "verify.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_run_manifest_best_effort(
        RUN_DIR,
        mass_gate={"status": "PASS" if e_mass <= 1e-3 else "FAIL",
                   "e_mass": float(f"{e_mass:.3e}")},
        warnings=["WSE 为诊断口径；垂直基准未闭合。"],
    )
    print(json.dumps({k: v for k, v in report.items() if k != "wse_series"},
                     ensure_ascii=False, indent=2))


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("prep", "all"):
        prep()
    if cmd in ("run", "all"):
        rec = run()
        if rec["exit"] != 0 and cmd == "all":
            print("run 失败，跳过 verify（run.log 留痕）", flush=True)
            return
    if cmd in ("verify", "all"):
        verify()


if __name__ == "__main__":
    main()
