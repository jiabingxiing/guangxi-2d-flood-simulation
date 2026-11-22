"""阶段6：贵港 G7 垂直基准 / S1 口径复核（只读，Phase H 第四轮）。

三项复核（ADR-0014 延续，结论登记不设门）：
1. apparent offset 全样本表——全部贵港 run 的锚点 WSE vs 站值（更多情景样本下
   检验 offset 是否趋稳；G7 结论只复核证据强度，不闭合基准）；
2. S1 阈值敏感性——sigma20m 在 -19/-17/-15 dB 下的观测水面积与各情景
   CSI/POD/FAR 重算（G17 固定阈值保守度量化；-17 与原始水体掩膜对齐校验）；
3. 常水景第二时点——09-05 常水观测 vs 模型充槽期（res-0010.wd，t=60 h）
   足迹内对比（河道位置独立验证，不依赖洪水时刻）。

用法：uv run python scripts/stage6/review_guigang_g7_s1.py
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
from scripts.common.grid_io import read_ascii_header, utc_now, write_json  # noqa: E402
from scripts.stage6.validate_guigang_s1 import (  # noqa: E402
    RUNS_ROOT,
    S1W_FLD,
    _read_asc,
    _s1_masks,
    SCENARIOS,
    WET_THRESHOLD_M,
)

S1W_PRE = PROJ / "data" / "processed" / "s1_water" / "guigang_20240905T105022_water20m.tif"
SIGMA_FLD = PROJ / "data" / "processed" / "s1_water" / "guigang_20240912T104222_sigma20m.tif"
OUT_JSON = PROJ / "data" / "catalog" / "guigang_g7_s1_review.json"
THRESHOLDS_DB = (-19.0, -18.0, -17.0, -16.0, -15.0)
PERM_FRAME = "res-0010.wd"  # t=60 h（09-05T12，最接近常水过境 09-05T10:42）


def _offset_table() -> list[dict]:
    rows = []
    for vj in sorted(RUNS_ROOT.glob("*/verify.json")):
        try:
            doc = json.loads(vj.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        ctx = doc.get("wse_gauge_context", {})
        r12, r14 = ctx.get("0912T14", {}), ctx.get("0914T05", {})
        if r12.get("model_wet_wse_m") is None:
            continue
        o12 = round(r12["gauge_m"] - r12["model_wet_wse_m"], 2) if r12.get("gauge_m") else None
        o14 = round(r14["gauge_m"] - r14["model_wet_wse_m"], 2) if r14.get("gauge_m") else None
        rows.append({"run_id": vj.parent.name, "model_wse_0912_m": r12.get("model_wet_wse_m"),
                     "offset_0912_m": o12, "offset_0914_m": o14})
    return rows


def _metrics(sim_wet: np.ndarray, obsb: np.ndarray, valid: np.ndarray, cell: float) -> dict:
    H = int((sim_wet & obsb).sum())
    FA = int((sim_wet & ~obsb & valid).sum())
    M = int((~sim_wet & obsb).sum())
    return {
        "hits": H, "false_alarm": FA, "misses": M,
        "csi": round(H / max(H + FA + M, 1), 3),
        "pod": round(H / max(H + M, 1), 3),
        "far": round(FA / max(H + FA, 1), 3),
        "area_obs_km2": round(obsb.sum() * cell * cell / 1e6, 2),
    }


def main() -> None:
    if not (S1W_FLD.is_file() and SIGMA_FLD.is_file() and S1W_PRE.is_file()):
        raise SystemExit("S1 产物缺失（water/sigma/pre）")
    ref_run = RUNS_ROOT / SCENARIOS[0][1]
    hdr = read_ascii_header(ref_run / "results" / "res.max")
    cell = float(hdr["cellsize"])
    nrows, ncols = int(hdr["nrows"]), int(hdr["ncols"])
    xll, yll = float(hdr["xllcorner"]), float(hdr["yllcorner"])
    transform = rasterio.Affine(cell, 0, xll, 0, -cell, yll + nrows * cell)

    payload: dict = {
        "script": "scripts/stage6/review_guigang_g7_s1.py",
        "generated_utc": utc_now(),
        "read_only": True,
        "caveats": [
            "G7：只复核证据强度，不闭合垂直基准；offset 非常数即不得当 datum_offset_m",
            "S1：全部为足迹内重叠度（覆盖约 20.6%）；阈值扫描只量化口径敏感性，主口径仍 -17 dB",
        ],
    }

    # ---- 1) offset 全样本 ----
    offs = _offset_table()
    o12 = [r["offset_0912_m"] for r in offs if r["offset_0912_m"] is not None]
    payload["g7_offset_table"] = {
        "n_runs": len(offs),
        "rows": offs,
        "offset_0912_spread": {"min": min(o12), "max": max(o12),
                               "spread": round(max(o12) - min(o12), 2)} if o12 else None,
        "note": "offset = 站值 − 模型锚点湿包络 WSE（G7 诊断口径）",
    }

    # ---- 2) S1 阈值敏感性 ----
    # sigma 重投影一次（nearest），阈值在模型空间施加；-17 与原始水体掩膜对齐校验
    with rasterio.open(SIGMA_FLD) as src:
        sig = np.full((nrows, ncols), np.nan, dtype="float32")
        reproject(src.read(1), sig, src_transform=src.transform, src_crs=src.crs,
                  dst_transform=transform, dst_crs="EPSG:32649",
                  resampling=Resampling.nearest)
    obsb_ref, valid = _s1_masks((nrows, ncols), transform, cell)
    sigma_obs = {}
    for thr in THRESHOLDS_DB:
        o = (sig < thr) & valid
        agree = float((o == obsb_ref).mean())
        sigma_obs[thr] = {"obs_cells": int(o.sum()),
                          "area_km2": round(o.sum() * cell * cell / 1e6, 2),
                          "agreement_with_ref_mask": round(agree, 4)}
    payload["s1_threshold_scan"] = {
        "thresholds_db": list(THRESHOLDS_DB),
        "obs_by_threshold": sigma_obs,
        "ref_mask_note": "obsb_ref = 原始 -17 dB 水体掩膜重投影（对齐校验基准）",
        "scenarios": [],
    }
    # 各情景 wd 一次读入，多阈值复算
    for tag, run_id, _mode in SCENARIOS:
        wd_path = RUNS_ROOT / run_id / "results" / "res-0038.wd"
        if not wd_path.is_file():
            continue
        wd = _read_asc(wd_path)
        if wd.shape != (nrows, ncols):
            continue
        sim_wet = wd > WET_THRESHOLD_M
        rows = {}
        for thr in THRESHOLDS_DB:
            obsb = (sig < thr) & valid
            rows[str(thr)] = _metrics(sim_wet, obsb, valid, cell)
        payload["s1_threshold_scan"]["scenarios"].append({"tag": tag, "metrics_by_threshold": rows})

    # ---- 3) 常水景第二时点（res-0010.wd，t=60 h vs 09-05 常水） ----
    obs_pre = np.zeros((nrows, ncols), dtype="uint8")
    cover_pre = np.zeros((nrows, ncols), dtype="uint8")
    with rasterio.open(S1W_PRE) as src:
        reproject(src.read(1), obs_pre, src_transform=src.transform, src_crs=src.crs,
                  dst_transform=transform, dst_crs="EPSG:32649", resampling=Resampling.nearest)
        reproject((src.read(1) != 255).astype("uint8"), cover_pre,
                  src_transform=src.transform, src_crs=src.crs,
                  dst_transform=transform, dst_crs="EPSG:32649", resampling=Resampling.nearest)
    obsb_pre = (obs_pre == 1) & (cover_pre == 1)
    valid_pre = cover_pre == 1
    perm_rows = []
    for tag, run_id, _mode in SCENARIOS:
        wd_path = RUNS_ROOT / run_id / "results" / PERM_FRAME
        if not wd_path.is_file():
            continue
        wd = _read_asc(wd_path)
        if wd.shape != (nrows, ncols):
            continue
        m = _metrics(wd > WET_THRESHOLD_M, obsb_pre, valid_pre, cell)
        m["tag"] = tag
        m["area_sim_km2"] = round(float((wd > WET_THRESHOLD_M).sum()) * cell * cell / 1e6, 2)
        perm_rows.append(m)
    payload["permanent_water_crosscheck"] = {
        "obs_scene": "2024-09-05T10:42（常水，灾前）",
        "model_frame": f"{PERM_FRAME}（t=60 h，充槽期）",
        "obs_cells": int(obsb_pre.sum()),
        "obs_area_km2": round(obsb_pre.sum() * cell * cell / 1e6, 2),
        "valid_footprint_km2": round(valid_pre.sum() * cell * cell / 1e6, 2),
        "scenarios": perm_rows,
        "note": "河道位置独立验证：模型充槽期河道内应湿，与常水观测在足迹内重叠",
    }

    write_json(OUT_JSON, payload)
    print(f"复核结果 -> {OUT_JSON}")
    print(f"offset 样本 {len(offs)} 个 run，0912 spread = "
          f"{payload['g7_offset_table']['offset_0912_spread']}")
    for thr, v in sigma_obs.items():
        print(f"  {thr} dB: obs {v['area_km2']} km2 (与 -17 掩膜一致率 {v['agreement_with_ref_mask']})")
    for r in perm_rows:
        print(f"  常水 {r['tag']}: CSI {r['csi']} POD {r['pod']} FAR {r['far']} 模拟 {r['area_sim_km2']} km2")


if __name__ == "__main__":
    main()
