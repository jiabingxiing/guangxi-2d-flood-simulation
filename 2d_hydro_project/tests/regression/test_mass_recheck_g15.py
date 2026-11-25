"""M4 回归测试：确保新增模块与产物不破坏既有 66 PASS / 4 FAIL-EXPLAINED 质量门 SSOT。"""
from __future__ import annotations

import json

from hydro_refine.core.paths import PROJ
from scripts.stage3 import g15_mass_recheck


def test_mass_recheck_ssot_remains_closed() -> None:
    g15_path = PROJ / "data" / "catalog" / "mass_recheck_g15.json"
    assert g15_path.is_file()
    doc = json.loads(g15_path.read_text(encoding="utf-8"))
    counts = doc["counts"]
    assert counts["FAIL"] == 0
    assert counts["PASS"] == 66
    assert counts["FAIL-EXPLAINED"] == 4


def test_pipenet_runs_are_outside_g15_scan_roots() -> None:
    """隔离机制断言：`outputs/pipenet` 不在 g15 的扫描根白名单内。

    这是「新增耦合算例不污染 66-4-0」的**真实机制**。
    早先版本断言 `outputs/pipenet` 下不存在任何 `res.mass`；但 ADR-0024 的
    物理重跑算例（`run_coupled_*`）按设计就带 `res.mass`，该断言会被本计划的
    正确产物触发。改为锁机制而非锁目录内容。
    """
    pipenet_root = (PROJ / "outputs" / "pipenet").resolve()
    for root in g15_mass_recheck.CASE_ROOTS:
        assert not pipenet_root.is_relative_to(root.resolve()), (
            f"{pipenet_root} 落在 g15 扫描根 {root} 内，会污染质量门计数"
        )


def test_g15_case_roots_are_explicit_whitelist() -> None:
    """g15 用显式白名单三案例根，不得改成递归扫描 outputs/。"""
    roots = [p.resolve() for p in g15_mass_recheck.CASE_ROOTS]
    assert len(roots) == 3, f"CASE_ROOTS 应为三案例白名单，实得 {roots}"
    names = sorted(p.name for p in roots)
    assert names == ["guigang", "jiulong", "ningming"], f"意外的扫描根：{names}"
    outputs_root = (PROJ / "outputs").resolve()
    assert outputs_root not in roots, "不得把 outputs/ 本身作为扫描根"


def test_pipenet_coupled_runs_carry_isolation_metadata() -> None:
    """产物自证隔离：耦合/基线算例的 meta.json 必须带隔离标记。"""
    pipenet_root = PROJ / "outputs" / "pipenet"
    if not pipenet_root.is_dir():
        return
    run_dirs = [
        p.parent.parent
        for p in pipenet_root.glob("*/run_*/results/res.mass")
    ]
    if not run_dirs:
        return
    for run_dir in run_dirs:
        meta_path = run_dir / "meta.json"
        assert meta_path.is_file(), f"{run_dir} 缺 meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta.get("isolated_from_g15_ssot") is True, f"{run_dir} 未声明隔离"
        assert meta.get("SYNTHETIC_DATA_NOT_REAL_ASSET") is True, (
            f"{run_dir} 未声明虚拟资产（ADR-0024 第 4 条）"
        )
        assert meta.get("physical_recalculation") is True, (
            f"{run_dir} 是物理重跑，不得标成诊断投影"
        )