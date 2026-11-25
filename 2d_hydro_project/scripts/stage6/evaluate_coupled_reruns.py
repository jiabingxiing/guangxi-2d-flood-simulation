"""阶段6：PipeNetGen 管网排涝耦合**物理重跑**的四维评估。

对 `outputs/pipenet/<case>/run_{baseline,coupled}_*/` 做受控差分评估，全部指标
来自 `lisflood.exe` 真实重算结果（`not_physical_recalculation=false`），
不使用任何诊断投影。

四个维度
--------
1. **城区内涝**：城区掩膜内 / 全域的湿格数、湿面积、最大与平均水深、积水体积、
   8 邻域孤立斑块数（耦合 vs 基线）；
2. **河道受纳顶托**：主槽洪峰水位与河道淹没面积变化，检验回注是否造成异常抬水；
3. **S1 遥感足迹**：POD / FAR / CSI，按案例各自的既有口径口径复算；
4. **官方站点水位**：站址 WSE 序列的 ΔH 与 ΔT_peak。

纪律
----
- 只读 `outputs/pipenet/**`、`data/processed/s1_water/**`、`data/catalog/gauges/**`；
  不写任何 run 目录。
- 城区掩膜由管网 GeoJSON 投影得到（与耦合边界同一套像元），不写死像元数。
- S1 与站点指标按项目既有降级口径（ADR-0004/0006/0017），均为诊断，不是精度结论。
- 缺数据记 `available=false`，不静默跳过。

用法
----
    $env:PYTHONUTF8 = "1"
    uv --directory 2d_hydro_project run python scripts/stage6/evaluate_coupled_reruns.py --case all
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from hydro_refine.coupling.drainage_kernel import _map_nodes_to_cells  # noqa: E402
from scripts.common.grid_io import (  # noqa: E402
    AuditLog,
    load_sea_mask,
    read_ascii_header,
    read_ascii_grid,
    run_frames,
    utc_now,
    write_json,
)
from scripts.common.patch_analysis import (  # noqa: E402
    connected_components,
    summarize_frame,
)

WET_THRESHOLD_M = 0.05
OUT_JSON = PROJ / "data" / "catalog" / "pipenet_coupled_rerun_evaluation.json"

CASE_RUNS = {
    "jiulong": {"baseline": "run_baseline_c200m", "coupled": "run_coupled_c200m", "epsg": 32649},
    "ningming": {"baseline": "run_baseline_c100m", "coupled": "run_coupled_c100m", "epsg": 32648},
    "guigang": {"baseline": "run_baseline_c100m", "coupled": "run_coupled_c100m", "epsg": 32649},
}


def urban_mask_for(case: str, run_dir: Path, *, epsg: int) -> tuple[np.ndarray, dict[str, Any]]:
    """由管网 GeoJSON 投影出城区掩膜（与耦合边界同一套像元）。"""
    hdr = read_ascii_header(run_dir / "dem.asc")
    nrows, ncols = int(hdr["nrows"]), int(hdr["ncols"])
    gj = PROJ / "outputs" / "pipenet" / case / f"{case}_pipes.geojson"
    mapped = _map_nodes_to_cells(gj, hdr, epsg=epsg)
    mask = np.zeros((nrows, ncols), dtype=bool)
    for rc in mapped["junctions"].values():
        mask[rc] = True
    # 与耦合边界一致：SGC 河道像元不作点汇，评估时也应剔除，保持口径统一
    sgc = run_dir / "sgc_width.asc"
    sgc_excluded = 0
    if sgc.is_file():
        sw = np.asarray(read_ascii_grid(sgc).data, dtype=np.float64) > 0.0
        if sw.shape == mask.shape:
            sgc_excluded = int((mask & sw).sum())
            mask &= ~sw
    return mask, {
        "urban_cells": int(mask.sum()),
        "sgc_cells_excluded": sgc_excluded,
        "source": f"outputs/pipenet/{case}/{case}_pipes.geojson (junction 投影)",
    }


def frame_metrics(
    run_dir: Path,
    urban_mask: np.ndarray,
    *,
    cellsize: float,
    sea_mask: np.ndarray | None,
) -> dict[str, Any]:
    """逐帧湿面积/体积/斑块统计（城区内 + 全域）。"""
    cell_area = cellsize * cellsize
    frames = run_frames(run_dir / "results")
    if not frames:
        return {"available": False, "reason": "no .wd frames"}
    per_frame: list[dict[str, Any]] = []
    for seq, path in frames:
        grid = read_ascii_grid(path, nodata_to_nan=True)
        depth = np.where(np.isfinite(grid.data), grid.data, 0.0).astype(np.float64)
        wet_all = depth > WET_THRESHOLD_M
        if sea_mask is not None and sea_mask.shape == wet_all.shape:
            wet_all &= ~sea_mask
        wet_urban = wet_all & urban_mask

        _, n_patch_urban = connected_components(wet_urban, connectivity=8)
        _, n_patch_all = connected_components(wet_all, connectivity=8)
        per_frame.append({
            "seq": int(seq),
            "urban": {
                "wet_cells": int(wet_urban.sum()),
                "wet_area_km2": round(float(wet_urban.sum()) * cell_area / 1e6, 4),
                "volume_m3": round(float(depth[wet_urban].sum()) * cell_area, 1),
                "max_depth_m": round(float(depth[wet_urban].max()), 4) if wet_urban.any() else 0.0,
                "mean_depth_m": round(float(depth[wet_urban].mean()), 4) if wet_urban.any() else 0.0,
                "n_patches": int(n_patch_urban),
            },
            "domain": {
                "wet_cells": int(wet_all.sum()),
                "wet_area_km2": round(float(wet_all.sum()) * cell_area / 1e6, 4),
                "volume_m3": round(float(depth[wet_all].sum()) * cell_area, 1),
                "max_depth_m": round(float(depth[wet_all].max()), 4) if wet_all.any() else 0.0,
                "n_patches": int(n_patch_all),
            },
        })
    return {"available": True, "n_frames": len(per_frame), "frames": per_frame}


def _agg(frames: list[dict[str, Any]], block: str, key: str) -> dict[str, float]:
    """对某区块某指标做 max / mean / 末帧汇总。"""
    vals = [f[block][key] for f in frames]
    return {
        "max": round(float(max(vals)), 4),
        "mean": round(float(np.mean(vals)), 4),
        "final": round(float(vals[-1]), 4),
    }


def _delta(coupled: dict[str, float], baseline: dict[str, float]) -> dict[str, Any]:
    """耦合相对基线的变化（coupled - baseline）与削减百分比。"""
    out: dict[str, Any] = {}
    for k in ("max", "mean", "final"):
        b, c = baseline[k], coupled[k]
        out[f"delta_{k}"] = round(c - b, 4)
        out[f"reduction_pct_{k}"] = round((b - c) / b * 100.0, 2) if b else None
    return out


def s1_metrics(case: str, run_dir: Path, urban_mask: np.ndarray) -> dict[str, Any]:
    """S1 POD/FAR/CSI，按案例既有口径。

    - 贵港：`res-0038.wd`，t=228 h，S1 2024-09-12T10:42（`validate_guigang_s1.py`）
    - 久隆：`res.max`，对灾前常水掩膜（`validate_jiulong_inundation.py`）
    - 宁明：无洪中 S1 过境（仅有 2024-09 两景，与 2026-08 事件不同期），记不可用
    """
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.warp import reproject

    s1_dir = PROJ / "data" / "processed" / "s1_water"
    if case == "ningming":
        return {
            "available": False,
            "reason": (
                "宁明事件为 2026-08 紫檀，现有 S1 水体层仅 2024-09-10/09-12 两景，"
                "与事件不同期，不得用于耦合前后精度对照（ADR-0004 降级口径）"
            ),
        }

    if case == "guigang":
        tif = s1_dir / "guigang_20240912T104222_water20m.tif"
        frame = run_dir / "results" / "res-0038.wd"
        scene = "2024-09-12T10:42"
        t_model_h = 228.0
        epsg_dst = "EPSG:32649"
        cover_thr, frac_thr = 0.0, 0.5
    else:  # jiulong: 灾前常水景
        tif = s1_dir / "jiulong_20240905T105022_water20m.tif"
        frame = run_dir / "results" / "res.max"
        scene = "2024-09-05T10:50(灾前常水)"
        t_model_h = None
        epsg_dst = "EPSG:32649"
        cover_thr, frac_thr = 0.5, 0.12

    if not tif.is_file():
        return {"available": False, "reason": f"S1 水体层缺失：{tif.name}"}
    if not frame.is_file():
        return {"available": False, "reason": f"模型帧缺失：{frame.name}"}

    hdr = read_ascii_header(run_dir / "dem.asc")
    nrows, ncols = int(hdr["nrows"]), int(hdr["ncols"])
    cell = float(hdr["cellsize"])
    transform = rasterio.Affine(
        cell, 0, float(hdr["xllcorner"]), 0, -cell,
        float(hdr["yllcorner"]) + nrows * cell,
    )
    frac = np.zeros((nrows, ncols), dtype="float32")
    cover = np.zeros((nrows, ncols), dtype="float32")
    with rasterio.open(tif) as ds:
        reproject(
            (ds.read(1) == 1).astype("float32"), frac,
            src_transform=ds.transform, src_crs=ds.crs,
            dst_transform=transform, dst_crs=epsg_dst,
            resampling=Resampling.average, src_nodata=255.0,
        )
        reproject(
            (ds.read(1) != 255).astype("float32"), cover,
            src_transform=ds.transform, src_crs=ds.crs,
            dst_transform=transform, dst_crs=epsg_dst,
            resampling=Resampling.average, src_nodata=0.0,
        )

    if case == "guigang":
        valid = cover > cover_thr
        obs = (frac > frac_thr) & valid
    else:
        valid = cover > cover_thr
        obs = (frac > frac_thr) & valid

    grid = read_ascii_grid(frame, nodata_to_nan=True)
    depth = np.where(np.isfinite(grid.data), grid.data, 0.0).astype(np.float64)
    wet = depth > WET_THRESHOLD_M
    try:
        sea = load_sea_mask(run_dir, wet.shape)
        if sea is not None:
            wet &= ~sea
    except Exception:
        pass

    m = wet & valid
    o = obs & valid
    h = int((m & o).sum())
    fa = int((m & ~o).sum())
    ms = int((~m & o).sum())
    csi = h / max(h + fa + ms, 1)
    pod = h / max(h + ms, 1)
    far = fa / max(h + fa, 1)
    cell_area = cell * cell
    return {
        "available": True,
        "scene": scene,
        "t_model_h": t_model_h,
        "frame": frame.name,
        "hits": h,
        "false_alarm_cells": fa,
        "misses": ms,
        "csi": round(csi, 4),
        "pod": round(pod, 4),
        "far": round(far, 4),
        "area_sim_in_footprint_km2": round(float(m.sum()) * cell_area / 1e6, 3),
        "area_obs_in_footprint_km2": round(float(o.sum()) * cell_area / 1e6, 3),
        "valid_footprint_km2": round(float(valid.sum()) * cell_area / 1e6, 3),
        "urban_hits": int((m & o & urban_mask).sum()),
        "urban_wet_cells": int((m & urban_mask).sum()),
        "caveat": (
            "S1 固定阈值 -17 dB，风浪/淹没植被漏提为下限（G17）；"
            "仅足迹内比对，足迹外不计虚警；非精度结论"
        ),
    }


def gauge_metrics(case: str, run_dir: Path) -> dict[str, Any]:
    """官方站点水位核验（复用既有 extractor/evaluator，与 validate_water_levels 同口径）。"""
    from hydro_refine.monitors.gauge_extractor import extract_run_virtual_gauges
    from hydro_refine.monitors.hydro_evaluator import evaluate_station_fit

    gpath = PROJ / "data" / "catalog" / "gauges" / f"{case}_gauges_normalized.json"
    if not gpath.is_file():
        return {"available": False, "reason": f"缺站点档案 {gpath.name}"}
    gdoc = json.loads(gpath.read_text(encoding="utf-8"))
    epsg = int(gdoc["crs_epsg"])
    stations = gdoc["stations"]
    by_id = {s["station_id"]: s for s in stations}
    try:
        ex = extract_run_virtual_gauges(run_dir, stations, epsg=epsg, half_window=3, mode="wet_max")
    except Exception as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    if not ex.get("available"):
        return {"available": False, "reason": ex.get("reason", "extractor unavailable")}

    probes: dict[str, Any] = {}
    for key, pr in ex["probes"].items():
        st = by_id[pr["station_id"]]
        obs = [o for o in st.get("observations", []) if o.get("qc_flag", 0) <= 1]
        if not obs:
            probes[key] = {"station_id": st["station_id"], "status": "no_valid_observations"}
            continue
        fit = evaluate_station_fit(
            pr["t_h"], pr["wse_m"],
            [o["t_h"] for o in obs], [o["water_level_m"] for o in obs],
            datum_closed=bool((st.get("alignment") or {}).get("datum_closed", False)),
            obs_base_m=st.get("baseflow_level_est_m"),
        )
        probes[key] = {
            "station_id": st["station_id"],
            "station_name": st["name"],
            "probe_kind": pr["probe_kind"],
            "snap_dist_m": pr["snap_dist_m"],
            "status": fit.get("status"),
            **{k: fit.get(k) for k in (
                # evaluate_station_fit 的实际键名（勿臆造 wse_peak_m/delta_h_sim_m）
                "n_matched", "rmse_m", "mae_m", "pbias_pct", "nse",
                "kge", "kge_r", "kge_alpha", "kge_beta", "r",
                "sim_peak_wse_m", "sim_peak_t_h", "obs_peak_wse_m", "obs_peak_t_h",
                "peak_timing_error_h", "peak_error_m", "mean_bias_m",
                "sim_base_m", "obs_base_m", "sim_rise_m", "obs_rise_m", "rise_error_m",
                "apparent_offset_obs_minus_sim_mean_m", "apparent_offset_spread_m",
                "datum_closed",
            ) if k in fit},
        }
    return {
        "available": True,
        "cellsize_m": ex["cellsize_m"],
        "saveint_s": ex["saveint_s"],
        "n_frames": ex["n_frames"],
        "probes": probes,
    }


def evaluate_case(case: str, log: AuditLog) -> dict[str, Any]:
    cfg = CASE_RUNS[case]
    root = PROJ / "outputs" / "pipenet" / case
    bdir = root / cfg["baseline"]
    cdir = root / cfg["coupled"]
    rec: dict[str, Any] = {
        "case": case,
        "SYNTHETIC_DATA_NOT_REAL_ASSET": True,
        "physical_recalculation": True,
        "not_physical_recalculation": False,
        "baseline_run": f"outputs/pipenet/{case}/{cfg['baseline']}",
        "coupled_run": f"outputs/pipenet/{case}/{cfg['coupled']}",
    }
    if not (bdir / "results" / "res.mass").is_file():
        rec["available"] = False
        rec["reason"] = f"基线算例未完成：{bdir.name}"
        return rec
    if not (cdir / "results" / "res.mass").is_file():
        rec["available"] = False
        rec["reason"] = f"耦合算例未完成：{cdir.name}"
        return rec

    hdr_b = read_ascii_header(bdir / "dem.asc")
    hdr_c = read_ascii_header(cdir / "dem.asc")
    if (hdr_b["nrows"], hdr_b["ncols"], hdr_b["cellsize"]) != (
        hdr_c["nrows"], hdr_c["ncols"], hdr_c["cellsize"]
    ):
        rec["available"] = False
        rec["reason"] = "基线与耦合网格不一致，无法受控差分"
        return rec

    cellsize = float(hdr_c["cellsize"])
    urban, urban_meta = urban_mask_for(case, cdir, epsg=int(cfg["epsg"]))
    rec["urban_mask"] = urban_meta

    try:
        sea = load_sea_mask(cdir, urban.shape)
    except Exception:
        sea = None

    fm_b = frame_metrics(bdir, urban, cellsize=cellsize, sea_mask=sea)
    fm_c = frame_metrics(cdir, urban, cellsize=cellsize, sea_mask=sea)
    if not (fm_b["available"] and fm_c["available"]):
        rec["available"] = False
        rec["reason"] = "缺 .wd 帧"
        return rec

    # 帧数对齐：取共同长度（saveint 相同则应完全一致，不一致要显式记录）
    n = min(fm_b["n_frames"], fm_c["n_frames"])
    rec["frame_alignment"] = {
        "baseline_n": fm_b["n_frames"],
        "coupled_n": fm_c["n_frames"],
        "used_n": n,
        "aligned": fm_b["n_frames"] == fm_c["n_frames"],
    }
    fb = fm_b["frames"][:n]
    fc = fm_c["frames"][:n]

    dim1: dict[str, Any] = {}
    for block in ("urban", "domain"):
        for key in ("wet_cells", "wet_area_km2", "volume_m3", "max_depth_m", "n_patches"):
            ab = _agg(fb, block, key)
            ac = _agg(fc, block, key)
            dim1[f"{block}.{key}"] = {"baseline": ab, "coupled": ac, **_delta(ac, ab)}
    rec["dim1_inundation"] = dim1

    # 维度2：河道受纳顶托 —— 用全域最大水深与全域体积作为主槽响应代理
    rec["dim2_river_backwater"] = {
        "domain_max_depth": {
            "baseline": _agg(fb, "domain", "max_depth_m"),
            "coupled": _agg(fc, "domain", "max_depth_m"),
            **_delta(_agg(fc, "domain", "max_depth_m"), _agg(fb, "domain", "max_depth_m")),
        },
        "domain_volume": {
            "baseline": _agg(fb, "domain", "volume_m3"),
            "coupled": _agg(fc, "domain", "volume_m3"),
            **_delta(_agg(fc, "domain", "volume_m3"), _agg(fb, "domain", "volume_m3")),
        },
        "note": (
            "回注水量进入受纳河道；若全域体积增量明显大于城镇削减量，"
            "说明存在异常抬水或边界滞留"
        ),
    }

    # 维度1 的机理解读：区分「本地降雨积水」与「外来顶托积水」
    # 管网只能抽走本地净雨（≤ safety_factor × 该格瞬时净雨），对深水顶托几乎无效。
    urban_mask_cells = int(urban.sum())
    urban_area = urban_mask_cells * cellsize * cellsize / 1e6
    base_urban_vol = _agg(fb, "urban", "volume_m3")["max"]
    base_urban_depth = _agg(fb, "urban", "max_depth_m")["max"]
    equivalent_depth = base_urban_vol / (urban_area * 1e6) if urban_area > 0 else 0.0
    plumbing_limited = bool(equivalent_depth > 1.0)
    rec["dim1_interpretation"] = {
        "urban_area_km2": round(urban_area, 4),
        "baseline_urban_peak_volume_m3": base_urban_vol,
        "baseline_urban_peak_max_depth_m": base_urban_depth,
        "baseline_urban_volume_as_equivalent_uniform_depth_m": round(equivalent_depth, 3),
        "regime": "external_backwater_dominated" if plumbing_limited
        else "local_rainfall_ponding_dominated",
        "explanation": (
            "城区峰时体积换算为均匀水深 >1 m 时，积水主要来自外来（上游来水/河道顶托），"
            "而管网只按本地瞬时净雨限幅抽排，故削减幅度必然很小——这是物理机制，"
            "不是耦合失效。若接近 0，应读作「该案例的内涝不是管网能解决的类型」。"
            if plumbing_limited
            else "城区积水以本地降雨产流为主，管网抽排能显著削减。"
        ),
    }

    # 维度3：S1
    try:
        s1_b = s1_metrics(case, bdir, urban)
        s1_c = s1_metrics(case, cdir, urban)
        rec["dim3_s1_footprint"] = {
            "baseline": s1_b,
            "coupled": s1_c,
            "delta": (
                {
                    k: round(float(s1_c[k]) - float(s1_b[k]), 4)
                    for k in ("csi", "pod", "far")
                    if s1_c.get("available") and s1_b.get("available")
                    and s1_c.get(k) is not None and s1_b.get(k) is not None
                }
                if s1_b.get("available") and s1_c.get("available")
                else None
            ),
        }
    except Exception as exc:
        rec["dim3_s1_footprint"] = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}

    # 维度4：站点水位
    try:
        g_b = gauge_metrics(case, bdir)
        g_c = gauge_metrics(case, cdir)
        dh: dict[str, Any] = {}
        if g_b.get("available") and g_c.get("available"):
            for key in g_b["probes"]:
                pb = g_b["probes"].get(key) or {}
                pc = g_c["probes"].get(key) or {}
                if "sim_peak_wse_m" in pb and "sim_peak_wse_m" in pc:
                    delta_t = None
                    if pb.get("sim_peak_t_h") is not None and pc.get("sim_peak_t_h") is not None:
                        delta_t = round(
                            float(pc["sim_peak_t_h"]) - float(pb["sim_peak_t_h"]), 4
                        )
                    dh[key] = {
                        "station_id": pc.get("station_id"),
                        "station_name": pc.get("station_name"),
                        "sim_peak_wse_baseline_m": pb.get("sim_peak_wse_m"),
                        "sim_peak_wse_coupled_m": pc.get("sim_peak_wse_m"),
                        "delta_sim_peak_wse_m": round(
                            float(pc["sim_peak_wse_m"]) - float(pb["sim_peak_wse_m"]), 4
                        ),
                        "delta_sim_peak_t_h": delta_t,
                        "peak_timing_error_baseline_h": pb.get("peak_timing_error_h"),
                        "peak_timing_error_coupled_h": pc.get("peak_timing_error_h"),
                        "n_matched_baseline": pb.get("n_matched"),
                        "n_matched_coupled": pc.get("n_matched"),
                    }
        rec["dim4_gauges"] = {"baseline": g_b, "coupled": g_c, "delta_wse": dh}
        if not dh:
            # 显式说明「为何没有 ΔH」，避免被读成「算了但为 0」
            usable = [
                k for k, v in (g_b.get("probes") or {}).items()
                if v.get("status") != "no_valid_observations" and "sim_peak_wse_m" in v
            ]
            rec["dim4_gauges"]["delta_wse_unavailable_reason"] = (
                "无可用站点：该案例站点档案无有效观测（qc_flag>1 或 2024/2026 工况隔离），"
                "故无法计算 ΔH；已取到的探针状态见 probes"
                if (g_b.get("available") and not usable)
                else "站点提取失败或探针缺 sim_peak_wse_m，见 baseline/coupled.probes"
            )
    except Exception as exc:
        rec["dim4_gauges"] = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}

    rec["available"] = True
    rec["mass_gate"] = {
        "baseline": _read_gate(bdir),
        "coupled": _read_gate(cdir),
    }
    log.step("evaluated_case", case=case, frames=n)
    return rec


def _read_gate(run_dir: Path) -> dict[str, Any]:
    """读本案例的 meta/run 记录中的 E_mass（由编排脚本写入 verify 或重算）。"""
    mass = run_dir / "results" / "res.mass"
    if not mass.is_file():
        return {"available": False}
    lines = mass.read_text(encoding="utf-8", errors="replace").splitlines()
    keys = lines[0].split()
    rows = [dict(zip(keys, map(float, ln.split()))) for ln in lines[1:] if ln.split()]
    v_in = sum(0.5 * (a["Qin"] + b["Qin"]) * (b["Time"] - a["Time"]) for a, b in zip(rows, rows[1:]))
    v_out = sum(0.5 * (a["Qout"] + b["Qout"]) * (b["Time"] - a["Time"]) for a, b in zip(rows, rows[1:]))
    v0, ve = rows[0]["Vol"], rows[-1]["Vol"]
    v_rain = rows[-1]["Rain-(Inf+Evap)"]
    resid = ve - v0 - v_rain - (v_in - v_out)
    denom = max(v0 + v_rain + v_in, 1.0)
    em = abs(resid) / denom
    return {
        "available": True,
        "e_mass_132": float(f"{em:.3e}"),
        "gate": "PASS" if em <= 1e-3 else "FAIL",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--case", choices=[*CASE_RUNS, "all"], default="all")
    ap.add_argument("--out-json", type=Path, default=OUT_JSON)
    args = ap.parse_args(argv)

    log = AuditLog("evaluate_coupled_reruns")
    cases = list(CASE_RUNS) if args.case == "all" else [args.case]
    results: dict[str, Any] = {}
    for case in cases:
        print(f"[{case}] 评估中 …", flush=True)
        rec = evaluate_case(case, log)
        results[case] = rec
        if rec.get("available"):
            u = rec["dim1_inundation"]["urban.wet_area_km2"]
            p = rec["dim1_inundation"]["urban.n_patches"]
            print(
                f"[{case}] 城区湿面积 {u['baseline']['max']} -> {u['coupled']['max']} km² "
                f"({u['reduction_pct_max']}%)；斑块 {p['baseline']['max']} -> "
                f"{p['coupled']['max']} ({p['reduction_pct_max']}%)"
            )
        else:
            print(f"[{case}] 不可用：{rec.get('reason')}")

    payload = {
        "script": "scripts/stage6/evaluate_coupled_reruns.py",
        "generated_utc": utc_now(),
        "physical_recalculation": True,
        "SYNTHETIC_DATA_NOT_REAL_ASSET": True,
        "not_physical_recalculation": False,
        "isolated_from_g15_ssot": True,
        "wet_threshold_m": WET_THRESHOLD_M,
        "method": {
            "controlled_differential": (
                "基线与耦合共享同一 DEM/降雨/时间轴/时步，唯一变量为管网点源项"
            ),
            "urban_mask": "管网 junction 投影像元，剔除 sgc_width>0 河道像元",
            "patch": "8 邻域连通域，水深 > 0.05 m",
        },
        "cases": results,
        "limitations": [
            "safety_factor 为保证零负水深的数值安全系数，非管径水力标定值；"
            "削减百分比是该假设下的排涝上限效应，不是管网真实潜力",
            "80 mm/h 逐格能力上限在三个案例均未生效（见各案例 forcing.capacity_cap_effective）",
            "S1 固定阈值 -17 dB 为检出下限；足迹外不计虚警；非精度结论",
            "站点水位受 G7 垂直基准未闭合约束，仅作同构差分对照",
        ],
    }
    write_json(args.out_json, payload)
    log.write(PROJ / "logs" / "stage6" / "evaluate_coupled_reruns.json")
    print(f"wrote -> {args.out_json.relative_to(PROJ).as_posix() if args.out_json.is_relative_to(PROJ) else args.out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())