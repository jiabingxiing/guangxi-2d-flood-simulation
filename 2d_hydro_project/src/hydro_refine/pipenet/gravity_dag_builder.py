"""基于 DEM 高程与重力坡降的有向管网拓扑构建器（借鉴 SWMManywhere / pysewer）。

核心保证：
1. 从水系受纳水体（河道中心线）周边识别排放口（Outfalls）；若孤立连通分量无邻近河道，
   自动选择该分量最低高程节点作为局部受纳出口，确保 100% 拓扑连通；
2. 采用多源有向 Dijkstra 最小成本汇水生成树（对逆坡流向施加高额惩罚），
   形成向排放口汇聚的有向无环图（DAG）；
3. 沿拓扑序从末端雨水口向下游单调设定管底标高（invert），绝对避免逆坡管段（zero negative slope）。
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable

import networkx as nx
import numpy as np
import rasterio
from pyproj import Transformer

from scripts.common.grid_io import read_ascii_grid


def sample_dem_elevations(
    coords_xy: list[tuple[float, float]],
    *,
    dem_path: Path | None = None,
    elev_fn: Callable[[float, float], float] | None = None,
) -> list[float]:
    """为节点平面坐标采样 DEM 地面高程。支持 GeoTIFF、ESRI ASCII (.asc) 或注入回调（单测用）。"""
    if elev_fn is not None:
        return [float(elev_fn(x, y)) for x, y in coords_xy]
    if dem_path is None or not dem_path.is_file():
        raise FileNotFoundError(f"DEM 文件不存在：{dem_path}")

    suffix = dem_path.suffix.lower()
    if suffix in (".asc", ".txt"):
        grid = read_ascii_grid(dem_path, nodata_to_nan=True)
        hdr = grid.header
        data = grid.data
        cell = grid.cell_size_m
        xll = float(hdr["xllcorner"])
        yll = float(hdr["yllcorner"])
        nrows, ncols = grid.shape
        out: list[float] = []
        valid_median = float(np.nanmedian(data)) if np.isfinite(data).any() else 20.0
        for x, y in coords_xy:
            c = min(max(int((x - xll) / cell), 0), ncols - 1)
            r = min(max(int((yll + nrows * cell - y) / cell), 0), nrows - 1)
            z = float(data[r, c])
            out.append(z if np.isfinite(z) else valid_median)
        return out

    with rasterio.open(dem_path) as ds:
        band = ds.read(1).astype(np.float64)
        nodata = ds.nodata
        if nodata is not None:
            band = np.where(band == nodata, np.nan, band)
        valid_median = float(np.nanmedian(band)) if np.isfinite(band).any() else 20.0
        tr = ds.transform
        nrows, ncols = band.shape
        out = []
        for x, y in coords_xy:
            col = min(max(int((x - tr.c) / tr.a), 0), ncols - 1)
            row = min(max(int((y - tr.f) / tr.e), 0), nrows - 1)
            z = float(band[row, col])
            out.append(z if np.isfinite(z) else valid_median)
        return out


def _load_waterway_pts_utm(waterway_geojson: Path | None, epsg: int) -> np.ndarray:
    if waterway_geojson is None or not waterway_geojson.is_file():
        return np.empty((0, 2), dtype=np.float64)
    doc = json.loads(waterway_geojson.read_text(encoding="utf-8"))
    features = [doc] if doc.get("type") == "Feature" else doc.get("features", [])
    tf = Transformer.from_crs(4326, epsg, always_xy=True)
    pts: list[tuple[float, float]] = []
    for feat in features:
        geom = feat.get("geometry", feat)
        gtype = geom.get("type")
        coords = geom.get("coordinates", [])
        if gtype == "LineString":
            for lon, lat in coords:
                pts.append(tf.transform(float(lon), float(lat)))
        elif gtype == "MultiLineString":
            for line in coords:
                for lon, lat in line:
                    pts.append(tf.transform(float(lon), float(lat)))
    return np.asarray(pts, dtype=np.float64) if pts else np.empty((0, 2), dtype=np.float64)


def build_gravity_dag(
    road_graph: nx.Graph,
    *,
    epsg: int,
    dem_path: Path | None = None,
    elev_fn: Callable[[float, float], float] | None = None,
    waterway_geojson: Path | None = None,
    outfall_buffer_m: float = 180.0,
    uphill_penalty_weight: float = 200.0,
    min_component_nodes: int = 3,
    min_slope: float = 0.002,
    min_cover_m: float = 1.0,
    max_depth_m: float = 6.0,
) -> nx.DiGraph:
    """由无向道路图与 DEM 构建严格无逆坡的重力流雨水有向无环图（DAG）。"""
    g = road_graph.copy()

    # 剔除过小孤立碎段
    for comp in list(nx.connected_components(g)):
        if len(comp) < min_component_nodes:
            g.remove_nodes_from(comp)

    if g.number_of_nodes() == 0:
        raise ValueError("过滤孤立微段后道路图为空，请检查 AOI 与提取规则")

    nodes_ordered = list(g.nodes())
    xy_list = [(float(g.nodes[n]["x"]), float(g.nodes[n]["y"])) for n in nodes_ordered]
    elevs = sample_dem_elevations(xy_list, dem_path=dem_path, elev_fn=elev_fn)
    for n, z in zip(nodes_ordered, elevs):
        g.nodes[n]["z_ground_m"] = round(float(z), 3)

    # 识别受纳水体排放口（Outfall）
    riv_pts = _load_waterway_pts_utm(waterway_geojson, epsg)
    outfalls: set[str] = set()
    if len(riv_pts) > 0:
        node_xy = np.asarray(xy_list, dtype=np.float64)
        # 分块计算最近河距，防止超大规模节点矩阵内存膨胀
        for start in range(0, len(node_xy), 2000):
            chunk = node_xy[start : start + 2000]
            d2 = (chunk[:, 0:1] - riv_pts[:, 0:1].T) ** 2 + (
                chunk[:, 1:2] - riv_pts[:, 1:2].T
            ) ** 2
            min_d = np.sqrt(np.min(d2, axis=1))
            for k, d in enumerate(min_d):
                if float(d) <= outfall_buffer_m:
                    outfalls.add(nodes_ordered[start + k])

    # 对每个连通分量保证至少有 1 个出口；若无河道命中则取该分量最低点为本地排口
    for comp in nx.connected_components(g):
        if not any(n in outfalls for n in comp):
            lowest = min(comp, key=lambda n: g.nodes[n]["z_ground_m"])
            outfalls.add(lowest)

    # 构造带方向惩罚的有向反向搜索图（从 outfall 逆流向上游搜索最短加权排水路径）
    rev_search = nx.DiGraph()
    for n, data in g.nodes(data=True):
        rev_search.add_node(n, **data)

    for u, v, edata in g.edges(data=True):
        length = max(float(edata["length_m"]), 1.0)
        zu = float(g.nodes[u]["z_ground_m"])
        zv = float(g.nodes[v]["z_ground_m"])
        # 水流 u -> v 时，若 zv > zu（逆坡），惩罚加权
        uphill_uv = max(0.0, (zv - zu) / length)
        uphill_vu = max(0.0, (zu - zv) / length)
        cost_uv = length * (1.0 + uphill_penalty_weight * uphill_uv)
        cost_vu = length * (1.0 + uphill_penalty_weight * uphill_vu)
        # 在反向搜索图中：v -> u 的边权对应水流 u -> v 的代价
        rev_search.add_edge(v, u, weight=cost_uv)
        rev_search.add_edge(u, v, weight=cost_vu)

    _dists, paths = nx.multi_source_dijkstra(rev_search, sources=outfalls, weight="weight")

    dag = nx.DiGraph()
    for n, data in g.nodes(data=True):
        dag.add_node(
            n,
            **data,
            node_type="junction",
            is_outlet_manhole=bool(n in outfalls),
            invert_m=round(float(data["z_ground_m"]) - min_cover_m - 0.3, 3),
            lift_warning=False,
        )

    for n in nodes_ordered:
        if n in outfalls:
            continue
        rev_path = paths.get(n)
        if not rev_path or len(rev_path) < 2:
            continue
        # rev_path[-1] == n, rev_path[-2] 是 n 的直接下游节点
        downstream = rev_path[-2]
        edata = g.get_edge_data(n, downstream)
        length = float(edata["length_m"])
        if not dag.has_edge(n, downstream):
            dag.add_edge(
                n,
                downstream,
                pipe_id=f"P_{n}_{downstream}",
                length_m=round(length, 2),
                highway=edata.get("highway", "residential"),
            )

    # 为每个河道排放口检查井附加唯一 1-to-1 终端 Outfall 节点（满足 EPA-SWMM5 出口单连接约束）
    outfall_slope = max(min_slope, 0.005)
    outfall_len_m = 10.0
    for n in sorted(outfalls):
        ndata = dag.nodes[n]
        out_id = f"OUT_{n}"
        dag.add_node(
            out_id,
            num_id=-1,
            x=round(float(ndata["x"]) + 5.0, 2),
            y=round(float(ndata["y"]), 2),
            lon=ndata["lon"],
            lat=ndata["lat"],
            highway=ndata.get("highway", "residential"),
            z_ground_m=ndata["z_ground_m"],
            node_type="outfall",
            is_outlet_manhole=False,
            invert_m=round(float(ndata["invert_m"]) - outfall_slope * outfall_len_m, 3),
            lift_warning=False,
        )
        dag.add_edge(
            n,
            out_id,
            pipe_id=f"PO_{n}",
            length_m=outfall_len_m,
            highway=ndata.get("highway", "residential"),
        )

    if not nx.is_directed_acyclic_graph(dag):
        raise RuntimeError("生成的管网拓扑存在环路，违反重力流 DAG 约束")

    # 拓扑序（上游叶节点 -> 下游出口）单调设定管底标高，数学保证无逆坡
    for u in nx.topological_sort(dag):
        inv_u = float(dag.nodes[u]["invert_m"])
        z_u = float(dag.nodes[u]["z_ground_m"])
        if z_u - inv_u > max_depth_m:
            dag.nodes[u]["lift_warning"] = True
        for _u, v, edata in dag.out_edges(u, data=True):
            L = float(edata["length_m"])
            required_max_inv_v = inv_u - min_slope * L
            cur_inv_v = float(dag.nodes[v]["invert_m"])
            if cur_inv_v > required_max_inv_v:
                dag.nodes[v]["invert_m"] = round(required_max_inv_v, 3)

    # 写回每条管段起止标高与水力坡降
    for u, v, edata in dag.edges(data=True):
        inv_u = float(dag.nodes[u]["invert_m"])
        inv_v = float(dag.nodes[v]["invert_m"])
        L = max(float(edata["length_m"]), 1.0)
        slope = (inv_u - inv_v) / L
        edata["invert_start_m"] = round(inv_u, 3)
        edata["invert_end_m"] = round(inv_v, 3)
        edata["slope"] = round(max(slope, min_slope), 6)

    return dag
