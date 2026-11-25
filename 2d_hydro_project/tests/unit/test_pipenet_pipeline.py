"""M2 单元测试与集成测试：PipeNetGen 虚拟管网拓扑、无逆坡约束、定径与 PySWMM 求解。"""
from __future__ import annotations

from pathlib import Path

import networkx as nx
import pytest

from hydro_refine.pipenet.gravity_dag_builder import build_gravity_dag
from hydro_refine.pipenet.hydraulic_sizer import (
    compute_storm_intensity_l_s_ha,
    manning_full_flow_q,
    size_gravity_network,
)
from hydro_refine.pipenet.osm_road_extractor import (
    build_road_graph,
    generate_synthetic_road_geojson,
)
from hydro_refine.pipenet.swmm_serializer import (
    export_pipenet_geojson,
    export_swmm_inp,
    summarize_pipenet,
)


def test_manning_sizing_and_storm_intensity_physics() -> None:
    q_l_s_ha = compute_storm_intensity_l_s_ha(
        a1=15.2, c=0.72, b=11.0, n=0.75, return_period_yr=5.0, duration_min=15.0
    )
    # 广西 5 年一遇 15 min 暴雨强度约 250~450 L/(s·ha)
    assert 200.0 <= q_l_s_ha <= 500.0
    q_full = manning_full_flow_q(1.0, 0.005, manning_n=0.013)
    assert q_full > 1.5


def test_pipenet_dag_zero_negative_slopes_and_pyswmm_run(tmp_path: Path) -> None:
    bbox = (109.58, 23.07, 109.61, 23.10)
    road_fc = generate_synthetic_road_geojson(bbox, nx_streets=4, ny_streets=4)
    graph = build_road_graph(
        road_fc,
        epsg=32649,
        default_spacing_m=150.0,
        snap_tolerance_m=20.0,
    )
    assert graph.number_of_nodes() >= 16

    # 南低北高坡面 + 局部微坑扰动，验证逆坡处理与连通性
    def fake_dem(x: float, y: float) -> float:
        return 20.0 + (y - 2553000.0) * 0.002 + (0.4 if int(x) % 200 < 50 else 0.0)

    dag = build_gravity_dag(
        graph,
        epsg=32649,
        elev_fn=fake_dem,
        outfall_buffer_m=150.0,
        min_slope=0.002,
        min_cover_m=1.0,
    )
    assert nx.is_directed_acyclic_graph(dag)

    sized = size_gravity_network(
        dag,
        storm_formula={"a1": 15.2, "c": 0.72, "b": 11.0, "n": 0.75, "return_period_yr": 5.0},
    )
    summary = summarize_pipenet(sized)
    assert summary["SYNTHETIC_DATA_NOT_REAL_ASSET"] is True
    assert summary["is_dag"] is True
    assert summary["negative_slope_count"] == 0
    assert summary["n_outfalls"] >= 1
    assert summary["n_conduits"] >= 10

    # 校验每条管段设计输水能力均 >= 设计流量（含多联干管 barrels，或达到上限）
    for _u, _v, edata in sized.edges(data=True):
        assert edata["invert_start_m"] >= edata["invert_end_m"]
        assert edata["q_full_m3s"] >= edata["q_design_m3s"] - 1e-4 or edata.get("barrels", 1) >= 6

    inp_p = export_swmm_inp(sized, tmp_path / "test_net.inp", storm_rain_mm_h=45.0)
    gj_p = export_pipenet_geojson(sized, tmp_path / "test_net.geojson")
    assert inp_p.is_file() and gj_p.is_file()

    # 调用真实 EPA-SWMM5 引擎通过 PySWMM 验证生成文件 100% 可求解
    from pyswmm import Simulation

    with Simulation(str(inp_p)) as sim:
        steps = 0
        for _step in sim:
            steps += 1
        assert steps > 0
