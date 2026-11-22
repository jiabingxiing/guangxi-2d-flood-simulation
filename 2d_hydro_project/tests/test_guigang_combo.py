"""贵港组合实验（ADR-0013）单元测试：净雨时基归一、ga 模式口径、矩阵冻结。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ))

from scripts.common import net_rain as nr  # noqa: E402


# ------------------------------------------------------------ 净雨读取 ----
def _write_rain(tmp_path: Path, unit: str, rows: list[tuple[float, float]],
                name: str = "rain.rain") -> Path:
    p = tmp_path / name
    p.write_text(f"# test\n{len(rows)} {unit}\n" +
                 "".join(f"{r:.4f}\t{t:.4f}\n" for r, t in rows), encoding="ascii")
    return p


def test_read_hours_unit_with_offset_and_clip(tmp_path):
    # 5 h 序列（t'=0..4），+48 h 移位，模拟窗 100 s → t'=0,1(=3600+48*3600>100 裁掉)
    p = _write_rain(tmp_path, "hours", [(0.0, 0.0), (2.0, 1.0), (3.0, 2.0), (0.0, 3.0), (1.0, 4.0)])
    out = nr.read_preprocessor_net_rain(p, t_offset_s=48 * 3600.0, sim_seconds=100.0)
    assert out["points"][0] == (0.0, 0.0)
    assert out["points"][-1] == (0.0, 100.0)
    # 全部移位后 >100 s → 窗内为空，仅端点
    assert len(out["points"]) == 2
    assert out["total_mm"] == pytest.approx(6.0)
    assert out["tail_dropped_mm"] == pytest.approx(6.0)
    assert out["source_unit"] == "hours"
    assert len(out["source_sha256"]) == 64


def test_read_seconds_unit_in_window(tmp_path):
    p = _write_rain(tmp_path, "seconds",
                    [(0.0, 0.0), (1.5, 10.0), (2.5, 20.0), (0.0, 30.0)])
    out = nr.read_preprocessor_net_rain(p, t_offset_s=0.0, sim_seconds=25.0)
    ts = [t for _r, t in out["points"]]
    assert ts == [0.0, 10.0, 20.0, 25.0]
    assert out["total_mm_in_window"] == pytest.approx(4.0)
    assert out["tail_dropped_mm"] == pytest.approx(0.0)


def test_rejects_bad_header_and_missing_file(tmp_path):
    with pytest.raises(nr.NetRainError):
        nr.read_preprocessor_net_rain(tmp_path / "missing.rain", t_offset_s=0.0, sim_seconds=100.0)
    p = tmp_path / "bad.rain"
    p.write_text("# x\n3 fortnights\n", encoding="ascii")
    with pytest.raises(nr.NetRainError):
        nr.read_preprocessor_net_rain(p, t_offset_s=0.0, sim_seconds=100.0)


def test_rejects_non_monotonic_and_negative(tmp_path):
    p = _write_rain(tmp_path, "seconds", [(1.0, 10.0), (1.0, 5.0)])
    with pytest.raises(nr.NetRainError):
        nr.read_preprocessor_net_rain(p, t_offset_s=0.0, sim_seconds=100.0)
    p2 = _write_rain(tmp_path, "seconds", [(-1.0, 1.0)], name="neg.rain")
    with pytest.raises(nr.NetRainError):
        nr.read_preprocessor_net_rain(p2, t_offset_s=0.0, sim_seconds=100.0)


def test_declared_count_mismatch_rejected(tmp_path):
    p = tmp_path / "cnt.rain"
    p.write_text("# x\n5 seconds\n1.0\t0.0\n", encoding="ascii")
    with pytest.raises(nr.NetRainError):
        nr.read_preprocessor_net_rain(p, t_offset_s=0.0, sim_seconds=100.0)


def test_probe_offset_derivation():
    probe = PROJ / "data" / "catalog" / "probes" / "guigang_sep2024_era5_hourly.json"
    if not probe.is_file():
        pytest.skip("贵港 probe 不存在")
    off = nr.derive_probe_offset_s(probe, t0_day=3)
    assert off == pytest.approx(48 * 3600.0)  # probe 首键 2024-09-05T00，T0=09-03


# ------------------------------------------------- 真实 GA 净雨产物口径 ----
def test_guigang_ga_net_rain_window():
    src = PROJ / "outputs" / "preprocessor" / "guigang" / "rainfall_net_green_ampt.rain"
    if not src.is_file():
        pytest.skip("贵港预处理器产物未生成")
    out = nr.read_preprocessor_net_rain(src, t_offset_s=48 * 3600.0, sim_seconds=14 * 86400.0)
    # 矩阵冻结值：全序列 38.93 mm，窗内 38.87 mm，尾部 0.0606 mm
    assert out["total_mm"] == pytest.approx(38.93, abs=0.01)
    assert out["total_mm_in_window"] == pytest.approx(38.87, abs=0.01)
    assert out["tail_dropped_mm"] == pytest.approx(0.0606, abs=0.005)
    # 时轴：首雨点 >= 48 h（不早于 probe 起点移位）
    rain_ts = [t for r, t in out["points"] if r > 0]
    assert min(rain_ts) >= 48 * 3600.0 - 1e-6
    # 末点 = 模拟窗末端
    assert out["points"][-1][1] == pytest.approx(14 * 86400.0)
    assert out["source_sha256"].startswith("c78a8788348e2b8f")


def test_write_lisflood_rain_strictly_increasing(tmp_path):
    p = nr.write_lisflood_rain([(0.0, 0.0), (1.0, 3600.0)], tmp_path / "rain.rain", "# t\n")
    lines = p.read_text(encoding="ascii").splitlines()
    assert lines[1] == "2 seconds"
    assert lines[2] == "0.0000\t0"
    assert lines[3] == "1.0000\t3600"
    with pytest.raises(nr.NetRainError):
        nr.write_lisflood_rain([(0.0, 10.0), (0.0, 10.0)], tmp_path / "bad.rain", "# t\n")


# ------------------------------------------------------------- 矩阵冻结 ----
def test_round2_matrix_frozen():
    p = PROJ / "data" / "catalog" / "guigang_round2_matrix.json"
    if not p.is_file():
        pytest.skip("round2 矩阵尚未落盘")
    doc = json.loads(p.read_text(encoding="utf-8"))
    pr = doc["preregistration"]
    assert pr["matrix_id"] == "guigang-round2-combo-v1"
    runs = pr["factorial_design"]["runs_new"]
    assert {r["tag"] for r in runs} == {
        "chhyd_d14_r2_lossmed", "chhyd_d14_r2_ga", "chhyd_d10_r2_norain"}
    # G1 对账结论已登记
    assert pr["g1_reconciliation"]["status"].startswith("PASS")
    ga = pr["ga_net_rain_source"]
    assert ga["sha256_prefix"] == "c78a8788348e2b8f"
    assert ga["params_snapshot"]["net_total_mm"] == pytest.approx(38.93)


def test_ga_mode_registered_in_scenario_script():
    src = (PROJ / "scripts" / "stage5" / "guigang_scenario.py").read_text(encoding="utf-8")
    assert '"ga"' in src and 'RAIN_MODES = ("on", "off", "loss_medium", "loss_high", "ga")' in src
    assert "read_preprocessor_net_rain" in src
