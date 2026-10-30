"""帧审计单元测试：缺帧、序号不连续、头不一致、超时长一律拒绝发布。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest

from conftest import SYNTH_COLS, SYNTH_ROWS, make_run_dir, synthetic_depth, write_ascii_grid, write_frame
from floodviz.quality.frames import (
    FrameInfo,
    audit_run,
    list_frame_files,
    sha256_file,
)

SAVEINT = 21600.0
DURATION = 43200.0  # 3 帧（seq 0/1/2）


def _audit(run_dir: Path, **kwargs):
    kwargs.setdefault("save_interval_s", SAVEINT)
    kwargs.setdefault("simulation_duration_s", DURATION)
    kwargs.setdefault("compute_sha256", False)
    return audit_run(run_dir, **kwargs)


def test_audit_contiguous_run_is_ok(tmp_path: Path) -> None:
    run_dir = make_run_dir(tmp_path / "run", [0, 1, 2])
    audit = _audit(run_dir)
    assert audit.ok, audit.reject_reasons
    assert audit.count == 3
    assert audit.expected_count == 3
    assert audit.contiguous is True
    assert audit.headers_identical is True
    assert [frame.frame_id for frame in audit.frames] == ["f0000", "f0001", "f0002"]
    assert [frame.filename for frame in audit.frames] == ["res-0000.wd", "res-0001.wd", "res-0002.wd"]
    assert audit.warnings  # 没有 res.max 只警告
    assert audit.max_grid_path is None


def test_audit_rejects_missing_frame_number(tmp_path: Path) -> None:
    run_dir = make_run_dir(tmp_path / "run", [0, 1, 3])
    audit = _audit(run_dir)
    assert not audit.ok
    assert audit.contiguous is False
    assert any("帧序号不连续" in reason for reason in audit.reject_reasons)
    # seq3 = 64800 s 也超过 43200 s 的模拟结束
    assert any("超过模拟结束" in reason for reason in audit.reject_reasons)


def test_audit_rejects_non_zero_first_sequence(tmp_path: Path) -> None:
    run_dir = make_run_dir(tmp_path / "run", [1, 2, 3])
    audit = _audit(run_dir, simulation_duration_s=3 * SAVEINT)
    assert not audit.ok
    assert any("首帧序号为 1" in reason for reason in audit.reject_reasons)


def test_audit_rejects_header_mismatch(tmp_path: Path) -> None:
    run_dir = make_run_dir(tmp_path / "run", [0, 1, 2])
    # 第 2 帧换成不同像元尺寸的头
    write_ascii_grid(run_dir / "results" / "res-0001.wd", synthetic_depth(), cellsize=100.0)
    audit = _audit(run_dir)
    assert not audit.ok
    assert audit.headers_identical is False
    assert any("栅格头不一致" in reason for reason in audit.reject_reasons)


def test_audit_rejects_last_frame_beyond_duration(tmp_path: Path) -> None:
    run_dir = make_run_dir(tmp_path / "run", [0, 1, 2, 3])
    audit = _audit(run_dir, simulation_duration_s=DURATION)  # seq3 = 64800 s > 43200 s
    assert not audit.ok
    assert any("超过模拟结束" in reason for reason in audit.reject_reasons)


def test_audit_rejects_insufficient_frames(tmp_path: Path) -> None:
    run_dir = make_run_dir(tmp_path / "run", [0, 1])
    audit = _audit(run_dir, simulation_duration_s=DURATION)
    assert not audit.ok
    assert any("帧数不足" in reason for reason in audit.reject_reasons)


def test_audit_extra_frames_only_warn(tmp_path: Path) -> None:
    run_dir = make_run_dir(tmp_path / "run", [0, 1, 2])
    audit = _audit(run_dir, expected_count=2)  # 显式期望 2 帧
    assert audit.ok, audit.reject_reasons
    assert audit.warnings
    assert any("帧数多于零起点期望值" in warning for warning in audit.warnings)


def test_audit_missing_run_dir(tmp_path: Path) -> None:
    audit = _audit(tmp_path / "nowhere")
    assert not audit.ok
    assert any("run 目录不存在" in reason for reason in audit.reject_reasons)


def test_audit_missing_results_dir(tmp_path: Path) -> None:
    (tmp_path / "run").mkdir()
    with pytest.raises(FileNotFoundError, match="缺少 results 目录"):
        _audit(tmp_path / "run")


def test_audit_empty_results(tmp_path: Path) -> None:
    (tmp_path / "run" / "results").mkdir(parents=True)
    audit = _audit(tmp_path / "run")
    assert not audit.ok
    assert any("没有 res-XXXX.wd" in reason for reason in audit.reject_reasons)


def test_audit_max_grid_mismatch_rejected(tmp_path: Path) -> None:
    run_dir = make_run_dir(tmp_path / "run", [0, 1, 2])
    write_ascii_grid(run_dir / "results" / "res.max", synthetic_depth(), cellsize=100.0)
    audit = _audit(run_dir)
    assert not audit.ok
    assert any("res.max 栅格头与帧头不一致" in reason for reason in audit.reject_reasons)


def test_audit_max_grid_present(tmp_path: Path) -> None:
    run_dir = make_run_dir(tmp_path / "run", [0, 1, 2])
    write_ascii_grid(run_dir / "results" / "res.max", synthetic_depth())
    audit = _audit(run_dir)
    assert audit.ok, audit.reject_reasons
    assert audit.max_grid_path == run_dir / "results" / "res.max"
    assert audit.to_dict()["max_grid_present"] is True


# ---------------------------------------------------------------- 文件工具


def test_list_frame_files_order_and_filter(tmp_path: Path) -> None:
    run_dir = make_run_dir(tmp_path / "run", [0, 2, 1])
    (run_dir / "results" / "notes.txt").write_text("ignore me", encoding="utf-8")
    (run_dir / "results" / "res-00x.wd").write_text("bad", encoding="utf-8")
    pairs = list_frame_files(run_dir)
    assert [seq for seq, _ in pairs] == [0, 1, 2]
    assert [path.name for _, path in pairs] == ["res-0000.wd", "res-0001.wd", "res-0002.wd"]


def test_sha256_file_matches_hashlib(tmp_path: Path) -> None:
    path = write_ascii_grid(tmp_path / "res-0000.wd", synthetic_depth())
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    assert sha256_file(path) == expected


def test_audit_computes_sha256_when_requested(tmp_path: Path) -> None:
    run_dir = make_run_dir(tmp_path / "run", [0, 1, 2])
    audit = _audit(run_dir, compute_sha256=True)
    assert audit.ok, audit.reject_reasons
    for frame in audit.frames:
        assert frame.sha256 == sha256_file(frame.path)
        assert len(frame.sha256) == 64


def test_audit_to_dict_is_json_ready(tmp_path: Path) -> None:
    import json

    run_dir = make_run_dir(tmp_path / "run", [0, 1, 2])
    payload = _audit(run_dir).to_dict()
    json.dumps(payload, ensure_ascii=False)
    assert payload["count"] == 3
    assert payload["save_interval_s"] == SAVEINT
    assert payload["frames"][0]["frame_id"] == "f0000"
    assert payload["run_dir"].endswith("run")


def test_frame_info_properties(tmp_path: Path) -> None:
    path = write_frame(tmp_path / "results", 7, synthetic_depth())
    header = {"ncols": SYNTH_COLS, "nrows": SYNTH_ROWS}
    frame = FrameInfo(seq=7, path=path, sha256="a" * 64, header=header, size_bytes=1)
    assert frame.frame_id == "f0007"
    assert frame.filename == "res-0007.wd"


def test_audit_mixed_frame_shapes_are_rejected(tmp_path: Path) -> None:
    run_dir = make_run_dir(tmp_path / "run", [0, 1, 2])
    # 换尺寸（行/列不同）也必须被头一致性拦住
    write_ascii_grid(run_dir / "results" / "res-0002.wd", np.zeros((SYNTH_ROWS + 1, SYNTH_COLS)))
    audit = _audit(run_dir)
    assert not audit.ok
    assert audit.headers_identical is False
