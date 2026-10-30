"""阶段3：久隆 2024-09 摩羯事件 四情景回算（雨驱动，无河道出流依赖）。

用法：uv --directory . run python scripts/stage3/jiulong_scenarios.py [all|prep <scen>|run <scen>]
情景（下游=南缘滨海段，G14 结论：FREE 须带坡度；潮位用 HFIX）：
  free   南缘 FREE 0.0005（基线排水）
  z0     南缘低单元 HFIX 0.0 m（平均海面）
  z05    HFIX +0.5 m（一般增水）
  z10    HFIX +1.0 m（强增水）
配置：200m 网格、09-05T00~09-10T00、ERA5 逐时均匀雨、fpfric 0.035、acceleration、dt=5s。
预处理：海面负高程钳为 0 m（RTC 类产品特征，登记）；DEM 12.5m 双线性重采样到 200m。
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
import requests
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))
from scripts.common.run_manifest import write_run_manifest_best_effort  # noqa: E402

LISFLOOD = PROJ / "models" / "lisflood-fp-8.1" / "LISFLOOD-FP-trunk" / "lisflood.exe"
DEM12 = PROJ / "data" / "interim" / "jiulong" / "dem_12p5m_32649.tif"
ERA5_DAILY = PROJ / "data" / "catalog" / "probes" / "jiulong_sep2024_era5.json"
ERA5_HOURLY_CACHE = PROJ / "data" / "catalog" / "probes" / "jiulong_sep2024_era5_hourly.json"
ERA5_HOURLY_EXT = PROJ / "data" / "catalog" / "probes" / "jiulong_sep2024_era5_hourly_ext.json"

AOI = (108.55, 21.92, 108.90, 22.20)
# Phase B1：网格/时间步敏感性（终稿 §11 第 7 项）。默认值=首轮基线 200 m / dt 5 s。
RES = float(sys.argv[sys.argv.index("--res") + 1]) if "--res" in sys.argv else 200.0
SIM_DAYS = float(sys.argv[sys.argv.index("--sim-days") + 1]) if "--sim-days" in sys.argv else 5.0
SIM_SECONDS = SIM_DAYS * 86400  # 默认 09-05T00 ~ 09-10T00；--sim-days 13 至 09-18T00（退水段）
DT = float(sys.argv[sys.argv.index("--dt") + 1]) if "--dt" in sys.argv else 5.0
# Phase B4：输出间隔可覆盖（默认=单快照；6h=21600 使产品可算到达/历时）
SAVEINT = float(sys.argv[sys.argv.index("--saveint") + 1]) if "--saveint" in sys.argv else SIM_SECONDS
ANCHOR = ("久隆镇/500kV站", 108.7161, 22.0638)
STATION_RADIUS_CELLS = 2  # 主口径：±2 格（200 m 网格约 400 m 见方），与产品包一致
SCENARIOS = {
    "free": ("FREE", None),
    "z0": ("HFIX", 0.0),
    "z05": ("HFIX", 0.5),
    "z10": ("HFIX", 1.0),
    "ztide": ("HVAR", None),  # 合成天文潮（钦州湾调和振幅，相位未定——包络有效）
}


RUN_TAG = os.environ.get("RUN_TAG", "").strip()


def run_dir(scen: str) -> Path:
    suffix = f"_{RUN_TAG}" if RUN_TAG else ""
    dt_tag = f"_dt{str(DT).replace('.', 'p')}" if abs(DT - 5.0) > 1e-9 else ""
    sv_tag = f"_sv{SAVEINT / 3600:.0f}h" if abs(SAVEINT - SIM_SECONDS) > 1e-9 else ""
    return PROJ / "outputs" / "jiulong" / "2024-09-yagi" / (
        f"scen_{scen}_{int(RES)}m{dt_tag}{sv_tag}{suffix}")


def fetch_hourly() -> dict[str, float]:
    """ERA5 逐时（久隆锚点，09-05~09-10；--sim-days>5 时并 09-10~09-17 延长段），失败回退日均分布。"""
    if ERA5_HOURLY_CACHE.exists():
        hourly = dict(json.loads(ERA5_HOURLY_CACHE.read_text(encoding="utf-8"))["hourly_mm"])
    else:
        hourly = {}
        for s, e in (("2024-09-05", "2024-09-07"), ("2024-09-08", "2024-09-09")):
            for attempt in range(3):
                try:
                    r = requests.get("https://archive-api.open-meteo.com/v1/archive", params={
                        "latitude": 22.0638, "longitude": 108.7161,
                        "start_date": s, "end_date": e, "hourly": "precipitation",
                        "timezone": "Asia/Shanghai"}, timeout=(10, 90))
                    r.raise_for_status()
                    d = r.json()
                    for t, v in zip(d["hourly"]["time"], d["hourly"]["precipitation"]):
                        hourly[t] = v or 0.0
                    break
                except Exception:
                    time.sleep(5)
        if hourly:
            ERA5_HOURLY_CACHE.write_text(
                json.dumps({"hourly_mm": hourly}, ensure_ascii=False), encoding="utf-8")
    if SIM_DAYS > 5.0:  # 退水段延长雨（独立缓存文件，不触碰已冻结基线雨缓存）
        ext: dict[str, float] = {}
        if ERA5_HOURLY_EXT.exists():
            ext = json.loads(ERA5_HOURLY_EXT.read_text(encoding="utf-8"))["hourly_mm"]
        else:
            for s, e in (("2024-09-10", "2024-09-14"), ("2024-09-15", "2024-09-17")):
                for attempt in range(3):
                    try:
                        r = requests.get("https://archive-api.open-meteo.com/v1/archive", params={
                            "latitude": 22.0638, "longitude": 108.7161,
                            "start_date": s, "end_date": e, "hourly": "precipitation",
                            "timezone": "Asia/Shanghai"}, timeout=(10, 90))
                        r.raise_for_status()
                        d = r.json()
                        for t, v in zip(d["hourly"]["time"], d["hourly"]["precipitation"]):
                            ext[t] = v or 0.0
                        break
                    except Exception:
                        time.sleep(5)
            if ext:
                ERA5_HOURLY_EXT.write_text(
                    json.dumps({"hourly_mm": ext}, ensure_ascii=False), encoding="utf-8")
        hourly.update(ext)
    if hourly:
        return hourly
    daily = json.loads(ERA5_DAILY.read_text(encoding="utf-8"))["daily_mm"]
    out = {}
    for day, tot in daily.items():
        if "2024-09-05" <= day <= "2024-09-09":
            for h in range(24):
                out[f"{day}T{h:02d}:00"] = tot / 24.0
    return out


def prep(scen: str) -> None:
    kind, val = SCENARIOS[scen]
    d = run_dir(scen)
    try:
        from scripts.common.run_manifest import prepare_run_dir
        prepare_run_dir(d)
    except FileExistsError:
        raise SystemExit(f"{d} 已有 res.mass；终稿 §10 禁止覆盖。请设置 RUN_TAG 使用新目录。") from None
    tf = Transformer.from_crs(4326, 32649, always_xy=True)
    corners = [tf.transform(x, y) for x, y in [(AOI[0], AOI[1]), (AOI[2], AOI[3])]]
    b = (min(c[0] for c in corners), min(c[1] for c in corners),
         max(c[0] for c in corners), max(c[1] for c in corners))
    with rasterio.open(DEM12) as src:
        with WarpedVRT(src, crs="EPSG:32649", resampling=Resampling.bilinear,
                       width=int((b[2] - b[0]) / RES), height=int((b[3] - b[1]) / RES),
                       transform=rasterio.Affine(RES, 0, b[0], 0, -RES, b[3])) as v:
            dem = v.read(1).astype("float32")
    dem_pre_edit = dem.copy()  # 海面钳零/BFS 填补之前的地形（地形修改留痕用）
    # G16（2026-10-25 发现）：本域 DEM 负值绝大多数非水——S1 09-05 常水景（无风、事件前）
    # 证实全域仅 ~18.5km²(200m) 真水体，而 DEM 负值 319.5km²（中位 -6m，深达 -20m），
    # 属沿海平原垃圾值；首版「边缘连通负值=海」把 300km² 误判海面并钳 0，地形尽毁（v1 作废）。
    # v2 修复规则（S1 约束）：
    #   1) S1 常水水体（20m 水体占比>0.2 聚合到 200m）为唯一可信水面；
    #   2) 其中与域边连通者 = 海/海湾 -> 钳 0 m；
    #   3) 其余负值/洞（含非 S1 水的负值、S1 水面处的垃圾负值）-> BFS 最近正地形扩散填充；
    #   4) 河/塘不另刻槽（200m 下钦江为亚像元，登记限制）。
    from rasterio.warp import reproject as _rpw
    S1W = PROJ / "data" / "processed" / "s1_water" / "jiulong_20240905T105022_water20m.tif"
    with rasterio.open(S1W) as ds:
        frac = np.zeros(dem.shape, dtype="float32")
        _rpw((ds.read(1) == 1).astype("float32"), frac,
             src_transform=ds.transform, src_crs=ds.crs,
             dst_transform=rasterio.Affine(RES, 0, b[0], 0, -RES, b[3]),
             dst_crs="EPSG:32649", resampling=Resampling.average)
    water = frac > 0.2
    h, w = dem.shape
    sea = np.zeros_like(water)
    sea[0, :] |= water[0, :]
    sea[-1, :] |= water[-1, :]
    sea[:, 0] |= water[:, 0]
    sea[:, -1] |= water[:, -1]
    for _ in range(max(h, w) * 2):
        grown = sea.copy()
        grown[1:, :] |= sea[:-1, :]
        grown[:-1, :] |= sea[1:, :]
        grown[:, 1:] |= sea[:, :-1]
        grown[:, :-1] |= sea[:, 1:]
        grown &= water
        if (grown == sea).all():
            break
        sea = grown
    dem[sea] = 0.0
    np.save(d / "sea_mask.npy", sea)  # 验证统计时剔除海面像元

    bad = ((dem < 0) | (dem == -9999)) & ~sea  # 待填充负值/洞
    n_bad_init = int(bad.sum())

    def _nb8_sum(a: np.ndarray) -> np.ndarray:
        p = np.pad(a.astype("float64"), 1, mode="constant")
        s = np.zeros(a.shape, dtype="float64")
        for dr in range(3):
            for dc in range(3):
                if dr == 1 and dc == 1:
                    continue
                s += p[dr:dr + a.shape[0], dc:dc + a.shape[1]]
        return s

    vals = np.where(~bad, dem.astype("float64"), 0.0)
    known = ~bad
    n_fill = 0
    for _ in range(max(h, w) * 2):
        if not bad.any():
            break
        s_v = _nb8_sum(np.where(known, vals, 0.0))
        s_w = _nb8_sum(known.astype("float64"))
        newly = bad & (s_w > 0)
        if not newly.any():
            break  # 孤岛（不应出现）
        vals[newly] = s_v[newly] / s_w[newly]
        known |= newly
        bad &= ~newly
    dem = vals.astype("float32")
    dem[sea] = 0.0
    # 地形修改留痕：海面钳零与 BFS 填补都算改写（可视化「地形修改证据」层与审计读取）
    edit_mask = np.abs(dem - dem_pre_edit) > 1e-6
    if edit_mask.any():
        np.savetxt(d / "dem_pre_carve.diff.txt",
                   np.where(edit_mask, dem - dem_pre_edit, 0.0),
                   fmt="%.2f", delimiter="\t",
                   header=f"ncols {w}\nnrows {h}\nxllcorner {b[0]:.1f}\nyllcorner {b[1]:.1f}\n"
                          f"cellsize {RES:.1f}\nNODATA_value -9999.0", comments="")
        base = {"driver": "GTiff", "height": h, "width": w, "count": 1,
                "crs": "EPSG:32649",
                "transform": rasterio.Affine(RES, 0, b[0], 0, -RES, b[3]), "compress": "lzw"}
        with rasterio.open(d / "dem_edit_mask.tif", "w", dtype="uint8", **base) as dst:
            dst.write(edit_mask.astype("uint8"), 1)
        with rasterio.open(d / "dem_edit_diff.tif", "w", dtype="float32",
                           nodata=0.0, **base) as dst:
            dst.write(np.where(edit_mask, dem - dem_pre_edit, 0.0).astype("float32"), 1)
    np.savetxt(d / "dem.asc", dem, fmt="%.3f", delimiter="\t",
               header=f"ncols         {w}\nnrows         {h}\nxllcorner     {b[0]:.1f}\n"
                      f"yllcorner     {b[1]:.1f}\ncellsize      {RES:.1f}\nNODATA_value  -9999.0",
               comments="")
    # 南缘下游边界。G14 真根因：bci 边界区间是地图坐标（米）非单元序号（input.cpp 实测），
    # 修复：超大坐标（钳位=全边段）；HFIX 同理。FREE 带显式坡度（无坡度时仅用边缘局部水面坡度）
    srow = h - 1
    low_cols = np.where(dem[srow - 2:srow + 1, :].min(axis=0) <= 0.5)[0]  # 仅统计记录
    BIG = 99999999
    bdy_line = ""
    if kind == "HFIX":
        seg = [f"S\t-{BIG}\t{BIG}\tHFIX\t{val}"]
    elif kind == "HVAR":
        # 合成天文潮（HVAR 时变水位）。振幅出处：钦州湾验潮站 2008-2020 调和分析
        # （陈旭等 2024《广西科学》：O1 0.96~1.04m、K1 0.88~0.95m、M2 0.40~0.46m，
        #  全日潮为主）；S2 按 M2 的 0.42 倍取 0.18m。迟角 g 无公开值 -> 置 0：
        # 相位不定、仅振幅/包络有效（登记）。序列减窗均值 -> 围绕 MSL(=DEM 0) 振荡。
        comps = ((1.00, 25.82), (0.92, 23.93), (0.43, 12.42), (0.18, 12.0))  # (振幅m, 周期h)
        ts_h = np.arange(0.0, SIM_SECONDS / 3600.0 + 1.0, 1.0)
        tide = sum(A * np.cos(2 * np.pi * ts_h / T) for A, T in comps)
        tide = tide - tide.mean()
        (d / "tide.bdy").write_text(
            "# synthetic astronomical tide, Qinzhou Bay gauge amplitudes, phases=0 (envelope only)\n"
            f"qztide\n{len(ts_h)} seconds\n" +
            "".join(f"{v:.3f}\t{t * 3600:.0f}\n" for v, t in zip(tide, ts_h)),
            encoding="ascii", newline="\n")
        bdy_line = "bdyfile\ttide.bdy\n"
        seg = [f"S\t-{BIG}\t{BIG}\tHVAR\tqztide"]
        print(f"合成天文潮: {len(ts_h)} 时点, 范围 {tide.min():.2f}~{tide.max():.2f} m (MSL 基准)")
    else:
        seg = [f"S\t-{BIG}\t{BIG}\tFREE 0.0005"]
    (d / "bci.bci").write_text(
        f"N\t-{BIG}\t{BIG}\tFREE\n"
        f"W\t-{BIG}\t{BIG}\tFREE\n"
        f"E\t-{BIG}\t{BIG}\tFREE\n" +
        "\n".join(seg) + "\n", encoding="ascii", newline="\n")
    # 雨：逐时 -> (t秒, mm/h)
    hourly = fetch_hourly()
    pts = [(0.0, 0.0)]
    for tkey in sorted(hourly):
        day, hh = tkey.split("T")
        t = (int(day[-2:]) - 5) * 86400 + int(hh[:2]) * 3600
        if 0 < t <= SIM_SECONDS:
            pts.append((hourly[tkey], float(t)))
    pts.append((0.0, SIM_SECONDS))
    (d / "rain.rain").write_text(
        "# ERA5 jiulong anchor hourly uniform rain (degraded)\n"
        f"{len(pts)} seconds\n" +
        "".join(f"{v:.4f}\t{t:.0f}\n" for v, t in pts), encoding="ascii", newline="\n")
    (d / "run.par").write_text(
        f"DEMfile\tdem.asc\nbcifile\tbci.bci\n{bdy_line}rainfall\train.rain\n"
        f"resroot\tres\ndirroot\tresults\nsim_time\t{SIM_SECONDS:.0f}\n"
        f"initial_tstep\t{DT}\nmassint\t3600\nsaveint\t{SAVEINT:.0f}\n"
        f"fpfric\t0.035\nacceleration\n", encoding="ascii", newline="\n")
    meta = {"scenario": scen, "res": RES, "dt": DT, "sim_seconds": SIM_SECONDS,
            "saveint": SAVEINT,
            "dem_v2": "S1 常水水体约束修复（G16）：海=边缘连通 S1 水体钳0；其余负值/洞 BFS 填充",
            "sea_px": int(sea.sum()), "s1_water_px": int(water.sum()),
            "filled_px": n_bad_init, "unfilled_px": int(bad.sum()),
            "s_low_cols": [int(low_cols.min()), int(low_cols.max())] if kind == "HFIX" and low_cols.size else None,
            "note": "雨驱动情景级；潮位情景为常数位面（增水带），天文潮时变待引入 TPXO/FES；海面像元已从淹没统计剔除；河槽未刻蚀（钦江亚像元，登记）",
            "generated_utc": datetime.now(timezone.utc).isoformat()}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"prep[{scen}] 完成：S1水 {int(water.sum())} 格、海 {int(sea.sum())} 格、填充 {n_bad_init} 格、残留 {int(bad.sum())}，南缘低列", meta["s_low_cols"])


def run(scen: str) -> dict:
    import psutil
    d = run_dir(scen)
    proc = subprocess.Popen([str(LISFLOOD), "run.par"], cwd=d,
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
    (d / "run.log").write_text(out, encoding="utf-8", errors="replace")
    rec = {"exit": proc.returncode, "wall_s": round(time.monotonic() - t0, 1),
           "peak_rss_mb": round(peak["mb"], 1)}
    write_run_manifest_best_effort(
        d, peak_rss_mb=rec["peak_rss_mb"], started_utc=started_utc,
        warnings=["运行后清单；verify 完成后更新质量门。"],
    )
    return rec


def verify(scen: str) -> dict:
    d = run_dir(scen)
    res = d / "results"
    with open(res / "res.max") as f:
        [next(f) for _ in range(6)]
        rmax = np.loadtxt(f)
    sea = np.load(d / "sea_mask.npy") if (d / "sea_mask.npy").exists() else np.zeros_like(rmax, dtype=bool)
    wet = (rmax > 0.05) & ~sea  # 剔除海面像元
    area = wet.sum() * RES * RES / 1e6
    tf = Transformer.from_crs(4326, 32649, always_xy=True)
    ax, ay = tf.transform(ANCHOR[1], ANCHOR[2])
    hdr = {}
    with open(res / "res.max") as f:
        for _ in range(6):
            k, *v = next(f).split()
            hdr[k.lower()] = float(v[0])
    col = int((ax - hdr["xllcorner"]) / RES)
    row = int((hdr["yllcorner"] + rmax.shape[0] * RES - ay) / RES)
    def max_in_radius(rad: int) -> tuple[float, tuple[int, int]]:
        r0, r1 = max(0, row - rad), min(rmax.shape[0], row + rad + 1)
        c0, c1 = max(0, col - rad), min(rmax.shape[1], col + rad + 1)
        sub = rmax[r0:r1, c0:c1]
        ri, ci = np.unravel_index(np.argmax(sub), sub.shape)
        return float(sub[ri, ci]), (int(r0 + ri - row), int(c0 + ci - col))

    center_depth = float(rmax[row, col])
    r2_max, r2_off = max_in_radius(STATION_RADIUS_CELLS)
    r5_max, r5_off = max_in_radius(5)
    r5_dist_km = float(np.hypot(*r5_off)) * RES / 1000.0
    anchor_max = r2_max  # 向后兼容：主口径改为 ±2 格
    mass = (res / "res.mass").read_text().splitlines()
    keys = mass[0].split()
    rows = [{k: float(v) for k, v in zip(keys, ln.split())} for ln in mass[1:] if ln.split()]
    v_in = sum(0.5 * (a["Qin"] + bb["Qin"]) * (bb["Time"] - a["Time"]) for a, bb in zip(rows, rows[1:]))
    v_out = sum(0.5 * (a["Qout"] + bb["Qout"]) * (bb["Time"] - a["Time"]) for a, bb in zip(rows, rows[1:]))
    v_rain = rows[-1]["Rain-(Inf+Evap)"]
    residual = abs(rows[-1]["Vol"] - rows[0]["Vol"] - v_rain - (v_in - v_out))
    report = {"scenario": scen, "wet_area_km2": round(area, 2),
              "anchor": ANCHOR[0],
              "anchor_max_depth_m": round(anchor_max, 2),
              "anchor_depth_center_m": round(center_depth, 3),
              "anchor_depth_r2_m": round(r2_max, 3),
              "anchor_depth_r5_m": round(r5_max, 3),
              "r2_max_offset_cells": [r2_off[0], r2_off[1]],
              "r5_max_offset_cells": [r5_off[0], r5_off[1]],
              "r5_distance_km": round(r5_dist_km, 2),
              "radius_note": "主口径=±2格（约400m见方，与产品包一致）；±5格值仅作邻域参考，不得称站址点水深",
              "final_vol_m3": round(rows[-1]["Vol"], 1),
              "qout_final": rows[-1]["Qout"],
              "e_mass_132": float(f"{residual / max(rows[0]['Vol'] + v_rain + v_in, 1):.3e}"),
              "utc": datetime.now(timezone.utc).isoformat()}
    (d / "verify.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_run_manifest_best_effort(
        d,
        mass_gate={"status": "PASS" if report["e_mass_132"] <= 1e-3 else "FAIL",
                   "e_mass": report["e_mass_132"]},
        warnings=["主验证口径：久隆情景结果；SAR 退水验证单独登记。"],
    )
    return report


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("prep",):
        prep(sys.argv[2])
    elif cmd in ("run", "verify"):
        scen = sys.argv[2]
        if cmd == "run":
            r = run(scen)
            print(json.dumps(r), flush=True)
        else:
            print(json.dumps(verify(scen), ensure_ascii=False), flush=True)
    elif cmd == "one":  # 单情景全流程（prep+run+verify），结果并入 summary json
        scen = sys.argv[2]
        prep(scen)
        r = run(scen)
        v = verify(scen) if r["exit"] == 0 else {"error": "run failed", **r}
        v.update(r)
        out = PROJ / "data" / "catalog" / "jiulong_scenarios_summary.json"
        summary = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
        summary[scen] = v
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(v, ensure_ascii=False), flush=True)
        print("->", out)
    elif cmd == "all":
        summary = {}
        for scen in SCENARIOS:
            prep(scen)
            r = run(scen)
            v = verify(scen) if r["exit"] == 0 else {"error": "run failed"}
            v.update(r)
            summary[scen] = v
            print(json.dumps(v, ensure_ascii=False), flush=True)
        out = PROJ / "data" / "catalog" / "jiulong_scenarios_summary.json"
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print("->", out)


if __name__ == "__main__":
    main()
