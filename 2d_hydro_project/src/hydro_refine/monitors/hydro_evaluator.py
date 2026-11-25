"""水文时序多维精度评价器（纯 numpy 实现，避免 GPLv3 传染并兼容标准公式）。

指标包含：
- NSE（Nash-Sutcliffe Efficiency，1970）
- KGE（Kling-Gupta Efficiency，Gupta et al., 2009，含相关系数 r、变差比 alpha、均值比 beta）
- RMSE / MAE / PBIAS / Pearson r
- 洪峰水位误差 ΔH_peak、峰现时间偏差 ΔT_peak、以及基准无关相对涨水偏差 Δ(ΔH)。
"""
from __future__ import annotations

from typing import Any

import numpy as np


def _clean_pair(sim: np.ndarray | list[float], obs: np.ndarray | list[float]) -> tuple[np.ndarray, np.ndarray]:
    s = np.asarray(sim, dtype=np.float64)
    o = np.asarray(obs, dtype=np.float64)
    if s.shape != o.shape:
        raise ValueError(f"sim 形状 {s.shape} 与 obs 形状 {o.shape} 不一致")
    mask = np.isfinite(s) & np.isfinite(o)
    return s[mask], o[mask]


def rmse(sim: np.ndarray | list[float], obs: np.ndarray | list[float]) -> float | None:
    s, o = _clean_pair(sim, obs)
    if s.size == 0:
        return None
    return float(np.sqrt(np.mean((s - o) ** 2)))


def mae(sim: np.ndarray | list[float], obs: np.ndarray | list[float]) -> float | None:
    s, o = _clean_pair(sim, obs)
    if s.size == 0:
        return None
    return float(np.mean(np.abs(s - o)))


def pbias(sim: np.ndarray | list[float], obs: np.ndarray | list[float]) -> float | None:
    """百分比偏差（%）：100 * sum(sim - obs) / sum(obs)。"""
    s, o = _clean_pair(sim, obs)
    if s.size == 0:
        return None
    denom = float(np.sum(o))
    if abs(denom) < 1e-12:
        return None
    return float(100.0 * np.sum(s - o) / denom)


def pearson_r(sim: np.ndarray | list[float], obs: np.ndarray | list[float]) -> float | None:
    s, o = _clean_pair(sim, obs)
    if s.size < 2:
        return None
    std_s = float(np.std(s))
    std_o = float(np.std(o))
    if std_s < 1e-12 or std_o < 1e-12:
        return None
    return float(np.corrcoef(s, o)[0, 1])


def nse(sim: np.ndarray | list[float], obs: np.ndarray | list[float]) -> float | None:
    """Nash-Sutcliffe 效率系数（1 - Σ(sim-obs)² / Σ(obs-mean(obs))²）。"""
    s, o = _clean_pair(sim, obs)
    if s.size < 2:
        return None
    denom = float(np.sum((o - np.mean(o)) ** 2))
    if denom < 1e-12:
        return None
    num = float(np.sum((s - o) ** 2))
    return float(1.0 - num / denom)


def kge_2009(
    sim: np.ndarray | list[float], obs: np.ndarray | list[float]
) -> dict[str, float | None]:
    """Kling-Gupta 效率系数（2009）：返回 {kge, r, alpha, beta}。"""
    s, o = _clean_pair(sim, obs)
    if s.size < 2:
        return {"kge": None, "r": None, "alpha": None, "beta": None}
    mu_s = float(np.mean(s))
    mu_o = float(np.mean(o))
    sd_s = float(np.std(s))
    sd_o = float(np.std(o))
    r = pearson_r(s, o)
    if r is None or abs(mu_o) < 1e-12 or sd_o < 1e-12:
        return {"kge": None, "r": r, "alpha": None, "beta": None}
    alpha = sd_s / sd_o
    beta = mu_s / mu_o
    kge = 1.0 - float(np.sqrt((r - 1.0) ** 2 + (alpha - 1.0) ** 2 + (beta - 1.0) ** 2))
    return {
        "kge": round(kge, 4),
        "r": round(r, 4),
        "alpha": round(alpha, 4),
        "beta": round(beta, 4),
    }


def evaluate_station_fit(
    t_sim_h: list[float] | np.ndarray,
    h_sim_m: list[float | None] | np.ndarray,
    t_obs_h: list[float] | np.ndarray,
    h_obs_m: list[float] | np.ndarray,
    *,
    datum_closed: bool = False,
    sim_base_m: float | None = None,
    obs_base_m: float | None = None,
    max_match_gap_h: float = 6.0,
) -> dict[str, Any]:
    """将观测水位时点对齐至模拟时程并评估精度与涨水幅度。

    对齐方式：在模拟有效时刻上线性插值模拟水位，要求观测时刻距最近有效模拟帧 ≤ max_match_gap_h。
    当 datum_closed=False（G7 未闭合）时，显式保留 apparent_offset 诊断值而不将绝对误差误标为精度。
    """
    ts = np.asarray(t_sim_h, dtype=np.float64)
    hs = np.asarray([np.nan if v is None else float(v) for v in h_sim_m], dtype=np.float64)
    to = np.asarray(t_obs_h, dtype=np.float64)
    ho = np.asarray(h_obs_m, dtype=np.float64)

    valid_sim = np.isfinite(ts) & np.isfinite(hs)
    valid_obs = np.isfinite(to) & np.isfinite(ho)
    if not valid_sim.any() or not valid_obs.any():
        return {
            "status": "no_overlap",
            "n_matched": 0,
            "datum_closed": datum_closed,
        }

    ts_v, hs_v = ts[valid_sim], hs[valid_sim]
    order = np.argsort(ts_v)
    ts_v, hs_v = ts_v[order], hs_v[order]

    to_v, ho_v = to[valid_obs], ho[valid_obs]
    matched_sim: list[float] = []
    matched_obs: list[float] = []
    matched_t: list[float] = []
    for ti, oi in zip(to_v, ho_v):
        if ti < ts_v[0] - max_match_gap_h or ti > ts_v[-1] + max_match_gap_h:
            continue
        nearest_gap = float(np.min(np.abs(ts_v - ti)))
        if nearest_gap > max_match_gap_h:
            continue
        si = float(np.interp(ti, ts_v, hs_v))
        matched_sim.append(si)
        matched_obs.append(float(oi))
        matched_t.append(float(ti))

    if not matched_sim:
        return {
            "status": "no_overlap_within_gap",
            "n_matched": 0,
            "datum_closed": datum_closed,
        }

    ms = np.asarray(matched_sim, dtype=np.float64)
    mo = np.asarray(matched_obs, dtype=np.float64)
    diffs = ms - mo  # sim - obs

    # 相对涨水计算（基准无关）
    s_base = float(sim_base_m) if sim_base_m is not None and np.isfinite(sim_base_m) else float(hs_v[0])
    o_base = float(obs_base_m) if obs_base_m is not None and np.isfinite(obs_base_m) else float(mo[0])
    sim_peak_idx = int(np.argmax(hs_v))
    obs_peak_idx = int(np.argmax(mo))
    sim_peak_h = float(hs_v[sim_peak_idx])
    obs_peak_h = float(mo[obs_peak_idx])
    sim_peak_t = float(ts_v[sim_peak_idx])
    obs_peak_t = float(matched_t[obs_peak_idx])

    sim_rise_m = sim_peak_h - s_base
    obs_rise_m = obs_peak_h - o_base
    rise_error_m = sim_rise_m - obs_rise_m

    out: dict[str, Any] = {
        "status": "evaluated" if len(matched_sim) >= 2 else "single_or_sparse_point",
        "n_matched": len(matched_sim),
        "datum_closed": datum_closed,
        "matched_pairs": [
            {"t_h": round(t, 2), "sim_wse_m": round(s, 3), "obs_wse_m": round(o, 3)}
            for t, s, o in zip(matched_t, matched_sim, matched_obs)
        ],
        "sim_peak_wse_m": round(sim_peak_h, 3),
        "sim_peak_t_h": round(sim_peak_t, 2),
        "obs_peak_wse_m": round(obs_peak_h, 3),
        "obs_peak_t_h": round(obs_peak_t, 2),
        "peak_timing_error_h": round(sim_peak_t - obs_peak_t, 2),
        "sim_base_m": round(s_base, 3),
        "obs_base_m": round(o_base, 3),
        "sim_rise_m": round(sim_rise_m, 3),
        "obs_rise_m": round(obs_rise_m, 3),
        "rise_error_m": round(rise_error_m, 3),
        "apparent_offset_obs_minus_sim_mean_m": round(float(np.mean(mo - ms)), 3),
        "apparent_offset_spread_m": round(float(np.max(mo - ms) - np.min(mo - ms)), 3),
    }

    if datum_closed:
        kge_d = kge_2009(ms, mo)
        nse_val = nse(ms, mo)
        rmse_val = rmse(ms, mo)
        mae_val = mae(ms, mo)
        pbias_val = pbias(ms, mo)
        out.update({
            "nse": None if nse_val is None else round(nse_val, 4),
            "kge": kge_d["kge"],
            "kge_r": kge_d["r"],
            "kge_alpha": kge_d["alpha"],
            "kge_beta": kge_d["beta"],
            "rmse_m": None if rmse_val is None else round(rmse_val, 4),
            "mae_m": None if mae_val is None else round(mae_val, 4),
            "pbias_pct": None if pbias_val is None else round(pbias_val, 3),
            "peak_error_m": round(sim_peak_h - obs_peak_h, 3),
            "mean_bias_m": round(float(np.mean(diffs)), 3),
        })
    else:
        # G7 未闭合：仅对基准平移后涨水幅度序列评估波形一致性（若点数>=2），绝对水位不标精度
        rise_obs_series = mo - o_base
        rise_sim_series = ms - s_base
        nse_rise = nse(rise_sim_series, rise_obs_series)
        rmse_rise = rmse(rise_sim_series, rise_obs_series)
        out.update({
            "nse": None,
            "kge": None,
            "rmse_m": None,
            "peak_error_m": None,
            "nse_relative_rise": None if nse_rise is None else round(nse_rise, 4),
            "rmse_relative_rise_m": None if rmse_rise is None else round(rmse_rise, 4),
            "caveat": "G7 垂直基准未闭合，绝对 NSE/RMSE 保留为 null；以相对涨水幅度 ΔH 与峰现时差评估。",
        })
    return out
