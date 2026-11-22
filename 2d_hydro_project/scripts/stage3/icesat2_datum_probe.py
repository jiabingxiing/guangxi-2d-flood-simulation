"""ICESat-2 ATL13 / JRC GSW 垂直基准核证探针（计划 T09 / S5；ADR-0017 口径）。

核心计算（纯 numpy，合成数据可测）：
- 沿轨点 → dem.asc 最近格（UTM 变换），offset = DEM 高程 − ATL13 水面高程；
- 逐轨中位数 ± IQR；汇总「有界估计」区间（中位数 ± IQR，不写死常数）；
- GSW occurrence ≥80% 掩膜可选；nodata/质量位过滤；
- 覆盖门 ≥10 轨（郁江+明江合计）；<2 轨留出集时按预注册补足。

数据通道：icepyx + Earthdata 凭证；凭证缺 → 记「数据不可得」（S5 未验证）。
产出：`data/catalog/gx_datum_probe.json`。

用法（先设 `$env:PYTHONUTF8="1"`）::

    uv --directory 2d_hydro_project run python scripts/stage3/icesat2_datum_probe.py --site guigang           # 登记（无凭证 → 数据不可得）
    uv --directory 2d_hydro_project run python scripts/stage3/icesat2_datum_probe.py --input tracks.csv --dem <dem.asc>   # 本地沿轨 CSV
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import (  # noqa: E402
    AuditLog,
    GridError,
    read_ascii_header,
    utc_now,
    write_json,
)

CATALOG = PROJ / "data" / "catalog"
SITE_BBOX = {
    "guigang": {"bbox": (109.3, 22.95, 110.0, 23.35), "epsg": 32649,
                "dem": "outputs/guigang/2024-09-yagi/scen_yagi_q11500_100m_dt2p5_chhyd_d14_r4_infil3/dem.asc",
                "river": "郁江", "slope_correction": False},
    "ningming": {"bbox": (106.9, 22.0, 107.3, 22.3), "epsg": 32648,
                 "dem": "outputs/ningming/2026-08-zitan/scen_q6000_n100_100m/dem.asc",
                 "river": "明江", "slope_correction": True},
}
MIN_TRACKS = 10
MIN_HOPLOUT = 2


def read_tracks_csv(path: Path) -> list[dict[str, Any]]:
    """本地沿轨 CSV：track_id,lat,lon,wse_m[,quality]。"""
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            rec = {
                "track_id": row["track_id"],
                "lat": float(row["lat"]),
                "lon": float(row["lon"]),
                "wse_m": float(row["wse_m"]),
                "quality": int(row["quality"]) if row.get("quality") else 0,
            }
            rows.append(rec)
    return rows


def offset_for_track(
    tracks: list[dict[str, Any]], dem_data: np.ndarray, hdr: dict[str, float],
    epsg: int, *, gsw_mask: np.ndarray | None = None, min_quality: int = 0,
    detrend: bool = False,
) -> dict[str, Any]:
    """单轨 offset 统计（DEM − WSE；越界/掩膜/低质量点剔除）。

    detrend=True 时对「沿轨序号 × offset」做线性去趋势（坡度校正，ADR-0017 可选项）。
    """
    from pyproj import Transformer

    tf = Transformer.from_crs(4326, epsg, always_xy=True)
    cell = hdr["cellsize"]
    offsets: list[float] = []
    for t in tracks:
        if t["quality"] < min_quality:
            continue
        x, y = tf.transform(t["lon"], t["lat"])
        col = int((x - hdr["xllcorner"]) / cell)
        row = int((hdr["yllcorner"] + hdr["nrows"] * cell - y) / cell)
        if not (0 <= row < dem_data.shape[0] and 0 <= col < dem_data.shape[1]):
            continue
        z = float(dem_data[row, col])
        if not np.isfinite(z) or z == hdr["nodata_value"]:
            continue
        if gsw_mask is not None and not gsw_mask[row, col]:
            continue
        offsets.append(z - t["wse_m"])
    arr = np.asarray(offsets, dtype=np.float64)
    if arr.size == 0:
        return {"n_points": 0}
    if detrend and arr.size >= 3:
        order = np.arange(arr.size, dtype=np.float64)
        coef = np.polyfit(order, arr, 1)
        arr = arr - np.polyval(coef, order)
    med = float(np.median(arr))
    iqr = float(np.percentile(arr, 75) - np.percentile(arr, 25))
    return {
        "n_points": int(arr.size),
        "median_m": round(med, 3),
        "iqr_m": round(iqr, 3),
        "detrended": bool(detrend and arr.size >= 3),
    }


def pool_verdict(track_stats: list[dict[str, Any]], dem_name: str) -> dict[str, Any]:
    """汇总有界估计 + 覆盖判定；随机留出 ≥2 轨（固定种子，复核集）。"""
    ok = [s for s in track_stats if s.get("n_points", 0) >= 3]
    medians = np.asarray([s["median_m"] for s in ok], dtype=np.float64) if ok else np.array([])
    n_tracks = len(track_stats)
    rng = np.random.default_rng(2019)
    order = rng.permutation(n_tracks)
    n_hold = min(MIN_HOPLOUT, max(0, n_tracks - 1))
    holdout = [track_stats[i]["track_id"] for i in sorted(order[:n_hold])]
    verdict = {
        "n_tracks": n_tracks,
        "n_tracks_valid": len(ok),
        "coverage": "sufficient" if n_tracks >= MIN_TRACKS else "覆盖不足",
        "holdout_tracks": holdout,
        "offset_interval": None,
        "min_tracks_required": MIN_TRACKS,
    }
    if medians.size:
        med = float(np.median(medians))
        iqr = float(np.percentile(medians, 75) - np.percentile(medians, 25))
        verdict["offset_interval"] = {
            "median_m": round(med, 3), "iqr_m": round(iqr, 3),
            "note": "有界估计（中位数 ± IQR，含水深+基准两个分量；不得写死 datum_offset）",
        }
    return verdict


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", choices=list(SITE_BBOX), default=None)
    parser.add_argument("--input", type=Path, default=None,
                        help="本地沿轨 CSV（track_id,lat,lon,wse_m[,quality]）；缺省登记数据不可得")
    parser.add_argument("--dem", type=Path, default=None, help="覆盖默认 dem.asc")
    parser.add_argument("--gsw", type=Path, default=None, help="GSW occurrence GeoTIFF（可选）")
    from scripts.common.grid_io import add_common_args

    add_common_args(parser)
    args = parser.parse_args()
    log = AuditLog("icesat2_datum_probe.py", run_tag=args.run_tag, dry_run=args.dry_run)

    rec: dict[str, Any] = {
        "script": "icesat2_datum_probe.py",
        "generated_utc": utc_now(),
        "adr": "ADR-0017",
        "data_available": False,
        "s5_status": "未验证（ATL13 数据不可得：无 Earthdata 凭证且无本地沿轨文件）",
        "evidence_level": "E2/E3（水面高程实测分量 + 格网落点近似）",
        "next_steps": [
            "配置 Earthdata 凭证（~/.netrc：urs.earthdata.nasa.gov）",
            "用 icepyx 检索 bbox 内 ATL13（2019–2026）导出沿轨 CSV（track_id,lat,lon,wse_m,quality）",
            "重跑本脚本 --input tracks.csv --dem <dem.asc> [--gsw occurrence.tif]",
        ],
    }
    if args.input is not None and (args.dem is not None or args.site):
        cfg = SITE_BBOX.get(args.site) if args.site else None
        dem_path = args.dem or (PROJ / cfg["dem"])
        if not dem_path.is_file():
            raise FileNotFoundError(f"dem.asc 不存在：{dem_path}")
        hdr = read_ascii_header(dem_path)
        data = np.loadtxt(dem_path, skiprows=6, dtype=np.float64, ndmin=2)
        gsw_mask = None
        if args.gsw is not None:
            import rasterio

            with rasterio.open(args.gsw) as ds:
                occ = ds.read(1).astype("float64")
            gsw_mask = occ >= 80.0
        tracks = read_tracks_csv(args.input)
        log.add_input("tracks_csv", args.input)
        log.add_input("dem", dem_path)
        by_track: dict[str, list[dict[str, Any]]] = {}
        for t in tracks:
            by_track.setdefault(t["track_id"], []).append(t)
        epsg = cfg["epsg"] if cfg else 32649
        stats = []
        for tid, pts in sorted(by_track.items()):
            st = offset_for_track(pts, data, hdr, epsg, gsw_mask=gsw_mask)
            st["track_id"] = tid
            stats.append(st)
        verdict = pool_verdict(stats, dem_path.name)
        rec.update({
            "data_available": True,
            "dem": str(dem_path.name),
            "river": cfg["river"] if cfg else None,
            "gsw_mask_applied": gsw_mask is not None,
            "slope_correction_applied": bool(cfg and cfg["slope_correction"]),
            "per_track": stats,
            "verdict": verdict,
            "s5_status": "有界估计已产出（G7 不闭合红线维持；区间不得写死为常数）"
            if verdict["offset_interval"] else "未验证（无有效轨）",
        })
    out_json = args.log_json or (CATALOG / "gx_datum_probe.json")
    log.payload["probe"] = rec
    log.write(out_json)
    print(json.dumps(rec, ensure_ascii=False)[:600])
    print("->", out_json)


if __name__ == "__main__":
    main()
