"""IMERG 预处理单测（计划 T08，ADR-0016 §2；纯合成数据，不联网）。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ))

from scripts.stage3.prep_imerg_rain import (  # noqa: E402
    bbox_mean_field,
    deaccumulate,
    hourly_uniform_from_halfhour,
    is_cumulative,
)


def test_is_cumulative_and_deaccumulate():
    assert is_cumulative(np.array([1.0, 2.0, 5.0, 5.0, 9.0])) is True
    assert is_cumulative(np.array([1.0, 0.0, 3.0, 0.0, 2.0])) is False  # 率值
    cum = np.array([0.0, 1.0, 4.0, 4.0])
    inc = deaccumulate(cum)
    assert inc.sum() == pytest.approx(4.0)
    assert (inc >= 0).all()


def test_hourly_uniform_from_halfhour_alignment():
    """事件 t0=本地 2026-08-21T00（=UTC 08-20T16），起点 UTC 08-19T00 → 偏移 40 h
    = 80 半点；本地第 0 小时 ← UTC 半点对索引 64/65（80−16）。"""
    n_half = 48 * 2 + 8  # 足量半点，起点 UTC 2026-08-19T00
    vals = np.arange(n_half, dtype=float) * 0.5  # 半点值递增，可断言索引
    times, hourly, missing = hourly_uniform_from_halfhour(
        vals, "2026-08-19T00:00", "2026-08-21T00:00", 4)
    assert missing == 0
    assert len(times) == 4 and len(hourly) == 4
    assert times[0].startswith("2026-08-21T00")
    i = 80 + 2 * 0 - 16
    expect = (vals[i] + vals[i + 1]) / 2
    assert hourly[0] == pytest.approx(expect)
    assert times[3].startswith("2026-08-21T03")


def test_hourly_uniform_missing_halfhour():
    vals = np.full(48 * 2 + 8, 2.0)
    vals[64] = np.nan  # 事件窗第 0 本地小时的 UTC 半点缺测
    _times, hourly, missing = hourly_uniform_from_halfhour(
        vals, "2026-08-19T00:00", "2026-08-21T00:00", 4)
    assert missing == 1
    assert hourly[0] == 0.0  # 缺测小时记 0（不插值）


def test_hourly_uniform_rejects_short_series():
    with pytest.raises(ValueError, match="时区余量"):
        hourly_uniform_from_halfhour(np.zeros(10), "2026-08-21T00:00",
                                     "2026-08-21T00:00", 4)


def test_bbox_mean_field():
    lat = np.arange(22.0, 22.5, 0.1)
    lon = np.arange(107.0, 107.5, 0.1)
    field = np.ones((len(lat), len(lon))) * 3.0
    mean = bbox_mean_field(field, lat, lon, (107.05, 22.05, 107.2, 22.25))
    assert mean == pytest.approx(3.0)
    empty = bbox_mean_field(field, np.array([10.0]), np.array([110.0]),
                            (107.05, 22.05, 107.2, 22.25))
    assert np.isnan(empty)
