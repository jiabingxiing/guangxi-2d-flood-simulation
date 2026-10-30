"""数值保真层：原生网格 float32 分块二进制 + 有效掩膜 PNG + numeric manifest。

契约（SPEC §3.2 / §7）：

    numeric/<case>/<run>/manifest.json
    numeric/<case>/<run>/mask.png
    numeric/<case>/<run>/<frame_id>/<block_y>/<block_x>.bin
    numeric/<case>/<run>/max/<block_y>/<block_x>.bin

- 块大小 256，边缘块按实际裁剪；块内 C 序；float32 小端；NaN = 无效/NoData；干地 = 0。
- 块坐标 `block_y/block_x` 自 0 起，覆盖
  `rows [by*256, min(rows,(by+1)*256)) x cols [bx*256, min(cols,(bx+1)*256))`；
  块内索引 `(r-r0)*w + (c-c0)`。
- `mask.png`：1-bit PNG、整幅 1:1、行 0 = 最北行、值 255 = 有效像元。
  口径 = **非 NaN**。海域像元仍保留真实值（模型已把海面钳 0），只在统计与着色中剔除。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

BLOCK_SIZE = 256
DTYPE = np.dtype("<f4")
LAYOUT_TEXT = (
    "block rows [by*256, min(rows,(by+1)*256)) x cols [bx*256, min(cols,(bx+1)*256)); C order; index=(r-r0)*w+(c-c0)"
)
MASK_ENCODING = "1-bit PNG, full grid 1:1, row 0 north, pixel value 255=valid"
MASK_NOTE = (
    "mask=non-NaN；海域像元仅从统计剔除，点查仍返回真实值（模型已钳 0）。"
    "海面像元在源 .wd 中即为 0（干地），故着色透明。"
)


class BlocksError(ValueError):
    """数值块写出/读取不合法。"""


def block_counts(rows: int, cols: int, block_size: int = BLOCK_SIZE) -> tuple[int, int]:
    """返回 (块行数, 块列数)。"""
    if rows <= 0 or cols <= 0 or block_size <= 0:
        raise BlocksError(f"非法块参数：rows={rows} cols={cols} block_size={block_size}")
    return math.ceil(rows / block_size), math.ceil(cols / block_size)


def iter_block_windows(
    rows: int, cols: int, block_size: int = BLOCK_SIZE
) -> Iterator[tuple[int, int, int, int, int, int]]:
    """按行主序产出 (block_y, block_x, r0, r1, c0, c1)。"""
    nby, nbx = block_counts(rows, cols, block_size)
    for by in range(nby):
        r0 = by * block_size
        r1 = min(rows, r0 + block_size)
        for bx in range(nbx):
            c0 = bx * block_size
            c1 = min(cols, c0 + block_size)
            yield by, bx, r0, r1, c0, c1


def frame_block_dir(numeric_run_dir: Path, frame_id: str) -> Path:
    return Path(numeric_run_dir) / frame_id


def block_path(block_dir: Path, block_y: int, block_x: int) -> Path:
    return Path(block_dir) / str(block_y) / f"{block_x}.bin"


def write_blocks(
    array: np.ndarray,
    block_dir: str | Path,
    *,
    block_size: int = BLOCK_SIZE,
) -> dict[str, Any]:
    """把二维数组写成 float32 小端 256 分块；返回块统计。"""
    arr = np.asarray(array)
    if arr.ndim != 2:
        raise BlocksError(f"只接受二维数组，实得 shape={arr.shape}")
    rows, cols = arr.shape
    block_dir = Path(block_dir)
    block_dir.mkdir(parents=True, exist_ok=True)
    nbytes = 0
    nby, nbx = block_counts(rows, cols, block_size)
    for by, bx, r0, r1, c0, c1 in iter_block_windows(rows, cols, block_size):
        block = np.ascontiguousarray(arr[r0:r1, c0:c1], dtype=DTYPE)
        path = block_path(block_dir, by, bx)
        path.parent.mkdir(parents=True, exist_ok=True)
        block.tofile(path)
        nbytes += path.stat().st_size
    return {
        "blocks_rows": nby,
        "blocks_cols": nbx,
        "block_files": nby * nbx,
        "bytes": nbytes,
    }


def read_block(
    block_dir: str | Path,
    block_y: int,
    block_x: int,
    *,
    rows: int,
    cols: int,
    block_size: int = BLOCK_SIZE,
) -> np.ndarray:
    """读回一个块（float32 小端，C 序），形状按边缘裁剪。"""
    r0 = block_y * block_size
    c0 = block_x * block_size
    if r0 >= rows or c0 >= cols:
        raise BlocksError(f"块 ({block_y},{block_x}) 超出网格 {rows}x{cols}")
    h = min(rows, r0 + block_size) - r0
    w = min(cols, c0 + block_size) - c0
    path = block_path(block_dir, block_y, block_x)
    flat = np.fromfile(path, dtype=DTYPE)
    if flat.size != h * w:
        raise BlocksError(f"{path} 元素数 {flat.size} != 期望 {h}x{w}={h * w}")
    return flat.reshape(h, w)


def read_frame(block_dir: str | Path, *, rows: int, cols: int, block_size: int = BLOCK_SIZE) -> np.ndarray:
    """把整帧块拼回二维数组（只用于校验/测试，发布链路保持流式）。"""
    out = np.empty((rows, cols), dtype=np.float32)
    for by, bx, r0, r1, c0, c1 in iter_block_windows(rows, cols, block_size):
        out[r0:r1, c0:c1] = read_block(block_dir, by, bx, rows=rows, cols=cols, block_size=block_size)
    return out


def read_value(
    block_dir: str | Path,
    row: int,
    col: int,
    *,
    rows: int,
    cols: int,
    block_size: int = BLOCK_SIZE,
) -> float:
    """点查：按行/列号取原生值（越界抛错，不夹边）。"""
    if not (0 <= row < rows and 0 <= col < cols):
        raise BlocksError(f"({row},{col}) 在网格外 {rows}x{cols}")
    by, bx = row // block_size, col // block_size
    block = read_block(block_dir, by, bx, rows=rows, cols=cols, block_size=block_size)
    return float(block[row - by * block_size, col - bx * block_size])


def write_mask_png(valid: np.ndarray, path: str | Path) -> int:
    """写 1-bit PNG 有效掩膜（白 = 有效）；返回字节数。"""
    arr = np.asarray(valid)
    if arr.ndim != 2:
        raise BlocksError(f"掩膜必须是二维，实得 {arr.shape}")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    binary = arr.astype(bool)
    img = Image.fromarray(np.where(binary, 255, 0).astype(np.uint8), mode="L").convert("1", dither=Image.Dither.NONE)
    img.save(path, format="PNG", optimize=False, compress_level=9)
    return path.stat().st_size


def read_mask_png(path: str | Path) -> np.ndarray:
    """读回有效掩膜（bool，白 = 有效）。"""
    with Image.open(path) as img:
        arr = np.array(img.convert("L"), dtype=np.uint8)
    return arr > 127


def grid_hash(grid: Any) -> str:
    """网格指纹：rows/cols/transform/crs 规范化 JSON 后 sha256（32 位十六进制前缀）。"""
    payload = {
        "rows": int(grid.rows),
        "cols": int(grid.cols),
        "transform": [round(float(v), 9) for v in grid.transform],
        "crs": str(grid.crs),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def build_numeric_manifest(
    *,
    case_id: str,
    run_id: str,
    grid: Any,
    block_size: int,
    frames: list[Any],
    mask_png_rel: str,
    sea_cell_count: int,
    has_max: bool,
) -> dict[str, Any]:
    """构造 `numeric/<case>/<run>/manifest.json`（SPEC §7）。"""
    nby, nbx = block_counts(grid.rows, grid.cols, block_size)
    prefix = f"numeric/{case_id}/{run_id}"
    doc: dict[str, Any] = {
        "schema_version": "1.0",
        "case_id": case_id,
        "run_id": run_id,
        "grid": {
            "crs": grid.crs,
            "rows": int(grid.rows),
            "cols": int(grid.cols),
            "shape": [int(grid.rows), int(grid.cols)],
            "transform": [float(v) for v in grid.transform],
            "cell_size_m": float(grid.cell_size_m),
            "cell_area_m2": float(grid.cell_area_m2),
            "grid_hash": grid_hash(grid),
        },
        "block_size": int(block_size),
        "blocks": {"rows": nby, "cols": nbx},
        "dtype": "<f4",
        "byte_order": "little",
        "nodata": "nan",
        "layout": LAYOUT_TEXT,
        "mask_png": mask_png_rel,
        "mask_encoding": MASK_ENCODING,
        "mask_note": MASK_NOTE,
        "sea_cell_count": int(sea_cell_count),
        "query_hint": (
            "lon/lat → proj4 到 crs → 逆仿射 → row=floor, col=floor；越界返回“域外”（不夹边）；"
            "value=(row-by*256)*bw+(col-bx*256)"
        ),
        "frames": [
            {"frame_id": frame.frame_id, "path_template": f"{prefix}/{frame.frame_id}/{{block_y}}/{{block_x}}.bin"}
            for frame in frames
        ],
    }
    if has_max:
        doc["max"] = {"path_template": f"{prefix}/max/{{block_y}}/{{block_x}}.bin"}
    return doc
