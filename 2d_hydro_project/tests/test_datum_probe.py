"""垂直基准探针单测（计划 T09，ADR-0017 口径；合成沿轨数据）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import AsciiGrid, write_ascii_grid  # noqa: E402
from scripts.stage3.icesat2_datum_probe import (  # noqa: E402
    offset_for_track,
    pool_verdict,
    read_tracks_csv,
)

HDR = {"ncols": 10, "nrows": 10, "xllcorner": 357000.0, "yllcorner": 2550000.0,
       "cellsize": 100.0, "nodata_value": -9999.0}


def _tracks_on_cell(row: int, col: int, dem_elev: float, wse: float, n: int,
                    track_id: str = "t1", quality: int = 0) -> list[dict]:
    """落在指定格的合成沿轨点（lat/lon 反算该格中心）。"""
    from pyproj import Transformer

    tf = Transformer.from_crs(32649, 4326, always_xy=True)
    x = HDR["xllcorner"] + (col + 0.5) * 100.0
    y = HDR["yllcorner"] + (HDR["nrows"] - row - 0.5) * 100.0
    lon, lat = tf.transform(x, y)
    return [{"track_id": track_id, "lat": lat, "lon": lon,
             "wse_m": wse, "quality": quality} for _ in range(n)]


def _dem(elev: float = 30.0, nodata_cell: tuple[int, int] | None = None) -> np.ndarray:
    arr = np.full((10, 10), elev)
    if nodata_cell:
        arr[nodata_cell] = -9999.0
    return arr


def test_offset_track_basic():
    dem = _dem(30.0)
    pts = _tracks_on_cell(2, 2, 30.0, 25.0, 5)  # DEM 30 − WSE 25 = 5 m
    st = offset_for_track(pts, dem, HDR, 32649)
    assert st["n_points"] == 5
    assert st["median_m"] == pytest.approx(5.0)
    assert st["iqr_m"] == pytest.approx(0.0)


def test_offset_track_filters():
    dem = _dem(30.0, nodata_cell=(2, 2))
    pts = _tracks_on_cell(2, 2, 30.0, 25.0, 3, quality=1)
    st = offset_for_track(pts, dem, HDR, 32649)  # nodata 格剔除
    assert st["n_points"] == 0
    dem2 = _dem(30.0)
    pts2 = _tracks_on_cell(2, 2, 30.0, 25.0, 2, quality=1) + \
        _tracks_on_cell(2, 2, 30.0, 25.0, 2, quality=0)
    st2 = offset_for_track(pts2, dem2, HDR, 32649, min_quality=1)
    assert st2["n_points"] == 2
    gsw = np.zeros((10, 10), dtype=bool)
    gsw[3, 3] = True
    pts3 = _tracks_on_cell(3, 3, 30.0, 25.0, 4)
    st3 = offset_for_track(pts3, dem2, HDR, 32649, gsw_mask=gsw)
    assert st3["n_points"] == 4


def test_offset_track_detrend():
    """沿轨线性下沉 → 去趋势后中位数回到常量偏移。"""
    dem = _dem(30.0)
    from pyproj import Transformer
    tf = Transformer.from_crs(32649, 4326, always_xy=True)
    pts = []
    for k in range(6):
        x = HDR["xllcorner"] + (5 + 0) * 100.0
        y = HDR["yllcorner"] + (5 - 0) * 100.0 + k * 0.0  # 同格不同 wse 序列
        lon, lat = tf.transform(x, y)
        pts.append({"track_id": "t1", "lat": lat, "lon": lon,
                    "wse_m": 25.0 + 0.5 * k, "quality": 0})
    raw = offset_for_track(pts, dem, HDR, 32649)
    det = offset_for_track(pts, dem, HDR, 32649, detrend=True)
    assert raw["n_points"] == 6
    assert det["detrended"] is True
    # 未去趋势：offset 从 5 递减到 2.5，中位数 ~3.75；去趋势后中位数 ≈ 常量残差
    assert det["median_m"] != raw["median_m"]
    assert det["iqr_m"] < raw["iqr_m"] or raw["iqr_m"] == 0.0


def test_pool_verdict_threshold_and_holdout():
    stats = [{"track_id": f"t{i}", "n_points": 5, "median_m": 4.0 + 0.1 * i,
              "iqr_m": 0.1} for i in range(12)]
    v = pool_verdict(stats, "dem.asc")
    assert v["coverage"] == "sufficient"
    assert v["offset_interval"]["median_m"] == pytest.approx(4.55, abs=0.01)
    assert len(v["holdout_tracks"]) == 2
    few = stats[:3]
    v2 = pool_verdict(few, "dem.asc")
    assert v2["coverage"] == "覆盖不足"
    assert len(v2["holdout_tracks"]) == 2


def test_pool_verdict_empty():
    v = pool_verdict([{"track_id": "t0", "n_points": 0}], "dem.asc")
    assert v["offset_interval"] is None
    assert v["coverage"] == "覆盖不足"


def test_read_tracks_csv(tmp_path):
    p = tmp_path / "tracks.csv"
    p.write_text(
        "track_id,lat,lon,wse_m,quality\n"
        "t1,23.10,109.60,25.0,1\n"
        "t1,23.11,109.61,25.1,1\n"
        "t2,22.14,107.07,20.0,0\n",
        encoding="utf-8")
    rows = read_tracks_csv(p)
    assert len(rows) == 3 and rows[0]["track_id"] == "t1"
    assert rows[2]["quality"] == 0


def test_write_and_rerun_via_cli(tmp_path, monkeypatch):
    """端到端：合成 dem + 沿轨 CSV → 审计 JSON 含有界估计与留出集。"""
    import scripts.stage3.icesat2_datum_probe as probe

    dem = _dem(30.0)
    write_ascii_grid(tmp_path / "dem.asc", AsciiGrid(dem.astype(np.float32), dict(HDR)))
    from pyproj import Transformer
    tf = Transformer.from_crs(32649, 4326, always_xy=True)
    lines = ["track_id,lat,lon,wse_m,quality"]
    for i in range(3):
        for k in range(3):  # 每轨 3 点（同格微移），满足 n_points≥3 有效判据
            x = HDR["xllcorner"] + 250.0 + k * 2.0
            y = HDR["yllcorner"] + 250.0
            lon, lat = tf.transform(x, y)
            lines.append(f"t{i},{lat:.6f},{lon:.6f},{25.0:.3f},0")
    csv_path = tmp_path / "tracks.csv"
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "prog", "--input", str(csv_path), "--dem", str(tmp_path / "dem.asc"),
        "--log-json", str(tmp_path / "probe.json"),
    ])
    probe.main()
    rec = json.loads((tmp_path / "probe.json").read_text(encoding="utf-8"))["probe"]
    assert rec["data_available"] is True
    assert rec["verdict"]["coverage"] == "覆盖不足"
    assert rec["verdict"]["offset_interval"]["median_m"] == pytest.approx(5.0)
    assert "datum_offset" in rec["verdict"]["offset_interval"]["note"]
