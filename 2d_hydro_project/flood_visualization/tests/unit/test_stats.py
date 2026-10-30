"""统计单元测试：阈值面积口径、零面积、海域剔除、流式累积器、与源统计比对。"""

from __future__ import annotations

import json

import jsonschema
import numpy as np
import pytest

from conftest import PROJECT_ROOT
from floodviz.quality.stats import (
    StatisticsBuilder,
    StatsError,
    compare_stats,
    compute_areas,
    envelope_from_frames,
    envelope_stats,
    threshold_key,
    threshold_keys,
    valid_cell_mask,
)

CELL_AREA = 40000.0  # 200 m × 200 m → 0.04 km²/像元


def _schema() -> dict:
    return json.loads((PROJECT_ROOT / "schemas" / "statistics.schema.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 阈值面积


def test_compute_areas_counts_strictly_above_threshold() -> None:
    depth = np.array(
        [
            [np.nan, 0.0, 0.06, 0.5],
            [1.0, 0.05, 0.051, 2.0],
        ],
        dtype=np.float32,
    )
    result = compute_areas(depth, [0.05, 0.5], CELL_AREA)
    # NaN 不算有效像元；恰等于 0.05 的不计（严格 >）
    assert result["valid_cell_count"] == 7
    assert result["wetted_cells"] == {"0.05": 5, "0.5": 2}
    expected_area_005 = round(5 * CELL_AREA / 1e6, 6)
    expected_area_05 = round(2 * CELL_AREA / 1e6, 6)
    assert result["areas_km2"] == {"0.05": expected_area_005, "0.5": expected_area_05}
    assert result["areas_km2"]["0.05"] == pytest.approx(0.2)
    assert result["areas_km2"]["0.5"] == pytest.approx(0.08)
    assert result["max_depth_m"] == pytest.approx(2.0)
    assert result["sea_cell_count"] == 0


def test_compute_areas_excludes_sea_mask() -> None:
    depth = np.array([[1.0, 2.0, 0.0], [0.2, 0.3, 0.4]], dtype=np.float32)
    sea = np.array([[True, False, False], [False, False, False]])
    result = compute_areas(depth, [0.05], CELL_AREA, sea_mask=sea)
    assert result["sea_cell_count"] == 1
    assert result["valid_cell_count"] == 5
    assert result["wetted_cells"]["0.05"] == 4  # 浸没像元 1.0 在被剔除的海域里


def test_compute_areas_zero_area_is_zero_not_blank() -> None:
    result = compute_areas(np.zeros((4, 4), dtype=np.float32), [0.05, 0.5], CELL_AREA)
    assert result["areas_km2"] == {"0.05": 0.0, "0.5": 0.0}
    assert result["wetted_cells"] == {"0.05": 0, "0.5": 0}
    assert result["max_depth_m"] == pytest.approx(0.0)


def test_compute_areas_all_nan() -> None:
    result = compute_areas(np.full((3, 3), np.nan, dtype=np.float32), [0.05], CELL_AREA)
    assert result["valid_cell_count"] == 0
    assert result["areas_km2"]["0.05"] == 0.0
    assert result["max_depth_m"] == pytest.approx(0.0)


def test_compute_areas_rejects_bad_input() -> None:
    with pytest.raises(StatsError, match="必须是二维"):
        compute_areas(np.zeros(3, dtype=np.float32), [0.05], CELL_AREA)
    with pytest.raises(StatsError, match="格面积必须为正"):
        compute_areas(np.zeros((2, 2), dtype=np.float32), [0.05], 0.0)


def test_valid_cell_mask_shape_guard() -> None:
    depth = np.zeros((2, 2), dtype=np.float32)
    with pytest.raises(StatsError, match="形状"):
        valid_cell_mask(depth, np.zeros((2, 3), dtype=bool))


def test_threshold_keys_are_strings_and_unique() -> None:
    assert threshold_key(0.05) == "0.05"
    assert threshold_key(1.0) == "1.0"
    assert threshold_key(10) == "10.0"
    assert threshold_keys([0.05, 0.5, 2.0]) == ["0.05", "0.5", "2.0"]
    with pytest.raises(StatsError, match="重复"):
        threshold_keys([0.05, 0.05])


# ---------------------------------------------------------------- 包络


def test_envelope_stats() -> None:
    max_depth = np.array([[0.0, 0.1], [1.0, np.nan]], dtype=np.float32)
    result = envelope_stats(max_depth, [0.05, 0.5], CELL_AREA)
    assert result["wetted_cells"] == {"0.05": 2, "0.5": 1}
    assert result["areas_km2"]["0.05"] == pytest.approx(0.08)
    assert result["max_depth_m"] == pytest.approx(1.0)


def test_envelope_from_frames_takes_per_threshold_max() -> None:
    frames = [
        {"areas_km2": {"0.05": 1.0, "0.5": 0.2}, "wetted_cells": {"0.05": 25, "0.5": 5}},
        {"areas_km2": {"0.05": 0.8, "0.5": 0.4}, "wetted_cells": {"0.05": 20, "0.5": 10}},
    ]
    env = envelope_from_frames(frames, [0.05, 0.5])
    assert env["areas_km2"] == {"0.05": 1.0, "0.5": 0.4}
    assert env["wetted_cells"] == {"0.05": 25, "0.5": 10}


# ---------------------------------------------------------------- 流式累积器


def _builder() -> StatisticsBuilder:
    return StatisticsBuilder(thresholds=[0.05, 0.5], cell_area_m2=CELL_AREA, default_threshold_m=0.05)


def test_statistics_builder_peak_and_doc_shape() -> None:
    builder = _builder()
    dry = np.zeros((2, 2), dtype=np.float32)
    wet = np.array([[0.1, 0.1], [1.0, 0.0]], dtype=np.float32)
    builder.add_frame(frame_id="f0000", sim_seconds=0.0, timestamp=None, depth=dry)
    builder.add_frame(frame_id="f0001", sim_seconds=21600.0, timestamp=None, depth=wet)
    builder.add_frame(frame_id="f0002", sim_seconds=43200.0, timestamp=None, depth=wet / 2)
    doc = builder.to_dict(case_id="demo", run_id="run_a", max_envelope=None)

    assert doc["schema_version"] == "1.0"
    assert doc["unit_area"] == "km2"
    assert doc["thresholds_m"] == [0.05, 0.5]
    assert doc["grid_cell_area_m2"] == CELL_AREA
    assert doc["valid_cell_count"] == 4
    assert len(doc["frames"]) == 3
    assert doc["peak_frame_id"] == "f0001"
    assert doc["peak_time_s"] == pytest.approx(21600.0)
    assert doc["peak_max_depth_m"] == pytest.approx(1.0)
    assert doc["peak_max_depth_frame_id"] == "f0001"
    assert "max_envelope" not in doc
    jsonschema.validate(doc, _schema())


def test_statistics_builder_with_envelope() -> None:
    builder = _builder()
    wet = np.array([[0.1, 1.0]], dtype=np.float32)
    builder.add_frame(frame_id="f0000", sim_seconds=0.0, timestamp="2024-09-03T00:00:00+08:00", depth=wet)
    envelope = {"areas_km2": {"0.05": 0.08, "0.5": 0.04}, "wetted_cells": {"0.05": 2, "0.5": 1}, "max_depth_m": 1.0}
    doc = builder.to_dict(case_id="demo", run_id="run_a", max_envelope=envelope)
    assert doc["max_envelope"]["peak_frame_id"] == "f0000"
    assert doc["max_envelope"]["areas_km2"]["0.05"] == pytest.approx(0.08)
    jsonschema.validate(doc, _schema())


def test_statistics_builder_rejects_duplicate_thresholds() -> None:
    with pytest.raises(StatsError, match="重复"):
        StatisticsBuilder(thresholds=[0.05, 0.05], cell_area_m2=CELL_AREA, default_threshold_m=0.05)


# ---------------------------------------------------------------- 与源统计比对


def _frame_stats(areas: dict[str, float], cells: dict[str, int]) -> dict:
    return {"frame_id": "f0000", "sim_seconds": 0.0, "timestamp": None, "areas_km2": areas, "wetted_cells": cells}


def test_compare_stats_identical() -> None:
    expected = {"frames": [_frame_stats({"0.05": 1.0}, {"0.05": 25})]}
    assert compare_stats(expected, expected) == []


def test_compare_stats_within_tolerance() -> None:
    expected = {"frames": [_frame_stats({"0.05": 1.0}, {"0.05": 25})]}
    actual = {"frames": [_frame_stats({"0.05": 1.0005}, {"0.05": 25})]}  # 0.05% < 0.1%
    assert compare_stats(expected, actual) == []


def test_compare_stats_beyond_tolerance() -> None:
    expected = {"frames": [_frame_stats({"0.05": 1.0}, {"0.05": 25})]}
    actual = {"frames": [_frame_stats({"0.05": 1.002}, {"0.05": 25})]}  # 0.2% > 0.1%
    problems = compare_stats(expected, actual)
    assert len(problems) == 1
    assert "面积" in problems[0]


def test_compare_stats_cell_count_mismatch() -> None:
    expected = {"frames": [_frame_stats({"0.05": 1.0}, {"0.05": 25})]}
    actual = {"frames": [_frame_stats({"0.05": 1.0}, {"0.05": 26})]}
    problems = compare_stats(expected, actual)
    assert any("像元数" in problem for problem in problems)


def test_compare_stats_frame_set_mismatch() -> None:
    expected = {"frames": [_frame_stats({"0.05": 1.0}, {"0.05": 25})]}
    actual = {"frames": []}
    problems = compare_stats(expected, actual)
    assert any("帧集合不一致" in problem for problem in problems)


def test_compare_stats_missing_threshold_value() -> None:
    expected = {"frames": [_frame_stats({"0.05": 1.0, "0.5": 0.5}, {"0.05": 25, "0.5": 12})]}
    actual = {"frames": [{"frame_id": "f0000", "areas_km2": {"0.05": 1.0}, "wetted_cells": {"0.05": 25}}]}
    problems = compare_stats(expected, actual)
    assert any("缺统计" in problem for problem in problems)
