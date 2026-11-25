"""基于 GB 50014 暴雨推理公式与 Manning 满管流的虚拟管网水力定径模块。"""
from __future__ import annotations

import math
from typing import Any

import networkx as nx

DEFAULT_NOMINAL_DIAMETERS_M = (0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.2, 1.5, 1.8, 2.0, 2.4)
# 圆形满流常数: C = 4^(5/3) / pi ≈ 3.208359
_MANNING_CIRCLE_CONST = (4.0 ** (5.0 / 3.0)) / math.pi


def compute_storm_intensity_l_s_ha(
    *,
    a1: float,
    c: float,
    b: float,
    n: float,
    return_period_yr: float,
    duration_min: float,
) -> float:
    """计算设计暴雨强度 q [L/(s·ha)]。

    公式：q = 166.7 * A1 * (1 + C * log10(P)) / (t + b)^n
    """
    if return_period_yr <= 0 or duration_min <= 0:
        raise ValueError("重现期与降雨历时必须为正")
    i_mm_min = a1 * (1.0 + c * math.log10(return_period_yr)) / ((duration_min + b) ** n)
    return 166.6667 * i_mm_min


def manning_full_flow_q(diameter_m: float, slope: float, manning_n: float = 0.013) -> float:
    """计算圆形满管流设计输水能力 Q_full (m³/s)。"""
    if diameter_m <= 0 or slope <= 0 or manning_n <= 0:
        raise ValueError("管径、坡降与糙率必须为正")
    area = math.pi * (diameter_m ** 2) / 4.0
    rh = diameter_m / 4.0
    return (1.0 / manning_n) * area * (rh ** (2.0 / 3.0)) * math.sqrt(slope)


def required_pipe_diameter_m(q_m3s: float, slope: float, manning_n: float = 0.013) -> float:
    """由设计流量反算圆形满管最小理论内径 D (m)。"""
    if q_m3s <= 0:
        return 0.3
    return (_MANNING_CIRCLE_CONST * manning_n * q_m3s / math.sqrt(max(slope, 1e-5))) ** (3.0 / 8.0)


def size_gravity_network(
    dag: nx.DiGraph,
    *,
    storm_formula: dict[str, float],
    runoff_coeff_by_highway: dict[str, float] | None = None,
    default_runoff_coeff: float = 0.65,
    tributary_width_m: float = 90.0,
    inlet_tc_min: float = 10.0,
    assumed_flow_velocity_ms: float = 1.0,
    pipe_manning_n: float = 0.013,
    nominal_diameters_m: list[float] | tuple[float, ...] = DEFAULT_NOMINAL_DIAMETERS_M,
) -> nx.DiGraph:
    """沿 DAG 拓扑序汇流并依据国标公称管径序列为全部管段定径。"""
    sized = dag.copy()
    rc_map = runoff_coeff_by_highway or {}
    diam_choices = sorted(float(d) for d in nominal_diameters_m)

    # 1. 为每个节点计算局部直属汇水面积 (ha) 与径流系数 psi
    for n, ndata in sized.nodes(data=True):
        hw = str(ndata.get("highway", "residential"))
        psi = float(rc_map.get(hw, default_runoff_coeff))
        # 按所连接管段半长分配局部道路影响面积
        incident_len = sum(
            float(sized.edges[u, v]["length_m"]) for u, v in sized.out_edges(n)
        ) + sum(
            float(sized.edges[u, v]["length_m"]) for u, v in sized.in_edges(n)
        )
        local_area_m2 = max(0.5 * incident_len * tributary_width_m, 900.0)
        ndata["local_area_ha"] = round(local_area_m2 / 10000.0, 4)
        ndata["runoff_coeff"] = round(psi, 3)
        ndata["cum_psi_area_ha"] = ndata["local_area_ha"] * psi
        ndata["tc_min"] = float(inlet_tc_min)

    # 2. 按拓扑序累加下游汇水面积与集流时间，求解各管段设计流量与管径
    for u in nx.topological_sort(sized):
        udata = sized.nodes[u]
        cum_psi_a = float(udata["cum_psi_area_ha"])
        tc_u = float(udata["tc_min"])
        q_l_s_ha = compute_storm_intensity_l_s_ha(
            a1=float(storm_formula["a1"]),
            c=float(storm_formula["c"]),
            b=float(storm_formula["b"]),
            n=float(storm_formula["n"]),
            return_period_yr=float(storm_formula.get("return_period_yr", 5.0)),
            duration_min=tc_u,
        )
        q_design_m3s = (q_l_s_ha * cum_psi_a) / 1000.0
        udata["q_design_m3s"] = round(q_design_m3s, 4)

        out_edges = list(sized.out_edges(u, data=True))
        if not out_edges:
            continue

        # 树状 DAG 每个非出口节点恰有一条出边
        for _u, v, edata in out_edges:
            slope = max(float(edata["slope"]), 0.002)
            d_theory = required_pipe_diameter_m(q_design_m3s, slope, pipe_manning_n)
            dn = next((d for d in diam_choices if d >= d_theory), diam_choices[-1])
            q_full_single = manning_full_flow_q(dn, slope, pipe_manning_n)
            barrels = 1
            if q_full_single < q_design_m3s and dn >= diam_choices[-1]:
                barrels = min(6, max(1, int(math.ceil(q_design_m3s / max(q_full_single, 1e-6)))))
            q_full = q_full_single * barrels
            length_m = float(edata["length_m"])
            travel_min = length_m / (max(assumed_flow_velocity_ms, 0.3) * 60.0)

            edata["diameter_m"] = round(dn, 3)
            edata["barrels"] = int(barrels)
            edata["manning_n"] = pipe_manning_n
            edata["q_design_m3s"] = round(q_design_m3s, 4)
            edata["q_full_m3s"] = round(q_full, 4)
            edata["capacity_ratio"] = round(q_full / max(q_design_m3s, 1e-6), 3)

            # 累加至下游节点 v
            vdata = sized.nodes[v]
            vdata["cum_psi_area_ha"] = round(float(vdata["cum_psi_area_ha"]) + cum_psi_a, 5)
            vdata["tc_min"] = round(max(float(vdata["tc_min"]), tc_u + travel_min), 2)

    return sized
