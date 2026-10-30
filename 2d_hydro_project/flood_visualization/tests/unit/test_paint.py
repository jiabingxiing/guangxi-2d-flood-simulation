"""显示层单元测试：固定色带着色、子图切分、四角经纬度、images.json 契约。"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import numpy as np
import pytest
from PIL import Image

from conftest import PROJECT_ROOT, SYNTH_NODATA
from floodviz.raster.grid import header_to_grid
from floodviz.raster.paint import (
    DepthClass,
    PaintError,
    build_images_doc,
    check_partition,
    choose_split,
    colorize_depth,
    colorize_gradient,
    crop_rgba,
    depth_classes_from_config,
    depth_frame_summary,
    describe_scale,
    measure_part_residual_px,
    parse_color,
    part_corners_lonlat,
    part_id,
    part_windows,
    save_png,
    split_edges,
)

IMAGES_SCHEMA = PROJECT_ROOT / "schemas" / "images.schema.json"


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


def _affine(x: float, y: float) -> tuple[float, float]:
    return (x * 1e-5 + 100.0, y * 1e-5)


# ---------------------------------------------------------------- 颜色


def test_parse_color() -> None:
    assert parse_color("#b7e4c7") == (183, 228, 199)
    assert parse_color("b7e4c7") == (183, 228, 199)
    with pytest.raises(PaintError, match="颜色必须是 #RRGGBB"):
        parse_color("#fff")
    with pytest.raises(PaintError, match="颜色"):
        parse_color("#zzzzzz")


def test_depth_classes_from_config(config) -> None:
    classes = depth_classes_from_config(config)
    assert len(classes) == 8
    assert [c.min_m for c in classes] == sorted(c.min_m for c in classes)
    assert classes[0].min_m == pytest.approx(0.05)
    assert classes[0].color == "#b7e4c7"
    assert classes[0].rgb == (183, 228, 199)
    assert classes[-1].max_m is None
    assert classes[-1].color == "#3f0071"
    assert classes[0].to_dict()["label"] == "0.05–0.15 m"


def test_colorize_depth_discrete_classes(config) -> None:
    classes = depth_classes_from_config(config)
    depth = np.array(
        [[np.nan, 0.0, 0.05, 0.051, 0.1, 0.2, 0.3, 1.5, 2.0, 12.0]],
        dtype=np.float32,
    )
    rgba = colorize_depth(depth, classes, transparent_below_m=0.05)
    assert rgba.dtype == np.uint8 and rgba.shape == (1, 10, 4)
    # NaN、干地、恰等阈值 → 全透明
    for col in (0, 1, 2):
        assert rgba[0, col, 3] == 0
    # 0.05–0.15 m → #b7e4c7
    assert tuple(rgba[0, 3, :3]) == (183, 228, 199) and rgba[0, 3, 3] == 255
    assert tuple(rgba[0, 4, :3]) == (183, 228, 199)
    # 0.15–0.30 m（0.3 落在本级上界）→ #74c69d
    assert tuple(rgba[0, 5, :3]) == (116, 198, 157)
    assert tuple(rgba[0, 6, :3]) == (116, 198, 157)
    # 1–2 m（2.0 落在本级上界）→ #f8961e；>2 m → #f3722c
    assert tuple(rgba[0, 7, :3]) == (248, 150, 30)
    assert tuple(rgba[0, 8, :3]) == (248, 150, 30)
    assert tuple(rgba[0, 9, :3]) == (63, 0, 113)


def test_colorize_depth_rejects_non_2d() -> None:
    classes = [DepthClass(0.05, None, "#000000", (0, 0, 0), "x")]
    with pytest.raises(PaintError, match="必须是二维"):
        colorize_depth(np.zeros(3, dtype=np.float32), classes)


def test_colorize_depth_all_transparent() -> None:
    classes = [DepthClass(0.05, None, "#000000", (0, 0, 0), "x")]
    rgba = colorize_depth(np.zeros((2, 2), dtype=np.float32), classes)
    assert rgba[..., 3].max() == 0


def test_colorize_gradient() -> None:
    stops = [(0, 0, 0), (255, 255, 255)]
    values = np.array([[np.nan, 0.0, 150.0, 300.0]], dtype=np.float32)
    rgba = colorize_gradient(values, stops, value_min=0.0, value_max=300.0)
    assert rgba[0, 0, 3] == 0  # NaN 透明
    assert tuple(rgba[0, 1, :3]) == (0, 0, 0) and rgba[0, 1, 3] == 255
    assert tuple(rgba[0, 3, :3]) == (255, 255, 255)
    assert 120 <= int(rgba[0, 2, 0]) <= 135


def test_colorize_gradient_flat_span() -> None:
    stops = [(10, 20, 30)]
    rgba = colorize_gradient(np.array([[5.0]], dtype=np.float32), stops, value_min=5.0, value_max=5.0)
    assert tuple(rgba[0, 0, :3]) == (10, 20, 30)
    assert rgba[0, 0, 3] == 255


# ---------------------------------------------------------------- 切分


def test_split_edges() -> None:
    assert split_edges(8, 1) == [0, 8]
    assert split_edges(8, 2) == [0, 4, 8]
    assert split_edges(1233, 2) == [0, 616, 1233]
    assert split_edges(6, 3) == [0, 2, 4, 6]
    with pytest.raises(PaintError, match="必须为正"):
        split_edges(8, 0)
    with pytest.raises(PaintError, match="大于网格边长"):
        split_edges(3, 4)


def test_part_windows_cover_exactly() -> None:
    windows = part_windows(6, 8, 2, 2)
    assert len(windows) == 4
    assert [(w.part_row, w.part_col) for w in windows] == [(0, 0), (0, 1), (1, 0), (1, 1)]
    assert (windows[0].r0, windows[0].r1, windows[0].c0, windows[0].c1) == (0, 3, 0, 4)
    assert (windows[-1].r0, windows[-1].r1, windows[-1].c0, windows[-1].c1) == (3, 6, 4, 8)
    assert windows[0].height == 3 and windows[0].width == 4
    check_partition(6, 8, 2, 2)


def test_part_id_naming() -> None:
    window = part_windows(6, 8, 2, 2)[3]
    assert part_id("f0000", window) == "f0000_r1c1"
    assert part_id("max", window) == "max_r1c1"


def test_part_corners_lonlat_order() -> None:
    grid = _grid()
    window = part_windows(6, 8, 2, 2)[0]  # TL 子图
    corners = part_corners_lonlat(grid, window, _affine)
    assert len(corners) == 4
    lons = [c[0] for c in corners]
    lats = [c[1] for c in corners]
    # 左上 → 右上 → 右下 → 左下
    assert corners[0] == pytest.approx([min(lons), max(lats)])
    assert corners[1] == pytest.approx([max(lons), max(lats)])
    assert corners[2] == pytest.approx([max(lons), min(lats)])
    assert corners[3] == pytest.approx([min(lons), min(lats)])
    # 整个网格的四角与 grid.corners_native 变换结果一致
    full = part_windows(6, 8, 1, 1)[0]
    full_corners = part_corners_lonlat(grid, full, _affine)
    expected = [_affine(x, y) for x, y in grid.corners_native()]
    assert np.allclose(np.array(full_corners), np.array(expected), atol=1e-9)


def test_residual_and_choose_split_for_small_grid() -> None:
    grid = _grid()
    window = part_windows(6, 8, 1, 1)[0]
    detail = measure_part_residual_px(grid, window, _affine, samples=5)
    assert detail["samples"] == 25
    assert detail["max_px"] < 0.01, "仿射投影下 1×1 残差应接近 0"
    chosen = choose_split(grid, _affine)
    assert chosen["split_rows"] == 1 and chosen["split_cols"] == 1
    assert chosen["max_px"] <= 0.5
    assert chosen["tol_px"] == 0.5
    assert chosen["attempts"][0]["split_rows"] == 1


def test_measure_residual_rejects_too_few_samples() -> None:
    grid = _grid()
    window = part_windows(6, 8, 1, 1)[0]
    with pytest.raises(PaintError, match="samples 至少 3"):
        measure_part_residual_px(grid, window, _affine, samples=2)


# ---------------------------------------------------------------- images.json


def _images_doc() -> dict:
    grid = _grid()
    classes = [
        DepthClass(0.05, 0.15, "#b7e4c7", (183, 228, 199), "0.05–0.15 m"),
        DepthClass(0.15, None, "#74c69d", (116, 198, 157), "≥0.15 m"),
    ]
    windows = part_windows(6, 8, 1, 1)
    part = {
        "id": part_id("f0000", windows[0]),
        "row": 0,
        "col": 0,
        "path": "frames/demo/run_a/f0000_r0c0.png",
        "width": 8,
        "height": 6,
        "coordinates": part_corners_lonlat(grid, windows[0], _affine),
        "max_depth_m": 0.5,
        "wet_pixels": 3,
    }
    return build_images_doc(
        case_id="demo",
        run_id="run_a",
        grid=grid,
        classes=classes,
        transparent_below_m=0.05,
        split_rows=1,
        split_cols=1,
        frames=[{"frame_id": "f0000", "max_depth_m": 0.5, "wet_pixels": 3, "parts": [part]}],
        max_entry={
            "max_depth_m": 1.0,
            "wet_pixels": 4,
            "parts": [dict(part, id="max_r0c0", path="frames/demo/run_a/max_r0c0.png")],
        },
    )


def test_build_images_doc_matches_schema() -> None:
    doc = _images_doc()
    jsonschema.validate(doc, json.loads(IMAGES_SCHEMA.read_text(encoding="utf-8")))
    assert doc["kind"] == "depth_frames"
    assert doc["split"] == {"rows": 1, "cols": 1}
    assert doc["part_order"] == "row major"
    assert doc["grid"]["rows"] == 6
    assert doc["color_scale"]["transparent_below_m"] == 0.05
    assert doc["frames"][0]["parts"][0]["path"] == "frames/demo/run_a/f0000_r0c0.png"


def test_build_images_doc_without_max_keeps_kind() -> None:
    grid = _grid()
    doc = build_images_doc(
        case_id="demo",
        run_id="run_a",
        grid=grid,
        classes=[DepthClass(0.05, None, "#000000", (0, 0, 0), "x")],
        transparent_below_m=0.05,
        split_rows=1,
        split_cols=1,
        frames=[
            {
                "frame_id": "f0000",
                "max_depth_m": 0.0,
                "wet_pixels": 0,
                "parts": [
                    {
                        "id": "f0000_r0c0",
                        "row": 0,
                        "col": 0,
                        "path": "frames/demo/run_a/f0000_r0c0.png",
                        "width": 8,
                        "height": 6,
                        "coordinates": part_corners_lonlat(grid, part_windows(6, 8, 1, 1)[0], _affine),
                    }
                ],
            }
        ],
        max_entry=None,
    )
    assert "max" not in doc


# ---------------------------------------------------------------- 写盘


def test_save_png_roundtrip(tmp_path: Path) -> None:
    rgba = np.zeros((6, 8, 4), dtype=np.uint8)
    rgba[..., 3] = 255
    rgba[0, 0] = (183, 228, 199, 255)
    path = tmp_path / "f0000_r0c0.png"
    nbytes = save_png(rgba, path)
    assert nbytes == path.stat().st_size
    with Image.open(path) as img:
        assert img.size == (8, 6)
        assert img.mode == "RGBA"
        pixels = np.array(img)
    np.testing.assert_array_equal(pixels, rgba)


def test_save_png_rejects_bad_array(tmp_path: Path) -> None:
    with pytest.raises(PaintError, match="需要 uint8 RGBA"):
        save_png(np.zeros((3, 3, 3), dtype=np.uint8), tmp_path / "bad.png")


def test_crop_rgba() -> None:
    rgba = np.zeros((6, 8, 4), dtype=np.uint8)
    rgba[..., 0] = np.arange(8, dtype=np.uint8)
    window = part_windows(6, 8, 2, 2)[3]
    cropped = crop_rgba(rgba, window)
    assert cropped.shape == (3, 4, 4)
    assert cropped[0, 0, 0] == 4


def test_depth_frame_summary() -> None:
    depth = np.array([[np.nan, 0.01, 0.2, 1.0]], dtype=np.float32)
    summary = depth_frame_summary(depth, transparent_below_m=0.05)
    assert summary == {"max_depth_m": 1.0, "wet_pixels": 2}


def test_describe_scale() -> None:
    assert describe_scale(200.0) == "200.0 m"
    assert describe_scale(1500.0) == "1.50 km"
