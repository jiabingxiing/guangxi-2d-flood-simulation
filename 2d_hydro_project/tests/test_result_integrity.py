"""结果完整性回归：防止 WSE 干像元、站址远邻域、时刻错配、伪到达/历时回退。"""
from __future__ import annotations

import json
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]


def load(rel: str):
    return json.loads((PROJ / rel).read_text(encoding="utf-8"))


def test_guigang_wse_uses_wet_cells_only():
    v = load("outputs/guigang/2024-09-yagi/scen_yagi_q11500_200m/verify.json")
    assert "wse_gauge_datum" not in v, "旧 +13.08 伪基准字段仍在"
    assert "wse_series_gauge" not in v, "旧伪基准序列仍在"
    assert v["wse_gauge_context"]["diagnostic_only"] is True
    # 09-12T14 湿像元 WSE 约 21.92 m，不是 30.01
    assert abs(v["wse_gauge_context"]["0912T14"]["model_wet_wse_m"] - 21.92) < 0.05
    assert v["wse_gauge_context"]["0912T14"]["diff_gauge_minus_model_m"] > 20.0
    # 两个时点 offset 不一致，证明不是常数基准
    assert abs(v["wse_gauge_context"]["0912T14"]["diff_gauge_minus_model_m"]
               - v["wse_gauge_context"]["0914T05"]["diff_gauge_minus_model_m"]) > 1.0
    for rec in v["wse_series"]:
        assert "wse_center_m" in rec and "wse_wet_max_m" in rec and "wet_cells" in rec


def test_jiulong_station_radius_contract():
    s = load("data/catalog/jiulong_scenarios_summary.json")
    for scen in ("free", "z0", "z05", "z10", "ztide"):
        r = s[scen]
        assert abs(r["anchor_depth_center_m"] - 0.008) < 1e-6
        assert abs(r["anchor_depth_r2_m"] - 0.547) < 1e-3
        assert abs(r["anchor_depth_r5_m"] - 1.684) < 1e-3
        assert abs(r["r5_distance_km"] - 1.28) < 0.01
        assert abs(r["anchor_max_depth_m"] - r["anchor_depth_r2_m"]) < 0.01, "主口径必须为 ±2 格"


def test_ningming_metrics_are_time_matched():
    d = load("data/catalog/ningming_ensemble.json")
    assert d["design"]["obs_frame"] == 13
    assert d["design"]["primary_threshold_m"] == 0.15
    assert "时刻切片" in d["design"]["metric_semantics"]
    for r in d["runs"]:
        assert r["obs_time"] == "2026-08-24T08:00+08:00"
        assert r["frame"] == 13
        assert r["valid_area_km2"] > 0
        assert "station_wd_at_obs_m" in r
        assert r["station_wd_center_m"] < 0.1, "站址中心像元不应被 11.88 m 面积最大值替代"


def test_products_arrival_duration_and_site_contract():
    d = load("outputs/products/products_summary.json")
    n = d["sites"]["ningming"]
    g = d["sites"]["guigang"]
    j = d["sites"]["jiulong"]
    assert n["arrival_method"] == "first snapshot wd>0.05"
    assert g["arrival_method"] == "first snapshot wd>0.05"
    # Phase B4：久隆改用 6h 输出重跑（scen_free_200m_sv6h，verify 与基线逐位一致），
    # 到达时间从“单快照不可算”升级为 6h 分辨率首湿快照
    assert j["arrival_method"] == "first snapshot wd>0.05"
    assert j["run"] == "scen_free_200m_sv6h"
    assert j["arrival_site_h"] is not None and j["arrival_site_h"] >= 0
    for rec in (n, g, j):
        assert rec["duration_method"] == "res.totaltm (hours)"
        assert rec["depth_max_site_cell_m"] <= rec["depth_max_site_r2_m"]
        assert "peak_area_km2" in rec
    assert abs(j["depth_max_site_cell_m"] - 0.008) < 1e-6
    assert abs(j["depth_max_site_r2_m"] - 0.547) < 1e-3


def test_mass_gate_policy_explicit():
    d = load("data/catalog/mass_recheck_g15.json")
    assert d["counts"]["FAIL"] == 0
    for name, r in d["runs"].items():
        assert "N/A" not in r["gate_1e-3"]
        if r["gate_1e-3"] == "FAIL-EXPLAINED":
            assert r["cause"]
    for scen in ("scen_z05_200m", "scen_z10_200m", "scen_ztide_200m"):
        key = next(k for k in d["runs"] if scen in k)
        assert d["runs"][key]["gate_1e-3"] == "FAIL-EXPLAINED"
