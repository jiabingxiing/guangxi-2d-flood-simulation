"""栅格层单元测试：ESRI ASCII 头、网格几何、像元中心/行列反算、NODATA 与掩膜。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from conftest import SYNTH_CELL, SYNTH_COLS, SYNTH_NODATA, SYNTH_ROWS, SYNTH_XLL, SYNTH_YLL
from floodviz.raster.grid import (
    DEFAULT_NODATA,
    Grid,
    GridError,
    GridIndexError,
    header_to_grid,
    read_ascii_grid,
    read_ascii_header,
    read_native_frame,
    read_sea_mask,
    valid_mask,
)


def _header(**overrides: object) -> dict[str, float]:
    base = {
        "ncols": SYNTH_COLS,
        "nrows": SYNTH_ROWS,
        "xllcorner": SYNTH_XLL,
        "yllcorner": SYNTH_YLL,
        "cellsize": SYNTH_CELL,
        "nodata_value": SYNTH_NODATA,
    }
    base.update(overrides)
    return base


def _grid(header: dict[str, float] | None = None, crs: str = "EPSG:32649") -> Grid:
    return header_to_grid(header or _header(), crs)


# ---------------------------------------------------------------- ASCII 头


def test_read_ascii_header(ascii_writer, tmp_path: Path) -> None:
    path = ascii_writer(tmp_path / "res-0000.wd", np.zeros((SYNTH_ROWS, SYNTH_COLS)))
    header = read_ascii_header(path)
    assert header["ncols"] == SYNTH_COLS
    assert header["nrows"] == SYNTH_ROWS
    assert header["xllcorner"] == pytest.approx(SYNTH_XLL)
    assert header["yllcorner"] == pytest.approx(SYNTH_YLL)
    assert header["cellsize"] == pytest.approx(SYNTH_CELL)
    assert header["nodata_value"] == pytest.approx(SYNTH_NODATA)
    assert set(header) == {
        "ncols",
        "nrows",
        "xllcorner",
        "yllcorner",
        "cellsize",
        "nodata_value",
    }


def test_read_ascii_header_requires_six_lines(tmp_path: Path) -> None:
    path = tmp_path / "short.wd"
    path.write_text("ncols 8\nnrows 6\nxllcorner 0\nyllcorner 0\ncellsize 200\n", encoding="utf-8")
    with pytest.raises(GridError, match="头部不足 6 行"):
        read_ascii_header(path)


def test_read_ascii_header_rejects_missing_field(tmp_path: Path) -> None:
    path = tmp_path / "badkey.wd"
    path.write_text(
        "ncols 8\nnrows 6\nxllcorner 0\nyllcorner 0\ncellsize 200\nNO_DATA -9999\n",
        encoding="utf-8",
    )
    with pytest.raises(GridError, match="头部缺少字段"):
        read_ascii_header(path)


def test_read_ascii_header_rejects_short_line(tmp_path: Path) -> None:
    path = tmp_path / "junk.wd"
    path.write_text("ncols\nnrows 6\nxllcorner 0\nyllcorner 0\ncellsize 200\nNODATA_value -9999\n", encoding="utf-8")
    with pytest.raises(GridError, match="头部行不合法"):
        read_ascii_header(path)


# ---------------------------------------------------------------- 几何


def test_header_to_grid_transform() -> None:
    grid = _grid()
    cell = SYNTH_CELL
    assert grid.rows == SYNTH_ROWS
    assert grid.cols == SYNTH_COLS
    assert grid.width == SYNTH_COLS and grid.height == SYNTH_ROWS
    assert grid.crs == "EPSG:32649"
    assert grid.transform == (cell, 0.0, SYNTH_XLL, 0.0, -cell, SYNTH_YLL + SYNTH_ROWS * cell)
    assert grid.cell_size_m == cell
    assert grid.cell_area_m2 == pytest.approx(cell * cell)


def test_header_to_grid_bounds_native() -> None:
    grid = _grid()
    minx, miny, maxx, maxy = grid.bounds_native
    assert minx == pytest.approx(SYNTH_XLL)
    assert miny == pytest.approx(SYNTH_YLL)
    assert maxx == pytest.approx(SYNTH_XLL + SYNTH_COLS * SYNTH_CELL)
    assert maxy == pytest.approx(SYNTH_YLL + SYNTH_ROWS * SYNTH_CELL)


def test_header_to_grid_rejects_bad_size() -> None:
    with pytest.raises(GridError, match="网格尺寸非法"):
        header_to_grid(_header(ncols=0), "EPSG:32649")
    with pytest.raises(GridError, match="网格尺寸非法"):
        header_to_grid(_header(cellsize=-1.0), "EPSG:32649")


def test_pixel_center_first_cell() -> None:
    grid = _grid()
    x, y = grid.pixel_center(0, 0)
    assert x == pytest.approx(SYNTH_XLL + SYNTH_CELL / 2)
    assert y == pytest.approx(SYNTH_YLL + SYNTH_ROWS * SYNTH_CELL - SYNTH_CELL / 2)


def test_pixel_center_matches_affine_formula() -> None:
    grid = _grid()
    a, b, c, d, e, f = grid.transform
    for row, col in ((0, 0), (1, 2), (SYNTH_ROWS - 1, SYNTH_COLS - 1)):
        x, y = grid.pixel_center(row, col)
        assert x == pytest.approx(a * (col + 0.5) + b * (row + 0.5) + c)
        assert y == pytest.approx(d * (col + 0.5) + e * (row + 0.5) + f)


def test_pixel_center_and_xy_to_rowcol_are_inverse() -> None:
    grid = _grid()
    for row in range(grid.rows):
        for col in range(grid.cols):
            x, y = grid.pixel_center(row, col)
            assert grid.xy_to_rowcol(x, y) == (row, col)


def test_xy_to_rowcol_rejects_outside() -> None:
    grid = _grid()
    top = SYNTH_YLL + SYNTH_ROWS * SYNTH_CELL
    right = SYNTH_XLL + SYNTH_COLS * SYNTH_CELL
    eps = 1e-6
    for x, y in (
        (SYNTH_XLL - eps, SYNTH_YLL + SYNTH_CELL),
        (right + eps, SYNTH_YLL + SYNTH_CELL),
        (SYNTH_XLL + SYNTH_CELL, SYNTH_YLL - eps),
        (SYNTH_XLL + SYNTH_CELL, top + eps),
        (right, top),  # 右上角外角不属于任何像元
    ):
        with pytest.raises(GridIndexError):
            grid.xy_to_rowcol(x, y)


def test_xy_to_rowcol_boundary_is_not_clamped() -> None:
    """恰在右/下边界时 floor 得到 cols/rows，必须报越界而不是夹边。"""
    grid = _grid()
    right = SYNTH_XLL + SYNTH_COLS * SYNTH_CELL
    with pytest.raises(GridIndexError):
        grid.xy_to_rowcol(right, SYNTH_YLL + SYNTH_CELL)
    with pytest.raises(GridIndexError):
        grid.xy_to_rowcol(SYNTH_XLL + SYNTH_CELL, SYNTH_YLL)


def test_corners_native_order_tl_tr_br_bl() -> None:
    grid = _grid()
    tl, tr, br, bl = grid.corners_native()
    right = SYNTH_XLL + SYNTH_COLS * SYNTH_CELL
    top = SYNTH_YLL + SYNTH_ROWS * SYNTH_CELL
    assert tl == pytest.approx((SYNTH_XLL, top))
    assert tr == pytest.approx((right, top))
    assert br == pytest.approx((right, SYNTH_YLL))
    assert bl == pytest.approx((SYNTH_XLL, SYNTH_YLL))


def test_xy_to_rowcol_rejects_degenerate_transform() -> None:
    flat = Grid(
        rows=3,
        cols=3,
        transform=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        crs="EPSG:32649",
        cell_size_m=1.0,
        cell_area_m2=1.0,
    )
    with pytest.raises(GridError, match="退化"):
        flat.xy_to_rowcol(0.0, 0.0)


def test_grid_error_types_are_value_errors() -> None:
    assert issubclass(GridError, ValueError)
    assert issubclass(GridIndexError, ValueError)


# ---------------------------------------------------------------- 读数值


def test_read_ascii_grid_matches_written_values(ascii_writer, depth_field, tmp_path: Path) -> None:
    values = depth_field()
    path = ascii_writer(tmp_path / "res-0000.wd", values)
    arr = read_ascii_grid(path)
    assert arr.shape == values.shape
    assert arr.dtype == np.float32
    np.testing.assert_allclose(arr, values, rtol=0, atol=1e-6)


def test_read_ascii_grid_shape_guard(ascii_writer, tmp_path: Path) -> None:
    path = ascii_writer(tmp_path / "res-0000.wd", np.zeros((SYNTH_ROWS, SYNTH_COLS)))
    with pytest.raises(GridError, match="形状"):
        read_ascii_grid(path, expected_shape=(SYNTH_COLS, SYNTH_ROWS))


def test_read_native_frame_nodata_to_nan(ascii_writer, depth_field, tmp_path: Path) -> None:
    values = depth_field()
    path = ascii_writer(tmp_path / "res-0000.wd", values)
    arr = read_native_frame(path)
    assert arr.shape == (SYNTH_ROWS, SYNTH_COLS)
    assert np.isnan(arr[0, 0]) and np.isnan(arr[SYNTH_ROWS - 1, SYNTH_COLS - 1])
    # 干地仍是 0，不是 NaN
    assert arr[0, 1] == pytest.approx(0.0)
    # 湿区保持一致
    assert arr[2, 3] == pytest.approx(values[2, 3], abs=1e-6)


def test_read_ascii_grid_nodata_to_nan_option(ascii_writer, depth_field, tmp_path: Path) -> None:
    path = ascii_writer(tmp_path / "res-0000.wd", depth_field())
    arr = read_ascii_grid(path, nodata_to_nan=True)
    assert np.isnan(arr[0, 0])
    assert not np.isnan(arr[0, 1])


def test_default_nodata_matches_contract() -> None:
    assert DEFAULT_NODATA == -9999.0


# ---------------------------------------------------------------- 掩膜


def test_read_sea_mask_absent(tmp_path: Path) -> None:
    assert read_sea_mask(tmp_path, (SYNTH_ROWS, SYNTH_COLS)) is None


def test_read_sea_mask_shape_guard(tmp_path: Path) -> None:
    (tmp_path / "sea_mask.npy").write_bytes(b"")
    np.save(tmp_path / "sea_mask.npy", np.zeros((SYNTH_ROWS + 1, SYNTH_COLS), dtype=np.uint8))
    with pytest.raises(GridError, match="形状"):
        read_sea_mask(tmp_path, (SYNTH_ROWS, SYNTH_COLS))


def test_read_sea_mask_bool(tmp_path: Path) -> None:
    mask = np.zeros((SYNTH_ROWS, SYNTH_COLS), dtype=np.uint8)
    mask[0, :] = 1
    np.save(tmp_path / "sea_mask.npy", mask)
    loaded = read_sea_mask(tmp_path, (SYNTH_ROWS, SYNTH_COLS))
    assert loaded is not None
    assert loaded.dtype == np.bool_
    assert loaded[0].all() and not loaded[1:].any()


def test_valid_mask_excludes_nan_and_sea() -> None:
    depth = np.zeros((3, 3), dtype=np.float32)
    depth[0, 0] = np.nan
    sea = np.zeros((3, 3), dtype=bool)
    sea[0, 1] = True
    mask = valid_mask(depth, sea)
    assert mask.sum() == 7
    assert not mask[0, 0] and not mask[0, 1]
    assert valid_mask(depth).sum() == 8
