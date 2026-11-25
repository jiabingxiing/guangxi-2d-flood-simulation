"""EPA-SWMM .inp 序列化器与虚拟管网 GeoJSON / 统计审计生成器。

说明：所有产出强制标注 SYNTHETIC_DATA_NOT_REAL_ASSET=true，明确由算法推导而非真实市政管网资产。
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import networkx as nx


def export_swmm_inp(
    dag: nx.DiGraph,
    out_inp: Path,
    *,
    title: str = "PipeNetGen Synthetic Drainage Network",
    storm_rain_mm_h: float = 35.0,
) -> Path:
    """将带水力定径的 DAG 导出为可直接被 PySWMM / EPA-SWMM5 运行的 .inp 文件。"""
    junctions = [(n, d) for n, d in dag.nodes(data=True) if d.get("node_type") != "outfall"]
    outfalls = [(n, d) for n, d in dag.nodes(data=True) if d.get("node_type") == "outfall"]
    conduits = list(dag.edges(data=True))

    lines: list[str] = [
        "[TITLE]",
        ";;SYNTHETIC_DATA_NOT_REAL_ASSET=true",
        title,
        "",
        "[OPTIONS]",
        "FLOW_UNITS           CMS",
        "INFILTRATION         HORTON",
        "FLOW_ROUTING         DYNWAVE",
        "INERTIAL_DAMPING     PARTIAL",
        "NORMAL_FLOW_LIMITED  BOTH",
        "VARIABLE_STEP        0.75",
        "LENGTHENING_STEP     15",
        "MIN_SURFAREA         1.167",
        "THREADS              8",
        "LINK_OFFSETS         DEPTH",
        "MIN_SLOPE            0.001",
        "START_DATE           09/05/2024",
        "START_TIME           00:00:00",
        "REPORT_START_DATE    09/05/2024",
        "REPORT_START_TIME    00:00:00",
        "END_DATE             09/05/2024",
        "END_TIME             06:00:00",
        "SWEEP_START          01/01",
        "SWEEP_END            12/31",
        "DRY_DAYS             0",
        "REPORT_STEP          00:15:00",
        "WET_STEP             00:05:00",
        "DRY_STEP             01:00:00",
        "ROUTING_STEP         0:00:30",
        "ALLOW_PONDING        YES",
        "",
        "[RAINGAGES]",
        ";;Name           Format    Interval SCF      Source",
        "RG_SYNTH         INTENSITY 1:00     1.0      TIMESERIES TS_STORM",
        "",
        "[SUBCATCHMENTS]",
        ";;Name           Rain Gage        Outlet           Area     %Imperv  Width    %Slope   CurbLen",
    ]

    for n, d in junctions:
        area_ha = max(float(d.get("local_area_ha", 0.2)), 0.05)
        imperv = min(max(float(d.get("runoff_coeff", 0.65)) * 100.0, 20.0), 95.0)
        width_m = round((area_ha * 10000.0) ** 0.5, 1)
        lines.append(
            f"SC_{n:<13} RG_SYNTH         {n:<16} {area_ha:<8.4f} {imperv:<8.1f} {width_m:<8.1f} 0.5      0"
        )

    lines += [
        "",
        "[SUBAREAS]",
        ";;Subcatchment   N-Imperv   N-Perv     S-Imperv   S-Perv     PctZero    RouteTo    PctRouted",
    ]
    for n, _d in junctions:
        lines.append(f"SC_{n:<13} 0.015      0.15       1.5        4.0        25         OUTLET")

    lines += [
        "",
        "[INFILTRATION]",
        ";;Subcatchment   MaxRate    MinRate    Decay      DryTime    MaxInfil",
    ]
    for n, _d in junctions:
        lines.append(f"SC_{n:<13} 50.0       3.0        4.0        7          0")

    lines += [
        "",
        "[JUNCTIONS]",
        ";;Name           Elevation  MaxDepth   InitDepth  SurDepth   Aponded",
    ]
    for n, d in junctions:
        inv = float(d["invert_m"])
        z = float(d["z_ground_m"])
        max_d = max(z - inv, 1.0)
        lines.append(f"{n:<16} {inv:<10.3f} {max_d:<10.3f} 0          0          500")

    lines += [
        "",
        "[OUTFALLS]",
        ";;Name           Elevation  Type       Stage Data       Gated",
    ]
    for n, d in outfalls:
        inv = float(d["invert_m"])
        lines.append(f"{n:<16} {inv:<10.3f} FREE                        NO")

    lines += [
        "",
        "[CONDUITS]",
        ";;Name           From Node        To Node          Length     Roughness  InOffset   OutOffset  InitFlow   MaxFlow",
    ]
    for u, v, d in conduits:
        pid = d["pipe_id"]
        length = max(float(d["length_m"]), 1.0)
        n_val = float(d.get("manning_n", 0.013))
        in_off = max(0.0, float(d.get("invert_start_m", dag.nodes[u]["invert_m"])) - float(dag.nodes[u]["invert_m"]))
        out_off = max(0.0, float(d.get("invert_end_m", dag.nodes[v]["invert_m"])) - float(dag.nodes[v]["invert_m"]))
        lines.append(
            f"{pid:<16} {u:<16} {v:<16} {length:<10.2f} {n_val:<10.4f} {in_off:<10.3f} {out_off:<10.3f} 0          0"
        )

    lines += [
        "",
        "[XSECTIONS]",
        ";;Link           Shape        Geom1            Geom2      Geom3      Geom4      Barrels",
    ]
    for _u, _v, d in conduits:
        pid = d["pipe_id"]
        diam = float(d.get("diameter_m", 0.4))
        barrels = int(d.get("barrels", 1))
        lines.append(f"{pid:<16} CIRCULAR     {diam:<16.3f} 0          0          0          {barrels}")

    lines += [
        "",
        "[TIMESERIES]",
        ";;Name           Date       Time       Value",
        f"TS_STORM                    0:00       {storm_rain_mm_h:.2f}",
        f"TS_STORM                    1:00       {storm_rain_mm_h * 1.4:.2f}",
        f"TS_STORM                    2:00       {storm_rain_mm_h * 0.8:.2f}",
        "TS_STORM                    3:00       0.00",
        "",
        "[COORDINATES]",
        ";;Node           X-Coord            Y-Coord",
    ]
    for n, d in dag.nodes(data=True):
        lines.append(f"{n:<16} {float(d['x']):<18.2f} {float(d['y']):<18.2f}")

    out_inp.parent.mkdir(parents=True, exist_ok=True)
    out_inp.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    return out_inp


def export_pipenet_geojson(dag: nx.DiGraph, out_geojson: Path) -> Path:
    """导出管网空间矢量 GeoJSON（EPSG:4326），强制携带虚拟资产免责标识。"""
    features: list[dict[str, Any]] = []
    for u, v, d in dag.edges(data=True):
        ud = dag.nodes[u]
        vd = dag.nodes[v]
        features.append({
            "type": "Feature",
            "properties": {
                "feature_kind": "conduit",
                "pipe_id": d["pipe_id"],
                "from_node": u,
                "to_node": v,
                "diameter_m": d.get("diameter_m"),
                "length_m": d.get("length_m"),
                "slope": d.get("slope"),
                "q_design_m3s": d.get("q_design_m3s"),
                "q_full_m3s": d.get("q_full_m3s"),
                "highway": d.get("highway"),
                "SYNTHETIC_DATA_NOT_REAL_ASSET": True,
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [[ud["lon"], ud["lat"]], [vd["lon"], vd["lat"]]],
            },
        })

    for n, d in dag.nodes(data=True):
        features.append({
            "type": "Feature",
            "properties": {
                "feature_kind": d.get("node_type", "junction"),
                "node_id": n,
                "z_ground_m": d.get("z_ground_m"),
                "invert_m": d.get("invert_m"),
                "local_area_ha": d.get("local_area_ha"),
                "q_design_m3s": d.get("q_design_m3s"),
                "lift_warning": d.get("lift_warning", False),
                "SYNTHETIC_DATA_NOT_REAL_ASSET": True,
            },
            "geometry": {
                "type": "Point",
                "coordinates": [d["lon"], d["lat"]],
            },
        })

    payload = {
        "type": "FeatureCollection",
        "name": "synthetic_storm_drainage_network",
        "metadata": {
            "SYNTHETIC_DATA_NOT_REAL_ASSET": True,
            "note": "由 PipeNetGen 算法结合公开 OSM 路网与 DEM 自动推导，非真实市政地下管网。",
        },
        "features": features,
    }
    out_geojson.parent.mkdir(parents=True, exist_ok=True)
    out_geojson.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_geojson


def summarize_pipenet(dag: nx.DiGraph) -> dict[str, Any]:
    """汇总虚拟管网拓扑与水力统计指标（用于质量检查与报告审计）。"""
    n_nodes = dag.number_of_nodes()
    n_edges = dag.number_of_edges()
    n_outfalls = sum(1 for _, d in dag.nodes(data=True) if d.get("node_type") == "outfall")
    n_junctions = n_nodes - n_outfalls
    total_len_km = sum(float(d["length_m"]) for _, _, d in dag.edges(data=True)) / 1000.0
    neg_slopes = sum(
        1
        for _, _, d in dag.edges(data=True)
        if float(d.get("invert_start_m", 0.0)) < float(d.get("invert_end_m", 0.0))
    )
    diam_counts = Counter(int(round(float(d.get("diameter_m", 0.3)) * 1000)) for _, _, d in dag.edges(data=True))
    lift_warnings = sum(1 for _, d in dag.nodes(data=True) if d.get("lift_warning"))

    return {
        "SYNTHETIC_DATA_NOT_REAL_ASSET": True,
        "is_dag": bool(nx.is_directed_acyclic_graph(dag)),
        "n_nodes": n_nodes,
        "n_junctions": n_junctions,
        "n_outfalls": n_outfalls,
        "n_conduits": n_edges,
        "total_length_km": round(total_len_km, 3),
        "negative_slope_count": neg_slopes,
        "lift_warning_nodes": lift_warnings,
        "diameter_dn_mm_histogram": {f"DN{k}": v for k, v in sorted(diam_counts.items())},
        "max_design_flow_m3s": round(
            max((float(d.get("q_design_m3s", 0.0)) for _, _, d in dag.edges(data=True)), default=0.0),
            3,
        ),
    }
