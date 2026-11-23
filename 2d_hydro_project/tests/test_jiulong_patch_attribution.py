"""diagnose_jiulong_patch_attribution 分类逻辑的合成网格单元测试。

只测纯函数（``classify_patch_indicators`` / ``match_tracks`` /
``bfs_distance_cells``）与 verdict 口径，不读任何 run 产物；阈值与规则优先级
见脚本模块 docstring（计划 §4.1/§4.2 任务 1）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ))

from scripts.stage6 import diagnose_jiulong_patch_attribution as PA  # noqa: E402


def base_indicators(**over):
    """一组不命中任何特殊类的中性指标（median ≥ 0.10、无编辑、离河远）。"""
    indicators = {
        "sea_frac": 0.0,
        "dem_edit_frac": 0.0,
        "terrain_edit_overlap": 0.0,
        "touches_edge": False,
        "median_depth_m": 0.5,
        "persistence_frames": 8,
        "distance_to_river_m": 1500.0,
        "distance_to_drainage_m": 800.0,
        "HAND_m": 4.0,
        "flow_acc_cells": 10,
        "patch_area_km2": 0.2,
        "largest_patch_area_km2": 37.4,
    }
    indicators.update(over)
    return indicators


# --------------------------------------------------------------------------- #
# 六个预注册类别的典型合成情形
# --------------------------------------------------------------------------- #
def test_sea_clamped_plateau_is_p05_high_confidence():
    cls, conf = PA.classify_patch_indicators(base_indicators(
        sea_frac=1.0, terrain_edit_overlap=1.0, median_depth_m=0.02, HAND_m=1.0,
    ))
    assert cls == "P05"
    assert conf == "high"


def test_deep_pit_no_edit_low_hand_high_acc_is_p01():
    cls, conf = PA.classify_patch_indicators(base_indicators(
        median_depth_m=0.8, HAND_m=2.0, flow_acc_cells=500, patch_area_km2=0.24,
    ))
    assert cls == "P01"
    assert conf == "high"


def test_thin_film_short_persistence_is_p06():
    cls, conf = PA.classify_patch_indicators(base_indicators(
        median_depth_m=0.02, persistence_frames=1,
    ))
    assert cls == "P06"
    assert conf == "high"


def test_unmatched_persistence_is_thin_water_candidate():
    # 未匹配轨迹按诚实口径记 persistence_frames=1，同样落入 P06 候选
    cls, _ = PA.classify_patch_indicators(base_indicators(
        median_depth_m=0.02, persistence_frames=1,
    ))
    assert cls == "P06"


def test_edge_touching_is_p07_when_not_p05_p02():
    cls, conf = PA.classify_patch_indicators(base_indicators(
        touches_edge=True, median_depth_m=0.3, persistence_frames=10,
    ))
    assert cls == "P07"
    assert conf == "high"


def test_near_river_low_hand_isolated_is_p03():
    cls, conf = PA.classify_patch_indicators(base_indicators(
        distance_to_river_m=400.0, HAND_m=2.0, patch_area_km2=0.12,
    ))
    assert cls == "P03"
    assert conf == "high"


def test_thin_but_persistent_far_from_river_is_p08_residual():
    # 薄水但轨迹持续 ≥ 3 帧、远离河道 → 落到 P08 兜底
    cls, conf = PA.classify_patch_indicators(base_indicators(
        median_depth_m=0.05, persistence_frames=5,
    ))
    assert cls == "P08"
    assert conf == "high"


# --------------------------------------------------------------------------- #
# 优先级与门槛
# --------------------------------------------------------------------------- #
def test_dem_edit_beats_edge_touch():
    # 触边 + DEM 填补过半 → P02 优先于 P07
    cls, _ = PA.classify_patch_indicators(base_indicators(
        dem_edit_frac=0.6, terrain_edit_overlap=0.6, touches_edge=True,
        HAND_m=2.0, flow_acc_cells=200,
    ))
    assert cls == "P02"


def test_isolation_gate_blocks_p03_for_main_body():
    # 近河且 HAND 低但面积 ≥ 0.5 × 最大斑块 → 不是 P03；median ≥ 0.10 → P04
    cls, _ = PA.classify_patch_indicators(base_indicators(
        distance_to_river_m=200.0, HAND_m=1.0,
        patch_area_km2=30.0, largest_patch_area_km2=37.4,
    ))
    assert cls == "P04"


def test_confidence_grades_by_support_condition_count():
    # P04 支持条件：median ≥ 0.10 / overlap == 0 / 离河 > 1000 / acc < 100
    assert PA.classify_patch_indicators(base_indicators())[1] == "high"
    assert PA.classify_patch_indicators(base_indicators(
        terrain_edit_overlap=0.2))[1] == "high"  # 3 条
    assert PA.classify_patch_indicators(base_indicators(
        terrain_edit_overlap=0.2, distance_to_river_m=500.0))[1] == "medium"  # 2 条
    assert PA.classify_patch_indicators(base_indicators(
        terrain_edit_overlap=0.2, distance_to_river_m=500.0,
        flow_acc_cells=200))[1] == "low"  # 1 条


@pytest.mark.parametrize("cls,expected", [
    ("P02", "repair_first"),
    ("P06", "filter_eligible"),
    ("P01", "preserve"),
    ("P05", "preserve"),
    ("P07", "preserve"),
    ("P08", "preserve"),
])
def test_verdict_policy(cls, expected):
    assert PA.verdict_for(cls) == expected


# --------------------------------------------------------------------------- #
# 纯 numpy 辅助函数
# --------------------------------------------------------------------------- #
def test_bfs_distance_cells_is_chebyshev_eight_neighbour():
    source = np.zeros((5, 5), dtype=bool)
    source[2, 2] = True
    dist = PA.bfs_distance_cells(source)
    assert dist[2, 2] == 0
    assert dist[1, 1] == 1  # 对角一步
    assert dist[0, 0] == 2  # 对角两步
    assert dist[2, 4] == 2
    assert dist[4, 4] == 2


def test_bfs_distance_cells_rejects_empty_source():
    with pytest.raises(ValueError):
        PA.bfs_distance_cells(np.zeros((3, 3), dtype=bool))


def test_match_tracks_prefers_longest_window_and_reports_unmatched():
    frames = [
        {"seq": 5, "centroid_row": 10.0, "centroid_col": 20.0},
        {"seq": 6, "centroid_row": 10.2, "centroid_col": 20.1},
        {"seq": 20, "centroid_row": 50.0, "centroid_col": 60.0},
    ]
    tracks = [
        {"track_id": 1, "frame_count": 16, "longest_consecutive_run": 16,
         "first_frame": 5, "last_frame": 20},
        {"track_id": 2, "frame_count": 2, "longest_consecutive_run": 2,
         "first_frame": 5, "last_frame": 6},
    ]
    centroids = np.array([[10.1, 20.2], [50.1, 60.2], [100.0, 100.0]])
    matched = PA.match_tracks(centroids, frames, tracks)
    # 两个轨迹都覆盖命中帧 → 取 frame_count 最大者；远离任何逐帧质心 → -1
    assert matched[0] == 1
    assert matched[1] == 1
    assert matched[2] == -1


def test_match_tracks_tie_prefers_smaller_track_id():
    frames = [{"seq": 1, "centroid_row": 0.0, "centroid_col": 0.0}]
    tracks = [
        {"track_id": 7, "frame_count": 2, "longest_consecutive_run": 2,
         "first_frame": 1, "last_frame": 2},
        {"track_id": 3, "frame_count": 2, "longest_consecutive_run": 2,
         "first_frame": 1, "last_frame": 2},
    ]
    matched = PA.match_tracks(np.array([[0.2, 0.1]]), frames, tracks)
    assert matched[0] == 3
