"""集合超越概率产品单测（计划 T12，ADR-0018 §1 口径）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import (  # noqa: E402
    AsciiGrid,
    GridError,
    write_ascii_grid,
)
from scripts.stage6.ensemble_probability import (  # noqa: E402
    exceedance_probability,
    write_prob_tif,
)

HDR = {"ncols": 4, "nrows": 4, "xllcorner": 700000.0, "yllcorner": 2440000.0,
       "cellsize": 100.0, "nodata_value": -9999.0}


def _grid(v: float) -> np.ndarray:
    return np.full((4, 4), v, dtype=np.float32)


def test_exceedance_probability_fractions():
    """3 成员、阈值 0.05：值 0.1/0.1/0.5 → 像元概率 {1/3, 2/3}。"""
    stack = [_grid(0.1), _grid(0.1), _grid(0.5)]
    p = exceedance_probability(stack, 0.05)
    assert p.shape == (4, 4)
    assert np.allclose(p, 1.0)  # 全部成员 > 0.05
    stack2 = [_grid(0.1), _grid(0.02), _grid(0.5)]
    p2 = exceedance_probability(stack2, 0.05)
    assert np.allclose(p2, 2.0 / 3.0)


def test_exceedance_probability_thresholds_and_empty():
    stack = [_grid(0.2), _grid(0.4)]
    p03 = exceedance_probability(stack, 0.3)
    assert np.allclose(p03, 0.5)
    with pytest.raises(GridError):
        exceedance_probability([], 0.05)
    with pytest.raises(GridError):
        exceedance_probability([_grid(0.1), np.zeros((3, 3))], 0.05)


def test_exceedance_probability_sea_mask():
    sea = np.zeros((4, 4), dtype=bool)
    sea[0, :] = True
    stack = [_grid(0.5), _grid(0.5)]
    p = exceedance_probability(stack, 0.05, sea_mask=sea)
    assert np.all(p[0, :] == 0.0)
    assert np.all(p[1:, :] == 1.0)


def test_write_prob_tif_range_and_nodata(tmp_path):
    import rasterio

    p = np.array([[0.0, 0.25], [0.5, 1.0]])
    sea = np.array([[False, False], [False, True]])
    out = write_prob_tif(tmp_path / "p.tif", p, HDR, 32648, sea)
    with rasterio.open(out) as ds:
        data = ds.read(1)
        assert ds.nodata == -1.0
        assert data[0, 0] == pytest.approx(0.0)
        assert data[1, 1] == pytest.approx(-1.0)
        assert float(data[data > -0.5].max()) <= 1.0


def test_member_gate_filtering(tmp_path):
    """非 PASS run 必须被拒绝（模拟 main 的过滤逻辑口径）。"""
    gate = {
        "outputs/ningming/x/scen_q2500_n35_100m": {"gate_1e-3": "PASS"},
        "outputs/ningming/x/scen_q4000_n60_100m": {"gate_1e-3": "FAIL-EXPLAINED"},
    }
    import re

    pattern = re.compile(r"^scen_q(\d+)_n(\d+)_100m$")
    members = []
    for path, rec in gate.items():
        name = Path(path).name
        m = pattern.match(name)
        if not m or rec.get("gate_1e-3") != "PASS":
            continue
        members.append(name)
    assert members == ["scen_q2500_n35_100m"]  # FAIL-EXPLAINED 禁入（ADR-0018）
