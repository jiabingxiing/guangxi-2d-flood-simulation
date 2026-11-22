"""Strict mass balance auditing and conservation verification.

Plan reference: §9 / §10.
Quality threshold: epsilon_rel <= 1.0e-5.
"""
from __future__ import annotations

from typing import Sequence
import numpy as np


def verify_mass_balance(
    P_mm: np.ndarray,
    R_net_mm: np.ndarray,
    Loss_mm: np.ndarray,
    *,
    tolerance: float = 1.0e-5,
) -> tuple[bool, float, float]:
    """Verify strict cell-level and domain-level water balance.

    P = R_net + Loss
    epsilon_rel = max(|P - (R_net + Loss)|) / max(sum(P), 1.0)

    Returns
    -------
    (passed, max_absolute_error_mm, relative_error)
    """
    P = np.asarray(P_mm, dtype=np.float64)
    R = np.asarray(R_net_mm, dtype=np.float64)
    L = np.asarray(Loss_mm, dtype=np.float64)

    err = np.abs(P - (R + L))
    max_abs_err = float(np.max(err))
    total_P = float(np.sum(P))

    rel_err = max_abs_err / max(total_P, 1.0)
    passed = (rel_err <= tolerance)

    return passed, max_abs_err, rel_err


def generate_mass_balance_table(
    timestamps: Sequence[str],
    P_series_mm: np.ndarray,
    R_net_series_mm: np.ndarray,
    Loss_series_mm: np.ndarray,
    cell_area_m2: float = 40000.0,
) -> list[dict]:
    """Generate hourly mass balance ledger with depths and volumes."""
    ledger = []
    cum_P = 0.0
    cum_R = 0.0
    cum_L = 0.0

    for i, ts in enumerate(timestamps):
        p_val = float(P_series_mm[i])
        r_val = float(R_net_series_mm[i])
        l_val = float(Loss_series_mm[i])
        diff = abs(p_val - (r_val + l_val))

        cum_P += p_val
        cum_R += r_val
        cum_L += l_val

        vol_p_m3 = (p_val / 1000.0) * cell_area_m2
        vol_r_m3 = (r_val / 1000.0) * cell_area_m2
        vol_l_m3 = (l_val / 1000.0) * cell_area_m2

        ledger.append({
            "step": i + 1,
            "timestamp": ts,
            "gross_rain_mm": round(p_val, 4),
            "net_rain_mm": round(r_val, 4),
            "loss_mm": round(l_val, 4),
            "step_error_mm": round(diff, 6),
            "cum_gross_rain_mm": round(cum_P, 4),
            "cum_net_rain_mm": round(cum_R, 4),
            "cum_loss_mm": round(cum_L, 4),
            "gross_vol_m3": round(vol_p_m3, 2),
            "net_vol_m3": round(vol_r_m3, 2),
            "loss_vol_m3": round(vol_l_m3, 2),
        })

    return ledger
