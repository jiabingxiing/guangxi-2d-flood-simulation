"""OSM 道路网抽取与雨水井/节点拓扑图构建器。

支持三种数据来源：
1. 本地中国全量 OSM PBF（`china-261121.osm.pbf`），通过 `uv run --with osmium` 抽取至缓存 GeoJSON；
2. 既有的 AOI 道路 GeoJSON 缓存；
3. Overpass API 明文 HTTP 回退或测试用合成正交道路网生成器。
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any

import networkx as nx
from pyproj import Transformer

DEFAULT_INCLUDE_HIGHWAYS = {
    "trunk",
    "primary",
    "secondary",
    "tertiary",
    "residential",
    "unclassified",
    "living_street",
}
DEFAULT_EXCLUDE_HIGHWAYS = {"footway", "cycleway", "path", "steps", "track", "pedestrian"}


def extract_roads_from_pbf(
    pbf_path: Path,
    bbox_wgs84: tuple[float, float, float, float],
    out_geojson: Path,
    *,
    include_highways: set[str] | None = None,
) -> Path:
    """调用 pyosmium 子进程从本地 .osm.pbf 抽取 AOI 道路 GeoJSON（遵循 extract_osm_jiulong_waterways 模式）。"""
    if not pbf_path.is_file():
        raise FileNotFoundError(f"OSM PBF 文件不存在：{pbf_path}")
    allowed = sorted(include_highways or DEFAULT_INCLUDE_HIGHWAYS)
    helper_code = (
        "import json, sys, osmium\n"
        "pbf, out_p = sys.argv[1], sys.argv[2]\n"
        "bbox = tuple(map(float, sys.argv[3:7]))\n"
        "allowed = set(sys.argv[7].split(','))\n"
        "feats = []\n"
        "class RoadH(osmium.SimpleHandler):\n"
        "    def way(self, w):\n"
        "        t = w.tags\n"
        "        hw = t.get('highway')\n"
        "        if hw not in allowed:\n"
        "            return\n"
        "        if t.get('bridge') == 'yes' or t.get('tunnel') == 'yes':\n"
        "            return\n"
        "        pts = []\n"
        "        for n in w.nodes:\n"
        "            if n.location.valid():\n"
        "                lon, lat = n.location.lon, n.location.lat\n"
        "                if bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]:\n"
        "                    pts.append([round(lon, 6), round(lat, 6)])\n"
        "        if len(pts) < 2:\n"
        "            return\n"
        "        feats.append({'type':'Feature','properties':{'osm_way_id':w.id,'highway':hw,'name':t.get('name','')},'geometry':{'type':'LineString','coordinates':pts}})\n"
        "RoadH().apply_file(pbf, locations=True)\n"
        "from pathlib import Path\n"
        "Path(out_p).parent.mkdir(parents=True, exist_ok=True)\n"
        "Path(out_p).write_text(json.dumps({'type':'FeatureCollection','features':feats}, ensure_ascii=False), encoding='utf-8')\n"
        "print(f'extracted_roads={len(feats)}')\n"
    )
    cmd = [
        "uv",
        "run",
        "--no-project",
        "--with",
        "osmium",
        "python",
        "-c",
        helper_code,
        str(pbf_path),
        str(out_geojson),
        str(bbox_wgs84[0]),
        str(bbox_wgs84[1]),
        str(bbox_wgs84[2]),
        str(bbox_wgs84[3]),
        ",".join(allowed),
    ]
    subprocess.run(cmd, check=True)
    return out_geojson


def generate_synthetic_road_geojson(
    bbox_wgs84: tuple[float, float, float, float],
    *,
    nx_streets: int = 5,
    ny_streets: int = 5,
) -> dict[str, Any]:
    """生成测试用合成正交道路网 GeoJSON（WGS84）。"""
    w, s, e, n = bbox_wgs84
    features: list[dict[str, Any]] = []
    wid = 1
    for i in range(nx_streets):
        lon = w + (e - w) * (i + 0.5) / nx_streets
        hw = "primary" if i == nx_streets // 2 else "residential"
        coords = [[round(lon, 6), round(s + (n - s) * j / (ny_streets - 1), 6)] for j in range(ny_streets)]
        features.append({
            "type": "Feature",
            "properties": {"osm_way_id": wid, "highway": hw, "name": f"NS_{i}"},
            "geometry": {"type": "LineString", "coordinates": coords},
        })
        wid += 1
    for j in range(ny_streets):
        lat = s + (n - s) * j / (ny_streets - 1)
        hw = "secondary" if j == ny_streets // 2 else "residential"
        coords = [[round(w + (e - w) * (i + 0.5) / nx_streets, 6), round(lat, 6)] for i in range(nx_streets)]
        features.append({
            "type": "Feature",
            "properties": {"osm_way_id": wid, "highway": hw, "name": f"EW_{j}"},
            "geometry": {"type": "LineString", "coordinates": coords},
        })
        wid += 1
    return {"type": "FeatureCollection", "features": features}


def load_road_geojson(
    geojson_path: Path,
    *,
    include_highways: set[str] | None = None,
    exclude_highways: set[str] | None = None,
) -> dict[str, Any]:
    """读取道路 GeoJSON 并按道路等级、桥梁/隧道规则过滤。"""
    doc = json.loads(geojson_path.read_text(encoding="utf-8"))
    inc = include_highways or DEFAULT_INCLUDE_HIGHWAYS
    exc = exclude_highways or DEFAULT_EXCLUDE_HIGHWAYS
    kept: list[dict[str, Any]] = []
    for feat in doc.get("features", []):
        props = feat.get("properties") or {}
        hw = str(props.get("highway", "")).strip()
        if hw in exc or (inc and hw not in inc):
            continue
        if props.get("bridge") == "yes" or props.get("tunnel") == "yes":
            continue
        geom = feat.get("geometry") or {}
        if geom.get("type") == "LineString" and len(geom.get("coordinates", [])) >= 2:
            kept.append(feat)
    return {"type": "FeatureCollection", "features": kept}


class _SpatialNodeSnapper:
    """基于空间网格哈希的 O(1) 邻近节点合并器。"""

    def __init__(self, tol_m: float) -> None:
        self.tol = max(float(tol_m), 1e-3)
        self.grid: dict[tuple[int, int], list[int]] = {}
        self.coords: list[tuple[float, float]] = []
        self.lons_lats: list[tuple[float, float]] = []
        self.highways: list[str] = []

    def get_or_add(
        self, x: float, y: float, lon: float, lat: float, highway: str
    ) -> int:
        gx = int(math.floor(x / self.tol))
        gy = int(math.floor(y / self.tol))
        best_id: int | None = None
        best_d2 = self.tol * self.tol
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for nid in self.grid.get((gx + dx, gy + dy), ()):
                    nx_, ny_ = self.coords[nid]
                    d2 = (x - nx_) ** 2 + (y - ny_) ** 2
                    if d2 <= best_d2:
                        best_d2 = d2
                        best_id = nid
        if best_id is not None:
            return best_id
        nid = len(self.coords)
        self.coords.append((x, y))
        self.lons_lats.append((lon, lat))
        self.highways.append(highway)
        self.grid.setdefault((gx, gy), []).append(nid)
        return nid


def build_road_graph(
    road_fc: dict[str, Any],
    *,
    epsg: int,
    spacing_by_highway: dict[str, float] | None = None,
    default_spacing_m: float = 65.0,
    snap_tolerance_m: float = 15.0,
) -> nx.Graph:
    """将道路矢量重投影到 UTM 平面，按道路等级等距采样并组装无向拓扑图。

    每个节点包含属性：id (N_xxxx), x, y, lon, lat, highway。
    每条边包含属性：length_m, highway。
    """
    tf = Transformer.from_crs(4326, epsg, always_xy=True)
    tf_inv = Transformer.from_crs(epsg, 4326, always_xy=True)
    spacing_map = spacing_by_highway or {}
    snapper = _SpatialNodeSnapper(snap_tolerance_m)
    edges_raw: list[tuple[int, int, float, str]] = []

    for feat in road_fc.get("features", []):
        props = feat.get("properties") or {}
        hw = str(props.get("highway", "residential"))
        step = float(spacing_map.get(hw, default_spacing_m))
        coords = feat["geometry"]["coordinates"]
        xy_pts = [tf.transform(float(lon), float(lat)) for lon, lat in coords]

        seg_nodes: list[int] = []
        for (ax, ay), (bx, by) in zip(xy_pts, xy_pts[1:]):
            seg_len = math.hypot(bx - ax, by - ay)
            if seg_len < 1e-3:
                continue
            n_sub = max(1, int(math.ceil(seg_len / step)))
            for k in range(n_sub + 1):
                if k == 0 and seg_nodes:
                    continue
                t = k / n_sub
                px = ax + t * (bx - ax)
                py = ay + t * (by - ay)
                plon, plat = tf_inv.transform(px, py)
                nid = snapper.get_or_add(px, py, plon, plat, hw)
                seg_nodes.append(nid)

        for u, v in zip(seg_nodes, seg_nodes[1:]):
            if u == v:
                continue
            ux, uy = snapper.coords[u]
            vx, vy = snapper.coords[v]
            dist = math.hypot(vx - ux, vy - uy)
            if dist >= 1.0:
                edges_raw.append((u, v, dist, hw))

    graph = nx.Graph()
    for nid, (x, y) in enumerate(snapper.coords):
        lon, lat = snapper.lons_lats[nid]
        node_name = f"N_{nid:05d}"
        graph.add_node(
            node_name,
            num_id=nid,
            x=round(x, 2),
            y=round(y, 2),
            lon=round(lon, 6),
            lat=round(lat, 6),
            highway=snapper.highways[nid],
        )

    for u, v, dist, hw in edges_raw:
        nu, nv = f"N_{u:05d}", f"N_{v:05d}"
        if not graph.has_edge(nu, nv):
            graph.add_edge(nu, nv, length_m=round(dist, 2), highway=hw)

    return graph
