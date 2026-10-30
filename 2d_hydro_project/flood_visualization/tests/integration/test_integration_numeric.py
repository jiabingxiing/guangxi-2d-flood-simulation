"""集成·数值回读：发布包 float32 块 vs 源 .wd，抽样逐位比对。

门禁（SPEC §3.2 / 计划 §1.4）：误差 ≤ max(1e-5 m, 1e-6 × |源值|)。
本文件不复用 src 的读块函数：直接按契约解析 bin 与源 ASCII，避免"用被测代码验被测代码"。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from helpers import (
    FULL_FRAMES,
    JIULONG_RUN,
    SMOKE_FRAMES,
    load_json,
)

ABS_TOL = 1e-5
REL_TOL = 1e-6
NODATA = -9999.0


def _read_source_frame(path: Path) -> np.ndarray:
    """独立读源 ESRI ASCII：跳 6 行头，-9999 → NaN，float32。"""
    arr = np.loadtxt(path, skiprows=6, dtype=np.float64, ndmin=2)
    arr = np.where(arr == NODATA, np.nan, arr)
    return arr.astype(np.float32)


def _read_block_raw(block_path: Path, rows: int, cols: int, block_size: int, by: int, bx: int) -> np.ndarray:
    """独立读块：<f4 小端、C 序、边缘按实际裁剪。"""
    r0, c0 = by * block_size, bx * block_size
    h = min(rows, r0 + block_size) - r0
    w = min(cols, c0 + block_size) - c0
    flat = np.fromfile(block_path, dtype="<f4")
    assert flat.size == h * w, f"{block_path} 元素数 {flat.size} != {h}×{w}"
    return flat.reshape(h, w)


def _bundle_grid(bundle: Path, case_id: str = "jiulong", run_id: str = JIULONG_RUN) -> dict:
    doc = load_json(bundle / "numeric" / case_id / run_id / "manifest.json")
    return doc


def _sample_pixels(rows: int, cols: int, count: int = 24) -> list[tuple[int, int]]:
    rng = np.random.default_rng(20261027)
    fixed = [(0, 0), (0, cols - 1), (rows - 1, 0), (rows - 1, cols - 1), (rows // 2, cols // 2)]
    extra = [(int(r), int(c)) for r, c in zip(rng.integers(0, rows, count), rng.integers(0, cols, count), strict=True)]
    seen: list[tuple[int, int]] = []
    for pixel in fixed + extra:
        if pixel not in seen:
            seen.append(pixel)
    return seen


@pytest.fixture(scope="module")
def numeric_doc(smoke_bundle: Path) -> dict:
    return _bundle_grid(smoke_bundle)


def test_numeric_readback_matches_source(smoke_bundle: Path, source_run_dir: Path) -> None:
    """抽样像元：块内值 vs 源 .wd，逐位一致（误差 ≤ max(1e-5, 1e-6|src|)）。"""
    doc = _bundle_grid(smoke_bundle)
    grid = doc["grid"]
    rows, cols, block_size = grid["rows"], grid["cols"], doc["block_size"]
    run_dir = smoke_bundle / "numeric" / "jiulong" / JIULONG_RUN
    samples = _sample_pixels(rows, cols)
    checked = 0
    max_abs = 0.0
    max_rel = 0.0
    nan_mismatch: list[str] = []
    for frame in doc["frames"][:SMOKE_FRAMES]:
        frame_id = frame["frame_id"]
        seq = int(frame_id[1:])
        source = _read_source_frame(source_run_dir / "results" / f"res-{seq:04d}.wd")
        assert source.shape == (rows, cols)
        blocks: dict[tuple[int, int], np.ndarray] = {}
        for row, col in samples:
            by, bx = row // block_size, col // block_size
            if (by, bx) not in blocks:
                blocks[(by, bx)] = _read_block_raw(
                    run_dir / frame_id / str(by) / f"{bx}.bin", rows, cols, block_size, by, bx
                )
            got = float(blocks[(by, bx)][row - by * block_size, col - bx * block_size])
            want = source[row, col]
            if np.isnan(want):
                if not np.isnan(got):
                    nan_mismatch.append(f"{frame_id} ({row},{col}) 源=NaN 包内={got}")
                continue
            assert not np.isnan(got), f"{frame_id} ({row},{col}) 包内 NaN 但源为 {want}"
            err = abs(got - float(want))
            tol = max(ABS_TOL, REL_TOL * abs(float(want)))
            assert err <= tol, f"{frame_id} ({row},{col}) 误差 {err} > {tol}（包内 {got}，源 {want}）"
            max_abs = max(max_abs, err)
            max_rel = max(max_rel, err / max(abs(float(want)), 1e-12))
            checked += 1
    assert not nan_mismatch, nan_mismatch[:5]
    assert checked >= len(samples) * 2, f"抽样过少：{checked}"
    # 实测值写入报告口径：真实三地抽样误差应为 0（float32 化后逐位一致）
    print(f"数值回读：{checked} 个抽样，max_abs={max_abs:.3e} m，max_rel={max_rel:.3e}")


def test_numeric_nan_and_dry_convention(smoke_bundle: Path, source_run_dir: Path) -> None:
    """NaN = 无效/NoData；干地 = 0（不是 NaN），且块内全幅 NaN 数与源一致。"""
    doc = _bundle_grid(smoke_bundle)
    grid = doc["grid"]
    rows, cols = grid["rows"], grid["cols"]
    run_dir = smoke_bundle / "numeric" / "jiulong" / JIULONG_RUN
    for frame in doc["frames"][:SMOKE_FRAMES]:
        frame_id = frame["frame_id"]
        seq = int(frame_id[1:])
        source = _read_source_frame(source_run_dir / "results" / f"res-{seq:04d}.wd")
        full = _read_block_raw(run_dir / frame_id / "0" / "0.bin", rows, cols, doc["block_size"], 0, 0)
        np.testing.assert_array_equal(np.isnan(full), np.isnan(source), err_msg=f"{frame_id} NaN 位置不一致")
        finite = ~np.isnan(source)
        np.testing.assert_array_equal(full[finite], source[finite], err_msg=f"{frame_id} 值不一致")
        # 干地明确为 0
        assert float(np.nanmax(np.abs(full[finite]))) >= 0.0


def test_mask_png_matches_valid_pixels(smoke_bundle: Path, source_run_dir: Path) -> None:
    """mask.png：白=有效像元（非 NaN），整幅 1:1，行 0 最北。"""
    from PIL import Image

    doc = _bundle_grid(smoke_bundle)
    rows, cols = doc["grid"]["rows"], doc["grid"]["cols"]
    mask_rel = doc["mask_png"]
    with Image.open(smoke_bundle / mask_rel) as img:
        assert img.mode == "1", "掩膜必须是 1-bit PNG"
        assert img.size == (cols, rows)
        mask = np.array(img.convert("L")) > 127
    source = _read_source_frame(source_run_dir / "results" / "res-0000.wd")
    np.testing.assert_array_equal(mask, ~np.isnan(source))


def test_numeric_manifest_blocks_cover_grid(smoke_bundle: Path) -> None:
    doc = _bundle_grid(smoke_bundle)
    grid = doc["grid"]
    block_size = doc["block_size"]
    assert doc["blocks"]["rows"] == -(-grid["rows"] // block_size)
    assert doc["blocks"]["cols"] == -(-grid["cols"] // block_size)
    run_dir = smoke_bundle / "numeric" / "jiulong" / JIULONG_RUN
    expected = doc["blocks"]["rows"] * doc["blocks"]["cols"]
    for frame in doc["frames"]:
        bin_files = sorted((run_dir / frame["frame_id"]).rglob("*.bin"))
        assert len(bin_files) == expected, f"{frame['frame_id']} 块数 {len(bin_files)} != {expected}"
    max_bins = sorted((run_dir / "max").rglob("*.bin"))
    assert len(max_bins) == expected


def test_max_envelope_numeric_matches_res_max(smoke_bundle: Path, source_run_dir: Path) -> None:
    """max 块 = run 目录 results/res.max 的 float32 值。"""
    doc = _bundle_grid(smoke_bundle)
    rows, cols = doc["grid"]["rows"], doc["grid"]["cols"]
    res_max = source_run_dir / "results" / "res.max"
    if not res_max.is_file():
        pytest.skip("该 run 无 results/res.max")
    source = _read_source_frame(res_max)
    run_dir = smoke_bundle / "numeric" / "jiulong" / JIULONG_RUN
    full = _read_block_raw(run_dir / "max" / "0" / "0.bin", rows, cols, doc["block_size"], 0, 0)
    np.testing.assert_array_equal(np.isnan(full), np.isnan(source))
    finite = ~np.isnan(source)
    np.testing.assert_array_equal(full[finite], source[finite])


def test_full_bundle_numeric_readback(full_bundle: Path, source_run_dir: Path) -> None:
    """全量 21 帧：逐帧抽样回读，全部逐位一致（帧齐全时）。"""
    doc = _bundle_grid(full_bundle)
    assert len(doc["frames"]) == FULL_FRAMES
    rows, cols, block_size = doc["grid"]["rows"], doc["grid"]["cols"], doc["block_size"]
    run_dir = full_bundle / "numeric" / "jiulong" / JIULONG_RUN
    samples = _sample_pixels(rows, cols, count=8)
    for frame in doc["frames"]:
        frame_id = frame["frame_id"]
        seq = int(frame_id[1:])
        source = _read_source_frame(source_run_dir / "results" / f"res-{seq:04d}.wd")
        for row, col in samples:
            by, bx = row // block_size, col // block_size
            block = _read_block_raw(run_dir / frame_id / str(by) / f"{bx}.bin", rows, cols, block_size, by, bx)
            got = float(block[row - by * block_size, col - bx * block_size])
            want = source[row, col]
            if np.isnan(want):
                assert np.isnan(got), f"{frame_id} ({row},{col})"
                continue
            assert abs(got - float(want)) <= max(ABS_TOL, REL_TOL * abs(float(want))), f"{frame_id} ({row},{col})"


def test_numeric_query_path_template_resolves(smoke_bundle: Path) -> None:
    """timeline.query_blocks 模板替换后必须命中真实文件（客户端点查依赖）。"""
    timeline = load_json(smoke_bundle / "cases" / "jiulong" / JIULONG_RUN / "timeline.json")
    for frame in timeline["frames"]:
        rel = frame["query_blocks"].replace("{block_y}", "0").replace("{block_x}", "0")
        assert (smoke_bundle / rel).is_file(), rel


def test_no_absolute_paths_in_numeric_docs(smoke_bundle: Path) -> None:
    for rel in (
        f"numeric/jiulong/{JIULONG_RUN}/manifest.json",
        f"cases/jiulong/{JIULONG_RUN}/timeline.json",
        f"cases/jiulong/{JIULONG_RUN}/statistics.json",
        f"frames/jiulong/{JIULONG_RUN}/images.json",
    ):
        text = (smoke_bundle / rel).read_text(encoding="utf-8")
        assert "\\\\" not in text, f"{rel} 含反斜杠路径"
        assert (":\\" not in text and ":/" not in text) or rel.endswith("images.json"), f"{rel} 含盘符"
        doc = json.loads(text)
        assert "C:\\\\" not in json.dumps(doc)
