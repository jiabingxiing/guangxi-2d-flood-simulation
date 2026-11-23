"""久隆双驱动对照诊断脚本的合成单测（零真实 run IO，零 exe）。

覆盖（计划 §8.1 R0–R4 诊断链）：
- factor_diff / build_factor_table：假 verify 指标 dict 的因子差分数学；
- dilate1：8 邻域 1 格膨胀（numpy 平移实现，无 scipy）；
- csi_pod_far：2×2 手算已知掩膜的 CSI/POD/FAR；
- recession_tail_delta_km2 / direction_of / same_direction：退水方向符号逻辑；
- wet_bfs_max_distance / trapz_clip / parse_qvar_bdy：耦合审计辅助（合成数据）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ))

from scripts.stage6 import audit_jiulong_coupling as AJ  # noqa: E402
from scripts.stage6 import compare_jiulong_scenarios as CJ  # noqa: E402
from scripts.stage6 import validate_jiulong_inundation as VJ  # noqa: E402


# --------------------------------------------------------------------------- #
# 因子差分（假 verify 指标 dict）
# --------------------------------------------------------------------------- #
def _mk(key: str, wet: float, patches: int, qin: float | None) -> dict:
    return {"run": key, "status": "ok", "wet_area_km2": wet,
            "n_patches": patches,
            "v_in_integrated_m3": qin}


def test_factor_diff_basic():
    a = _mk("r3", 120.0, 7, 3_000_000.0)
    b = _mk("r1", 80.0, 3, 0.0)
    d = CJ.factor_diff(a, b)
    assert d["delta_wet_area_km2"] == pytest.approx(40.0)
    assert d["delta_n_patches"] == 4
    assert d["delta_v_in_integrated_m3"] == pytest.approx(3_000_000.0)


def test_factor_diff_missing_sides_give_none():
    full = _mk("r2", 10.0, 2, 1.0)
    partial = {"run": "r0", "status": "ok", "wet_area_km2": 5.0}  # 旧 schema 缺 ∫Qin/斑块
    d = CJ.factor_diff(full, partial)
    assert d["delta_wet_area_km2"] == pytest.approx(5.0)
    assert d["delta_n_patches"] is None
    assert d["delta_v_in_integrated_m3"] is None
    assert CJ.factor_diff(None, full) == {
        "delta_wet_area_km2": None, "delta_n_patches": None,
        "delta_v_in_integrated_m3": None}


def test_build_factor_table_with_missing_runs():
    metrics = {
        "r0": _mk("r0", 100.0, 10, 0.0),
        "r1": _mk("r1", 60.0, 4, 0.0),
        # r2a/r2 缺失 → 对应行 available=False、值 None
        "r3": _mk("r3", 150.0, 8, 2_000_000.0),
        "r4": {"run": "r4", "status": "missing"},
    }
    rows = {row["pair"]: row for row in CJ.build_factor_table(metrics)}
    assert rows["r1_vs_r0"]["delta_wet_area_km2"] == pytest.approx(-40.0)
    assert rows["r3_vs_r1"]["delta_wet_area_km2"] == pytest.approx(90.0)
    assert rows["r3_vs_r1"]["delta_v_in_integrated_m3"] == pytest.approx(2_000_000.0)
    assert rows["r2_vs_r2a"]["available"] is False
    assert rows["r2_vs_r2a"]["delta_wet_area_km2"] is None
    assert rows["r4_vs_r0"]["available"] is False
    # 归因对齐全集
    assert {"r3_vs_r1", "r3_vs_r2", "r2_vs_r2a", "r3_vs_r0"} <= set(rows)


# --------------------------------------------------------------------------- #
# 河邻 1 格膨胀
# --------------------------------------------------------------------------- #
def test_dilate1_single_center_pixel():
    m = np.zeros((5, 5), dtype=bool)
    m[2, 2] = True
    d = CJ.dilate1(m)
    assert d.sum() == 9                     # 3×3 邻域
    assert d[1:4, 1:4].all()
    assert not d[0, 0] and not d[0, 4] and not d[4, 0] and not d[4, 4]


def test_dilate1_edges_no_wraparound():
    m = np.zeros((3, 4), dtype=bool)
    m[0, 0] = True
    m[2, 3] = True
    d = CJ.dilate1(m)
    assert d.sum() == 8                     # 两个角格各扩 3 格 + 自身，不回绕
    assert d[0, 1] and d[1, 0] and d[1, 1]
    assert d[1, 2] and d[1, 3] and d[2, 2]


def test_river_adjacent_fraction():
    channel = np.zeros((4, 4), dtype=bool)
    channel[:, 1] = True
    dil = CJ.dilate1(channel)
    wet = np.zeros((4, 4), dtype=bool)
    wet[:, 1] = True                        # 一半湿格在河道上
    wet[0, 3] = True                        # 一个离河湿格
    frac = float((wet & dil).sum()) / int(wet.sum())
    assert frac == pytest.approx(4 / 5)


# --------------------------------------------------------------------------- #
# CSI/POD/FAR（2×2 手算）
# --------------------------------------------------------------------------- #
VALID_ALL = np.ones((2, 2), dtype=bool)


def test_csi_pod_far_2x2_hand_computed():
    model = np.array([[True, True], [False, False]])
    obs = np.array([[True, False], [True, False]])
    r = VJ.csi_pod_far(model, obs, VALID_ALL)
    # H=(0,0) FA=(0,1) M=(1,0) → CSI=1/3, POD=1/2, FAR=1/2（脚本 round 4 位）
    assert (r["hits"], r["false_alarm_cells"], r["misses"]) == (1, 1, 1)
    assert r["csi"] == pytest.approx(1 / 3, abs=1e-3)
    assert r["pod"] == pytest.approx(0.5, abs=1e-3)
    assert r["far"] == pytest.approx(0.5, abs=1e-3)


def test_csi_pod_far_perfect_and_empty():
    wet = np.array([[True, False], [False, False]])
    obs = wet.copy()
    r = VJ.csi_pod_far(wet, obs, VALID_ALL)
    assert (r["csi"], r["pod"], r["far"]) == (1.0, 1.0, 0.0)
    # 模型与观测完全错开 → CSI=0
    r0 = VJ.csi_pod_far(np.array([[True, False], [False, False]]),
                        np.array([[False, False], [False, True]]), VALID_ALL)
    assert r0["csi"] == 0.0 and r0["pod"] == 0.0 and r0["far"] == 1.0


def test_csi_pod_far_footprint_excludes_false_alarm():
    model = np.array([[True, True], [False, False]])
    obs = np.array([[True, False], [False, False]])
    valid = np.array([[True, False], [False, False]])  # (0,1) 无观测 → 不计虚警
    r = VJ.csi_pod_far(model, obs, valid)
    assert r["false_alarm_cells"] == 0
    assert r["far"] == 0.0 and r["pod"] == 1.0 and r["csi"] == 1.0


def test_csi_pod_far_no_observation_returns_none():
    valid = np.zeros((2, 2), dtype=bool)
    r = VJ.csi_pod_far(np.ones((2, 2), bool), np.zeros((2, 2), bool), valid)
    assert r["csi"] is None and r["pod"] is None and r["far"] is None


# --------------------------------------------------------------------------- #
# 退水方向符号逻辑
# --------------------------------------------------------------------------- #
def test_recession_tail_delta_and_direction():
    dec = [{"seq": i, "wet_area_km2": v} for i, v in enumerate((12.0, 10.0, 8.0), start=1)]
    assert CJ.recession_tail_delta_km2(dec) == pytest.approx(-4.0)   # 8 − 12
    assert CJ.direction_of(-4.0) == "receding"
    assert CJ.direction_of(0.5) == "expanding"
    assert CJ.direction_of(0.0) == "flat"
    assert CJ.direction_of(None) is None
    assert CJ.recession_tail_delta_km2(dec[:2]) is None              # 不足 3 帧
    assert CJ.recession_tail_delta_km2([]) is None


def test_same_direction_matches_s1_reference():
    assert VJ.same_direction(-4.0, CJ.S1_RECESSION_REFERENCE_KM2) is True
    assert VJ.same_direction(+2.0, CJ.S1_RECESSION_REFERENCE_KM2) is False
    assert VJ.same_direction(None, -5.84) is None
    assert VJ.same_direction(0.0, 0.0) is True


# --------------------------------------------------------------------------- #
# 耦合审计辅助（合成数据）
# --------------------------------------------------------------------------- #
def test_wet_bfs_max_distance_diagonal_and_blocked():
    wet = np.array([[True, False, False],
                    [False, True, False],
                    [False, False, True]], dtype=bool)
    dist, max_d, reached = AJ.wet_bfs_max_distance(wet, (0, 0))
    assert reached and max_d == 2            # 8 邻域对角逐步传播
    assert dist[2, 2] == 2 and dist[0, 0] == 0
    # 断开的湿地（隔 2 列，8 邻域不可达）：不计入
    wet2 = np.array([[True, False, False],
                     [False, False, True]], dtype=bool)
    _, max_d2, reached2 = AJ.wet_bfs_max_distance(wet2, (0, 0))
    assert reached2 and max_d2 == 0
    # 入口未湿
    dry = np.array([[False, True], [True, True]], dtype=bool)
    dist3, max_d3, reached3 = AJ.wet_bfs_max_distance(dry, (0, 0))
    assert not reached3 and max_d3 == 0 and dist3.sum() == -4  # 全 -1


def test_trapz_clip_linear():
    t = np.array([0.0, 3600.0, 7200.0])
    q = np.array([1.0, 1.0, 1.0])            # m³/s 常数
    assert AJ.trapz_clip(t, q) == pytest.approx(7200.0)
    assert AJ.trapz_clip(t, q, t_max=7200.0) == pytest.approx(7200.0)
    # 截断到 5400 s：整段 3600 + 半段 1800
    assert AJ.trapz_clip(t, q, t_max=5400.0) == pytest.approx(5400.0)
    # 单点序列积分为 0
    assert AJ.trapz_clip(np.array([0.0]), np.array([1.0])) == 0.0


def test_parse_qvar_bdy_raw_values(tmp_path: Path):
    lines = ["# synthetic", "test", "3 seconds",
             "0.5\t0", "1.0\t3600", "0.25\t7200"]
    p = tmp_path / "synthetic.bdy"
    p.write_text("\n".join(lines) + "\n", encoding="ascii")
    times, vals = AJ.parse_qvar_bdy(p)
    assert times.tolist() == [0.0, 3600.0, 7200.0]
    assert vals.tolist() == [0.5, 1.0, 0.25]     # 文件原值（Q_m3s/cellsize）
    # 源码口径：Q = 值 × cellsize × n_edges（boundary.cpp QVAR5 每边 ×dx）
    q = vals * 200.0 * 3
    expected = (0.5 + 1.0) / 2 * 3600 * 600 + (1.0 + 0.25) / 2 * 3600 * 600
    assert AJ.trapz_clip(times, q) == pytest.approx(expected)


def test_qvar_n_edges_from_bci(tmp_path: Path):
    p = tmp_path / "bci.bci"
    p.write_text("N\t100.0\t700.0\tQVAR\tq\nS\t-99999999\t99999999\tFREE 0.0005\n",
                 encoding="ascii")
    # input.cpp：BCi1=int((start−xll)/dx)=0，BCi2=int((finish−xll)/dx)−1=2 → 3 条边
    assert AJ.qvar_n_edges(p, xll=100.0, cellsize_m=200.0) == 3
    assert AJ.qvar_n_edges(tmp_path / "missing.bci", 0.0, 200.0) is None
