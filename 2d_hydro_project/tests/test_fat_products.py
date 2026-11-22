"""FAT / 超阈时间产品单测（计划 T02，ADR-0015 口径）。

覆盖：首湿/超阈时刻、累计历时、NA 与最近湿像元、三级窗标签、站表 ±2 格口径、
质量门覆盖过滤、以及 tmp run 目录端到端产出（合成数据，零重跑）。
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
    AuditLog,
    GridError,
    write_ascii_grid,
)
from scripts.stage6.make_fat_products import (  # noqa: E402
    FatResult,
    compute_fat,
    load_gate_runs,
    nearest_wet,
    process_run,
    site_rowcol,
    station_record,
    thr_tag,
    window_counts,
    window_label,
    write_tif,
)

# 网格头：8×8 @100 m，锚定贵港站址（真实 lon/lat 变换后落在 (row=5, col=3) 附近），
# 保证 station_record 的站址变换在网格内。
def _station_hdr(ncols: int = 8, nrows: int = 8, pad: float = 300.0) -> dict:
    from pyproj import Transformer

    tf = Transformer.from_crs(4326, 32649, always_xy=True)
    sx, sy = tf.transform(109.60, 23.11)
    return {
        "ncols": ncols, "nrows": nrows,
        "xllcorner": sx - pad, "yllcorner": sy - pad,
        "cellsize": 100.0, "nodata_value": -9999.0,
    }


HDR = _station_hdr()
STATION_ROWCOL = site_rowcol(HDR, 109.60, 23.11, 32649)  # (row, col) 参考值


def _grid(data: np.ndarray) -> AsciiGrid:
    return AsciiGrid(data=data.astype(np.float32), header=dict(HDR))


def _frame_dir(tmp_path: Path, depth_by_frame: list[np.ndarray]) -> Path:
    """构造 tmp run 目录：results/res.max + res-XXXX.wd + run.par。"""
    res = tmp_path / "results"
    res.mkdir(parents=True)
    peak = np.max(np.stack(depth_by_frame), axis=0)
    write_ascii_grid(res / "res.max", _grid(peak), fmt="%.3f")
    for seq, depth in enumerate(depth_by_frame):
        write_ascii_grid(res / f"res-{seq:04d}.wd", _grid(depth), fmt="%.3f")
    (tmp_path / "run.par").write_text("saveint\t21600\nsim_time\t28800\n", encoding="ascii")
    return tmp_path


def _depths(depth_fn) -> list[np.ndarray]:
    return [depth_fn(t) for t in range(5)]


def test_window_and_tag_helpers():
    """纯函数：阈值命名与三级窗标签。"""
    assert thr_tag(0.3) == "0p3" and thr_tag(0.5) == "0p5" and thr_tag(1.0) == "1"
    assert window_label(-1.0) == "never"
    assert window_label(3.0) == "w1_lt6h"
    assert window_label(7.0) == "w2_6to24h"
    assert window_label(30.0) == "w3_gt24h"


def test_compute_fat_file_backed(tmp_path):
    """文件版逐帧扫描：首湿/超阈/历时与手工推演一致。"""
    def depth(t: int) -> np.ndarray:
        arr = np.zeros((8, 8))
        arr[1, 1] = [0.0, 0.4, 0.6, 0.2, 0.0][t]
        arr[2, 2] = [0.0, 0.2, 1.2, 1.2, 0.0][t]
        return arr
    run = _frame_dir(tmp_path / "run", _depths(depth))
    frames = []
    from scripts.stage6.make_fat_products import read_frame_wd
    for p in sorted((run / "results").glob("res-*.wd")):
        frames.append((int(p.stem.split("-")[1]), p))
    fat = compute_fat(frames, 6.0, (0.3, 0.5, 1.0), (8, 8))
    assert fat.first_wet_h[1, 1] == pytest.approx(6.0)
    assert fat.exceed_first_h[0.3][1, 1] == pytest.approx(6.0)
    assert fat.exceed_first_h[0.5][1, 1] == pytest.approx(12.0)
    assert fat.exceed_first_h[1.0][1, 1] == pytest.approx(-1.0)
    assert fat.duration_h(0.3)[1, 1] == pytest.approx(12.0)
    assert fat.duration_h(0.5)[1, 1] == pytest.approx(6.0)
    # cell(2,2)：首湿 6h（0.2>0.05），1.0 超阈 12h，1.0 历时 12h
    assert fat.first_wet_h[2, 2] == pytest.approx(6.0)
    assert fat.exceed_first_h[0.3][2, 2] == pytest.approx(12.0)
    assert fat.exceed_first_h[1.0][2, 2] == pytest.approx(12.0)
    assert fat.duration_h(1.0)[2, 2] == pytest.approx(12.0)
    # cell(0,0) 全程干
    assert fat.first_wet_h[0, 0] == pytest.approx(-1.0)
    assert fat.duration_h(0.3)[0, 0] == 0


def test_nearest_wet_chebyshev():
    wet = np.zeros((9, 9), dtype=bool)
    first = np.full((9, 9), -1.0)
    wet[2, 5] = True; first[2, 5] = 30.0   # Chebyshev 距离 3
    wet[4, 2] = True; first[4, 2] = 12.0   # 距离 3，但首湿更早
    near = nearest_wet(wet, first, 5, 5, cap=10)
    assert near is not None and near["distance_cells"] == 3
    assert near["first_wet_h"] == pytest.approx(12.0)  # 同距取首湿最早
    wet[5, 7] = True; first[5, 7] = 6.0     # 距离 2 → 命中
    near = nearest_wet(wet, first, 5, 5, cap=10)
    assert near["distance_cells"] == 2 and near["first_wet_h"] == pytest.approx(6.0)
    empty = np.zeros((9, 9), dtype=bool)
    assert nearest_wet(empty, np.full((9, 9), -1.0), 4, 4, cap=3) is None


def test_station_record_na_with_nearest():
    row, col = STATION_ROWCOL
    assert HDR["nrows"] == 8 and 3 <= row < 6  # 锚定后站址在网格内
    first = np.full((8, 8), -1.0)
    first[row - 3, col] = 18.0  # 站址的 Chebyshev 距离 3（r2 窗外）
    exceed = np.full((8, 8), -1.0)
    exceed[row - 3, col] = 24.0
    dur = np.zeros((8, 8))
    fat = FatResult(saveint_h=6.0, first_wet_h=first,
                    exceed_first_h={0.3: exceed}, exceed_frames={0.3: dur / 6.0})
    resmax = np.full((8, 8), 0.02)
    resmax[row - 3, col] = 0.9
    st = station_record(fat, resmax, HDR, "guigang", "run_x", (0.3,))
    assert st["center_first_wet_h"] is None
    assert st["r2_first_wet_h"] is None  # ±2 格内无湿
    assert st["nearest_wet"]["found"] is True
    assert st["nearest_wet"]["distance_cells"] == 3
    assert st["nearest_wet"]["first_wet_h"] == pytest.approx(18.0)
    assert st["center_exceed_0p3_h"] is None
    assert st["depth_max_center_m"] == pytest.approx(0.02)


def test_station_record_wet_center_window():
    row, col = STATION_ROWCOL
    first = np.full((8, 8), -1.0)
    first[row, col] = 7.0
    exceed = np.full((8, 8), -1.0)
    exceed[row, col] = 7.0
    exceed[row - 2, col] = 3.0  # r2 窗内更早超阈
    dur = np.zeros((8, 8))
    dur[row, col] = 30.0
    fat = FatResult(saveint_h=6.0, first_wet_h=first,
                    exceed_first_h={0.3: exceed}, exceed_frames={0.3: dur / 6.0})
    resmax = np.full((8, 8), 0.5)
    st = station_record(fat, resmax, HDR, "guigang", "run_y", (0.3,))
    assert st["center_first_wet_h"] == pytest.approx(7.0)
    assert st["r2_first_wet_h"] == pytest.approx(7.0)  # r2 首湿只有中心湿
    assert st["center_exceed_0p3_h"] == pytest.approx(7.0)
    assert st["r2_exceed_0p3_h"] == pytest.approx(3.0)
    assert st["center_duration_0p3_h"] == pytest.approx(30.0)
    assert st["center_window_0p3"] == "w2_6to24h"
    assert "nearest_wet" not in st


def test_site_rowcol_outside_raises():
    with pytest.raises(GridError):
        site_rowcol(HDR, 500.0, 30.0, 32649)


def test_window_counts():
    arr = np.array([3.0, 7.0, 30.0, -1.0, 5.9])
    counts = window_counts(arr)
    assert counts == {"never": 1, "w1_lt6h": 2, "w2_6to24h": 1, "w3_gt24h": 1}


def test_load_gate_runs_filters_pass(tmp_path):
    cat = tmp_path / "mass_recheck_g15.json"
    cat.write_text(json.dumps({"runs": {
        "outputs/a/r1": {"gate_1e-3": "PASS"},
        "outputs/a/r2": {"gate_1e-3": "FAIL-EXPLAINED"},
        "outputs/b/r3": {"gate_1e-3": "FAIL"},
    }}, ensure_ascii=False), encoding="utf-8")
    gate = load_gate_runs(cat)
    assert list(gate) == ["outputs/a/r1"]
    assert set(load_gate_runs(cat)) == {"outputs/a/r1"}


def test_write_tif_na(tmp_path):
    arr = np.array([[6.0, -1.0], [-1.0, 0.0]])
    import rasterio
    p = write_tif(tmp_path / "x.tif", arr, HDR, 32649)
    with rasterio.open(p) as ds:
        data = ds.read(1)
        assert ds.nodata == -9999.0
        assert data[0, 0] == pytest.approx(6.0)
        assert data[0, 1] == pytest.approx(-9999.0)


def test_process_run_end_to_end(tmp_path):
    """tmp run 目录 → tif + 站表 CSV/JSON + 审计记录（零重跑链路）。"""
    row, col = STATION_ROWCOL

    def depth(t: int) -> np.ndarray:
        arr = np.zeros((8, 8))
        arr[row, col] = [0.0, 0.6, 0.8, 0.2, 0.0][t]
        return arr
    run = _frame_dir(tmp_path / "results_run", _depths(depth))
    out_root = tmp_path / "products"
    log = AuditLog("test", dry_run=False)
    rec = process_run(run, "guigang", (0.3, 0.5, 1.0), out_root, dry_run=False)
    assert rec["status"] == "ok"
    assert rec["n_frames"] == 5
    assert rec["saveint_h"] == pytest.approx(6.0)
    out_dir = out_root / "guigang" / "fat" / "results_run"
    for name in ("first_wet_h.tif", "exceed_h_thr0p3.tif", "duration_h_thr0p3.tif",
                 "exceed_h_thr0p5.tif", "duration_h_thr0p5.tif", "exceed_h_thr1.tif",
                 "duration_h_thr1.tif"):
        assert (out_dir / name).is_file(), name
    st_csv = out_root / "guigang" / "fat" / "station_tables" / "results_run_stations.csv"
    st_json = out_root / "guigang" / "fat" / "station_tables" / "results_run_stations.json"
    assert st_csv.is_file() and st_json.is_file()
    st = json.loads(st_json.read_text(encoding="utf-8"))["stations"][0]
    assert st["center_first_wet_h"] == pytest.approx(6.0)
    assert st["center_exceed_0p3_h"] == pytest.approx(6.0)
    assert st["center_exceed_0p5_h"] == pytest.approx(6.0)  # 0.6>0.5 在 t=6h
    assert st["center_exceed_1_h"] is None  # 峰值 0.8 < 1.0
    # 干跑：不写任何文件
    out2 = tmp_path / "products_dry"
    rec_dry = process_run(run, "guigang", (0.3,), out2, dry_run=True)
    assert rec_dry["status"] == "ok"
    assert not (out2 / "guigang" / "fat").exists()


def test_process_run_skips_missing_frames(tmp_path):
    empty = tmp_path / "no_frames"
    empty.mkdir()
    rec = process_run(empty, "guigang", (0.3,), tmp_path / "out")
    assert rec["status"] == "skipped_no_frames"
