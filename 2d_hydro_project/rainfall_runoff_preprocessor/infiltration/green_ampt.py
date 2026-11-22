"""Vectorized Green-Ampt infiltration and excess precipitation engine.

Plan reference: §4.2 / §6.4 / §7.2 / §8.3 / §10.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np


@dataclass(frozen=True)
class GreenAmptResult:
    """Result of grid-based Green-Ampt simulation.

    Attributes
    ----------
    net_rain_mm: 3D array of net rain depth per hour (time, rows, cols), mm
    infil_depth_mm: 3D array of actual infiltration depth per hour (time, rows, cols), mm
    cumulative_infil_mm: 3D or 2D array of final cumulative infiltration, mm
    mass_balance_error_mm: 3D array of |P - (R_net + I)|, mm
    max_relative_error: Maximum relative error across all active cells
    """

    net_rain_mm: np.ndarray
    infil_depth_mm: np.ndarray
    cumulative_infil_mm: np.ndarray
    mass_balance_error_mm: np.ndarray
    max_relative_error: float


def green_ampt_net_rain(
    P_mm: np.ndarray,
    Ks_mm_h: np.ndarray,
    psi_mm: np.ndarray,
    theta_s: np.ndarray,
    theta_i: np.ndarray,
    *,
    dt_h: float = 1.0,
    substep_min: float = 1.0,
    F_max_mm: np.ndarray | float | None = None,
    drainage_mm_h: float = 0.0,
) -> GreenAmptResult:
    """Calculate grid-based Green-Ampt infiltration and net rainfall.

    Parameters
    ----------
    P_mm: Rainfall depth per time step (time, rows, cols) or (time,), mm.
    Ks_mm_h: Saturated hydraulic conductivity (rows, cols) or scalar, mm/hour.
    psi_mm: Wetting front suction head (rows, cols) or scalar, mm.
    theta_s: Saturated volumetric water content (rows, cols) or scalar.
    theta_i: Initial volumetric water content (rows, cols) or scalar.
    dt_h: Time step of P_mm in hours (default 1.0).
    substep_min: Sub-stepping interval in minutes (default 1.0 min).
    F_max_mm: Optional finite soil storage capacity limit (mm) for shallow groundwater
              or saturation-excess runoff. When F >= F_max, infiltration capacity drops to drainage_mm_h.
    drainage_mm_h: Deep percolation / drainage rate once saturated (default 0.0).

    Returns
    -------
    GreenAmptResult container with net rain, infiltration depths, and mass balance.
    """
    P = np.asarray(P_mm, dtype=np.float64)
    is_1d = (P.ndim == 1)
    if is_1d:
        # Reshape (time,) -> (time, 1, 1)
        P = P[:, None, None]

    n_time, rows, cols = P.shape
    grid_shape = (rows, cols)

    Ks = np.broadcast_to(np.asarray(Ks_mm_h, dtype=np.float64), grid_shape).copy()
    psi = np.broadcast_to(np.asarray(psi_mm, dtype=np.float64), grid_shape).copy()
    ts = np.broadcast_to(np.asarray(theta_s, dtype=np.float64), grid_shape).copy()
    ti = np.broadcast_to(np.asarray(theta_i, dtype=np.float64), grid_shape).copy()

    delta_theta = np.maximum(1e-4, ts - ti)
    psi_delta_theta = psi * delta_theta

    if F_max_mm is not None:
        F_cap = np.broadcast_to(np.asarray(F_max_mm, dtype=np.float64), grid_shape).copy()
    else:
        F_cap = None

    # Sub-stepping setup
    dt_sub_h = substep_min / 60.0
    n_substeps = max(1, int(round(dt_h / dt_sub_h)))
    actual_dt_sub_h = dt_h / n_substeps

    # Initialize state
    F_cum = np.zeros(grid_shape, dtype=np.float64)
    net_rain_all = np.zeros((n_time, rows, cols), dtype=np.float64)
    infil_all = np.zeros((n_time, rows, cols), dtype=np.float64)

    # Time integration loop
    for t in range(n_time):
        step_P = P[t]  # (rows, cols), mm in dt_h
        if np.max(step_P) <= 1e-9:
            # Zero rain during this step: no infiltration, no excess rain
            continue

        r_rate = step_P / dt_h  # mm/hour

        step_net_rain = np.zeros(grid_shape, dtype=np.float64)
        step_infil = np.zeros(grid_shape, dtype=np.float64)

        for _ in range(n_substeps):
            # Infiltration capacity fc (mm/h)
            # If F == 0, fc is practically infinite for the first tiny sub-step
            with np.errstate(divide="ignore", invalid="ignore"):
                fc = np.where(
                    F_cum <= 1e-6,
                    1e7,  # Effectively infinite initial capacity
                    Ks * (1.0 + psi_delta_theta / F_cum),
                )

            # Saturated storage cap check
            if F_cap is not None:
                saturated = F_cum >= F_cap
                fc = np.where(saturated, drainage_mm_h, fc)

            # Actual infiltration rate in sub-step (mm/h)
            f_rate = np.minimum(r_rate, fc)

            # Cap infiltration if storage capacity would be exceeded in this sub-step
            if F_cap is not None:
                rem_storage = np.maximum(0.0, F_cap - F_cum)
                max_f_rate = rem_storage / actual_dt_sub_h + drainage_mm_h
                f_rate = np.minimum(f_rate, max_f_rate)

            # Infiltration depth and excess rain depth in sub-step
            sub_infil = f_rate * actual_dt_sub_h
            sub_net = (r_rate - f_rate) * actual_dt_sub_h
            sub_net = np.maximum(0.0, sub_net)

            # Update cumulative infiltration
            F_cum += sub_infil
            step_infil += sub_infil
            step_net_rain += sub_net

        net_rain_all[t] = step_net_rain
        infil_all[t] = step_infil

    # Mass balance audit
    mb_error = np.abs(P - (net_rain_all + infil_all))
    max_err = float(np.max(mb_error))
    total_P = float(np.sum(P))
    rel_err = max_err / max(total_P, 1.0)

    if is_1d:
        net_rain_all = net_rain_all[:, 0, 0]
        infil_all = infil_all[:, 0, 0]
        mb_error = mb_error[:, 0, 0]

    return GreenAmptResult(
        net_rain_mm=net_rain_all,
        infil_depth_mm=infil_all,
        cumulative_infil_mm=F_cum,
        mass_balance_error_mm=mb_error,
        max_relative_error=rel_err,
    )


def green_ampt_grid_with_landcover(
    P_mm: np.ndarray,
    soil: Any,
    fractions: Any,
    theta_i: np.ndarray,
    *,
    dt_h: float = 1.0,
    substep_min: float = 1.0,
    apply_storage_cap: bool = True,
) -> GreenAmptResult:
    """Calculate Green-Ampt net rain with sub-grid land cover fractions.

    Plan reference §6.4: R_net = sum(f_i * R_net,i)
    - Impervious fraction generates 100% net rain: R_net,imp = P
    - Water bodies generate 100% net rain: R_net,water = P
    - Pervious categories infiltrate via Green-Ampt
    """
    P = np.asarray(P_mm, dtype=np.float64)
    if P.ndim == 1:
        P = P[:, None, None]

    n_time, rows, cols = P.shape
    grid_shape = (rows, cols)

    # Base pervious Green-Ampt
    F_cap = (soil.effective_depth_m * (soil.theta_s - theta_i) * 1000.0) if apply_storage_cap else None

    pervious_res = green_ampt_net_rain(
        P_mm=P,
        Ks_mm_h=soil.ks_mm_h,
        psi_mm=soil.psi_mm,
        theta_s=soil.theta_s,
        theta_i=theta_i,
        dt_h=dt_h,
        substep_min=substep_min,
        F_max_mm=F_cap,
    )

    f_imp = fractions.impervious_fraction  # (rows, cols)
    f_pervious = np.maximum(0.0, 1.0 - f_imp)

    # Composite net rain and infiltration
    # Impervious: R_net = P, Infil = 0
    # Pervious: R_net = pervious_res.net_rain_mm, Infil = pervious_res.infil_depth_mm
    net_rain_composite = f_imp[None, :, :] * P + f_pervious[None, :, :] * pervious_res.net_rain_mm
    infil_composite = f_pervious[None, :, :] * pervious_res.infil_depth_mm

    mb_error = np.abs(P - (net_rain_composite + infil_composite))
    max_err = float(np.max(mb_error))
    total_P = float(np.sum(P))
    rel_err = max_err / max(total_P, 1.0)

    return GreenAmptResult(
        net_rain_mm=net_rain_composite,
        infil_depth_mm=infil_composite,
        cumulative_infil_mm=pervious_res.cumulative_infil_mm * f_pervious,
        mass_balance_error_mm=mb_error,
        max_relative_error=rel_err,
    )
