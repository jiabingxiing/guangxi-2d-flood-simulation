"""Phase B4 收尾：久隆退水期 S1 弱验证（终稿 §3.2 降级口径）。

久隆 2024-09 摩羯洪峰期无 S1 过境（09-17 最早，退水后期）；本脚本将 free 情景
延长模拟至 09-18T00（scen_free_200m_sv6h_recession，ERA5 逐时含 09-10~09-17 延长段），
取最接近 S1 09-17T10:50:23 UTC（=北京时 18:50:23，t≈1,104,623 s）的 6h 快照帧
（res-0051 = t 1,101,600 s，早于过境 50 min，±6h 窗内），与 S1 水体掩膜（20m 聚合
到 200m、占比>0.2 记水体，与 DEM prep 同阈值）做像元对照。

弱验证限制（结论必须降级标注）：
  1) S1 为退水后期景，非洪峰；且 09-17 检出水 (8.00 km²) 低于事件前常水 (10.82 km²)，
     风浪/入射角使检出偏低（G17），S1 只能作下限参照；
  2) 模型无河渠/排水网（钦江亚像元未刻蚀、无排水网络），洼积退水只能经南缘 FREE 缓排，
     模型湿面积系统性偏大；
  3) 常水语义：S1 总水体含河/塘（模型未编码常水），对照记 CSI/POD/FAR 并附面积口径，
     不作为独立验证通过门。

输出：data/catalog/jiulong_recession_s1.json
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject

PROJ = Path(__file__).resolve().parents[2]
RUN = PROJ / "outputs" / "jiulong" / "2024-09-yagi" / "scen_free_200m_sv6h_recession"
W_DIR = PROJ / "data" / "processed" / "s1_water"
OUT = PROJ / "data" / "catalog" / "jiulong_recession_s1.json"
AOI = (108.55, 21.92, 108.90, 22.20)
SAVEINT = 21600.0
WET_THR = 0.05
S1_FRAC_THR = 0.2


def read_asc(path: Path) -> tuple[np.ndarray, dict]:
    with open(path) as f:
        hdr = {}
        for _ in range(6):
            k, *v = next(f).split()
            hdr[k.lower()] = float(v[0]) if v else None
        arr = np.loadtxt(f)
    return arr, hdr


def s1_to_grid(tif: Path, shape, transform) -> tuple[np.ndarray, np.ndarray]:
    """S1 20m 水体聚合到模型网格：返回 (水体占比, 有效率)。"""
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


def e_mass() -> float:
    lines = (RUN / "results" / "res.mass").read_text().splitlines()
    keys = lines[0].split()
    rows = [dict(zip(keys, map(float, ln.split()))) for ln in lines[1:] if ln.split()]
    v_in = sum(0.5 * (a["Qin"] + b["Qin"]) * (b["Time"] - a["Time"]) for a, b in zip(rows, rows[1:]))
    v_out = sum(0.5 * (a["Qout"] + b["Qout"]) * (b["Time"] - a["Time"]) for a, b in zip(rows, rows[1:]))
    resid = rows[-1]["Vol"] - rows[0]["Vol"] - rows[-1]["Rain-(Inf+Evap)"] - (v_in - v_out)
    return float(f"{abs(resid) / max(rows[0]['Vol'] + rows[-1]['Rain-(Inf+Evap)'] + v_in, 1.0):.3e}")


def frame_wet(idx: int, shape, sea) -> tuple[np.ndarray, float]:
    p = RUN / "results" / f"res-{idx:04d}.wd"
    wd, _ = read_asc(p)
    wet = (wd > WET_THR) & ~sea
    return wet, float(wet.sum()) * 200.0 * 200.0 / 1e6


def metrics(model_wet: np.ndarray, s1_wat: np.ndarray, valid: np.ndarray) -> dict:
    m = model_wet & valid
    o = s1_wat & valid
    h = int((m & o).sum())
    fa = int((m & ~o).sum())
    ms = int((~m & o).sum())
    csi = h / (h + fa + ms) if (h + fa + ms) else None
    pod = h / (h + ms) if (h + ms) else None
    far = fa / (h + fa) if (h + fa) else None
    return {"hit_cells": h, "false_alarm_cells": fa, "miss_cells": ms,
            "csi": round(csi, 3) if csi is not None else None,
            "pod": round(pod, 3) if pod is not None else None,
            "far": round(far, 3) if far is not None else None,
            "model_wet_km2": round(m.sum() * 0.04, 2),
            "s1_water_km2": round(o.sum() * 0.04, 2)}


def main() -> None:
    rmax, hdr = read_asc(RUN / "results" / "res.max")
    shape = rmax.shape
    transform = rasterio.Affine(200.0, 0, hdr["xllcorner"], 0, -200.0,
                                hdr["yllcorner"] + shape[0] * 200.0)
    sea = np.load(RUN / "sea_mask.npy") if (RUN / "sea_mask.npy").exists() else np.zeros(shape, bool)

    # S1 09-17 过境（10:50:23 UTC -> 北京时 18:50:23 -> t=1,104,623 s，12.785 d）
    t_target = (17 - 5) * 86400 + 18 * 3600 + 50 * 60 + 23
    idx_target = int(round(t_target / SAVEINT))
    frames = sorted(int(p.stem.split("-")[1]) for p in (RUN / "results").glob("res-*.wd"))
    idx = min(frames, key=lambda i: abs(i * SAVEINT - t_target))
    dt_off_h = (idx * SAVEINT - t_target) / 3600.0

    wet_51, area_51 = frame_wet(idx, shape, sea)
    idx_0910 = int(round(5 * 86400 / SAVEINT))  # 09-10T00（原 5 天窗末）
    wet_0910, area_0910 = frame_wet(idx_0910, shape, sea)

    frac17, cov17 = s1_to_grid(W_DIR / "jiulong_20240917T105023_water20m.tif", shape, transform)
    frac05, cov05 = s1_to_grid(W_DIR / "jiulong_20240905T105022_water20m.tif", shape, transform)
    valid = (cov17 > 0.5) & ~sea
    s1_w17 = (frac17 > S1_FRAC_THR) & valid
    s1_w05 = (frac05 > S1_FRAC_THR) & (cov05 > 0.5) & ~sea

    rep = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "run_dir": RUN.name,
        "s1_scene_utc": "20240917T105023",
        "frame_used": f"res-{idx:04d}.wd",
        "frame_time_offset_h_vs_overpass": round(dt_off_h, 2),
        "e_mass_extended": e_mass(),
        "model_wet_km2_0910T00": round(area_0910, 2),
        "model_wet_km2_at_overpass_frame": round(area_51, 2),
        "model_area_change_0910_to_overpass_km2": round(area_51 - area_0910, 2),
        "s1_water_km2_0905_prevent_in_aoi_nosea": round(s1_w05.sum() * 0.04, 2),
        "s1_water_km2_0917_in_aoi_nosea": round(s1_w17.sum() * 0.04, 2),
        "metrics_model_vs_s1_0917": metrics(wet_51, s1_w17, valid),
        "verdict": "WEAK（退水期单景、G17 下限、模型无排水网面积系统性偏大；仅作趋势/量级参照，不作独立验证通过门）",
        "caveats": [
            "S1 为退水后期景（洪峰无过境），结论降级（终稿 §3.2）",
            "S1 09-17 检出低于 09-05 常水（风浪/入射角/植被漏提，G17），S1 为下限",
            "模型常水（河/塘/海）未编码，对照为总水体口径；海面像元双侧剔除",
            "快照帧早于过境 50 min（±6h 窗内）",
            "延长雨段（09-10~09-17，84 mm）下模型面积不降反增（+27.7 km²）——"
            "无排水网洼积+FREE 缓排不足以退水，与 S1 实测退水方向相反，"
            "定量支持『湿面积系统性偏大、仅情景级』既有限制",
        ],
    }
    OUT.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    print("->", OUT)


if __name__ == "__main__":
    main()
