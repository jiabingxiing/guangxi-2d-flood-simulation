"""Antecedent soil moisture determination and SCS-CN AMC condition adjustment.

Plan reference: §3.3 / §4.1.3 / §4.2.4 / §7.4.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np


@dataclass(frozen=True)
class AntecedentMoistureState:
    """Initial soil moisture condition prior to storm event.

    Attributes
    ----------
    theta_i: 2D array of initial volumetric soil water content (m3/m3)
    moisture_deficit: theta_s - theta_i (m3/m3)
    available_storage_mm: Z_eff * (theta_s - theta_i) * 1000.0 (mm)
    amc_class: AMC category (1 = AMC-I dry, 2 = AMC-II average, 3 = AMC-III wet)
    wetness_index_eta: Normalized soil wetness index in [0, 1]
    """

    theta_i: np.ndarray
    moisture_deficit: np.ndarray
    available_storage_mm: np.ndarray
    amc_class: int
    wetness_index_eta: float


def classify_amc(
    antecedent_rain_5d_mm: float,
    season: Literal["growing", "dormant"] = "growing",
) -> int:
    """Classify NRCS Antecedent Moisture Condition (AMC) based on 5-day prior rainfall.

    Parameters
    ----------
    antecedent_rain_5d_mm: 5-day cumulative antecedent rainfall (mm)
    season: 'growing' (default for Guangxi typhoon season May-Oct) or 'dormant'

    Returns
    -------
    1 (AMC-I: dry), 2 (AMC-II: average), or 3 (AMC-III: wet/saturated)
    """
    if season == "growing":
        if antecedent_rain_5d_mm < 36.0:
            return 1
        elif antecedent_rain_5d_mm <= 53.0:
            return 2
        else:
            return 3
    else:
        if antecedent_rain_5d_mm < 13.0:
            return 1
        elif antecedent_rain_5d_mm <= 28.0:
            return 2
        else:
            return 3


def adjust_cn_for_amc(
    cn_ii: np.ndarray,
    amc_class: int,
) -> np.ndarray:
    """Convert standard CN-II curve number grid to CN-I (dry) or CN-III (wet).

    Plan reference: §4.1.3:
      CN_I = CN_II / (2.281 - 0.01281 * CN_II)
      CN_III = CN_II / (0.427 + 0.00573 * CN_II)

    Parameters
    ----------
    cn_ii: 2D array of CN-II values
    amc_class: 1, 2, or 3

    Returns
    -------
    Adjusted CN array, clipped to [1.0, 99.9].
    """
    cn = np.clip(np.asarray(cn_ii, dtype=np.float64), 1.0, 99.9)

    if amc_class == 1:
        denom = 2.281 - 0.01281 * cn
        denom = np.where(denom <= 0.01, 0.01, denom)
        cn_adj = cn / denom
    elif amc_class == 3:
        denom = 0.427 + 0.00573 * cn
        denom = np.where(denom <= 0.01, 0.01, denom)
        cn_adj = cn / denom
    else:
        cn_adj = cn

    return np.clip(cn_adj, 1.0, 99.9)


def calculate_initial_moisture(
    theta_s: np.ndarray,
    theta_wp: np.ndarray,
    theta_fc: np.ndarray | None = None,
    *,
    scenario: Literal["dry", "mid", "wet", "custom"] = "mid",
    wetness_index_eta: float | None = None,
    antecedent_rain_5d_mm: float | None = None,
    effective_depth_m: np.ndarray | float = 0.8,
) -> AntecedentMoistureState:
    """Derive initial volumetric soil moisture theta_i.

    Plan reference: §4.2.4:
      theta_i = theta_wp + eta * (theta_s - theta_wp)

    Parameters
    ----------
    theta_s: Saturated water content (m3/m3)
    theta_wp: Wilting point water content (m3/m3)
    theta_fc: Optional field capacity water content
    scenario: 'dry' (eta=0.15), 'mid' (eta=0.50), 'wet' (eta=0.85)
    wetness_index_eta: Explicit eta value in [0, 1] overriding scenario
    antecedent_rain_5d_mm: Optional 5-day prior rain in mm
    effective_depth_m: Effective soil profile depth (m)
    """
    if wetness_index_eta is not None:
        eta = float(np.clip(wetness_index_eta, 0.0, 1.0))
        amc = 1 if eta < 0.35 else (3 if eta > 0.70 else 2)
    elif antecedent_rain_5d_mm is not None:
        amc = classify_amc(antecedent_rain_5d_mm)
        if amc == 1:
            eta = 0.15
        elif amc == 2:
            eta = 0.50
        else:
            eta = 0.85
    else:
        if scenario == "dry":
            eta = 0.15
            amc = 1
        elif scenario == "wet":
            eta = 0.85
            amc = 3
        else:
            eta = 0.50
            amc = 2

    # Continuous initial water content interpolation
    theta_i = theta_wp + eta * (theta_s - theta_wp)

    # Ensure theta_i < theta_s with safety margin to prevent division by zero in Green-Ampt
    max_theta_i = theta_s - 0.005
    theta_i = np.minimum(theta_i, max_theta_i)
    theta_i = np.maximum(theta_i, theta_wp)

    deficit = theta_s - theta_i

    depth = np.asarray(effective_depth_m, dtype=np.float64)
    storage_mm = depth * deficit * 1000.0

    return AntecedentMoistureState(
        theta_i=theta_i,
        moisture_deficit=deficit,
        available_storage_mm=storage_mm,
        amc_class=amc,
        wetness_index_eta=eta,
    )
