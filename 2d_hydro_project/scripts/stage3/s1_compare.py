"""S1 提取结果对照（终稿 8.1，降级模式）。

1) 贵港 09-12 vs 09-05 双景亮度归一化变化检测：
   Δ = median(σ2−σ1 | 双景均确为陆地 σ>−14dB)，剔除轨道/入射角整体亮度差；
   新增水（洪涝候选）= σ2−Δ<−17 且 σ1≥−17；失水 = σ1<−17 且 σ2−Δ≥−17。
   产出阶段5验证靶 guigang_flood_change.tif + 统计。
2) 久隆 09-05（事件前、无风）与 09-17 常水期水体 vs DEM 负值（河/海面）一致性。
3) 各景 AOI 内实际覆盖率（掩膜网格含外扩边框，须按 AOI 复核）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.warp import reproject

PROJ = Path(__file__).resolve().parents[2]
W_DIR = PROJ / "data" / "processed" / "s1_water"
OUT_JSON = PROJ / "data" / "catalog" / "s1_compare.json"
THR = -17.0


def aoi_window(ds, aoi, epsg):
    tf = Transformer.from_crs(4326, epsg, always_xy=True)
    pts = [tf.transform(x, y) for x, y in
           [(aoi[0], aoi[1]), (aoi[2], aoi[1]), (aoi[0], aoi[3]), (aoi[2], aoi[3])]]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    rows, cols = rasterio.transform.rowcol(ds.transform, [min(xs), max(xs)], [max(ys), min(ys)])
    return ((max(0, int(min(rows))), min(ds.height, int(max(rows)) + 1)),
            (max(0, int(min(cols))), min(ds.width, int(max(cols)) + 1)))


def coverage_and_water(tif: Path, aoi, epsg) -> dict:
    with rasterio.open(tif) as ds:
        (r0, r1), (c0, c1) = aoi_window(ds, aoi, epsg)
        win = ds.read(1, window=rasterio.windows.Window(c0, r0, c1 - c0, r1 - r0))
    valid = win != 255
    return {"cover_in_aoi": round(float(valid.mean()), 3),
            "water_km2_in_aoi": round(float((win == 1).sum()) * 20 * 20 / 1e6, 2)}


def guigang_change() -> dict:
    aoi, epsg = (109.20, 22.95, 110.00, 23.40), 32649
    s1 = W_DIR / "guigang_20240905T105022_sigma20m.tif"
    s2 = W_DIR / "guigang_20240912T104222_sigma20m.tif"
    with rasterio.open(s1) as ds1:
        A1 = ds1.read(1)
        H, W, T, CRS = ds1.height, ds1.width, ds1.transform, ds1.crs
    with rasterio.open(s2) as ds2:
        A2 = np.full((H, W), np.nan)
        reproject(ds2.read(1), A2, src_transform=ds2.transform, src_crs=ds2.crs,
                  dst_transform=T, dst_crs=CRS, src_nodata=np.nan, dst_nodata=np.nan,
                  resampling=Resampling.bilinear)
    both = np.isfinite(A1) & np.isfinite(A2)
    land = both & (A1 > -14.0) & (A2 > -14.0)
    delta = float(np.median(A2[land] - A1[land]))
    w1 = A1 < THR
    w2n = (A2 - delta) < THR
    # AOI 内统计
    tf = Transformer.from_crs(4326, epsg, always_xy=True)
    xs, ys = zip(*[tf.transform(x, y) for x, y in
                   [(aoi[0], aoi[1]), (aoi[2], aoi[3])]])
    rows, cols = rasterio.transform.rowcol(T, [min(xs), max(xs)], [max(ys), min(ys)])
    r0, r1 = max(0, min(rows)), min(H, max(rows) + 1)
    c0, c1 = max(0, min(cols)), min(W, max(cols) + 1)
    inaoi = np.zeros_like(both)
    inaoi[r0:r1, c0:c1] = True
    v = both & inaoi
    new_w = (w2n & ~w1) & v
    lost_w = (w1 & ~w2n) & v
    chg = np.full((H, W), 255, dtype="uint8")
    chg[(~w1) & (~w2n) & both] = 0
    chg[w1 & w2n & both] = 1
    chg[new_w] = 2
    chg[lost_w] = 3
    out_tif = W_DIR / "guigang_flood_change_0912vs0905.tif"
    profile = {"driver": "GTiff", "height": H, "width": W, "count": 1, "dtype": "uint8",
               "crs": CRS, "transform": T, "compress": "deflate", "nodata": 255}
    with rasterio.open(out_tif, "w", **profile) as dst:
        dst.write(chg, 1)
    return {
        "delta_db_brightness": round(delta, 2),
        "n_land_ref_px": int(land.sum()),
        "water_0905_km2_in_aoi": round(float((w1 & v).sum()) * 400 / 1e6, 2),
        "water_0912_km2_in_aoi": round(float((w2n & v).sum()) * 400 / 1e6, 2),
        "new_water_km2": round(float(new_w.sum()) * 400 / 1e6, 2),
        "lost_water_km2": round(float(lost_w.sum()) * 400 / 1e6, 2),
        "cover_in_aoi": round(float(v.mean() / max(inaoi.mean(), 1e-9)), 3),
        "legend": "0 陆-陆 1 水-水(常水) 2 新增水(洪涝候选) 3 失水 255 无覆盖",
        "out": str(out_tif),
        "note": "贵港站 09-12T14 洪峰段 43.09m/10500m3/s，景 09-12T10:42 略前于峰",
    }


def jiulong_consistency() -> dict:
    aoi, epsg = (108.55, 21.92, 108.90, 22.20), 32649
    run = PROJ / "outputs" / "jiulong" / "2024-09-yagi" / "scen_free_200m"
    dem_p = run / "dem.asc"
    with open(dem_p) as f:
        hdr = [next(f) for _ in range(6)]
    dem = np.loadtxt(dem_p, skiprows=6)
    hd = {l.split()[0]: float(l.split()[1]) for l in hdr}
    cell = hd["cellsize"]
    xll, yll = hd["xllcorner"], hd["yllcorner"]
    sea_p = run / "sea_mask.npy"
    sea = np.load(sea_p) if sea_p.exists() else np.zeros_like(dem, dtype=bool)
    ref = (dem < 0) | sea.astype(bool)  # DEM 负值水面（河）+ 海面钳0区
    tgt = rasterio.transform.Affine(cell, 0, xll, 0, -cell, yll + dem.shape[0] * cell)
    out = {"ref_water_km2_200m": round(float(ref.sum()) * cell * cell / 1e6, 2)}
    for stamp in ("20240905T105022", "20240917T105023"):
        with rasterio.open(W_DIR / f"jiulong_{stamp}_water20m.tif") as ds:
            obs = np.zeros(dem.shape, dtype="uint8")
            reproject(ds.read(1), obs, src_transform=ds.transform, src_crs=ds.crs,
                      dst_transform=tgt, dst_crs="EPSG:32649",
                      resampling=Resampling.nearest)
        obsb = obs == 1
        valid = obsb | ref
        H = int((obsb & ref).sum())
        FA = int((obsb & ~ref).sum())
        M = int((~obsb & ref).sum())
        out[f"obs_{stamp[:8]}"] = {
            "obs_water_km2": round(float(obsb.sum()) * cell * cell / 1e6, 2),
            "iou": round(H / max(H + FA + M, 1), 3),
            "pod": round(H / max(H + M, 1), 3),
            "far": round(FA / max(H + FA, 1), 3),
        }
    return out


def main() -> None:
    report = {"generated_utc": datetime.now(timezone.utc).isoformat(), "thr_db": THR}
    report["guigang_change"] = guigang_change()
    report["jiulong_dem_consistency"] = jiulong_consistency()
    report["coverage_in_aoi"] = {}
    for site, aoi, epsg, stamps in (
            ("ningming", (106.96, 22.06, 107.20, 22.26), 32648,
             ("20240910T105833", "20240912T225045")),
            ("guigang", (109.20, 22.95, 110.00, 23.40), 32649,
             ("20240905T105022", "20240912T104222")),
            ("jiulong", (108.55, 21.92, 108.90, 22.20), 32649,
             ("20240905T105022", "20240917T105023"))):
        for st in stamps:
            tif = W_DIR / f"{site}_{st}_water20m.tif"
            report["coverage_in_aoi"][f"{site}_{st[:8]}"] = coverage_and_water(tif, aoi, epsg)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
