"""久隆钦江 A1/A2 流域分区建库（计划 任务2 / ADR-0019）。

A1 = 模型外钦江上游集水区（产流只进 QVAR 边界）；
A2 = 二维模型域（净雨只进 .rain）。两账分立，禁止重复计量（计划 §6.2 红线 2）。

DEM：Copernicus GLO-30（data/raw/dem_source/glo30/，EPSG:4326，公开下载）
→ 重采样 EPSG:32649 @200 m（与生产基线一致）→ Priority-Flood 填洼 → D8 →
汇流累积 → 上游集水。填洼结果只用于水文拓扑派生，严禁送入水动力求解器
（计划 §4.4 四层 DEM 契约）。

入域断面：AOI 北缘/东缘外 3 格环内汇流累积最大的像元 = 钦江入域断面候选，
由 prepare_jiulong_channel.py 用河网矢量复核。

证据等级 E3（公开 30 m DEM 水文派生，未率定）。
"""

from __future__ import annotations

import json
import sys
from argparse import ArgumentParser
from pathlib import Path

import numpy as np
import rasterio
from rasterio.features import shapes as feat_shapes
from pyproj import Transformer
from rasterio.transform import from_bounds
from rasterio.warp import reproject, Resampling

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import AuditLog, add_common_args, utc_now, write_json  # noqa: E402
from scripts.common.terrain_hydro import (  # noqa: E402
    d8_receiver,
    flow_accumulation,
    priority_flood,
    upstream_mask,
)

GLO30_DIR = PROJ / "data" / "raw" / "dem_source" / "glo30"
OUT_DIR = PROJ / "data" / "interim" / "jiulong" / "catchment"
CATALOG = PROJ / "data" / "catalog" / "jiulong_catchments.json"

AOI_WGS84 = (108.55, 21.92, 108.90, 22.20)  # 生产基线 AOI（configs/cases/jiulong.yaml）
BASIN_BBOX_WGS84 = (107.80, 21.40, 109.90, 23.80)  # 钦江全流域外扩框
RES_M = 200.0
EPSG = 32649
LUWU_AREA_KM2_PRIOR = 1400.0  # 陆屋水文站控制面积先验（文献）
A1_SANITY_KM2 = (1000.0, 2600.0)
STREAM_MIN_AREA_KM2 = 5.0
ENTRY_RING_CELLS = 3

EVIDENCE_LEVEL = "E3（GLO-30 公开 DEM 水文派生，未率定）"


def target_grid(res_m: float):
    """钦江流域外扩框在 EPSG:32649 下的目标网格。"""
    tr = Transformer.from_crs("EPSG:4326", f"EPSG:{EPSG}", always_xy=True)
    x0, y0 = tr.transform(BASIN_BBOX_WGS84[0], BASIN_BBOX_WGS84[1])
    x1, y1 = tr.transform(BASIN_BBOX_WGS84[2], BASIN_BBOX_WGS84[3])
    west, east = min(x0, x1), max(x0, x1)
    south, north = min(y0, y1), max(y0, y1)
    ncols = int(np.ceil((east - west) / res_m))
    nrows = int(np.ceil((north - south) / res_m))
    transform = from_bounds(west, south, east, north, ncols, nrows)
    return np.full((nrows, ncols), np.nan, dtype=np.float32), transform, (
        west,
        south,
        east,
        north,
    )


def load_basin_dem(canvas: np.ndarray, transform) -> np.ndarray:
    """逐瓦片重投影 GLO-30 到目标网格（瓦片 1°×1° 不重叠）。"""
    tiles = sorted(GLO30_DIR.glob("*.tif"))
    if not tiles:
        raise FileNotFoundError(f"GLO-30 瓦片缺失：{GLO30_DIR}")
    for tile in tiles:
        with rasterio.open(tile) as src:
            band = np.full(canvas.shape, np.nan, dtype=np.float32)
            reproject(
                rasterio.band(src, 1),
                band,
                dst_transform=transform,
                dst_crs=f"EPSG:{EPSG}",
                dst_nodata=np.nan,
                resampling=Resampling.bilinear,
            )
            valid = np.isfinite(band) & (band > -9999)
            canvas[valid] = band[valid]
    n_hole = int(np.count_nonzero(~np.isfinite(canvas)))
    canvas[~np.isfinite(canvas)] = 0.0  # 海面/边缘空洞置 0
    return canvas, n_hole


def find_entry_cell(acc: np.ndarray, transform, res_m: float) -> dict:
    """AOI 北缘/东缘外 3 格环内汇流累积最大像元。"""
    nrows, ncols = acc.shape
    tr_fwd = Transformer.from_crs(f"EPSG:{EPSG}", "EPSG:4326", always_xy=True)
    tr_inv = Transformer.from_crs("EPSG:4326", f"EPSG:{EPSG}", always_xy=True)
    ax0, ay0 = tr_inv.transform(AOI_WGS84[0], AOI_WGS84[1])
    ax1, ay1 = tr_inv.transform(AOI_WGS84[2], AOI_WGS84[3])
    aoi_west, aoi_east = min(ax0, ax1), max(ax0, ax1)
    aoi_south, aoi_north = min(ay0, ay1), max(ay0, ay1)

    xs = transform.c + (np.arange(ncols) + 0.5) * transform.a
    ys = transform.f + (np.arange(nrows) + 0.5) * transform.e
    X = np.broadcast_to(xs[None, :], acc.shape)
    Y = np.broadcast_to(ys[:, None], acc.shape)

    ring = (
        ((X > aoi_west) & (X <= aoi_east) & (Y > aoi_north) & (Y <= aoi_north + ENTRY_RING_CELLS * res_m))
        | ((Y >= aoi_south) & (Y <= aoi_north) & (X > aoi_east) & (X <= aoi_east + ENTRY_RING_CELLS * res_m))
    )
    if not ring.any():
        raise RuntimeError("AOI 北/东缘外环内未找到候选像元，检查 BASIN_BBOX/AOI")
    masked = np.where(ring, acc, -1.0)
    r, c = np.unravel_index(int(np.argmax(masked)), acc.shape)
    lon, lat = tr_fwd.transform(
        transform.c + (c + 0.5) * transform.a, transform.f + (r + 0.5) * transform.e
    )
    x, y = transform.c + (c + 0.5) * transform.a, transform.f + (r + 0.5) * transform.e
    side = "N" if y > aoi_north else "E"
    return {
        "row": int(r),
        "col": int(c),
        "side": side,
        "acc_cells": float(acc[r, c]),
        "acc_km2": float(acc[r, c]) * (res_m**2) / 1e6,
        "easting": float(x),
        "northing": float(y),
        "lon": float(lon),
        "lat": float(lat),
    }


def _poly_area(polygon: dict) -> float:
    coords = polygon["coordinates"][0]
    a = 0.0
    for (x0, y0), (x1, y1) in zip(coords[:-1], coords[1:]):
        a += x0 * y1 - x1 * y0
    return abs(a / 2.0)


def catchment_geojson(mask: np.ndarray, transform) -> dict:
    """A1 掩膜 → 最大连通多边形 GeoJSON。"""
    largest = None
    largest_area = -1.0
    for geom, value in feat_shapes(
        mask.astype(np.uint8), mask=mask.astype(np.uint8), transform=transform
    ):
        if value != 1:
            continue
        poly = {"type": "Polygon", "coordinates": geom["coordinates"]}
        area = _poly_area(poly)
        if area > largest_area:
            largest_area, largest = area, poly
    if largest is None:
        raise RuntimeError("A1 掩膜无连通多边形")
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "jiulong_a1_upstream", "source": "glo30_d8"},
                "geometry": largest,
            }
        ],
    }


def write_tif(path: Path, arr: np.ndarray, transform) -> Path:
    data = arr.astype(np.float32)
    data[~np.isfinite(data)] = -9999.0
    profile = {
        "driver": "GTiff",
        "height": arr.shape[0],
        "width": arr.shape[1],
        "count": 1,
        "dtype": "float32",
        "crs": f"EPSG:{EPSG}",
        "transform": transform,
        "compress": "lzw",
        "nodata": -9999.0,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__)
    add_common_args(parser)
    parser.add_argument("--res", type=float, default=RES_M)
    args = parser.parse_args(argv)
    log = AuditLog("build_jiulong_catchments", run_tag=args.run_tag, dry_run=args.dry_run)
    log.add_input_dir("glo30", GLO30_DIR, pattern="*.tif")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (PROJ / "logs" / "stage3").mkdir(parents=True, exist_ok=True)

    res_m = float(args.res)

    canvas, transform, bbox_proj = target_grid(res_m)
    log.step("grid", res_m=res_m, bbox_proj=[round(v, 1) for v in bbox_proj])
    dem, n_hole = load_basin_dem(canvas, transform)
    log.step("load_dem", shape=list(dem.shape), hole_cells_filled=n_hole)

    filled = priority_flood(dem, eps=1e-5)  # PF+ε：平点梯度指向溢出口
    receiver = d8_receiver(filled)
    acc = flow_accumulation(filled, receiver)
    cell_area_km2 = res_m**2 / 1e6
    log.step("terrain_hydro", filled_max_m=float(filled.max()))

    entry = find_entry_cell(acc, transform, res_m)
    a1_mask = upstream_mask(receiver, (entry["row"], entry["col"]))
    a1_area_km2 = float(a1_mask.sum() * cell_area_km2)
    a2_area_km2 = 1106.56  # 200 m 基线 182×152 格（root_cause_report §0.2）
    sanity_ok = A1_SANITY_KM2[0] <= a1_area_km2 <= A1_SANITY_KM2[1]
    log.step(
        "delineate",
        entry=entry,
        a1_area_km2=a1_area_km2,
        sanity_ok=sanity_ok,
        luwu_prior_km2=LUWU_AREA_KM2_PRIOR,
    )
    if not sanity_ok:
        log.warn(
            f"A1 面积 {a1_area_km2:.0f} km² 超出合理性区间 {A1_SANITY_KM2}"
            f"（陆屋先验 {LUWU_AREA_KM2_PRIOR:.0f} km²），下游使用需复核"
        )

    stream_mask = acc * cell_area_km2 >= STREAM_MIN_AREA_KM2

    outputs: dict[str, str] = {}
    if not args.dry_run:
        targets = {
            "dem_filled": OUT_DIR / "dem_glo30_filled_200m.tif",
            "flow_acc": OUT_DIR / "flow_acc_cells.tif",
            "a1_mask": OUT_DIR / "catchment_a1.tif",
            "stream_mask": OUT_DIR / "stream_mask_5km2.tif",
        }
        write_tif(targets["dem_filled"], filled, transform)
        write_tif(targets["flow_acc"], acc, transform)
        write_tif(targets["a1_mask"], np.where(a1_mask, 1.0, 0.0), transform)
        write_tif(targets["stream_mask"], np.where(stream_mask, 1.0, 0.0), transform)
        gj_path = OUT_DIR / "catchment_a1.geojson"
        gj_path.write_text(
            json.dumps(catchment_geojson(a1_mask, transform), ensure_ascii=False),
            encoding="utf-8",
        )
        outputs = {
            key: str(path.relative_to(PROJ)).replace("\\", "/")
            for key, path in {**targets, "a1_geojson": gj_path}.items()
        }

    catalog = {
        "case": "jiulong",
        "generated_utc": utc_now(),
        "evidence_level": EVIDENCE_LEVEL,
        "grid": {
            "res_m": res_m,
            "epsg": EPSG,
            "bbox_proj": [round(v, 1) for v in bbox_proj],
            "bbox_wgs84": list(BASIN_BBOX_WGS84),
            "shape": list(dem.shape),
        },
        "a1": {
            "area_km2": a1_area_km2,
            "entry": entry,
            "sanity_ok": sanity_ok,
            "sanity_range_km2": list(A1_SANITY_KM2),
            "luwu_prior_km2": LUWU_AREA_KM2_PRIOR,
            "method": "GLO-30 Priority-Flood + D8 upstream_mask",
            "outputs": outputs,
        },
        "a2": {
            "area_km2": a2_area_km2,
            "note": "200 m 生产基线 182×152 格；A2 净雨只进 .rain，A1 产流只进 QVAR",
        },
        "stream_min_area_km2": STREAM_MIN_AREA_KM2,
        "no_double_counting": {"a1_to_qvar_only": True, "a2_to_rain_only": True},
    }
    if not args.dry_run:
        write_json(CATALOG, catalog)
    tag = f".{args.run_tag}" if args.run_tag else ""
    log.write(PROJ / "logs" / "stage3" / f"build_jiulong_catchments{tag}.json")
    print(
        f"A1={a1_area_km2:.1f} km² entry=({entry['row']},{entry['col']},{entry['side']})"
        f" acc_at_entry={entry['acc_km2']:.0f} km² sanity={'OK' if sanity_ok else 'WARN'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
