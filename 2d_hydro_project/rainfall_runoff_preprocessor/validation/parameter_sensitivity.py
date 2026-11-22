"""Automated parameter sensitivity suite and multi-model scenario execution.

Plan reference: §9 / §10.
Executes Baseline, CN-200, CN-050, GA-Dry, GA-Mid, GA-Wet, GA-Layered, and Horton.
"""
from __future__ import annotations

from typing import Any
import numpy as np

from ..infiltration.green_ampt import (
    green_ampt_net_rain,
    green_ampt_grid_with_landcover,
)
from ..infiltration.scs_cn import (
    scs_cn_net_rain,
    scs_cn_grid_with_landcover,
)
from ..infiltration.horton import (
    horton_net_rain,
    horton_grid_with_landcover,
)
from ..preprocessing.antecedent_moisture import (
    calculate_initial_moisture,
    adjust_cn_for_amc,
)
from .event_runoff_validation import (
    EventRunoffMetrics,
    compute_event_metrics,
    compare_runoff_inflation,
)


def run_parameter_sensitivity_suite(
    P_mm: np.ndarray,
    soil: Any,
    fractions: Any,
    cn_lookup: dict[str, dict[str, float]],
    horton_defaults: dict[str, dict[str, float]],
    *,
    domain_area_m2: float = 1.0e8,
    dt_h: float = 1.0,
) -> dict[str, Any]:
    """Execute the full 8-scenario parameter sensitivity matrix.

    Scenarios:
      1. Baseline: Zero loss (P_net = P, Loss = 0)
      2. CN-200: SCS-CN with standard lambda=0.20, CN-II
      3. CN-050: SCS-CN with lambda=0.05 (initial abstraction sensitivity)
      4. GA-Dry: Green-Ampt dry initial soil (eta=0.15)
      5. GA-Mid: Green-Ampt average initial soil (eta=0.50)
      6. GA-Wet: Green-Ampt wet antecedent soil (eta=0.85)
      7. GA-Layered: Green-Ampt with finite profile capacity (shallow soil Z_eff=0.5m)
      8. Horton: Horton model with HSG-based f0, fc, k

    Returns
    -------
    dict with metrics per scenario, comparison against baseline, and ranking.
    """
    scenarios: dict[str, EventRunoffMetrics] = {}
    time_series_results: dict[str, dict[str, np.ndarray]] = {}

    # 1. Baseline: Zero Infiltration Loss
    zero_loss = np.zeros_like(P_mm)
    base_metrics = compute_event_metrics("Baseline", P_mm, P_mm, zero_loss, domain_area_m2=domain_area_m2)
    scenarios["Baseline"] = base_metrics
    time_series_results["Baseline"] = {"net_rain": P_mm, "loss": zero_loss}

    # 2. CN-200 (lambda = 0.20, AMC-II)
    cn200_res = scs_cn_grid_with_landcover(P_mm, fractions, cn_lookup, soil.hsg, lam=0.20, amc_class=2)
    scenarios["CN-200"] = compute_event_metrics(
        "CN-200", P_mm, cn200_res.net_rain_mm, cn200_res.infil_loss_mm, domain_area_m2=domain_area_m2
    )
    time_series_results["CN-200"] = {"net_rain": cn200_res.net_rain_mm, "loss": cn200_res.infil_loss_mm}

    # 3. CN-050 (lambda = 0.05, calibrated initial abstraction)
    cn050_res = scs_cn_grid_with_landcover(P_mm, fractions, cn_lookup, soil.hsg, lam=0.05, amc_class=2)
    scenarios["CN-050"] = compute_event_metrics(
        "CN-050", P_mm, cn050_res.net_rain_mm, cn050_res.infil_loss_mm, domain_area_m2=domain_area_m2
    )
    time_series_results["CN-050"] = {"net_rain": cn050_res.net_rain_mm, "loss": cn050_res.infil_loss_mm}

    # 4. GA-Dry (eta = 0.15)
    moist_dry = calculate_initial_moisture(soil.theta_s, soil.theta_wp, scenario="dry")
    ga_dry_res = green_ampt_grid_with_landcover(P_mm, soil, fractions, moist_dry.theta_i, dt_h=dt_h)
    scenarios["GA-Dry"] = compute_event_metrics(
        "GA-Dry", P_mm, ga_dry_res.net_rain_mm, ga_dry_res.infil_depth_mm, domain_area_m2=domain_area_m2
    )
    time_series_results["GA-Dry"] = {"net_rain": ga_dry_res.net_rain_mm, "loss": ga_dry_res.infil_depth_mm}

    # 5. GA-Mid (eta = 0.50) - Preferred Baseline
    moist_mid = calculate_initial_moisture(soil.theta_s, soil.theta_wp, scenario="mid")
    ga_mid_res = green_ampt_grid_with_landcover(P_mm, soil, fractions, moist_mid.theta_i, dt_h=dt_h)
    scenarios["GA-Mid"] = compute_event_metrics(
        "GA-Mid", P_mm, ga_mid_res.net_rain_mm, ga_mid_res.infil_depth_mm, domain_area_m2=domain_area_m2
    )
    time_series_results["GA-Mid"] = {"net_rain": ga_mid_res.net_rain_mm, "loss": ga_mid_res.infil_depth_mm}

    # 6. GA-Wet (eta = 0.85)
    moist_wet = calculate_initial_moisture(soil.theta_s, soil.theta_wp, scenario="wet")
    ga_wet_res = green_ampt_grid_with_landcover(P_mm, soil, fractions, moist_wet.theta_i, dt_h=dt_h)
    scenarios["GA-Wet"] = compute_event_metrics(
        "GA-Wet", P_mm, ga_wet_res.net_rain_mm, ga_wet_res.infil_depth_mm, domain_area_m2=domain_area_m2
    )
    time_series_results["GA-Wet"] = {"net_rain": ga_wet_res.net_rain_mm, "loss": ga_wet_res.infil_depth_mm}

    # 7. GA-Layered (shallow soil profile Z_eff=0.5m with saturation cap)
    moist_layer = calculate_initial_moisture(soil.theta_s, soil.theta_wp, scenario="mid", effective_depth_m=0.5)
    ga_layer_res = green_ampt_grid_with_landcover(
        P_mm, soil, fractions, moist_layer.theta_i, dt_h=dt_h, apply_storage_cap=True
    )
    scenarios["GA-Layered"] = compute_event_metrics(
        "GA-Layered", P_mm, ga_layer_res.net_rain_mm, ga_layer_res.infil_depth_mm, domain_area_m2=domain_area_m2
    )
    time_series_results["GA-Layered"] = {"net_rain": ga_layer_res.net_rain_mm, "loss": ga_layer_res.infil_depth_mm}

    # 8. Horton
    horton_res = horton_grid_with_landcover(P_mm, fractions, soil.hsg, horton_defaults, dt_h=dt_h)
    scenarios["Horton"] = compute_event_metrics(
        "Horton", P_mm, horton_res.net_rain_mm, horton_res.infil_depth_mm, domain_area_m2=domain_area_m2
    )
    time_series_results["Horton"] = {"net_rain": horton_res.net_rain_mm, "loss": horton_res.infil_depth_mm}

    # Quantify runoff inflation of Baseline against all models
    inflation_comparisons = {}
    for name, met in scenarios.items():
        if name != "Baseline":
            inflation_comparisons[name] = compare_runoff_inflation(base_metrics, met)

    return {
        "scenarios": scenarios,
        "time_series": time_series_results,
        "inflation_comparisons": inflation_comparisons,
    }
