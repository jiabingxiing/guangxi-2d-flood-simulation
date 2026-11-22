"""State tracking for grid-based soil moisture and cumulative infiltration.

Plan reference: §4.2.5 / §7.4 / §9.
"""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np


class SoilWaterTracker:
    """Tracks soil moisture evolution, storage deficits, and cumulative infiltration."""

    def __init__(
        self,
        theta_s: np.ndarray,
        theta_i: np.ndarray,
        effective_depth_m: np.ndarray | float = 0.8,
    ) -> None:
        self.theta_s = np.asarray(theta_s, dtype=np.float64)
        self.theta_current = np.asarray(theta_i, dtype=np.float64).copy()
        self.depth_m = np.asarray(effective_depth_m, dtype=np.float64)

        self.cum_gross_rain_mm = np.zeros_like(self.theta_s)
        self.cum_net_rain_mm = np.zeros_like(self.theta_s)
        self.cum_infil_mm = np.zeros_like(self.theta_s)

    @property
    def moisture_deficit(self) -> np.ndarray:
        return np.maximum(0.0, self.theta_s - self.theta_current)

    @property
    def remaining_storage_mm(self) -> np.ndarray:
        return self.depth_m * self.moisture_deficit * 1000.0

    def update_step(
        self,
        gross_rain_step_mm: np.ndarray,
        net_rain_step_mm: np.ndarray,
        infil_step_mm: np.ndarray,
    ) -> None:
        """Update tracker with one time step of depths."""
        self.cum_gross_rain_mm += gross_rain_step_mm
        self.cum_net_rain_mm += net_rain_step_mm
        self.cum_infil_mm += infil_step_mm

        # Update moisture content based on infiltrated depth
        delta_theta = infil_step_mm / (self.depth_m * 1000.0)
        self.theta_current = np.minimum(self.theta_s, self.theta_current + delta_theta)

    def check_mass_balance(self, tolerance: float = 1e-5) -> tuple[bool, float]:
        """Check if cumulative gross rain == cumulative net rain + cumulative infiltration."""
        error = np.abs(self.cum_gross_rain_mm - (self.cum_net_rain_mm + self.cum_infil_mm))
        max_err = float(np.max(error))
        total_p = float(np.sum(self.cum_gross_rain_mm))
        rel_err = max_err / max(total_p, 1.0)
        return (rel_err <= tolerance), rel_err
