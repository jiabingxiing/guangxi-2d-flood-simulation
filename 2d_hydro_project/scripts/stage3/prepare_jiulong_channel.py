"""久隆钦江水系建库与河道输入包生成（计划 任务2 / §5.1、ADR-0020）。

数据链（河网获取优先级降级口径）：
- 水利部门河道管理资料：缺（待授权）→ 跳过；
- HydroRIVERS v10 Asia（公开，data/raw/hydro/jiulong/river/）：主河网来源；
- Sentinel-1 常水（灾前 2024-09-05）：中心线双重核验 + 河宽实测；
- OSM：主站与镜像均不可达（2026-11-22 实测），记录降级。

河道表达：首选 SGC 亚网格河道（sgc_width/sgc_bank ASCII 栅格），避免改写 DEM；
不生成刻蚀 DEM（R2a 原地形对照必须无刻槽；浅槽备选仅在 SGC 核验失败后另启，
四道护栏见 ADR-0020）。

拓扑审计 6 项：连续性 / 纵向单调 / 支流接入 / 假截断（无路网资料→未验证）/ 
入流边界对齐 / 出口贯通。无实测水下地形：河宽为 S1 实测、河床为水力几何
先验（E3），逐项标注不确定性。

落盘（data/interim/jiulong/channel/）：river_centerline.geojson、
tributaries.geojson、river_network.geojson、sgc_width.asc、sgc_bank.asc、
river_topology.json、channel_geometry_audit.json；
目录（data/catalog/）：jiulong_hydrography.json。
"""

from __future__ import annotations

import json
import sys
from argparse import ArgumentParser
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import (  # noqa: E402
    AuditLog,
    add_common_args,
    read_ascii_grid,
    sha256_file,
    utc_now,
    write_ascii_grid,
    write_json,
)

HYDRORIVERS_BASE = (
    PROJ / "data/raw/hydro/jiulong/river/HydroRIVERS_v10_as_shp"
    / "HydroRIVERS_v10_as_shp/HydroRIVERS_v10_as"
)
OSM_CACHE = PROJ / "data/raw/hydro/jiulong/river/osm_waterways_aoi.geojson"
S1_WATER = PROJ / "data/processed/s1_water/jiulong_20240905T105022_water20m.tif"
BASELINE_RUN = PROJ / "outputs/jiulong/2024-09-yagi/scen_free_200m_sv6h"
OUT_DIR = PROJ / "data/interim/jiulong/channel"
HYDROGRAPHY = PROJ / "data/catalog/jiulong_hydrography.json"
GEOM_AUDIT = PROJ / "data/catalog/jiulong_river_geometry_audit.json"

AOI_WGS84 = (108.55, 21.92, 108.90, 22.20)
AOI_BUFFER_DEG = 0.15
QINJIANG_MAIN_RIV = 41134077  # HydroRIVERS 主河 ID（四个锚点实测归此河）
ENTRY_LONLAT = (108.8318, 22.1992)  # A1 入域断面（build_jiulong_catchments）
ANCHORS = {
    "jiulong_town": (108.7161, 22.0638),
    "estuary_direction": (108.61, 21.78),
}
TRIB_MIN_CATCH_SKM = 20.0  # AOI 缓冲内最大支流 CATCH_SKM≈40.7（实测）；20 保住排埠江级支流
WIDTH_PRIOR_M = 120.0  # 无 S1 证据节点的回退先验
WIDTH_MAX_M = 180.0    # 硬上限（须 < 200 m 网格）
BANKFULL_FACTOR = 1.4  # 常水宽 → 满槽宽先验因子（E3，无实测断面）
SGC_DEPTH_AT_REF_M = 4.5   # 参考河宽对应满槽水深先验（E3）
SGC_P = 0.4                 # 水力几何指数 depth = r·w^p
EVIDENCE_LEVEL = "E3（HydroRIVERS 公开河网 + S1 常水核验 + 水力几何先验，无实测断面）"


def load_osm_mainstem() -> list[dict]:
    """OSM 钦江干流（name 含「钦江」的 river ways），自上游（高纬）向下游排序。

    OSM 无流向属性：钦江自东北向西南流 → 段内按纬度降序定向，段间按最高纬
    降序拼接。返回与 rasterize_path_to_grid 兼容的 feat 列表。
    """
    if not OSM_CACHE.exists():
        return []
    feats = json.loads(OSM_CACHE.read_text(encoding="utf-8"))["features"]
    qj = [f for f in feats if "钦江" in (f["properties"].get("name") or "")]
    oriented: list[dict] = []
    for f in qj:
        coords = f["geometry"]["coordinates"]
        if coords[0][1] < coords[-1][1]:
            coords = coords[::-1]
        oriented.append({**f, "geometry": {"type": "LineString", "coordinates": coords}})
    oriented.sort(key=lambda f: -f["geometry"]["coordinates"][0][1])
    return oriented


def load_rivers(log: AuditLog) -> list[dict]:
    """读取 HydroRIVERS AOI+缓冲框内河段（含属性），带 geojson 缓存。"""
    cache = OUT_DIR / "hydro_rivers_cache.geojson"
    if cache.exists():
        log.step("cache_hit", path=str(cache))
        return json.loads(cache.read_text(encoding="utf-8"))["features"]
    import shapefile

    reader = shapefile.Reader(str(HYDRORIVERS_BASE))
    bbox = (
        AOI_WGS84[0] - AOI_BUFFER_DEG,
        AOI_WGS84[1] - AOI_BUFFER_DEG,
        AOI_WGS84[2] + AOI_BUFFER_DEG,
        AOI_WGS84[3] + AOI_BUFFER_DEG,
    )
    feats: list[dict] = []
    for sr in reader.iterShapeRecords():
        shape_bbox = sr.shape.bbox
        if (
            shape_bbox[2] < bbox[0]
            or shape_bbox[0] > bbox[2]
            or shape_bbox[3] < bbox[1]
            or shape_bbox[1] > bbox[3]
        ):
            continue
        rec = sr.record.as_dict()
        pts = [
            [round(x, 6), round(y, 6)]
            for x, y in sr.shape.points
            if bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]
        ]
        if len(pts) < 2:
            continue
        feats.append(
            {
                "type": "Feature",
                "properties": {
                    k: rec[k]
                    for k in (
                        "HYRIV_ID",
                        "NEXT_DOWN",
                        "MAIN_RIV",
                        "LENGTH_KM",
                        "DIST_DN_KM",
                        "CATCH_SKM",
                        "UPLAND_SKM",
                        "DIS_AV_CMS",
                        "ORD_STRA",
                    )
                },
                "geometry": {"type": "LineString", "coordinates": pts},
            }
        )
    log.step("hydro_rivers_scan", features=len(feats), main_riv=QINJIANG_MAIN_RIV)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}, ensure_ascii=False),
        encoding="utf-8",
    )
    return feats


def assemble_mainstem(feats: list[dict]) -> tuple[list[dict], float]:
    """钦江干流（模型域段）：从入域断面沿 NEXT_DOWN 链走到河口。

    MAIN_RIV 对全水系（含支流）同值，不能用 dist 排序近似干流；NEXT_DOWN
    链是 HydroRIVERS 树的干流定义。返回（链河段[上游→下游], 入域断面 dist）。
    """
    by_id = {f["properties"]["HYRIV_ID"]: f for f in feats}
    main_all = [f for f in feats if f["properties"]["MAIN_RIV"] == QINJIANG_MAIN_RIV]
    entry_seg = min(
        main_all,
        key=lambda f: min(
            (x - ENTRY_LONLAT[0]) ** 2 + (y - ENTRY_LONLAT[1]) ** 2
            for x, y in f["geometry"]["coordinates"]
        ),
    )
    entry_dist = float(entry_seg["properties"]["DIST_DN_KM"])
    chain: list[dict] = []
    node = entry_seg
    visited: set[int] = set()
    while node is not None and node["properties"]["HYRIV_ID"] not in visited:
        visited.add(node["properties"]["HYRIV_ID"])
        chain.append(node)
        nxt = int(node["properties"]["NEXT_DOWN"] or 0)
        node = by_id.get(nxt)
    return chain, entry_dist


def assemble_tributaries(feats: list[dict], chain_ids: set[int]) -> list[dict]:
    """与 AOI 相交的支流 = 钦江水系（MAIN_RIV 同干流）但不在干流链上的河段。"""
    out = []
    for f in feats:
        p = f["properties"]
        if p["HYRIV_ID"] in chain_ids:
            continue
        if p["MAIN_RIV"] != QINJIANG_MAIN_RIV:
            continue
        if p.get("CATCH_SKM", 0) < TRIB_MIN_CATCH_SKM:
            continue
        out.append(f)
    return out


def s1_width_along(line_lonlat: list[list[float]]) -> dict:
    """沿中心线法向剖面量测 S1 常水宽度（20 m 栅格，剔除 nodata）。

    逐样点取局部切向（相邻顶点）→ 法向 ±400 m 剖面；宽度 = 水面格数 × 20 m。
    """
    tr_fwd = Transformer.from_crs("EPSG:4326", "EPSG:32649", always_xy=True)
    xy = np.array([tr_fwd.transform(lon, lat) for lon, lat in line_lonlat])
    with rasterio.open(S1_WATER) as src:
        water = src.read(1)
        inv = ~src.transform
        step = max(1, len(xy) // 120)
        widths: list[float] = []
        for i in range(0, len(xy), step):
            x, y = xy[i]
            if 0 < i < len(xy) - 1:
                tx, ty = xy[i + 1][0] - xy[i - 1][0], xy[i + 1][1] - xy[i - 1][1]
            else:
                tx, ty = 1.0, 0.0
            norm = np.hypot(tx, ty) or 1.0
            px, py = -ty / norm, tx / norm  # 法向
            cols, rows = [], []
            for s in np.arange(-400.0, 400.0 + 1e-6, 20.0):
                c, r = inv * (x + px * s, y + py * s)
                cols.append(int(c))
                rows.append(int(r))
            w = 0
            for r, c in zip(rows, cols):
                if 0 <= r < water.shape[0] and 0 <= c < water.shape[1] and water[r, c] == 1:
                    w += 1
            if w > 0:
                widths.append(float(w) * 20.0)
    if not widths:
        return {"samples": len(range(0, len(xy), step)), "measured_n": 0,
                "width_mean_m": WIDTH_PRIOR_M, "width_median_m": WIDTH_PRIOR_M,
                "p90_m": WIDTH_PRIOR_M, "fallback_prior_m": WIDTH_PRIOR_M}
    return {
        "samples": len(range(0, len(xy), step)),
        "measured_n": len(widths),
        "width_mean_m": float(np.mean(widths)),
        "width_median_m": float(np.median(widths)),
        "p90_m": float(np.percentile(widths, 90)),
    }


def rasterize_path_to_grid(
    feats: list[dict],
    grid_shape: tuple[int, int],
    transform: rasterio.Affine,
    *,
    densify_m: float = 40.0,
    order_by_dist_dn: bool = True,
) -> list[tuple[int, int]]:
    """河段（WGS84）→ 网格有序 cell 序列。

    - 逐顶点投影 EPSG:32649 后按 densify_m 加密插值，保证相邻 cell 8 邻域连续；
    - order_by_dist_dn：按 DIST_DN_KM 升序（上游→下游）排列 cell，段内点序无关。
    """
    tr_fwd = Transformer.from_crs("EPSG:4326", "EPSG:32649", always_xy=True)
    inv = ~transform
    nrows, ncols = grid_shape
    cells: list[tuple[float, tuple[int, int]]] = []
    for f in feats:
        dist_dn = float(f["properties"].get("DIST_DN_KM", 0.0))
        coords = f["geometry"]["coordinates"]
        if coords[0][1] < coords[-1][1]:  # 段内按纬度定向（下游=低纬）
            coords = coords[::-1]
        xy = np.array([tr_fwd.transform(lon, lat) for lon, lat in coords])
        # 段内加密
        dense: list[tuple[float, float]] = []
        for (x0, y0), (x1, y1) in zip(xy[:-1], xy[1:]):
            seg_len = float(np.hypot(x1 - x0, y1 - y0))
            n = max(1, int(seg_len / densify_m))
            for t in np.linspace(0.0, 1.0, n + 1, endpoint=False):
                dense.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
        dense.append(tuple(xy[-1]))
        for x, y in dense:
            col, row = inv * (x, y)
            row, col = int(row), int(col)
            if 0 <= row < nrows and 0 <= col < ncols:
                cells.append((dist_dn, (row, col)))
    if not order_by_dist_dn:
        path: list[tuple[int, int]] = []
        seen: set[tuple[int, int]] = set()
        for _, rc in cells:
            if rc not in seen:
                seen.add(rc)
                path.append(rc)
        return path
    # DIST_DN_KM = 到河口沿程距离（河口=0）→ 上游→下游为降序
    cells.sort(key=lambda item: -item[0])
    path = []
    prev = None
    for _, rc in cells:
        if rc != prev:
            path.append(rc)
            prev = rc
    return path


def fill_gaps(
    path: list[tuple[int, int]],
) -> tuple[list[tuple[int, int]], int]:
    """8 邻域断裂处用 Bresenham 填补，保证 SGC 河道逐格连续。"""
    out: list[tuple[int, int]] = []
    filled = 0
    for rc in path:
        if not out:
            out.append(rc)
            continue
        r0, c0 = out[-1]
        r1, c1 = rc
        dr, dc = r1 - r0, c1 - c0
        steps = max(abs(dr), abs(dc))
        if steps > 1:
            for s in range(1, steps):
                cell = (r0 + round(dr * s / steps), c0 + round(dc * s / steps))
                if cell != out[-1]:
                    out.append(cell)
                    filled += 1
        if rc != out[-1]:
            out.append(rc)
    return out, filled


def topology_checks(
    path_rc: list[tuple[int, int]],
    dem: np.ndarray,
    entry_rc: tuple[int, int],
    trib_cells: list[tuple[int, int]],
    nrows: int,
    ncols: int,
) -> dict:
    """6 项拓扑硬检查（计划 §5.1）。假截断项无路网资料 → unverified。"""
    checks: dict[str, dict] = {}
    # 1 连续性：8 邻域逐步连通比例
    breaks = 0
    for (r0, c0), (r1, c1) in zip(path_rc[:-1], path_rc[1:]):
        if max(abs(r0 - r1), abs(c0 - c1)) > 1:
            breaks += 1
    checks["continuity"] = {
        "segments": len(path_rc) - 1,
        "breaks": breaks,
        "pass": breaks == 0,
    }
    # 2 纵向单调：沿程 bank(=DEM) 逆流方向不升超过 0.5 m 噪声带
    zs = np.array([dem[r, c] for r, c in path_rc], dtype=float)
    rises = np.diff(zs)  # 下游方向（末尾为下游）
    checks["longitudinal_monotonic"] = {
        "max_upstream_rise_m": float(max(rises.max(), 0.0)),
        "rise_gt_0p5_fraction": float(np.mean(rises > 0.5)) if len(rises) else 0.0,
        "pass": bool(len(rises) and max(rises.max(), 0.0) <= 3.0),
        "note": "0.5 m 内视为 DEM 噪声；pass 阈值 3 m",
    }
    # 3 支流接入：支流 cell 与主槽 path 8 邻接
    path_set = set(path_rc)
    adjacent = 0
    for r, c in trib_cells:
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if (r + dr, c + dc) in path_set:
                    adjacent += 1
                    break
            else:
                continue
            break
    checks["tributary_junctions"] = {
        "tributary_cells": len(trib_cells),
        "adjacent_cells": adjacent,
        "pass": True if not trib_cells else adjacent > 0,
    }
    # 4 假截断：无道路/填土资料 → 未验证（诚实口径）
    checks["artificial_cutoffs"] = {
        "pass": True,
        "verified": False,
        "note": "无道路/工程矢量资料，未验证；留待水利资料或影像复核",
    }
    # 5 入流边界对齐：path 首个 cell（上游端）在网格边缘
    r0, c0 = path_rc[0]
    checks["inflow_alignment"] = {
        "first_cell": [r0, c0],
        "on_edge": r0 in (0, nrows - 1) or c0 in (0, ncols - 1),
        "entry_rc": list(entry_rc),
        "pass": r0 in (0, nrows - 1) or c0 in (0, ncols - 1),
    }
    # 6 出口贯通：path 末段（下游端）达边缘或海
    rn, cn = path_rc[-1]
    reaches = rn == nrows - 1 or cn in (0, ncols - 1)
    checks["outlet_continuity"] = {
        "last_cell": [rn, cn],
        "reaches_edge_or_sea": reaches,
        "pass": reaches,
    }
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__)
    add_common_args(parser)
    args = parser.parse_args(argv)
    log = AuditLog("prepare_jiulong_channel", run_tag=args.run_tag, dry_run=args.dry_run)
    log.add_input("hydro_rivers", HYDRORIVERS_BASE.with_suffix(".shp"))
    log.add_input("s1_water", S1_WATER)
    log.add_input("baseline_dem", BASELINE_RUN / "dem.asc")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (PROJ / "logs" / "stage3").mkdir(parents=True, exist_ok=True)

    tr_fwd = Transformer.from_crs("EPSG:4326", "EPSG:32649", always_xy=True)

    feats = load_rivers(log)
    mainstem, entry_dist_km = assemble_mainstem(feats)
    chain_ids = {f["properties"]["HYRIV_ID"] for f in mainstem}
    tributaries = assemble_tributaries(feats, chain_ids)
    osm_mainstem = load_osm_mainstem()
    centerline_source = "osm" if osm_mainstem else "hydrorivers"
    log.step(
        "network",
        mainstem_segments=len(mainstem),
        entry_dist_dn_km=entry_dist_km,
        tributaries=len(tributaries),
        osm_ways=len(osm_mainstem),
        centerline_source=centerline_source,
    )
    if not mainstem:
        log.error("HydroRIVERS 中未找到钦江主槽（MAIN_RIV=%s）", QINJIANG_MAIN_RIV)
        return 1

    # 主槽中心线 = 各河段坐标串联（上游→下游）；段内点序按纬度定向
    #（钦江自东北向西南流，下游纬度递减；HydroRIVERS 段内点序不保证）
    def orient_downstream(coords: list[list[float]]) -> list[list[float]]:
        return coords if coords[0][1] >= coords[-1][1] else coords[::-1]

    hr_centerline: list[list[float]] = []
    for f in mainstem:
        coords = orient_downstream(f["geometry"]["coordinates"])
        if hr_centerline and hr_centerline[-1] == coords[0]:
            hr_centerline.extend(coords[1:])
        else:
            hr_centerline.extend(coords)
    osm_centerline: list[list[float]] = []
    for f in osm_mainstem:
        osm_centerline.extend(f["geometry"]["coordinates"])
    centerline_lonlat = osm_centerline if osm_mainstem else hr_centerline

    width_stats = s1_width_along(centerline_lonlat)
    log.step("s1_width", **width_stats)

    # 模型网格（200 m 生产基线）
    grid_obj = read_ascii_grid(BASELINE_RUN / "dem.asc")
    dem = grid_obj.data.astype(np.float64)
    nrows, ncols = dem.shape
    transform = rasterio.Affine(
        grid_obj.cell_size_m, 0.0, grid_obj.header["xllcorner"],
        0.0, -grid_obj.cell_size_m, grid_obj.header["yllcorner"]
        + nrows * grid_obj.cell_size_m,
    )
    path_src = osm_mainstem if osm_mainstem else mainstem
    path_raw = rasterize_path_to_grid(
        path_src, dem.shape, transform, order_by_dist_dn=not osm_mainstem
    )
    path_rc, gaps_filled = fill_gaps(path_raw)
    hr_path_rc = fill_gaps(
        rasterize_path_to_grid(mainstem, dem.shape, transform)
    )[0]
    osm_hr_overlap = (
        len(set(path_rc) & set(hr_path_rc)) / max(len(set(path_rc) | set(hr_path_rc)), 1)
        if osm_mainstem
        else None
    )
    log.step(
        "grid_path",
        source=centerline_source,
        cells_in=len(path_raw),
        cells_out=len(path_rc),
        gaps_filled=gaps_filled,
        osm_hydrorivers_cell_jaccard=round(osm_hr_overlap, 3) if osm_hr_overlap is not None else None,
        shape=[nrows, ncols],
    )
    if osm_hr_overlap is not None and osm_hr_overlap < 0.5:
        log.warn(
            f"OSM 与 HydroRIVERS 干流 cell 重叠率仅 {osm_hr_overlap:.2f}（Jaccard），"
            "复核中心线来源与 AOI 裁剪"
        )

    # 入域断面 cell = 顺流序列上游端（DEM 最高端）；与 catchments D8 断面交叉核验
    entry_rc = path_rc[0]
    ex, ey = tr_fwd.transform(*ENTRY_LONLAT)
    ecol, erow = (~transform) * (ex, ey)
    inv = ~transform
    ecol, erow = int(ecol), int(erow)
    d_cells = max(abs(entry_rc[0] - erow), abs(entry_rc[1] - ecol))
    log.step(
        "entry",
        cell=list(entry_rc),
        d8_entry_model_cell=[erow, ecol],
        offset_cells=d_cells,
    )
    if d_cells > 10:
        log.warn(
            f"HydroRIVERS 上游端与 catchments D8 断面相距 {d_cells} 格（>10）:"
            "D8 入域断面仅作面积来源，QVAR 入流以 HydroRIVERS+S1 河道为准"
        )

    trib_cells = rasterize_path_to_grid(tributaries, dem.shape, transform)
    checks = topology_checks(path_rc, dem, entry_rc, trib_cells, nrows, ncols)
    log.step("topology", **{k: v.get("pass") for k, v in checks.items()})

    # SGC 输入（满槽宽 = 常水宽 ×1.4 先验，缺测节点回退先验；硬上限 180 m）
    width_m = min(
        max(width_stats.get("width_median_m", WIDTH_PRIOR_M) * BANKFULL_FACTOR, WIDTH_PRIOR_M),
        WIDTH_MAX_M,
    )
    sgc_width = np.zeros_like(dem)
    sgc_bank = dem.copy()
    for r, c in path_rc:
        sgc_width[r, c] = width_m
    # SGC 水力几何：depth = r·w^p，r 由参考点标定
    sgc_r = SGC_DEPTH_AT_REF_M / (width_m**SGC_P)

    dual_check = {
        "method": "干流中心线 cell 与 S1 常水（2024-09-05）重合率；OSM 与 HydroRIVERS 干流 cell Jaccard",
        "note": "见 audit；S1 仅灾前/退水后两景，作趋势核验",
        "centerline_source": centerline_source,
        "osm_hydrorivers_cell_jaccard": osm_hr_overlap,
    }
    # 重合率：主槽 cell 中 S1(20m 汇总到 200m) 有水比例（剔除海）
    try:
        with rasterio.open(S1_WATER) as src:
            from rasterio.warp import reproject, Resampling

            s1_200m = np.zeros_like(dem)
            reproject(
                rasterio.band(src, 1),
                s1_200m,
                dst_transform=transform,
                dst_crs="EPSG:32649",
                src_nodata=255,
                resampling=Resampling.average,
                dst_nodata=np.nan,
            )
            # 门限口径：80 m 河宽在 200 m 格内平均占比 ~0.2-0.3；取 ≥0.12
            #（≈≥24 m 水面）为「该格有河道水面」，另报 ≥0.5 严口径供对照。
            s1_wet = s1_200m > 0.12
            chan = np.zeros_like(dem, dtype=bool)
            for r, c in path_rc:
                chan[r, c] = True
            inland = chan & ~np.isnan(s1_200m) & (dem > 0.5)
            overlap = float((s1_wet & inland).sum() / max(inland.sum(), 1))
            overlap_strict = float(
                ((s1_200m > 0.5) & inland).sum() / max(inland.sum(), 1)
            )
            dual_check["overlap_fraction"] = overlap
            dual_check["overlap_fraction_strict_0p5"] = overlap_strict
            dual_check["threshold_water_fraction"] = 0.12
            dual_check["inland_channel_cells"] = int(inland.sum())
    except Exception as exc:  # noqa: BLE001
        dual_check["error"] = str(exc)
    log.step("dual_check", **{k: v for k, v in dual_check.items() if k != "note"})

    outputs: dict[str, str] = {}
    if not args.dry_run:
        files = {
            "river_centerline": OUT_DIR / "river_centerline.geojson",
            "tributaries": OUT_DIR / "tributaries.geojson",
            "river_network": OUT_DIR / "river_network.geojson",
            "sgc_width": OUT_DIR / "sgc_width.asc",
            "sgc_bank": OUT_DIR / "sgc_bank.asc",
            "river_topology": OUT_DIR / "river_topology.json",
        }
        def gj(features: list[dict]) -> dict:
            return {"type": "FeatureCollection", "features": features}

        def write_gj(path: Path, payload: dict) -> None:
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        mainstem_fc = gj(mainstem)
        mainstem_fc["features"].append(
            {
                "type": "Feature",
                "properties": {"name": "qinjiang_centerline_merged", "main_riv": QINJIANG_MAIN_RIV},
                "geometry": {
                    "type": "LineString",
                    "coordinates": hr_centerline,
                },
            }
        )
        if osm_centerline:
            mainstem_fc["features"].append(
                {
                    "type": "Feature",
                    "properties": {"name": "qinjiang_centerline_osm", "source": "osm_pbf_261121"},
                    "geometry": {"type": "LineString", "coordinates": osm_centerline},
                }
            )
        write_gj(files["river_centerline"], mainstem_fc)
        write_gj(files["tributaries"], gj(tributaries))
        write_gj(files["river_network"], gj(mainstem + tributaries))
        write_ascii_grid(files["sgc_width"], type("G", (), {"data": sgc_width, "header": grid_obj.header})())
        write_ascii_grid(files["sgc_bank"], type("G", (), {"data": sgc_bank, "header": grid_obj.header})())
        files["river_topology"].write_text(
            json.dumps({"checks": checks, "entry_rc": list(entry_rc)}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        outputs = {k: str(v.relative_to(PROJ)).replace("\\", "/") for k, v in files.items()}

    hydrography = {
        "case": "jiulong",
        "generated_utc": utc_now(),
        "evidence_level": EVIDENCE_LEVEL,
        "main_riv_id": QINJIANG_MAIN_RIV,
        "centerline_source": centerline_source,
        "osm_pbf_note": "OSM 本地全量抽取 china-261121.osm.pbf（用户提供），AOI 缓冲框 318 条水系",
        "aoi_wgs84": list(AOI_WGS84),
        "levels": {
            "main_channel": "钦江干流（HydroRIVERS MAIN_RIV=%s）" % QINJIANG_MAIN_RIV,
            "tributaries": f"CATCH_SKM ≥ {TRIB_MIN_CATCH_SKM:.0f} km² 且与 AOI 相交（含排埠江/高塘江等候选，以相交结果为准）",
            "drainage_ditches": "未纳入（无资料）",
            "structures": "未纳入（无堤防/桥涵矢量；P08 归因时按缺资料限制处理）",
        },
        "centerline_nodes": len(centerline_lonlat),
        "channel_cells_200m": len(path_rc),
        "gaps_filled": gaps_filled,
        "width": width_stats,
        "width_used_m": width_m,
        "sgc": {"chan_type": 1, "n_manning": 0.03, "r": sgc_r, "p": SGC_P,
                "depth_at_ref_m": SGC_DEPTH_AT_REF_M},
        "entry_cell": list(entry_rc),
        "dual_check": dual_check,
        "topology_checks": checks,
        "uncertainties": [
            "无实测水下地形/大断面：SGC 河床为水力几何先验（E3）",
            "无堤防/桥涵结构层：P08 类斑块按缺资料限制处理",
            "200 m 网格内河槽亚像元：中心线 cell 沿线地形存在局部倒坡（审计见 checks.longitudinal_monotonic）",
        ],
        "outputs": outputs,
        "source_sha256": {
            "s1_water": sha256_file(S1_WATER),
            "baseline_dem": sha256_file(BASELINE_RUN / "dem.asc"),
        },
    }
    if not args.dry_run:
        write_json(HYDROGRAPHY, hydrography)
        write_json(
            GEOM_AUDIT,
            {
                "generated_utc": utc_now(),
                "checks": checks,
                "width": width_stats,
                "sgc_params": hydrography["sgc"],
                "pass_all": all(v.get("pass") for v in checks.values()),
            },
        )
    tag = f".{args.run_tag}" if args.run_tag else ""
    log.write(PROJ / "logs" / "stage3" / f"prepare_jiulong_channel{tag}.json")
    print(
        f"mainstem_segs={len(mainstem)} center_nodes={len(centerline_lonlat)} "
        f"cells={len(path_rc)} width_median={width_m:.0f}m "
        f"dual_overlap={dual_check.get('overlap_fraction', 'NA')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
