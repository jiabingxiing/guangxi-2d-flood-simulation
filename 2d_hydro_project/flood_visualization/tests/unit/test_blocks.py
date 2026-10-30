"""数值保真层单元测试：256 分块读写、边缘裁剪、掩膜 PNG、numeric manifest 契约。"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import numpy as np
import pytest
from PIL import Image

from conftest import PROJECT_ROOT, SYNTH_NODATA
from floodviz.raster.blocks import (
    BLOCK_SIZE,
    BlocksError,
    block_counts,
    build_numeric_manifest,
    iter_block_windows,
    read_block,
    read_frame,
    read_mask_png,
    read_value,
    write_blocks,
    write_mask_png,
)
from floodviz.raster.grid import header_to_grid

MANIFEST_SCHEMA = PROJECT_ROOT / "schemas" / "numeric_manifest.schema.json"


def _grid(rows: int = 6, cols: int = 8):
    return header_to_grid(
        {
            "ncols": cols,
            "nrows": rows,
            "xllcorner": 246900.0,
            "yllcorner": 2456400.0,
            "cellsize": 200.0,
            "nodata_value": SYNTH_NODATA,
        },
        "EPSG:32649",
    )


def _sample(rows: int = 6, cols: int = 8) -> np.ndarray:
    arr = np.arange(rows * cols, dtype=np.float32).reshape(rows, cols) / 10.0
    arr[0, 0] = np.nan
    arr[rows - 1, cols - 1] = np.nan
    return arr


# ---------------------------------------------------------------- 块布局


def test_block_counts() -> None:
    assert block_counts(256, 256) == (1, 1)
    assert block_counts(257, 256) == (2, 1)
    assert block_counts(300, 600) == (2, 3)
    assert block_counts(6, 8) == (1, 1)
    with pytest.raises(BlocksError):
        block_counts(0, 10)
    with pytest.raises(BlocksError):
        block_counts(10, 10, block_size=0)


def test_iter_block_windows_cover_exactly() -> None:
    windows = list(iter_block_windows(300, 600))
    assert len(windows) == 6
    assert windows[0][:2] == (0, 0)
    assert windows[0][2:] == (0, 256, 0, 256)
    assert windows[-1][:2] == (1, 2)
    assert windows[-1][2:] == (256, 300, 512, 600)
    coverage = np.zeros((300, 600), dtype=np.int16)
    for _by, _bx, r0, r1, c0, c1 in windows:
        coverage[r0:r1, c0:c1] += 1
    assert coverage.min() == 1 and coverage.max() == 1


# ---------------------------------------------------------------- 读写


def test_write_read_frame_roundtrip(tmp_path: Path) -> None:
    arr = _sample()
    stats = write_blocks(arr, tmp_path / "f0000")
    assert stats["blocks_rows"] == 1 and stats["blocks_cols"] == 1
    assert stats["bytes"] == arr.size * 4
    back = read_frame(tmp_path / "f0000", rows=arr.shape[0], cols=arr.shape[1])
    np.testing.assert_array_equal(np.isnan(back), np.isnan(arr))
    finite = ~np.isnan(arr)
    np.testing.assert_array_equal(back[finite], arr[finite])
    assert back.dtype == np.float32


def test_write_read_edge_blocks(tmp_path: Path) -> None:
    rows, cols = 300, 600
    arr = np.arange(rows * cols, dtype=np.float32).reshape(rows, cols)
    write_blocks(arr, tmp_path / "frame")
    last = read_block(tmp_path / "frame", 1, 2, rows=rows, cols=cols)
    assert last.shape == (44, 88)
    np.testing.assert_array_equal(last, arr[256:300, 512:600])
    full = read_frame(tmp_path / "frame", rows=rows, cols=cols)
    np.testing.assert_array_equal(full, arr)


def test_read_value_and_out_of_range(tmp_path: Path) -> None:
    arr = _sample()
    write_blocks(arr, tmp_path / "frame")
    for row, col in ((0, 0), (2, 5), (5, 7)):
        value = read_value(tmp_path / "frame", row, col, rows=6, cols=8)
        if np.isnan(arr[row, col]):
            assert np.isnan(value)
        else:
            assert value == pytest.approx(float(arr[row, col]))
    with pytest.raises(BlocksError, match="在网格外"):
        read_value(tmp_path / "frame", 6, 0, rows=6, cols=8)
    with pytest.raises(BlocksError, match="在网格外"):
        read_value(tmp_path / "frame", -1, 0, rows=6, cols=8)


def test_blocks_are_float32_little_endian(tmp_path: Path) -> None:
    arr = np.array([[1.5, -2.25]], dtype=np.float64)
    write_blocks(arr, tmp_path / "frame")
    raw = (tmp_path / "frame" / "0" / "0.bin").read_bytes()
    assert raw == np.array([1.5, -2.25], dtype="<f4").tobytes()


def test_read_block_size_guard(tmp_path: Path) -> None:
    arr = _sample()
    write_blocks(arr, tmp_path / "frame")
    (tmp_path / "frame" / "0" / "0.bin").write_bytes(b"\x00\x00\x00\x00")
    with pytest.raises(BlocksError, match="元素数"):
        read_block(tmp_path / "frame", 0, 0, rows=6, cols=8)


def test_write_blocks_rejects_non_2d(tmp_path: Path) -> None:
    with pytest.raises(BlocksError, match="只接受二维"):
        write_blocks(np.zeros(3, dtype=np.float32), tmp_path / "bad")


def test_custom_block_size(tmp_path: Path) -> None:
    arr = np.arange(20, dtype=np.float32).reshape(4, 5)
    write_blocks(arr, tmp_path / "frame", block_size=2)
    assert (tmp_path / "frame" / "1" / "2.bin").is_file()
    back = read_frame(tmp_path / "frame", rows=4, cols=5, block_size=2)
    np.testing.assert_array_equal(back, arr)


def test_default_block_size_is_256() -> None:
    assert BLOCK_SIZE == 256


# ---------------------------------------------------------------- 掩膜 PNG


def test_mask_png_roundtrip(tmp_path: Path) -> None:
    valid = np.ones((6, 8), dtype=bool)
    valid[0, 0] = False
    valid[2, 3] = False
    path = tmp_path / "mask.png"
    write_mask_png(valid, path)
    with Image.open(path) as img:
        assert img.mode == "1", "掩膜必须是 1-bit PNG"
    back = read_mask_png(path)
    assert back.shape == valid.shape
    np.testing.assert_array_equal(back, valid)


def test_mask_png_rejects_non_2d(tmp_path: Path) -> None:
    with pytest.raises(BlocksError, match="必须是二维"):
        write_mask_png(np.zeros(3, dtype=bool), tmp_path / "mask.png")


# ---------------------------------------------------------------- manifest


def test_numeric_manifest_contract() -> None:
    grid = _grid()
    frames = [type("F", (), {"frame_id": "f0000"})(), type("F", (), {"frame_id": "f0001"})()]
    doc = build_numeric_manifest(
        case_id="demo",
        run_id="run_a",
        grid=grid,
        block_size=BLOCK_SIZE,
        frames=frames,
        mask_png_rel="numeric/demo/run_a/mask.png",
        sea_cell_count=0,
        has_max=True,
    )
    jsonschema.validate(doc, json.loads(MANIFEST_SCHEMA.read_text(encoding="utf-8")))
    assert doc["grid"]["rows"] == 6
    assert doc["grid"]["cols"] == 8
    assert doc["blocks"] == {"rows": 1, "cols": 1}
    assert doc["dtype"] == "<f4" and doc["byte_order"] == "little" and doc["nodata"] == "nan"
    assert [f["frame_id"] for f in doc["frames"]] == ["f0000", "f0001"]
    assert doc["frames"][0]["path_template"] == "numeric/demo/run_a/f0000/{block_y}/{block_x}.bin"
    assert doc["max"]["path_template"] == "numeric/demo/run_a/max/{block_y}/{block_x}.bin"


def test_numeric_manifest_without_max() -> None:
    doc = build_numeric_manifest(
        case_id="demo",
        run_id="run_a",
        grid=_grid(),
        block_size=BLOCK_SIZE,
        frames=[type("F", (), {"frame_id": "f0000"})()],
        mask_png_rel="numeric/demo/run_a/mask.png",
        sea_cell_count=3,
        has_max=False,
    )
    assert "max" not in doc
    assert doc["sea_cell_count"] == 3
    jsonschema.validate(doc, json.loads(MANIFEST_SCHEMA.read_text(encoding="utf-8")))
