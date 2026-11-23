"""质量门单元测试：三态（PASS / FAIL-EXPLAINED / unrecorded）与编码容错。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from floodviz.catalog.quality import find_mass_recheck, load_mass_gate, merge_quality

PASS_RUN = "outputs/jiulong/2024-09-yagi/scen_free_200m_sv6h"
FAIL_EXPLAINED_RUN = "outputs/jiulong/2024-09-yagi/scen_z05_200m"


def _write_mass_file(tmp_path: Path, doc: dict, encoding: str) -> Path:
    parent = tmp_path / "parent"
    path = parent / "data" / "catalog" / "mass_recheck_g15.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(doc, ensure_ascii=False).encode(encoding))
    return parent


# ---------------------------------------------------------------- 真实数据（只读）


def test_find_mass_recheck_real_file(parent_root: Path) -> None:
    path = find_mass_recheck(parent_root)
    assert path.is_file(), f"父项目质量复核文件应存在：{path}"


def test_real_run_gate_is_pass(parent_root: Path) -> None:
    result = load_mass_gate(parent_root, PASS_RUN)
    assert result["mass_gate"] == "PASS"
    assert result["e_mass"] == pytest.approx(0.0004893)
    assert result["cause"] is None
    assert result["excluded_from_pass_count"] is False
    assert Path(result["source"]).is_file()


def test_real_overall_counts(parent_root: Path) -> None:
    """质量复核表通过数随真实 DEM run 注册更新。"""
    result = load_mass_gate(parent_root, PASS_RUN)
    assert result["counts"]["FAIL"] == 0
    assert result["counts"]["FAIL-EXPLAINED"] == 4  # r4 感潮 HVAR 入表（2026-11-22，g15 政策 FE）
    assert result["counts"]["PASS"] >= 36


def test_real_fail_explained_is_not_pass(parent_root: Path) -> None:
    """FAIL-EXPLAINED 是解释性失败，不计入通过。"""
    result = load_mass_gate(parent_root, FAIL_EXPLAINED_RUN)
    assert result["mass_gate"] == "FAIL-EXPLAINED"
    assert result["excluded_from_pass_count"] is True
    assert result["cause"]


def test_real_unknown_run_is_unrecorded(parent_root: Path) -> None:
    result = load_mass_gate(parent_root, "outputs/nowhere/2099-01-none/scen_x")
    assert result["mass_gate"] == "unrecorded"
    assert result["e_mass"] is None
    assert "未进入质量复核表" in result["cause"]


def test_path_normalization(parent_root: Path) -> None:
    """查询路径容忍反斜杠、首尾斜杠与大小写（Windows 迁移）。"""
    variants = [
        PASS_RUN.replace("/", "\\"),
        "/" + PASS_RUN + "/",
        "OUTPUTS/JIULONG/2024-09-YAGI/SCEN_FREE_200M_SV6H",
    ]
    for variant in variants:
        assert load_mass_gate(parent_root, variant)["mass_gate"] == "PASS", variant


# ---------------------------------------------------------------- 缺失与容错


def test_missing_catalog_file_is_unrecorded(tmp_path: Path) -> None:
    result = load_mass_gate(tmp_path / "empty_parent", PASS_RUN)
    assert result["mass_gate"] == "unrecorded"
    assert result["source"] is None
    assert "质量复核文件不存在" in result["cause"]


def test_gbk_encoded_file_is_tolerated(tmp_path: Path) -> None:
    doc = {
        "counts": {"PASS": 1, "FAIL-EXPLAINED": 1, "FAIL": 0},
        "runs": {
            "outputs/ningming/2026-08-zitan/scen_q6000_n100_100m": {
                "e_mass_132": 0.0001234,
                "gate_1e-3": "FAIL-EXPLAINED",
                "cause": "边界通量未完全闭合（GBK 中文原因）",
                "excluded_from_pass_count": True,
            }
        },
    }
    parent = _write_mass_file(tmp_path, doc, "gbk")
    raw = (parent / "data" / "catalog" / "mass_recheck_g15.json").read_bytes()
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")  # 确认文件确实不是 UTF-8
    result = load_mass_gate(parent, "outputs/ningming/2026-08-zitan/scen_q6000_n100_100m")
    assert result["mass_gate"] == "FAIL-EXPLAINED"
    assert result["cause"] == "边界通量未完全闭合（GBK 中文原因）"
    assert result["e_mass"] == pytest.approx(0.0001234)


def test_utf8_bom_encoded_file_is_tolerated(tmp_path: Path) -> None:
    doc = {"runs": {"outputs/x/run_bom": {"gate_1e-3": "PASS", "e_mass_132": 0.001}}}
    parent = _write_mass_file(tmp_path, doc, "utf-8-sig")
    assert load_mass_gate(parent, "outputs/x/run_bom")["mass_gate"] == "PASS"


def test_illegal_gate_value_becomes_unrecorded(tmp_path: Path) -> None:
    doc = {"runs": {"outputs/x/run_bad": {"gate_1e-3": "PASSED", "e_mass_132": 0.001}}}
    parent = _write_mass_file(tmp_path, doc, "utf-8")
    result = load_mass_gate(parent, "outputs/x/run_bad")
    assert result["mass_gate"] == "unrecorded"


def test_record_without_gate_key_is_unrecorded(tmp_path: Path) -> None:
    doc = {"runs": {"outputs/x/run_nogate": {"e_mass_132": 0.002}}}
    parent = _write_mass_file(tmp_path, doc, "utf-8")
    assert load_mass_gate(parent, "outputs/x/run_nogate")["mass_gate"] == "unrecorded"


def test_run_record_without_runs_key(tmp_path: Path) -> None:
    parent = _write_mass_file(tmp_path, {"counts": {"PASS": 0}}, "utf-8")
    assert load_mass_gate(parent, "outputs/x/run_y")["mass_gate"] == "unrecorded"


# ---------------------------------------------------------------- 合并


def test_merge_quality_overrides_optimistic_config() -> None:
    """配置里写 PASS 也不能盖住实测 unrecorded（UI 不得显示 PASS）。"""
    case_quality = {
        "mass_gate": "PASS",
        "e_mass": 0.0004893,
        "validation_status": "降级模式情景级",
        "vertical_datum_status": "未闭合",
        "warnings": ["情景级成果"],
    }
    merged = merge_quality(
        case_quality,
        {"mass_gate": "unrecorded", "e_mass": None, "cause": "该 run 未进入质量复核表（不得显示 PASS）"},
    )
    assert merged["mass_gate"] == "unrecorded"
    assert merged["e_mass"] is None
    assert merged["mass_gate_cause"].startswith("该 run 未进入质量复核表")
    assert merged["validation_status"] == "降级模式情景级"
    assert merged["warnings"] == ["情景级成果"]
    # 不修改入参
    assert case_quality["mass_gate"] == "PASS"


def test_merge_quality_keeps_case_fields_and_counts(parent_root: Path) -> None:
    merged = merge_quality({"validation_status": "x"}, load_mass_gate(parent_root, PASS_RUN))
    assert merged["mass_gate"] == "PASS"
    assert merged["e_mass"] == pytest.approx(0.0004893)
    assert merged["mass_counts_overall"]["FAIL"] == 0
    assert merged["mass_counts_overall"]["FAIL-EXPLAINED"] == 4  # r4 感潮 HVAR 入表（2026-11-22）
    assert merged["mass_counts_overall"]["PASS"] >= 36
    assert merged["validation_status"] == "x"
