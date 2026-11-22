"""Vectorized Horton infiltration capacity model.

Plan reference: §4.3 / §5 / §7.4 / §10.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True)
class HortonResult:
    """Result of grid-based Horton infiltration simulation.

    Attributes
    ----------
    net_rain_mm: 3D array of net rain depth per hour (time, rows, cols), mm
    infil_depth_mm: 3D array of infiltration depth per hour (time, rows, cols), mm
    cumulative_infil_mm: 3D or 2D array of final cumulative infiltration, mm
    mass_balance_error_mm: 3D array of |P - (R_net + I)|, mm
    max_relative_error: Maximum relative error
    """

    net_rain_mm: np.ndarray
    infil_depth_mm: np.ndarray
    cumulative_infil_mm: np.ndarray
    mass_balance_error_mm: np.ndarray
    max_relative_error: float


def horton_net_rain(
    P_mm: np.ndarray,
    f0_mm_h: np.ndarray,
    fc_mm_h: np.ndarray,
    k_h_inv: np.ndarray | float = 2.0,
    *,
    dt_h: float = 1.0,
    substep_min: float = 1.0,
) -> HortonResult:
    """Calculate Horton exponential decay infiltration and net rainfall.

    Plan reference: §4.3.1:
      f(t) = fc + (f0 - fc) * exp(-k * t)
      f_actual = min(r, f(t))
      r_net = max(0, r - f_actual)

    Parameters
    ----------
    P_mm: (time, rows, cols) or (time,), rainfall depth per time step, mm.
    f0_mm_h: Initial infiltration capacity (rows, cols) or scalar, mm/h.
    fc_mm_h: Ultimate steady infiltration capacity (rows, cols) or scalar, mm/h.
    k_h_inv: Decay coefficient (1/hour).
    dt_h: Hourly step.
    substep_min: Integration sub-step in minutes.
    """
    P = np.asarray(P_mm, dtype=np.float64)
    is_1d = (P.ndim == 1)
    if is_1d:
        P = P[:, None, None]

    n_time, rows, cols = P.shape
    grid_shape = (rows, cols)

    f0 = np.broadcast_to(np.asarray(f0_mm_h, dtype=np.float64), grid_shape).copy()
    fc = np.broadcast_to(np.asarray(fc_mm_h, dtype=np.float64), grid_shape).copy()
    k = np.broadcast_to(np.asarray(k_h_inv, dtype=np.float64), grid_shape).copy()

    dt_sub_h = substep_min / 60.0
    n_substeps = max(1, int(round(dt_h / dt_sub_h)))
    actual_dt_sub_h = dt_h / n_substeps

    net_rain_all = np.zeros((n_time, rows, cols), dtype=np.float64)
    infil_all = np.zeros((n_time, rows, cols), dtype=np.float64)
    F_cum = np.zeros(grid_shape, dtype=np.float64)

    current_t_h = 0.0

    for t in range(n_time):
        step_P = P[t]
        if np.max(step_P) <= 1e-9:
            current_t_h += dt_h
            continue

        r_rate = step_P / dt_h

        step_net_rain = np.zeros(grid_shape, dtype=np.float64)
        step_infil = np.zeros(grid_shape, dtype=np.float64)

        for s in range(n_substeps):
            sub_t = current_t_h + (s + 0.5) * actual_dt_sub_h
            # Infiltration capacity at sub_t
            decay = np.exp(-k * sub_t)
            f_cap = fc + (f0 - fc) * decay

            f_actual = np.minimum(r_rate, f_cap)
            sub_infil = f_actual * actual_dt_sub_h
            sub_net = (r_rate - f_actual) * actual_dt_sub_h
            sub_net = np.maximum(0.0, sub_net)

            step_infil += sub_infil
            step_net_rain += sub_net
            F_cum += sub_infil

        current_t_h += dt_h
        net_rain_all[t] = step_net_rain
        infil_all[t] = step_infil

    mb_error = np.abs(P - (net_rain_all + infil_all))
    max_err = float(np.max(mb_error))
    total_P = float(np.sum(P))
    rel_err = max_err / max(total_P, 1.0)

    if is_1d:
        net_rain_all = net_rain_all[:, 0, 0]
        infil_all = infil_all[:, 0, 0]
        mb_error = mb_error[:, 0, 0]

    return HortonResult(
        net_rain_mm=net_rain_all,
        infil_depth_mm=infil_all,
        cumulative_infil_mm=F_cum,
        mass_balance_error_mm=mb_error,
        max_relative_error=rel_err,
    )


def horton_grid_with_landcover(
    P_mm: np.ndarray,
    fractions: Any,
    hsg_grid: np.ndarray,
    horton_defaults: dict[str, dict[str, float]],
    *,
    dt_h: float = 1.0,
    substep_min: float = 1.0,
) -> HortonResult:
    """Calculate Horton net rain with sub-grid land cover fractions."""
    P = np.asarray(P_mm, dtype=np.float64)
    if P.ndim == 1:
        P = P[:, None, None]

    n_time, rows, cols = P.shape
    grid_shape = (rows, cols)

    # Build spatial f0, fc, k from HSG
    f0_arr = np.full(grid_shape, 125.0, dtype=np.float64)
    fc_arr = np.full(grid_shape, 6.3, dtype=np.float64)
    k_arr = np.full(grid_shape, 2.0, dtype=np.float64)

    hsg_map = {1: "A", 2: "B", 3: "C", 4: "D"}
    for code, letter in hsg_map.items():
        params = horton_defaults.get(letter, {"f0_mm_h": 125.0, "fc_mm_h": 6.3, "k_h_inv": 2.0})
        mask = (hsg_grid == code)
        f0_arr[mask] = params["f0_mm_h"]
        fc_arr[mask] = params["fc_mm_h"]
        k_arr[mask] = params["k_h_inv"]

    pervious_res = horton_net_rain(
        P_mm=P,
        f0_mm_h=f0_arr,
        fc_mm_h=fc_arr,
        k_h_inv=k_arr,
        dt_h=dt_h,
        substep_min=substep_min,
    )

    f_imp = fractions.impervious_fraction
    f_perv = np.maximum(0.0, 1.0 - f_imp)

    net_rain_composite = f_imp[None, :, :] * P + f_perv[None, :, :] * pervious_res.net_rain_mm
    infil_composite = f_perv[None, :, :] * pervious_res.infil_depth_mm

    mb_error = np.abs(P - (net_rain_composite + infil_composite))
    max_err = float(np.max(mb_error))
    total_P = float(np.sum(P))
    rel_err = max_err / max(total_P, 1.0)

    return HortonResult(
        net_rain_mm=net_rain_composite,
        infil_depth_mm=infil_composite,
        cumulative_infil_mm=pervious_res.cumulative_infil_mm * f_perv,
        mass_balance_error_mm=mb_error,
        max_relative_error=rel_err,
    )
