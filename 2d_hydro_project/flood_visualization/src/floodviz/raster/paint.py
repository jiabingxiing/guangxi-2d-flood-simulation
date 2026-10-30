"""显示层：固定色带预着色 PNG（原生网格 1:1）+ 自适应子图 + 四角经纬度。

用户 2026-10-27 决定：不逐帧生成 XYZ 瓦片金字塔，直接复用已有 6 h 间隔的真实推演帧，
每帧只渲染原生网格 1:1 的预着色 PNG，用 MapLibre `image` source 按四角经纬度贴图。

空间门禁（Lead 2026-10-27 裁决）：单张 image source 的四角双线性 warp 无法表达 UTM
等东坐标线在经纬度空间的弯曲；当实测反贴图残差 > 0.5 源像元时，**沿整数像元边界**把
网格切成 `split_rows × split_cols` 个子图分别贴图（不重采样、不插值、无重叠、严丝合缝）。
久隆 200 m / 宁明 100 m / 贵港 200 m 为 1×1；贵港 30 m 实测 1.261 px → 沿列切 1×2 后 0.332 px。
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

MERCATOR_R = 6378137.0
DEFAULT_TOL_PX = 0.5
SPLIT_CANDIDATES: tuple[tuple[int, int], ...] = (
    (1, 1),
    (1, 2),
    (2, 1),
    (2, 2),
    (1, 3),
    (3, 1),
    (1, 4),
    (4, 1),
    (2, 3),
    (3, 2),
    (3, 3),
    (1, 6),
    (6, 1),
    (2, 4),
    (4, 2),
    (4, 4),
    (1, 8),
    (8, 1),
    (4, 6),
    (6, 4),
    (6, 6),
    (8, 8),
)


class PaintError(ValueError):
    """着色/子图几何不合法。"""


@dataclass(frozen=True)
class DepthClass:
    """深度分级；max_m=None 表示开区间上界。"""

    min_m: float
    max_m: float | None
    color: str
    rgb: tuple[int, int, int]
    label: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_m": self.min_m,
            "max_m": self.max_m,
            "color": self.color,
            "label": self.label,
        }


def parse_color(value: str) -> tuple[int, int, int]:
    text = str(value).strip().lstrip("#")
    if len(text) != 6:
        raise PaintError(f"颜色必须是 #RRGGBB：{value!r}")
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError as exc:  # pragma: no cover - 配置错误路径
        raise PaintError(f"颜色不合法：{value!r}") from exc


def depth_classes_from_config(config: Any) -> list[DepthClass]:
    """从 Config.depth_classes（default.yaml）构造固定色带。"""
    classes: list[DepthClass] = []
    for raw in config.depth_classes:
        classes.append(
            DepthClass(
                min_m=float(raw["min_m"]),
                max_m=None if raw.get("max_m") is None else float(raw["max_m"]),
                color=str(raw["color"]),
                rgb=parse_color(raw["color"]),
                label=str(raw.get("label", "")),
            )
        )
    if not classes:
        raise PaintError("depth_classes 为空（无法着色）")
    classes.sort(key=lambda c: c.min_m)
    return classes


def colorize_depth(
    depth: np.ndarray,
    classes: Sequence[DepthClass],
    *,
    transparent_below_m: float = 0.05,
) -> np.ndarray:
    """水深 → RGBA uint8。`depth <= transparent_below_m` 或 NaN → 全透明。"""
    arr = np.asarray(depth)
    if arr.ndim != 2:
        raise PaintError(f"水深必须是二维，实得 {arr.shape}")
    rgba = np.zeros((arr.shape[0], arr.shape[1], 4), dtype=np.uint8)
    below = arr <= np.float32(transparent_below_m)
    finite = np.isfinite(arr)
    usable = finite & ~below
    if not usable.any():
        return rgba
    # 逐级上色；后一级覆盖前一级的边界，保证分级互斥且首级 >transparent_below_m
    for cls in classes:
        if cls.max_m is None:
            mask = usable & (arr > np.float32(cls.min_m))
        else:
            mask = usable & (arr > np.float32(cls.min_m)) & (arr <= np.float32(cls.max_m))
        if not mask.any():
            continue
        r, g, b = cls.rgb
        rgba[..., 0][mask] = r
        rgba[..., 1][mask] = g
        rgba[..., 2][mask] = b
        rgba[..., 3][mask] = 255
    return rgba


DEFAULT_ARCGIS_ELEVATION_STOPS: tuple[str, ...] = (
    "#267300",  # 深绿（低谷/水滨）
    "#38a800",  # 翠绿（平原低地）
    "#70a800",  # 黄绿（缓坡丘陵）
    "#a8c400",  # 浅黄绿 / 柠檬绿
    "#ffd300",  # 亮暖黄（低中山过渡）
    "#e69800",  # 金橙（中山）
    "#e67300",  # 橙红（高山）
    "#a84700",  # 陶土褐（峻岭）
    "#732600",  # 深山褐（高海拔山体）
    "#998273",  # 岩石板岩灰（山脊石质带）
    "#e0e0e0",  # 浅岩灰（极高岭）
    "#ffffff",  # 纯白（最高雪线/峰顶）
)


def parse_color_stops(stops: Sequence[str]) -> list[tuple[int, int, int]]:
    return [parse_color(s) for s in stops]


def colorize_gradient(
    values: np.ndarray,
    stops: Sequence[tuple[int, int, int]],
    *,
    value_min: float,
    value_max: float,
) -> np.ndarray:
    """连续色带（DEM）：NaN → 透明；最近邻口径由调用方保证（不做双线性造高）。"""
    arr = np.asarray(values, dtype=np.float32)
    if arr.ndim != 2:
        raise PaintError(f"栅格必须是二维，实得 {arr.shape}")
    rgba = np.zeros((arr.shape[0], arr.shape[1], 4), dtype=np.uint8)
    finite = np.isfinite(arr)
    if not finite.any() or not stops:
        return rgba
    span = float(value_max) - float(value_min)
    if span <= 0:
        t = np.zeros(arr.shape, dtype=np.float32)
    else:
        t = np.clip((arr - np.float32(value_min)) / np.float32(span), 0.0, 1.0)
    n = len(stops)
    pos = np.clip(t * (n - 1), 0.0, n - 1.0)
    pos = np.where(finite, pos, 0.0).astype(np.float32)
    idx = np.floor(pos).astype(np.int16)
    frac = (pos - idx).astype(np.float32)
    idx_next = np.clip(idx + 1, 0, n - 1)
    col = np.array(stops, dtype=np.float32)
    rgb = col[idx] * (1.0 - frac[..., None]) + col[idx_next] * frac[..., None]
    rgba[..., :3] = np.rint(np.nan_to_num(rgb, nan=0.0)).astype(np.uint8)
    rgba[..., 3] = np.where(finite, 255, 0).astype(np.uint8)
    return rgba


def split_edges(total: int, parts: int) -> list[int]:
    """把 [0,total) 沿整数边界等分为 parts 段；两端严格 0 与 total。"""
    if parts <= 0:
        raise PaintError(f"split 段数必须为正：{parts}")
    if parts > total:
        raise PaintError(f"split 段数 {parts} 大于网格边长 {total}")
    edges = [round(i * total / parts) for i in range(parts + 1)]
    edges[0], edges[-1] = 0, total
    if any(b <= a for a, b in itertools.pairwise(edges)):
        raise PaintError(f"split 边界非法：{edges}（total={total}, parts={parts}）")
    return edges


@dataclass(frozen=True)
class PartWindow:
    """子图在原生网格上的整数像元窗口（右/下开区间）。"""

    part_row: int
    part_col: int
    r0: int
    r1: int
    c0: int
    c1: int

    @property
    def height(self) -> int:
        return self.r1 - self.r0

    @property
    def width(self) -> int:
        return self.c1 - self.c0

    def slice(self) -> tuple[slice, slice]:
        return slice(self.r0, self.r1), slice(self.c0, self.c1)


def part_windows(rows: int, cols: int, split_rows: int, split_cols: int) -> list[PartWindow]:
    """按行主序（row 主序、col 次序）产出全部子图窗口，覆盖严格等于整幅。"""
    redges = split_edges(rows, split_rows)
    cedges = split_edges(cols, split_cols)
    out: list[PartWindow] = []
    for pr in range(split_rows):
        for pc in range(split_cols):
            out.append(
                PartWindow(
                    part_row=pr,
                    part_col=pc,
                    r0=redges[pr],
                    r1=redges[pr + 1],
                    c0=cedges[pc],
                    c1=cedges[pc + 1],
                )
            )
    return out


def part_id(frame_id: str, window: PartWindow) -> str:
    return f"{frame_id}_r{window.part_row}c{window.part_col}"


def _to_merc(lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
    x = np.radians(lon) * MERCATOR_R
    y = np.log(np.tan(np.pi / 4.0 + np.radians(lat) / 2.0)) * MERCATOR_R
    return np.column_stack([x, y])


def transform_points(
    fn: Callable[[float, float], tuple[float, float]],
    xs: np.ndarray,
    ys: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """调用坐标变换：优先批量（pyproj Transformer 风格），失败退回逐点标量调用。"""
    try:
        out = fn(xs, ys)  # type: ignore[arg-type]
        lon = np.asarray(out[0], dtype=float)
        lat = np.asarray(out[1], dtype=float)
        if lon.shape == np.asarray(xs).shape and lat.shape == np.asarray(ys).shape:
            return lon, lat
    except (TypeError, ValueError):
        pass
    pairs = [fn(float(x), float(y)) for x, y in zip(np.asarray(xs).ravel(), np.asarray(ys).ravel(), strict=False)]
    lon = np.array([p[0] for p in pairs], dtype=float).reshape(np.asarray(xs).shape)
    lat = np.array([p[1] for p in pairs], dtype=float).reshape(np.asarray(ys).shape)
    return lon, lat


def part_corners_lonlat(
    grid: Any,
    window: PartWindow,
    crs_to_wgs84: Callable[[float, float], tuple[float, float]],
    *,
    precision: int = 7,
) -> list[list[float]]:
    """子图四角经纬度，顺序 = 左上、右上、右下、左下（pixel-is-area 外角）。"""
    a, b, c, d, e, f = (float(v) for v in grid.transform)

    def native(col: float, row: float) -> tuple[float, float]:
        return a * col + b * row + c, d * col + e * row + f

    pts = [
        native(window.c0, window.r0),
        native(window.c1, window.r0),
        native(window.c1, window.r1),
        native(window.c0, window.r1),
    ]
    corners = []
    for x, y in pts:
        lon, lat = crs_to_wgs84(x, y)
        corners.append([round(float(lon), precision), round(float(lat), precision)])
    return corners


def _bilinear_inverse(quad: np.ndarray, pts: np.ndarray, *, iters: int = 60) -> np.ndarray:
    """quad: (4,2) TL,TR,BR,BL；pts: (N,2)。返回 (N,2) 的 (u,v) ∈ [0,1]。"""
    p0, p1, p2, p3 = quad[0], quad[1], quad[2], quad[3]
    basis = np.column_stack([p1 - p0, p3 - p0])
    try:
        uv = (pts - p0) @ np.linalg.inv(basis).T
    except np.linalg.LinAlgError:  # pragma: no cover - 退化网格
        raise PaintError("子图四角退化，无法反算贴图坐标") from None
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


def measure_part_residual_px(
    grid: Any,
    window: PartWindow,
    crs_to_wgs84: Callable[[float, float], tuple[float, float]],
    *,
    samples: int = 13,
) -> dict[str, Any]:
    """实测子图反贴图残差（源像元单位）。

    在子图内取规则采样像元中心：(col+0.5,row+0.5) 原生坐标 → proj 到 WGS84 →
    Web Mercator（MapLibre image source 的渲染空间）→ 用子图四角双线性反算 → 回到
    原生连续像元坐标，与真值比较。返回最大偏差与采样明细。
    """
    if samples < 3:
        raise PaintError(f"samples 至少 3，实得 {samples}")
    a, b, c, d, e, f = (float(v) for v in grid.transform)
    # 连续像元坐标（像元中心 = 整数索引 + 0.5）
    frac = (np.arange(samples) + 0.5) / samples
    col_f = window.c0 + frac * window.width
    row_f = window.r0 + frac * window.height
    cc, rr = np.meshgrid(col_f, row_f)
    cols = cc.ravel()
    rows = rr.ravel()
    xs = a * cols + b * rows + c
    ys = d * cols + e * rows + f
    lon, lat = transform_points(crs_to_wgs84, xs, ys)
    pts = _to_merc(lon, lat)

    corners = part_corners_lonlat(grid, window, crs_to_wgs84, precision=12)
    quad = _to_merc(
        np.array([p[0] for p in corners], dtype=float),
        np.array([p[1] for p in corners], dtype=float),
    )
    uv = _bilinear_inverse(quad, pts)
    rec_col = window.c0 + uv[:, 0] * window.width
    rec_row = window.r0 + uv[:, 1] * window.height
    err_col = np.abs(rec_col - cols)
    err_row = np.abs(rec_row - rows)
    err_px = np.hypot(err_col, err_row)
    worst = int(np.argmax(err_px))
    return {
        "max_px": float(err_px[worst]),
        "max_m": float(err_px[worst]) * float(grid.cell_size_m),
        "max_col_px": float(err_col.max()),
        "max_row_px": float(err_row.max()),
        "rms_px": float(np.sqrt(np.mean(err_px**2))),
        "samples": int(err_px.size),
        "worst_at": {"row": float(rows[worst]), "col": float(cols[worst])},
        "split_rows": 0,
        "split_cols": 0,
    }


def check_partition(rows: int, cols: int, split_rows: int, split_cols: int) -> None:
    """断言子图严丝合缝：无重叠、无空隙、覆盖等于整幅。"""
    coverage = np.zeros((rows, cols), dtype=np.int16)
    for win in part_windows(rows, cols, split_rows, split_cols):
        coverage[win.r0 : win.r1, win.c0 : win.c1] += 1
    if coverage.min() != 1 or coverage.max() != 1:
        bad = int((coverage != 1).sum())
        raise PaintError(f"子图未严丝合缝覆盖整幅（异常像元 {bad} 个）")


def choose_split(
    grid: Any,
    crs_to_wgs84: Callable[[float, float], tuple[float, float]],
    *,
    tol_px: float = DEFAULT_TOL_PX,
    samples: int = 13,
    candidates: Sequence[tuple[int, int]] = SPLIT_CANDIDATES,
) -> dict[str, Any]:
    """自适应选择子图切分：从 1×1 起，实测残差全部 ≤ tol 即采用；否则继续拆。

    候选按"子图总数最少 → 边长最均衡"排序，保证在满足 ≤tol 的前提下用最少的贴图数量
    （贵港 30 m 实测沿列切 1×2 已达 0.33 px，故不必拆成 2×2）。
    """
    ordered = sorted(set(candidates), key=lambda rc: (rc[0] * rc[1], max(rc[0], rc[1]), rc))
    attempts: list[dict[str, Any]] = []
    for split_rows, split_cols in ordered:
        if split_rows > grid.rows or split_cols > grid.cols:
            continue
        check_partition(grid.rows, grid.cols, split_rows, split_cols)
        worst = 0.0
        worst_part = None
        parts: list[dict[str, Any]] = []
        for win in part_windows(grid.rows, grid.cols, split_rows, split_cols):
            detail = measure_part_residual_px(grid, win, crs_to_wgs84, samples=samples)
            detail["part_row"] = win.part_row
            detail["part_col"] = win.part_col
            parts.append(detail)
            if detail["max_px"] > worst:
                worst = detail["max_px"]
                worst_part = win
        attempts.append(
            {
                "split_rows": split_rows,
                "split_cols": split_cols,
                "max_px": worst,
                "max_m": worst * float(grid.cell_size_m),
                "parts": parts,
                "worst_part": None if worst_part is None else [worst_part.part_row, worst_part.part_col],
            }
        )
        if worst <= tol_px:
            return {
                "split_rows": split_rows,
                "split_cols": split_cols,
                "tol_px": tol_px,
                "max_px": worst,
                "max_m": worst * float(grid.cell_size_m),
                "samples_per_part": samples,
                "parts": parts,
                "attempts": [{k: a[k] for k in ("split_rows", "split_cols", "max_px", "max_m")} for a in attempts],
            }
    raise PaintError(
        f"最多拆到 {ordered[-1]} 仍超过 {tol_px} 源像元；实测："
        + ", ".join(f"{a['split_rows']}x{a['split_cols']}={a['max_px']:.3f}px" for a in attempts)
    )


def save_png(rgba: np.ndarray, path: str | Path, *, compress_level: int = 6) -> int:
    """写 RGBA PNG（不优化调色板，保持与固定色带逐像素一致）；返回字节数。"""
    arr = np.asarray(rgba)
    if arr.dtype != np.uint8 or arr.ndim != 3 or arr.shape[2] != 4:
        raise PaintError(f"需要 uint8 RGBA 数组，实得 {arr.dtype} {arr.shape}")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr, mode="RGBA").save(path, format="PNG", optimize=False, compress_level=compress_level)
    return path.stat().st_size


def crop_rgba(rgba: np.ndarray, window: PartWindow) -> np.ndarray:
    return rgba[window.r0 : window.r1, window.c0 : window.c1]


def depth_frame_summary(depth: np.ndarray, *, transparent_below_m: float) -> dict[str, Any]:
    """单帧摘要：最大水深与着色像元数（> 透明阈值）。"""
    arr = np.asarray(depth)
    finite = np.isfinite(arr)
    wet = int(np.count_nonzero(finite & (arr > np.float32(transparent_below_m))))
    max_depth = float(np.nanmax(arr)) if finite.any() else 0.0
    return {"max_depth_m": round(max_depth, 4), "wet_pixels": wet}


def build_images_doc(
    *,
    case_id: str,
    run_id: str,
    grid: Any,
    classes: Sequence[DepthClass],
    transparent_below_m: float,
    split_rows: int,
    split_cols: int,
    frames: list[dict[str, Any]],
    max_entry: dict[str, Any] | None,
    kind: str = "depth_frames",
) -> dict[str, Any]:
    """构造 `frames/<case>/<run>/images.json`（Lead 2026-10-27 裁决的两层结构）。"""
    doc: dict[str, Any] = {
        "schema_version": "1.0",
        "case_id": case_id,
        "run_id": run_id,
        "kind": kind,
        "grid": {
            "crs": grid.crs,
            "rows": int(grid.rows),
            "cols": int(grid.cols),
            "transform": [float(v) for v in grid.transform],
            "cell_size_m": float(grid.cell_size_m),
            "cell_area_m2": float(grid.cell_area_m2),
        },
        "color_scale": {
            "classes": [c.to_dict() for c in classes],
            "transparent_below_m": float(transparent_below_m),
            "note": "固定色带，逐像素 1:1 着色；depth<=透明阈值 或 NaN → alpha=0；不可由颜色反算水深",
        },
        "split": {"rows": int(split_rows), "cols": int(split_cols)},
        "part_order": "row major",
        "frames": frames,
    }
    if max_entry is not None:
        doc["max"] = max_entry
    return doc


def describe_scale(value: float) -> str:
    """便于人读的米/千米描述（写入报告）。"""
    if value >= 1000:
        return f"{value / 1000.0:.2f} km"
    return f"{value:.1f} m"
