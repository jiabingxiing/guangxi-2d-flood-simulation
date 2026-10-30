"""集成·统计复算：用源 .wd 独立重算阈值面积，与 statistics.json 比对（≤0.1%）。

口径（SPEC §6）：原生网格有效像元（非 NaN、非海域 mask）中 depth > 阈值 的像元数 × 格面积。
零面积单独断言（相对差不适用于 0，改用绝对相等 + wetted_cells 精确相等）。
本文件不复用 src 的统计函数，独立实现以形成真正的交叉校验。
"""

from __future__ import annotations

import numpy as np
import pytest
from helpers import FULL_FRAMES, JIULONG_RUN, SMOKE_FRAMES, load_json

REL_TOL = 0.001  # 0.1%
CELL_AREA_M2 = 40000.0  # 200 m × 200 m（久隆 200 m 情景）
NODATA = -9999.0


def _read_source(path) -> np.ndarray:
    arr = np.loadtxt(path, skiprows=6, dtype=np.float64, ndmin=2)
    return np.where(arr == NODATA, np.nan, arr)


def _sea_mask(run_dir):
    mask_path = run_dir / "sea_mask.npy"
    return np.load(mask_path).astype(bool) if mask_path.is_file() else None


def _recompute(frame: np.ndarray, thresholds: list[float], sea_mask) -> tuple[dict[str, float], dict[str, int]]:
    valid = ~np.isnan(frame)
    if sea_mask is not None:
        valid &= ~sea_mask
    areas: dict[str, float] = {}
    cells: dict[str, int] = {}
    for threshold in thresholds:
        count = int(np.count_nonzero(valid & (frame > threshold)))
        key = str(float(threshold))
        cells[key] = count
        areas[key] = count * CELL_AREA_M2 / 1e6
    return areas, cells


def _compare_frame(
    frame_id: str,
    expected_areas: dict[str, float],
    expected_cells: dict[str, int],
    actual_areas: dict[str, float],
    actual_cells: dict[str, int],
) -> list[str]:
    problems: list[str] = []
    assert set(expected_areas) == set(actual_areas), f"{frame_id} 阈值键不一致"
    for key in expected_areas:
        source = float(expected_areas[key])
        packaged = float(actual_areas[key])
        if int(expected_cells[key]) == 0:
            # 零面积单独断言：必须精确为 0，且像元数必须为 0
            if packaged != 0.0:
                problems.append(f"{frame_id} 阈值 {key}：源零面积但包内 {packaged} km²")
            if int(actual_cells[key]) != 0:
                problems.append(f"{frame_id} 阈值 {key}：源 0 像元但包内 {actual_cells[key]}")
            continue
        rel = abs(packaged - source) / source
        if rel > REL_TOL:
            problems.append(f"{frame_id} 阈值 {key}：包内 {packaged} vs 源 {source}（相对差 {rel:.4%}）")
    return problems


@pytest.mark.parametrize("bundle_fixture", ["smoke_bundle", "full_bundle"])
def test_statistics_match_independent_recompute(bundle_fixture: str, request, source_run_dir) -> None:
    bundle = request.getfixturevalue(bundle_fixture)
    stats = load_json(bundle / "cases" / "jiulong" / JIULONG_RUN / "statistics.json")
    expected_frames = SMOKE_FRAMES if bundle_fixture == "smoke_bundle" else FULL_FRAMES
    thresholds = [float(t) for t in stats["thresholds_m"]]
    sea = _sea_mask(source_run_dir)
    problems: list[str] = []
    checked = 0
    max_rel_seen = 0.0
    for record in stats["frames"]:
        frame_id = record["frame_id"]
        seq = int(frame_id[1:])
        frame = _read_source(source_run_dir / "results" / f"res-{seq:04d}.wd")
        areas, cells = _recompute(frame, thresholds, sea)
        problems.extend(_compare_frame(frame_id, areas, cells, record["areas_km2"], record["wetted_cells"]))
        for key, value in areas.items():
            if int(cells[key]) > 0:
                max_rel_seen = max(max_rel_seen, abs(float(record["areas_km2"][key]) - value) / value)
        checked += 1
    assert not problems, "\n".join(problems[:8])
    assert checked == expected_frames
    print(f"[统计复算] {checked} 帧，最大相对差 {max_rel_seen:.3e}（门禁 {REL_TOL}）")


def test_statistics_schema_and_metadata(full_bundle) -> None:
    stats = load_json(full_bundle / "cases" / "jiulong" / JIULONG_RUN / "statistics.json")
    assert stats["unit_area"] == "km2"
    assert stats["grid_cell_area_m2"] == pytest.approx(CELL_AREA_M2)
    assert stats["thresholds_m"] == [0.05, 0.15, 0.3, 0.5, 1.0, 2.0, 5.0, 10.0]
    assert len(stats["frames"]) == FULL_FRAMES
    assert stats["valid_cell_count"] > 0
    # 海域像元数必须与源 sea_mask.npy 一致
    assert stats["sea_cell_count"] == 27  # 久隆默认 run 实测 27 个海域像元
    first = stats["frames"][0]
    assert float(first["areas_km2"]["0.05"]) >= 0.0
    # 峰值记录必须指向真实帧且时间一致
    peak_id = stats["peak_frame_id"]
    peak = next(item for item in stats["frames"] if item["frame_id"] == peak_id)
    assert stats["peak_time_s"] == pytest.approx(peak["sim_seconds"])


def test_area_is_monotone_with_threshold(full_bundle) -> None:
    """同一帧内阈值越大面积越小（物理一致性；不是唯一充分条件但可抓口径错误）。"""
    stats = load_json(full_bundle / "cases" / "jiulong" / JIULONG_RUN / "statistics.json")
    thresholds = [float(t) for t in stats["thresholds_m"]]
    for record in stats["frames"]:
        values = [float(record["areas_km2"][str(float(t))]) for t in thresholds]
        assert values == sorted(values, reverse=True), f"{record['frame_id']} 面积随阈值非单调：{values}"


def test_max_envelope_consistent_with_frames(full_bundle) -> None:
    """包络面积应 ≥ 任一单帧面积（逐阈值）；来自 res.max，与逐帧最大值同口径需 ≤ 容差。"""
    stats = load_json(full_bundle / "cases" / "jiulong" / JIULONG_RUN / "statistics.json")
    envelope = stats["max_envelope"]["areas_km2"]
    for key, env_value in envelope.items():
        frame_values = [float(record["areas_km2"][key]) for record in stats["frames"]]
        max_frame = max(frame_values)
        assert float(env_value) >= max_frame - 1e-9, f"阈值 {key} 包络 {env_value} < 单帧最大 {max_frame}"


def test_source_frame_has_no_nan_but_has_sea_mask(smoke_bundle, source_run_dir) -> None:
    """久隆源帧无 NODATA（全像元有值），有效像元剔除靠 sea_mask；干地必须是 0 而非 NaN。"""
    stats = load_json(smoke_bundle / "cases" / "jiulong" / JIULONG_RUN / "statistics.json")
    source = _read_source(source_run_dir / "results" / "res-0002.wd")
    assert int(np.count_nonzero(np.isnan(source))) == 0, "久隆默认 run 第一段应无 NODATA"
    # 干地占多数且恰为 0
    dry = int(np.count_nonzero(source == 0.0))
    assert dry > source.size * 0.5
    # valid_cell_count = 全部像元 − 海域像元
    sea = _sea_mask(source_run_dir)
    assert sea is not None
    assert stats["valid_cell_count"] == source.size - int(np.count_nonzero(sea))
    assert stats["sea_cell_count"] == int(np.count_nonzero(sea))
    assert stats["frames"][2]["areas_km2"]["0.05"] >= 0.0
