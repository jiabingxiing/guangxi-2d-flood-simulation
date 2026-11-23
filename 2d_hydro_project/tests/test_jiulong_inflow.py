"""事件产汇流与入流文件单元测试（ADR-0019，合成算例先行）。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.event_hydro import (  # noqa: E402
    GA_PRIOR,
    route_nash,
    route_scs_uh,
    run_event_hourly,
    scs_unit_hydrograph,
)


def test_scs_unit_hydrograph_peak_and_base():
    """UH 峰值出现在 t≈Tp；每 10 mm 每 km² 的体积守恒（1e4 m³）。"""
    tp = 5.0
    uh = scs_unit_hydrograph(tp, 1.0)
    assert int(np.argmax(uh)) == 5  # 峰在 t=Tp
    assert uh.sum() * 3600.0 == pytest.approx(1e4, rel=1e-6)  # 10mm·1km²
    assert uh[0] == pytest.approx(0.0, abs=1e-12)
    assert len(uh) == 10  # 覆盖到 2·Tp


def test_route_scs_uh_volume_conservation():
    """10 mm 净雨在 A km² 上的总量 ≈ 0.01·A·1e6 m³（卷积求和）。"""
    excess = np.zeros(24)
    excess[10] = 10.0
    area = 100.0
    q = route_scs_uh(excess, area, 5.0)
    volume = q.sum() * 3600.0
    assert volume == pytest.approx(0.01 * area * 1e6, rel=1e-6)


def test_route_nash_attenuates_peak():
    """Nash 级联削峰不变形：峰值滞后；总水量受序列末端截断影响（容差 15%）。"""
    excess = np.zeros(48)
    excess[5:15] = 5.0
    q = route_nash(excess, k_h=2.0, n_res=3)
    assert int(np.argmax(q)) >= 14
    assert q.sum() == pytest.approx(excess.sum(), rel=0.15)


def test_event_model_mass_balance():
    """事件模型 ∫q dt 与净雨体积相对偏差 < 1%；径流系数介于 0 与 1。"""
    rng = np.random.default_rng(0)
    p = rng.uniform(0.0, 8.0, size=120)
    p[::7] = 0.0
    res = run_event_hourly(p, 500.0, tp_h=4.0)
    assert res["volume_rel_dev"] < 0.01
    assert 0.0 <= res["runoff_coeff"] <= 1.0
    assert res["peak_m3s"] > 0
    # 质量账：P = 净雨 + 下渗（preprocessor 账目闭合）
    infil = res["ga"]["infil_total_mm"]
    assert p.sum() == pytest.approx(res["excess_total_mm"] + infil, rel=1e-2)


def test_event_model_zero_rain():
    res = run_event_hourly(np.zeros(48), 100.0)
    assert res["peak_m3s"] == 0.0
    assert res["runoff_coeff"] == 0.0


def test_ga_prior_frozen_fields():
    """先验参数冻结口径（ADR-0019）：深渗默认关闭，Ks=3.5。"""
    assert GA_PRIOR["ks_mm_h"] == 3.5
    assert GA_PRIOR["f_max_mm"] == 120.0
    assert GA_PRIOR["drainage_mm_h"] == 0.0
