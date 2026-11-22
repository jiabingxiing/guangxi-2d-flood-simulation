"""终稿十四章自动化测试：对真实运行产物与冻结清单的验收测试。

覆盖第 14 章清单中此前缺成文的项：#5 雨时刻对齐、#4 边界单位、#6 水量账目、
#8 SAR 指标、#11 运行清单、#12 基准回归、以及 G16/网格/NoData/非负水深等产物不变量。
（#1/#2/#3/#7 的配置面已由 test_config_contract.py 覆盖，本文件补产物面。）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ / "scripts" / "stage3"))

REF = {
    "ningming": PROJ / "outputs/ningming/2026-08-zitan/scen_q6000_n100_100m",
    "jiulong": PROJ / "outputs/jiulong/2024-09-yagi/scen_free_200m",
    "jiulong_ztide": PROJ / "outputs/jiulong/2024-09-yagi/scen_ztide_200m",
    "guigang": PROJ / "outputs/guigang/2024-09-yagi/scen_yagi_q11500_200m",
}


def read_hdr(p: Path) -> dict:
    with open(p) as f:
        hdr = [next(f) for _ in range(6)]
    return {l.split()[0].lower(): float(l.split()[1]) for l in hdr}


def read_asc(p: Path):
    return np.loadtxt(p, skiprows=6), read_hdr(p)


def _is_count_line(parts: list[str]) -> bool:
    """计数行判定：首词为整数且次词非数值（如 "5 seconds"、"3 hours"）。"""
    if not parts:
        return False
    try:
        int(parts[0])
    except ValueError:
        return False
    return len(parts) == 1 or not parts[1].lstrip("-").replace(".", "", 1).isdigit()


def read_series_blocks(p: Path):
    """读 .rain/.bdy 全部时序块 → [(name, [(value, time), ...]), ...]。

    模型格式（input.cpp::LoadTimeSeries）：可选名称行 → "<count> <units>" →
    count 行 "<value> <time>"；.rain 无名称行，.bdy 可含多个命名系列块
    （如 ADR-0016 顶托 run 的 yujiang QVAR + zdown HVAR 共文件）。
    """
    lines = [l for l in p.read_text().splitlines() if l.strip() and not l.startswith("#")]
    blocks = []
    i = 0
    while i < len(lines):
        parts = lines[i].split()
        name = ""
        if not _is_count_line(parts):  # .bdy 名称行
            name = lines[i].strip()
            i += 1
            if i >= len(lines):
                break
            parts = lines[i].split()
            if not _is_count_line(parts):
                i += 1
                continue
        count = int(parts[0])
        i += 1
        pts = []
        while len(pts) < count and i < len(lines):
            toks = lines[i].split()
            if len(toks) >= 2:
                try:
                    pts.append((float(toks[0]), float(toks[1])))
                except ValueError:
                    break
            i += 1
        blocks.append((name or "(anon)", pts))
    return blocks


def read_series(p: Path):
    """读时序文件的第一个块（原口径；点源标度律等单系列用法）。"""
    return read_series_blocks(p)[0][1]


# --- #11 test_run_manifest：冻结清单存在且小文件哈希可复验 ---
def test_run_manifest():
    mf = PROJ / "data/catalog/input_manifest.json"
    assert mf.exists(), "input_manifest.json 缺失（scripts/stage0/make_input_manifest.py）"
    man = json.loads(mf.read_text(encoding="utf-8"))
    import hashlib
    checked = 0
    for key, rec in man["files"].items():
        assert not rec.get("missing"), f"{key} 缺文件"
        if rec["bytes"] < 100_000:  # 小文件现场复验哈希
            p = Path(rec["path"])
            p = p if p.is_absolute() else PROJ / p
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            assert h == rec["sha256"], f"{key} 哈希漂移（输入变更，须按终稿十六评估重跑范围）"
            checked += 1
    assert checked >= 3, "至少复验 3 个小文件"


# --- #5 test_rainfall_time_alignment：全部时序文件时刻严格递增且列序正确 ---
def test_rainfall_time_alignment():
    files = list((PROJ / "outputs").rglob("rain.rain")) + list((PROJ / "outputs").rglob("*.bdy"))
    files = [f for f in files if "_v1invalid" not in str(f) and "_polyfitbed" not in str(f) and "_repro" not in str(f)]
    assert len(files) >= 8
    for f in files:
        blocks = read_series_blocks(f)
        assert blocks, f"{f} 无时序块"
        for name, pts in blocks:  # 多系列块（.bdy 命名系列）逐块检查
            assert len(pts) >= 2, f"{f}:{name} 时序块点数 < 2"
            ts = [t for _, t in pts]
            assert all(b > a for a, b in zip(ts, ts[1:])), \
                f"{f}:{name} 时刻非严格递增（列互换/重号）"
        if f.suffix == ".rain":  # 雨强非负；.bdy（潮位）允许负值
            vs = [v for _, pts in blocks for v, _ in pts]
            assert min(vs) >= 0, f"{f} 雨强为负（疑似列互换：值列应是时刻）"


# --- #4 test_boundary_units_and_sign：点源标度律（B6：文件值=Q/网格边长）---
def test_boundary_units_and_sign():
    p = read_series(REF["ningming"] / "mingjiang.bdy")  # 基流 80 m3/s @100m -> 0.8
    assert abs(p[0][0] - 80.0 / 100.0) < 1e-3
    g = read_series(REF["guigang"] / "yujiang.bdy")      # 基流 1000 m3/s @200m -> 5.0
    assert abs(g[0][0] - 1000.0 / 200.0) < 1e-3
    for f in (REF["ningming"] / "bci.bci", REF["guigang"] / "bci.bci"):
        assert "99999999" in f.read_text(), f"{f} 未用全边段超大坐标（G14 修复基线）"


# --- #6 test_water_balance：统一口径公式（合成质量文件，已知残差）---
def test_water_balance(tmp_path):
    from g15_mass_recheck import e_mass
    res = tmp_path / "results"
    res.mkdir()
    rows = ("Time Tstep MinTstep NumTsteps Area Vol Qin Hds Qout Qerror Verror Rain-(Inf+Evap)\n"
            "0 1 1 1 0 0 0 0 0 0 0 0\n"
            "3600 1 1 2 0 180100 1000 0 900 0 0 100\n")
    # 梯形积分：∫Qin=1.8e6, ∫Qout=1.62e6 -> Ve = 1.8e5 + rain 100 = 180100，恰闭合
    (res / "res.mass").write_text(rows)
    rec = e_mass(tmp_path)
    assert rec["e_mass_132"] <= 1e-6, rec


# --- #8 test_sar_metrics：合成掩膜精确指标 ---
def test_sar_metrics():
    def scores(sim, obs):
        H = int((sim & obs).sum()); FA = int((sim & ~obs).sum()); M = int((~sim & obs).sum())
        return (round(H / (H + FA + M), 3), round(H / (H + M), 3), round(FA / (H + FA), 3))
    sim = np.array([1, 1, 1, 0, 0], bool)
    obs = np.array([1, 1, 0, 1, 0], bool)
    assert scores(sim, obs) == (0.5, 0.667, 0.333)


# --- 产物不变量：网格一致 / NoData / 非负水深 / G16 ---
def test_grid_alignment():
    for run in REF.values():
        d1, _ = read_asc(run / "dem.asc")
        d2, hdr2 = read_asc(run / "results" / "res.max")
        h1 = read_hdr(run / "dem.asc")
        assert d1.shape == d2.shape, run
        for k in ("xllcorner", "yllcorner", "cellsize", "ncols", "nrows"):
            assert h1[k] == hdr2[k], f"{run} {k} 不一致"


def test_nodata_and_g16():
    for run in REF.values():
        dem, _ = read_asc(run / "dem.asc")
        assert (dem != -9999).all(), f"{run} 残留 NoData"
        assert np.isfinite(dem).all(), run
    jl, _ = read_asc(REF["jiulong"] / "dem.asc")
    assert (jl >= 0).all(), "久隆 v2 DEM 出现负值（G16 回归！）"


def test_dry_wet_transition():
    for run in REF.values():
        wds = sorted((run / "results").glob("res-*.wd"))
        assert wds, run
        w, _ = read_asc(wds[-1])
        assert (w >= -1e-9).all() and np.isfinite(w).all(), f"{run} 负水深/NaN"


# --- #12 test_regression_reference_cases：基准登记与质量门字段 ---
def test_regression_reference_cases():
    bm = json.loads((PROJ / "data/catalog/benchmarks.json").read_text(encoding="utf-8"))
    for key in ("B1", "B2", "B3", "B4", "B5", "B6", "B7"):
        assert key in bm, f"{key} 未登记"
        assert bm[key].get("status"), f"{key} 缺 status"
    assert bm["B2"]["status"] == "SYMMETRY_PASS_MASS_FAIL"
    assert bm["B2"]["mass_gate"] == "FAIL", "B2 质量增益必须显式记 FAIL"
    # B8 于 Phase B2 执行：合成堤防段 PASS；T016c 桥洞段与仓库 5.7.0 参考不一致 ->
    # 只允许 PASS_PARTIAL（不得把“差异”当通过，也不得再标 NOT_RUN）
    assert bm["B8"]["status"] in ("PASS", "PASS_PARTIAL", "FAIL"), bm["B8"]["status"]
    assert bm["B8"]["status"] != "NOT_RUN", "B8 已执行，未跑标记必须更新"
    assert bm["B8"]["B8a_status"] == "PASS", "合成堤防连通性判据未通过"
    assert bm["B8"]["B8b_status"] in ("PASS", "DIFFERS_FROM_REFERENCE")
    if bm["B8"]["B8b_status"] == "DIFFERS_FROM_REFERENCE":
        assert bm["B8"]["status"] == "PASS_PARTIAL", "参考构建不一致时不得整体判 PASS"
    mr = json.loads((PROJ / "data/catalog/mass_recheck_g15.json").read_text(encoding="utf-8"))
    assert mr["counts"]["FAIL"] == 0, "存在未解释的质量门 FAIL"
    for name, rec in mr["runs"].items():
        assert rec["gate_1e-3"] in ("PASS", "FAIL-EXPLAINED", "FAIL"), name
        if rec["gate_1e-3"] == "FAIL-EXPLAINED":
            assert rec.get("cause"), f"{name} FAIL-EXPLAINED 缺原因"


# --- #3/#10 土地覆盖与糙率网格值域 ---
def test_landcover_roughness_range():
    import rasterio
    with rasterio.open(PROJ / "data/interim/ningming/mannings_12p5m_32648.tif") as ds:
        v = ds.read(1)
    vals = v[(v > 0) & (v < 9999)]
    assert 0.01 <= vals.min() and vals.max() <= 0.5, f"糙率越界 {vals.min()}~{vals.max()}"
