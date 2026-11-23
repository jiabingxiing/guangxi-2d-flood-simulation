"""A2 净雨与防重复计量口径测试（ADR-0019，合成算例先行）。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from rainfall_runoff_preprocessor.infiltration.green_ampt import green_ampt_net_rain  # noqa: E402


def test_net_rain_nonnegative_and_balanced():
    """净雨严格非负；P = 净雨 + 下渗（逐时账目闭合）。"""
    rng = np.random.default_rng(1)
    p = rng.uniform(0.0, 10.0, size=72)
    res = green_ampt_net_rain(
        p, 3.5, 300.0, 0.45, 0.32, dt_h=1.0, F_max_mm=120.0,
    )
    net = np.asarray(res.net_rain_mm).reshape(-1)
    infil = np.asarray(res.infil_depth_mm).reshape(-1)
    assert np.all(net >= 0.0)
    assert p.sum() == pytest.approx(net.sum() + infil.sum(), rel=1e-3)


def test_drainage_caps_post_saturation_capacity():
    """preprocessor 语义核对：drainage = 饱和后的入渗容量（docstring），
    仅蓄满后生效；未蓄满时与无 drainage 等价。A2 深渗口径即用此语义。"""
    rng = np.random.default_rng(2)
    p_light = rng.uniform(0.5, 3.0, size=48)  # 不蓄满
    no_drain = green_ampt_net_rain(p_light, 3.5, 300.0, 0.45, 0.32, dt_h=1.0)
    with_drain = green_ampt_net_rain(
        p_light, 3.5, 300.0, 0.45, 0.32, dt_h=1.0, drainage_mm_h=3.5,
    )
    assert np.asarray(no_drain.net_rain_mm).sum() == pytest.approx(
        np.asarray(with_drain.net_rain_mm).sum(), rel=1e-9
    )
    # 蓄满场景：强降雨 120 h → F_cum 触顶后 drainage 生效，净雨减少
    p_heavy = np.full(120, 8.0)
    no2 = green_ampt_net_rain(p_heavy, 3.5, 300.0, 0.45, 0.32, dt_h=1.0, F_max_mm=120.0)
    dr2 = green_ampt_net_rain(
        p_heavy, 3.5, 300.0, 0.45, 0.32, dt_h=1.0, F_max_mm=120.0, drainage_mm_h=3.5,
    )
    assert np.asarray(dr2.net_rain_mm).sum() < np.asarray(no2.net_rain_mm).sum()


def test_net_rain_first_hour_ponding_full_capture():
    """首小时小雨全部入渗（GA 初始容量近无穷），净雨为 0。"""
    p = np.array([2.0])
    res = green_ampt_net_rain(p, 3.5, 300.0, 0.45, 0.32, dt_h=1.0)
    net = np.asarray(res.net_rain_mm).reshape(-1)
    assert net[0] == pytest.approx(0.0, abs=1e-9)
