"""配置层单元测试：合法加载、案例/run 解析、图层状态机、非法配置拒绝。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from conftest import PROJECT_ROOT, patch_text
from floodviz.config import (
    ALLOWED_LAYER_STATUS,
    ALLOWED_ORIGIN_STATUS,
    ENABLED_LAYER_STATUS,
    ConfigError,
    load_config,
)

JIULONG = "cases/jiulong.yaml"
DEFAULT = "default.yaml"
LAYERS = "layers.yaml"


def _expect_config_error(config_copy: Callable[[], Path], mutate: Callable[[Path], None], match: str) -> None:
    """复制 configs → 变异 → 断言 load_config 抛 ConfigError。"""
    default = config_copy()
    mutate(default.parent)
    with pytest.raises(ConfigError, match=match):
        load_config(default)


# ---------------------------------------------------------------- 合法加载


def test_loads_real_config(config, project_root: Path) -> None:
    assert config.root == project_root
    assert config.parent_root == project_root.parent
    assert config.case_ids == ("jiulong", "ningming", "guigang")
    assert config.default_case == "jiulong"
    assert config.thresholds_m == (0.05, 0.15, 0.3, 0.5, 1.0, 2.0, 5.0, 10.0)
    assert config.default_threshold_m == pytest.approx(0.05)
    assert len(config.depth_classes) == 8
    assert config.depth_classes[-1]["max_m"] is None
    assert config.block_size == 256
    assert config.image_max_pixels == 4096
    assert config.bundle_root == (project_root / "outputs" / "web_bundle").resolve()
    assert len(config.basemaps) == 2
    # 2026-10-28 新增地形分层：源地形层 + 地形修改证据层，预设由 6 个增至 8 个
    assert len(config.presets) == 8
    assert [p["id"] for p in config.presets] == [
        "optical_only",
        "sar_only",
        "dem_only",
        "terrain_source_only",
        "terrain_edit_only",
        "flood_only",
        "all_available",
        "custom",
    ]
    assert config.layer_order[0] == "basemap" and "flood" in config.layer_order
    # 地形分层顺序固定：源地形 -> 修改证据 -> 模型地形
    assert config.layer_order.index("terrain_source") < config.layer_order.index("terrain_edit")
    assert config.layer_order.index("terrain_edit") < config.layer_order.index("dem")


def test_run_frame_counts_match_contract(config) -> None:
    """SUCCESS 基准：久隆 21 / 宁明 19 / 贵港 57 帧（SPEC §2）。"""
    assert config.case("jiulong").run().expected_frame_count == 21
    assert config.case("ningming").run().expected_frame_count == 19
    assert config.case("guigang").run().expected_frame_count == 57
    assert config.case("jiulong").run().save_interval_s == 21600.0
    assert config.case("guigang").run().simulation_duration_s == 1209600.0


def test_origin_status_policy(config) -> None:
    """久隆/宁明起点是候选（不得写绝对时间）；贵港 meta 有 t0。"""
    assert config.case("jiulong").run().origin_status == "relative_only_candidate"
    assert config.case("ningming").run().origin_status == "relative_only_candidate"
    assert config.case("guigang").run().origin_status == "model_schedule_confirmed"
    assert config.case("guigang").run().time_origin == "2024-09-03T00:00:00+08:00"
    assert config.case("jiulong").run().time_origin == "2024-09-05T00:00:00+08:00"
    assert set(ALLOWED_ORIGIN_STATUS) == {
        "model_schedule_confirmed",
        "relative_only_candidate",
        "relative_only",
    }


def test_layer_state_machine_constants(config) -> None:
    assert {
        "remote_verified",
        "local_unchecked",
        "ready",
        "partial",
        "missing",
        "failed",
    } == ALLOWED_LAYER_STATUS
    assert {"ready", "partial"} == ENABLED_LAYER_STATUS
    jiulong = config.case("jiulong")
    assert jiulong.layers["flood"].selectable
    assert jiulong.layers["dem"].selectable
    assert jiulong.layers["max"].selectable
    # 未质检的本地影像默认禁用，且必须带原因文案
    assert not jiulong.layers["optical"].selectable
    assert jiulong.layers["optical"].summary and jiulong.layers["optical"].note


def test_case_and_run_lookup_errors(config) -> None:
    with pytest.raises(ConfigError, match="未知案例"):
        config.case("nowhere")
    with pytest.raises(ConfigError, match="无 run"):
        config.case("jiulong").run("nowhere")


def test_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="配置文件不存在"):
        load_config(tmp_path / "not_there.yaml")


# ---------------------------------------------------------------- 非法配置


def test_reject_illegal_layer_status(config_copy) -> None:
    _expect_config_error(
        config_copy,
        lambda configs: patch_text(configs / JIULONG, "dem: {status: ready", "dem: {status: bogus"),
        r"status='bogus' 非法",
    )


def test_reject_bbox_wrong_order(config_copy) -> None:
    def mutate(configs: Path) -> None:
        patch_text(
            configs / JIULONG,
            "model_bbox: [108.545265, 21.92, 108.902171, 22.199335]",
            "model_bbox: [109.0, 22.2, 108.5, 21.9]",
        )

    _expect_config_error(config_copy, mutate, r"\[西,南,东,北\]")


def test_reject_bbox_wrong_length(config_copy) -> None:
    def mutate(configs: Path) -> None:
        patch_text(
            configs / JIULONG,
            "model_bbox: [108.545265, 21.92, 108.902171, 22.199335]",
            "model_bbox: [108.5, 21.92, 108.9]",
        )

    _expect_config_error(config_copy, mutate, r"model_bbox 必须是 4 个数")


def test_reject_decreasing_thresholds(config_copy) -> None:
    def mutate(configs: Path) -> None:
        patch_text(
            configs / DEFAULT,
            "thresholds_m: [0.05, 0.15, 0.3, 0.5, 1.0, 2.0, 5.0, 10.0]",
            "thresholds_m: [0.3, 0.1]",
        )

    _expect_config_error(config_copy, mutate, r"必须是严格递增的正数")


def test_reject_non_positive_threshold(config_copy) -> None:
    def mutate(configs: Path) -> None:
        patch_text(
            configs / DEFAULT,
            "thresholds_m: [0.05, 0.15, 0.3, 0.5, 1.0, 2.0, 5.0, 10.0]",
            "thresholds_m: [0.0, 0.15]",
        )

    _expect_config_error(config_copy, mutate, r"必须是严格递增的正数")


def test_reject_duplicate_thresholds(config_copy) -> None:
    def mutate(configs: Path) -> None:
        patch_text(
            configs / DEFAULT,
            "thresholds_m: [0.05, 0.15, 0.3, 0.5, 1.0, 2.0, 5.0, 10.0]",
            "thresholds_m: [0.05, 0.05, 0.3]",
        )

    _expect_config_error(config_copy, mutate, r"严格递增")


def test_reject_missing_thresholds(config_copy) -> None:
    def mutate(configs: Path) -> None:
        patch_text(
            configs / DEFAULT,
            "thresholds_m: [0.05, 0.15, 0.3, 0.5, 1.0, 2.0, 5.0, 10.0]\n",
            "",
        )

    _expect_config_error(config_copy, mutate, r"缺少必填字段 'thresholds_m'")


def test_reject_missing_depth_classes(config_copy) -> None:
    def mutate(configs: Path) -> None:
        patch_text(configs / DEFAULT, "\ndepth_classes:", "\ndepth_classes_renamed:")

    _expect_config_error(config_copy, mutate, r"缺少 depth_classes")


def test_reject_missing_case_field(config_copy) -> None:
    def mutate(configs: Path) -> None:
        patch_text(configs / JIULONG, "name: 久隆\n", "")

    _expect_config_error(config_copy, mutate, r"缺少必填字段 'name'")


def test_reject_default_run_not_in_runs(config_copy) -> None:
    def mutate(configs: Path) -> None:
        target_line = next(
            line
            for line in (configs / JIULONG).read_text(encoding="utf-8").splitlines(keepends=True)
            if line.startswith("default_run_id:")
        )
        patch_text(configs / JIULONG, target_line, "default_run_id: nope\n")

    _expect_config_error(config_copy, mutate, r"default_run_id='nope' 不在 runs 中")


def test_reject_default_case_not_in_cases(config_copy) -> None:
    def mutate(configs: Path) -> None:
        patch_text(configs / DEFAULT, "default_case: jiulong", "default_case: nowhere")

    _expect_config_error(config_copy, mutate, r"default_case='nowhere' 不在案例清单中")


def test_reject_illegal_origin_status(config_copy) -> None:
    def mutate(configs: Path) -> None:
        patch_text(configs / JIULONG, "origin_status: relative_only_candidate", "origin_status: confirmed")

    _expect_config_error(config_copy, mutate, r"origin_status='confirmed' 非法")


def test_reject_naive_time_origin(config_copy) -> None:
    def mutate(configs: Path) -> None:
        patch_text(
            configs / JIULONG,
            'time_origin: "2024-09-05T00:00:00+08:00"',
            'time_origin: "2024-09-05T00:00:00"',
        )

    _expect_config_error(config_copy, mutate, r"必须带时区偏移")


def test_reject_malformed_time_origin(config_copy) -> None:
    def mutate(configs: Path) -> None:
        patch_text(
            configs / JIULONG,
            'time_origin: "2024-09-05T00:00:00+08:00"',
            'time_origin: "2024-13-45T99:00:00+08:00"',
        )

    _expect_config_error(config_copy, mutate, r"不是合法 ISO8601")


def test_reject_run_missing_save_interval(config_copy) -> None:
    def mutate(configs: Path) -> None:
        patch_text(
            configs / JIULONG,
            "    save_interval_s: 21600\n",
            "",
        )

    _expect_config_error(config_copy, mutate, r"缺少必填字段 'save_interval_s'")


def test_reject_basemap_missing_attribution(config_copy) -> None:
    def mutate(configs: Path) -> None:
        patch_text(
            configs / "basemaps.yaml",
            '    attribution: "Source: Esri, Vantor, Earthstar Geographics, and the GIS User Community"\n',
            "",
        )

    _expect_config_error(config_copy, mutate, r"缺少 'attribution'")


def test_project_root_resolution(config, project_root: Path) -> None:
    """config.root 必须指向子项目根（configs/ 的上一级），保证相对路径不与 cwd 耦合。"""
    assert config.path == (project_root / "configs" / "default.yaml").resolve()
    assert project_root == PROJECT_ROOT
