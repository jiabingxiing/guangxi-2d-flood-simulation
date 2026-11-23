"""从本地 OSM PBF 提取久隆 AOI 水系（护栏 2：OSM+S1 双重核验用）。

PBF 来源：用户提供本地中国全量抽取（china-261121.osm.pbf，2026-11-21），
路径经 --osm-pbf 或环境变量 GX_OSM_PBF 传入（不写入代码/配置，规避
test_no_host_paths）。解析用 pyosmium（uv run --with osmium）。

输出缓存：data/raw/hydro/jiulong/river/osm_waterways_aoi.geojson
（AOI+0.15° 缓冲框内全部 waterway=river/stream/canal/drain 线要素）。
"""

from __future__ import annotations

import json
import os
import sys
from argparse import ArgumentParser
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

AOI_WGS84 = (108.55, 21.92, 108.90, 22.20)
BUFFER_DEG = 0.15
OUT = PROJ / "data/raw/hydro/jiulong/river/osm_waterways_aoi.geojson"


def main(argv: list[str] | None = None) -> int:
    import osmium

    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--osm-pbf", default=os.environ.get("GX_OSM_PBF", ""))
    parser.add_argument("--buffer-deg", type=float, default=BUFFER_DEG)
    args = parser.parse_args(argv)
    if not args.osm_pbf or not Path(args.osm_pbf).exists():
        print("缺少 PBF：--osm-pbf 或 GX_OSM_PBF", file=sys.stderr)
        return 1
    bbox = (
        AOI_WGS84[0] - args.buffer_deg,
        AOI_WGS84[1] - args.buffer_deg,
        AOI_WGS84[2] + args.buffer_deg,
        AOI_WGS84[3] + args.buffer_deg,
    )
    ways: list[dict] = []

    class HW(osmium.SimpleHandler):
        def way(self, w):
            tags = w.tags
            ww = tags.get("waterway")
            if ww not in ("river", "stream", "canal", "drain"):
                return
            pts = []
            for n in w.nodes:
                if n.location.valid():
                    lon, lat = n.location.lon, n.location.lat
                    if bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]:
                        pts.append([round(lon, 6), round(lat, 6)])
            if len(pts) < 2:
                return
            ways.append(
                {
                    "type": "Feature",
                    "properties": {
                        "osm_way_id": w.id,
                        "waterway": ww,
                        "name": tags.get("name", ""),
                        "name_en": tags.get("name:en", ""),
                        "tunnel": tags.get("tunnel", ""),
                        "bridge": tags.get("bridge", ""),
                    },
                    "geometry": {"type": "LineString", "coordinates": pts},
                }
            )

    handler = HW()
    # pyosmium 4.x：SimpleHandler.apply_file(locations=True) 自建节点位置索引
    handler.apply_file(args.osm_pbf, locations=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps({"type": "FeatureCollection", "features": ways}, ensure_ascii=False),
        encoding="utf-8",
    )
    named = sum(1 for f in ways if f["properties"]["name"])
    print(f"features={len(ways)} named={named} -> {OUT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
