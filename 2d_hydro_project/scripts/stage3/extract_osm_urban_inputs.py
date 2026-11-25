"""单遍扫描本地 OSM PBF，同步抽取贵港/久隆/宁明三区路网及宁明水系缓存 GeoJSON。

路径经 --osm-pbf 或环境变量 GX_OSM_PBF 传入（不写入代码/配置）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from hydro_refine.core.paths import external  # noqa: E402

TARGETS = {
    "guigang": (109.53, 23.04, 109.68, 23.14),
    "jiulong": (108.66, 22.02, 108.77, 22.11),
    "ningming": (107.03, 22.10, 107.12, 22.18),
}
WATERWAY_BBOX = (106.95, 22.05, 107.22, 22.27)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--osm-pbf", type=Path, default=None)
    args = ap.parse_args()

    pbf = args.osm_pbf or external("OSM_PBF")
    if pbf is None or not pbf.is_file():
        print("缺少 OSM PBF：请传 --osm-pbf 或设 GX_OSM_PBF", file=sys.stderr)
        return 1

    helper = (
        "import json, sys, osmium\n"
        "from pathlib import Path\n"
        "pbf = sys.argv[1]\n"
        "out_root = Path(sys.argv[2])\n"
        "bboxes = json.loads(sys.argv[3])\n"
        "ww_bbox = json.loads(sys.argv[4])\n"
        "allowed_hw = {'trunk','primary','secondary','tertiary','residential','unclassified','living_street'}\n"
        "roads = {k: [] for k in bboxes}\n"
        "waterways = []\n"
        "class H(osmium.SimpleHandler):\n"
        "    def way(self, w):\n"
        "        t = w.tags\n"
        "        hw = t.get('highway')\n"
        "        ww = t.get('waterway')\n"
        "        if hw in allowed_hw and t.get('bridge') != 'yes' and t.get('tunnel') != 'yes':\n"
        "            if len(w.nodes) < 2:\n"
        "                return\n"
        "            loc0 = w.nodes[0].location\n"
        "            if not loc0.valid() or not (106.8 <= loc0.lon <= 109.9 and 21.8 <= loc0.lat <= 23.4):\n"
        "                return\n"
        "            pts = [[round(n.location.lon, 6), round(n.location.lat, 6)] for n in w.nodes if n.location.valid()]\n"
        "            if len(pts) >= 2:\n"
        "                for k, (w0, s0, e0, n0) in bboxes.items():\n"
        "                    sub = [p for p in pts if w0 <= p[0] <= e0 and s0 <= p[1] <= n0]\n"
        "                    if len(sub) >= 2:\n"
        "                        roads[k].append({'type':'Feature','properties':{'osm_way_id':w.id,'highway':hw,'name':t.get('name','')},'geometry':{'type':'LineString','coordinates':sub}})\n"
        "        elif ww in ('river','stream','canal','drain'):\n"
        "            if len(w.nodes) < 2:\n"
        "                return\n"
        "            loc0 = w.nodes[0].location\n"
        "            if not loc0.valid() or not (106.8 <= loc0.lon <= 107.4 and 21.9 <= loc0.lat <= 22.4):\n"
        "                return\n"
        "            pts = [[round(n.location.lon, 6), round(n.location.lat, 6)] for n in w.nodes if n.location.valid()]\n"
        "            sub = [p for p in pts if ww_bbox[0] <= p[0] <= ww_bbox[2] and ww_bbox[1] <= p[1] <= ww_bbox[3]]\n"
        "            if len(sub) >= 2:\n"
        "                waterways.append({'type':'Feature','properties':{'osm_way_id':w.id,'waterway':ww,'name':t.get('name','')},'geometry':{'type':'LineString','coordinates':sub}})\n"
        "H().apply_file(pbf, locations=True)\n"
        "for k, feats in roads.items():\n"
        "    p = out_root / k / 'roads_aoi.geojson'\n"
        "    p.parent.mkdir(parents=True, exist_ok=True)\n"
        "    p.write_text(json.dumps({'type':'FeatureCollection','features':feats}, ensure_ascii=False), encoding='utf-8')\n"
        "    print(f'{k}_roads={len(feats)}')\n"
        "wp = out_root / 'ningming' / 'osm_waterways_aoi.geojson'\n"
        "wp.parent.mkdir(parents=True, exist_ok=True)\n"
        "wp.write_text(json.dumps({'type':'FeatureCollection','features':waterways}, ensure_ascii=False), encoding='utf-8')\n"
        "print(f'ningming_waterways={len(waterways)}')\n"
    )
    out_root = PROJ / "data" / "raw" / "hydro"
    cmd = [
        "uv", "run", "--no-project", "--with", "osmium",
        "python", "-c", helper,
        str(pbf),
        str(out_root),
        json.dumps(TARGETS),
        json.dumps(WATERWAY_BBOX),
    ]
    subprocess.run(cmd, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
