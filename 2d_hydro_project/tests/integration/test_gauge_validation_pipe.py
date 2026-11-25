"""M1 集成测试：官方水情接入、反混淆 fail-closed、虚拟水尺提取与同化导出流水线。"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import requests

from hydro_refine.core.paths import PROJ
from hydro_refine.monitors.boundary_assimilation import (
    export_hvar_bdy,
    export_qvar_bdy,
    stage_to_discharge_rating,
)
from hydro_refine.monitors.gauge_extractor import extract_run_virtual_gauges
from hydro_refine.monitors.gauge_ingestor import (
    fetch_public_http_table,
    ingest_case_gauges,
    interpolate_hourly_series,
)
from scripts.common.grid_io import AsciiGrid, write_ascii_grid


def test_ingest_guigang_and_ningming_configs() -> None:
    gg = ingest_case_gauges(PROJ / "configs" / "gauges" / "guigang_stations.yaml")
    assert gg.case == "guigang"
    assert len(gg.stations) == 2
    assert len(gg.stations[0].observations) >= 2

    nm = ingest_case_gauges(PROJ / "configs" / "gauges" / "ningming_stations.yaml")
    assert nm.case == "ningming"
    # shapefile 含 4 个官方测站
    assert len(nm.stations) >= 4


def test_font_obfuscation_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResp:
        status_code = 200
        text = '{"result":[{"z":"#jAxlk0usK3_123otltag#FontTag"}]}'

    monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResp())
    out = fetch_public_http_table("http://example.invalid/water")
    assert out["status"] == "protected_or_obfuscated"
    assert out["rows"] == []


def test_virtual_gauge_extraction_and_bdy_export(tmp_path: Path) -> None:
    run_dir = tmp_path / "synth_run"
    res_dir = run_dir / "results"
    res_dir.mkdir(parents=True)
    (run_dir / "run.par").write_text("saveint\t21600\n", encoding="ascii")

    # 构造 5x5 网格，中心对应 Guigang 锚点附近 (lon=109.6, lat=23.11)
    from pyproj import Transformer
    tf = Transformer.from_crs(4326, 32649, always_xy=True)
    ax, ay = tf.transform(109.60, 23.11)
    cell = 100.0
    xll = ax - 2.5 * cell
    yll = ay - 2.5 * cell
    hdr = {"ncols": 5, "nrows": 5, "xllcorner": xll, "yllcorner": yll, "cellsize": cell, "nodata_value": -9999.0}

    dem = np.full((5, 5), 20.0, dtype=np.float32)
    dem[2, 2] = 18.0
    write_ascii_grid(run_dir / "dem.asc", AsciiGrid(dem, hdr))

    for seq, depth in [(0, 1.0), (1, 3.5), (2, 2.0)]:
        wd = np.zeros((5, 5), dtype=np.float32)
        wd[2, 2] = depth
        write_ascii_grid(res_dir / f"res-{seq:04d}.wd", AsciiGrid(wd, hdr))

    stations = [{
        "station_id": "test_gg",
        "name": "测试站",
        "lon": 109.60,
        "lat": 23.11,
        "in_model_domain": True,
        "exclude_from_validation": False,
    }]
    ext = extract_run_virtual_gauges(run_dir, stations, epsg=32649, half_window=1, mode="center")
    assert ext["available"] is True
    pr = ext["probes"]["test_gg__raw_anchor"]
    assert pr["t_h"] == [0.0, 6.0, 12.0]
    assert pr["wse_m"] == [19.0, 21.5, 20.0]

    # 测试插值与水位-流量同化输出
    q_arr = stage_to_discharge_rating(pr["wse_m"], h0_m=18.0, coeff_a=120.0, exponent_b=1.5)
    bdy_q = export_qvar_bdy(tmp_path / "inflow.bdy", "test_in", [0.0, 21600.0, 43200.0], q_arr, cellsize_m=100.0)
    bdy_h = export_hvar_bdy(tmp_path / "stage.bdy", "test_out", [0.0, 21600.0, 43200.0], pr["wse_m"])
    assert "3 seconds" in bdy_q.read_text(encoding="ascii")
    assert "3 seconds" in bdy_h.read_text(encoding="ascii")

    gg = ingest_case_gauges(PROJ / "configs" / "gauges" / "guigang_stations.yaml")
    interp = interpolate_hourly_series(gg.stations[0].observations, max_gap_h=48.0)
    assert len(interp) > len(gg.stations[0].observations)
