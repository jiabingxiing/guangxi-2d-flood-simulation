"""成因归因产品单测（计划 T03，ADR-0015 §3 规则）。

覆盖：四分支规则、边界环带优先标记、面积统计、规则未冻结拒绝计算、
形状不一致 fail-closed、tmp run 目录端到端（tif + meta + 审计）。
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
    AsciiGrid,
    GridError,
    write_ascii_grid,
)
from scripts.stage6.make_genesis_attribution import (  # noqa: E402
    classify,
    class_stats,
    load_rules,
    write_origin_tif,
)

HDR = {"ncols": 6, "nrows": 6, "xllcorner": 500000.0, "yllcorner": 2400000.0,
       "cellsize": 100.0, "nodata_value": -9999.0}


def _grid(data: np.ndarray) -> AsciiGrid:
    return AsciiGrid(data=data.astype(np.float32), header=dict(HDR))


def _max_with(cells: dict[tuple[int, int], float]) -> np.ndarray:
    arr = np.zeros((6, 6))
    for (r, c), v in cells.items():
        arr[r, c] = v
    return arr


def test_classify_four_branches():
    full = _max_with({(1, 1): 0.6, (1, 2): 0.6, (1, 3): 0.0, (2, 1): 0.0})
    off = _max_with({(1, 1): 0.4, (1, 2): 0.0, (1, 3): 0.4, (2, 1): 0.0})
    cls = classify(full, off, wet_thr=0.05, edge_band_cells=1)
    assert cls[1, 1] == 1  # both wet → river-origin
    assert cls[1, 2] == 2  # full wet, off dry → pluvial
    assert cls[1, 3] == 4  # full dry, off wet → disagreement
    assert cls[2, 1] == 0  # both dry → never
    # 边界环带（1 格）：(0,5) 在环带上且 off 湿 → 3 覆盖成因类
    off2 = off.copy()
    off2[0, 5] = 0.8
    cls2 = classify(full, off2, wet_thr=0.05, edge_band_cells=1)
    assert cls2[0, 5] == 3


def test_classify_edge_band_priority_over_all():
    full = _max_with({(0, 0): 0.6})
    off = _max_with({})
    cls = classify(full, off, wet_thr=0.05, edge_band_cells=1)
    assert cls[0, 0] == 3  # 环带上 river-origin 优先标 edge_band
    inner = classify(full, off, wet_thr=0.05, edge_band_cells=0)
    assert inner[0, 0] == 2  # 关闭环带后回到成因类（full 湿 ∧ off 干 → pluvial）


def test_classify_shape_mismatch():
    with pytest.raises(GridError):
        classify(np.zeros((5, 5)), np.zeros((6, 6)))


def test_class_stats():
    cls = np.zeros((6, 6), dtype=np.uint8)
    cls[1:3, 1:3] = 1
    cls[3, 3] = 2
    cls[3, 4] = 4
    cls[0, 0] = 3
    stats = class_stats(cls, 100.0 * 100.0)
    assert stats["river_origin"]["cells"] == 4
    assert stats["river_origin"]["area_km2"] == pytest.approx(0.04)
    assert stats["pluvial_origin"]["cells"] == 1
    assert stats["disagreement"]["cells"] == 1
    assert stats["edge_band"]["cells"] == 1
    assert stats["never_wet"]["cells"] == 6 * 6 - 7


def test_load_rules_requires_frozen(tmp_path):
    p = tmp_path / "rules.json"
    p.write_text(json.dumps({"full_run": "x", "rain_off_run": "y",
                             "wet_threshold_m": 0.05, "frozen": False}),
                 encoding="utf-8")
    with pytest.raises(ValueError, match="frozen"):
        load_rules(p)
    missing = tmp_path / "none.json"
    with pytest.raises(FileNotFoundError):
        load_rules(missing)


def test_load_rules_rejects_missing_keys(tmp_path):
    p = tmp_path / "rules.json"
    p.write_text(json.dumps({"frozen": True}), encoding="utf-8")
    with pytest.raises(ValueError, match="full_run"):
        load_rules(p)


def test_end_to_end_origin_tif(tmp_path, monkeypatch):
    """两个 tmp run res.max → 分类 tif + meta + 审计（CLI 主链路，tmp 内落盘）。"""
    full = _max_with({(1, 1): 0.6, (1, 2): 0.6, (3, 3): 0.9, (0, 0): 0.5})
    off = _max_with({(1, 1): 0.4, (1, 3): 0.3, (3, 3): 0.1})
    full_run = tmp_path / "full_run"
    off_run = tmp_path / "off_run"
    (full_run / "results").mkdir(parents=True)
    (off_run / "results").mkdir(parents=True)
    write_ascii_grid(full_run / "results" / "res.max", _grid(full))
    write_ascii_grid(off_run / "results" / "res.max", _grid(off))
    rules = {
        "doc": "测试规则", "frozen": True, "wet_threshold_m": 0.05,
        "full_run": str(full_run), "rain_off_run": str(off_run),
        "basis": "event-max", "edge_band_cells": 1,
        "confounder_note": "测试", "prohibited_readings": [],
    }
    rules_path = tmp_path / "rules.json"
    rules_path.write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")
    assert load_rules(rules_path)["frozen"] is True

    import scripts.stage6.make_genesis_attribution as gen
    monkeypatch.setattr(sys, "argv", [
        "prog", "--rules", str(rules_path), "--run-tag", "t1",
        "--out-dir", str(tmp_path / "genesis_out"),
        "--log-json", str(tmp_path / "genesis_audit.json"),
    ])
    gen.main()

    out_tif = tmp_path / "genesis_out" / "origin_class.tif"
    assert out_tif.is_file()
    assert (tmp_path / "genesis_out" / "origin_class.tif.meta.json").is_file()
    audit = json.loads((tmp_path / "genesis_audit.json").read_text(encoding="utf-8"))
    # 环带只标记湿像元：测试数据只有 (0,0) 在环带上湿 → edge_band=1
    assert audit["class_stats"]["edge_band"]["cells"] == 1
    # 湿像元共 5：(1,1) river、(1,2) pluvial、(1,3) disagreement、(0,0) edge_band、(3,3) river
    assert audit["class_stats"]["never_wet"]["cells"] == 31
    assert audit["confounder_note"] == "测试"
    assert audit["rules_sha256"]
    import rasterio
    with rasterio.open(out_tif) as ds:
        data = ds.read(1)
        assert ds.nodata == 255
        assert data[1, 1] == 1
        assert data[1, 2] == 2
        assert data[1, 3] == 4
        assert data[0, 0] == 3
        assert data[3, 3] == 1  # full 0.9 湿 ∧ off 0.1 湿 → river-origin
