"""DEM_REPAIRED v2 融合修补单元测试（合成算例先行，ADR-0022）。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.stage3.prepare_jiulong_dem_layers_v2 import (  # noqa: E402
    REASON_GLO30_FUSION,
    REASON_NONE,
    datum_offset,
    fuse_negatives,
)


def test_datum_offset_median_on_land_overlap():
    """偏移 = 陆域重叠区 median(raw − glo30)；样本不足即拒绝。"""
    rng = np.random.default_rng(0)
    raw = rng.uniform(5.0, 50.0, size=(20, 20))
    glo30 = raw - 22.5  # glo30 比 raw 低 22.5 → offset = +22.5
    land = np.ones_like(raw, dtype=bool)
    off = datum_offset(raw, glo30, land)
    assert off["offset_m"] == pytest.approx(22.5, abs=1e-6)
    with pytest.raises(LayerError := __import__("scripts.stage3.prepare_jiulong_dem_layers", fromlist=["LayerError"]).LayerError):
        datum_offset(raw, np.full_like(raw, np.nan), land)


def test_fuse_replaces_unresolved_negatives_and_marks_reason():
    """未解析负值被对齐值替换且 reason=3；正值格与海锚格不动。"""
    raw = np.array([[-3.0, 2.0], [-1.5, 10.0]])
    repaired = raw.copy()
    reason = np.zeros_like(raw, dtype=np.uint8)
    reason[1, 1] = 2  # 海锚格
    glo30_aligned = np.array([[-0.5, 1.0], [-0.2, 9.0]])
    valid = np.ones_like(raw, dtype=bool)
    sea = np.zeros_like(raw, dtype=bool)
    fused, reason_out, ledger = fuse_negatives(repaired, reason, glo30_aligned, valid, sea)
    assert fused[0, 0] == pytest.approx(-0.5)
    assert fused[0, 1] == pytest.approx(2.0)  # 正值不动
    assert fused[1, 1] == pytest.approx(10.0)  # 海锚格不动
    assert reason_out[0, 0] == REASON_GLO30_FUSION
    assert reason_out[0, 1] == REASON_NONE
    assert reason_out[1, 1] == 2
    assert ledger["fused_cells"] == 2
    assert ledger["still_negative_cells"] == 2  # 对齐值本身仍为负（近岸证据值）


def test_fuse_ledger_volume_matches_integrated_raise():
    """体积账目 = Σ(对齐值 − 原值)，挖与填分开记。"""
    raw = np.array([[-5.0, -1.0]])
    reason = np.zeros_like(raw, dtype=np.uint8)
    glo30_aligned = np.array([[-2.0, -3.0]])
    fused, _, ledger = fuse_negatives(
        raw, reason, glo30_aligned, np.ones_like(raw, dtype=bool),
        np.zeros_like(raw, dtype=bool),
    )
    assert fused[0, 0] == pytest.approx(-2.0)  # 抬 3 m
    assert fused[0, 1] == pytest.approx(-3.0)  # 挖 2 m
    assert ledger["fill_volume_m3"] == pytest.approx(1.0)
    assert ledger["dig_volume_m3"] == pytest.approx(-2.0)
