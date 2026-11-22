"""水力约束河槽重构引擎（案例无关核心，ADR-0012）。

四层功能（对应 plans/无实测水下地形条件下的河槽水力刻蚀与几何重构算法（针对贵港干流漫滩）.txt）：
1. Channel Geometry Estimator —— Manning 梯形断面反演水深（`manning_trapezoid_depth`）；
2. 断面形状 —— 圆滑梯形 smoothstep 剖面（`section_depth_fraction`，.txt §6.1）；
3. 纵向控制 —— 岸顶面平滑 + 床面顺流单调（`bed_profile`，.txt §6.4 增强二）；
4. 栅格刻蚀 —— 4×4 亚采样面积加权、`Z_new=min(Z_orig, Z_ideal)`、走廊外零修改
   （`carve_corridor_grid`，.txt §6.4 增强一）。

工程约定（与 scripts/common/grid_io.py 一致）：
- 不依赖 scipy / shapely / scikit-image（uv 环境未装）：Manning 反演用二分法，
  距离场用逐段解析计算；
- 网格几何由调用方显式传入（rasterio Affine），本模块不做路径/坐标系假设；
- 输出统计只描述，不落盘（落盘由案例脚本负责）；
- 边坡系数固定 z=2.0（与 priors 预注册一致），底宽规则 B = W − 2zD，下钳 0.4W。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

SIDE_SLOPE_Z = 2.0  # 2H:1V，预注册值（guigang_channel_priors.json）
BOTTOM_WIDTH_FLOOR = 0.4  # B 下钳比例（B >= 0.4W）


# ---------------------------------------------------------------- 第 1 层 ----
def trapezoid_geometry(
    depth: float, width_top: float, side_slope: float = SIDE_SLOPE_Z
) -> tuple[float, float, float]:
    """梯形断面几何：返回 (底宽 B, 过水面积 A, 湿周 P)。"""
    if min(depth, width_top, side_slope) <= 0:
        raise ValueError(f"depth/width_top/side_slope 必须为正，实得 {depth}/{width_top}/{side_slope}")
    bottom = width_top - 2.0 * side_slope * depth
    if bottom <= 0:
        raise ValueError(
            f"槽底宽 {bottom:.2f} <= 0：水深 {depth} 超过 W/(2z)={width_top / (2 * side_slope):.2f}"
        )
    area = depth * (bottom + side_slope * depth)
    perimeter = bottom + 2.0 * depth * math.sqrt(1.0 + side_slope**2)
    return bottom, area, perimeter


def manning_conveyance_q(
    depth: float, width_top: float, roughness: float, slope: float, side_slope: float = SIDE_SLOPE_Z
) -> float:
    """Manning 均匀流输水能力 Q(D)（.txt §2.4 断面公式）。"""
    if roughness <= 0 or slope <= 0:
        raise ValueError("糙率与坡降必须为正")
    _bottom, area, perimeter = trapezoid_geometry(depth, width_top, side_slope)
    rh = area / perimeter
    return area * rh ** (2.0 / 3.0) * math.sqrt(slope) / roughness


def manning_trapezoid_depth(
    q_target: float,
    width_top: float,
    roughness: float,
    slope: float,
    side_slope: float = SIDE_SLOPE_Z,
    xtol: float = 1e-6,
) -> float:
    """给定流量反演梯形断面水深（二分法；Q(D) 关于 D 严格单调增）。

    验证锚点（.txt §2.4，W=300/n=0.03/S=0.000116/z=2）：
    Q=11500 -> 19.12 m；70% -> 14.92；60% -> 13.44；50% -> 11.90。
    """
    if q_target <= 0:
        raise ValueError(f"流量必须为正，实得 {q_target}")
    hi = width_top / (2.0 * side_slope) * (1.0 - 1e-9)
    if manning_conveyance_q(hi, width_top, roughness, slope, side_slope) < q_target:
        raise ValueError(
            f"顶宽 {width_top} m 在给定 n/slope 下即使三角形极限也达不到 {q_target} m³/s"
        )
    lo = 1e-4
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if manning_conveyance_q(mid, width_top, roughness, slope, side_slope) < q_target:
            lo = mid
        else:
            hi = mid
        if hi - lo < xtol:
            break
    return 0.5 * (lo + hi)


def bottom_width_for(depth: float, width_top: float) -> float:
    """底宽规则（priors 预注册）：B = W − 2zD，下钳 0.4W。"""
    return max(BOTTOM_WIDTH_FLOOR * width_top, width_top - 2.0 * SIDE_SLOPE_Z * depth)


# ---------------------------------------------------------------- 第 2 层 ----
def smoothstep(u: np.ndarray | float) -> np.ndarray | float:
    """三次平滑过渡 G(u)=3u²−2u³，u 裁剪到 [0,1]（.txt §6.1）。"""
    x = np.clip(u, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def section_depth_fraction(
    dist_m: np.ndarray | float, width_top: float, bottom_width: float
) -> np.ndarray | float:
    """圆滑梯形断面的归一化深度 F(d)∈[0,1]（1=槽底，0=岸顶）。

    d<=B/2 取 1；B/2<d<W/2 取 1−smoothstep(u)；d>=W/2 取 0。
    """
    half_bottom = bottom_width / 2.0
    half_top = width_top / 2.0
    if not (0 < half_bottom < half_top):
        raise ValueError(f"要求 0 < B/2 < W/2，实得 {half_bottom}/{half_top}")
    d = np.abs(np.asarray(dist_m, dtype="float64"))
    u = (d - half_bottom) / (half_top - half_bottom)
    return np.where(d <= half_bottom, 1.0, np.where(d >= half_top, 0.0, 1.0 - smoothstep(u)))


# ---------------------------------------------------------------- 第 3 层 ----
def moving_mean(x: np.ndarray, win: int) -> np.ndarray:
    """边缘填充滑动均值（v3 口径：15 节点）。"""
    if win < 1:
        raise ValueError(f"窗口必须 >=1，实得 {win}")
    x = np.asarray(x, dtype="float64")
    if win == 1:
        return x.copy()
    ker = np.ones(win) / win
    pad = win // 2
    return np.convolve(np.pad(x, pad, mode="edge"), ker, mode="valid")[: len(x)]


def bank_surface_along_path(
    dem: np.ndarray, path_rc: list[tuple[int, int]], band: int = 1
) -> np.ndarray:
    """沿线局部地表：±band 带内最小值（v3 口径，作为岸顶参考面）。"""
    h, w = dem.shape
    out = np.empty(len(path_rc), dtype="float64")
    for i, (r, c) in enumerate(path_rc):
        r0, r1 = max(0, r - band), min(h, r + band + 1)
        c0, c1 = max(0, c - band), min(w, c + band + 1)
        out[i] = float(dem[r0:r1, c0:c1].min())
    return out


@dataclass
class BedProfile:
    """沿程床面与岸顶参考（按路径节点序，上游->下游）。"""

    bank: np.ndarray  # 岸顶参考面（平滑后）
    bed: np.ndarray   # 床面（bank − depth，顺流单调不升）
    depth: np.ndarray


def bed_profile(
    bank_surface: np.ndarray,
    depth_m: float,
    *,
    smooth_win: int = 15,
    monotonic: bool = True,
) -> BedProfile:
    """床面 = 岸顶平滑面 − 设计水深；`monotonic` 时向下游累计取小（.txt §6.4 增强二）。"""
    bank_s = moving_mean(bank_surface, smooth_win)
    bed = bank_s - float(depth_m)
    if monotonic:
        bed = np.minimum.accumulate(bed)
    return BedProfile(
        bank=np.asarray(bank_s, dtype="float64"),
        bed=np.asarray(bed, dtype="float64"),
        depth=np.full(len(bank_s), float(depth_m)),
    )


# ---------------------------------------------------------------- 第 4 层 ----
def _point_segment(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> tuple[float, float, float]:
    """点到线段的（距离, 单位法向 nx, ny）。法向指向点所在一侧（signed>=0）。"""
    abx, aby = bx - ax, by - ay
    denom = abx * abx + aby * aby
    if denom <= 0:
        return math.hypot(px - ax, py - ay), 0.0, 0.0
    t = ((px - ax) * abx + (py - ay) * aby) / denom
    t = min(1.0, max(0.0, t))
    qx, qy = ax + t * abx, ay + t * aby
    dist = math.hypot(px - qx, py - qy)
    norm = math.sqrt(denom)
    nx, ny = -aby / norm, abx / norm
    if (px - ax) * nx + (py - ay) * ny < 0:
        nx, ny = -nx, -ny
    return dist, nx, ny


def carve_corridor_grid(
    dem: np.ndarray,
    transform,
    path_xy: np.ndarray,
    bed_line: np.ndarray,
    bank_line: np.ndarray,
    width_m: float,
    *,
    supersample: int = 4,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict]:
    """对 DEM 执行圆滑梯形河槽刻蚀。

    参数
    ----
    dem : 2D 栅格（投影坐标米）
    transform : rasterio Affine（a=步长, c/f=西/北边界；e 为负）
    path_xy : (N,2) 沿程中心线投影坐标（上游->下游）
    bed_line / bank_line : (N,) 各节点床面与岸顶参考高程
    width_m : 河槽顶宽（m）
    supersample : 每维亚采样数（4 => 4×4；1 = 仅格心）

    返回
    ----
    (dem_new, edit_mask, diff, stats)
      diff = dem − dem_new（正值=下切）。

    语义：Z_ideal = bed(i) + (bank(i) − bed(i)) · (1 − F̄)，
    F̄ 为亚采样平均的断面归一化深度（子点距离 ≈ 格心段距 + 偏移·n̂，直线段局部精确）；
    Z_new = min(Z_orig, Z_ideal)；F̄=0（走廊外）零修改；多节点覆盖取 min（弯道安全）。
    """
    if supersample < 1:
        raise ValueError(f"supersample 必须 >=1，实得 {supersample}")
    h, w = dem.shape
    a = transform
    res_x, res_y = float(a.a), float(-a.e)
    n_nodes = len(path_xy)
    if not (len(bed_line) == len(bank_line) == n_nodes) or n_nodes < 2:
        raise ValueError("path_xy/bed_line/bank_line 长度必须一致且 >=2")

    if supersample == 1:
        offs_x = np.array([0.0])
        offs_y = np.array([0.0])
    else:
        edge = np.linspace(-0.5 + 0.5 / supersample, 0.5 - 0.5 / supersample, supersample)
        offs_x = np.repeat(edge, supersample)
        offs_y = np.tile(edge, supersample)

    rad = int(math.ceil((width_m / 2.0) / min(res_x, res_y))) + 1
    skip_dist = width_m / 2.0 + 0.5 * math.hypot(res_x, res_y)

    dem_new = np.array(dem, dtype="float64", copy=True)
    diff = np.zeros_like(dem_new)
    edit_mask = np.zeros((h, w), dtype=bool)

    for i in range(n_nodes):
        x_i, y_i = float(path_xy[i][0]), float(path_xy[i][1])
        bed_i, bank_i = float(bed_line[i]), float(bank_line[i])
        depth_i = bank_i - bed_i
        if depth_i <= 0:
            continue
        bottom_i = bottom_width_for(depth_i, width_m)
        segs = []
        if i > 0:
            segs.append((float(path_xy[i - 1][0]), float(path_xy[i - 1][1]), x_i, y_i))
        if i < n_nodes - 1:
            segs.append((x_i, y_i, float(path_xy[i + 1][0]), float(path_xy[i + 1][1])))
        if not segs:
            continue
        row = int((y_i - a.f) / a.e)
        col = int((x_i - a.c) / a.a)
        for r in range(max(0, row - rad), min(h, row + rad + 1)):
            cy = a.f + (r + 0.5) * a.e
            for c in range(max(0, col - rad), min(w, col + rad + 1)):
                cx = a.c + (c + 0.5) * a.a
                best_d, best_nx, best_ny = math.inf, 0.0, 0.0
                for (ax, ay, bx, by) in segs:
                    d, nx, ny = _point_segment(cx, cy, ax, ay, bx, by)
                    if d < best_d:
                        best_d, best_nx, best_ny = d, nx, ny
                if best_d > skip_dist:
                    continue
                f_bar = 0.0
                for ox, oy in zip(offs_x, offs_y):
                    d_sub = abs(best_d + ox * res_x * best_nx + oy * res_y * best_ny)
                    f_bar += float(section_depth_fraction(d_sub, width_m, bottom_i))
                f_bar /= len(offs_x)
                if f_bar <= 0.0:
                    continue
                z_ideal = bed_i + depth_i * (1.0 - f_bar)
                if z_ideal >= dem_new[r, c]:
                    continue
                diff[r, c] = dem_new[r, c] - z_ideal
                dem_new[r, c] = z_ideal
                edit_mask[r, c] = True

    edit_count = int(edit_mask.sum())
    stats = {
        "modified_pixel_count": edit_count,
        "mean_dem_change_m": round(float(diff[edit_mask].mean()), 4) if edit_count else 0.0,
        "max_dem_change_m": round(float(diff.max()), 4) if edit_count else 0.0,
        "channel_length_m": round(
            float(sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path_xy, path_xy[1:]))), 1
        ),
        "width_top_m": float(width_m),
        "supersample": supersample,
        "side_slope_z": SIDE_SLOPE_Z,
    }
    return dem_new, edit_mask, diff, stats


def rowcol_of(transform, h: int, w: int, x: float, y: float) -> tuple[int, int]:
    """投影坐标 -> (row, col)（栅格外 clamp 到最近格）。"""
    col = int((x - transform.c) / transform.a)
    row = int((y - transform.f) / transform.e)
    return (min(max(row, 0), h - 1), min(max(col, 0), w - 1))


def carve_statistics_extra(
    dem_before: np.ndarray,
    dem_after: np.ndarray,
    edit_mask: np.ndarray,
    bed_line: np.ndarray,
    path_xy: np.ndarray,
) -> dict:
    """补足 .txt §6.4 的统计字段（纵坡、单调性、下切深度范围）。"""
    seg_len = np.array(
        [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path_xy, path_xy[1:])]
    )
    bed_drop = -np.diff(bed_line)
    slope = np.divide(bed_drop, seg_len, out=np.zeros_like(bed_drop), where=seg_len > 0)
    has_edit = bool(edit_mask.any())
    return {
        "longitudinal_slope_min_per_m": round(float(slope.min()), 8),
        "longitudinal_slope_max_per_m": round(float(slope.max()), 8),
        "longitudinal_monotonic": bool((bed_drop >= -1e-9).all()),
        "carved_depth_min_m": round(float((dem_before - dem_after)[edit_mask].min()), 3) if has_edit else 0.0,
        "carved_depth_max_m": round(float((dem_before - dem_after)[edit_mask].max()), 3) if has_edit else 0.0,
    }
