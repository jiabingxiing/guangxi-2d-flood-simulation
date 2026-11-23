"""SGC 合成算例组装逻辑的纯 Python 单元测试（计划任务4 / ADR-0020 护栏）。

只测 scripts/stage3/run_sgc_synthetic_tests.py 中不依赖 lisflood.exe 的函数：
run.par 装配、.bdy/.bci 书写、DEM/河道掩膜、res.mass 质量解析（口径同
g15_mass_recheck）与判据评估。exe 实跑由该脚本自身完成，不在 pytest 内。
res.mass 样例串镜像 outputs/jiulong/2024-09-yagi/scen_free_200m_sv6h/results/res.mass
的真实表头（12 列、空白分隔、CRLF）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import read_ascii_grid  # noqa: E402
from scripts.stage3.run_sgc_synthetic_tests import (  # noqa: E402
    CHAN_COL,
    CHAN_WIDTH,
    DX,
    E_MASS_MAX,
    NCOLS,
    NROWS,
    S1_DRIFT_MAX,
    S1_PIT_DEPTH_MIN_M,
    S2_DRY_BLOCK_LIMIT,
    S2_WET_THRESH_M,
    S5_JUNCTION_ROW,
    S7_END_WET_MAX,
    S7_MAX_DEPTH_M,
    S8_BAND_WET_MIN,
    assemble_par,
    bankfull_depth,
    build_case,
    build_grids,
    case_grids,
    count_dry_blocks_2x2,
    e_mass_from_rows,
    evaluate_case,
    inflow_x_range,
    mass_rows,
    render_bdy,
    render_grid,
    s5_staircase_path,
)

# 与真实 res.mass 相同的表头（原文件为 CRLF、多空格分列）
REAL_MASS_HEADER = (
    "Time         Tstep      MinTstep   NumTsteps    Area         Vol         "
    "Qin         Hds        Qout          Qerror       Verror       Rain-(Inf+Evap)"
)


def _mass_text(body_rows: list[str]) -> str:
    return REAL_MASS_HEADER + "\r\n" + "\r\n".join(body_rows) + "\r\n"


# --------------------------------------------------------------------------- #
# run.par 装配
# --------------------------------------------------------------------------- #
def test_assemble_par_contains_sgc_lines() -> None:
    """SGC 开启时：裸词开关行 + 全部 TAB 分隔的 SGC 参数与基础键必须齐备。"""
    par = assemble_par(bdy="inflow.bdy")
    assert "SGC_enable\n" in par  # 裸词行，无值
    for line in (
        "DEMfile\tdem.asc",
        "bcifile\tbci.bci",
        "bdyfile\tinflow.bdy",
        "sim_time\t3600.0",
        "initial_tstep\t0.5",
        "massint\t10.0",
        "saveint\t60.0",
        "fpfric\t0.035",
        "acceleration\n",
        "SGCwidth\tsgc_width.asc",
        "SGCbank\tsgc_bank.asc",
        "SGCchan\t1",
        "SGCn\t0.03",
        "SGCr\t0.55",
        "SGCp\t0.4",
    ):
        assert line in par, line


def test_assemble_par_omits_optional_blocks() -> None:
    """SGC/入流/降雨未启用时不得出现对应行；基础键仍须齐备。"""
    par = assemble_par(sgc=False, bdy=None, rainfall=None)
    assert "SGC" not in par
    assert "bdyfile" not in par
    assert "rainfall" not in par
    for key in ("DEMfile", "bcifile", "resroot", "dirroot", "sim_time", "initial_tstep",
                "massint", "saveint", "fpfric", "acceleration"):
        assert key in par, key


# --------------------------------------------------------------------------- #
# 入流 .bdy / 边界 .bci
# --------------------------------------------------------------------------- #
def test_render_bdy_converts_q_and_orders_times() -> None:
    """文件值 = Q_m3s/dx（QVAR 逐边界像元乘 dx）；时间点严格递增。"""
    points = [(40.0, 0.0), (40.0, 1799.0), (0.0, 1800.0), (0.0, 3600.0)]
    lines = render_bdy(points, DX, comment="SGC synthetic s6").splitlines()
    assert lines[0] == "# SGC synthetic s6"
    assert lines[1] == "qin"
    assert lines[2] == "4 seconds"
    assert lines[3] == "4.0000\t0"
    assert "4.0000\t1799" in lines
    assert "0.0000\t1800" in lines
    assert "0.0000\t3600" in lines
    times = [float(ln.split("\t")[1]) for ln in lines[3:]]
    assert times == sorted(times) and len(set(times)) == len(times)
    for ln, (q, _t) in zip(lines[3:], points):
        assert abs(float(ln.split("\t")[0]) * DX - q) < 1e-9


def test_render_bdy_rejects_non_increasing_times() -> None:
    with pytest.raises(ValueError):
        render_bdy([(40.0, 0.0), (40.0, 0.0)], DX)
    with pytest.raises(ValueError):
        render_bdy([(40.0, 100.0), (0.0, 50.0)], DX)
    with pytest.raises(ValueError):
        render_bdy([(40.0, 0.0)], DX)  # 单点也不合法


def test_inflow_x_range_covers_single_channel_column() -> None:
    """北缘入流必须只覆盖河道一列：多列会把总入流放大成 n×Q（QVAR 逐像元施加）。"""
    x0, x1 = inflow_x_range()
    assert x0 == CHAN_COL * DX and x1 == (CHAN_COL + 1) * DX
    # input.cpp 北缘周界索引：BCi1 = floor((x-blx)/dx)，BCi2 = floor((finish-blx)/dx)-1
    assert int(x0 / DX) == CHAN_COL
    assert int(x1 / DX) - 1 == CHAN_COL


def test_build_case_file_set_and_bci() -> None:
    contents = build_case("s6")
    assert set(contents) == {"dem.asc", "sgc_width.asc", "sgc_bank.asc",
                             "bci.bci", "inflow.bdy", "run.par", "meta.json"}
    bci_lines = contents["bci.bci"].splitlines()
    assert bci_lines[0].startswith("N\t") and "QVAR\tqin" in bci_lines[0]
    assert bci_lines[1].startswith("S\t-99999999\t99999999\tFREE")
    assert "SGC_enable" in contents["run.par"]
    assert "sgc_width.asc" in contents["run.par"]


# --------------------------------------------------------------------------- #
# DEM / 河道掩膜
# --------------------------------------------------------------------------- #
def test_build_grids_dem_slope_and_channel_mask() -> None:
    g = build_grids()
    dem, width, bank = g["dem"], g["width"], g["bank"]
    assert dem.shape == (NROWS, NCOLS) == (40, 60)
    assert dem[0, 0] == pytest.approx(10.0)   # 北缘 10 m
    assert dem[-1, 0] == pytest.approx(5.0)   # 南缘 5 m
    assert np.all(np.diff(dem[:, 0]) < 0)     # 北→南单调下降
    assert np.all(width[:, CHAN_COL] == CHAN_WIDTH)   # 中心列全行有河道
    assert np.count_nonzero(width) == NROWS
    assert np.all(np.delete(width, CHAN_COL, axis=1) == 0)  # 其余列全为 0
    assert np.array_equal(bank, dem)          # bank 用 DEM 填满


def test_bankfull_depth_separates_s3_and_s4_regimes() -> None:
    """满岸水深 = r·w^p ≈ 1.263 m：5 m³/s 在槽内（s3），40 m³/s 必漫溢（s4）。

    r=0.55 同时盖过南缘 FREE(0.0005) 出流雍水（稳态约 0.98 m）与入流波到口
    涌高（约 1.24 m）；满岸输水约 37 m³/s 仍低于 s4 入流 40 m³/s。
    """
    assert bankfull_depth() == pytest.approx(0.55 * 8.0 ** 0.4)
    assert bankfull_depth() == pytest.approx(1.263, abs=1e-2)


def test_render_grid_roundtrip(tmp_path: Path) -> None:
    """render_grid 产物必须能被 grid_io.read_ascii_grid 读回且数值一致。"""
    g = build_grids()
    path = tmp_path / "dem.asc"
    path.write_text(render_grid(g["dem"], {"ncols": NCOLS, "nrows": NROWS, "xllcorner": 0.0,
                                           "yllcorner": 0.0, "cellsize": DX,
                                           "nodata_value": -9999.0}), encoding="ascii")
    grid = read_ascii_grid(path)
    assert grid.shape == (NROWS, NCOLS)
    assert grid.data == pytest.approx(g["dem"].astype(np.float32), abs=1e-3)


# --------------------------------------------------------------------------- #
# res.mass 解析（真实表头）
# --------------------------------------------------------------------------- #
def test_e_mass_matches_g15_formula() -> None:
    """∫Qin=600、∫Qout=390、V0=0、Ve=480、Vrain=0 → E=|480−210|/600=0.45。"""
    rows = mass_rows(_mass_text([
        "0.000    0.5  0.5  1     0.0  0.0    5.000  0.0  0.000  0.0  0.0  0.0",
        "60.000   0.5  0.5  120   0.0  250.0  5.000  0.0  4.000  0.0  0.0  0.0",
        "120.000  0.5  0.5  240   0.0  480.0  5.000  0.0  5.000  0.0  0.0  0.0",
    ]))
    assert len(rows) == 3
    parsed = e_mass_from_rows(rows)
    assert parsed["v_in_m3"] == pytest.approx(600.0)
    assert parsed["v_out_m3"] == pytest.approx(390.0)
    assert parsed["v_end_m3"] == pytest.approx(480.0)
    assert parsed["e_mass"] == pytest.approx(0.45)
    assert parsed["qout_last"] == 5.0


def test_e_mass_includes_rain_column() -> None:
    """Vrain 取末行 Rain-(Inf+Evap)：resid=350−100−(300−120)=70，E=70/400。"""
    parsed = e_mass_from_rows(mass_rows(_mass_text([
        "0.000   0.5  0.5  1     0.0  0.0    5.000  0.0  0.000  0.0  0.0  0.0",
        "60.000  0.5  0.5  120   0.0  350.0  5.000  0.0  4.000  0.0  0.0  100.0",
    ])))
    assert parsed["v_rain_m3"] == pytest.approx(100.0)
    assert parsed["e_mass"] == pytest.approx(70.0 / 400.0)


def test_e_mass_denominator_floor_at_one() -> None:
    """全零行：分母 max(V0+Vrain+∫Qin,1)=1，E=0。"""
    parsed = e_mass_from_rows(mass_rows(_mass_text([
        "0.000   0.5  0.5  1   0.0  0.0  0.0  0.0  0.0  0.0  0.0  0.0",
        "60.000  0.5  0.5  120 0.0  0.0  0.0  0.0  0.0  0.0  0.0  0.0",
    ])))
    assert parsed["e_mass"] == 0.0


# --------------------------------------------------------------------------- #
# 判据评估
# --------------------------------------------------------------------------- #
def test_evaluate_case_s3() -> None:
    kwargs = dict(e_mass=1e-4, qout_last=5.0, wet_end=None, wet_peak=None, inflow_q=5.0)
    assert evaluate_case("s3", wet_off_max=1, **kwargs)["pass"]
    assert not evaluate_case("s3", wet_off_max=3, **kwargs)["pass"]  # 槽外湿像元 > 2
    assert not evaluate_case("s3", qout_last=3.0, wet_off_max=0, e_mass=1e-4,
                             wet_end=None, wet_peak=None, inflow_q=5.0)["pass"]  # 出流未收敛
    failed = evaluate_case("s3", e_mass=2e-3, qout_last=5.0, wet_off_max=0,
                           wet_end=None, wet_peak=None, inflow_q=5.0)
    assert not failed["pass"]
    assert failed["criteria"]["e_mass_le_1e-3"]["limit"] == E_MASS_MAX


def test_evaluate_case_s4_s6() -> None:
    assert evaluate_case("s4", e_mass=1e-4, qout_last=15.0, wet_off_max=150,
                         wet_end=None, wet_peak=None, inflow_q=40.0)["pass"]
    assert not evaluate_case("s4", e_mass=1e-4, qout_last=15.0, wet_off_max=90,
                             wet_end=None, wet_peak=None, inflow_q=40.0)["pass"]
    ok = evaluate_case("s6", e_mass=1e-4, qout_last=0.0, wet_off_max=200,
                       wet_end=10, wet_peak=200, inflow_q=40.0)
    assert ok["pass"]
    assert ok["criteria"]["end_wet_le_20pct_of_peak"]["value"] == pytest.approx(0.05)
    assert not evaluate_case("s6", e_mass=1e-4, qout_last=0.0, wet_off_max=200,
                             wet_end=50, wet_peak=200, inflow_q=40.0)["pass"]
    # 峰值湿像元为 0（异常 run）→ 判 FAIL，不得除零放行
    assert not evaluate_case("s6", e_mass=1e-4, qout_last=0.0, wet_off_max=0,
                             wet_end=0, wet_peak=0, inflow_q=40.0)["pass"]


def test_evaluate_case_unknown_name() -> None:
    with pytest.raises(ValueError):
        evaluate_case("s9", e_mass=0.0, qout_last=0.0, wet_off_max=0,
                      wet_end=None, wet_peak=None, inflow_q=1.0)


# --------------------------------------------------------------------------- #
# 新增算例（计划 §4.5）：s1/s2/s5/s7/s8 组装
# --------------------------------------------------------------------------- #
def test_build_case_s1_rain_and_depression() -> None:
    """s1：无 SGC/无入流，仅南缘 FREE + train.rain；洼地 3x3 内坑 z=3 + 一圈 z=4。"""
    contents = build_case("s1")
    assert set(contents) == {"dem.asc", "bci.bci", "train.rain", "run.par", "meta.json"}
    par = contents["run.par"]
    assert "rainfall\ttrain.rain" in par and "bdyfile" not in par and "SGC" not in par
    assert "acceleration\n" in par
    bci = contents["bci.bci"].splitlines()
    assert bci == ["S\t-99999999\t99999999\tFREE 0.0005"]
    rain = contents["train.rain"].splitlines()
    assert rain[1] == "4 seconds"                            # LoadTimeSeries 不读序列名
    assert rain[2] == "20.0000\t0" and "20.0000\t1799" in rain
    assert "0.0000\t1800" in rain and rain[-1] == "0.0000\t3600"
    dem = case_grids("s1")["dem"]
    assert dem.shape == (NROWS, NCOLS) and dem[0, 0] == pytest.approx(5.0)
    assert dem[19:22, 29:32] == pytest.approx(3.0)          # 内坑 3x3
    ring = dem[18:23, 28:33].copy()
    ring[1:4, 1:4] = 5.0                                     # 排除内坑后看过渡沿
    assert ring[ring < 5.0] == pytest.approx(4.0)            # 一圈 z=4，且无其他值
    assert np.count_nonzero(dem < 5.0) == 25                 # 9 内坑 + 16 过渡沿


def test_build_case_s2_plane_and_constant_rain() -> None:
    """s2：平面 z=8→2 单调南降；15 mm/h 恒定全程；无 SGC/入流。"""
    contents = build_case("s2")
    assert set(contents) == {"dem.asc", "bci.bci", "train.rain", "run.par", "meta.json"}
    par = contents["run.par"]
    assert "rainfall\ttrain.rain" in par and "SGC" not in par and "bdyfile" not in par
    dem = case_grids("s2")["dem"]
    assert dem[0, 0] == pytest.approx(8.0) and dem[-1, 0] == pytest.approx(2.0)
    assert np.all(np.diff(dem[:, 0]) < 0)                    # 北→南单调下降
    assert dem[0, -1] == pytest.approx(dem[0, 0])            # 东西向无坡
    rain = contents["train.rain"].splitlines()
    assert rain[1] == "2 seconds"
    assert rain[2] == "15.0000\t0" and rain[3] == "15.0000\t3600"
    assert contents["bci.bci"].splitlines() == ["S\t-99999999\t99999999\tFREE 0.0005"]


def test_s5_staircase_path_connectivity() -> None:
    """s5 支流路径：起点/终点正确、逐格 4 连通、单调南行、无断链。"""
    for start, end in ((0, CHAN_COL), (NCOLS - 1, CHAN_COL)):
        path = s5_staircase_path(start, end, S5_JUNCTION_ROW)
        assert path[0] == (start, 0) and path[-1] == (end, S5_JUNCTION_ROW)
        rows = [r for _, r in path]
        assert rows == sorted(rows)                          # 逐行南下不回头
        for (c0, r0), (c1, r1) in zip(path, path[1:]):
            assert abs(c1 - c0) + abs(r1 - r0) == 1          # 相邻格必共边
        assert all(0 <= c < NCOLS and 0 <= r < NROWS for c, r in path)


def test_build_case_s5_y_channel_two_qvar_series() -> None:
    """s5：Y 形河道掩膜连通含干流；双 N 行 QVAR 各 5 m³/s；回退模式单条全缘 10 m³/s。"""
    contents = build_case("s5")
    assert set(contents) == {"dem.asc", "sgc_width.asc", "sgc_bank.asc",
                             "bci.bci", "inflow.bdy", "run.par", "meta.json"}
    width = case_grids("s5")["width"]
    assert width[0, 0] == CHAN_WIDTH and width[0, NCOLS - 1] == CHAN_WIDTH   # 两支头
    assert np.all(width[S5_JUNCTION_ROW:, CHAN_COL] == CHAN_WIDTH)           # 干流贯通
    assert np.count_nonzero(width) > NROWS                                   # 支流额外格
    # 掩膜整体连通：从两支头均可 4 连通走到干流南端
    mask = width > 0
    seen = {(0, 0)}
    frontier = [(0, 0)]
    while frontier:
        c, r = frontier.pop()
        for dc, dr in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            cc, rr = c + dc, r + dr
            if 0 <= cc < NCOLS and 0 <= rr < NROWS and mask[rr, cc] and (cc, rr) not in seen:
                seen.add((cc, rr))
                frontier.append((cc, rr))
    assert (CHAN_COL, NROWS - 1) in seen                     # 干流南端可达
    bci = contents["bci.bci"].splitlines()
    assert bci[0] == "N\t0.0\t10.0\tQVAR\tqin_w"
    assert bci[1] == f"N\t{(NCOLS - 1) * DX:.1f}\t{NCOLS * DX:.1f}\tQVAR\tqin_e"
    assert bci[2] == "S\t-99999999\t99999999\tFREE 0.0005"
    bdy = contents["inflow.bdy"].splitlines()
    assert bdy[1] == "qin_w" and bdy[2] == "2 seconds" and bdy[3] == "0.5000\t0"
    assert "qin_e" in bdy and bdy.count("0.5000\t0") == 2    # 两序列各 0.5 m³/s/列
    assert "SGC_enable" in contents["run.par"]
    assert build_case("s5")["meta.json"] is not None
    # 回退：单条全缘 QVAR，value = 10/(60·dx)
    fb = build_case("s5", inflow_mode="combined")
    assert fb["bci.bci"].splitlines()[0] == f"N\t0.0\t{NCOLS * DX:.1f}\tQVAR\tqin"
    assert "0.0167\t0" in fb["inflow.bdy"].splitlines()      # 10/600
    assert fb["bci.bci"].splitlines()[1] == "S\t-99999999\t99999999\tFREE 0.0005"


def test_build_case_s7_trench_drains_to_south_edge() -> None:
    """s7：洼地经浅沟与南缘连通（沟床沿程渐降）；脉冲 2 m³/s 分摊 2 列；无 SGC。"""
    contents = build_case("s7")
    assert set(contents) == {"dem.asc", "bci.bci", "inflow.bdy", "run.par", "meta.json"}
    par = contents["run.par"]
    assert "bdyfile\tinflow.bdy" in par and "SGC" not in par
    dem = case_grids("s7")["dem"]
    assert dem[0, 0] == pytest.approx(2.0)
    assert dem[17:23, 27:33] == pytest.approx(1.7)           # 6x6 洼地
    trench = dem[22:40, 29:31]                               # 浅沟 cols 29-30
    assert np.all(np.diff(trench[:, 0]) < 0)                 # 沟床沿程渐降（可排空）
    assert trench[0, 0] == pytest.approx(1.7)                # 与洼底衔接
    assert trench[-1, 0] < 1.7                               # 南缘低于洼底
    bci = contents["bci.bci"].splitlines()
    assert bci[0] == "N\t290.0\t310.0\tQVAR\tqin"
    assert bci[1] == "S\t-99999999\t99999999\tFREE 0.0005"
    bdy = contents["inflow.bdy"].splitlines()
    assert bdy[1] == "qin" and bdy[2] == "4 seconds"
    assert bdy[3] == "0.1000\t0"                             # 2 m³/s 分摊 2 列 = 0.1
    assert "0.0000\t600" in bdy and bdy[-1] == "0.0000\t3600"


def test_build_case_s8_hvar_stage_series() -> None:
    """s8：南缘 HVAR 水面高程 5.0→6.2 m 缓升后持稳；无雨无入流无 SGC。"""
    contents = build_case("s8")
    assert set(contents) == {"dem.asc", "bci.bci", "inflow.bdy", "run.par", "meta.json"}
    par = contents["run.par"]
    assert "bdyfile\tinflow.bdy" in par
    assert "SGC" not in par and "rainfall" not in par
    assert case_grids("s8")["dem"][0, 0] == pytest.approx(5.0)
    assert contents["bci.bci"].splitlines() == ["S\t-99999999\t99999999\tHVAR\tqhvar"]
    bdy = contents["inflow.bdy"].splitlines()
    assert bdy[1] == "qhvar" and bdy[2] == "3 seconds"
    assert bdy[3] == "5.0000\t0" and bdy[4] == "6.2000\t1800" and bdy[5] == "6.2000\t3600"


# --------------------------------------------------------------------------- #
# 新增算例判据评估
# --------------------------------------------------------------------------- #
def test_evaluate_case_s1_s2() -> None:
    """s1：坑内最小水深与坑蓄漂移；s2：湿面积比与上半场全干 2x2 块。"""
    kw = dict(qout_last=0.0, wet_off_max=0, wet_end=None, wet_peak=None, inflow_q=0.0)
    assert evaluate_case("s1", e_mass=1e-4, pit_depth_min=1.3, pit_drift=0.02, **kw)["pass"]
    assert not evaluate_case("s1", e_mass=1e-4, pit_depth_min=0.8, pit_drift=0.02,
                             **kw)["pass"]                      # 坑内水深不足
    assert not evaluate_case("s1", e_mass=1e-4, pit_depth_min=1.3, pit_drift=0.5,
                             **kw)["pass"]                      # 漂移超 5%
    assert not evaluate_case("s1", e_mass=1e-4, pit_depth_min=None, pit_drift=None,
                             **kw)["pass"]                      # 指标缺失不放行
    failed = evaluate_case("s1", e_mass=2e-3, pit_depth_min=1.3, pit_drift=0.02, **kw)
    assert not failed["pass"]
    assert failed["criteria"]["pit_depth_min_ge_1m"]["limit"] == S1_PIT_DEPTH_MIN_M
    assert failed["criteria"]["pit_volume_drift_le_5pct"]["limit"] == S1_DRIFT_MAX
    assert evaluate_case("s2", e_mass=1e-4, wet_fraction=0.95, upper_dry_blocks=5, **kw)["pass"]
    assert not evaluate_case("s2", e_mass=1e-4, wet_fraction=0.7, upper_dry_blocks=5,
                             **kw)["pass"]
    assert not evaluate_case("s2", e_mass=1e-4, wet_fraction=0.95,
                             upper_dry_blocks=S2_DRY_BLOCK_LIMIT + 1, **kw)["pass"]


def test_evaluate_case_s5_s7_s8() -> None:
    """s5：干流出流 ±10% 与无断湿；s7：有限性/末帧湿像元/最大水深；s8：南带湿像元。"""
    kw = dict(wet_off_max=0, wet_end=None, wet_peak=None, inflow_q=10.0)
    assert evaluate_case("s5", e_mass=1e-4, qout_last=10.2, stem_dry_cells=0, **kw)["pass"]
    assert not evaluate_case("s5", e_mass=1e-4, qout_last=8.0, stem_dry_cells=0, **kw)["pass"]
    assert not evaluate_case("s5", e_mass=1e-4, qout_last=10.2, stem_dry_cells=1, **kw)["pass"]
    assert evaluate_case("s7", e_mass=1e-4, qout_last=0.0, res_max_finite=True,
                         end_wet_total=3, max_depth_run=1.2,
                         wet_off_max=0, wet_end=None, wet_peak=None, inflow_q=0.0)["pass"]
    assert not evaluate_case("s7", e_mass=1e-4, qout_last=0.0, res_max_finite=False,
                             end_wet_total=3, max_depth_run=1.2,
                             wet_off_max=0, wet_end=None, wet_peak=None, inflow_q=0.0)["pass"]
    assert not evaluate_case("s7", e_mass=1e-4, qout_last=0.0, res_max_finite=True,
                             end_wet_total=S7_END_WET_MAX + 1, max_depth_run=1.2,
                             wet_off_max=0, wet_end=None, wet_peak=None, inflow_q=0.0)["pass"]
    assert not evaluate_case("s7", e_mass=1e-4, qout_last=0.0, res_max_finite=True,
                             end_wet_total=3, max_depth_run=S7_MAX_DEPTH_M + 0.1,
                             wet_off_max=0, wet_end=None, wet_peak=None, inflow_q=0.0)["pass"]
    kw8 = dict(e_mass=1e-4, qout_last=0.0, wet_off_max=0, wet_end=None, wet_peak=None,
               inflow_q=0.0)
    assert evaluate_case("s8", south_band_wet=S8_BAND_WET_MIN, res_max_finite=True, **kw8)["pass"]
    assert not evaluate_case("s8", south_band_wet=S8_BAND_WET_MIN - 1, res_max_finite=True,
                             **kw8)["pass"]
    assert not evaluate_case("s8", south_band_wet=80, res_max_finite=False, **kw8)["pass"]


def test_count_dry_blocks_2x2_matches_manual_count() -> None:
    """棋盘伪影计数：全干块向量化计数与手工枚举一致。"""
    depth = np.zeros((6, 6))
    depth[2, 2] = depth[3, 3] = 0.01                        # 打散部分 2x2 全干块
    dry = depth <= S2_WET_THRESH_M
    manual = sum(1 for r in range(5) for c in range(5)
                 if dry[r, c] and dry[r, c + 1] and dry[r + 1, c] and dry[r + 1, c + 1])
    assert count_dry_blocks_2x2(depth, S2_WET_THRESH_M) == manual
    assert count_dry_blocks_2x2(np.zeros((4, 4)), S2_WET_THRESH_M) == 9  # 全干 4x4 → 9 块
    assert count_dry_blocks_2x2(np.full((4, 4), 0.1), S2_WET_THRESH_M) == 0
