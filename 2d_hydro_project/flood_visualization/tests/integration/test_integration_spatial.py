"""集成·空间几何：images.json 的 parts 四角、无缝覆盖、反贴图残差 ≤0.5 源像元。

独立实现（不复用 src/floodviz/raster/paint.py）：
1. parts 的四角经纬度 → 反投影回原生 CRS → 量化成整数像元边界；
2. 用这些边界检查「无重叠、无空隙、并集=整幅」；
3. 抽样像元中心 → WGS84 → Web Mercator → 用该 part 四角做双线性反算 → 回原生连续像元坐标，
   偏差必须 ≤ 0.5 源像元（SPEC §3.1 硬门禁）。
"""

from __future__ import annotations

import numpy as np
import pytest
from helpers import FULL_FRAMES, JIULONG_RUN, SMOKE_FRAMES, load_json
from pyproj import Transformer

TOL_PX = 0.5
MERCATOR_R = 6378137.0


def _transformers(crs: str) -> tuple[Transformer, Transformer]:
    forward = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    inverse = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    return forward, inverse


def _native_to_rowcol(transform, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = transform
    det = a * e - b * d
    dx, dy = x - c, y - f
    col_f = (e * dx - b * dy) / det
    row_f = (-d * dx + a * dy) / det
    return row_f, col_f


def _to_merc(lon, lat):
    lon = np.asarray(lon, dtype=float)
    lat = np.asarray(lat, dtype=float)
    return np.column_stack(
        [np.radians(lon) * MERCATOR_R, np.log(np.tan(np.pi / 4.0 + np.radians(lat) / 2.0)) * MERCATOR_R]
    )


def _bilinear_inverse(quad: np.ndarray, pts: np.ndarray, iters: int = 60) -> np.ndarray:
    """quad: (4,2) TL,TR,BR,BL；返回 (N,2) 的 (u,v)。"""
    p0, p1, p2, p3 = quad
    basis = np.column_stack([p1 - p0, p3 - p0])
    uv = (pts - p0) @ np.linalg.inv(basis).T
    u, v = uv[:, 0].copy(), uv[:, 1].copy()
    for _ in range(iters):
        dSdu = (1 - v)[:, None] * (p1 - p0) + v[:, None] * (p2 - p3)
        dSdv = (1 - u)[:, None] * (p3 - p0) + u[:, None] * (p2 - p1)
        surf = (
            ((1 - u) * (1 - v))[:, None] * p0
            + (u * (1 - v))[:, None] * p1
            + (u * v)[:, None] * p2
            + ((1 - u) * v)[:, None] * p3
        )
        resid = surf - pts
        det = dSdu[:, 0] * dSdv[:, 1] - dSdu[:, 1] * dSdv[:, 0]
        du = (resid[:, 0] * dSdv[:, 1] - resid[:, 1] * dSdv[:, 0]) / det
        dv = (dSdu[:, 0] * resid[:, 1] - dSdu[:, 1] * resid[:, 0]) / det
        u -= du
        v -= dv
        if np.max(np.abs(du)) < 1e-15 and np.max(np.abs(dv)) < 1e-15:
            break
    return np.column_stack([u, v])


def _images(bundle) -> dict:
    return load_json(bundle / "frames" / "jiulong" / JIULONG_RUN / "images.json")


@pytest.fixture(scope="module")
def images_doc(full_bundle) -> dict:
    return _images(full_bundle)


def test_parts_cover_grid_exactly_without_overlap(images_doc) -> None:
    """parts 的四个外角反投影回原生 CRS 后必须量化到整数像元边界，且拼接严格等于整幅。"""
    grid = images_doc["grid"]
    rows, cols = grid["rows"], grid["cols"]
    transform = grid["transform"]
    _forward, inverse = _transformers(grid["crs"])
    coverage = np.zeros((rows, cols), dtype=np.int16)
    for frame in images_doc["frames"]:
        parts = frame["parts"]
        expected = images_doc["split"]["rows"] * images_doc["split"]["cols"]
        assert len(parts) == expected, f"{frame['frame_id']} parts 数 {len(parts)} != {expected}"
        for part in parts:
            coords = part["coordinates"]
            assert len(coords) == 4
            native = [inverse.transform(lon, lat) for lon, lat in coords]
            # 左上 → 右上 → 右下 → 左下
            r0, c0 = _native_to_rowcol(transform, *native[0])
            r1, c1 = _native_to_rowcol(transform, *native[2])
            for value in (r0, c0, r1, c1):
                # 四角经纬度按 7 位小数写入（≈1 cm），反投影量化误差应远小于 1 像元
                assert abs(value - round(value)) <= 1e-3, f"{part['id']} 边界非整数像元：{value}"
            r0, c0, r1, c1 = round(r0), round(c0), round(r1), round(c1)
            assert 0 <= r0 < r1 <= rows and 0 <= c0 < c1 <= cols, f"{part['id']} 窗口越界"
            coverage[r0:r1, c0:c1] += 1
            # 声明的像素尺寸必须与窗口一致
            assert part["width"] == c1 - c0, f"{part['id']} width 与几何不符"
            assert part["height"] == r1 - r0, f"{part['id']} height 与几何不符"
        break  # 单帧即可代表切分（同一 split）
    coverage_all = np.zeros((rows, cols), dtype=np.int16)
    # 只用一帧统计覆盖（全部帧用同一 split，累计会重复计数）
    for part in images_doc["frames"][0]["parts"]:
        native = [inverse.transform(lon, lat) for lon, lat in part["coordinates"]]
        r0, c0 = _native_to_rowcol(transform, *native[0])
        r1, c1 = _native_to_rowcol(transform, *native[2])
        coverage_all[round(r0) : round(r1), round(c0) : round(c1)] += 1
    assert coverage_all.min() == 1 and coverage_all.max() == 1, (
        f"parts 未严丝合缝：min={coverage_all.min()} max={coverage_all.max()}"
    )


def test_part_corners_match_grid_transform(images_doc) -> None:
    """整幅四角：第一张图 TL/TR/BR/BL 必须等于网格四角经投影后的经纬度。"""
    grid = images_doc["grid"]
    rows, cols = grid["rows"], grid["cols"]
    a, b, c, d, e, f = grid["transform"]
    forward, _inverse = _transformers(grid["crs"])
    native_corners = [
        (c, f),
        (c + a * cols, f + d * cols),
        (c + a * cols + b * rows, f + d * cols + e * rows),
        (c + b * rows, f + e * rows),
    ]
    expected = [forward.transform(x, y) for x, y in native_corners]

    # 找出位置在 (0,0) 的 part（左上角），其 TL 就是整幅 TL
    frames = images_doc["frames"]
    first = frames[0]["parts"]
    top_left_parts = [p for p in first if p["row"] == 0 and p["col"] == 0]
    assert len(top_left_parts) == 1
    part = top_left_parts[0]
    assert part["coordinates"][0] == pytest.approx([expected[0][0], expected[0][1]], abs=1e-6)
    # 右下角 part 的 BR 等于整幅 BR
    bottom_right = [
        p for p in first if p["row"] == images_doc["split"]["rows"] - 1 and p["col"] == images_doc["split"]["cols"] - 1
    ]
    assert len(bottom_right) == 1
    assert bottom_right[0]["coordinates"][2] == pytest.approx([expected[2][0], expected[2][1]], abs=1e-6)


def test_warp_residual_under_half_pixel(images_doc, full_bundle) -> None:
    """抽样像元中心 → 经纬度 → Mercator → part 四角双线性反算，偏差 ≤0.5 源像元。"""
    grid = images_doc["grid"]
    transform = grid["transform"]
    _forward, inverse = _transformers(grid["crs"])
    forward = _transformers(grid["crs"])[0]
    worst = 0.0
    samples_total = 0
    for part in images_doc["frames"][0]["parts"]:
        r0_f, c0_f = _native_to_rowcol(transform, *inverse.transform(*part["coordinates"][0]))
        r1_f, c1_f = _native_to_rowcol(transform, *inverse.transform(*part["coordinates"][2]))
        r0, c0, r1, c1 = round(r0_f), round(c0_f), round(r1_f), round(c1_f)
        # part 内规则采样像元中心（整数索引 + 0.5）
        frac = (np.arange(9) + 0.5) / 9
        cols = c0 + frac * (c1 - c0)
        rows = r0 + frac * (r1 - r0)
        cc, rr = np.meshgrid(cols, rows)
        cols_flat, rows_flat = cc.ravel(), rr.ravel()
        a, b, c, d, e, f = transform
        xs = a * cols_flat + b * rows_flat + c
        ys = d * cols_flat + e * rows_flat + f
        lon, lat = forward.transform(xs, ys)
        pts = _to_merc(lon, lat)
        quad = _to_merc(
            [p[0] for p in part["coordinates"]],
            [p[1] for p in part["coordinates"]],
        )
        uv = _bilinear_inverse(quad, pts)
        rec_col = c0 + uv[:, 0] * (c1 - c0)
        rec_row = r0 + uv[:, 1] * (r1 - r0)
        err = np.hypot(rec_col - cols_flat, rec_row - rows_flat)
        worst = max(worst, float(err.max()))
        samples_total += int(err.size)
    assert worst <= TOL_PX, f"反贴图残差 {worst:.4f} px > 门禁 {TOL_PX} px"
    print(f"[空间] {samples_total} 个抽样，最大反贴图残差 {worst:.4f} 源像元（门禁 {TOL_PX}）")


def test_images_frame_count_and_paths(full_bundle, images_doc) -> None:
    assert images_doc["kind"] == "depth_frames"
    assert len(images_doc["frames"]) == FULL_FRAMES
    assert images_doc["color_scale"]["transparent_below_m"] == 0.05
    assert [c["min_m"] for c in images_doc["color_scale"]["classes"]] == [
        0.05,
        0.15,
        0.3,
        0.5,
        1.0,
        2.0,
        5.0,
        10.0,
    ]
    for frame in images_doc["frames"]:
        for part in frame["parts"]:
            assert part["path"].startswith("frames/")
            assert (full_bundle / part["path"]).is_file(), part["path"]
    assert "max" in images_doc
    for part in images_doc["max"]["parts"]:
        assert (full_bundle / part["path"]).is_file(), part["path"]


def test_max_parts_match_frame_split(full_bundle, images_doc) -> None:
    max_parts = images_doc["max"]["parts"]
    frame_parts = images_doc["frames"][0]["parts"]
    assert len(max_parts) == len(frame_parts)
    frame_coords = {(p["row"], p["col"]): p["coordinates"] for p in frame_parts}
    for part in max_parts:
        assert frame_coords[(part["row"], part["col"])] == part["coordinates"]
        assert part["id"].startswith("max_r")


def test_png_dimensions_match_declared(images_doc, full_bundle) -> None:
    from PIL import Image

    grid = images_doc["grid"]
    rows, cols = grid["rows"], grid["cols"]
    parts = images_doc["frames"][0]["parts"]
    assert sum(p["width"] * p["height"] for p in parts) == rows * cols
    for part in parts:
        with Image.open(full_bundle / part["path"]) as img:
            assert img.size == (part["width"], part["height"]), part["path"]
            assert img.mode == "RGBA"


def test_split_choice_is_minimal(full_bundle) -> None:
    """split 必须记录在 manifest.validation.spatial，且残差达标（久隆 1×1）。"""
    spatial = load_json(full_bundle / "manifest.json")["validation"]["spatial"]
    info = spatial["jiulong"][JIULONG_RUN]
    assert info["passed"] is True
    assert info["split"] == {"rows": 1, "cols": 1}
    assert info["max_px"] <= 0.5


def test_smoke_bundle_spatial_gate(smoke_bundle) -> None:
    doc = _images(smoke_bundle)
    assert len(doc["frames"]) == SMOKE_FRAMES
    assert doc["split"] == {"rows": 1, "cols": 1}
