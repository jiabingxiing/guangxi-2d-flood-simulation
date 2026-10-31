"""久隆治理计划的硬约束契约测试（计划 §1.3 / §4 / §7.1）。

本文件把计划里"不得违反"的条目写成可执行断言，防止后续改动回退：

1. 已有 `res.mass` 的 run 目录不得被覆盖（终稿 §10）——`guard_no_overwrite`。
2. 所有物理面积阈值必须按栅格头 `cellsize` 换算，不得写死像元数
   （计划 §1.4 / §3.3 参数冻结规则）。
3. 100 m 与 200 m 单元面积差 4 倍，同一物理面积阈值对应像元数不同
   （计划 §1.4 表格）。
4. 派生层必须带 `not_physical_recalculation=true` 与 `derived_from`
   （计划 §5 Phase 1 验收）。
5. 质量门口径：`FAIL-EXPLAINED` 不计通过（ADR-0006 / 计划 §7.1）。

这些断言只读文件，不修改任何模型输出。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import (  # noqa: E402
    AsciiGrid,
    OverwriteRefused,
    guard_no_overwrite,
    read_ascii_grid,
    read_ascii_header,
)
from scripts.common.patch_analysis import (  # noqa: E402
    connected_components,
    summarize_frame,
)

JIU = PROJ / "outputs" / "jiulong" / "2024-09-yagi"
RECESSION = JIU / "scen_free_200m_sv6h_recession"
RUN_200M = JIU / "scen_free_200m_sv6h"
RUN_100M = JIU / "scen_free_100m_dt2p5_sv6h_realdem"


# --------------------------------------------------------------------------- #
# 1. res.mass 覆盖保护（终稿 §10）
# --------------------------------------------------------------------------- #
def test_guard_refuses_existing_res_mass() -> None:
    """真实 run 目录已有 res.mass，写入前检查必须抛 OverwriteRefused。"""
    assert (RECESSION / "results" / "res.mass").is_file(), "测试前提：该 run 必须有 res.mass"
    with pytest.raises(OverwriteRefused, match="禁止覆盖"):
        guard_no_overwrite(RECESSION)


def test_guard_allows_fresh_directory(tmp_path: Path) -> None:
    """空目录（无 res.mass）允许写入。"""
    target = tmp_path / "new_run"
    target.mkdir()
    guard_no_overwrite(target)  # 不抛异常即通过
    # 建了 res.mass 之后必须拒绝
    (target / "results").mkdir()
    (target / "results" / "res.mass").write_text("Time\n", encoding="utf-8")
    with pytest.raises(OverwriteRefused):
        guard_no_overwrite(target)


def test_derived_dirs_must_not_contain_res_mass() -> None:
    """display_qc 派生层不得含 res.mass。

    `scripts/stage3/g15_mass_recheck.py` 用 `rglob("results/res.mass")` 扫
    `outputs/{jiulong,ningming,guigang}`；派生目录若带 res.mass 会被误计为
    独立 run，污染质量门 PASS/FAIL 计数。
    """
    display_root = PROJ / "outputs" / "display_qc"
    if not display_root.is_dir():
        pytest.skip("display_qc 尚未生成（Phase 1 未完成）")
    offenders = [p for p in display_root.rglob("res.mass")]
    assert not offenders, f"派生目录出现 res.mass：{[str(p) for p in offenders]}"


# --------------------------------------------------------------------------- #
# 2/3. 分辨率换算：物理面积阈值 → 像元数
# --------------------------------------------------------------------------- #
def test_cell_area_read_from_header_not_hardcoded() -> None:
    """单元面积必须来自栅格头，200 m 与 100 m 相差 4 倍。"""
    h200 = read_ascii_header(RUN_200M / "results" / "res.max")
    h100 = read_ascii_header(RUN_100M / "results" / "res.max")
    assert float(h200["cellsize"]) == 200.0
    assert float(h100["cellsize"]) == 100.0
    area200 = float(h200["cellsize"]) ** 2
    area100 = float(h100["cellsize"]) ** 2
    assert area200 == 40000.0
    assert area100 == 10000.0
    assert area200 == 4 * area100, "200 m 单元面积必须是 100 m 的 4 倍"


def test_cells_for_area_scales_with_grid() -> None:
    """同一物理面积阈值在两种网格上换算出的像元数必须成 4 倍关系。"""
    h200 = read_ascii_header(RUN_200M / "results" / "res.max")
    h100 = read_ascii_header(RUN_100M / "results" / "res.max")
    grid200 = AsciiGrid(data=np.zeros((1, 1)), header=h200)
    grid100 = AsciiGrid(data=np.zeros((1, 1)), header=h100)

    # 计划 §1.4：0.04 km² 在 100 m 是 4 格，在 200 m 是 1 格
    assert grid100.cells_for_area(0.04) == 4
    assert grid200.cells_for_area(0.04) == 1
    # 0.36 km² 在 100 m 是 36 格，在 200 m 是 9 格
    assert grid100.cells_for_area(0.36) == 36
    assert grid200.cells_for_area(0.36) == 9
    # 换算单调且至少 1 格
    for area in (0.0, 0.001, 0.01, 0.04, 1.0, 100.0):
        assert grid200.cells_for_area(area) >= 1
        assert grid100.cells_for_area(area) >= grid200.cells_for_area(area)


def test_no_hardcoded_100m_assumption_in_display_qc() -> None:
    """display_qc 脚本不得写死 100 m 单元面积或 4/9 像元阈值。

    检查项：
    - 不得出现 `100.0 * 100.0` / `100*100` 这类字面面积；
    - 不得把 min_patch_cells 默认值定为 4 或 shallow_patch_cells 定为 9
      且同时不提供 cellsize 换算。
    """
    script = PROJ / "scripts" / "stage6" / "clean_jiulong_display.py"
    if not script.is_file():
        pytest.skip("clean_jiulong_display.py 尚未创建（Phase 1 未完成）")
    text = script.read_text(encoding="utf-8")
    for literal in ("100.0 * 100.0", "100*100", "100.0*100.0", "10000.0"):
        assert literal not in text, f"display_qc 脚本出现写死的 100 m 单元面积：{literal}"
    assert "cellsize" in text.lower() or "cell_area" in text.lower(), (
        "display_qc 脚本必须从栅格头读 cellsize / cell_area"
    )


# --------------------------------------------------------------------------- #
# 4. 派生层标记契约
# --------------------------------------------------------------------------- #
REQUIRED_DERIVED_META = (
    "derived_from",
    "not_physical_recalculation",
    "cellsize_m",
    "cell_area_m2",
    "param_set_id",
    "param_hash",
    "generated_utc",
    "caveats",
)


def test_display_qc_meta_marks_derived_layer() -> None:
    """display_qc 每个派生目录的 meta.json 必须声明派生属性。"""
    display_root = PROJ / "outputs" / "display_qc"
    if not display_root.is_dir():
        pytest.skip("display_qc 尚未生成（Phase 1 未完成）")
    metas = sorted(display_root.rglob("meta.json"))
    assert metas, "display_qc 下没有 meta.json"
    for meta_path in metas:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        missing = [k for k in REQUIRED_DERIVED_META if k not in meta]
        assert not missing, f"{meta_path} 缺派生标记字段：{missing}"
        assert meta["not_physical_recalculation"] is True, (
            f"{meta_path} 必须声明 not_physical_recalculation=true"
        )
        assert meta["derived_from"], f"{meta_path} 必须写 derived_from"
        assert float(meta["cellsize_m"]) > 0
        assert float(meta["cell_area_m2"]) == float(meta["cellsize_m"]) ** 2


def test_display_qc_audit_declares_preregistered_params() -> None:
    """审计 JSON 必须登记 A/B/C 三档参数，且带网格换算结果。

    参数既可直接位于档位记录内，也可嵌在 `params` 子对象中（两种结构都接受）。
    """
    audit = PROJ / "data" / "catalog" / "jiulong_display_qc_audit.json"
    if not audit.is_file():
        pytest.skip("display_qc 审计尚未生成（Phase 1 未完成）")
    doc = json.loads(audit.read_text(encoding="utf-8"))
    assert doc.get("not_physical_recalculation") is True
    param_sets = doc.get("param_sets") or doc.get("parameter_sets") or {}
    assert len(param_sets) >= 3, f"必须至少预注册 A/B/C 三档，实得 {list(param_sets)}"
    for name, rec in param_sets.items():
        flat = dict(rec)
        if isinstance(rec.get("params"), dict):
            flat = {**rec["params"], **{k: v for k, v in rec.items() if k != "params"}}
        assert "cell_area_m2" in flat, f"参数档 {name} 缺 cell_area_m2"
        assert "cellsize_m" in flat, f"参数档 {name} 缺 cellsize_m"
        # 单元面积必须与 cellsize 满足平方关系（证明是换算而非写死）
        assert abs(float(flat["cell_area_m2"]) - float(flat["cellsize_m"]) ** 2) < 1e-6, (
            f"参数档 {name} 的 cell_area_m2 与 cellsize_m 不满足平方关系"
        )
        # 每档必须给出按当前网格换算出的像元数
        keys = " ".join(flat.keys()).lower()
        assert "cells" in keys or "n_cell" in keys, f"参数档 {name} 缺像元数换算结果"
        # 每档必须声明参数来源与置信度，防止事后挑档
        assert flat.get("source"), f"参数档 {name} 缺参数来源"
        assert flat.get("confidence"), f"参数档 {name} 缺置信度"


# --------------------------------------------------------------------------- #
# 5. 质量门口径
# --------------------------------------------------------------------------- #
def test_mass_gate_never_reports_na_and_explained_is_not_pass() -> None:
    """FAIL-EXPLAINED 不计通过；不得出现 N/A 伪状态。"""
    doc = json.loads(
        (PROJ / "data" / "catalog" / "mass_recheck_g15.json").read_text(encoding="utf-8")
    )
    for name, rec in doc["runs"].items():
        gate = rec["gate_1e-3"]
        assert gate in ("PASS", "FAIL-EXPLAINED", "FAIL"), f"{name} 非法质量门状态：{gate}"
        if gate != "PASS":
            assert rec.get("excluded_from_pass_count") is True, (
                f"{name} 非 PASS 必须排除出通过计数"
            )
    counts = doc["counts"]
    assert counts["PASS"] + counts["FAIL-EXPLAINED"] + counts["FAIL"] == len(doc["runs"])


# --------------------------------------------------------------------------- #
# 6. 斑块分析口径（计划 §3.3 / §3.4）
# --------------------------------------------------------------------------- #
def test_connected_components_uses_8_neighbourhood() -> None:
    """对角相连必须算同一斑块；4 邻域会切成两块。"""
    mask = np.zeros((3, 3), dtype=bool)
    mask[0, 0] = True
    mask[1, 1] = True
    mask[2, 2] = True
    _labels8, n8 = connected_components(mask, connectivity=8)
    _labels4, n4 = connected_components(mask, connectivity=4)
    assert n8 == 1, "8 邻域下对角相连应为 1 个斑块"
    assert n4 == 3, "4 邻域下应为 3 个独立像元"


def test_patch_area_uses_header_cell_area() -> None:
    """斑块面积必须用传入的单元面积，不是写死常数。"""
    depth = np.zeros((10, 10), dtype=np.float32)
    depth[0:2, 0:2] = 0.5  # 4 格
    summary200 = summarize_frame(depth, 0, wet_threshold_m=0.05, cell_area_m2=40000.0)
    summary100 = summarize_frame(depth, 0, wet_threshold_m=0.05, cell_area_m2=10000.0)
    assert summary200.n_patches == 1
    assert summary200.wet_cells == 4
    # 4 格 × 0.04 km² = 0.16 km²；4 格 × 0.01 km² = 0.04 km²
    assert abs(summary200.wet_area_km2 - 0.16) < 1e-9
    assert abs(summary100.wet_area_km2 - 0.04) < 1e-9
    assert abs(summary200.wet_area_km2 - 4 * summary100.wet_area_km2) < 1e-9


def test_sea_mask_excluded_from_wet_stats() -> None:
    """海域像元必须从湿面积统计中剔除（计划 §3.3 第 1 条）。"""
    depth = np.full((6, 6), 1.0, dtype=np.float32)
    sea = np.zeros((6, 6), dtype=bool)
    sea[0, :] = True  # 首行 6 格为海
    without = summarize_frame(depth, 0, wet_threshold_m=0.05, cell_area_m2=40000.0)
    with_sea = summarize_frame(
        depth, 0, wet_threshold_m=0.05, cell_area_m2=40000.0, sea_mask=sea
    )
    assert without.wet_cells == 36
    assert with_sea.wet_cells == 30
    assert with_sea.wet_area_km2 < without.wet_area_km2


def test_wet_threshold_is_strictly_greater() -> None:
    """`depth > 阈值` 是严格大于；等于阈值不算湿（与 floodviz 口径一致）。"""
    depth = np.full((4, 4), 0.05, dtype=np.float32)
    summary = summarize_frame(depth, 0, wet_threshold_m=0.05, cell_area_m2=40000.0)
    assert summary.wet_cells == 0
    depth[0, 0] = 0.0501
    summary2 = summarize_frame(depth, 0, wet_threshold_m=0.05, cell_area_m2=40000.0)
    assert summary2.wet_cells == 1
