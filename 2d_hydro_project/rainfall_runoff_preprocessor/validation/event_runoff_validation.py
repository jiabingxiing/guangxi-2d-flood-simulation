"""Event runoff metrics calculation and runoff inflation analysis.

Plan reference: §9 / §10.
Quantifies how zero-loss baseline artificially inflates runoff volume and peak rates.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True)
class EventRunoffMetrics:
    """Hydrologic event summary metrics.

    Attributes
    ----------
    scenario_name: Name of model scenario (e.g. 'Baseline', 'GA-Mid', 'CN-200')
    gross_rain_mm: Total precipitation depth (mm)
    net_rain_mm: Total net runoff depth (mm)
    loss_mm: Total infiltration / retention loss depth (mm)
    runoff_coefficient: C = net_rain_mm / gross_rain_mm
    total_gross_vol_m3: Total gross rainfall volume (m3) across domain
    total_net_vol_m3: Total net runoff volume (m3) across domain
    total_loss_vol_m3: Total loss volume (m3) across domain
    peak_gross_rate_mm_h: Peak gross rain intensity (mm/h)
    peak_net_rate_mm_h: Peak net runoff intensity (mm/h)
    peak_attenuation_ratio: 1.0 - (peak_net / peak_gross)
    """

    scenario_name: str
    gross_rain_mm: float
    net_rain_mm: float
    loss_mm: float
    runoff_coefficient: float
    total_gross_vol_m3: float
    total_net_vol_m3: float
    total_loss_vol_m3: float
    peak_gross_rate_mm_h: float
    peak_net_rate_mm_h: float
    peak_attenuation_ratio: float


def compute_event_metrics(
    scenario_name: str,
    P_mm: np.ndarray,
    R_net_mm: np.ndarray,
    Loss_mm: np.ndarray,
    *,
    domain_area_m2: float = 1.0e8,
) -> EventRunoffMetrics:
    """Compute event hydrological summary metrics.

    Parameters
    ----------
    scenario_name: Scenario tag
    P_mm: 1D or 3D gross rainfall array
    R_net_mm: 1D or 3D net rainfall array
    Loss_mm: 1D or 3D loss array
    domain_area_m2: Total catchment / domain area in square meters
    """
    # If 3D, compute spatial average time series
    if P_mm.ndim == 3:
        p_ts = np.mean(P_mm, axis=(1, 2))
        r_ts = np.mean(R_net_mm, axis=(1, 2))
        l_ts = np.mean(Loss_mm, axis=(1, 2))
    else:
        p_ts = P_mm
        r_ts = R_net_mm
        l_ts = Loss_mm

    tot_p = float(np.sum(p_ts))
    tot_r = float(np.sum(r_ts))
    tot_l = float(np.sum(l_ts))

    rc = tot_r / max(tot_p, 1e-6)

    vol_p = (tot_p / 1000.0) * domain_area_m2
    vol_r = (tot_r / 1000.0) * domain_area_m2
    vol_l = (tot_l / 1000.0) * domain_area_m2

    peak_p = float(np.max(p_ts))
    peak_r = float(np.max(r_ts))
    attenuation = 1.0 - (peak_r / max(peak_p, 1e-6))

    return EventRunoffMetrics(
        scenario_name=scenario_name,
        gross_rain_mm=round(tot_p, 2),
        net_rain_mm=round(tot_r, 2),
        loss_mm=round(tot_l, 2),
        runoff_coefficient=round(rc, 4),
        total_gross_vol_m3=round(vol_p, 2),
        total_net_vol_m3=round(vol_r, 2),
        total_loss_vol_m3=round(vol_l, 2),
        peak_gross_rate_mm_h=round(peak_p, 2),
        peak_net_rate_mm_h=round(peak_r, 2),
        peak_attenuation_ratio=round(attenuation, 4),
    )


def compare_runoff_inflation(
    baseline_metrics: EventRunoffMetrics,
    model_metrics: EventRunoffMetrics,
) -> dict[str, Any]:
    """Quantify runoff inflation of baseline (zero-loss) relative to a loss model.

    Returns
    -------
    dict with overestimation volume (m3), overestimation percentage (%),
    and peak intensity inflation.
    """
    v_base = baseline_metrics.total_net_vol_m3
    v_model = model_metrics.total_net_vol_m3

    overestimate_vol_m3 = v_base - v_model
    overestimate_pct = (overestimate_vol_m3 / max(v_model, 1.0)) * 100.0
    runoff_reduction_pct = (1.0 - (v_model / max(v_base, 1.0))) * 100.0

    peak_inflation_mm_h = baseline_metrics.peak_net_rate_mm_h - model_metrics.peak_net_rate_mm_h
    peak_inflation_pct = (peak_inflation_mm_h / max(model_metrics.peak_net_rate_mm_h, 1e-6)) * 100.0

    return {
        "baseline_scenario": baseline_metrics.scenario_name,
        "model_scenario": model_metrics.scenario_name,
        "baseline_net_vol_m3": v_base,
        "model_net_vol_m3": v_model,
        "overestimated_volume_m3": round(overestimate_vol_m3, 2),
        "overestimated_volume_million_m3": round(overestimate_vol_m3 / 1e6, 3),
        "runoff_inflation_pct": round(overestimate_pct, 2),
        "runoff_reduction_pct": round(runoff_reduction_pct, 2),
        "baseline_peak_mm_h": baseline_metrics.peak_net_rate_mm_h,
        "model_peak_mm_h": model_metrics.peak_net_rate_mm_h,
        "peak_rate_inflation_mm_h": round(peak_inflation_mm_h, 2),
        "peak_rate_inflation_pct": round(peak_inflation_pct, 2),
    }
