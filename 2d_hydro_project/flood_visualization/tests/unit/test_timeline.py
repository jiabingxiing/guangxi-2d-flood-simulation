"""时间轴单元测试：相对秒、绝对时间格式、时间契约自检（禁止猜填时间）。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from conftest import PROJECT_ROOT
from floodviz.catalog.timeline import (
    build_timeline,
    compute_sim_seconds,
    format_timestamp,
    origin_has_absolute_time,
    validate_timeline,
)
from floodviz.quality.frames import FrameInfo

TIMELINE_SCHEMA = PROJECT_ROOT / "schemas" / "timeline.schema.json"


def _frames(seqs: list[int]) -> list[FrameInfo]:
    header = {
        "ncols": 8.0,
        "nrows": 6.0,
        "xllcorner": 246900.0,
        "yllcorner": 2456400.0,
        "cellsize": 200.0,
        "nodata_value": -9999.0,
    }
    return [
        FrameInfo(seq=seq, path=Path(f"res-{seq:04d}.wd"), sha256=f"{seq:064x}", header=header, size_bytes=1024)
        for seq in seqs
    ]


def _build(config, case_id: str, seqs: list[int], run_id: str | None = None) -> dict[str, Any]:
    case = config.case(case_id)
    run = case.run(run_id)
    return build_timeline(
        case,
        run,
        _frames(seqs),
        images_prefix=f"frames/{case_id}/{run.run_id}",
        blocks_prefix=f"numeric/{case_id}/{run.run_id}",
        statistics_path=f"cases/{case_id}/{run.run_id}/statistics.json",
        numeric_manifest_path=f"numeric/{case_id}/{run.run_id}/manifest.json",
    )


def _schema(name: str) -> dict[str, Any]:
    return json.loads((PROJECT_ROOT / "schemas" / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 相对时间


def test_compute_sim_seconds() -> None:
    assert compute_sim_seconds(0, 21600) == 0.0
    assert compute_sim_seconds(1, 21600) == 21600.0
    assert compute_sim_seconds(20, 21600) == 432000.0
    assert compute_sim_seconds(5, 3600) == 18000.0


def test_compute_sim_seconds_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="帧序号不能为负"):
        compute_sim_seconds(-1, 21600)
    with pytest.raises(ValueError, match="save_interval_s 必须为正"):
        compute_sim_seconds(1, 0)
    with pytest.raises(ValueError, match="save_interval_s 必须为正"):
        compute_sim_seconds(1, -3600)


# ---------------------------------------------------------------- 绝对时间


def test_origin_has_absolute_time() -> None:
    assert origin_has_absolute_time("model_schedule_confirmed")
    assert not origin_has_absolute_time("relative_only_candidate")
    assert not origin_has_absolute_time("relative_only")


def test_format_timestamp_relative_only_is_none() -> None:
    """起点未确认（relative_only*）必须返回 None，禁止猜填绝对时间。"""
    assert format_timestamp("2024-09-05T00:00:00+08:00", 21600, origin_status="relative_only_candidate") is None
    assert format_timestamp("2024-09-05T00:00:00+08:00", 0, origin_status="relative_only") is None


def test_format_timestamp_confirmed_keeps_offset() -> None:
    assert (
        format_timestamp("2024-09-03T00:00:00+08:00", 0, origin_status="model_schedule_confirmed")
        == "2024-09-03T00:00:00+08:00"
    )
    assert (
        format_timestamp("2024-09-03T00:00:00+08:00", 21600, origin_status="model_schedule_confirmed")
        == "2024-09-03T06:00:00+08:00"
    )
    assert (
        format_timestamp("2024-09-03T00:00:00+08:00", 1209600, origin_status="model_schedule_confirmed")
        == "2024-09-17T00:00:00+08:00"
    )


def test_format_timestamp_utc_origin() -> None:
    assert format_timestamp("2024-09-03T00:00:00+00:00", 21600) == "2024-09-03T06:00:00+00:00"


def test_format_timestamp_none_origin_or_naive() -> None:
    assert format_timestamp(None, 0, origin_status="model_schedule_confirmed") is None
    with pytest.raises(ValueError, match="必须带时区偏移"):
        format_timestamp("2024-09-03T00:00:00", 0, origin_status="model_schedule_confirmed")


# ---------------------------------------------------------------- 构造与自检


def test_build_timeline_relative_candidate_has_null_timestamps(config) -> None:
    doc = _build(config, "jiulong", [0, 1, 2])
    assert doc["case_id"] == "jiulong"
    run_id = config.cases["jiulong"].default_run_id
    assert doc["run_id"] == run_id
    assert doc["origin_status"] == "relative_only_candidate"
    assert doc["time_origin"] is None
    assert doc["time_zone"] == "Asia/Shanghai"
    assert doc["save_interval_s"] == 21600.0
    assert doc["simulation_duration_s"] == 432000.0
    assert doc["temporal_interpolation"] == "none"
    assert doc["frame_count"] == 3
    assert [f["sim_seconds"] for f in doc["frames"]] == [0.0, 21600.0, 43200.0]
    assert all(f["timestamp"] is None for f in doc["frames"])
    first = doc["frames"][0]
    assert first["frame_id"] == "f0000"
    assert first["source_sequence"] == 0
    assert first["depth_image"] == f"frames/jiulong/{run_id}/f0000.png"
    assert first["query_blocks"] == f"numeric/jiulong/{run_id}/f0000/{{block_y}}/{{block_x}}.bin"
    assert first["sha256"] == "0" * 64


def test_build_timeline_confirmed_has_absolute_timestamps(config) -> None:
    doc = _build(config, "guigang", [0, 1, 56])
    assert doc["origin_status"] == "model_schedule_confirmed"
    assert doc["time_origin"] == "2024-09-03T00:00:00+08:00"
    assert doc["frame_count"] == 3
    assert [f["timestamp"] for f in doc["frames"]] == [
        "2024-09-03T00:00:00+08:00",
        "2024-09-03T06:00:00+08:00",
        "2024-09-17T00:00:00+08:00",
    ]


@pytest.mark.parametrize("case_id", ["jiulong", "ningming", "guigang"])
def test_built_timeline_passes_schema(config, case_id: str) -> None:
    run_id = config.case(case_id).default_run_id
    doc = _build(config, case_id, [0, 1, 2], run_id=run_id)
    jsonschema.validate(doc, _schema("timeline.schema.json"))
    # 契约自检与 schema 必须一致（都通过）
    validate_timeline(doc)


def test_build_timeline_rejects_frame_beyond_duration(config) -> None:
    """末帧超模拟结束 → 拒绝发布（久隆 21 帧 = seq 0..20；seq 21 = 453600 s > 432000 s）。"""
    with pytest.raises(ValueError, match="超过模拟结束"):
        _build(config, "jiulong", [0, 20, 21])


def test_build_timeline_rejects_non_increasing_sequence(config) -> None:
    with pytest.raises(ValueError, match="帧时间不严格递增"):
        _build(config, "jiulong", [0, 2, 1])


def test_build_timeline_rejects_duplicate_sequence(config) -> None:
    with pytest.raises(ValueError, match="帧时间不严格递增"):
        _build(config, "jiulong", [0, 1, 1])


# ---------------------------------------------------------------- 校验器拒绝项


def _valid_doc(config) -> dict[str, Any]:
    return _build(config, "guigang", [0, 1, 2])


def test_validate_timeline_accepts_valid(config) -> None:
    validate_timeline(_valid_doc(config))


def test_validate_timeline_rejects_empty_frames(config) -> None:
    doc = _valid_doc(config)
    doc["frames"] = []
    doc["frame_count"] = 0
    with pytest.raises(ValueError, match="frames 为空"):
        validate_timeline(doc)


def test_validate_timeline_rejects_frame_count_mismatch(config) -> None:
    doc = _valid_doc(config)
    doc["frame_count"] = len(doc["frames"]) + 1
    with pytest.raises(ValueError, match="frame_count"):
        validate_timeline(doc)


def test_validate_timeline_rejects_interpolation(config) -> None:
    doc = _valid_doc(config)
    doc["temporal_interpolation"] = "linear"
    with pytest.raises(ValueError, match="temporal_interpolation"):
        validate_timeline(doc)


def test_validate_timeline_rejects_timestamp_when_origin_unconfirmed(config) -> None:
    doc = _build(config, "jiulong", [0, 1, 2])
    doc["frames"][1]["timestamp"] = "2024-09-05T06:00:00+08:00"
    with pytest.raises(ValueError, match="origin 未确认却写了绝对 timestamp"):
        validate_timeline(doc)


def test_validate_timeline_rejects_missing_timestamp_when_confirmed(config) -> None:
    doc = _valid_doc(config)
    doc["frames"][1]["timestamp"] = None
    with pytest.raises(ValueError, match="origin 已确认但 timestamp 为空"):
        validate_timeline(doc)


def test_validate_timeline_rejects_unsorted_sim_seconds(config) -> None:
    doc = _valid_doc(config)
    doc["frames"][2]["sim_seconds"] = doc["frames"][0]["sim_seconds"]
    with pytest.raises(ValueError, match="sim_seconds 必须严格递增"):
        validate_timeline(doc)


def test_validate_timeline_rejects_duplicate_source_sequence(config) -> None:
    doc = _valid_doc(config)
    doc["frames"][2]["source_sequence"] = doc["frames"][1]["source_sequence"]
    with pytest.raises(ValueError, match="source_sequence"):
        validate_timeline(doc)


def test_validate_timeline_rejects_missing_frame_field(config) -> None:
    doc = _valid_doc(config)
    del doc["frames"][0]["sha256"]
    with pytest.raises(ValueError, match="缺少字段 'sha256'"):
        validate_timeline(doc)


def test_validate_timeline_rejects_absolute_depth_image(config) -> None:
    doc = _valid_doc(config)
    doc["frames"][0]["depth_image"] = "https://example.com/f0000.png"
    with pytest.raises(ValueError, match="depth_image 必须是发布包内相对路径"):
        validate_timeline(doc)


def test_validate_timeline_rejects_bad_query_template(config) -> None:
    doc = _valid_doc(config)
    doc["frames"][0]["query_blocks"] = "numeric/guigang/run/f0000/0/0.bin"
    with pytest.raises(ValueError, match="query_blocks 模板"):
        validate_timeline(doc)


def test_validate_timeline_mutation_does_not_touch_original(config) -> None:
    doc = _valid_doc(config)
    snapshot = copy.deepcopy(doc)
    validate_timeline(doc)
    assert doc == snapshot
