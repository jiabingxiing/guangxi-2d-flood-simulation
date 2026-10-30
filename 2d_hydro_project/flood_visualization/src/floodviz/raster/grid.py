"""原生模型网格：ASCII 栅格读取、网格几何、数值帧读取。

父项目 LISFLOOD-FP 输出为 ESRI ASCII（.wd / .asc / res.max）：
    6 行头 + 行优先数值（首行 = 最北行），cellsize 为米，NODATA_value=-9999。

约定（SPEC §3.2）：transform 固定为 rasterio/Affine 顺序 [a,b,c,d,e,f]，
北向上时 d<0，c/f 为左上角坐标；像元中心：
    x = a*(col+0.5) + b*(row+0.5) + c
    y = d*(col+0.5) + e*(row+0.5) + f
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

HEADER_KEYS = ("ncols", "nrows", "xllcorner", "yllcorner", "cellsize", "nodata_value")
DEFAULT_NODATA = -9999.0


class GridError(ValueError):
    """栅格头或数据不合法。"""


@dataclass(frozen=True)
class Grid:
    """原生模型网格几何（行列、6 元仿射、CRS、像元面积）。"""

    rows: int
    cols: int
    transform: tuple[float, float, float, float, float, float]
    crs: str
    cell_size_m: float
    cell_area_m2: float
    nodata: float = DEFAULT_NODATA

    @property
    def width(self) -> int:
        return self.cols

    @property
    def height(self) -> int:
        return self.rows

    @property
    def bounds_native(self) -> tuple[float, float, float, float]:
        """原生 CRS 下的 (minx, miny, maxx, maxy)。"""
        a, b, c, d, e, f = self.transform
        xs = [c, c + a * self.cols, c + b * self.rows, c + a * self.cols + b * self.rows]
        ys = [f, f + d * self.cols, f + e * self.rows, f + d * self.cols + e * self.rows]
        return (min(xs), min(ys), max(xs), max(ys))

    def pixel_center(self, row: int, col: int) -> tuple[float, float]:
        a, b, c, d, e, f = self.transform
        return (a * (col + 0.5) + b * (row + 0.5) + c, d * (col + 0.5) + e * (row + 0.5) + f)

    def xy_to_rowcol(self, x: float, y: float) -> tuple[int, int]:
        """原生坐标 → (row, col)；越界抛 GridIndexError，不夹到边缘。"""
        a, b, c, d, e, f = self.transform
        det = a * e - b * d
        if abs(det) < 1e-12:
            raise GridError(f"网格变换退化（det={det}）")
        dx, dy = x - c, y - f
        col_f = (e * dx - b * dy) / det
        row_f = (-d * dx + a * dy) / det
        row, col = math.floor(row_f), math.floor(col_f)
        if row < 0 or col < 0 or row >= self.rows or col >= self.cols:
            raise GridIndexError(f"坐标 ({x:.3f}, {y:.3f}) 落在网格外：{row=}, {col=}")
        return row, col

    def corners_native(self) -> list[tuple[float, float]]:
        """四角原生坐标，顺序：左上、右上、右下、左下（北向上）。"""
        a, b, c, d, e, f = self.transform
        x0, y0 = c, f
        x1, y1 = c + a * self.cols, f + d * self.cols
        x2, y2 = c + a * self.cols + b * self.rows, f + d * self.cols + e * self.rows
        x3, y3 = c + b * self.rows, f + e * self.rows
        return [(x0, y0), (x1, y1), (x2, y2), (x3, y3)]


class GridIndexError(ValueError):
    """坐标落在网格外。"""


def read_ascii_header(path: str | Path) -> dict[str, float]:
    """读 ESRI ASCII 头 6 行，返回小写键名 dict。"""
    header: dict[str, float] = {}
    with Path(path).open("r", encoding="utf-8", errors="replace") as fh:
        for _ in range(6):
            line = fh.readline()
            if not line:
                raise GridError(f"{path} 头部不足 6 行")
            parts = line.split()
            if len(parts) < 2:
                raise GridError(f"{path} 头部行不合法：{line!r}")
            header[parts[0].strip().lower()] = float(parts[1])
    missing = [k for k in HEADER_KEYS if k not in header]
    if missing:
        raise GridError(f"{path} 头部缺少字段 {missing}；实得 {sorted(header)}")
    return header


def header_to_grid(header: dict[str, float], crs: str, *, nodata: float | None = None) -> Grid:
    """ASCII 头 → Grid（左上角 xllcorner / yllcorner + nrows*cellsize）。"""
    ncols = int(header["ncols"])
    nrows = int(header["nrows"])
    cell = float(header["cellsize"])
    xll = float(header["xllcorner"])
    yll = float(header["yllcorner"])
    if ncols <= 0 or nrows <= 0 or cell <= 0:
        raise GridError(f"网格尺寸非法：{ncols}x{nrows} @ {cell}")
    nd = float(header.get("nodata_value", DEFAULT_NODATA)) if nodata is None else float(nodata)
    return Grid(
        rows=nrows,
        cols=ncols,
        transform=(cell, 0.0, xll, 0.0, -cell, yll + nrows * cell),
        crs=crs,
        cell_size_m=cell,
        cell_area_m2=cell * cell,
        nodata=nd,
    )


def read_ascii_grid(
    path: str | Path,
    *,
    dtype: type[np.floating] = np.float32,
    nodata_to_nan: bool = False,
    expected_shape: tuple[int, int] | None = None,
) -> np.ndarray:
    """读 ESRI ASCII 数值主体（跳过 6 行头）。

    使用 np.loadtxt（numpy 2.x 的快速解析器；本机贵港 30 m 一帧 2.75M 值约 0.5 s，
    比 np.fromstring 快约 6 倍）。数据量小时同样适用。
    """
    path = Path(path)
    arr = np.loadtxt(path, skiprows=6, dtype=dtype, ndmin=2)
    if expected_shape is not None and arr.shape != expected_shape:
        raise GridError(f"{path} 形状 {arr.shape} 与期望 {expected_shape} 不符")
    if nodata_to_nan:
        nd = read_ascii_header(path)["nodata_value"]
        arr = np.where(arr == dtype(nd), np.nan, arr).astype(dtype, copy=False)
    return arr


def read_native_frame(path: str | Path, *, dtype: type[np.floating] = np.float32) -> np.ndarray:
    """读一帧模型水深/水位 ASCII，返回 float32 数组，NODATA(-9999) 与 NaN 统一为 NaN，干地为 0。"""
    path = Path(path)
    header = read_ascii_header(path)
    shape = (int(header["nrows"]), int(header["ncols"]))
    arr = read_ascii_grid(path, dtype=dtype, expected_shape=shape)
    nd = dtype(header["nodata_value"])
    arr[arr == nd] = np.nan
    return arr


def read_sea_mask(run_dir: str | Path, shape: tuple[int, int]) -> np.ndarray | None:
    """读 run 目录 sea_mask.npy（若存在）；返回 bool 数组（True=海域像元）。"""
    path = Path(run_dir) / "sea_mask.npy"
    if not path.is_file():
        return None
    mask = np.load(path)
    if mask.shape != shape:
        raise GridError(f"{path} 形状 {mask.shape} 与网格 {shape} 不符")
    return np.asarray(mask).astype(bool)


def valid_mask(depth: np.ndarray, sea_mask: np.ndarray | None = None) -> np.ndarray:
    """有效像元：非 NaN 且非海域。"""
    mask = ~np.isnan(depth)
    if sea_mask is not None:
        mask &= ~sea_mask
    return mask
