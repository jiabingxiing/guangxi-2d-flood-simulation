"""PipeNetGen 虚拟地下雨水管网合成、有向重力拓扑与水力尺寸计算包。"""
from __future__ import annotations

from hydro_refine.pipenet.gravity_dag_builder import (
    build_gravity_dag,
    sample_dem_elevations,
)
from hydro_refine.pipenet.hydraulic_sizer import (
    compute_storm_intensity_l_s_ha,
    manning_full_flow_q,
    size_gravity_network,
)
from hydro_refine.pipenet.osm_road_extractor import (
    build_road_graph,
    extract_roads_from_pbf,
    generate_synthetic_road_geojson,
    load_road_geojson,
)
from hydro_refine.pipenet.swmm_serializer import (
    export_pipenet_geojson,
    export_swmm_inp,
    summarize_pipenet,
)

__all__ = [
    "build_gravity_dag",
    "build_road_graph",
    "compute_storm_intensity_l_s_ha",
    "export_pipenet_geojson",
    "export_swmm_inp",
    "extract_roads_from_pbf",
    "generate_synthetic_road_geojson",
    "load_road_geojson",
    "manning_full_flow_q",
    "sample_dem_elevations",
    "size_gravity_network",
    "summarize_pipenet",
]
