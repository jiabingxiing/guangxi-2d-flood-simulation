"""GR4J 产汇流 / IHC 映射 / 时分配 / bdy 格式单测（计划 T06，ADR-0016 口径）。

覆盖：单位线和恒 1 与已知序组、1 日生产库公式值、长序列水量守恒（含 UH 排空）、
日→时等分守恒、mm/d→m³/s 换算、bdy 文件格式回读（<点数> <单位> + 值/时间），
以及 tmp 锚点 run 的 run 组装（复制/替换/守卫拒绝）。
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
    AuditLog,
    GridError,
    OverwriteRefused,
    AsciiGrid,
    write_ascii_grid,
)
from scripts.stage3.build_inflow_hydrology import (  # noqa: E402
    daily_to_hourly,
    mm_per_day_to_m3s,
    prepare_run,
    run_gr4j,
    gr4j_balance_residual,
    unit_hydrographs,
    write_bdy,
    basin_points,
    hvar_series,
    edge_bed_reference,
    PARAMS_BASE,
)

PARAMS = dict(PARAMS_BASE)


def test_unit_hydrographs_sum_and_ordinates():
    uh1, uh2 = unit_hydrographs(1.7)
    assert uh1.sum() == pytest.approx(1.0, abs=1e-12)
    assert uh2.sum() == pytest.approx(1.0, abs=1e-12)
    assert uh1.shape == (2,) and uh2.shape == (4,)  # ceil(1.7)=2, ceil(3.4)=4
    assert uh1[0] == pytest.approx(0.265386, abs=1e-5)
    assert uh2[0] == pytest.approx(0.132693, abs=1e-5)
    assert uh2[-1] == pytest.approx(1 - 0.132693 - 0.559579 - 0.294301, abs=1e-3)
    # X4=2（整数）：SH2 在 t=2X4 处恰为 1
    uh1b, uh2b = unit_hydrographs(2.0)
    assert uh1b.shape == (2,) and uh2b.shape == (4,)
    assert uh2b.sum() == pytest.approx(1.0, abs=1e-12)
    with pytest.raises(ValueError):
        unit_hydrographs(0.3)


def test_gr4j_one_day_production():
    """X1=350、S0=0.5X1、P=10、E=2 → 手算独立值（渗漏乘数为 S 非 X1）。"""
    p = np.array([10.0, 0.0])
    e = np.array([2.0, 0.0])
    st = run_gr4j(p, e, **PARAMS)
    s1 = 0.5 * 350.0
    sr = s1 / 350.0
    th = np.tanh(8.0 / 350.0)
    ps_expect = 350.0 * (1 - sr * sr) * th / (1 + sr * th)
    s_after = s1 + ps_expect
    perc_expect = s_after * (1 - (1 + (4 / 9 * s_after / 350.0) ** 4) ** (-0.25))
    assert st["pr_mm"][0] == pytest.approx(perc_expect + 8.0 - ps_expect, abs=1e-9)
    assert st["s_mm"][0] == pytest.approx(s_after - perc_expect, abs=1e-9)


def test_gr4j_water_balance_with_drain_tail():
    """尾部补零雨日使 UH 排空：ΣQ+ΔS+ΔR+在途 = Σ(Pn−Es)（X2=0 严格守恒）。"""
    rng = np.random.default_rng(42)
    p = rng.uniform(0, 30, 60)
    e = rng.uniform(0, 4, 60)
    p[48:] = 0.0  # 尾部 12 天零雨，UH（基长 ≤ 4 天）完全排空
    st = run_gr4j(p, e, **PARAMS)
    res = gr4j_balance_residual(p, e, st, 0.5 * PARAMS["x1"], 0.0)
    assert res == pytest.approx(0.0, abs=1e-9)
    assert (st["q_mm"] >= 0).all()
    assert (st["s_mm"] >= 0).all() and (st["r_mm"] >= 0).all()
    # ET 受库供水限制时 ≤ 需水；Pn−Es ≤ P−E 的日净输入
    assert (st["et_mm"] <= np.maximum(0.0, e - p) + 1e-12).all()


def test_gr4j_zero_input_decay():
    """无雨无 ET：渗漏驱动库排空 → Q 先升后单调不增（R 峰值后）。"""
    n = 60
    st = run_gr4j(np.zeros(n), np.zeros(n), **PARAMS)
    q = st["q_mm"]
    peak = int(np.argmax(q))
    assert (np.diff(q[peak:]) <= 1e-12).all()
    assert q[-1] < q[peak]


def test_daily_to_hourly_conservation():
    q = np.array([10.0, 0.0, 5.5])
    hourly = daily_to_hourly(q)
    assert hourly.size == 72
    assert hourly.sum() == pytest.approx(15.5, abs=1e-12)
    for d in range(3):
        assert hourly[d * 24:(d + 1) * 24].sum() == pytest.approx(q[d], abs=1e-12)
    truncated = daily_to_hourly(q, sim_seconds=3600 * 36)
    assert truncated.size == 36
    assert truncated.sum() == pytest.approx(10.0 + 0.0, abs=1e-12)  # 覆盖第 1、2 日


def test_mm_to_m3s():
    """1 mm/d × 100 km² = 100000 m³/d = 1.157407 m³/s。"""
    out = mm_per_day_to_m3s(np.array([1.0]), 100.0)
    assert out[0] == pytest.approx(1000.0 * 100.0 / 86400.0, rel=1e-12)


def test_write_bdy_roundtrip(tmp_path):
    q = np.array([100.0, 500.0, 100.0])
    t = np.array([0.0, 3600.0, 7200.0])
    p = write_bdy(tmp_path / "inflow_test.bdy", "yujiang", q, t,
                  value_divisor=100.0, header_comment="test")
    lines = p.read_text(encoding="ascii").splitlines()
    assert lines[0] == "# test"
    assert lines[1] == "yujiang"
    assert lines[2] == "3 seconds"
    v1, t1 = lines[3].split("\t")
    assert float(v1) == pytest.approx(1.0, abs=1e-4)  # 100/100
    assert float(t1) == 0.0
    v2, t2 = lines[4].split("\t")
    assert float(v2) == pytest.approx(5.0, abs=1e-4) and float(t2) == 3600.0
    # 时间非递增拒绝
    with pytest.raises(GridError):
        write_bdy(tmp_path / "bad.bdy", "yujiang", q, np.array([0.0, 0.0, 3600.0]))


def test_hvar_series():
    v, t = hvar_series(30.0, 4.0, 1209600.0)
    assert v[0] == pytest.approx(34.0) and v[1] == pytest.approx(34.0)
    assert t[0] == 0.0 and t[1] == 1209600.0


def test_basin_points_dedup():
    basins = [
        {"name": "a", "lon": (107.0, 108.0), "lat": (22.0, 22.5), "step": 0.5},
        {"name": "b", "lon": (107.5, 108.0), "lat": (22.0, 22.5), "step": 0.5},
    ]
    pts = basin_points(basins)
    keys = {(p["lon"], p["lat"]) for p in pts}
    assert len(keys) == len(pts)
    assert len(pts) == 6  # a: 3 经度×2 纬度；b 的格点全被 a 去重


def _make_anchor(tmp_path: Path) -> Path:
    anchor = tmp_path / "anchor_run"
    res = anchor / "results"
    res.mkdir(parents=True)
    hdr = {"ncols": 3, "nrows": 3, "xllcorner": 357000.0, "yllcorner": 2542000.0,
           "cellsize": 100.0, "nodata_value": -9999.0}
    dem = np.array([[10, 10, 8], [10, 9, 7], [10, 10, 6]], dtype=float)
    write_ascii_grid(anchor / "dem.asc", AsciiGrid(dem.astype(np.float32), dict(hdr)))
    (anchor / "bci.bci").write_text(
        "P\t357100\t2542100\tQVAR\tyujiang\nE\t-99999999\t99999999\tFREE\t0.00015\n",
        encoding="ascii")
    (anchor / "manning.map").write_text("0.035\n", encoding="ascii")
    (anchor / "rain.rain").write_text("# t\n1 seconds\n1.0\t0\n", encoding="ascii")
    (anchor / "run.par").write_text(
        "DEMfile\tdem.asc\nbcifile\tbci.bci\nbdyfile\tyujiang.bdy\nsim_time\t7200\n",
        encoding="ascii")
    (anchor / "yujiang.bdy").write_text(
        "yujiang\n2 seconds\n10.0000\t0\n115.0000\t7200\n", encoding="ascii")
    (res / "res.mass").write_text("Time Vol\n0 0\n", encoding="ascii")
    return anchor


def test_prepare_run_assembly(tmp_path):
    anchor = _make_anchor(tmp_path)
    out = tmp_path / "new_run"
    log = AuditLog("test", dry_run=False)
    bdy = write_bdy(tmp_path / "inflow_guigang.bdy", "yujiang",
                    np.array([1.0, 2.0]), np.array([0.0, 7200.0]), value_divisor=100.0)
    run = prepare_run(anchor, out, bdy_files={"inflow": bdy},
                      run_par_edits={"bdyfile": bdy.name}, bci_edits=None,
                      meta_extra={"gr4j": {"peak": 200.0}}, log=log)
    assert (run / "dem.asc").is_file()
    assert (run / "inflow_guigang.bdy").is_file()
    par = (run / "run.par").read_text(encoding="utf-8")
    assert "bdyfile\tinflow_guigang.bdy" in par
    assert "sim_time\t7200" in par  # 其余键保持
    meta = json.loads((run / "meta.json").read_text(encoding="utf-8"))
    assert meta["gr4j"]["peak"] == 200.0
    assert meta["anchor_run"].endswith("anchor_run")
    assert (run / "run_manifest.json").is_file()
    # bed 参考（E 边界列 [8,7,6] 第 10 百分位 ≈ 6.2）
    bed = edge_bed_reference(anchor / "dem.asc", edge="E", pct=10.0)
    assert bed == pytest.approx(6.2, abs=0.2)


def test_prepare_run_guard_refuses_overwrite(tmp_path):
    anchor = _make_anchor(tmp_path)
    out = tmp_path / "existing_run"
    (out / "results").mkdir(parents=True)
    (out / "results" / "res.mass").write_text("Time Vol\n0 0\n")
    log = AuditLog("test", dry_run=False)
    bdy = write_bdy(tmp_path / "b.bdy", "yujiang", np.array([1.0, 1.0]),
                    np.array([0.0, 1.0]))
    with pytest.raises(OverwriteRefused):
        prepare_run(anchor, out, bdy_files={"inflow": bdy},
                    run_par_edits={"bdyfile": "b.bdy"}, bci_edits=None,
                    meta_extra={}, log=log)
