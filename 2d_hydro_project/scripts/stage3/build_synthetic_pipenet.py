"""阶段3：基于 PipeNetGen 算法合成城市/集镇虚拟雨水管网（SWMM .inp + GeoJSON）。

用法（先设 $env:PYTHONUTF8 = "1"）：
    uv --directory 2d_hydro_project run python scripts/stage3/build_synthetic_pipenet.py --case guigang --osm-pbf "<path>"
    uv --directory 2d_hydro_project run python scripts/stage3/build_synthetic_pipenet.py --case guigang
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from hydro_refine.core.paths import external, rel_posix, resolve_legacy  # noqa: E402
from hydro_refine.pipenet.gravity_dag_builder import build_gravity_dag  # noqa: E402
from hydro_refine.pipenet.hydraulic_sizer import size_gravity_network  # noqa: E402
from hydro_refine.pipenet.osm_road_extractor import (  # noqa: E402
    build_road_graph,
    extract_roads_from_pbf,
    load_road_geojson,
)
from hydro_refine.pipenet.swmm_serializer import (  # noqa: E402
    export_pipenet_geojson,
    export_swmm_inp,
    summarize_pipenet,
)
from scripts.common.grid_io import AuditLog, utc_now, write_json  # noqa: E402

RULES_MAP = {
    "guigang": PROJ / "configs" / "pipenet" / "guigang_urban_rules.yaml",
    "jiulong": PROJ / "configs" / "pipenet" / "jiulong_town_rules.yaml",
    "ningming": PROJ / "configs" / "pipenet" / "ningming_urban_rules.yaml",
}


def build_case_pipenet(
    case: str,
    *,
    osm_pbf: Path | None = None,
    roads_geojson: Path | None = None,
    refresh_osm: bool = False,
) -> dict:
    cfg = yaml.safe_load(RULES_MAP[case].read_text(encoding="utf-8"))
    epsg = int(cfg["crs_epsg"])
    bbox = tuple(float(v) for v in cfg["aoi_wgs84"])
    dem_path = resolve_legacy(cfg["dem_path"])
    waterway_path = resolve_legacy(cfg["waterway_path"])

    raw_road_cache = roads_geojson or (PROJ / "data" / "raw" / "hydro" / case / "roads_aoi.geojson")
    rex_cfg = cfg["road_extraction"]
    inc = set(rex_cfg.get("include_highways", []))
    exc = set(rex_cfg.get("exclude_highways", []))

    if not raw_road_cache.is_file() or refresh_osm:
        pbf = osm_pbf or external("OSM_PBF")
        if pbf is None or not pbf.is_file():
            raise FileNotFoundError(
                f"未找到道路缓存 {raw_road_cache} 且缺失 OSM PBF（传 --osm-pbf 或设 GX_OSM_PBF）"
            )
        extract_roads_from_pbf(pbf, bbox, raw_road_cache, include_highways=inc)

    road_fc = load_road_geojson(raw_road_cache, include_highways=inc, exclude_highways=exc)
    graph = build_road_graph(
        road_fc,
        epsg=epsg,
        spacing_by_highway=rex_cfg.get("inlet_spacing_m"),
        default_spacing_m=float(rex_cfg.get("default_spacing_m", 65.0)),
        snap_tolerance_m=float(rex_cfg.get("snap_tolerance_m", 15.0)),
    )

    topo_cfg = cfg["topology"]
    hyd_cfg = cfg["hydraulics"]
    dag = build_gravity_dag(
        graph,
        epsg=epsg,
        dem_path=dem_path,
        waterway_geojson=waterway_path,
        outfall_buffer_m=float(topo_cfg.get("outfall_buffer_m", 180.0)),
        uphill_penalty_weight=float(topo_cfg.get("uphill_penalty_weight", 200.0)),
        min_component_nodes=int(topo_cfg.get("min_component_nodes", 3)),
        min_slope=float(hyd_cfg.get("min_slope", 0.002)),
        min_cover_m=float(hyd_cfg.get("min_cover_m", 1.0)),
        max_depth_m=float(hyd_cfg.get("max_depth_m", 6.0)),
    )

    sized_dag = size_gravity_network(
        dag,
        storm_formula=hyd_cfg["storm_formula"],
        runoff_coeff_by_highway=hyd_cfg.get("runoff_coeff_by_highway"),
        default_runoff_coeff=float(hyd_cfg.get("default_runoff_coeff", 0.65)),
        tributary_width_m=float(hyd_cfg.get("tributary_width_m", 90.0)),
        inlet_tc_min=float(hyd_cfg.get("inlet_tc_min", 10.0)),
        assumed_flow_velocity_ms=float(hyd_cfg.get("assumed_flow_velocity_ms", 1.0)),
        pipe_manning_n=float(hyd_cfg.get("pipe_manning_n", 0.013)),
        nominal_diameters_m=hyd_cfg.get("nominal_diameters_m", [0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.2, 1.5]),
    )

    out_dir = PROJ / "outputs" / "pipenet" / case
    out_inp = export_swmm_inp(
        sized_dag,
        out_dir / f"{case}_synthetic.inp",
        title=f"{case} PipeNetGen Synthetic Stormwater Network (GB 50014)",
    )
    out_gj = export_pipenet_geojson(sized_dag, out_dir / f"{case}_pipes.geojson")
    summary = summarize_pipenet(sized_dag)
    summary.update({
        "case": case,
        "generated_utc": utc_now(),
        "rules_config": rel_posix(RULES_MAP[case]),
        "road_features_count": len(road_fc.get("features", [])),
        "out_inp": rel_posix(out_inp),
        "out_geojson": rel_posix(out_gj),
    })
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--case", choices=list(RULES_MAP), default="guigang")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--osm-pbf", type=Path, default=None)
    ap.add_argument("--roads-geojson", type=Path, default=None)
    ap.add_argument("--refresh-osm", action="store_true")
    args = ap.parse_args()

    log = AuditLog("build_synthetic_pipenet")
    cases = list(RULES_MAP) if args.all else [args.case]
    catalog_path = PROJ / "data" / "catalog" / "synthetic_pipe_catalogs.json"
    existing = {}
    if catalog_path.is_file():
        import json
        existing = json.loads(catalog_path.read_text(encoding="utf-8")).get("cases", {})

    for c in cases:
        summary = build_case_pipenet(
            c,
            osm_pbf=args.osm_pbf,
            roads_geojson=args.roads_geojson,
            refresh_osm=args.refresh_osm,
        )
        existing[c] = summary
        log.step("pipenet_built", **summary)
        print(
            f"[{c}] nodes={summary['n_nodes']} conduits={summary['n_conduits']} "
            f"outfalls={summary['n_outfalls']} length={summary['total_length_km']} km "
            f"neg_slopes={summary['negative_slope_count']} -> {summary['out_inp']}"
        )

    write_json(
        catalog_path,
        {
            "script": "scripts/stage3/build_synthetic_pipenet.py",
            "generated_utc": utc_now(),
            "SYNTHETIC_DATA_NOT_REAL_ASSET": True,
            "cases": existing,
        },
    )
    log.write(PROJ / "logs" / "stage3" / "build_synthetic_pipenet.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
