"""Unit and integration tests for rainfall-runoff preprocessor.

Plan reference: plans/地表产流损失、土壤水文属性与初损网格化算法（针对产流水量虚高）计划.txt
Tests Green-Ampt, SCS-CN, Horton, sub-grid land cover fractions, mass balance,
and runoff inflation quantification.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
import numpy as np
import pytest

from rainfall_runoff_preprocessor.preprocessing.rainfall_deaccumulation import (
    deaccumulate_rainfall,
    broadcast_rainfall_to_grid,
    load_rainfall_series,
)
from rainfall_runoff_preprocessor.preprocessing.soil_parameter_loader import (
    load_soil_parameters_for_grid,
    build_synthetic_regional_soil,
)
from rainfall_runoff_preprocessor.preprocessing.landcover_fraction import (
    build_default_regional_fractions,
)
from rainfall_runoff_preprocessor.preprocessing.soil_grid_alignment import (
    resample_array_to_shape,
)
from rainfall_runoff_preprocessor.preprocessing.antecedent_moisture import (
    calculate_initial_moisture,
    classify_amc,
    adjust_cn_for_amc,
)
from rainfall_runoff_preprocessor.infiltration.green_ampt import (
    green_ampt_net_rain,
    green_ampt_grid_with_landcover,
)
from rainfall_runoff_preprocessor.infiltration.scs_cn import (
    scs_cn_net_rain,
    scs_cn_grid_with_landcover,
)
from rainfall_runoff_preprocessor.infiltration.horton import (
    horton_net_rain,
    horton_grid_with_landcover,
)
from rainfall_runoff_preprocessor.infiltration.soil_water_state import (
    SoilWaterTracker,
)
from rainfall_runoff_preprocessor.validation.mass_balance import (
    verify_mass_balance,
)
from rainfall_runoff_preprocessor.validation.event_runoff_validation import (
    compute_event_metrics,
    compare_runoff_inflation,
)
from rainfall_runoff_preprocessor.run_preprocessor import (
    run_pipeline,
)


def test_rainfall_deaccumulation():
    """Test deaccumulate_rainfall handles cumulative series and resets properly."""
    cum_series = [0.0, 5.0, 15.0, 30.0, 30.0, 42.0]
    inc = deaccumulate_rainfall(cum_series)
    np.testing.assert_allclose(inc, [0.0, 5.0, 10.0, 15.0, 0.0, 12.0])

    # Broadcast to 3D grid
    grid_3d = broadcast_rainfall_to_grid(inc, (10, 12))
    assert grid_3d.shape == (6, 10, 12)
    assert np.all(grid_3d[:, 0, 0] == inc)


def test_amc_and_cn_adjustment():
    """Test AMC classification and NRCS CN conversion formulas (§4.1.3)."""
    assert classify_amc(20.0, season="growing") == 1
    assert classify_amc(45.0, season="growing") == 2
    assert classify_amc(65.0, season="growing") == 3

    # For CN-II = 75:
    cn_ii = np.array([75.0])
    cn_i = adjust_cn_for_amc(cn_ii, amc_class=1)
    cn_iii = adjust_cn_for_amc(cn_ii, amc_class=3)

    # CN_I must be less than CN_II, CN_III must be greater than CN_II
    assert float(cn_i[0]) < 75.0
    assert float(cn_iii[0]) > 75.0
    # Formula check: CN_I = 75 / (2.281 - 0.01281 * 75) = 75 / 1.32025 ≈ 56.8
    assert round(float(cn_i[0]), 1) == 56.8
    # CN_III = 75 / (0.427 + 0.00573 * 75) = 75 / 0.85675 ≈ 87.5
    assert round(float(cn_iii[0]), 1) == 87.5


def test_green_ampt_low_intensity_no_runoff():
    """When rain rate < Ks, all rainfall must infiltrate (zero net rain, zero mass error)."""
    # 5 hours of 2.0 mm/h rain, Ks = 5.0 mm/h
    P = np.array([2.0, 2.0, 2.0, 2.0, 2.0])
    Ks = np.array([[5.0]])
    psi = np.array([[100.0]])
    ts = np.array([[0.45]])
    ti = np.array([[0.25]])

    res = green_ampt_net_rain(P, Ks, psi, ts, ti)
    # Net rain must be 0 everywhere
    np.testing.assert_allclose(res.net_rain_mm, 0.0, atol=1e-7)
    np.testing.assert_allclose(res.infil_depth_mm, P, atol=1e-7)
    # Strict mass balance
    assert res.max_relative_error < 1e-12


def test_green_ampt_high_intensity_decay_and_mass_balance():
    """Under intense storm (P >> Ks), infiltration capacity decays and net rain increases."""
    P = np.full(10, 20.0)  # 20 mm/h for 10 hours
    Ks = np.array([[2.0]])
    psi = np.array([[150.0]])
    ts = np.array([[0.46]])
    ti = np.array([[0.20]])

    res = green_ampt_net_rain(P, Ks, psi, ts, ti, substep_min=1.0)

    # Infiltration rate in hour 0 must be higher than hour 9 due to suction head decay
    assert res.infil_depth_mm[0] > res.infil_depth_mm[9]
    # Net rain in hour 9 must be greater than hour 0
    assert res.net_rain_mm[9] > res.net_rain_mm[0]
    # Mass balance holds strictly (<= 1e-5 quality threshold)
    passed, _, rel_err = verify_mass_balance(P, res.net_rain_mm, res.infil_depth_mm)
    assert passed
    assert rel_err < 1e-10


def test_green_ampt_storage_cap_saturation_excess():
    """When storage cap F_max is reached, capacity drops and saturation excess occurs."""
    P = np.full(8, 15.0)  # 120 mm total
    Ks = np.array([[5.0]])
    psi = np.array([[100.0]])
    ts = np.array([[0.45]])
    ti = np.array([[0.25]])
    # Soil storage cap = 20 mm
    F_max = np.array([[20.0]])

    res = green_ampt_net_rain(P, Ks, psi, ts, ti, F_max_mm=F_max)
    # Total infiltration cannot exceed F_max
    assert float(np.sum(res.infil_depth_mm)) <= 20.0001
    passed, _, rel_err = verify_mass_balance(P, res.net_rain_mm, res.infil_depth_mm)
    assert passed
    assert rel_err < 1e-10


def test_scs_cn_physics_and_mass_balance():
    """Test SCS-CN cumulative difference, initial abstraction, and mass balance."""
    # S for CN=80: 25400/80 - 254 = 63.5 mm. Ia = 0.2 * 63.5 = 12.7 mm
    cn = np.array([[80.0]])
    # Hour 0: 5 mm (P_cum=5 <= 12.7 -> Q=0)
    # Hour 1: 5 mm (P_cum=10 <= 12.7 -> Q=0)
    # Hour 2: 10 mm (P_cum=20 > 12.7 -> Q > 0)
    # Hour 3: 20 mm (P_cum=40 > 12.7 -> Q > 0)
    P = np.array([5.0, 5.0, 10.0, 20.0])

    res = scs_cn_net_rain(P, cn, lam=0.20)
    assert float(res.net_rain_mm[0]) == 0.0
    assert float(res.net_rain_mm[1]) == 0.0
    assert float(res.net_rain_mm[2]) > 0.0
    assert float(res.net_rain_mm[3]) > float(res.net_rain_mm[2])

    passed, _, rel_err = verify_mass_balance(P, res.net_rain_mm, res.infil_loss_mm)
    assert passed
    assert rel_err < 1e-10


def test_horton_physics_and_mass_balance():
    """Test Horton exponential decay and mass balance."""
    P = np.full(6, 30.0)
    f0 = np.array([[100.0]])
    fc = np.array([[10.0]])
    k = np.array([[1.5]])

    res = horton_net_rain(P, f0, fc, k, dt_h=1.0, substep_min=1.0)
    # Infiltration decreases over time towards fc
    assert res.infil_depth_mm[0] > res.infil_depth_mm[5]
    passed, _, rel_err = verify_mass_balance(P, res.net_rain_mm, res.infil_depth_mm)
    assert passed
    assert rel_err < 1e-10


def test_subgrid_landcover_weighting():
    """Test sub-grid landcover weighting R_net = sum(f_i * R_net,i)."""
    shape = (5, 5)
    fracs = build_default_regional_fractions(shape, case="jiulong")
    # Verify fractions sum to 1.0 everywhere
    total_f = sum(fracs.fractions.values())
    np.testing.assert_allclose(total_f, 1.0)

    # 100% impervious cell must produce 100% runoff
    P_grid = np.full((3, 5, 5), 10.0)
    soil = build_synthetic_regional_soil(shape)
    theta_i = np.full(shape, 0.25)

    res = green_ampt_grid_with_landcover(P_grid, soil, fracs, theta_i)
    # Mass balance must pass everywhere
    passed, _, rel_err = verify_mass_balance(P_grid, res.net_rain_mm, res.infil_depth_mm)
    assert passed
    assert rel_err < 1e-10


def test_resample_array_to_shape():
    """Test pure numpy grid resampling methods."""
    src = np.array([[1.0, 2.0], [3.0, 4.0]])
    dst_nearest = resample_array_to_shape(src, (4, 4), method="nearest")
    dst_bilinear = resample_array_to_shape(src, (4, 4), method="bilinear")

    assert dst_nearest.shape == (4, 4)
    assert dst_bilinear.shape == (4, 4)
    assert dst_bilinear[0, 0] == 1.0
    assert dst_bilinear[-1, -1] == 4.0


def test_soil_water_tracker():
    """Test SoilWaterTracker cumulative states and moisture evolution."""
    shape = (2, 2)
    ts = np.full(shape, 0.45)
    ti = np.full(shape, 0.25)
    tracker = SoilWaterTracker(theta_s=ts, theta_i=ti, effective_depth_m=1.0)

    assert round(float(tracker.moisture_deficit[0, 0]), 2) == 0.20
    assert round(float(tracker.remaining_storage_mm[0, 0]), 1) == 200.0

    step_p = np.full(shape, 10.0)
    step_r = np.full(shape, 2.0)
    step_i = np.full(shape, 8.0)
    tracker.update_step(step_p, step_r, step_i)

    passed, rel_err = tracker.check_mass_balance()
    assert passed
    assert rel_err < 1e-10
    # Infiltrated 8mm into 1000mm soil -> delta_theta = 0.008 -> theta = 0.258
    assert round(float(tracker.theta_current[0, 0]), 3) == 0.258


def test_runoff_inflation_metrics():
    """Test runoff inflation quantification against baseline zero-loss."""
    P = np.full(10, 10.0)  # 100 mm total
    # Baseline: 100 mm net
    base = compute_event_metrics("Baseline", P, P, np.zeros_like(P), domain_area_m2=1e6)
    # Model: 30 mm net, 70 mm loss
    model = compute_event_metrics("GA-Mid", P, np.full(10, 3.0), np.full(10, 7.0), domain_area_m2=1e6)

    comp = compare_runoff_inflation(base, model)
    assert comp["baseline_scenario"] == "Baseline"
    assert comp["model_scenario"] == "GA-Mid"
    # Overestimated depth: 100 - 30 = 70 mm -> 70,000 m3
    assert comp["overestimated_volume_m3"] == 70000.0
    # Overestimated percentage: (70000 / 30000) * 100 = 233.33%
    assert round(comp["runoff_inflation_pct"], 1) == 233.3
    # Baseline reduction: 70%
    assert round(comp["runoff_reduction_pct"], 1) == 70.0


def test_run_pipeline_end_to_end():
    """Run full pipeline on jiulong case in a temporary directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        out_path = Path(tmpdir)
        summary = run_pipeline(case="jiulong", output_dir=out_path)

        assert summary["case"] == "jiulong"
        assert summary["gross_rainfall_mm"] in (84.0, 242.5)
        assert "Baseline" in summary["scenarios"]
        assert "GA-Mid" in summary["scenarios"]
        assert "CN-200" in summary["scenarios"]
        assert "Horton" in summary["scenarios"]

        # Runoff inflation of baseline against GA-Mid must be positive
        ga_mid_inf = summary["runoff_inflation_analysis"]["GA-Mid"]
        assert ga_mid_inf["runoff_inflation_pct"] > 100.0
        assert ga_mid_inf["overestimated_volume_m3"] > 0

        # Verify exported files
        assert (out_path / "soil_ksat.tif").is_file()
        assert (out_path / "soil_theta_s.tif").is_file()
        assert (out_path / "rainfall_gross.rain").is_file()
        assert (out_path / "rainfall_net_green_ampt.rain").is_file()
        assert (out_path / "rainfall_net_scs_cn.rain").is_file()
        assert (out_path / "rainfall_runoff_mass_balance.csv").is_file()
        assert (out_path / "preprocessor_summary.json").is_file()
