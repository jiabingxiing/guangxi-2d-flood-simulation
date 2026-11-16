"""S1 GRD VV 水体提取（降级模式，终稿 8.1 / ADR-0004）。

方法：SAFE zip 免解压（GDAL /vsizip）。辐射定标：σ⁰[dB] = 20log10(DN) − 20log10(K)，
     K 取 calibration XML sigmaNought LUT（线/像素双线性插值）。
     地理定位：annotation geolocationGridPointList（仅 10x21 网格，双线性；
     椭球定位无地形校正 -> 平原定位误差约 1-3 个 20m 格，山体更大，成果须标注）。
     分割：5x5 均值去斑噪 + 固定阈值 σ⁰_VV = -17 dB（C 波段洪水制图常用值），
     避免陆域单峰直方图下 Otsu 把陆地劈半（首版实测失败，见 s1_water_masks 首轮）。
     前向溅射到 20 m UTM 网格（多数表决），未覆盖格 nodata=255。
限制（成果必须标注）：无去噪 LUT、无地形 geocoding、未用 VH 交叉验证；
     -17 dB 阈值对风浪水面（台风期）偏保守（水面粗糙后散射增强 -> 漏提）。
产出：data/processed/s1_water/{site}_{yyyymmddThhmm}_water20m.tif + 汇总 JSON。
"""
from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))
from scripts.common.paths import resolve_legacy  # noqa: E402

OUT_DIR = PROJ / "data" / "processed" / "s1_water"
S1_JSON = PROJ / "data" / "catalog" / "s1_downloads.json"

SITES = {
    "jiulong": {"aoi": (108.55, 21.92, 108.90, 22.20), "epsg": 32649},
    "guigang": {"aoi": (109.20, 22.95, 110.00, 23.40), "epsg": 32649},
    "ningming": {"aoi": (106.96, 22.06, 107.20, 22.26), "epsg": 32648},
}
MARGIN_DEG = 0.03
TGT_RES = 20.0
SIGMA_THR_DB = -17.0


def box_mean(a: np.ndarray, k: int = 5) -> np.ndarray:
    pad = k // 2
    c = np.pad(a.astype("float64"), pad, mode="edge")
    c = np.cumsum(np.cumsum(c, axis=0), axis=1)
    c = np.pad(c, ((1, 0), (1, 0)))
    h, w = a.shape
    s = c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]
    return s / (k * k)


def _bilinear_grid(vals_l: list, vals_p: list, grids: list) -> tuple:
    """把 (line,pixel)->值 的散点装进规则网格，返回 (lines_u, pixels_u, arr_list)。"""
    lines_u = np.unique(sorted(vals_l))
    pixels_u = np.unique(sorted(vals_p))
    ni, nj = len(lines_u), len(pixels_u)
    li = np.searchsorted(lines_u, vals_l)
    pj = np.searchsorted(pixels_u, vals_p)
    out = []
    for g in grids:
        arr = np.full((ni, nj), np.nan)
        arr[li, pj] = g
        assert not np.isnan(arr).any(), "网格不完整"
        out.append(arr)
    return lines_u, pixels_u, out


def _interp2(lines_u, pixels_u, grid, rr, cc):
    rr = np.clip(rr, lines_u[0], lines_u[-1])
    cc = np.clip(cc, pixels_u[0], pixels_u[-1])
    i0 = np.clip(np.searchsorted(lines_u, rr) - 1, 0, len(lines_u) - 2)
    j0 = np.clip(np.searchsorted(pixels_u, cc) - 1, 0, len(pixels_u) - 2)
    tl = (rr - lines_u[i0]) / (lines_u[i0 + 1] - lines_u[i0])
    tp = (cc - pixels_u[j0]) / (pixels_u[j0 + 1] - pixels_u[j0])
    return ((1 - tl) * (1 - tp) * grid[i0, j0] + tl * (1 - tp) * grid[i0 + 1, j0]
            + (1 - tl) * tp * grid[i0, j0 + 1] + tl * tp * grid[i0 + 1, j0 + 1])


def extract(zip_path: Path, site: str) -> dict:
    cfg = SITES[site]
    w, s, e, n = cfg["aoi"]
    zf = zipfile.ZipFile(zip_path)
    names = zf.namelist()
    ann = next(x for x in names if "/annotation/s1" in x and "-vv-" in x and x.endswith(".xml"))
    cal = next(x for x in names if "calibration-s1a-iw-grd-vv" in x)
    meas = next(x for x in names if "/measurement/" in x and "-vv-" in x and x.endswith(".tiff"))

    # 大地定位网格
    root = ET.fromstring(zf.read(ann))
    gl = {"line": [], "pixel": [], "lat": [], "lon": []}
    for g in root.iter("geolocationGridPoint"):
        gl["line"].append(float(g.findtext("line")))
        gl["pixel"].append(float(g.findtext("pixel")))
        gl["lat"].append(float(g.findtext("latitude")))
        gl["lon"].append(float(g.findtext("longitude")))
    lines_u, pixels_u, (lat_g, lon_g) = _bilinear_grid(
        gl["line"], gl["pixel"], [gl["lat"], gl["lon"]])

    # 定标 K 网格（每向量：line + pixel 数组 + sigmaNought 数组）
    croot = ET.fromstring(zf.read(cal))
    cl, cp, ck = [], [], []
    for v in croot.iter("calibrationVector"):
        pix = np.fromstring(v.findtext("pixel"), sep=" ")
        sig = np.fromstring(v.findtext("sigmaNought"), sep=" ")
        cp.extend(pix.tolist())
        ck.extend(sig.tolist())
        cl.extend([float(v.findtext("line"))] * len(pix))
    klines, kpixels, (k_g,) = _bilinear_grid(cl, cp, [ck])

    # AOI 窗（网格点筛 + 外扩一格）
    inbox = ((lon_g >= w - MARGIN_DEG) & (lon_g <= e + MARGIN_DEG)
             & (lat_g >= s - MARGIN_DEG) & (lat_g <= n + MARGIN_DEG))
    ii, jj = np.where(inbox)
    if ii.size == 0:
        return {"site": site, "zip": str(zip_path), "error": "AOI 与景足印无交集"}
    r0 = max(0, int(lines_u[max(ii.min() - 1, 0)]) - 5)
    r1 = int(lines_u[min(ii.max() + 1, len(lines_u) - 1)]) + 5
    c0 = max(0, int(pixels_u[max(jj.min() - 1, 0)]) - 5)
    c1 = int(pixels_u[min(jj.max() + 1, len(pixels_u) - 1)]) + 5

    vsi = f"/vsizip/{zip_path.as_posix()}/{meas}"
    with rasterio.open(vsi) as ds:
        H, W = ds.shape
        r0, r1 = max(0, r0), min(H, r1)
        c0, c1 = max(0, c0), min(W, c1)
        dn = ds.read(1, window=rasterio.windows.Window(c0, r0, c1 - c0, r1 - r0)).astype("float32")
    dn[dn <= 0] = np.nan
    rr = np.arange(r0, r1)[:, None] * np.ones((1, c1 - c0))
    cc = np.ones((r1 - r0, 1)) * np.arange(c0, c1)[None, :]
    k = _interp2(klines, kpixels, k_g, rr, cc)
    sigma = 20.0 * np.log10(dn) - 20.0 * np.log10(k)
    valid = np.isfinite(sigma)
    fill = np.nanmedian(sigma) if valid.any() else 0.0
    sig_s = box_mean(np.where(valid, sigma, fill))
    water = (sig_s < SIGMA_THR_DB) & valid
    pcts = np.nanpercentile(sigma[valid], [1, 5, 25, 50, 75, 95]) if valid.any() else [np.nan] * 6

    # 前向溅射到 20m UTM（多数表决）
    lon = _interp2(lines_u, pixels_u, lon_g, rr, cc)
    lat = _interp2(lines_u, pixels_u, lat_g, rr, cc)
    tf = Transformer.from_crs(4326, f"EPSG:{cfg['epsg']}", always_xy=True)
    X, Y = tf.transform(lon, lat)
    x0 = np.floor(np.nanmin(X) / TGT_RES) * TGT_RES
    y1 = np.ceil(np.nanmax(Y) / TGT_RES) * TGT_RES
    tcol = np.round((X - x0) / TGT_RES).astype("int64")
    trow = np.round((y1 - Y) / TGT_RES).astype("int64")
    TH, TW = int(np.nanmax(trow)) + 2, int(np.nanmax(tcol)) + 2
    inb = valid & (trow >= 0) & (trow < TH) & (tcol >= 0) & (tcol < TW)
    flat = trow[inb].astype("int64") * TW + tcol[inb].astype("int64")
    all_cnt = np.bincount(flat, minlength=TH * TW)
    wet_cnt = np.bincount(flat, weights=water[inb].astype("float64"), minlength=TH * TW)
    sig_sum = np.bincount(flat, weights=np.where(valid, sig_s, 0.0)[inb], minlength=TH * TW)
    cover = all_cnt > 0
    wmaj = ((wet_cnt * 2 > all_cnt) & cover).reshape(TH, TW).astype("uint8")
    sig_mean = np.where(cover, sig_sum / np.maximum(all_cnt, 1), np.nan).reshape(TH, TW)

    m = re.search(r"_(\d{8})T(\d{6})_", zip_path.name)
    stamp = f"{m.group(1)}T{m.group(2)}"
    out_tif = OUT_DIR / f"{site}_{stamp}_water20m.tif"
    profile = {"driver": "GTiff", "height": TH, "width": TW, "count": 1, "dtype": "uint8",
               "crs": f"EPSG:{cfg['epsg']}", "transform": rasterio.Affine(
                   TGT_RES, 0, float(x0), 0, -TGT_RES, float(y1)),
               "compress": "deflate", "nodata": 255}
    with rasterio.open(out_tif, "w", **profile) as dst:
        dst.write(np.where(cover.reshape(TH, TW), wmaj, 255), 1)
    out_sig = OUT_DIR / f"{site}_{stamp}_sigma20m.tif"
    profile_sig = dict(profile, dtype="float32", nodata=np.nan, compress="deflate")
    with rasterio.open(out_sig, "w", **profile_sig) as dst:
        dst.write(sig_mean.astype("float32"), 1)
    return {
        "site": site, "scene": zip_path.name, "sensing_utc": stamp,
        "aoi_bbox_wgs84": list(cfg["aoi"]), "epsg": cfg["epsg"],
        "sigma0_thr_db": SIGMA_THR_DB,
        "sigma0_pcts_1_5_25_50_75_95": [round(float(x), 1) for x in pcts],
        "water_km2_20m": round(float(wmaj.sum()) * TGT_RES * TGT_RES / 1e6, 2),
        "cover_frac": round(float(cover.mean()), 3),
        "window_rows_cols": [int(r1 - r0), int(c1 - c0)],
        "geoloc": "annotation geolocationGridPointList 10x21 双线性（椭球定位，无地形校正，定位误差约百米级）",
        "calibration": "sigma0=20log10(DN)-20log10(K)，K=calibration LUT",
        "out": str(out_tif), "out_sigma": str(out_sig),
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cat = json.loads(S1_JSON.read_text(encoding="utf-8"))
    results = []
    for key, rec in cat["scenes"].items():
        site = key.split("/")[0]
        if site not in SITES:
            continue
        print(f"[{site}] {key} ...", flush=True)
        try:
            # 清单里的 path 是历史宿主绝对路径（可能旧盘符/旧上级目录名），
            # 经 paths 重锚到当前项目；解析失败只跳过该景，不中断批次。
            r = extract(resolve_legacy(rec["path"]), site)
        except Exception as exc:  # 单景失败不中断批次
            r = {"site": site, "scene": key, "error": repr(exc)}
        print(json.dumps(r, ensure_ascii=False), flush=True)
        results.append(r)
    OUT = PROJ / "data" / "catalog" / "s1_water_masks.json"
    OUT.write_text(json.dumps(
        {"generated_utc": datetime.now(timezone.utc).isoformat(),
         "v2_note": "首版 Otsu 在陆域单峰直方图失效（全图过提），本版改定标+固定阈值 -17dB",
         "scenes": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("->", OUT, flush=True)


if __name__ == "__main__":
    main()
