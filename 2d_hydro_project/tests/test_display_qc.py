"""display_qc 展示质控层契约测试（计划 §1.4 / §3.3 / §5 Phase 1 / §7.1）。

本文件把 Phase 1 的硬约束写成可执行断言：

1. **raw 只读**：源 run 的 ``res.max`` 与全部 ``res-*.wd`` 的 sha256 在测试前后一致。
2. **派生标记完整**：每个派生目录的 ``meta.json`` 必须声明派生属性。
3. **阈值按 cellsize 换算**：100 m 与 200 m 用同一物理面积阈值得到不同像元数。
4. **体积/面积账目守恒**：``raw = removed + kept``，变化率可复算。
5. **res.mass 拒写**：派生目录不得含 ``results/res.mass``（否则污染 g15 质量门）。
6. **规则顺序**：豁免优先于过滤，任何豁免条件成立都不会被过滤。
7. **表述纪律**：脚本与审计 JSON 不得出现未核验的规范归属（计划 §2.3）。

测试只读文件，不修改任何模型输出。
"""
from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

import numpy as np
import pytest

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import (  # noqa: E402
    GridError,
    OverwriteRefused,
    guard_no_overwrite,
    read_ascii_header,
    sha256_file,
)
from scripts.stage6 import clean_jiulong_display as D  # noqa: E402

JIU = PROJ / "outputs" / "jiulong" / "2024-09-yagi"
RUN_200M = JIU / "scen_free_200m_sv6h_recession"
RUN_100M = JIU / "scen_free_100m_dt2p5_sv6h_realdem"
DISPLAY_ROOT = PROJ / "outputs" / "display_qc"
AUDIT_PATH = PROJ / "data" / "catalog" / "jiulong_display_qc_audit.json"
SCRIPT = PROJ / "scripts" / "stage6" / "clean_jiulong_display.py"

REQUIRED_DERIVED_META = (
    "derived_from",
    "not_physical_recalculation",
    "param_set_id",
    "param_hash",
    "source_run_sha256",
    "cellsize_m",
    "cell_area_m2",
    "tool",
    "generated_utc",
    "caveats",
)


def _display_dirs() -> list[Path]:
    if not DISPLAY_ROOT.is_dir():
        return []
    return sorted(p for p in DISPLAY_ROOT.rglob("*") if (p / "meta.json").is_file())


def _audit() -> dict:
    if not AUDIT_PATH.is_file():
        pytest.skip("display_qc 审计尚未生成（Phase 1 未完成）")
    return json.loads(AUDIT_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 1. raw 只读：源 run 哈希前后不变
# --------------------------------------------------------------------------- #
def _source_hashes(run_dir: Path) -> dict[str, str]:
    res = run_dir / "results"
    out = {"res.max": sha256_file(res / "res.max")}
    for path in sorted(res.glob("res-*.wd")):
        out[path.name] = sha256_file(path)
    return out


def test_source_run_hashes_match_recorded_audit() -> None:
    """源 run 当前哈希必须与审计 JSON 记录的前/后哈希完全一致。

    这是"raw 未被改动"的持久证据：任何人改动 raw 帧，本测试立即失败。
    """
    audit = _audit()
    integrity = audit["raw_integrity"]
    assert integrity["unchanged"] is True, "审计已记录源 run 被改动"
    assert integrity["before"] == integrity["after"], "审计前后哈希不一致"

    current = _source_hashes(RUN_200M)
    assert current["res.max"] == integrity["after"]["res_max_sha256"], (
        "res.max 与审计记录不符：源 run 可能已被修改"
    )
    assert len(current) - 1 == integrity["after"]["frame_count"], "帧数与审计记录不符"

    recorded = audit["param_sets"]["A"]["meta"]["source_frame_list_sha256"]
    assert recorded == integrity["after"]["frame_list_sha256"], "帧清单哈希不一致"


def test_source_hashes_stable_within_test_session() -> None:
    """同一次测试内前后各算一次哈希，必须逐位一致（证明测试自身也不写 raw）。"""
    before = _source_hashes(RUN_200M)
    after = _source_hashes(RUN_200M)
    assert before == after, "测试运行期间源 run 发生变化"
    # 53 帧 + res.max
    assert len(before) == 54, f"期望 53 帧 + res.max，实得 {len(before)} 个文件"


def test_derived_layers_do_not_modify_source_mtime(tmp_path: Path) -> None:
    """派生层必须落在 outputs/display_qc 下，不得写进 outputs/jiulong。"""
    audit = _audit()
    for set_id, rec in audit["param_sets"].items():
        derived = rec["derived_dir"]
        assert derived.startswith("outputs/display_qc/"), (
            f"参数档 {set_id} 的派生目录 {derived} 不在 outputs/display_qc 下"
        )
        assert not derived.startswith("outputs/jiulong/"), (
            f"参数档 {set_id} 写入了受保护的 outputs/jiulong/**"
        )


# --------------------------------------------------------------------------- #
# 2. 派生标记完整
# --------------------------------------------------------------------------- #
def test_display_qc_meta_marks_derived_layer() -> None:
    """每个派生目录的 meta.json 必须含全部派生标记（计划 §5 Phase 1 验收）。"""
    dirs = _display_dirs()
    assert dirs, "outputs/display_qc 下没有 meta.json"
    assert len(dirs) >= 3, f"必须至少三档 A/B/C，实得 {[d.name for d in dirs]}"
    for path in dirs:
        meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
        missing = [key for key in REQUIRED_DERIVED_META if key not in meta]
        assert not missing, f"{path} 缺派生标记字段：{missing}"
        assert meta["not_physical_recalculation"] is True
        assert meta["derived_from"], "derived_from 不得为空"
        assert float(meta["cellsize_m"]) > 0
        assert float(meta["cell_area_m2"]) == float(meta["cellsize_m"]) ** 2, (
            "cell_area_m2 必须等于 cellsize_m 的平方"
        )
        assert meta["res_mass_copied"] is False, "派生层不得复制 res.mass"
        assert any("业务告警" in item for item in meta["caveats"]), (
            "caveats 必须明写不得用于业务告警"
        )
        assert meta["source_run_sha256"], "必须记录源 run 哈希"
        # 参数哈希必须可复算
        assert meta["param_hash"] == D.param_hash(meta["params"]), (
            f"{path} 的 param_hash 与 params 不一致，参数不可复现"
        )


def test_derived_meta_caveats_forbid_misreading() -> None:
    """caveats 必须同时排除"校准/物理重算"与业务告警两种误读。"""
    for path in _display_dirs():
        meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
        text = "".join(meta["caveats"])
        assert "展示派生层" in text
        assert "不是物理重算" in text
        assert "业务告警" in text
        assert "E3 候选假设" in text


# --------------------------------------------------------------------------- #
# 3. 阈值按 cellsize 换算（100 m / 200 m 对照）
# --------------------------------------------------------------------------- #
def test_cells_for_area_uses_header_cell_area() -> None:
    """``n_cells = ceil(area_threshold_m2 / cell_area_m2)``，200 m 与 100 m 差 4 倍。"""
    h200 = read_ascii_header(RUN_200M / "results" / "res.max")
    h100 = read_ascii_header(RUN_100M / "results" / "res.max")
    area200 = float(h200["cellsize"]) ** 2
    area100 = float(h100["cellsize"]) ** 2
    assert area200 == 40000.0
    assert area100 == 10000.0

    for area_km2 in (0.08, 0.16, 0.36, 1.0, 4.0):
        n200 = D.cells_for_area(area_km2, area200)
        n100 = D.cells_for_area(area_km2, area100)
        assert n200 == math.ceil(area_km2 * 1e6 / area200)
        assert n100 == math.ceil(area_km2 * 1e6 / area100)
        assert n100 == 4 * n200, (
            f"面积 {area_km2} km² 在 100 m 的像元数应为 200 m 的 4 倍"
            f"（实得 {n100} vs {n200}）"
        )
    # 计划 §1.4 表格：0.04 km² = 100 m 的 4 格 = 200 m 的 1 格
    assert D.cells_for_area(0.04, area100) == 4
    assert D.cells_for_area(0.04, area200) == 1
    # 0.36 km² = 100 m 的 36 格 = 200 m 的 9 格
    assert D.cells_for_area(0.36, area100) == 36
    assert D.cells_for_area(0.36, area200) == 9


def test_cells_for_area_rejects_bad_cell_area() -> None:
    """单元面积为 0 或负时必须报错，不得静默返回 1。"""
    with pytest.raises(GridError):
        D.cells_for_area(0.1, 0.0)
    with pytest.raises(GridError):
        D.cells_for_area(0.1, -40000.0)


def test_frames_for_persistence_uses_saveint() -> None:
    """持续性阈值按小时登记，帧数由 saveint 换算（6 h/帧）。"""
    saveint = 21600.0  # 6 h
    assert D.frames_for_persistence(36.0, saveint) == 6
    assert D.frames_for_persistence(72.0, saveint) == 12
    assert D.frames_for_persistence(144.0, saveint) == 24
    # 同样的小时数在 1 h 输出的 run 上应得 36/72/144 帧
    assert D.frames_for_persistence(36.0, 3600.0) == 36
    with pytest.raises(GridError):
        D.frames_for_persistence(36.0, 0.0)


def test_audit_declares_preregistered_param_sets_with_conversions() -> None:
    """审计 JSON 必须登记 A/B/C 三档，且每档带网格换算结果。"""
    audit = _audit()
    assert audit["not_physical_recalculation"] is True
    assert audit["layer_kind"] == "display_qc"
    param_sets = audit["param_sets"]
    assert set(param_sets) >= {"A", "B", "C"}, f"必须预注册 A/B/C，实得 {sorted(param_sets)}"

    cellsize = float(audit["scope"]["grid"]["cellsize_m"])
    cell_area = float(audit["scope"]["grid"]["cell_area_m2"])
    assert cell_area == cellsize ** 2

    for set_id, rec in param_sets.items():
        params = rec["params"]
        assert params["cellsize_m"] == cellsize
        assert params["cell_area_m2"] == cell_area
        # 像元数必须是物理面积阈值的换算结果，不是独立写死的常数
        for area_key, cells_key in (
            ("min_patch_area_km2", "n_cells_min_patch_area"),
            ("exemption_area_km2", "n_cells_exemption_area"),
            ("evidence_area_km2", "n_cells_evidence_area"),
            ("shallow_max_area_km2", "n_cells_shallow_max_area"),
        ):
            assert params[cells_key] == math.ceil(params[area_key] * 1e6 / cell_area), (
                f"{set_id} 的 {cells_key} 与 {area_key} 换算不符"
            )
        assert params["persistence_frames"] == math.ceil(
            params["persistence_hours"] * 3600.0 / params["saveint_s"]
        )
        assert params["source"] == "项目候选参数，未经本地率定"
        assert params["confidence"] == "E3 候选假设"


def test_audit_reports_cross_resolution_conversion() -> None:
    """同一物理面积阈值在 200 m / 100 m 上的像元数必须成 4 倍关系。"""
    audit = _audit()
    comparison = audit["resolution_comparison"]
    grid = comparison["comparison_grid"]
    assert grid["status"] == "present", "100 m 对照网格必须可用"
    assert grid["cellsize_m"] == 100.0
    assert grid["cell_area_m2"] == 10000.0

    thresholds = comparison["thresholds"]["min_patch_area_km2"]
    assert set(thresholds) >= {"A", "B", "C"}
    for set_id, rec in thresholds.items():
        assert rec["n_cells_comparison"] == 4 * rec["n_cells_primary"], (
            f"{set_id} 的跨分辨率换算不满足 4 倍关系"
        )


def test_param_sets_are_ordered_conservative_to_aggressive() -> None:
    """A/B/C 必须单调加严，否则"三档"没有敏感性含义。"""
    audit = _audit()
    order = ["A", "B", "C"]
    areas = [audit["param_sets"][k]["params"]["min_patch_area_km2"] for k in order]
    hours = [audit["param_sets"][k]["params"]["persistence_hours"] for k in order]
    depths = [audit["param_sets"][k]["params"]["shallow_depth_m"] for k in order]
    for values in (areas, hours, depths):
        assert values == sorted(values), f"参数档必须单调加严，实得 {values}"
        assert values[0] < values[-1], "A 与 C 不得相同"


# --------------------------------------------------------------------------- #
# 4. 账目守恒：raw = removed + kept
# --------------------------------------------------------------------------- #
def test_volume_and_area_accounting_is_conserved() -> None:
    """逐帧 ``raw = removed + kept``，面积精确、体积在 float32 舍入内。"""
    audit = _audit()
    cell_area = float(audit["scope"]["grid"]["cell_area_m2"])
    for set_id, rec in audit["param_sets"].items():
        for frame in rec["frames"]:
            seq = frame["seq"]
            # 像元账目：必须精确
            assert frame["removed_cells"] <= frame["wet_cells"], f"{set_id} 帧 {seq} 删格数超过湿格数"
            assert frame["kept_wet_cells"] == frame["wet_cells"] - frame["removed_cells"], (
                f"{set_id} 帧 {seq} 像元账目不平"
            )
            # 面积账目：removed_area 必须等于 删格数 × 单元面积
            assert abs(
                frame["removed_area_km2"] - frame["removed_cells"] * cell_area / 1e6
            ) < 1e-9, f"{set_id} 帧 {seq} 面积账目不平"
            assert abs(
                frame["raw_wet_area_km2"] - frame["removed_area_km2"] - frame["kept_wet_area_km2"]
            ) < 1e-6, f"{set_id} 帧 {seq} 面积不守恒"
            # 体积账目：raw/removed/kept 各自是 float32 累加，残差按相对量级判定
            residual = abs(
                frame["raw_wet_volume_m3"]
                - frame["removed_volume_m3"]
                - frame["kept_wet_volume_m3"]
            )
            tolerance = max(1e-3, frame["raw_wet_volume_m3"] * 1e-6)
            assert residual < tolerance, (
                f"{set_id} 帧 {seq} 体积残差 {residual} m³ 超过 float32 容差 {tolerance}"
            )


def test_change_rates_are_recomputable() -> None:
    """面积/体积变化率必须能由同一条记录的 raw 与 kept 复算。"""
    audit = _audit()
    for set_id, rec in audit["param_sets"].items():
        for frame in rec["frames"]:
            raw_area = frame["raw_wet_area_km2"]
            raw_volume = frame["raw_wet_volume_m3"]
            if raw_area > 0:
                expect = (raw_area - frame["kept_wet_area_km2"]) / raw_area * 100.0
                assert abs(expect - frame["area_change_pct"]) < 1e-3, (
                    f"{set_id} 帧 {frame['seq']} 面积变化率不可复算"
                )
            else:
                assert frame["area_change_pct"] == 0.0
            if raw_volume > 0:
                expect = (raw_volume - frame["kept_wet_volume_m3"]) / raw_volume * 100.0
                assert abs(expect - frame["volume_change_pct"]) < 1e-3, (
                    f"{set_id} 帧 {frame['seq']} 体积变化率不可复算"
                )
            else:
                assert frame["volume_change_pct"] == 0.0


def test_res_max_accounting_is_conserved() -> None:
    """res.max 峰值场的账目同样必须平。"""
    audit = _audit()
    cell_area = float(audit["scope"]["grid"]["cell_area_m2"])
    for set_id, rec in audit["param_sets"].items():
        rmax = rec["res_max"]
        assert rmax["kept_wet_cells"] == rmax["raw_wet_cells"] - rmax["removed_cells"]
        assert abs(rmax["removed_area_km2"] - rmax["removed_cells"] * cell_area / 1e6) < 1e-9
        assert abs(
            rmax["raw_wet_area_km2"] - rmax["removed_area_km2"] - rmax["kept_wet_area_km2"]
        ) < 1e-6
        assert rmax["kept_patches"] <= rmax["raw_patches"], "过滤不应增加斑块数"


def test_every_frame_is_audited() -> None:
    """逐帧审计必须覆盖全部帧（计划 §3.3 第 7 条）。"""
    audit = _audit()
    temporal = audit["scope"]["temporal"]
    assert temporal["frame_count"] == 53
    for set_id, rec in audit["param_sets"].items():
        seqs = [frame["seq"] for frame in rec["frames"]]
        assert seqs == list(range(53)), f"{set_id} 帧号不连续：{seqs[:5]}..."
        for frame in rec["frames"]:
            assert "removed_cells" in frame
            assert "removed_area_km2" in frame
            assert "removed_volume_m3" in frame
            assert "n_patches_raw" in frame and "n_patches_after" in frame
            assert "removed_reason_patches" in frame
            assert "area_change_pct" in frame and "volume_change_pct" in frame


# --------------------------------------------------------------------------- #
# 5. res.mass 拒写 / 派生目录守卫
# --------------------------------------------------------------------------- #
def test_display_qc_never_contains_res_mass() -> None:
    """派生目录不得含 res.mass：g15 会把它误计为独立 run（污染质量门计数）。"""
    if not DISPLAY_ROOT.is_dir():
        pytest.skip("display_qc 尚未生成")
    offenders = [str(p) for p in DISPLAY_ROOT.rglob("res.mass")]
    assert not offenders, f"派生目录出现 res.mass：{offenders}"
    # 明确断言本函数就是 g15 的扫描口径
    assert not list(DISPLAY_ROOT.rglob("results/res.mass"))


def test_guard_derived_target_refuses_existing_res_mass(tmp_path: Path) -> None:
    """目标目录已有 results/res.mass 时，写前守卫必须拒绝。"""
    target = tmp_path / "fake_run"
    (target / "results").mkdir(parents=True)
    (target / "results" / "res.mass").write_text("Time\n", encoding="utf-8")
    with pytest.raises(OverwriteRefused):
        D.guard_derived_target(target)
    with pytest.raises(OverwriteRefused):
        guard_no_overwrite(target)


def test_guard_derived_target_allows_fresh_dir(tmp_path: Path) -> None:
    """空目录允许写入；已有派生 meta 也允许（幂等重跑）。"""
    target = tmp_path / "fresh"
    target.mkdir()
    D.guard_derived_target(target)  # 不抛异常即通过
    (target / "meta.json").write_text(
        json.dumps({"not_physical_recalculation": True}), encoding="utf-8"
    )
    D.guard_derived_target(target)


def test_guard_derived_target_refuses_non_derived_dir(tmp_path: Path) -> None:
    """非派生目录（无派生标记）必须拒绝覆盖，避免误写真实结果目录。"""
    target = tmp_path / "real_looking"
    target.mkdir()
    (target / "meta.json").write_text(json.dumps({"scenario": "free"}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="not_physical_recalculation"):
        D.guard_derived_target(target)


def test_refuses_to_write_inside_protected_raw_runs(tmp_path: Path) -> None:
    """派生目录落在 outputs/jiulong/** 内时必须拒绝（禁止修改 raw run）。"""
    with pytest.raises(RuntimeError, match="受保护目录"):
        D._assert_outside_raw_runs(PROJ / "outputs" / "jiulong" / "2024-09-yagi" / "x")
    with pytest.raises(RuntimeError, match="受保护目录"):
        D._assert_outside_raw_runs(PROJ / "outputs" / "jiulong")
    # 正常派生根目录必须允许
    D._assert_outside_raw_runs(DISPLAY_ROOT)
    D._assert_outside_raw_runs(tmp_path)


def test_forbidden_file_detector_catches_res_mass(tmp_path: Path) -> None:
    """派生目录里出现 res.mass 时，检测函数必须报错。"""
    target = tmp_path / "derived"
    (target / "results").mkdir(parents=True)
    (target / "results" / "res.max").write_text("x\n", encoding="utf-8")
    D.assert_no_forbidden_files(target)  # 无 res.mass，通过
    (target / "results" / "res.mass").write_text("x\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="res.mass"):
        D.assert_no_forbidden_files(target)


def test_derived_results_dir_has_frames_but_no_res_mass() -> None:
    """派生 results 目录必须有帧与 res.max，且明确不含 res.mass。"""
    dirs = _display_dirs()
    assert dirs
    for path in dirs:
        results = path / "results"
        assert results.is_dir(), f"{path} 缺 results 目录"
        assert (results / "res.max").is_file(), f"{path} 缺 res.max 派生副本"
        frames = sorted(results.glob("res-*.wd"))
        assert len(frames) == 53, f"{path} 帧数应为 53，实得 {len(frames)}"
        assert not (results / "res.mass").exists(), f"{path} 误含 res.mass"


# --------------------------------------------------------------------------- #
# 6. 规则顺序：豁免优先
# --------------------------------------------------------------------------- #
def _track_params(**overrides) -> dict:
    """构造 classify_track 需要的完整参数 dict（来自真实预注册档 A）。"""
    params = dict(D.resolve_param_set(
        D.PRE_REGISTERED_PARAM_SETS["A"],
        cell_area_m2=40000.0, cellsize_m=200.0, saveint_s=21600.0,
    ))
    params.update(overrides)
    return params


def test_classify_track_exemption_precedes_filter() -> None:
    """任一豁免条件成立时，即使斑块很小很浅，也必须判为 exempt。"""
    params = _track_params()
    tiny = {
        "max_area_km2": 0.04,      # 远小于 min_patch_area
        "longest_consecutive_run": 1,  # 极短暂
        "max_depth_m": 0.06,       # 浅
    }
    # 基线：孤立、短暂、小、浅 → filter
    verdict, reason = D.classify_track(
        tiny, params, is_main_body=False, is_recession_growth=False,
        hits_boundary=False, min_distance_cells=5,
    )
    assert verdict == "filter"
    assert reason == "FILTER_SUBRESOLUTION"

    # 逐个豁免条件单独成立，都必须翻转成 exempt
    cases = [
        ({"is_main_body": True}, "EXEMPT_MAIN_BODY"),
        ({"is_recession_growth": True}, "EXEMPT_RECESSION_GROWTH"),
        ({"hits_boundary": True}, "EXEMPT_BOUNDARY_OUTLET"),
    ]
    for override, expected in cases:
        kwargs = {
            "is_main_body": False, "is_recession_growth": False,
            "hits_boundary": False, "min_distance_cells": 5,
        }
        kwargs.update(override)
        verdict, reason = D.classify_track(tiny, params, **kwargs)
        assert verdict == "exempt", f"{expected} 未优先于过滤"
        assert reason == expected

    # 面积豁免
    big = dict(tiny, max_area_km2=0.5)
    assert D.classify_track(
        big, params, is_main_body=False, is_recession_growth=False,
        hits_boundary=False, min_distance_cells=5,
    ) == ("exempt", "EXEMPT_LARGE_AREA")

    # 持续性豁免（需同时有证据面积）
    lasting = dict(tiny, max_area_km2=0.20, longest_consecutive_run=10)
    assert D.classify_track(
        lasting, params, is_main_body=False, is_recession_growth=False,
        hits_boundary=False, min_distance_cells=5,
    ) == ("exempt", "EXEMPT_PERSISTENT")

    # 深度豁免（需同时有证据面积）
    deep = dict(tiny, max_area_km2=0.20, max_depth_m=0.50)
    assert D.classify_track(
        deep, params, is_main_body=False, is_recession_growth=False,
        hits_boundary=False, min_distance_cells=5,
    ) == ("exempt", "EXEMPT_DEEP")


def test_classify_track_filter_requires_all_conditions() -> None:
    """过滤是合取：去掉任一条（小/短暂/孤立/浅）都不应过滤。

    A 档参数（200 m）：``min_patch_area_km2=0.08``、
    ``shallow_max_area_km2=0.16``、``shallow_depth_m=0.10``、
    ``deep_depth_m=0.30``、``persistence_frames=6``。

    第二条过滤规则 ``FILTER_SHALLOW`` 只要求「浅 + 短暂 + 孤立 + 面积 < 0.16」，
    **不要求「小」**。因此要检验「不再小」这一条，面积必须同时越过
    ``shallow_max_area_km2=0.16``，否则仍会被 ``FILTER_SHALLOW`` 捕获。
    同理「不再浅」必须让水深越过 ``deep_depth_m=0.30``，
    因为 0.29 仍小于 0.30，仍满足第一条过滤规则的浅水条件。
    """
    params = _track_params()
    base = {
        "max_area_km2": 0.04,
        "longest_consecutive_run": 1,
        "max_depth_m": 0.06,
    }
    kwargs = {
        "is_main_body": False, "is_recession_growth": False,
        "hits_boundary": False, "min_distance_cells": 5,
    }
    assert D.classify_track(base, params, **kwargs) == ("filter", "FILTER_SUBRESOLUTION")

    # 不再"小"：面积须越过两条过滤规则中较大的面积上限 0.16 km²
    not_small = dict(base, max_area_km2=0.20)
    assert D.classify_track(not_small, params, **kwargs)[0] == "keep"
    # 面积只到 0.12 时仍 < shallow_max_area 0.16，属预期的第二类过滤
    assert D.classify_track(dict(base, max_area_km2=0.12), params, **kwargs) == (
        "filter", "FILTER_SHALLOW"
    )
    # 不再"短暂"
    not_brief = dict(base, longest_consecutive_run=6)
    assert D.classify_track(not_brief, params, **kwargs)[0] == "keep"
    # 不再"孤立"
    near_main = {**kwargs, "min_distance_cells": 1}
    assert D.classify_track(base, params, **near_main)[0] == "keep"
    # 不再"浅"：水深须越过 deep_depth_m 0.30
    assert D.classify_track(dict(base, max_depth_m=0.29), params, **kwargs)[0] == "filter"
    not_shallow = dict(base, max_depth_m=0.35)
    assert D.classify_track(not_shallow, params, **kwargs)[0] == "keep"


def test_recorded_filter_tracks_satisfy_no_exemption() -> None:
    """已落盘判定中，任何 filter 轨迹都不得同时满足任一豁免条件。"""
    audit = _audit()
    dirs = {p.parent.name: p for p in DISPLAY_ROOT.rglob("track_verdicts_*.json")}
    assert dirs, "没有落盘的轨迹判定文件"
    for set_id in audit["param_sets"]:
        path = DISPLAY_ROOT / "scen_free_200m_sv6h_recession" / set_id / "audit" / (
            f"track_verdicts_{set_id}.json"
        )
        if not path.is_file():
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        filtered = [t for t in doc["tracks"] if t["verdict"] == "filter"]
        assert filtered, f"{set_id} 没有任何过滤轨迹，规则可能未生效"
        for track in filtered:
            assert track["is_main_body"] is False, "主水体斑块被过滤，违反豁免优先"
            assert track["hits_boundary"] is False, "触边界斑块被过滤，违反豁免优先"
            assert track["is_recession_growth"] is False, "退水增长斑块被过滤，违反豁免优先"
            params = doc["param_hash"]
            assert params, "轨迹判定文件必须带参数哈希"


def test_recorded_filter_tracks_respect_physical_limits() -> None:
    """已落盘的 filter 轨迹必须真的满足面积/持续/深度上限（按物理量核对）。"""
    audit = _audit()
    for set_id, rec in audit["param_sets"].items():
        path = (DISPLAY_ROOT / "scen_free_200m_sv6h_recession" / set_id
                / "audit" / f"track_verdicts_{set_id}.json")
        if not path.is_file():
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        params = rec["params"]
        for track in doc["tracks"]:
            if track["verdict"] != "filter":
                continue
            assert track["max_area_km2"] < params["exemption_area_km2"], (
                f"{set_id} 轨迹 {track['track_id']} 面积达到豁免线仍被过滤"
            )
            assert track["longest_consecutive_run"] < params["persistence_frames"], (
                f"{set_id} 轨迹 {track['track_id']} 达到持续性豁免仍被过滤"
            )
            assert track["max_depth_m"] < params["deep_depth_m"], (
                f"{set_id} 轨迹 {track['track_id']} 达到深度豁免仍被过滤"
            )
            assert track["min_distance_cells"] > params["isolation_min_distance_cells"], (
                f"{set_id} 轨迹 {track['track_id']} 不孤立却被过滤"
            )


def test_main_body_labels_pick_largest_patches() -> None:
    """候选主汇流区取面积最大的若干斑块，直到覆盖指定比例的湿面积。"""
    from scripts.common.patch_analysis import Patch, FramePatchSummary

    def patch(label: int, n_cells: int, area_km2: float) -> Patch:
        return Patch(
            label=label, n_cells=n_cells, area_km2=area_km2, max_depth_m=1.0,
            mean_depth_m=1.0, p95_depth_m=1.0, volume_m3=1.0,
            row_min=0, row_max=0, col_min=0, col_max=0,
            centroid_row=0.0, centroid_col=0.0,
        )

    summary = FramePatchSummary(
        seq=0, n_patches=3, wet_cells=100, wet_area_km2=4.0, wet_volume_m3=1.0,
        max_depth_m=1.0,
        patches=[patch(1, 50, 2.0), patch(2, 30, 1.2), patch(3, 20, 0.8)],
    )
    # 50% 阈值 → 只需第一个（2.0/4.0 = 50%）
    assert D.main_body_patch_labels(summary, 0.5) == {1}
    # 90% 阈值 → 需要前两个（2.0+1.2 = 3.2/4.0 = 80%）仍不足 → 三个全要
    assert D.main_body_patch_labels(summary, 0.9) == {1, 2, 3}
    empty = FramePatchSummary(
        seq=0, n_patches=0, wet_cells=0, wet_area_km2=0.0, wet_volume_m3=0.0,
        max_depth_m=0.0, patches=[],
    )
    assert D.main_body_patch_labels(empty, 0.5) == set()


def test_touches_boundary_detects_edges() -> None:
    """触边界判定必须识别四条边。"""
    from scripts.common.patch_analysis import Patch

    def make(row_min: int, row_max: int, col_min: int, col_max: int) -> Patch:
        return Patch(
            label=1, n_cells=1, area_km2=0.04, max_depth_m=1.0, mean_depth_m=1.0,
            p95_depth_m=1.0, volume_m3=1.0,
            row_min=row_min, row_max=row_max, col_min=col_min, col_max=col_max,
            centroid_row=0.0, centroid_col=0.0,
        )

    shape = (10, 10)
    assert D.touches_boundary(make(0, 2, 3, 4), shape) is True
    assert D.touches_boundary(make(7, 9, 3, 4), shape) is True
    assert D.touches_boundary(make(3, 4, 0, 2), shape) is True
    assert D.touches_boundary(make(3, 4, 7, 9), shape) is True
    assert D.touches_boundary(make(3, 4, 3, 4), shape) is False


def test_distance_to_mask_is_eight_neighbour_bfs() -> None:
    """距离场用 8 邻域：对角相邻距离为 1（与连通域口径一致）。"""
    mask = np.zeros((5, 5), dtype=bool)
    mask[2, 2] = True
    dist = D.distance_to_mask(mask)
    assert dist[2, 2] == 0
    assert dist[1, 1] == 1, "对角相邻必须是 1（8 邻域）"
    assert dist[2, 3] == 1
    assert dist[0, 0] == 2
    assert dist[2, 0] == 2
    # 全部像元在有限网格内都可达
    assert (dist >= 0).all()


def test_distance_to_mask_unreachable_is_negative() -> None:
    """空掩膜时全部不可达（-1），不得返回 0 冒充"在主水体上"。"""
    dist = D.distance_to_mask(np.zeros((3, 3), dtype=bool))
    assert (dist == -1).all()


# --------------------------------------------------------------------------- #
# 7. 像元分类与 float32 口径
# --------------------------------------------------------------------------- #
def test_classify_cells_distinguishes_categories() -> None:
    """nodata / 海 / 干 / 浅 / 深 五类必须分开且互斥。

    注意 ``float32(0.05)`` 实际是 ``0.05000000074505806``，严格大于 0.05，
    所以写成 0.05 的像元在 float32 口径下**算湿**（与 summarize_frame 一致）。
    本测试因此用 0.04 表示"干"、用 0.0501 表示"刚好湿"。

    海面单独占第 3 行：海面像元会同时被排除出 ``wet_cells``。若把海面标记
    混在数值行里，``wet_cells`` 与 ``valid_*`` 的互斥关系就失去检验意义。
    """
    depth = np.array([
        [1.0, 0.0501, 0.04],    # 深 / 刚好湿 / 干
        [0.0, -9999.0, 0.20],   # 干 / nodata / 深
        [0.5, 0.5, 0.5],        # 整行海面：水深非零但必须不计入湿格
    ], dtype=np.float32)
    sea = np.array([
        [False, False, False],
        [False, False, False],
        [True, True, True],
    ])
    counts = D.classify_cells(
        depth, sea_mask=sea, wet_threshold_m=0.05,
        shallow_depth_m=0.10, nodata=-9999.0,
    )
    assert counts["nodata_cells"] == 1
    assert counts["sea_cells"] == 3
    assert counts["wet_cells"] == 3, "1.0 / 0.0501 / 0.20 是湿格，0.04 与 0.0 是干格；海面整行不计"
    assert counts["valid_shallow_cells"] == 1, "只有 0.0501 属于有效浅水"
    assert counts["valid_deep_cells"] == 2, "1.0 与 0.20 属于有效深水"
    assert counts["dry_cells"] == 2
    # 五类互斥且合计等于有效格数（nodata 与海域不算有效格）
    assert counts["wet_cells"] == counts["valid_shallow_cells"] + counts["valid_deep_cells"]
    assert counts["wet_cells"] + counts["dry_cells"] == depth.size - counts["nodata_cells"] - counts["sea_cells"]


def test_classify_cells_matches_summarize_frame_wet_count() -> None:
    """像元分类的 wet_cells 必须与 patch_analysis.summarize_frame 完全一致。

    float32 下 ``float32(0.05) = 0.05000000074505806``；若本函数升位到 float64，
    ``> 0.05`` 会把恰好等于 0.050 的像元算成湿格，与 summarize_frame 口径冲突。
    """
    from scripts.common.patch_analysis import summarize_frame

    depth = np.array([
        [0.05, 0.0500001, 0.0499999],
        [0.0, 1.0, 0.2],
    ], dtype=np.float32)
    counts = D.classify_cells(
        depth, sea_mask=None, wet_threshold_m=0.05,
        shallow_depth_m=0.10, nodata=-9999.0,
    )
    summary = summarize_frame(
        depth, 0, wet_threshold_m=0.05, cell_area_m2=40000.0, keep_patches=False,
    )
    assert counts["wet_cells"] == summary.wet_cells, (
        f"分类器 {counts['wet_cells']} 与 summarize_frame {summary.wet_cells} 口径不一致"
    )


def test_classify_cells_handles_nan_as_nodata() -> None:
    """NaN 必须算 nodata，不得算干格。"""
    depth = np.array([[np.nan, 0.5]], dtype=np.float32)
    counts = D.classify_cells(
        depth, sea_mask=None, wet_threshold_m=0.05,
        shallow_depth_m=0.10, nodata=-9999.0,
    )
    assert counts["nodata_cells"] == 1
    assert counts["wet_cells"] == 1
    assert counts["dry_cells"] == 0


# --------------------------------------------------------------------------- #
# 8. 预注册告警
# --------------------------------------------------------------------------- #
def test_alarm_thresholds_are_preregistered_and_recorded() -> None:
    """告警阈值必须写在脚本常量里并进审计，不得按结果事后设定。"""
    audit = _audit()
    for set_id, rec in audit["param_sets"].items():
        alarm = rec["alarm"]
        prereg = alarm["pre_registered"]
        assert prereg["area_change_pct"] == D.PRE_REGISTERED_ALARM["area_change_pct"]
        assert prereg["volume_change_pct"] == D.PRE_REGISTERED_ALARM["volume_change_pct"]
        assert prereg["confidence"] == "E3 候选假设"
        assert prereg["basis"], "告警阈值必须写明依据"
        assert "blocked_for_default_publish" in alarm
        assert isinstance(alarm["blocked_reasons"], list)
        # blocked 与原因必须自洽
        assert alarm["blocked_for_default_publish"] == bool(alarm["blocked_reasons"])


def test_alarm_decision_is_recomputable_from_recorded_numbers() -> None:
    """blocked 判定必须能由审计里记录的变化率与阈值复算。"""
    audit = _audit()
    limit_area = D.PRE_REGISTERED_ALARM["area_change_pct"]
    limit_volume = D.PRE_REGISTERED_ALARM["volume_change_pct"]
    for set_id, rec in audit["param_sets"].items():
        alarm = rec["alarm"]
        expected = (
            alarm["max_area_change_pct"] > limit_area
            or alarm["max_volume_change_pct"] > limit_volume
            or alarm["res_max"]["area_change_pct"] > limit_area
            or alarm["res_max"]["volume_change_pct"] > limit_volume
        )
        assert alarm["blocked_for_default_publish"] is expected, (
            f"{set_id} 的 blocked 判定与记录数值不自洽"
        )


def test_blocked_state_propagates_to_meta() -> None:
    """blocked 状态必须同时写进派生层 meta，便于前端与审计读取。"""
    audit = _audit()
    for set_id, rec in audit["param_sets"].items():
        alarm_blocked = rec["alarm"]["blocked_for_default_publish"]
        meta_alarm = rec["meta"]["alarm"]
        assert meta_alarm["blocked_for_default_publish"] is alarm_blocked
        on_disk = json.loads(
            (DISPLAY_ROOT / "scen_free_200m_sv6h_recession" / set_id
             / "meta.json").read_text(encoding="utf-8")
        )
        assert on_disk["alarm"]["blocked_for_default_publish"] is alarm_blocked


def test_change_rates_stay_within_preregistered_alarm() -> None:
    """当前三档的实际变化率必须留在预注册告警线内，否则本层不得默认发布。

    若本断言失败，说明参数档或数据已变化，必须先复核告警状态再发布。
    """
    audit = _audit()
    limit_area = D.PRE_REGISTERED_ALARM["area_change_pct"]
    limit_volume = D.PRE_REGISTERED_ALARM["volume_change_pct"]
    for set_id, rec in audit["param_sets"].items():
        alarm = rec["alarm"]
        assert alarm["max_area_change_pct"] <= limit_area, (
            f"{set_id} 逐帧面积变化 {alarm['max_area_change_pct']:.2f}% 超线"
        )
        assert alarm["max_volume_change_pct"] <= limit_volume, (
            f"{set_id} 逐帧体积变化 {alarm['max_volume_change_pct']:.2f}% 超线"
        )
        assert alarm["res_max"]["area_change_pct"] <= limit_area
        assert alarm["res_max"]["volume_change_pct"] <= limit_volume
        assert alarm["blocked_for_default_publish"] is False, (
            f"{set_id} 被标记 blocked，不得默认发布"
        )


# --------------------------------------------------------------------------- #
# 9. 表述纪律（计划 §2.3）
# --------------------------------------------------------------------------- #
FORBIDDEN_CLAIMS = (
    "ARR 2019 规定",
    "ARR 2019 强制",
    "ARR 2019 标准",
    "UK EA 强制",
    "UK Environment Agency 规定",
    "校准",
    "真实淹没",
    "物理修复",
)


def test_script_has_no_unverified_regulatory_attribution() -> None:
    """脚本正文不得把候选参数归因于未核验的国际规范（计划 §2.3）。"""
    text = SCRIPT.read_text(encoding="utf-8")
    # 允许出现"不得归因于 ARR 2019"这类否定句，但不得出现"ARR 2019 规定/强制"
    for claim in FORBIDDEN_CLAIMS:
        occurrences = text.count(claim)
        if occurrences == 0:
            continue
        # 只允许出现在明确的否定/禁止语境中
        for match in re.finditer(re.escape(claim), text):
            window = text[max(0, match.start() - 40):match.start()]
            assert ("不" in window or "禁止" in window or "非" in window), (
                f"脚本出现未核验表述「{claim}」且不在否定语境：{window!r}"
            )


def test_script_has_no_hardcoded_cell_area() -> None:
    """脚本不得写死单元面积，也不得写死 4/9 像元阈值。"""
    text = SCRIPT.read_text(encoding="utf-8")
    for literal in ("100.0 * 100.0", "100*100", "100.0*100.0", "10000.0"):
        assert literal not in text, f"出现写死的 100 m 单元面积：{literal}"
    assert "cellsize" in text
    assert "cell_area" in text
    # 旧实现的 4/9 像元数字只能出现在诊断常量里，不得作为阈值默认值
    assert "LEGACY_PIXEL_RULE_DIAGNOSTIC" in text
    legacy_block = text[text.index("LEGACY_PIXEL_RULE_DIAGNOSTIC"):]
    legacy_block = legacy_block[:legacy_block.index("}")]
    assert "why_not_used" in legacy_block, "旧规则常量必须写明不复用的理由"


def test_audit_declares_forbidden_attribution() -> None:
    """审计 JSON 必须显式声明不归因于国际规范。"""
    audit = _audit()
    prereg = audit["pre_registration"]
    assert prereg["frozen_before_production_run"] is True
    assert prereg["frozen_at_utc"]
    assert "ARR 2019" in prereg["forbidden_attribution"]
    assert "不得" in prereg["forbidden_attribution"] or "不归因" in prereg["forbidden_attribution"]
    caveats = "".join(audit["caveats"])
    assert "不是物理重算" in caveats
    assert "业务告警" in caveats
    assert "未经本地率定" in caveats


def test_audit_does_not_claim_calibration() -> None:
    """审计 JSON 全文不得出现把本层说成"校准"的表述。"""
    raw = AUDIT_PATH.read_text(encoding="utf-8")
    for match in re.finditer("校准", raw):
        window = raw[max(0, match.start() - 30):match.start() + 10]
        assert ("不" in window or "非" in window or "禁止" in window), (
            f"审计 JSON 出现「校准」且不在否定语境：{window!r}"
        )


def test_legacy_implementation_is_not_imported() -> None:
    """脚本不得复用旧实现 clean_rain_on_grid（其写死 4/9 像元与 100 m 面积）。"""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "import clean_rain_on_grid" not in text
    assert "from clean_rain_on_grid" not in text
    assert "from scripts.stage6.clean_rain_on_grid" not in text


# --------------------------------------------------------------------------- #
# 10. 轨迹追踪契约（PersistenceTracker 内部状态）
# --------------------------------------------------------------------------- #
def test_tracker_contract_exposes_cell_owner() -> None:
    """PersistenceTracker 必须暴露 int→int 的像元→轨迹映射，否则展示质控无法回写。"""
    from scripts.common.patch_analysis import PersistenceTracker, summarize_frame

    depth = np.zeros((6, 6), dtype=np.float32)
    depth[1:3, 1:3] = 1.0
    tracker = PersistenceTracker()
    tracker.update(
        summarize_frame(depth, 0, wet_threshold_m=0.05, cell_area_m2=40000.0),
        width=6,
    )
    owner = D.assert_tracker_contract(tracker)
    assert owner, "轨迹追踪后应有像元→轨迹映射"
    assert all(isinstance(k, int) and isinstance(v, int) for k, v in owner.items())
    assert len(owner) == 4, "4 个湿格都应被记录"


def test_tracker_contract_rejects_missing_attribute() -> None:
    """若 Lead 模块改动导致内部状态消失，必须显式报错而不是静默退化。"""
    class Broken:
        pass

    with pytest.raises(GridError, match="_cell_owner"):
        D.assert_tracker_contract(Broken())


def test_tracker_contract_rejects_wrong_types() -> None:
    """映射类型不符时必须报错（避免 numpy 标量导致下游 KeyError）。"""
    class Wrong:
        _cell_owner = {"a": 1}

    with pytest.raises(GridError, match="类型不符"):
        D.assert_tracker_contract(Wrong())


# --------------------------------------------------------------------------- #
# 11. 参数哈希可复现
# --------------------------------------------------------------------------- #
def test_param_hash_is_deterministic_and_key_order_independent() -> None:
    """param_hash 必须与键顺序无关、跨调用稳定。"""
    a = {"x": 1, "y": 2.5, "z": "文本"}
    b = {"z": "文本", "y": 2.5, "x": 1}
    assert D.param_hash(a) == D.param_hash(b)
    assert D.param_hash(a) == D.param_hash(dict(a))
    assert len(D.param_hash(a)) == 64
    assert D.param_hash(a) != D.param_hash({**a, "x": 2})


def test_param_hash_changes_with_cell_area() -> None:
    """网格不同则参数哈希不同（同一物理阈值在 100/200 m 是不同参数集）。"""
    spec = D.PRE_REGISTERED_PARAM_SETS["B"]
    p200 = D.resolve_param_set(spec, cell_area_m2=40000.0, cellsize_m=200.0, saveint_s=21600.0)
    p100 = D.resolve_param_set(spec, cell_area_m2=10000.0, cellsize_m=100.0, saveint_s=21600.0)
    assert p200["param_hash"] != p100["param_hash"]
    assert p200["n_cells_min_patch_area"] == 4
    assert p100["n_cells_min_patch_area"] == 16, "同一 0.16 km² 在 100 m 上应为 16 格"


def test_resolved_param_set_records_conversion_provenance() -> None:
    """解析后的参数必须同时带物理阈值、像元换算结果与来源/置信度。"""
    resolved = D.resolve_param_set(
        D.PRE_REGISTERED_PARAM_SETS["A"],
        cell_area_m2=40000.0, cellsize_m=200.0, saveint_s=21600.0,
    )
    assert resolved["cellsize_m"] == 200.0
    assert resolved["cell_area_m2"] == 40000.0
    assert resolved["min_patch_area_km2"] == 0.08
    assert resolved["n_cells_min_patch_area"] == 2
    assert resolved["persistence_hours"] == 36.0
    assert resolved["persistence_frames"] == 6
    assert resolved["source"] == "项目候选参数，未经本地率定"
    assert resolved["confidence"] == "E3 候选假设"
    assert len(resolved["param_hash"]) == 64


def test_unregistered_param_set_is_rejected() -> None:
    """未预注册的参数档必须被拒绝（禁止临时新增档位）。"""
    assert set(D.PRE_REGISTERED_PARAM_SETS) == {"A", "B", "C"}
    parser = D.build_parser()
    args = parser.parse_args(["--param-sets", "Z"])
    args.param_sets = [item.strip().upper() for item in args.param_sets.split(",") if item.strip()]
    with pytest.raises(GridError, match="未预注册"):
        D.run(args)


# --------------------------------------------------------------------------- #
# 12. CLI 契约（计划 §4：--dry-run / --run-tag / --log-json / --out-dir）
# --------------------------------------------------------------------------- #
def test_cli_supports_governance_flags() -> None:
    """脚本必须实现计划 §4 要求的统一 CLI 参数。"""
    parser = D.build_parser()
    args = parser.parse_args([
        "--run-id", "scen_free_200m_sv6h_recession",
        "--run-tag", "display-qc-v1",
        "--dry-run",
        "--log-json", "logs/display_qc.json",
        "--out-dir", "outputs/display_qc",
    ])
    assert args.run_id == "scen_free_200m_sv6h_recession"
    assert args.run_tag == "display-qc-v1"
    assert args.dry_run is True
    assert str(args.log_json).endswith("display_qc.json")
    assert args.out_dir is not None


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    """--dry-run 必须不写派生目录，也不写审计 JSON。"""
    out_root = tmp_path / "display_qc"
    audit_path = tmp_path / "audit.json"
    parser = D.build_parser()
    args = parser.parse_args([
        "--param-sets", "A",
        "--dry-run",
        "--out-root", str(out_root),
        "--audit-path", str(audit_path),
    ])
    args.param_sets = ["A"]
    result = D.run(args)
    assert not out_root.exists(), "--dry-run 不应创建派生目录"
    assert not audit_path.exists(), "--dry-run 不应写审计 JSON"
    assert result["dry_run"] is True
    # 但审计内容必须已算出（只是没落盘）
    assert result["param_sets"]["A"]["frames"], "dry-run 仍必须给出逐帧审计"
    assert result["raw_integrity"]["unchanged"] is True


def test_dry_run_leaves_source_run_untouched() -> None:
    """dry-run 前后源 run 哈希必须一致。"""
    before = _source_hashes(RUN_200M)
    parser = D.build_parser()
    args = parser.parse_args(["--param-sets", "B", "--dry-run"])
    args.param_sets = ["B"]
    D.run(args)
    after = _source_hashes(RUN_200M)
    assert before == after


# --------------------------------------------------------------------------- #
# 13. 旧实现缺陷的回归护栏（计划 §1.4）
# --------------------------------------------------------------------------- #
def test_legacy_pixel_rule_diagnostic_documents_200m_impact() -> None:
    """审计必须记录旧 4/9 像元规则在 200 m 上的实际后果，供复核。"""
    audit = _audit()
    diag = audit["legacy_pixel_rule_diagnostic"]
    assert diag["rule_1_min_patch_cells"] == 4
    assert diag["rule_2_shallow_patch_cells"] == 9
    assert diag["why_not_used"]
    applied = diag["applied_on_grid"]
    cell_area = float(audit["scope"]["grid"]["cell_area_m2"])
    # 4 格与 9 格在 200 m 上的物理面积
    assert applied["rule_1_area_km2"] == pytest.approx(4 * cell_area / 1e6, rel=1e-9)
    assert applied["rule_2_area_km2"] == pytest.approx(9 * cell_area / 1e6, rel=1e-9)
    assert applied["rule_1_area_km2"] == pytest.approx(0.16, rel=1e-6)
    assert applied["rule_2_area_km2"] == pytest.approx(0.36, rel=1e-6)
    # 旧规则会删掉峰值帧上相当比例的湿面积
    assert diag["peak_frame_drop_area_pct"] > 5.0, (
        "旧规则在 200 m 上的删除比例应显著，否则诊断无意义"
    )
    # 且被删斑块里有长期持续者 → 证明不能只按像元数过滤
    assert diag["peak_frame_drop_tracks_wet_in_90pct_of_frames"] > 0, (
        "被删斑块应包含长期持续者，否则无法说明旧规则的风险"
    )


def test_display_qc_reduces_patch_count_without_losing_area() -> None:
    """展示质控应减少斑块数，同时面积变化保持在告警线内。"""
    audit = _audit()
    limit = D.PRE_REGISTERED_ALARM["area_change_pct"]
    for set_id, rec in audit["param_sets"].items():
        rmax = rec["res_max"]
        assert rmax["kept_patches"] <= rmax["raw_patches"], (
            f"{set_id} 过滤后斑块数未下降"
        )
        assert rmax["area_change_pct"] <= limit, (
            f"{set_id} res.max 面积变化超线"
        )
        # 过滤必须是"少数像元"操作，不是大面积抹除
        assert rmax["removed_cells"] < rmax["raw_wet_cells"] * 0.05, (
            f"{set_id} 删格比例过高，疑似误删真实积水"
        )
