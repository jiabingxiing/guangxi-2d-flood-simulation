"""M1 单元测试：水文时序多维评估指标（NSE/KGE/RMSE/涨水拟合）。"""
from __future__ import annotations

import numpy as np
import pytest

from hydro_refine.monitors.hydro_evaluator import (
    evaluate_station_fit,
    kge_2009,
    mae,
    nse,
    pbias,
    pearson_r,
    rmse,
)


def test_perfect_fit_metrics() -> None:
    obs = np.array([1.0, 3.0, 5.0, 4.0, 2.0])
    sim = obs.copy()
    assert nse(sim, obs) == pytest.approx(1.0)
    assert rmse(sim, obs) == pytest.approx(0.0)
    assert mae(sim, obs) == pytest.approx(0.0)
    assert pbias(sim, obs) == pytest.approx(0.0)
    assert pearson_r(sim, obs) == pytest.approx(1.0)
    assert kge_2009(sim, obs)["kge"] == pytest.approx(1.0)


def test_nse_and_kge_known_values() -> None:
    obs = np.array([2.0, 4.0, 6.0, 4.0, 2.0])
    sim = np.array([2.5, 4.5, 6.5, 4.5, 2.5])  # constant +0.5 m bias
    # var(obs) = mean((-1.6, 0.4, 2.4, 0.4, -1.6)^2) = 11.2/5 = 2.24; mse = 0.25 -> NSE = 1 - 0.25/2.24
    expected_nse = 1.0 - 0.25 / 2.24
    assert nse(sim, obs) == pytest.approx(expected_nse, rel=1e-4)
    assert rmse(sim, obs) == pytest.approx(0.5)
    k = kge_2009(sim, obs)
    assert k["r"] == pytest.approx(1.0)
    assert k["alpha"] == pytest.approx(1.0)
    assert k["beta"] == pytest.approx(4.1 / 3.6, rel=1e-3)


def test_evaluate_station_fit_unclosed_datum_blocks_absolute_nse() -> None:
    res = evaluate_station_fit(
        t_sim_h=[0.0, 6.0, 12.0, 18.0],
        h_sim_m=[20.0, 22.0, 24.0, 21.0],
        t_obs_h=[0.0, 12.0],
        h_obs_m=[40.0, 44.0],
        datum_closed=False,
    )
    assert res["n_matched"] == 2
    assert res["nse"] is None
    assert res["nse_relative_rise"] == pytest.approx(1.0)
    assert res["rise_error_m"] == pytest.approx(0.0)
    assert res["peak_timing_error_h"] == pytest.approx(0.0)


def test_evaluate_station_fit_closed_datum_reports_nse_and_peak() -> None:
    res = evaluate_station_fit(
        t_sim_h=[0.0, 6.0, 12.0, 18.0],
        h_sim_m=[40.2, 42.1, 43.8, 41.0],
        t_obs_h=[0.0, 12.0],
        h_obs_m=[40.0, 44.0],
        datum_closed=True,
    )
    assert res["nse"] is not None and res["nse"] > 0.95
    assert res["peak_error_m"] == pytest.approx(-0.2)
    assert res["kge"] is not None
