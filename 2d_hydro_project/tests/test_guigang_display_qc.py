"""贵港 display_qc 展示质控层契约测试（贵港计划 §3 Phase 1 / §4 / §5）。

本文件把 Phase 1 的硬约束写成可执行断言，结构与 ``tests/test_display_qc.py``
（久隆）对齐，并补充贵港特有的两条纪律：

1. **raw 只读**：源 run 的 ``res.max`` 与全部 ``res-*.wd`` 的 sha256 在测试前后一致。
2. **派生标记完整**：每个派生目录的 ``meta.json`` 必须声明派生属性。
3. **阈值按 cellsize 换算**：面积阈值以 km² 登记，像元数由 ``cells_for_area`` 现算；
   脚本不得写死 ``10000.0`` 单元面积。
4. **账目守恒**：``raw = removed + kept``，变化率可复算。
5. **res.mass 拒写**：派生目录不得含 ``results/res.mass``（否则污染 g15 质量门）。
6. **规则顺序**：先豁免后过滤，任何豁免条件成立都不会被过滤。
7. **表述纪律**：不得把本层说成“校准/真实淹没/物理修复”，不得归因未核验规范。
8. **预注册不可事后挑选**：A/B/C 三档数值等于计划 §3 表格原值；``param_hash`` 可复算。
9. **主水体 100% 保护**：res.max 最大连通斑块 614.96 km² 必须零像元被删。
10. **验收差距如实登记**：未达计划 §4 的 >=90% 目标时必须写明，不得写成已达成。

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
from scripts.stage6 import clean_guigang_display as G  # noqa: E402

EVENT = "2024-09-yagi"
RUN_ID = "scen_yagi_q11500_100m_dt2p5_choff_realdem"
DISPLAY_RUN_ID = "scen_yagi_q11500_100m_realdem_display_qc"
RUN_DIR = PROJ / "outputs" / "guigang" / EVENT / RUN_ID
DISPLAY_ROOT = PROJ / "outputs" / "display_qc" / "guigang" / EVENT
DISPLAY_DIR = DISPLAY_ROOT / DISPLAY_RUN_ID
AUDIT_PATH = PROJ / "data" / "catalog" / "guigang_display_qc_audit.json"
SCRIPT = PROJ / "scripts" / "stage6" / "clean_guigang_display.py"

#: 计划 §3 Phase 1 表格原值（预注册参数，不得为凑结果修改）。
PLAN_TIERS = {
    "A": {"shallow_max_area_km2": 0.04, "shallow_depth_m": 0.15,
          "planned_patch_removal_pct": 80.2},
    "B": {"shallow_max_area_km2": 0.09, "shallow_depth_m": 0.30,
          "planned_patch_removal_pct": 92.5},
    "C": {"shallow_max_area_km2": 0.25, "shallow_depth_m": 0.50,
          "planned_patch_removal_pct": 96.8},
}

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
        pytest.skip("贵港 display_qc 审计尚未生成（Phase 1 未完成）")
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

    current = _source_hashes(RUN_DIR)
    assert current["res.max"] == integrity["after"]["res_max_sha256"], (
        "res.max 与审计记录不符：源 run 可能已被修改"
    )
    assert len(current) - 1 == integrity["after"]["frame_count"], "帧数与审计记录不符"

    recorded = audit["param_sets"]["A"]["meta"]["source_frame_list_sha256"]
    assert recorded == integrity["after"]["frame_list_sha256"], "帧清单哈希不一致"


def test_source_hashes_stable_within_test_session() -> None:
    """同一次测试内前后各算一次哈希，必须逐位一致（证明测试自身也不写 raw）。"""
    before = _source_hashes(RUN_DIR)
    after = _source_hashes(RUN_DIR)
    assert before == after, "测试运行期间源 run 发生变化"
    # 57 帧 + res.max
    assert len(before) == 58, f"期望 57 帧 + res.max，实得 {len(before)} 个文件"


def test_derived_layers_live_under_display_qc_not_raw() -> None:
    """派生层必须落在 outputs/display_qc 下，不得写进 outputs/guigang。"""
    audit = _audit()
    for set_id, rec in audit["param_sets"].items():
        derived = rec["derived_dir"]
        assert derived.startswith("outputs/display_qc/"), (
            f"参数档 {set_id} 的派生目录 {derived} 不在 outputs/display_qc 下"
        )
        assert not derived.startswith("outputs/guigang/"), (
            f"参数档 {set_id} 写入了受保护的 outputs/guigang/**"
        )


# --------------------------------------------------------------------------- #
# 2. 派生标记完整
# --------------------------------------------------------------------------- #
def test_display_qc_meta_marks_derived_layer() -> None:
    """每个派生目录的 meta.json 必须含全部派生标记。"""
    dirs = _display_dirs()
    assert dirs, "outputs/display_qc/guigang 下没有 meta.json"
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
        assert meta["param_hash"] == G.param_hash(meta["params"]), (
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
# 3. 阈值按 cellsize 换算
# --------------------------------------------------------------------------- #
def test_cells_for_area_uses_header_cell_area() -> None:
    """``n_cells = ceil(area_threshold_m2 / cell_area_m2)``，100 m 每格 0.01 km²。"""
    header = read_ascii_header(RUN_DIR / "results" / "res.max")
    cell_area = float(header["cellsize"]) ** 2
    assert cell_area == 10000.0

    for area_km2 in (0.04, 0.09, 0.25, 0.5, 5.0):
        n = G.cells_for_area(area_km2, cell_area)
        assert n == math.ceil(area_km2 * 1e6 / cell_area)
    # 计划 §3 表格括注：0.04 km² = 4 个像元，0.09 = 9 个像元，0.25 = 25 个像元
    assert G.cells_for_area(0.04, cell_area) == 4
    assert G.cells_for_area(0.09, cell_area) == 9
    assert G.cells_for_area(0.25, cell_area) == 25


def test_cells_for_area_rejects_bad_cell_area() -> None:
    """单元面积为 0 或负时必须报错，不得静默返回 1。"""
    with pytest.raises(GridError):
        G.cells_for_area(0.1, 0.0)
    with pytest.raises(GridError):
        G.cells_for_area(0.1, -10000.0)


def test_frames_for_persistence_uses_saveint() -> None:
    """持续性阈值按小时登记，帧数由 saveint 换算（6 h/帧）。"""
    saveint = 21600.0  # 6 h
    assert G.frames_for_persistence(36.0, saveint) == 6
    assert G.frames_for_persistence(72.0, saveint) == 12
    assert G.frames_for_persistence(144.0, saveint) == 24
    # 同样的小时数在 1 h 输出的 run 上应得 36/72/144 帧
    assert G.frames_for_persistence(36.0, 3600.0) == 36
    with pytest.raises(GridError):
        G.frames_for_persistence(36.0, 0.0)


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
            ("shallow_max_area_km2", "n_cells_shallow_max_area"),
            ("main_stem_min_area_km2", "n_cells_main_stem_min_area"),
        ):
            assert params[cells_key] == math.ceil(params[area_key] * 1e6 / cell_area), (
                f"{set_id} 的 {cells_key} 与 {area_key} 换算不符"
            )
        assert params["persistence_frames"] == math.ceil(
            params["persistence_hours"] * 3600.0 / params["saveint_s"]
        )
        assert params["min_persistence_ratio"] == pytest.approx(
            params["persistence_frames"] / params["n_frames"], rel=1e-12)
        assert params["source"] == "项目候选参数，未经本地率定"
        assert params["confidence"] == "E3 候选假设"


def test_param_sets_equal_plan_table_values() -> None:
    """三档面积/水深上限必须**逐位等于**计划 §3 表格原值，不得为凑结果修改。"""
    audit = _audit()
    for set_id, expected in PLAN_TIERS.items():
        params = audit["param_sets"][set_id]["params"]
        assert params["shallow_max_area_km2"] == expected["shallow_max_area_km2"], (
            f"{set_id} 档面积上限被改动：{params['shallow_max_area_km2']}"
            f" != 计划值 {expected['shallow_max_area_km2']}"
        )
        assert params["shallow_depth_m"] == expected["shallow_depth_m"], (
            f"{set_id} 档水深上限被改动：{params['shallow_depth_m']}"
            f" != 计划值 {expected['shallow_depth_m']}"
        )
        assert params["planned_patch_removal_pct"] == expected["planned_patch_removal_pct"]


def test_param_sets_are_ordered_conservative_to_aggressive() -> None:
    """A/B/C 必须单调加严，否则"三档"没有敏感性含义。"""
    audit = _audit()
    order = ["A", "B", "C"]
    areas = [audit["param_sets"][k]["params"]["shallow_max_area_km2"] for k in order]
    depths = [audit["param_sets"][k]["params"]["shallow_depth_m"] for k in order]
    hours = [audit["param_sets"][k]["params"]["persistence_hours"] for k in order]
    for values in (areas, depths, hours):
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
            assert frame["removed_cells"] <= frame["wet_cells"], (
                f"{set_id} 帧 {seq} 删格数超过湿格数")
            assert frame["kept_wet_cells"] == frame["wet_cells"] - frame["removed_cells"], (
                f"{set_id} 帧 {seq} 像元账目不平"
            )
            assert abs(
                frame["removed_area_km2"] - frame["removed_cells"] * cell_area / 1e6
            ) < 1e-9, f"{set_id} 帧 {seq} 面积账目不平"
            assert abs(
                frame["raw_wet_area_km2"] - frame["removed_area_km2"]
                - frame["kept_wet_area_km2"]
            ) < 1e-6, f"{set_id} 帧 {seq} 面积不守恒"
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
    """res.max 峰值场的账目同样必须平（双口径）。

    派生 res.max 取「保留帧包络」，因此必须区分两套口径（见 ``res_max_scope_note``）：

    - 口径 A（``verdict_*``）：源 res.max 的斑块级判定删除量，对齐计划 §4 的
      4,422 斑块口径。``patch_removal_pct`` 由此得出。
    - 口径 B（``removed_*`` / ``kept_*`` / ``*_change_pct``）：派生包络相对源
      res.max 的实际变化。源 res.max 是子步累积包络、含帧间峰，派生包络只由
      6 h 采样帧构成，故口径 B 的减少量必然大于口径 A。

    两套口径都必须各自闭合，且不得混用。
    """
    audit = _audit()
    cell_area = float(audit["scope"]["grid"]["cell_area_m2"])
    for set_id, rec in audit["param_sets"].items():
        rmax = rec["res_max"]
        # 口径 B：湿格账目必须精确闭合
        assert rmax["kept_wet_cells"] == rmax["raw_wet_cells"] - rmax["removed_cells"]
        assert abs(rmax["removed_area_km2"] - rmax["removed_cells"] * cell_area / 1e6) < 1e-9
        assert abs(
            rmax["raw_wet_area_km2"] - rmax["removed_area_km2"] - rmax["kept_wet_area_km2"]
        ) < 1e-6
        # 口径 A：斑块级判定删除量必须存在且自洽
        assert "verdict_removed_cells" in rmax, "必须保留源 res.max 斑块级判定口径"
        assert abs(
            rmax["verdict_removed_area_km2"]
            - rmax["verdict_removed_cells"] * cell_area / 1e6
        ) < 1e-9
        # 包络口径的减少量必须 >= 斑块判定口径（源 res.max 含帧间峰）
        assert rmax["removed_cells"] >= rmax["verdict_removed_cells"], (
            f"{set_id}: 派生包络减少量应不小于斑块判定删除量"
            f"（{rmax['removed_cells']} vs {rmax['verdict_removed_cells']}）"
        )
        assert rmax["kept_patches"] <= rmax["raw_patches"], "过滤不应增加斑块数"
        assert rmax["res_max_scope_note"], "必须写明双口径说明，防止混用"


def test_res_max_envelope_guarantees_monotonicity() -> None:
    """派生 res.max 必须是保留帧的包络，即 >= 任一帧（floodviz 单向性断言）。

    这是 v8 发布包 validate 曾失败（4 项「违反单向性」）的回归护栏。
    """
    from scripts.common.grid_io import read_ascii_grid, run_frames

    root = PROJ / "outputs" / "display_qc" / "guigang" / "2024-09-yagi" / (
        "scen_yagi_q11500_100m_realdem_display_qc"
    )
    if not root.is_dir():
        pytest.skip("贵港 display_qc 尚未生成")
    for set_id in ("A", "B", "C"):
        d = root / set_id / "results"
        if not (d / "res.max").is_file():
            pytest.skip(f"{set_id} 缺 res.max")
        rmax = read_ascii_grid(d / "res.max").data
        worst = 0.0
        worst_seq = None
        for seq, path in run_frames(d):
            frame = read_ascii_grid(path).data
            diff = float((frame - rmax).max())
            if diff > worst:
                worst, worst_seq = diff, seq
        assert worst <= 1e-6, (
            f"{set_id}: res.max 低于第 {worst_seq} 帧 {worst:.6f} m，违反单向性"
        )


def test_every_frame_is_audited() -> None:
    """逐帧审计必须覆盖全部 57 帧（计划 §3 第 8 条）。"""
    audit = _audit()
    temporal = audit["scope"]["temporal"]
    assert temporal["frame_count"] == 57
    for set_id, rec in audit["param_sets"].items():
        seqs = [frame["seq"] for frame in rec["frames"]]
        assert seqs == list(range(57)), f"{set_id} 帧号不连续：{seqs[:5]}..."
        for frame in rec["frames"]:
            for key in ("removed_cells", "removed_area_km2", "removed_volume_m3",
                        "n_patches_raw", "n_patches_after", "removed_reason_patches",
                        "area_change_pct", "volume_change_pct"):
                assert key in frame, f"{set_id} 帧 {frame['seq']} 缺字段 {key}"


# --------------------------------------------------------------------------- #
# 5. res.mass 拒写 / 派生目录守卫
# --------------------------------------------------------------------------- #
def test_display_qc_never_contains_res_mass() -> None:
    """派生目录不得含 res.mass：g15 会把它误计为独立 run（污染质量门计数）。"""
    if not DISPLAY_ROOT.is_dir():
        pytest.skip("display_qc 尚未生成")
    offenders = [str(p) for p in DISPLAY_ROOT.rglob("res.mass")]
    assert not offenders, f"派生目录出现 res.mass：{offenders}"
    assert not list(DISPLAY_ROOT.rglob("results/res.mass"))


def test_guard_derived_target_refuses_existing_res_mass(tmp_path: Path) -> None:
    """目标目录已有 results/res.mass 时，写前守卫必须拒绝。"""
    target = tmp_path / "fake_run"
    (target / "results").mkdir(parents=True)
    (target / "results" / "res.mass").write_text("Time\n", encoding="utf-8")
    with pytest.raises(OverwriteRefused):
        G.guard_derived_target(target)
    with pytest.raises(OverwriteRefused):
        guard_no_overwrite(target)


def test_guard_derived_target_allows_fresh_dir(tmp_path: Path) -> None:
    """空目录允许写入；已有派生 meta 也允许（幂等重跑）。"""
    target = tmp_path / "fresh"
    target.mkdir()
    G.guard_derived_target(target)
    (target / "meta.json").write_text(
        json.dumps({"not_physical_recalculation": True}), encoding="utf-8"
    )
    G.guard_derived_target(target)


def test_guard_derived_target_refuses_non_derived_dir(tmp_path: Path) -> None:
    """非派生目录（无派生标记）必须拒绝覆盖，避免误写真实结果目录。"""
    target = tmp_path / "real_looking"
    target.mkdir()
    (target / "meta.json").write_text(json.dumps({"scenario": "free"}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="not_physical_recalculation"):
        G.guard_derived_target(target)


def test_refuses_to_write_inside_protected_raw_runs(tmp_path: Path) -> None:
    """派生目录落在 outputs/guigang/** 内时必须拒绝（禁止修改 raw run）。"""
    with pytest.raises(RuntimeError, match="受保护目录"):
        G._assert_outside_raw_runs(PROJ / "outputs" / "guigang" / EVENT / "x")
    with pytest.raises(RuntimeError, match="受保护目录"):
        G._assert_outside_raw_runs(PROJ / "outputs" / "guigang")
    G._assert_outside_raw_runs(DISPLAY_ROOT)
    G._assert_outside_raw_runs(tmp_path)


def test_forbidden_file_detector_catches_res_mass(tmp_path: Path) -> None:
    """派生目录里出现 res.mass 时，检测函数必须报错。"""
    target = tmp_path / "derived"
    (target / "results").mkdir(parents=True)
    (target / "results" / "res.max").write_text("x\n", encoding="utf-8")
    G.assert_no_forbidden_files(target)
    (target / "results" / "res.mass").write_text("x\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="res.mass"):
        G.assert_no_forbidden_files(target)


def test_derived_results_dir_has_frames_but_no_res_mass() -> None:
    """派生 results 目录必须有 57 帧与 res.max，且明确不含 res.mass。"""
    dirs = _display_dirs()
    assert dirs
    for path in dirs:
        results = path / "results"
        assert results.is_dir(), f"{path} 缺 results 目录"
        assert (results / "res.max").is_file(), f"{path} 缺 res.max 派生副本"
        frames = sorted(results.glob("res-*.wd"))
        assert len(frames) == 57, f"{path} 帧数应为 57，实得 {len(frames)}"
        assert not (results / "res.mass").exists(), f"{path} 误含 res.mass"


# --------------------------------------------------------------------------- #
# 6. 规则顺序：豁免优先
# --------------------------------------------------------------------------- #
def _patch(area_km2: float, max_depth_m: float, *, n_cells: int = 1, label: int = 1):
    from scripts.common.patch_analysis import Patch

    return Patch(
        label=label, n_cells=n_cells, area_km2=area_km2, max_depth_m=max_depth_m,
        mean_depth_m=max_depth_m, p95_depth_m=max_depth_m, volume_m3=1.0,
        row_min=5, row_max=5, col_min=5, col_max=5,
        centroid_row=5.0, centroid_col=5.0,
        cell_indices=np.array([100], dtype=np.int64),
    )


def _track_params(set_id: str = "A") -> dict:
    """构造 classify_patch 需要的完整参数 dict（来自真实预注册档）。"""
    return dict(G.resolve_param_set(
        G.PRE_REGISTERED_PARAM_SETS[set_id],
        cell_area_m2=10000.0, cellsize_m=100.0, saveint_s=21600.0, n_frames=57,
    ))


def test_classify_patch_exemption_precedes_filter() -> None:
    """任一豁免条件成立时，即使斑块很小很浅，也必须判为 exempt。"""
    params = _track_params("A")
    tiny = _patch(0.02, 0.10)  # 远小于 0.04 km²、远浅于 0.15 m
    base = dict(
        is_main_body=False, longest_consecutive_run=1, persistence_ratio=1 / 57,
        is_recession_growth=False, hits_boundary=False, min_distance_cells=5,
    )
    # 基线：小 + 浅 + 短暂 → 过滤
    assert G.classify_patch(tiny, params, **base) == ("filter", "FILTER_DISCRETE_PUDDLE")

    # 逐个豁免条件单独成立，都必须翻转成 exempt
    cases = [
        ({"is_main_body": True}, "EXEMPT_MAIN_BODY"),
        ({"is_recession_growth": True}, "EXEMPT_RECESSION_GROWTH"),
        ({"hits_boundary": True}, "EXEMPT_BOUNDARY_OUTLET"),
    ]
    for override, expected in cases:
        kwargs = dict(base)
        kwargs.update(override)
        verdict, reason = G.classify_patch(tiny, params, **kwargs)
        assert verdict == "exempt", f"{expected} 未优先于过滤"
        assert reason == expected

    # 主干水体面积豁免（计划 §3 第 1 条，>=5 km²）
    assert G.classify_patch(
        _patch(5.0, 0.10), params, **base) == ("exempt", "EXEMPT_MAIN_STEM_LARGE")
    # 显著深水洼地豁免（计划 §3 第 2 条，>=0.50 m）
    assert G.classify_patch(
        _patch(0.02, 0.50), params, **base) == ("exempt", "EXEMPT_DEEP_WATER")


def test_classify_patch_filter_requires_all_conditions() -> None:
    """第 3 条过滤是合取：面积或水深任一超限都不应触发该条。"""
    params = _track_params("A")  # 0.04 km² / 0.15 m / 6 帧
    base = dict(
        is_main_body=False, longest_consecutive_run=1, persistence_ratio=1 / 57,
        is_recession_growth=False, hits_boundary=False, min_distance_cells=5,
    )
    assert G.classify_patch(_patch(0.04, 0.15), params, **base) == (
        "filter", "FILTER_DISCRETE_PUDDLE")
    # 面积超限（0.05 > 0.04）→ 落到第 4 条持久性；此处为短暂，故仍过滤但原因不同
    assert G.classify_patch(_patch(0.05, 0.10), params, **base) == (
        "filter", "FILTER_TRANSIENT_NOISE")
    # 水深超限（0.20 > 0.15）→ 同上
    assert G.classify_patch(_patch(0.02, 0.20), params, **base) == (
        "filter", "FILTER_TRANSIENT_NOISE")
    # 既超面积又超水深、且持久 → 保留
    long_lived = dict(base, longest_consecutive_run=20, persistence_ratio=0.5)
    assert G.classify_patch(_patch(0.05, 0.20), params, **long_lived) == (
        "keep", "KEEP_NATURAL_WETLAND")


def test_persistence_rule_is_independent_and_effective() -> None:
    """第 4 条时空持久性必须真正生效：两种“短暂”判定任一成立即过滤。"""
    params = _track_params("B")  # 12 帧 / ratio 12/57
    base = dict(
        is_main_body=False, is_recession_growth=False, hits_boundary=False,
        min_distance_cells=5,
    )
    patch = _patch(0.5, 0.20)  # 面积、水深都超出第 3 条上限 → 只能由第 4 条过滤

    # 连续帧数不足 → 过滤
    assert G.classify_patch(
        patch, params, longest_consecutive_run=11, persistence_ratio=0.9,
        **base) == ("filter", "FILTER_TRANSIENT_NOISE")
    # 连续帧数够，但出现帧占比不足 → 仍过滤（两判定取"或"）
    assert G.classify_patch(
        patch, params, longest_consecutive_run=12, persistence_ratio=0.20,
        **base) == ("filter", "FILTER_TRANSIENT_NOISE")
    # 两条都满足 → 保留
    assert G.classify_patch(
        patch, params, longest_consecutive_run=12, persistence_ratio=0.5,
        **base) == ("keep", "KEEP_NATURAL_WETLAND")


def test_recorded_filter_records_satisfy_no_exemption() -> None:
    """已落盘判定中，任何 filter 斑块都不得同时满足任一豁免条件。"""
    audit = _audit()
    for set_id in audit["param_sets"]:
        path = DISPLAY_DIR / set_id / "audit" / f"track_verdicts_{set_id}.json"
        if not path.is_file():
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        params = audit["param_sets"][set_id]["params"]
        filtered = [r for r in doc["patch_verdicts"] if r["verdict"] == "filter"]
        assert filtered, f"{set_id} 没有任何过滤记录，规则可能未生效"
        for rec in filtered:
            assert rec["is_main_body"] is False, "主水体斑块被过滤，违反豁免优先"
            assert rec["hits_boundary"] is False, "触边界斑块被过滤，违反豁免优先"
            assert rec["is_recession_growth"] is False, "退水增长斑块被过滤，违反豁免优先"
            assert rec["area_km2"] < params["main_stem_min_area_km2"], (
                "达到主干水体面积豁免仍被过滤")
            assert rec["max_depth_m"] < params["deep_depth_threshold_m"], (
                "达到深水豁免仍被过滤")
        assert doc["param_hash"], "判定文件必须带参数哈希"


def test_main_body_labels_pick_largest_patches() -> None:
    """候选主汇流区取面积最大的若干斑块，直到覆盖指定比例的湿面积。"""
    from scripts.common.patch_analysis import FramePatchSummary, Patch

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
    assert G.main_body_patch_labels(summary, 0.5) == {1}
    assert G.main_body_patch_labels(summary, 0.9) == {1, 2, 3}
    empty = FramePatchSummary(
        seq=0, n_patches=0, wet_cells=0, wet_area_km2=0.0, wet_volume_m3=0.0,
        max_depth_m=0.0, patches=[],
    )
    assert G.main_body_patch_labels(empty, 0.5) == set()


def test_touches_boundary_detects_edges() -> None:
    """触边界判定必须识别四条边。"""
    from scripts.common.patch_analysis import Patch

    def make(row_min: int, row_max: int, col_min: int, col_max: int) -> Patch:
        return Patch(
            label=1, n_cells=1, area_km2=0.01, max_depth_m=1.0, mean_depth_m=1.0,
            p95_depth_m=1.0, volume_m3=1.0,
            row_min=row_min, row_max=row_max, col_min=col_min, col_max=col_max,
            centroid_row=0.0, centroid_col=0.0,
        )

    shape = (10, 10)
    assert G.touches_boundary(make(0, 2, 3, 4), shape) is True
    assert G.touches_boundary(make(7, 9, 3, 4), shape) is True
    assert G.touches_boundary(make(3, 4, 0, 2), shape) is True
    assert G.touches_boundary(make(3, 4, 7, 9), shape) is True
    assert G.touches_boundary(make(3, 4, 3, 4), shape) is False


def test_distance_to_mask_is_eight_neighbour_bfs() -> None:
    """距离场用 8 邻域：对角相邻距离为 1（与连通域口径一致）。"""
    mask = np.zeros((5, 5), dtype=bool)
    mask[2, 2] = True
    dist = G.distance_to_mask(mask)
    assert dist[2, 2] == 0
    assert dist[1, 1] == 1, "对角相邻必须是 1（8 邻域）"
    assert dist[2, 3] == 1
    assert dist[0, 0] == 2
    assert dist[2, 0] == 2
    assert (dist >= 0).all()


def test_distance_to_mask_unreachable_is_negative() -> None:
    """空掩膜时全部不可达（-1），不得返回 0 冒充"在主水体上"。"""
    dist = G.distance_to_mask(np.zeros((3, 3), dtype=bool))
    assert (dist == -1).all()


# --------------------------------------------------------------------------- #
# 7. 像元分类与 float32 口径
# --------------------------------------------------------------------------- #
def test_classify_cells_distinguishes_categories() -> None:
    """nodata / 海 / 干 / 浅 / 深 五类必须分开且互斥。

    注意 ``float32(0.05)`` 实际是 ``0.05000000074505806``，严格大于 0.05，
    所以写成 0.05 的像元在 float32 口径下**算湿**（与 summarize_frame 一致）。
    """
    depth = np.array([
        [1.0, 0.0501, 0.04],
        [0.0, -9999.0, 0.20],
        [0.5, 0.5, 0.5],
    ], dtype=np.float32)
    sea = np.array([
        [False, False, False],
        [False, False, False],
        [True, True, True],
    ])
    counts = G.classify_cells(
        depth, sea_mask=sea, wet_threshold_m=0.05,
        shallow_depth_m=0.10, nodata=-9999.0,
    )
    assert counts["nodata_cells"] == 1
    assert counts["sea_cells"] == 3
    assert counts["wet_cells"] == 3
    assert counts["valid_shallow_cells"] == 1, "只有 0.0501 属于有效浅水"
    assert counts["valid_deep_cells"] == 2, "1.0 与 0.20 属于有效深水"
    assert counts["dry_cells"] == 2
    assert counts["wet_cells"] == counts["valid_shallow_cells"] + counts["valid_deep_cells"]


def test_classify_cells_matches_summarize_frame_wet_count() -> None:
    """像元分类的 wet_cells 必须与 patch_analysis.summarize_frame 完全一致。"""
    from scripts.common.patch_analysis import summarize_frame

    depth = np.array([
        [0.05, 0.0500001, 0.0499999],
        [0.0, 1.0, 0.2],
    ], dtype=np.float32)
    counts = G.classify_cells(
        depth, sea_mask=None, wet_threshold_m=0.05,
        shallow_depth_m=0.10, nodata=-9999.0,
    )
    summary = summarize_frame(
        depth, 0, wet_threshold_m=0.05, cell_area_m2=10000.0, keep_patches=False,
    )
    assert counts["wet_cells"] == summary.wet_cells, (
        f"分类器 {counts['wet_cells']} 与 summarize_frame {summary.wet_cells} 口径不一致"
    )


def test_quick_frame_counts_matches_summarize_frame() -> None:
    """快速统计口径必须与 summarize_frame 一致（只省略逐斑块分位）。"""
    from scripts.common.patch_analysis import summarize_frame

    depth = np.zeros((12, 12), dtype=np.float32)
    depth[1:3, 1:3] = 0.5   # 4 格斑块
    depth[6, 6] = 0.2       # 1 格斑块
    depth[8:11, 8:9] = 0.05  # float32(0.05) 严格大于 0.05 → 湿
    quick = G._quick_frame_counts(
        depth, wet_threshold_m=0.05, cell_area_m2=10000.0, sea_mask=None)
    summary = summarize_frame(
        depth, 0, wet_threshold_m=0.05, cell_area_m2=10000.0, keep_patches=False)
    assert quick["wet_cells"] == summary.wet_cells
    assert quick["n_patches"] == summary.n_patches
    assert quick["wet_area_km2"] == pytest.approx(summary.wet_area_km2, rel=1e-12)
    assert quick["wet_volume_m3"] == pytest.approx(summary.wet_volume_m3, rel=1e-6)


def test_classify_cells_handles_nan_as_nodata() -> None:
    """NaN 必须算 nodata，不得算干格。"""
    depth = np.array([[np.nan, 0.5]], dtype=np.float32)
    counts = G.classify_cells(
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
        assert prereg["area_change_pct"] == G.PRE_REGISTERED_ALARM["area_change_pct"]
        assert prereg["volume_change_pct"] == G.PRE_REGISTERED_ALARM["volume_change_pct"]
        assert prereg["confidence"] == "E3 候选假设"
        assert prereg["basis"], "告警阈值必须写明依据"
        assert "blocked_for_default_publish" in alarm
        assert isinstance(alarm["blocked_reasons"], list)
        assert alarm["blocked_for_default_publish"] == bool(alarm["blocked_reasons"])


def test_alarm_decision_is_recomputable_from_recorded_numbers() -> None:
    """blocked 判定必须能由审计里记录的变化率与阈值复算。"""
    audit = _audit()
    limit_area = G.PRE_REGISTERED_ALARM["area_change_pct"]
    limit_volume = G.PRE_REGISTERED_ALARM["volume_change_pct"]
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
        assert rec["meta"]["alarm"]["blocked_for_default_publish"] is alarm_blocked
        on_disk = json.loads(
            (DISPLAY_DIR / set_id / "meta.json").read_text(encoding="utf-8")
        )
        assert on_disk["alarm"]["blocked_for_default_publish"] is alarm_blocked


# --------------------------------------------------------------------------- #
# 9. 表述纪律
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
    """脚本正文不得把候选参数归因于未核验的国际规范。"""
    text = SCRIPT.read_text(encoding="utf-8")
    for claim in FORBIDDEN_CLAIMS:
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


# --------------------------------------------------------------------------- #
# 10. 主水体保护（计划 §4：614.96 km² 100% 完整保护）
# --------------------------------------------------------------------------- #
def test_main_stem_patch_is_fully_protected() -> None:
    """res.max 最大连通斑块 614.96 km² 必须零像元被删（计划 §4 验收项）。"""
    audit = _audit()
    for set_id, rec in audit["param_sets"].items():
        lp = rec["main_body"]["largest_patch"]
        assert lp["raw_area_km2"] == pytest.approx(614.96, abs=1e-6), (
            f"{set_id} 主水体面积应为 614.96 km²，实得 {lp['raw_area_km2']}"
        )
        assert lp["plan_reference_area_km2"] == 614.96
        assert lp["removed_cells"] == 0, f"{set_id} 主水体被删了 {lp['removed_cells']} 个像元"
        assert lp["kept_area_km2"] == pytest.approx(614.96, abs=1e-6)
        assert lp["fully_protected"] is True
        assert rec["acceptance"]["main_stem_fully_protected"] is True
        assert rec["acceptance"]["main_stem_kept_area_km2"] == pytest.approx(614.96, abs=1e-6)


def test_main_body_union_is_fully_protected() -> None:
    """候选主汇流区（跨帧并集）也必须零像元被删。"""
    audit = _audit()
    for set_id, rec in audit["param_sets"].items():
        mb = rec["main_body"]
        assert mb["removed_cells"] == 0, f"{set_id} 候选主汇流区被删像元"
        assert mb["fully_protected"] is True
        assert mb["kept_cells"] == mb["raw_cells"]
        assert mb["kept_area_km2"] == pytest.approx(mb["raw_area_km2"], abs=1e-6)


def test_s1_mask_not_used_as_main_stem_whitelist() -> None:
    """必须登记为何不用 S1 常水掩膜做白名单，并附实测数字。"""
    audit = _audit()
    prot = audit["main_body_protection"]
    evidence = prot["s1_mask_evidence"]
    assert evidence["why_not_used"], "必须写明为何不用 S1 白名单"
    measured = evidence["measured"]
    # 实测证据：足印仅约 26%、北半域为 0、最大常水块远小于主水体
    assert measured["footprint_fraction_of_model_domain"] == pytest.approx(0.2619, abs=1e-4)
    assert measured["north_half_row_valid_coverage"] == 0.0
    assert measured["largest_constant_water_patch_km2_at_20m"] < measured["main_body_area_km2"] / 10
    assert measured["main_body_area_km2"] == 614.96
    for set_id in audit["param_sets"]:
        meta = audit["param_sets"][set_id]["meta"]
        assert meta["main_body_protection"]["s1_mask_used"] is False
        assert "候选主汇流区" in meta["main_body_protection"]["definition"]


# --------------------------------------------------------------------------- #
# 11. 验收差距：如实登记未达成
# --------------------------------------------------------------------------- #
def test_acceptance_is_honestly_reported_as_not_met() -> None:
    """计划 §4 的 >=90% 去除目标在计划参数下不可达，必须如实写"未达到"。"""
    audit = _audit()
    for set_id, rec in audit["param_sets"].items():
        acc = rec["acceptance"]
        rmax = rec["res_max"]
        # 实测去除率远低于计划预测值
        assert acc["actual_patch_removal_pct"] == rmax["patch_removal_pct"]
        assert acc["actual_patch_removal_pct"] < acc["plan_predicted_removal_pct"], (
            f"{set_id} 实测去除率不应高于计划预测值"
        )
        assert acc["deviation_pp"] < 0, "偏差必须为负（低于计划预测）"
        # 未达到计划 §4 的 <=450 斑块目标
        assert rmax["kept_patches"] > acc["plan_target_patch_count"], (
            f"{set_id} 斑块数 {rmax['kept_patches']} 意外达到了 <=450 目标，"
            "请复核是否放宽了预注册阈值"
        )
        assert acc["patch_target_met"] is False
        assert acc["verdict"] == "未达到计划 §4 验收目标", (
            f"{set_id} 验收结论必须如实写未达到，实得 {acc['verdict']}"
        )
        # meta 里也必须带同一结论
        assert rec["meta"]["acceptance"]["verdict"] == "未达到计划 §4 验收目标"


def test_acceptance_gap_documents_structural_ceiling() -> None:
    """审计必须量化计划规则结构自身的去除率上界，解释差距原因。"""
    audit = _audit()
    gap = audit["acceptance_gap"]
    assert gap["diagnostic_only"] is True
    assert gap["why"]
    ceiling = gap["ceiling_under_plan_rules"]
    # 计划 §3 第 2 条把 max_depth >= 0.50 m 全豁免 → 去除率上界远低于 90%
    assert ceiling["removable_pct_upper_bound"] < 30.0, (
        "在计划规则结构下，去除率上界应显著低于计划 §4 的 90% 目标"
    )
    assert ceiling["removable_patches_upper_bound"] < 4422 * 0.9
    # 三档实际去除率都低于该上界
    for set_id, rec in gap["pre_registered_actual"].items():
        assert rec["patch_removal_pct"] <= ceiling["removable_pct_upper_bound"] + 1e-6
    assert "不可达" in gap["conclusion"] or "未达到" in gap["conclusion"]


def test_persistence_only_removal_is_reported_separately() -> None:
    """必须单独报告"仅持久性规则能去掉多少"，证明第 4 条真正生效。"""
    audit = _audit()
    gap = audit["acceptance_gap"]
    persistence_only = gap["persistence_only_removal"]
    assert set(persistence_only) >= {"A", "B", "C"}
    for set_id, rec in persistence_only.items():
        assert rec["removed_patches"] > 0, f"{set_id} 持久性规则未去掉任何斑块"
        assert rec["patch_removal_pct"] > 0.0


def test_gap_reports_minimal_change_requirement() -> None:
    """达到 >=90% 所需的最小阈值变更必须登记，并标明属参数变更。"""
    audit = _audit()
    gap = audit["acceptance_gap"]
    minimal = gap["minimal_change_to_meet_target"]
    if minimal is None:
        pytest.skip("阈值扫描未找到达标组合")
    assert minimal["deep_exemption_disabled"] is True
    assert minimal["depth_cap_m"] > 0.50, "达标必须放宽水深上限"
    assert minimal["patch_removal_pct"] >= 89.82
    assert "重新预注册" in minimal["note"]
    assert "不得作为本次交付结论" in minimal["note"]


def test_small_patch_depth_profile_explains_gap() -> None:
    """审计必须记录离散斑块的深度谱，解释为何"面积+水深"滤不掉。"""
    audit = _audit()
    prof = audit["small_patch_depth_profile"]
    assert prof["le_9_cell_patch_count"] == 4089
    q = prof["le_9_cell_max_depth_quantiles_m"]
    # 实测中位数约 1.03 m，远高于计划三档的水深上限
    assert q["p50"] == pytest.approx(1.034, abs=0.01)
    assert q["p50"] > max(t["shallow_depth_m"] for t in PLAN_TIERS.values())
    # 落在各档水深上限内的斑块数远少于总数
    within = prof["le_9_cell_within_tier_depth_caps"]
    for set_id, count in within.items():
        assert count < prof["le_9_cell_patch_count"] * 0.3, (
            f"{set_id} 档水深上限覆盖的斑块比例不应超过 30%"
        )


# --------------------------------------------------------------------------- #
# 12. 轨迹追踪契约
# --------------------------------------------------------------------------- #
def test_tracker_contract_exposes_cell_owner() -> None:
    """PersistenceTracker 必须暴露 int→int 的像元→轨迹映射，否则展示质控无法回写。"""
    from scripts.common.patch_analysis import PersistenceTracker, summarize_frame

    depth = np.zeros((6, 6), dtype=np.float32)
    depth[1:3, 1:3] = 1.0
    tracker = PersistenceTracker()
    tracker.update(
        summarize_frame(depth, 0, wet_threshold_m=0.05, cell_area_m2=10000.0),
        width=6,
    )
    owner = G.assert_tracker_contract(tracker)
    assert owner
    assert all(isinstance(k, int) and isinstance(v, int) for k, v in owner.items())
    assert len(owner) == 4, "4 个湿格都应被记录"


def test_tracker_contract_rejects_missing_attribute() -> None:
    """若 Lead 模块改动导致内部状态消失，必须显式报错而不是静默退化。"""
    class Broken:
        pass

    with pytest.raises(GridError, match="_cell_owner"):
        G.assert_tracker_contract(Broken())


def test_tracker_contract_rejects_wrong_types() -> None:
    """映射类型不符时必须报错（避免 numpy 标量导致下游 KeyError）。"""
    class Wrong:
        _cell_owner = {"a": 1}

    with pytest.raises(GridError, match="类型不符"):
        G.assert_tracker_contract(Wrong())


# --------------------------------------------------------------------------- #
# 13. 参数哈希可复现 / 未预注册档拒绝
# --------------------------------------------------------------------------- #
def test_param_hash_is_deterministic_and_key_order_independent() -> None:
    """param_hash 必须与键顺序无关、跨调用稳定。"""
    a = {"x": 1, "y": 2.5, "z": "文本"}
    b = {"z": "文本", "y": 2.5, "x": 1}
    assert G.param_hash(a) == G.param_hash(b)
    assert G.param_hash(a) == G.param_hash(dict(a))
    assert len(G.param_hash(a)) == 64
    assert G.param_hash(a) != G.param_hash({**a, "x": 2})


def test_param_hash_changes_with_grid_and_tiers() -> None:
    """网格不同或档位不同则参数哈希不同（禁止按结果事后挑选同一档）。"""
    spec = G.PRE_REGISTERED_PARAM_SETS["B"]
    p100 = G.resolve_param_set(
        spec, cell_area_m2=10000.0, cellsize_m=100.0, saveint_s=21600.0, n_frames=57)
    p200 = G.resolve_param_set(
        spec, cell_area_m2=40000.0, cellsize_m=200.0, saveint_s=21600.0, n_frames=57)
    assert p100["param_hash"] != p200["param_hash"]
    assert p100["n_cells_shallow_max_area"] == 9, "0.09 km² 在 100 m 上应为 9 格"
    assert p200["n_cells_shallow_max_area"] == 3, "0.09 km² 在 200 m 上应为 3 格"

    hashes = {
        set_id: G.resolve_param_set(
            spec_i, cell_area_m2=10000.0, cellsize_m=100.0,
            saveint_s=21600.0, n_frames=57)["param_hash"]
        for set_id, spec_i in G.PRE_REGISTERED_PARAM_SETS.items()
    }
    assert len(set(hashes.values())) == 3, "三档参数哈希必须互不相同"


def test_resolved_param_set_records_conversion_provenance() -> None:
    """解析后的参数必须同时带物理阈值、像元换算结果与来源/置信度。"""
    resolved = G.resolve_param_set(
        G.PRE_REGISTERED_PARAM_SETS["A"],
        cell_area_m2=10000.0, cellsize_m=100.0, saveint_s=21600.0, n_frames=57,
    )
    assert resolved["cellsize_m"] == 100.0
    assert resolved["cell_area_m2"] == 10000.0
    assert resolved["shallow_max_area_km2"] == 0.04
    assert resolved["n_cells_shallow_max_area"] == 4
    assert resolved["shallow_depth_m"] == 0.15
    assert resolved["persistence_hours"] == 36.0
    assert resolved["persistence_frames"] == 6
    assert resolved["main_stem_min_area_km2"] == 5.0
    assert resolved["deep_depth_threshold_m"] == 0.50
    assert resolved["source"] == "项目候选参数，未经本地率定"
    assert resolved["confidence"] == "E3 候选假设"
    assert len(resolved["param_hash"]) == 64


def test_unregistered_param_set_is_rejected() -> None:
    """未预注册的参数档必须被拒绝（禁止临时新增档位）。"""
    assert set(G.PRE_REGISTERED_PARAM_SETS) == {"A", "B", "C"}
    parser = G.build_parser()
    args = parser.parse_args(["--param-sets", "Z"])
    args.param_sets = [item.strip().upper() for item in args.param_sets.split(",") if item.strip()]
    with pytest.raises(GridError, match="未预注册"):
        G.run(args)


# --------------------------------------------------------------------------- #
# 14. CLI 契约
# --------------------------------------------------------------------------- #
def test_cli_supports_governance_flags() -> None:
    """脚本必须实现统一 CLI 参数。"""
    parser = G.build_parser()
    args = parser.parse_args([
        "--run-id", RUN_ID,
        "--display-run-id", DISPLAY_RUN_ID,
        "--run-tag", "display-qc-v1",
        "--dry-run",
        "--log-json", "logs/display_qc.json",
        "--out-dir", "outputs/display_qc",
    ])
    assert args.run_id == RUN_ID
    assert args.display_run_id == DISPLAY_RUN_ID
    assert args.run_tag == "display-qc-v1"
    assert args.dry_run is True
    assert str(args.log_json).endswith("display_qc.json")
    assert args.out_dir is not None


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    """--dry-run 必须不写派生目录，也不写审计 JSON。"""
    out_root = tmp_path / "display_qc"
    audit_path = tmp_path / "audit.json"
    parser = G.build_parser()
    args = parser.parse_args([
        "--param-sets", "A",
        "--dry-run",
        "--out-root", str(out_root),
        "--audit-path", str(audit_path),
    ])
    args.param_sets = ["A"]
    result = G.run(args)
    assert not out_root.exists(), "--dry-run 不应创建派生目录"
    assert not audit_path.exists(), "--dry-run 不应写审计 JSON"
    assert result["dry_run"] is True
    assert result["param_sets"]["A"]["frames"], "dry-run 仍必须给出逐帧审计"
    assert result["raw_integrity"]["unchanged"] is True


def test_derived_path_includes_event_level() -> None:
    """派生目录必须带事件层级 2024-09-yagi（计划 §3 第 3 条给定路径）。"""
    audit = _audit()
    assert G.EVENT_ID == EVENT
    assert "2024-09-yagi" in str(G.DISPLAY_QC_ROOT)
    for set_id, rec in audit["param_sets"].items():
        assert f"outputs/display_qc/guigang/{EVENT}/{DISPLAY_RUN_ID}/" in rec["derived_dir"], (
            f"{set_id} 派生路径缺事件层级：{rec['derived_dir']}"
        )
