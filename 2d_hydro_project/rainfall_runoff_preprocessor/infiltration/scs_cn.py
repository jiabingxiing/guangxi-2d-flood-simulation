"""Vectorized SCS-CN runoff curve number and cumulative difference engine.

Plan reference: §4.1 / §6.4 / §7.3 / §10.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True)
class SCSCNResult:
    """Result of grid-based SCS-CN simulation.

    Attributes
    ----------
    net_rain_mm: 3D array of net rain depth per hour (time, rows, cols), mm
    infil_loss_mm: 3D array of initial + infiltration loss per hour (time, rows, cols), mm
    cumulative_runoff_mm: 3D or 2D array of final cumulative runoff depth, mm
    mass_balance_error_mm: 3D array of |P - (R_net + Loss)|, mm
    max_relative_error: Maximum relative error
    """

    net_rain_mm: np.ndarray
    infil_loss_mm: np.ndarray
    cumulative_runoff_mm: np.ndarray
    mass_balance_error_mm: np.ndarray
    max_relative_error: float


def scs_cn_net_rain(
    P_mm: np.ndarray,
    CN: np.ndarray,
    *,
    lam: float = 0.20,
) -> SCSCNResult:
    """Calculate SCS-CN cumulative event runoff converted to incremental net rain.

    Plan reference: §4.1.4 / §7.3.
    Q_cum(t) = (P_cum - Ia)^2 / (P_cum + (1 - lam) * S) for P_cum > Ia, else 0
    R_net(t) = Q_cum(t) - Q_cum(t-1)

    Parameters
    ----------
    P_mm: (time, rows, cols) or (time,), rainfall depth per time step, mm.
    CN: (rows, cols) or scalar, curve number grid in [1, 100].
    lam: Initial abstraction ratio, typically 0.20 or 0.05.

    Returns
    -------
    SCSCNResult with incremental net rain and losses.
    """
    P = np.asarray(P_mm, dtype=np.float64)
    is_1d = (P.ndim == 1)
    if is_1d:
        P = P[:, None, None]

    n_time, rows, cols = P.shape
    grid_shape = (rows, cols)

    cn = np.clip(np.asarray(CN, dtype=np.float64), 1.0, 100.0)
    cn = np.broadcast_to(cn, grid_shape).copy()

    if not (0.0 <= lam < 1.0):
        raise ValueError(f"lam must be in [0, 1), got {lam}")

    # Maximum potential retention S (mm)
    # Special case CN == 100 -> S = 0
    S = np.where(cn >= 99.99, 0.0, 25400.0 / cn - 254.0)
    Ia = lam * S  # Initial abstraction (mm)

    P_cum = np.cumsum(P, axis=0)  # (n_time, rows, cols)

    S3 = S[None, :, :]
    Ia3 = Ia[None, :, :]

    # Calculate cumulative runoff Q_cum
    denominator = P_cum + (1.0 - lam) * S3
    denominator = np.where(denominator <= 1e-9, 1e-9, denominator)

    valid = P_cum > Ia3
    Q_cum = np.zeros_like(P_cum)

    # For 100% CN (impervious/water), Q_cum = P_cum
    perfect_imperv = (cn >= 99.99)[None, :, :]
    np.divide((P_cum - Ia3) ** 2, denominator, out=Q_cum, where=valid)
    Q_cum = np.where(perfect_imperv, P_cum, Q_cum)
    Q_cum = np.clip(Q_cum, 0.0, P_cum)

    # Convert cumulative runoff to incremental net rain: R_net(t) = Q_cum(t) - Q_cum(t-1)
    net_rain = np.diff(Q_cum, axis=0, prepend=0.0)
    net_rain = np.maximum(0.0, net_rain)

    # Loss is remainder of P
    loss = np.maximum(0.0, P - net_rain)

    mb_error = np.abs(P - (net_rain + loss))
    max_err = float(np.max(mb_error))
    total_P = float(np.sum(P))
    rel_err = max_err / max(total_P, 1.0)

    final_Q_cum = Q_cum[-1]

    if is_1d:
        net_rain = net_rain[:, 0, 0]
        loss = loss[:, 0, 0]
        final_Q_cum = final_Q_cum[0, 0]
        mb_error = mb_error[:, 0, 0]

    return SCSCNResult(
        net_rain_mm=net_rain,
        infil_loss_mm=loss,
        cumulative_runoff_mm=final_Q_cum,
        mass_balance_error_mm=mb_error,
        max_relative_error=rel_err,
    )


def scs_cn_grid_with_landcover(
    P_mm: np.ndarray,
    fractions: Any,
    cn_lookup: dict[str, dict[str, float]],
    hsg_grid: np.ndarray,
    *,
    lam: float = 0.20,
    amc_class: int = 2,
) -> SCSCNResult:
    """Calculate SCS-CN net rain with sub-grid land cover fractions.

    Plan reference §6.4: R_net = sum(f_i * R_net,i)
    Avoids averaging non-linear CN parameters directly.
    """
    P = np.asarray(P_mm, dtype=np.float64)
    if P.ndim == 1:
        P = P[:, None, None]

    n_time, rows, cols = P.shape
    grid_shape = (rows, cols)

    # Map numeric HSG (1=A, 2=B, 3=C, 4=D) to letter
    hsg_letters = np.full(grid_shape, "C", dtype=object)
    hsg_letters[hsg_grid == 1] = "A"
    hsg_letters[hsg_grid == 2] = "B"
    hsg_letters[hsg_grid == 3] = "C"
    hsg_letters[hsg_grid == 4] = "D"

    # Sum category weighted net rain
    composite_net_rain = np.zeros((n_time, rows, cols), dtype=np.float64)
    composite_loss = np.zeros((n_time, rows, cols), dtype=np.float64)
    composite_final_q = np.zeros(grid_shape, dtype=np.float64)

    for cat_name, frac_arr in fractions.fractions.items():
        if np.all(frac_arr <= 0.0):
            continue

        # Look up CN for this category
        cat_cn_map = cn_lookup.get(cat_name, {"A": 60, "B": 70, "C": 80, "D": 85})
        cat_cn_grid = np.zeros(grid_shape, dtype=np.float64)
        for h in ("A", "B", "C", "D"):
            mask = (hsg_letters == h)
            cat_cn_grid[mask] = float(cat_cn_map.get(h, 80))

        # Adjust for AMC if not AMC-II
        if amc_class != 2:
            from ..preprocessing.antecedent_moisture import adjust_cn_for_amc
            cat_cn_grid = adjust_cn_for_amc(cat_cn_grid, amc_class)

        cat_res = scs_cn_net_rain(P, cat_cn_grid, lam=lam)

        f3 = frac_arr[None, :, :]
        composite_net_rain += f3 * cat_res.net_rain_mm
        composite_loss += f3 * cat_res.infil_loss_mm
        composite_final_q += frac_arr * cat_res.cumulative_runoff_mm

    mb_error = np.abs(P - (composite_net_rain + composite_loss))
    max_err = float(np.max(mb_error))
    total_P = float(np.sum(P))
    rel_err = max_err / max(total_P, 1.0)

    return SCSCNResult(
        net_rain_mm=composite_net_rain,
        infil_loss_mm=composite_loss,
        cumulative_runoff_mm=composite_final_q,
        mass_balance_error_mm=mb_error,
        max_relative_error=rel_err,
    )
