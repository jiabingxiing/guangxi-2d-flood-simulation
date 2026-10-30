"""Phase B 回归：B8 结构连通性物理判据、B0 空间 Manning 生效性、B3 入渗账目、B1 敏感性表完整性。

原则：产物缺失时 skip（不伪造通过）；产物存在时必须满足物理/口径判据。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

PROJ = Path(__file__).resolve().parents[1]


def load(rel: str):
    p = PROJ / rel
    if not p.exists():
        pytest.skip(f"缺产物 {rel}（未运行对应 Phase）")
    return json.loads(p.read_text(encoding="utf-8"))


def test_b8_levee_connectivity_physics():
    """封闭堤防不得越堤；开口/堰必须过水但仍少于无结构。"""
    d = load("outputs/bench/B8_levee/summary.json")["B8a"]
    assert d["status"] == "PASS", f"B8a 未通过：{d['checks']}"
    r = d["ratios"]
    assert r["wall_over_open"] == 0.0, "全封闭堤防仍越堤"
    assert 0.0 < r["notch_over_open"] < 1.0
    assert 0.0 < r["weir_over_open"] < 1.0
    for mode, rec in d["runs"].items():
        assert rec["exit"] == 0, f"{mode} 求解器非零退出"
        assert rec["e_mass"] is not None and rec["e_mass"] < 1e-3, f"{mode} E_mass 超门"
    assert d["runs"]["wall"]["first_wet_west_h"] is None, "封闭堤防下游出现湿单元"
    assert d["runs"]["wall"]["v_out_integrated_m3"] == 0.0, "封闭堤防出现出口流量"


def test_b8_t016c_reports_build_difference():
    """8.1.0 与仓库 5.7.0 参考的桥洞结果必须如实记录差异，不得宣称一致。"""
    d = load("outputs/bench/B8_levee/summary.json")["B8b"]
    assert d["status"] in ("PASS", "DIFFERS_FROM_REFERENCE")
    assert d["max_abs_diff_m"] >= 0
    if d["status"] == "DIFFERS_FROM_REFERENCE":
        assert "未获独立验证" in d["gate"]
        assert d["orifice_downstream_wet"]["v81"] != d["orifice_downstream_wet"]["v57"], \
            "两组下游干湿一致时不应标为差异"


def test_benchmarks_b8_not_not_run():
    d = load("data/catalog/benchmarks.json")
    assert "B8" in d
    assert d["B8"]["status"] != "NOT_RUN", "B8 仍标未跑"
    assert d["B8"]["B8a_status"] == "PASS"
    assert d["B8"]["evidence"]


def test_manningfile_mechanism_effective():
    """均匀 manning.asc 必须与 fpfric 同值等价；改 n 必须改变结果。"""
    d = load("data/catalog/manning_sensitivity.json")["smoke_equivalence"]
    c = d["checks"]
    assert c["status"] == "PASS"
    assert c["uniform_manning_equals_fpfric"], "manningfile 未被真正使用"
    assert c["max_abs_diff_uniform_vs_fpfric_m"] < 1e-9
    assert c["changed_n_changes_result"]


def test_manning_spatial_run_recorded():
    d = load("data/catalog/manning_sensitivity.json")["comparison"]
    gs = d["design"]["spatial"]["grid_stats"]
    assert gs and gs["uncovered_cells_fallback_fpfric"] >= 0
    assert 0.02 <= gs["n_mean"] <= 0.15
    prim = d["primary_threshold_0p15"]
    assert set(prim["delta"]) == {"csi", "pod", "far", "area_sim_km2"}


def test_karst_accounting_smoke():
    d = load("data/catalog/karst_scenarios.json")
    chk = d["smoke"]["checks"]
    assert chk["status"] == "PASS", f"入渗账目未通过：{chk}"
    assert abs(chk["depth_with_inf_mm"] - chk["expected_with_inf_mm"]) < 2.0
    assert abs(chk["inf_loss_equivalent_mm"] - 18.0) < 2.0
    assert "分布式不生效" in d["source_code_check"]["infilfile"]


def test_karst_scenarios_bounded():
    d = load("data/catalog/karst_scenarios.json")["scenarios"]
    for tag in ("inf1e-6", "inf5e-6"):
        rec = d[tag]
        if "thresholds" not in rec:
            pytest.skip(f"{tag} 未跑完")
        assert rec["infiltration_m_s"] > 0
        t = rec["thresholds"]["0.15"]
        assert 0 <= t["csi"] <= 1 and 0 <= t["far"] <= 1
        assert rec["e_mass_132"] < 1e-3


def test_sensitivity_table_consistency():
    d = load("data/catalog/sensitivity.json")
    assert d["cases"], "敏感性表为空"
    for case, blk in d["cases"].items():
        assert blk["runs"], f"{case} 无任何 run"
        for name, rec in blk["runs"].items():
            assert rec["res_m"] > 0 and rec["dt_s"] > 0
            assert rec["e_mass"] < 1e-3, f"{name} E_mass 超门"
            assert rec["wet_area_km2"] > 0
            assert rec["station_depth_center_m"] <= rec["station_depth_r200m_m"] + 1e-9


def test_new_runs_carry_provenance():
    """新增 runs 的 meta 必须记录空间糙率/入渗开关，避免口径不可追溯。"""
    base = PROJ / "outputs" / "ningming" / "2026-08-zitan"
    nsp = base / "scen_q6000_n100_100m_nsp_b0" / "meta.json"
    if not nsp.exists():
        pytest.skip("空间 Manning run 未跑")
    meta = json.loads(nsp.read_text(encoding="utf-8"))
    assert meta["manning_spatial"]["n_mean"] > 0
    assert "manningfile" in (base / "scen_q6000_n100_100m_nsp_b0" / "run.par").read_text()
    for tag in ("inf1e-6", "inf5e-6"):
        p = base / f"scen_q6000_n100_100m_{tag}_karst" / "meta.json"
        if p.exists():
            m = json.loads(p.read_text(encoding="utf-8"))
            assert m["infiltration_m_s"] > 0
            assert m["infiltration_note"], "入渗情景必须标注为代理口径"
