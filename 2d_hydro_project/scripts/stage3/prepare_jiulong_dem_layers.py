"""Phase 2：久隆四层 DEM 契约与选择性水文调理候选。

按计划 §3.2「四层 DEM 契约」与 §5 Phase 2 / T3 生成四层地形：

| 层 | 用途 | 允许操作 | 本脚本实际做的事 |
|---|---|---|---|
| `DEM_RAW` | 原始归档 | 不修改 | 源 GeoTIFF 双线性重采样到模型网格；只登记来源/哈希/NoData/基准 |
| `DEM_REPAIRED` | 修复确认无效值 | 仅局部、有来源 | 只修两类有来源的像元：重采样欠冲、S1 海面基准锚定 |
| `DEM_HYDRO` | D8、汇流、连通分析 | 选择性填洼 | Priority-Flood，S1 常水像元受保护不填高 |
| `DEM_HYDRAULIC` | 水动力求解 | 保留真实洼地 | 不填洼；结构修复因缺授权资料登记为阻塞 |

**`DEM_HYDRO` 不得直接替代 `DEM_HYDRAULIC`**（计划 §3.2 末段）。本脚本不写任何
run 目录，只产出候选输入与审计。

## 算法与依赖

环境只有 numpy + rasterio（无 scipy / skimage / pysheds）。Priority-Flood 用
`heapq` 优先队列自行实现（Barnes et al. 2014 的「Priority-Flood」单队列版本）。
填洼后用 `flat_surface_stats` 显式统计无下游像元、近邻等值像元与严格洼点，
并**只按像元数量报告**。计划 §2.3 禁止对平坦面下断言，因此本脚本的审计
输出里不给出任何平坦面存在性结论，只给计数。

## 明确不做的事（计划 §2.3 / §3.5）

- 不做全域统一钳位（负值不整体置 0 或置正）；
- 不做全域固定刻蚀（0.5–1.5 m 全域下切被禁止）；
- 不默认施加全域最小坡度；
- 不用全域 Spline / IDW 推断未知排水路径；
- 不给出绝对高程结论：垂直基准未闭合（G7 / ADR-0004），全部指标为相对量。

用法：

    uv --directory 2d_hydro_project run python scripts/stage3/prepare_jiulong_dem_layers.py --dry-run
    uv --directory 2d_hydro_project run python scripts/stage3/prepare_jiulong_dem_layers.py --run-tag v1

输出：

    data/interim/jiulong/dem_layers/<layer>/dem_<layer>.tif
    data/interim/jiulong/dem_layers/<layer>/reason_<layer>.tif   （修复原因码，uint8）
    data/interim/jiulong/dem_layers/<layer>/diff_<layer>.tif     （相对父层改变量，m）
    data/interim/jiulong/dem_layers/<layer>/meta.json
    data/catalog/jiulong_dem_layers.json
"""
from __future__ import annotations

import argparse
import heapq
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import Affine
from rasterio.vrt import WarpedVRT
from rasterio.warp import reproject

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import (  # noqa: E402
    AuditLog,
    GridError,
    add_common_args,
    guard_no_overwrite,
    read_ascii_header,
    sha256_file,
    write_json,
)
from scripts.common.patch_analysis import connected_components  # noqa: E402

DEM_SOURCE = PROJ / "data" / "interim" / "jiulong" / "dem_12p5m_32649.tif"
S1_WATER = PROJ / "data" / "processed" / "s1_water" / "jiulong_20240905T105022_water20m.tif"
DEFAULT_OUT = PROJ / "data" / "interim" / "jiulong" / "dem_layers"
CATALOG = PROJ / "data" / "catalog" / "jiulong_dem_layers.json"
REFERENCE_DEM = (PROJ / "outputs" / "jiulong" / "2024-09-yagi"
                 / "scen_free_200m_sv6h_recession" / "dem.asc")

# 久隆 AOI（与 jiulong_scenarios.py 一致，EPSG:4326）。模型网格由它换算到 32649。
AOI = (108.55, 21.92, 108.90, 22.20)
EPSG = 32649
NODATA = -9999.0

# S1 水体聚合阈值：与 jiulong_scenarios.prep 一致（20 m 水体占比 > 0.2 记为水体）。
S1_FRAC_THRESHOLD = 0.2
# 修复原因码
REASON_NONE = 0
REASON_RESAMPLE_ARTEFACT = 1
REASON_SEA_MSL_ANCHOR = 2
REASON_NAMES = {
    REASON_NONE: "no_change",
    REASON_RESAMPLE_ARTEFACT: "resample_undershoot",
    REASON_SEA_MSL_ANCHOR: "sea_msl_anchor",
}
# 深洼地登记门（预注册，仅用于报告，不参与任何改写决策）
DEEP_DEPRESSION_REPORT_M = 5.0
# 平坦面判据：近邻高程差 <= 该值视为等值
FLAT_TOL_M = 1e-3

# --------------------------------------------------------------------------- #
# 选择性填洼策略（**预注册**，处理前固定，不得按结果事后调整）
# --------------------------------------------------------------------------- #
# 依据一：`data/catalog/dem_artifact_qa_jiulong.json` 实测相邻跳变 p95 = 1.0 m
#         （行列剖面 `adjacent_jump.p95` 均为 1.0）。抬高量 <= 1.0 m 的洼地
#         与 DEM 自身相邻噪声同量级，无法与产品噪声区分，故只填这一类。
# 依据二：局部性上限 9 格（200 m 下 0.36 km²）。超过者只登记、不自动填，
#         交专家复核；避免 Priority-Flood 一次性重写大面积地形
#         （全域填洼实测会改 3794 格 / 151.76 km²，属于计划 §3.5 禁止的量级）。
SELECTIVE_FILL_POLICY: dict[str, Any] = {
    "name": "micro_depression_only",
    "max_raise_m": 1.0,
    "max_cells": 9,
    "max_raise_evidence": (
        "dem_artifact_qa_jiulong.json axis_profiles.*.adjacent_jump.p95 = 1.0 m"
    ),
    "max_cells_evidence": "200 m 网格 9 格 = 0.36 km²；局部性上限，非物理阈值",
    "registered_before_processing": True,
}

LAYER_ORDER = ("DEM_RAW", "DEM_REPAIRED", "DEM_HYDRO", "DEM_HYDRAULIC")

LAYER_CONTRACT: dict[str, dict[str, Any]] = {
    "DEM_RAW": {
        "purpose": "原始归档：源 DEM 在模型网格上的未修复版本，不修改高程值；作为全部派生层的父层",
        "allowed_operations": ["resample (bilinear) 到模型网格"],
        "forbidden_operations": ["任何高程值修复", "任何钳位", "任何填洼"],
        "downstream_use": "审计与差异比较的唯一基线；不直接供水动力求解",
    },
    "DEM_REPAIRED": {
        "purpose": "修复确认无效值：只修有像元级证据的值",
        "allowed_operations": ["局部、有来源的无效值修复"],
        "forbidden_operations": ["全域钳位", "全域插值", "全域固定刻蚀", "填洼"],
        "downstream_use": "DEM_HYDRO 与 DEM_HYDRAULIC 的共同父层",
    },
    "DEM_HYDRO": {
        "purpose": "水文分析：D8 流向、汇流累积、连通分析",
        "allowed_operations": ["选择性填洼"],
        "forbidden_operations": ["替代 DEM_HYDRAULIC", "抹除有证据的真实洼地"],
        "downstream_use": "仅水文分析；不得作为水动力求解输入",
    },
    "DEM_HYDRAULIC": {
        "purpose": "水动力求解：保留真实洼地",
        "allowed_operations": ["按授权资料修结构（当前无资料，未执行）"],
        "forbidden_operations": ["填洼", "全域刻蚀", "全域最小坡度"],
        "downstream_use": "水动力求解候选输入；DEM_HYDRO 不得替代本层",
    },
}

_NEIGHBOURS8 = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
# D8 方向码：0=N 1=NE 2=E 3=SE 4=S 5=SW 6=W 7=NW，-1 = 无下游
_D8_OFFSETS = ((-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1))


class LayerError(RuntimeError):
    """四层契约被破坏（例如 DEM_HYDRO 被当作 DEM_HYDRAULIC）。"""


# --------------------------------------------------------------------------- #
# 1. Priority-Flood 填洼（纯 numpy + heapq）
# --------------------------------------------------------------------------- #
@dataclass
class FillResult:
    """填洼结果与账目。"""

    filled: np.ndarray
    raised: np.ndarray
    protect_violations: int
    seeds: int
    popped: int


def priority_flood_fill(
    dem: np.ndarray,
    *,
    valid: np.ndarray | None = None,
    protect: np.ndarray | None = None,
) -> FillResult:
    """Priority-Flood 填洼（8 邻域，Barnes et al. 2014 单队列版本）。

    参数
    ----
    dem
        高程数组（浮点）。不修改入参。
    valid
        有效像元掩膜；False 的像元既不填也不传播（保持原值）。
    protect
        受保护像元（本项目 = S1 常水水体）。**永不抬高**。

        实现方式是**两趟**：先在全部有效像元上做标准 Priority-Flood，
        再把受保护像元恢复为原值。这样做的原因（实测得出，非风格偏好）：
        若在遍历中"跳过"受保护像元的抬高，它的抬升结果会依赖堆的弹出顺序，
        同样输入可能因平局打破方式不同而给出不同的邻域高程——不可复现。
        两趟法结果唯一确定。

        已知后果（登记，不隐藏）：恢复原值后，受保护像元若低于其已填高的邻域，
        就会成为 `DEM_HYDRO` 中的**有意保留洼点**。这是本项目刻意选择的方向——
        "不抹除有证据真实洼地"优先于"得到完全无洼的 D8 场"（计划 §3.5 / §8）。
        这些洼点的数量由 `flat_surface_stats(...)["strict_pit_cells"]` 报告。

    返回
    ----
    `FillResult`。`raised = filled - dem`（保护像元恒为 0）。

    注意：本函数**不**保证填洼后没有平坦面；调用方必须用
    `flat_surface_stats` 显式检查并报告（计划 §2.3）。
    """
    dem = np.asarray(dem, dtype=np.float64)
    height, width = dem.shape
    valid_mask = np.ones(dem.shape, dtype=bool) if valid is None else np.asarray(valid, dtype=bool)
    protect_mask = (
        np.zeros(dem.shape, dtype=bool) if protect is None else np.asarray(protect, dtype=bool)
    ) & valid_mask

    filled = dem.copy()
    seen = np.zeros(dem.shape, dtype=bool)
    heap: list[tuple[float, int, int]] = []
    seeds = 0

    def _push(r: int, c: int) -> None:
        nonlocal seeds
        if seen[r, c] or not valid_mask[r, c]:
            return
        seen[r, c] = True
        heapq.heappush(heap, (float(dem[r, c]), r, c))
        seeds += 1

    # 域边界像元恒为出流种子（开放边界假设，与 LISFLOOD-FP 四边 FREE 一致）
    for c in range(width):
        _push(0, c)
        _push(height - 1, c)
    for r in range(height):
        _push(r, 0)
        _push(r, width - 1)

    popped = 0
    while heap:
        level, r, c = heapq.heappop(heap)
        popped += 1
        for dr, dc in _NEIGHBOURS8:
            nr, nc = r + dr, c + dc
            if not (0 <= nr < height and 0 <= nc < width):
                continue
            if seen[nr, nc] or not valid_mask[nr, nc]:
                continue
            seen[nr, nc] = True
            own = float(dem[nr, nc])
            if own < level:
                filled[nr, nc] = level
                heapq.heappush(heap, (level, nr, nc))
            else:
                heapq.heappush(heap, (own, nr, nc))

    # 第二趟：受保护像元恢复原值（顺序无关，结果唯一）
    if protect_mask.any():
        filled = np.where(protect_mask, dem, filled)

    violations = int(np.count_nonzero(protect_mask & (filled > dem + 1e-9)))
    raised = np.where(valid_mask, filled - dem, 0.0)
    return FillResult(
        filled=filled, raised=raised, protect_violations=violations, seeds=seeds, popped=popped
    )


# --------------------------------------------------------------------------- #
# 2. 平坦面与流向
# --------------------------------------------------------------------------- #
def flat_surface_stats(
    dem: np.ndarray, *, valid: np.ndarray | None = None, tol_m: float = FLAT_TOL_M
) -> dict[str, Any]:
    """统计平坦面与严格洼点。**只报告像元计数，不对平坦面存在性下结论。**

    - `no_downslope_cells`：8 邻域内不存在严格更低有效像元的像元（D8 无下游）；
    - `level_neighbour_cells`：全部**域内**有效邻域与自身高程差 <= `tol_m` 的像元
      （域外像元不计入，因此边界像元也可判定为等值面）；
    - `strict_pit_cells`：所有域内有效邻域都**严格更高**的像元（真洼点）。
      `DEM_HYDRO` 上该值等于受保护常水像元形成的保留洼点数量。

    计划 §2.3 禁止对平坦面下断言，故此处只给计数，不给结论。
    """
    dem = np.asarray(dem, dtype=np.float64)
    valid_mask = np.ones(dem.shape, dtype=bool) if valid is None else np.asarray(valid, dtype=bool)
    height, width = dem.shape
    padded = np.pad(np.where(valid_mask, dem, np.nan), 1, mode="constant", constant_values=np.nan)
    has_lower = np.zeros(dem.shape, dtype=bool)
    has_level_or_lower = np.zeros(dem.shape, dtype=bool)
    all_level = np.ones(dem.shape, dtype=bool)
    any_valid_nb = np.zeros(dem.shape, dtype=bool)
    for dr, dc in _NEIGHBOURS8:
        nb = padded[1 + dr:1 + dr + height, 1 + dc:1 + dc + width]
        in_domain = np.isfinite(nb)
        with np.errstate(invalid="ignore"):
            lower = in_domain & (nb < dem - tol_m)
            level = in_domain & (np.abs(nb - dem) <= tol_m)
            not_higher = in_domain & (nb <= dem + tol_m)
        has_lower |= lower
        has_level_or_lower |= not_higher
        all_level &= ~in_domain | level
        any_valid_nb |= in_domain
    in_domain_mask = valid_mask & any_valid_nb
    no_downslope = in_domain_mask & ~has_lower
    level_nb = in_domain_mask & all_level
    strict_pit = in_domain_mask & ~has_level_or_lower
    total = max(1, int(np.count_nonzero(valid_mask)))
    return {
        "no_downslope_cells": int(np.count_nonzero(no_downslope)),
        "no_downslope_fraction": round(float(np.count_nonzero(no_downslope)) / total, 6),
        "level_neighbour_cells": int(np.count_nonzero(level_nb)),
        "level_neighbour_fraction": round(float(np.count_nonzero(level_nb)) / total, 6),
        "strict_pit_cells": int(np.count_nonzero(strict_pit)),
        "tolerance_m": tol_m,
        "definition": (
            "no_downslope = 无严格更低有效邻域；level_neighbour = 全部域内邻域与自身等值；"
            "strict_pit = 全部域内邻域严格更高"
        ),
        "statement": "平坦面只按像元数量报告；本脚本不对平坦面是否存在下结论（计划 §2.3）",
    }


def d8_flow_direction(
    dem: np.ndarray, *, valid: np.ndarray | None = None, tol_m: float = FLAT_TOL_M
) -> np.ndarray:
    """D8 流向（最陡下降）。返回 int8，-1 表示无下游（洼地/平坦面）。"""
    dem = np.asarray(dem, dtype=np.float64)
    height, width = dem.shape
    valid_mask = np.ones(dem.shape, dtype=bool) if valid is None else np.asarray(valid, dtype=bool)
    padded = np.pad(dem, 1, mode="constant", constant_values=np.inf)
    best_drop = np.zeros(dem.shape, dtype=np.float64)
    direction = np.full(dem.shape, -1, dtype=np.int8)
    for code, (dr, dc) in enumerate(_D8_OFFSETS):
        nb = padded[1 + dr:1 + dr + height, 1 + dc:1 + dc + width]
        drop = dem - nb
        better = valid_mask & (drop > best_drop + tol_m)
        best_drop = np.where(better, drop, best_drop)
        direction = np.where(better, np.int8(code), direction)
    return np.where(valid_mask, direction, np.int8(-1))


def flow_direction_change(before: np.ndarray, after: np.ndarray) -> dict[str, Any]:
    """比较两个 D8 流向场。"""
    before = np.asarray(before)
    after = np.asarray(after)
    if before.shape != after.shape:
        raise GridError(f"流向形状不一致：{before.shape} vs {after.shape}")
    changed = before != after
    both_defined = (before >= 0) & (after >= 0)
    return {
        "changed_cells": int(np.count_nonzero(changed)),
        "changed_fraction": round(float(np.count_nonzero(changed)) / max(1, changed.size), 6),
        "changed_with_both_downstream_cells": int(np.count_nonzero(changed & both_defined)),
        "undefined_before_cells": int(np.count_nonzero(before < 0)),
        "undefined_after_cells": int(np.count_nonzero(after < 0)),
    }


# --------------------------------------------------------------------------- #
# 3. 洼地清单（真实洼地登记）
# --------------------------------------------------------------------------- #
def depression_inventory(
    dem: np.ndarray,
    filled: np.ndarray,
    *,
    cell_area_m2: float,
    valid: np.ndarray | None = None,
    protect: np.ndarray | None = None,
    top_n: int = 20,
) -> dict[str, Any]:
    """把「被填高的像元」按连通域聚成洼地，逐个登记面积/深度/体积。

    这是「不抹除有证据真实洼地」的账目基础：`DEM_HYDRO` 里每个被填高的洼地
    都能在这里查到位置、体积与是否含 S1 常水像元。
    """
    dem = np.asarray(dem, dtype=np.float64)
    filled = np.asarray(filled, dtype=np.float64)
    valid_mask = np.ones(dem.shape, dtype=bool) if valid is None else np.asarray(valid, dtype=bool)
    raised = valid_mask & (filled > dem + 1e-9)
    protect_mask = (
        np.zeros(dem.shape, dtype=bool) if protect is None else np.asarray(protect, dtype=bool)
    )
    n_raised = int(np.count_nonzero(raised))
    total_volume = float(np.sum(np.where(raised, filled - dem, 0.0))) * cell_area_m2
    if n_raised == 0:
        return {
            "filled_cells": 0, "filled_area_km2": 0.0, "fill_volume_m3": 0.0,
            "max_raise_m": 0.0, "n_depressions": 0, "depressions": [],
            "deep_depression_report_threshold_m": DEEP_DEPRESSION_REPORT_M,
            "deep_depression_count": 0,
            "with_protected_water_count": 0,
        }
    labels, n_labels = connected_components(raised, connectivity=8)
    depth = np.where(raised, filled - dem, 0.0)
    flat_labels = labels.ravel()
    order = np.argsort(flat_labels, kind="stable")
    sorted_labels = flat_labels[order]
    boundaries = np.flatnonzero(np.diff(sorted_labels)) + 1
    width = dem.shape[1]
    records: list[dict[str, Any]] = []
    for group in np.split(order, boundaries):
        if group.size == 0:
            continue
        label = int(flat_labels[group[0]])
        if label == 0:
            continue
        rows, cols = np.divmod(group, width)
        vals = depth.ravel()[group]
        spill = filled.ravel()[group]
        records.append({
            "label": label,
            "n_cells": int(group.size),
            "area_km2": round(float(group.size) * cell_area_m2 / 1e6, 4),
            "max_raise_m": round(float(vals.max()), 3),
            "mean_raise_m": round(float(vals.mean()), 3),
            "volume_m3": round(float(vals.sum()) * cell_area_m2, 1),
            "spill_elevation_m": round(float(spill.max()), 3),
            "bbox_rowcol": [int(rows.min()), int(cols.min()), int(rows.max()), int(cols.max())],
            "contains_protected_water": bool(np.any(protect_mask.ravel()[group])),
        })
    records.sort(key=lambda r: (-r["volume_m3"], r["label"]))
    return {
        "filled_cells": n_raised,
        "filled_area_km2": round(n_raised * cell_area_m2 / 1e6, 4),
        "fill_volume_m3": round(total_volume, 1),
        "max_raise_m": round(float(depth.max()), 3),
        "n_depressions": len(records),
        "deep_depression_report_threshold_m": DEEP_DEPRESSION_REPORT_M,
        "deep_depression_count": int(sum(1 for r in records if r["max_raise_m"] >= DEEP_DEPRESSION_REPORT_M)),
        "with_protected_water_count": int(sum(1 for r in records if r["contains_protected_water"])),
        "depressions": records[:top_n],
        "depressions_truncated": max(0, len(records) - top_n),
    }


# --------------------------------------------------------------------------- #
# 4. 选择性填洼（按预注册策略筛选洼地）
# --------------------------------------------------------------------------- #
@dataclass
class SelectiveFillResult:
    """选择性填洼结果：填过的 / 登记的 / 策略。"""

    filled: np.ndarray
    raised: np.ndarray
    applied: dict[str, Any]
    registered_only: dict[str, Any]
    policy: dict[str, Any]


def selective_fill(
    dem: np.ndarray,
    *,
    valid: np.ndarray,
    protect: np.ndarray,
    policy: dict[str, Any] | None = None,
    cell_area_m2: float = 1.0,
) -> SelectiveFillResult:
    """按预注册策略只填「微坑」，其余洼地只登记不填。

    步骤：
    1. 全域 Priority-Flood 得到**参考**填洼结果（只用于识别洼地与抬升量，
       不直接采用）；
    2. 把被抬高像元聚成洼地，逐个判定；
    3. 只有同时满足 `max_raise_m` 与 `max_cells` 的洼地才写入 `filled`；
    4. 其余洼地进 `registered_only`，带位置/体积/抬升，供专家复核。

    这样 `DEM_HYDRO` 不会一次性重写大面积地形（计划 §3.5「仅对确认的数字微坑
    试验」），同时每个未填洼地都有登记，可追溯。
    """
    pol = dict(SELECTIVE_FILL_POLICY if policy is None else policy)
    dem = np.asarray(dem, dtype=np.float64)
    valid = np.asarray(valid, dtype=bool)
    protect = np.asarray(protect, dtype=bool) & valid

    reference = priority_flood_fill(dem, valid=valid, protect=protect)
    raised_ref = valid & (reference.raised > 1e-9)
    out = dem.copy()
    if not raised_ref.any():
        empty = {
            "filled_cells": 0, "filled_area_km2": 0.0, "fill_volume_m3": 0.0,
            "max_raise_m": 0.0, "n_depressions": 0, "n_applied": 0,
        }
        return SelectiveFillResult(out, np.zeros_like(dem), empty, dict(empty), pol)

    labels, n_labels = connected_components(raised_ref, connectivity=8)
    depth = np.where(raised_ref, reference.raised, 0.0)
    flat_labels = labels.ravel()
    order = np.argsort(flat_labels, kind="stable")
    sorted_labels = flat_labels[order]
    boundaries = np.flatnonzero(np.diff(sorted_labels)) + 1
    width = dem.shape[1]

    applied: list[dict[str, Any]] = []
    registered: list[dict[str, Any]] = []
    for group in np.split(order, boundaries):
        if group.size == 0:
            continue
        label = int(flat_labels[group[0]])
        if label == 0:
            continue
        rows, cols = np.divmod(group, width)
        vals = depth.ravel()[group]
        rec = {
            "label": label,
            "n_cells": int(group.size),
            "area_km2": round(float(group.size) * cell_area_m2 / 1e6, 4),
            "max_raise_m": round(float(vals.max()), 3),
            "mean_raise_m": round(float(vals.mean()), 3),
            "volume_m3": round(float(vals.sum()) * cell_area_m2, 1),
            "spill_elevation_m": round(float(reference.filled.ravel()[group].max()), 3),
            "bbox_rowcol": [int(rows.min()), int(cols.min()), int(rows.max()), int(cols.max())],
            "contains_protected_water": bool(np.any(protect.ravel()[group])),
        }
        ok = (rec["max_raise_m"] <= float(pol["max_raise_m"])
              and rec["n_cells"] <= int(pol["max_cells"]))
        if ok:
            out.ravel()[group] = reference.filled.ravel()[group]
            applied.append(rec)
        else:
            registered.append(rec)

    def _agg(recs: list[dict[str, Any]]) -> dict[str, Any]:
        if not recs:
            return {"filled_cells": 0, "filled_area_km2": 0.0, "fill_volume_m3": 0.0,
                    "max_raise_m": 0.0, "n_depressions": 0}
        cells = sum(r["n_cells"] for r in recs)
        return {
            "filled_cells": int(cells),
            "filled_area_km2": round(float(cells) * cell_area_m2 / 1e6, 4),
            "fill_volume_m3": round(float(sum(r["volume_m3"] for r in recs)), 1),
            "max_raise_m": round(float(max(r["max_raise_m"] for r in recs)), 3),
            "n_depressions": len(recs),
            "n_cells_p50": int(np.percentile([r["n_cells"] for r in recs], 50)),
            "n_cells_max": int(max(r["n_cells"] for r in recs)),
        }

    applied_agg = _agg(applied)
    applied_agg["n_applied"] = len(applied)
    applied_agg["depressions"] = sorted(applied, key=lambda r: -r["volume_m3"])
    reg_agg = _agg(registered)
    reg_agg["n_registered"] = len(registered)
    reg_agg["depressions"] = sorted(registered, key=lambda r: -r["volume_m3"])
    reg_agg["reason"] = (
        "超过预注册局部性/抬升上限，只登记不填：Priority-Flood 会改写大面积地形，"
        "须专家复核或取得资料后另立试验（计划 §3.5）"
    )
    out = np.where(valid, out, dem)
    return SelectiveFillResult(out, np.where(valid, out - dem, 0.0), applied_agg, reg_agg, pol)


# --------------------------------------------------------------------------- #
# 5. 四层构建（纯函数，供合成算例测试）
# --------------------------------------------------------------------------- #
@dataclass
class LayerStack:
    """四层高程数组 + 修复原因码 + 填洼账目。"""

    layers: dict[str, np.ndarray]
    reason: np.ndarray
    fill: FillResult
    fill_unprotected: FillResult
    selective: SelectiveFillResult | None = None


def build_layer_stack(
    raw: np.ndarray,
    *,
    valid: np.ndarray,
    s1_water: np.ndarray,
    sea: np.ndarray,
    block_mean: np.ndarray | None = None,
    cell_area_m2: float = 1.0,
    fill_policy: dict[str, Any] | None = None,
    anchor_sea: bool = True,
) -> LayerStack:
    """从 DEM_RAW 构建四层。

    修复规则只有两条，每条都能追到像元级来源：

    - `resample_undershoot`：200 m 像元为负，但 12.5 m 源块均值 >= 0 →
      负值是双线性重采样欠冲，用源块均值替换（来源：源栅格块统计）。
    - `sea_msl_anchor`：像元属 S1 常水且与域边连通（= 海/海湾）且为负 →
      按 ADR-0004「模型内部以 DEM 自身基准为基准、海面取 0」锚定为 0 m
      （来源：S1 水体掩膜 + 域边连通性 + 基准约定）。

    其余负值**不修**：来源未闭合（G7），无像元级证据，全域钳位被禁止。
    """
    raw = np.asarray(raw, dtype=np.float64)
    valid = np.asarray(valid, dtype=bool)
    s1_water = np.asarray(s1_water, dtype=bool) & valid
    sea = np.asarray(sea, dtype=bool) & valid
    if raw.shape != valid.shape or raw.shape != s1_water.shape:
        raise GridError(f"数组形状不一致：{raw.shape} / {valid.shape} / {s1_water.shape}")

    repaired = np.where(valid, raw, NODATA)
    reason = np.zeros(raw.shape, dtype=np.uint8)

    if block_mean is not None:
        block_mean = np.asarray(block_mean, dtype=np.float64)
        artefact = valid & (raw < 0) & np.isfinite(block_mean) & (block_mean >= 0)
        repaired = np.where(artefact, block_mean, repaired)
        reason[artefact] = REASON_RESAMPLE_ARTEFACT

    anchor = valid & (raw < 0) & sea & (reason == REASON_NONE)
    if anchor_sea:
        repaired = np.where(anchor, 0.0, repaired)
        reason[anchor] = REASON_SEA_MSL_ANCHOR
    else:
        anchor = np.zeros(raw.shape, dtype=bool)

    repaired = np.where(valid, repaired, NODATA)
    hydraulic = repaired.copy()  # 保留真实洼地：不做任何填洼

    # 全域 Priority-Flood 只作**参考/对照**记录，不直接成为 DEM_HYDRO
    fill = priority_flood_fill(repaired, valid=valid, protect=s1_water)
    fill_unprotected = priority_flood_fill(repaired, valid=valid, protect=None)
    # DEM_HYDRO 只填预注册策略允许的微坑；其余洼地只登记
    selective = selective_fill(
        repaired, valid=valid, protect=s1_water, cell_area_m2=cell_area_m2,
        policy=fill_policy,
    )
    hydro = np.where(valid, selective.filled, NODATA)

    return LayerStack(
        layers={
            "DEM_RAW": np.where(valid, raw, NODATA),
            "DEM_REPAIRED": repaired,
            "DEM_HYDRO": hydro,
            "DEM_HYDRAULIC": hydraulic,
        },
        reason=reason,
        fill=fill,
        fill_unprotected=fill_unprotected,
        selective=selective,
    )


# --------------------------------------------------------------------------- #
# 5. 网格与输入读取
# --------------------------------------------------------------------------- #
def model_grid(res: float) -> dict[str, float]:
    """由 AOI 换算模型网格（与 jiulong_scenarios.prep 同源）。"""
    from pyproj import Transformer

    tf = Transformer.from_crs(4326, EPSG, always_xy=True)
    corners = [tf.transform(x, y) for x, y in ((AOI[0], AOI[1]), (AOI[2], AOI[3]))]
    west = min(c[0] for c in corners)
    south = min(c[1] for c in corners)
    east = max(c[0] for c in corners)
    north = max(c[1] for c in corners)
    return {
        "ncols": float(int((east - west) / res)),
        "nrows": float(int((north - south) / res)),
        "xllcorner": west,
        "yllcorner": south,
        "north": north,
        "cellsize": float(res),
    }


def grid_transform(grid: dict[str, float]) -> Affine:
    """栅格变换。**北缘用 AOI 的 north，不用 yllcorner+nrows*res**（见 georef 检查）。"""
    return Affine(grid["cellsize"], 0.0, grid["xllcorner"], 0.0, -grid["cellsize"], grid["north"])


def read_source_resampled(
    path: Path, grid: dict[str, float], resampling: Resampling
) -> np.ndarray:
    """把源 DEM 重采样到模型网格。"""
    shape = (int(grid["nrows"]), int(grid["ncols"]))
    with rasterio.open(path) as src:
        with WarpedVRT(
            src, crs=f"EPSG:{EPSG}", resampling=resampling,
            width=shape[1], height=shape[0], transform=grid_transform(grid),
        ) as vrt:
            return vrt.read(1).astype(np.float64)


def s1_water_on_grid(grid: dict[str, float]) -> np.ndarray:
    """S1 常水水体聚合到模型网格（覆盖率 > 阈值记为水体）。"""
    shape = (int(grid["nrows"]), int(grid["ncols"]))
    frac = np.zeros(shape, dtype="float32")
    with rasterio.open(S1_WATER) as ds:
        reproject(
            (ds.read(1) == 1).astype("float32"), frac,
            src_transform=ds.transform, src_crs=ds.crs,
            dst_transform=grid_transform(grid), dst_crs=f"EPSG:{EPSG}",
            resampling=Resampling.average,
        )
    return frac > S1_FRAC_THRESHOLD


def edge_connected(water: np.ndarray) -> np.ndarray:
    """与域边连通的 4 连通水体（G16 口径：只有这部分才是海/海湾）。"""
    height, width = water.shape
    sea = np.zeros_like(water)
    sea[0, :] |= water[0, :]
    sea[-1, :] |= water[-1, :]
    sea[:, 0] |= water[:, 0]
    sea[:, -1] |= water[:, -1]
    for _ in range(max(height, width) * 2):
        grown = sea.copy()
        grown[1:, :] |= sea[:-1, :]
        grown[:-1, :] |= sea[1:, :]
        grown[:, 1:] |= sea[:, :-1]
        grown[:, :-1] |= sea[:, 1:]
        grown &= water
        if (grown == sea).all():
            break
        sea = grown
    return sea


def georeferencing_check(grid: dict[str, float], raw: np.ndarray) -> dict[str, Any]:
    """核对模型网格北缘与既有 dem.asc 头声明是否一致。

    `jiulong_scenarios.prep` 用 AOI 的 north 作为栅格顶边，却把 `yllcorner`
    写成 AOI 的 south；两者相差 `nrows*cellsize` 的取整余量，因此 ASCII 头
    声明的北缘比数据实际位置低一个余量。此处用既有 dem.asc 实测比对。
    """
    rec: dict[str, Any] = {
        "grid_north_from_aoi_m": round(float(grid["north"]), 3),
        "grid_yllcorner_m": round(float(grid["yllcorner"]), 3),
        "cellsize_m": float(grid["cellsize"]),
    }
    if not REFERENCE_DEM.is_file():
        rec["status"] = "reference_missing"
        return rec
    header = read_ascii_header(REFERENCE_DEM)
    north_header = float(header["yllcorner"]) + float(header["nrows"]) * float(header["cellsize"])
    rec.update({
        "reference_dem": str(REFERENCE_DEM.relative_to(PROJ)),
        "reference_sha256": sha256_file(REFERENCE_DEM),
        "north_from_asc_header_m": round(north_header, 3),
        "mismatch_m": round(float(grid["north"]) - north_header, 3),
        "mismatch_cells": round((float(grid["north"]) - north_header) / float(grid["cellsize"]), 4),
    })
    reference = np.loadtxt(REFERENCE_DEM, skiprows=6)
    if reference.shape != raw.shape:
        rec["status"] = "shape_mismatch"
        rec["reference_shape"] = list(reference.shape)
        return rec
    comparable = np.isfinite(reference) & (reference >= 0)  # 负值已被生产流程改写，不参与比对
    delta = np.abs(reference - raw)
    matched = int(np.count_nonzero(comparable & (delta < 0.01)))
    rec.update({
        "comparable_cells": int(np.count_nonzero(comparable)),
        "matched_cells_within_0p01m": matched,
        "matched_fraction": round(matched / max(1, int(np.count_nonzero(comparable))), 6),
        "status": "asc_header_shift_confirmed" if matched > 0.99 * max(1, int(np.count_nonzero(comparable))) else "unexplained",
        "note": (
            "既有 dem.asc 的数值与「AOI north 顶边 + 双线性」重采样逐格一致，"
            "但其 ASCII 头声明的 yllcorner 使北缘低一个取整余量；"
            "本脚本输出的 GeoTIFF 采用数据真实位置（AOI north），"
            "与既有 dem.asc 逐格比较时必须按行列索引对齐，不得按声明坐标对齐。"
        ),
    })
    return rec


def _write_geotiff(path: Path, data: np.ndarray, grid: dict[str, float], *, dtype: str,
                   nodata: float | None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    shape = data.shape
    with rasterio.open(
        path, "w", driver="GTiff", height=shape[0], width=shape[1], count=1,
        dtype=dtype, crs=f"EPSG:{EPSG}", transform=grid_transform(grid),
        nodata=nodata, compress="lzw",
    ) as dst:
        dst.write(data.astype(dtype), 1)
        dst.update_tags(
            cellsize_m=str(float(grid["cellsize"])),
            cell_area_m2=str(float(grid["cellsize"]) ** 2),
            vertical_datum="unclosed (ADR-0004 / G7); relative indicators only",
            layer_contract="jiulong_dem_layers_v1",
        )
    return path


# --------------------------------------------------------------------------- #
# 6. 主流程
# --------------------------------------------------------------------------- #
def _layer_record(
    name: str,
    data: np.ndarray,
    parent: str | None,
    *,
    grid: dict[str, float],
    reason: np.ndarray | None,
    diff_vs_parent: np.ndarray,
    flat: dict[str, Any],
    flow: dict[str, Any] | None,
    out_dir: Path,
    sha256: str | None,
    extra_operations: list[str],
    uncertainty: dict[str, Any],
    fill_volume_m3: float = 0.0,
    fill_raise_cells: int = 0,
    protect_violations: int = 0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成单层契约记录。**必须**包含计划 §3.2「必须记录」四列与任务书全部字段。"""
    cell_area = float(grid["cellsize"]) ** 2
    valid = data > NODATA + 1.0
    changed = valid & (np.abs(diff_vs_parent) > 1e-9)
    rec: dict[str, Any] = {
        "layer": name,
        "purpose": LAYER_CONTRACT[name]["purpose"],
        "allowed_operations": LAYER_CONTRACT[name]["allowed_operations"],
        "forbidden_operations": LAYER_CONTRACT[name]["forbidden_operations"],
        "downstream_use": LAYER_CONTRACT[name]["downstream_use"],
        "parent_layer": parent,
        "operations_applied": extra_operations,
        "path": rec_path_hint(out_dir),
        "sha256": sha256,
        "crs": f"EPSG:{EPSG}",
        "resolution_m": float(grid["cellsize"]),
        "cell_area_m2": cell_area,
        "shape": [int(data.shape[0]), int(data.shape[1])],
        "nodata": NODATA,
        "valid_cells": int(np.count_nonzero(valid)),
        "changed_cells": int(np.count_nonzero(changed)),
        "changed_area_km2": round(float(np.count_nonzero(changed)) * cell_area / 1e6, 4),
        "max_abs_change_m": round(
            float(np.max(np.abs(diff_vs_parent[valid]))) if valid.any() else 0.0, 3),
        "fill_volume_m3": round(float(fill_volume_m3), 1),
        "flow_direction_change": flow,
        "flat_surface_check": flat,
        "uncertainty": uncertainty,
        "rollback_path": (
            f"删除 {rec_path_hint(out_dir)} 即回滚；源 DEM 与 S1 只读，sha256 不变"
        ),
    }
    if reason is not None:
        rec["repair_reason_cells"] = {
            REASON_NAMES[code]: int(np.count_nonzero(reason == code)) for code in REASON_NAMES
        }
        # 逐原因统计改变量，使「每个改动像元能追到原因」可复算
        per_reason: dict[str, Any] = {}
        raw_values = data - diff_vs_parent
        for code, label in REASON_NAMES.items():
            sel = reason == code
            n = int(np.count_nonzero(sel))
            if code == REASON_NONE or n == 0:
                per_reason[label] = {"cells": n}
                continue
            delta = diff_vs_parent[sel]
            per_reason[label] = {
                "cells": n,
                "area_km2": round(n * cell_area / 1e6, 4),
                "delta_min_m": round(float(delta.min()), 3),
                "delta_max_m": round(float(delta.max()), 3),
                "abs_delta_max_m": round(float(np.max(np.abs(delta))), 3),
                "source_elevation_min_m": round(float(raw_values[sel].min()), 3),
                "source_elevation_max_m": round(float(raw_values[sel].max()), 3),
                "repaired_elevation_min_m": round(float(data[sel].min()), 3),
                "repaired_elevation_max_m": round(float(data[sel].max()), 3),
            }
        rec["repair_reason_detail"] = per_reason
    if name == "DEM_HYDRO":
        rec["fill_raise_cells"] = int(fill_raise_cells)
        rec["protect_violations"] = int(protect_violations)
    if extra:
        rec.update(extra)
    return rec


def rec_path_hint(path: Path) -> str:
    """相对项目根的路径字符串（用于回滚说明）。"""
    try:
        return path.relative_to(PROJ).as_posix()
    except ValueError:
        return str(path)


def run(args: argparse.Namespace) -> int:
    out_root = Path(args.out_dir) if args.out_dir else DEFAULT_OUT
    if args.run_tag and args.out_dir is None:
        out_root = out_root.parent / f"{out_root.name}_{args.run_tag}"
    catalog_path = Path(args.log_json) if args.log_json else CATALOG

    guard_no_overwrite(out_root)  # 终稿 §10：目标目录含 res.mass 即退出

    grid = model_grid(args.res)
    shape = (int(grid["nrows"]), int(grid["ncols"]))
    log = AuditLog("scripts/stage3/prepare_jiulong_dem_layers.py",
                   run_tag=args.run_tag, dry_run=args.dry_run)
    log.add_input("dem_source", DEM_SOURCE)
    log.add_input("s1_water", S1_WATER)
    log.add_input("reference_dem", REFERENCE_DEM)
    log.param(res_m=float(args.res), s1_frac_threshold=S1_FRAC_THRESHOLD,
              deep_depression_report_m=DEEP_DEPRESSION_REPORT_M, flat_tol_m=FLAT_TOL_M,
              out_root=rec_path_hint(out_root), layer_order=list(LAYER_ORDER))

    with rasterio.open(DEM_SOURCE) as ds:
        src_meta = {
            "path": str(DEM_SOURCE.relative_to(PROJ)),
            "sha256": sha256_file(DEM_SOURCE),
            "bytes": DEM_SOURCE.stat().st_size,
            "crs": str(ds.crs),
            "width": int(ds.width), "height": int(ds.height),
            "resolution_m": [round(float(ds.res[0]), 6), round(float(ds.res[1]), 6)],
            "nodata": ds.nodata,
        }
        src_nodata = int(np.count_nonzero(ds.read(1) == (ds.nodata if ds.nodata is not None else NODATA)))

    raw = read_source_resampled(DEM_SOURCE, grid, Resampling.bilinear)
    block_mean = read_source_resampled(DEM_SOURCE, grid, Resampling.average)
    valid = np.isfinite(raw) & (raw > NODATA + 1.0)
    if raw.shape != shape:
        raise GridError(f"重采样结果 {raw.shape} 与网格 {shape} 不符")
    water = s1_water_on_grid(grid)
    sea = edge_connected(water)

    stack = build_layer_stack(raw, valid=valid, s1_water=water, sea=sea, block_mean=block_mean,
                              cell_area_m2=float(grid["cellsize"]) ** 2,
                              anchor_sea=not args.no_sea_anchor)
    log.step("read_inputs", grid=grid, valid_cells=int(valid.sum()),
             source_nodata_px=src_nodata, s1_water_cells=int(water.sum()),
             sea_cells=int(sea.sum()))

    layers = stack.layers
    cell_area = float(grid["cellsize"]) ** 2
    negative = valid & (layers["DEM_REPAIRED"] < 0)
    unresolved = valid & (layers["DEM_REPAIRED"] < 0) & ~water

    # 流向与平坦面：以 DEM_RAW 为基准比较 DEM_HYDRO
    flow_raw = d8_flow_direction(layers["DEM_RAW"], valid=valid)
    flow_hydro = d8_flow_direction(layers["DEM_HYDRO"], valid=valid)
    flow_change = flow_direction_change(flow_raw, flow_hydro)

    flat_raw = flat_surface_stats(layers["DEM_RAW"], valid=valid)
    flat_repaired = flat_surface_stats(layers["DEM_REPAIRED"], valid=valid)
    flat_hydro = flat_surface_stats(layers["DEM_HYDRO"], valid=valid)
    flat_hydraulic = flat_surface_stats(layers["DEM_HYDRAULIC"], valid=valid)

    inventory = depression_inventory(
        layers["DEM_REPAIRED"], layers["DEM_HYDRO"], cell_area_m2=cell_area,
        valid=valid, protect=water,
    )
    # 对照：全域 Priority-Flood（**未采用**，仅作敏感性记录）
    reference_inventory = depression_inventory(
        layers["DEM_REPAIRED"], np.where(valid, stack.fill.filled, NODATA),
        cell_area_m2=cell_area, valid=valid, protect=water, top_n=10,
    )
    # 保护效果：无保护填洼 vs 有保护填洼
    protected_effect = {
        "protected_cells": int(np.count_nonzero(water)),
        "raised_without_protection_cells": int(np.count_nonzero(stack.fill_unprotected.raised[water] > 1e-9)),
        "raised_without_protection_area_km2": round(
            float(np.count_nonzero(stack.fill_unprotected.raised[water] > 1e-9)) * cell_area / 1e6, 4),
        "raised_without_protection_volume_m3": round(
            float(np.sum(stack.fill_unprotected.raised[water])) * cell_area, 1),
        "raised_with_protection_cells": int(np.count_nonzero(stack.fill.raised[water] > 1e-9)),
        "raised_in_dem_hydro_cells": int(np.count_nonzero(stack.selective.raised[water] > 1e-9)),
        "max_raise_without_protection_m": round(
            float(np.max(stack.fill_unprotected.raised[water])) if water.any() else 0.0, 3),
        "rule": "S1 常水像元在 DEM_HYDRO 中永不抬高（有证据的真实水体/洼地保护）",
        "verification": "若 S1 常水像元被抬高，本脚本抛 LayerError 并中止",
    }
    if protected_effect["raised_with_protection_cells"] != 0:
        raise LayerError("DEM_HYDRO 抬高了 S1 常水像元，违反真实洼地保护约束")
    if protected_effect["raised_in_dem_hydro_cells"] != 0:
        raise LayerError("选择性填洼抬高了 S1 常水像元，违反真实洼地保护约束")

    uncertainty = {
        "vertical_datum": {
            "status": "unclosed",
            "datum_offset_m": None,
            "evidence": "G7 / ADR-0004：源 DEM 来源与垂直基准未查证，未填猜测常数",
            "consequence": "禁止绝对高程结论；全部指标为相对量（改变量、体积、面积）",
        },
        "negative_elevation": {
            "status": "unresolved",
            "cells": int(np.count_nonzero(negative)),
            "area_km2": round(float(np.count_nonzero(negative)) * cell_area / 1e6, 4),
            "cells_outside_s1_water": int(np.count_nonzero(unresolved)),
            "area_outside_s1_water_km2": round(float(np.count_nonzero(unresolved)) * cell_area / 1e6, 4),
            "depth_m": {
                "min": round(float(layers["DEM_REPAIRED"][negative].min()), 3) if negative.any() else None,
                "p50": round(float(np.median(layers["DEM_REPAIRED"][negative])), 3) if negative.any() else None,
            },
            "why_not_repaired": (
                "来源未闭合（计划 §1.2 DEM 行 / G16）：约 96% 负值不与 S1 常水重合，"
                "既不能视为海面，也不能证明是有效地形；无像元级证据时不做全域钳位"
            ),
            "required_action": "取得 DEM 产品说明或控制点后重做本层；当前仅作候选输入",
        },
        "resampling": {
            "from_m": src_meta["resolution_m"],
            "to_m": float(grid["cellsize"]),
            "method_bilinear": "DEM_RAW / DEM_REPAIRED / DEM_HYDRAULIC",
            "method_average": "仅用于判定重采样欠冲的源块均值",
            "consequence": "局部坡度与微坑随尺度变化；不得跨分辨率比较像元数（计划 §3.4）",
        },
        "s1_evidence": {
            "role": "WEAK（ADR-0004）：灾前常水景，仅用于分辨真实水体，不作精度验证",
            "frac_threshold": S1_FRAC_THRESHOLD,
        },
    }

    dry = bool(args.dry_run)
    layer_records: dict[str, Any] = {}
    out_dirs: dict[str, Path] = {}
    parent_of = {"DEM_RAW": None, "DEM_REPAIRED": "DEM_RAW",
                 "DEM_HYDRO": "DEM_REPAIRED", "DEM_HYDRAULIC": "DEM_REPAIRED"}
    for name in LAYER_ORDER:
        layer_dir = out_root / name.lower()
        out_dirs[name] = layer_dir
        guard_no_overwrite(layer_dir)
        data = layers[name]
        reason = stack.reason if name == "DEM_REPAIRED" else None
        fill = stack.fill if name == "DEM_HYDRO" else None
        flat = {"DEM_RAW": flat_raw, "DEM_REPAIRED": flat_repaired,
                "DEM_HYDRO": flat_hydro, "DEM_HYDRAULIC": flat_hydraulic}[name]
        flow = flow_change if name == "DEM_HYDRO" else None
        ops = ["resample_bilinear_12p5m_to_model_grid"]
        if name == "DEM_REPAIRED":
            ops = ["local_repair: resample_undershoot (source block mean)",
                   "local_repair: sea_msl_anchor (S1 water + edge connectivity)"]
        elif name == "DEM_HYDRO":
            ops = ["priority_flood_fill (8-neighbour, heapq)",
                   "protect: S1 permanent water cells never raised"]
        elif name == "DEM_HYDRAULIC":
            ops = ["none (structure repair blocked: no authorized data)"]

        sha = None
        parent = parent_of[name]
        diff = (np.zeros(data.shape, dtype="float64") if parent is None
                else np.where(valid, data - layers[parent], 0.0))
        if not dry:
            tif = _write_geotiff(layer_dir / f"{name.lower()}.tif", data, grid,
                                 dtype="float32", nodata=NODATA)
            sha = sha256_file(tif)
            if parent is not None:
                _write_geotiff(layer_dir / f"diff_vs_{parent.lower()}.tif",
                               diff.astype("float32"), grid, dtype="float32", nodata=None)
            if name == "DEM_REPAIRED":
                _write_geotiff(layer_dir / "reason_codes.tif", stack.reason, grid,
                               dtype="uint8", nodata=None)

        extra: dict[str, Any] = {}
        if name == "DEM_HYDRO":
            sel = stack.selective
            extra = {
                "fill_policy": sel.policy,
                "applied_fill": {k: v for k, v in sel.applied.items() if k != "depressions"},
                "applied_depressions": sel.applied["depressions"],
                "registered_not_filled": {k: v for k, v in sel.registered_only.items()
                                          if k != "depressions"},
                "registered_depressions_top": sel.registered_only["depressions"][:20],
                "reference_full_fill": {
                    "note": "全域 Priority-Flood 对照值，**未采用**；仅作敏感性登记",
                    **{k: v for k, v in reference_inventory.items()
                       if k not in ("depressions",)},
                },
            }
        rec = _layer_record(
            name, data, parent, grid=grid, reason=reason, diff_vs_parent=diff, flat=flat,
            flow=flow, out_dir=layer_dir, sha256=sha, extra_operations=ops,
            uncertainty=uncertainty,
            fill_volume_m3=(float(np.sum(stack.selective.raised)) * cell_area
                            if name == "DEM_HYDRO" else 0.0),
            fill_raise_cells=(int(np.count_nonzero(stack.selective.raised > 1e-9))
                              if name == "DEM_HYDRO" else 0),
            protect_violations=(int(stack.fill.protect_violations)
                                if name == "DEM_HYDRO" else 0),
            extra=extra,
        )
        rec["meta_path"] = rec_path_hint(layer_dir / "meta.json")
        if not dry:
            write_json(layer_dir / "meta.json", {
                "layer": name, "contract": "jiulong_dem_layers_v1",
                "generated_utc": log.payload["generated_utc"], "run_tag": args.run_tag,
                "crs": f"EPSG:{EPSG}", "resolution_m": float(grid["cellsize"]),
                "cell_area_m2": cell_area, "nodata": NODATA,
                "sha256": sha, "grid": {k: grid[k] for k in ("ncols", "nrows", "xllcorner", "north", "cellsize")},
                "parent_layer": parent, "operations_applied": ops,
                "changed_cells": rec["changed_cells"],
                "changed_area_km2": rec["changed_area_km2"],
                "max_abs_change_m": rec["max_abs_change_m"],
                "fill_volume_m3": rec["fill_volume_m3"],
                "uncertainty_headline": "vertical datum unclosed (G7); relative indicators only",
                "rollback_path": rec["rollback_path"],
            })
        layer_records[name] = rec

    # 契约硬检查：DEM_HYDRO 不得被当作 DEM_HYDRAULIC
    substitution = {
        "DEM_HYDRO_must_not_replace_DEM_HYDRAULIC": True,
        "enforced": True,
        "check": "本脚本不写 run 目录、不生成 run.par；替代必须另立 run + 独立试验 + 用户批准（计划 §3.2）",
        "hydro_differs_from_hydraulic_cells": int(np.count_nonzero(
            np.abs(layers["DEM_HYDRO"] - layers["DEM_HYDRAULIC"]) > 1e-9)),
        "hydro_equals_hydraulic": bool(np.allclose(
            layers["DEM_HYDRO"][valid], layers["DEM_HYDRAULIC"][valid])),
    }
    payload = {
        "case": "jiulong",
        "event": "2024-09 摩羯",
        "contract_version": "jiulong_dem_layers_v1",
        "script": "scripts/stage3/prepare_jiulong_dem_layers.py",
        "generated_utc": log.payload["generated_utc"],
        "run_tag": args.run_tag,
        "dry_run": dry,
        "grid": {
            "ncols": int(grid["ncols"]), "nrows": int(grid["nrows"]),
            "cellsize_m": float(grid["cellsize"]), "cell_area_m2": cell_area,
            "xllcorner": float(grid["xllcorner"]), "north": float(grid["north"]),
            "yllcorner_effective": float(grid["north"]) - int(grid["nrows"]) * float(grid["cellsize"]),
            "crs": f"EPSG:{EPSG}", "nodata": NODATA,
        },
        "vertical_datum": uncertainty["vertical_datum"],
        "sources": {"dem_source": src_meta,
                    "s1_water": {"path": str(S1_WATER.relative_to(PROJ)),
                                 "sha256": sha256_file(S1_WATER),
                                 "frac_threshold": S1_FRAC_THRESHOLD,
                                 "role": "WEAK 真实水体唯一可信来源"},
                    "reference_dem": {"path": str(REFERENCE_DEM.relative_to(PROJ)),
                                      "role": "仅用于地理配准核对，不作为输入"}},
        "georeferencing_check": georeferencing_check(grid, raw),
        "inputs": log.payload["inputs"],
        "layer_order": list(LAYER_ORDER),
        "layers": layer_records,
        "substitution_rules": substitution,
        "fill_policy": SELECTIVE_FILL_POLICY,
        "sea_anchor_policy": {
            "enabled": not bool(args.no_sea_anchor),
            "cells": int(np.count_nonzero(stack.reason == REASON_SEA_MSL_ANCHOR)),
            "rule": "与域边连通的 S1 常水像元若为负值，锚定为 0 m（ADR-0004：模型内部以 DEM 自身基准为基准）",
            "evidence": (
                "S1 常水掩膜 + 4 连通域边可达性（G16 口径）；该子集是唯一可判定为海/海湾的像元"
            ),
            "consistency_with_production": (
                "与 jiulong_scenarios.prep 的 v2 规则一致：既有生产 dem.asc 在这些像元上也是 0.0 m"
            ),
            "uncertainty": (
                "海面取 0 m 是基准约定，不是实测水位；垂直基准未闭合（G7），"
                "本层只用于相对量比较"
            ),
            "disable_with": "--no-sea-anchor",
        },
        "depression_inventory": inventory,
        "reference_full_fill_sensitivity": {
            "note": ("全域 Priority-Flood 对照值。**未采用**：会改 "
                     f"{reference_inventory['filled_cells']} 格 / "
                     f"{reference_inventory['filled_area_km2']} km²，"
                     "属于计划 §3.5 禁止的「默认替换水动力 DEM」量级"),
            **{k: v for k, v in reference_inventory.items() if k != "depressions"},
        },
        "real_depression_protection": protected_effect,
        "flat_surface_summary": {
            "note": "填洼后仍按像元数量报告平坦面与洼点；本文件不对平坦面是否存在下结论（计划 §2.3）",
            "DEM_RAW": flat_raw, "DEM_REPAIRED": flat_repaired,
            "DEM_HYDRO": flat_hydro, "DEM_HYDRAULIC": flat_hydraulic,
            "hydro_flat_vs_raw_delta_cells": flat_hydro["no_downslope_cells"] - flat_raw["no_downslope_cells"],
            "hydro_strict_pit_cells": flat_hydro["strict_pit_cells"],
            "preserved_pit_explanation": (
                "DEM_HYDRO 的严格洼点数量 = S1 常水受保护像元被恢复原值后形成的"
                "有意保留洼点；不是填洼遗漏，也不得据此声称「无洼地」"
            ),
        },
        "not_done": [
            "结构修复（bridge/orifice）：无授权资料，ADR-0008 要求先再验证",
            "垂直基准闭合：无控制点，datum_offset_m 保持 null（ADR-0004）",
            "12.5 m 原生层：本脚本默认 200 m（方案 A 生产基线），可用 --res 重跑",
            "全域最小坡度与固定刻蚀：计划 §3.5 明令禁止，未实现",
        ],
        "blocked": [
            {"item": "DEM_HYDRAULIC 结构修复", "reason": "缺排水/结构/断面授权资料", "owner": "用户/Phase 4"},
            {"item": "负值来源闭合", "reason": "缺 DEM 产品说明与测绘控制点（G7）", "owner": "用户"},
        ],
        "warnings": log.payload["warnings"],
        "catalog": rec_path_hint(catalog_path),
    }
    log.step("built_layers",
             changed_cells={k: v["changed_cells"] for k, v in layer_records.items()},
             fill_volume_m3=layer_records["DEM_HYDRO"]["fill_volume_m3"],
             protect_violations=layer_records["DEM_HYDRO"]["protect_violations"])
    log.payload["catalog"] = rec_path_hint(catalog_path)

    if not dry:
        write_json(catalog_path, payload)
    _print_summary(payload, dry)
    return 0


def _print_summary(payload: dict[str, Any], dry: bool) -> None:
    prefix = "[dry-run] " if dry else ""
    print(f"{prefix}四层 DEM（{payload['grid']['nrows']}x{payload['grid']['ncols']} @ "
          f"{payload['grid']['cellsize_m']:.1f} m）")
    for name in LAYER_ORDER:
        rec = payload["layers"][name]
        print(f"  {name:14s} changed={rec['changed_cells']:6d} "
              f"area={rec['changed_area_km2']:8.4f} km2 "
              f"max|d|={rec['max_abs_change_m']:7.3f} m "
              f"fill_vol={rec['fill_volume_m3']:12.1f} m3 "
              f"flat_no_downslope={rec['flat_surface_check']['no_downslope_cells']}")
    inv = payload["depression_inventory"]
    print(f"  已填微坑：{inv['n_depressions']} 个，填高 {inv['filled_cells']} 格 / "
          f"{inv['filled_area_km2']} km²，体积 {inv['fill_volume_m3']} m³，"
          f"最大抬高 {inv['max_raise_m']} m")
    hyd = payload["layers"]["DEM_HYDRO"]
    reg = hyd.get("registered_not_filled", {})
    print(f"  只登记未填：{reg.get('n_registered', 0)} 个洼地 / "
          f"{reg.get('filled_cells', 0)} 格 / {reg.get('fill_volume_m3', 0)} m³"
          f"（超预注册上限，须专家复核）")
    ref = payload["reference_full_fill_sensitivity"]
    print(f"  全域填洼对照（未采用）：{ref['filled_cells']} 格 / {ref['filled_area_km2']} km² / "
          f"{ref['fill_volume_m3']} m³")
    prot = payload["real_depression_protection"]
    print(f"  真实洼地保护：S1 常水 {prot['protected_cells']} 格；"
          f"无保护时会被抬高 {prot['raised_without_protection_cells']} 格 / "
          f"{prot['raised_without_protection_area_km2']} km²，"
          f"DEM_HYDRO 实际抬高 {prot['raised_in_dem_hydro_cells']} 格")
    sub = payload["substitution_rules"]
    print(f"  DEM_HYDRO≠DEM_HYDRAULIC：差异 {sub['hydro_differs_from_hydraulic_cells']} 格")
    geo = payload["georeferencing_check"]
    if geo.get("status") == "asc_header_shift_confirmed":
        print(f"  ⚠ 地理配准：既有 dem.asc 头声明的北缘比数据实际位置低 "
              f"{abs(geo['mismatch_m']):.2f} m（{abs(geo['mismatch_cells']):.3f} 格）")
    print(f"  {'（dry-run 未写任何文件）' if dry else '→ ' + payload.get('catalog', '')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="久隆四层 DEM 契约与选择性水文调理候选")
    add_common_args(parser)
    parser.add_argument("--res", type=float, default=200.0, help="模型网格分辨率（默认 200 m）")
    parser.add_argument(
        "--no-sea-anchor", action="store_true",
        help=("关闭 S1 海面基准锚定（不做「与域边连通的 S1 水体负值→0 m」修复）。"
              "默认开启，与既有生产流程 jiulong_scenarios.prep 的做法一致；"
              "关闭后该类像元保留负值并登记为未修复"),
    )
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
