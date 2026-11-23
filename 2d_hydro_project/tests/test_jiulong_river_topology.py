"""钦江水系建库纯函数测试（ADR-0020，合成算例先行）。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.terrain_hydro import (  # noqa: E402
    d8_receiver,
    flow_accumulation,
    priority_flood,
    upstream_mask,
)
from scripts.stage3.prepare_jiulong_channel import fill_gaps, rasterize_path_to_grid


def _grid_transform(shape):
    return from_bounds(0.0, 0.0, shape[1] * 200.0, shape[0] * 200.0, shape[1], shape[0])


def test_priority_flood_eps_drains_flats_to_spill():
    """PF+ε：填洼后平点带微梯度，D8 无平点死区（漏斗语义：排向边界）。"""
    dem = np.zeros((8, 8), dtype=float)
    dem[3:5, 3:5] = -2.0  # 内部洼地
    filled = priority_flood(dem, eps=1e-5)
    receiver = d8_receiver(filled)
    acc = flow_accumulation(filled, receiver)
    assert acc.max() > 1  # 存在汇流
    # 蓄满洼地底不再是无下游死区（eps 梯度指向溢出口）
    assert (receiver[4, 4] == -1) or filled[4, 4] > dem[4, 4] + 0.5


def test_upstream_mask_matches_accumulation():
    """上游掩膜面积 == 出流点汇流累积（内部一致性硬校验）。"""
    rng = np.random.default_rng(3)
    dem = rng.uniform(0.0, 1.0, size=(40, 40)).cumsum(axis=0).cumsum(axis=1)
    filled = priority_flood(dem, eps=1e-5)
    receiver = d8_receiver(filled)
    acc = flow_accumulation(filled, receiver)
    outlet = np.unravel_index(int(np.argmax(acc)), acc.shape)
    mask = upstream_mask(receiver, (int(outlet[0]), int(outlet[1])))
    assert float(mask.sum()) == pytest.approx(float(acc[outlet]), rel=1e-6)


def test_fill_gaps_bresenham_continuous():
    """单格以上跳变被 Bresenham 填补，输出逐格 8 邻域连续。"""
    path = [(0, 0), (3, 3), (3, 6)]
    out, filled = fill_gaps(path)
    assert filled == 4
    for (r0, c0), (r1, c1) in zip(out[:-1], out[1:]):
        assert max(abs(r0 - r1), abs(c0 - c1)) <= 1


def test_rasterize_path_orientation_and_dedup():
    """段内按纬度定向（下游=低纬），加密后相邻 cell 连续。"""
    feats = [
        {
            "type": "Feature",
            "properties": {"DIST_DN_KM": 10.0},
            "geometry": {
                "type": "LineString",
                "coordinates": [[108.720, 22.2030], [108.720, 22.2010]],
            },
        }
    ]
    # 网格以投影后的线段中点为中心（EPSG:32649，200 m 格）
    from pyproj import Transformer

    tr = Transformer.from_crs("EPSG:4326", "EPSG:32649", always_xy=True)
    xc, yc = tr.transform(108.720, 22.202)
    transform = from_bounds(xc - 4000, yc - 4000, xc + 4000, yc + 4000, 40, 40)
    cells = rasterize_path_to_grid(feats, (40, 40), transform)
    assert len(cells) >= 2
    for (r0, c0), (r1, c1) in zip(cells[:-1], cells[1:]):
        assert max(abs(r0 - r1), abs(abs(c0 - c1))) <= 2


def test_rasterize_empty_feature_returns_empty():
    cells = rasterize_path_to_grid(
        [], (40, 40), _grid_transform((40, 40))
    )
    assert cells == []
