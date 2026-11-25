"""M1 单元测试：垂直基准对齐与相对涨水计算。"""
from __future__ import annotations

import numpy as np
import pytest

from hydro_refine.monitors.datum_converter import (
    align_water_levels,
    compute_stage_rise,
)


def test_explicit_offset_closes_datum() -> None:
    res = align_water_levels(
        [43.09, 44.00],
        source_datum="station_local",
        target_datum="dem_local",
        explicit_offset_m=-20.5,
    )
    assert res.datum_closed is True
    assert res.uncertainty_iqr_m == 0.0
    assert np.allclose(res.aligned_levels_m, [22.59, 23.50])


def test_unclosed_g7_remains_unclosed_and_keeps_atl13_iqr() -> None:
    res = align_water_levels(
        [43.09, 44.00],
        source_datum="station_local",
        target_datum="dem_local",
        atl13_bounds={"median_m": -22.1, "iqr_m": 0.85, "coverage": "sufficient"},
    )
    assert res.datum_closed is False
    assert res.offset_applied_m == 0.0
    assert res.uncertainty_iqr_m == pytest.approx(0.85)
    assert np.allclose(res.aligned_levels_m, [43.09, 44.00])


def test_standard_national_datum_conversion() -> None:
    res = align_water_levels(
        [10.0],
        source_datum="1956_yellow_sea",
        target_datum="1985_national",
    )
    assert res.datum_closed is True
    assert np.allclose(res.aligned_levels_m, [9.971])


def test_compute_stage_rise_invariant_to_datum_shift() -> None:
    levels_a = np.array([109.24, 122.96, 123.43])
    levels_b = levels_a - 21.5
    rise_a = compute_stage_rise(levels_a)
    rise_b = compute_stage_rise(levels_b)
    assert np.allclose(rise_a, rise_b)
    assert rise_a[-1] == pytest.approx(14.19)
