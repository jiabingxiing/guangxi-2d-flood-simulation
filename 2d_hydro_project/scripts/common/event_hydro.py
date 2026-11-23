"""久隆钦江事件型小时级产汇流（ADR-0019 方案 B 公开数据降级实现）。

定位（计划 §5.2）：
- 方案 B：事件型小时级模型 = Green-Ampt 产流损失 + SCS 无因次单位线汇流，
  保留台风洪峰起涨陡度与峰现时滞；步长 1 h，时间轴 UTC+8 本地时。
- 日尺度 GR4J 仅作水量先验对照（复用 build_inflow_hydrology 的实现），禁止
  把日均流量均分 24 h 当作最终洪峰驱动（计划 §2.4 修正项）。

证据等级 E3：文献参数 + 公开面积推算，情景级，未率定（对齐 ADR-0016 口径）。

两条防重复计量红线（计划 §6.2，调用方必须遵守）：
1. A1 上游流域产流只进 QVAR 边界；A2 域内净雨只进 .rain；两账分立。
2. .rain 若已是 GA 净雨，run.par 严禁再叠加求解器 infiltration（互斥校验
   由 run 组装器 run_jiulong_dual_forcing.py 锁死）。
"""

from __future__ import annotations

import numpy as np

from rainfall_runoff_preprocessor.infiltration.green_ampt import green_ampt_net_rain

# SCS 无因次单位线标准表（t/Tp, q/qp），US National Engineering Handbook ch.16。
SCS_UH_TABLE: tuple[tuple[float, float], ...] = (
    (0.0, 0.000),
    (0.1, 0.030),
    (0.2, 0.100),
    (0.3, 0.190),
    (0.4, 0.310),
    (0.5, 0.470),
    (0.6, 0.660),
    (0.7, 0.820),
    (0.8, 0.930),
    (0.9, 0.990),
    (1.0, 1.000),
    (1.1, 0.970),
    (1.2, 0.890),
    (1.3, 0.750),
    (1.4, 0.560),
    (1.5, 0.390),
    (1.6, 0.230),
    (1.7, 0.120),
    (1.8, 0.050),
    (1.9, 0.020),
    (2.0, 0.000),
)

# 华南红壤 Green-Ampt 先验（Sandy Clay Loam 系，文献先验，未率定）。
# drainage_mm_h=0.0：默认无深渗（事件窗形态用途）；域内净雨等需饱和深渗口径的
# 调用处显式传 drainage_mm_h=ks_mm_h（ADR-0019）。
GA_PRIOR = {
    "ks_mm_h": 3.5,        # 饱和导水率
    "psi_mm": 300.0,       # 湿润锋吸力头
    "theta_s": 0.45,       # 饱和体积含水率
    "theta_i": 0.32,       # 台风前初始含水率（湿润）
    "f_max_mm": 120.0,     # 土壤蓄水上限（饱和超渗）
    "drainage_mm_h": 0.0,  # 饱和后深渗率（0=不启用）
}


def scs_unit_hydrograph(tp_h: float, dt_h: float = 1.0) -> np.ndarray:
    """SCS 无因次 UH 离散化，返回每 10 mm 净雨每 km² 的 m³/s 序列。

    形状取 US-NEH 标准无因次表；幅值按**体积守恒标定**：
    每 10 mm 净雨在 1 km² 上的总量 = 1e4 m³（10 mm × 1e6 m² / 1000），
    qp = 1e4 / (3600·dt_h·Σu)。 folklore 的 2.08·A/Tp 系数在 Δt→0 时与之等价，
    离散 dt=1 h 下直接标定可消除 ~29% 的截断水量损失（验收口径：水量守恒）。
    """
    if tp_h <= 0:
        raise ValueError("tp_h 必须为正")
    table_t = np.array([t for t, _ in SCS_UH_TABLE])
    table_q = np.array([q for _, q in SCS_UH_TABLE])
    n = max(1, int(np.ceil(2.0 * tp_h / dt_h)))
    u = np.interp(np.arange(n) * dt_h / tp_h, table_t, table_q)
    sum_u = float(u.sum())
    if sum_u <= 0:
        raise ValueError("UH 形状退化为零")
    qp = 1e4 / (3600.0 * dt_h * sum_u)
    return u * qp


def route_scs_uh(
    excess_mm: np.ndarray, area_km2: float, tp_h: float, dt_h: float = 1.0
) -> np.ndarray:
    """把逐时净雨（mm）经 SCS UH 汇流为流量序列（m³/s）。"""
    uh = scs_unit_hydrograph(tp_h, dt_h)
    # 每 10 mm 净雨在 1 km² 上的 UH 响应（m³/s）；km² 缩放。
    resp = uh * area_km2
    q = np.convolve(
        np.asarray(excess_mm, dtype=np.float64) / 10.0, resp
    )[: len(excess_mm) + len(uh) - 1]
    return q


def route_nash(excess_mm: np.ndarray, k_h: float, n_res: int = 3) -> np.ndarray:
    """Nash 级联（n 个线性水库，k 小时），备用汇流形态。

    离散解：Q_i = C·Q_{i-1} + (1−C)·I_i，C = exp(−dt/k)，逐级传递。
    """
    q = np.asarray(excess_mm, dtype=np.float64)
    c = np.exp(-1.0 / max(k_h, 1e-6))
    for _ in range(int(n_res)):
        out = np.empty_like(q)
        prev = 0.0
        for i, inflow in enumerate(q):
            prev = c * prev + (1.0 - c) * inflow
            out[i] = prev
        q = out
    return q


def run_event_hourly(
    p_hourly_mm: np.ndarray,
    area_km2: float,
    *,
    ga: dict | None = None,
    tp_h: float = 4.0,
    dt_h: float = 1.0,
) -> dict:
    """事件模型主入口：GA 产流损失 → SCS UH 汇流。

    返回 excess_mm、q_m3s、水量账目与 GA 统计；水量守恒
    ∫q·dt ≈ excess_mm/1000 * area_km2 * 1e6（卷积截断误差 <1%）。
    """
    ga = {**GA_PRIOR, **(ga or {})}
    p = np.asarray(p_hourly_mm, dtype=np.float64)
    ga_res = green_ampt_net_rain(
        p,
        ga["ks_mm_h"],
        ga["psi_mm"],
        ga["theta_s"],
        ga["theta_i"],
        dt_h=dt_h,
        F_max_mm=ga["f_max_mm"],
        drainage_mm_h=ga.get("drainage_mm_h", 0.0),
    )
    excess = np.asarray(ga_res.net_rain_mm.reshape(p.shape), dtype=np.float64)
    q = route_scs_uh(excess, area_km2, tp_h, dt_h)
    volume_m3 = float(np.sum(q) * dt_h * 3600.0)
    expected_m3 = float(excess.sum()) / 1000.0 * area_km2 * 1e6
    rel_dev = (
        abs(volume_m3 - expected_m3) / expected_m3 if expected_m3 > 0 else 0.0
    )
    peak_idx = int(np.argmax(q))
    return {
        "excess_mm": excess,
        "q_m3s": q,
        "peak_m3s": float(q[peak_idx]),
        "peak_t_h": peak_idx * dt_h,
        "volume_m3": volume_m3,
        "expected_volume_m3": expected_m3,
        "volume_rel_dev": rel_dev,
        "rain_total_mm": float(p.sum()),
        "excess_total_mm": float(excess.sum()),
        "runoff_coeff": (
            float(excess.sum() / p.sum()) if p.sum() > 0 else 0.0
        ),
        "ga": {
            "params": ga,
            "infil_total_mm": float(
                np.asarray(ga_res.infil_depth_mm).sum()
            ),
            "mass_balance_max_rel_err": float(ga_res.max_relative_error),
        },
        "tp_h": tp_h,
        "area_km2": area_km2,
    }


def scale_sensitivity(
    event: dict, factors: tuple[float, ...] = (0.7, 1.0, 1.4)
) -> list[dict]:
    """低/中/高三档敏感性包络（Q 缩放；水量守恒按比例保持）。"""
    return [
        {
            "factor": f,
            "peak_m3s": event["peak_m3s"] * f,
            "volume_m3": event["volume_m3"] * f,
        }
        for f in factors
    ]
