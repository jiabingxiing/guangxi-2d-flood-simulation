"""Phase H 研究性产品图层（FAT / 成因归因 / 集合概率）进发布包的样式与统计。

口径（ADR-0015/0016/0018；计划 §7.1 发布门）：

- 产品以**单帧伪 run** 进入 `catalog.runs[]`（role=product），复用水深管线的
  数值块 / 分幅 / 统计契约；点查走数值块真值，不走 PNG 颜色；
- 统计文档保持标准 schema（thresholds_m 与 catalog.display 一致、unit_area=km2），
  阈值语义由 `product.value_semantics` 声明为**产品值**（小时 / 概率 / 类别码），
  不是水深米——`not_depth_note` 强制出现，防止误读；
- 全部图层带 `research_product=true` 与派生声明；不改 default_run_id，禁业务告警。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from ..raster.paint import DepthClass, PaintError, parse_color

SCHEMA_VERSION = "1.0"

# kind → (unit, 值语义, 透明阈, 分级)；threshold_m 只对 exceed/duration 有意义
_HOURS_CLASSES = (
    (0.0, 6.0, "#fde725", "0–6 h（最早）"),
    (6.0, 12.0, "#a5db36", "6–12 h"),
    (12.0, 24.0, "#21918c", "12–24 h"),
    (24.0, 48.0, "#3b528b", "24–48 h"),
    (48.0, 96.0, "#440154", "48–96 h"),
    (96.0, None, "#8c8c8c", "≥96 h"),
)
_PROB_CLASSES = (
    (0.0, 0.25, "#fdd49e", "P 0–25%"),
    (0.25, 0.5, "#fc8d59", "P 25–50%"),
    (0.5, 0.75, "#d73027", "P 50–75%"),
    (0.75, 1.0, "#b30000", "P 75–100%"),
)
_CLASS_MAP_CLASSES = (
    (0.5, 1.5, "#2c7fb8", "1 river-origin（有雨/无雨差分）"),
    (1.5, 2.5, "#f9a648", "2 pluvial-origin（降雨积水）"),
    (2.5, 3.5, "#b0b0b0", "3 edge-band（边界环带）"),
    (3.5, 4.5, "#7a0177", "4 disagreement（水量再分配）"),
)

VALUE_SEMANTICS = {
    "fat_first_wet_h": "值=首湿时刻（自模拟起点小时；wd>0.05 m 首次超过的 6 h 快照）；NaN=事件全程未湿",
    "fat_exceed_h": "值=首次 wd>情景阈值 的时刻（h）；NaN=全程未达该情景阈值",
    "fat_duration_h": "值=累计超阈小时数；NaN=未湿",
    "ensemble_probability": "值=集合成员超阈占比 P∈[0,1]（成员=PASS run 清单）；"
    "情景集合枚举，非年超越概率/预报概率",
    "class_map": "值=差分归类码：0 未湿 / 1 river-origin / 2 pluvial-origin / 3 edge-band / 4 disagreement",
}


@dataclass(frozen=True)
class ProductStyle:
    """产品图层着色与阈值语义。

    render_zero_epsilon=True（小时类）：精确 0 h（首帧即湿，如常水河槽）在渲染时
    平移为 1e-4 h 进入 0–6 h 分级（schema 要求 transparent_below_m ≥ 0）；
    数值块保真 0，点查不受影响。
    """

    kind: str
    unit: str
    value_semantics: str
    transparent_below: float
    depth_classes: list[DepthClass]
    not_depth_note: str
    render_zero_epsilon: bool = False

    def class_dicts(self) -> list[dict[str, Any]]:
        return [c.to_dict() for c in self.depth_classes]


def _classes_from(specs, prefix: str) -> list[DepthClass]:
    out: list[DepthClass] = []
    for min_m, max_m, color, label in specs:
        out.append(DepthClass(min_m=min_m, max_m=max_m, color=color,
                              rgb=parse_color(color), label=f"{prefix}{label}"))
    return out


def style_for(kind: str, *, threshold_m: float | None = None) -> ProductStyle:
    """按产品类型给出色带与透明阈；未知 kind 抛 PaintError。"""
    if kind in ("fat_first_wet_h", "fat_exceed_h", "fat_duration_h"):
        prefix = "" if kind == "fat_first_wet_h" else f"[>{threshold_m:g} m] " if threshold_m else ""
        return ProductStyle(
            kind=kind, unit="h", value_semantics=VALUE_SEMANTICS[kind],
            transparent_below=0.0,
            depth_classes=_classes_from(_HOURS_CLASSES, prefix),
            not_depth_note="阈值为小时数（到达/历时），不是水深；色带仅作展示分级",
            render_zero_epsilon=True,
        )
    if kind == "ensemble_probability":
        return ProductStyle(
            kind=kind, unit="probability", value_semantics=VALUE_SEMANTICS[kind],
            transparent_below=0.0,  # P=0（无成员超阈）透明
            depth_classes=_classes_from(_PROB_CLASSES, ""),
            not_depth_note="阈值为概率值，不是水深；P 越高仅代表情景集合内一致性强",
        )
    if kind == "class_map":
        return ProductStyle(
            kind=kind, unit="class", value_semantics=VALUE_SEMANTICS[kind],
            transparent_below=0.5,  # 类别码 0（未湿）透明
            depth_classes=_classes_from(_CLASS_MAP_CLASSES, ""),
            not_depth_note="阈值为类别码边界，不是水深；差分归因不得称精确成因分解",
        )
    raise PaintError(f"未知产品图层类型：{kind!r}；允许 {sorted(VALUE_SEMANTICS)}")


def read_product_array(path, grid) -> np.ndarray:
    """读产品 GeoTIFF → 最近邻重投影到案例网格的 float32 数组（nodata→NaN）。"""
    import rasterio
    from rasterio.warp import Resampling, reproject

    dst = np.full((grid.rows, grid.cols), np.nan, dtype="float32")
    with rasterio.open(path) as ds:
        band = ds.read(1).astype("float32")
        nodata = ds.nodata
        reproject(
            band,
            dst,
            src_transform=ds.transform,
            src_crs=ds.crs,
            src_nodata=nodata,
            dst_transform=grid.transform,
            dst_crs=grid.crs,
            dst_nodata=np.nan,
            resampling=Resampling.nearest,
        )
    if nodata is not None:
        dst[dst == np.float32(nodata)] = np.nan
    return dst


def product_stats_doc(
    *,
    case_id: str,
    product_id: str,
    arr: np.ndarray,
    cell_area_m2: float,
    thresholds: list[float],
    style: ProductStyle,
    frame_id: str = "f0000",
    derivation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """标准 schema 统计文档 + `product` 子文档（诚实语义声明）。

    标准部分沿用 quality.stats.compute_areas：areas_km2[t] = 值>t 的有效像元面积；
    阈值语义（小时/概率/类别码）由 product.value_semantics 与 not_depth_note 声明。
    """
    from ..quality.stats import compute_areas

    result = compute_areas(arr, thresholds, cell_area_m2)
    frame_record = {
        "frame_id": frame_id,
        "sim_seconds": 0.0,
        "timestamp": None,
        "areas_km2": result["areas_km2"],
        "wetted_cells": result["wetted_cells"],
        "max_depth_m": round(float(result["max_depth_m"]), 4),
    }
    finite = np.isfinite(arr)
    valid = finite & (arr > np.float32(style.transparent_below))
    breakdown: list[dict[str, Any]] = []
    for cls in style.depth_classes:
        mask = valid & (arr > np.float32(cls.min_m))
        if cls.max_m is not None:
            mask &= arr <= np.float32(cls.max_m)
        cells = int(np.count_nonzero(mask))
        breakdown.append({
            "label": cls.label, "color": cls.color, "cells": cells,
            "area_km2": round(cells * cell_area_m2 / 1e6, 4),
        })
    doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id,
        "run_id": product_id,
        "unit_area": "km2",
        "thresholds_m": [float(t) for t in thresholds],
        "grid_cell_area_m2": float(cell_area_m2),
        "frames": [frame_record],
        "max_envelope": {
            "areas_km2": dict(frame_record["areas_km2"]),
            "wetted_cells": dict(frame_record["wetted_cells"]),
            "peak_frame_id": frame_id,
            "peak_time_s": 0.0,
        },
        "product": {
            "kind": style.kind,
            "unit": style.unit,
            "value_semantics": style.value_semantics,
            "not_depth_note": style.not_depth_note,
            "value_summary": {
                "valid_cells": int(np.count_nonzero(finite)),
                "rendered_cells": int(np.count_nonzero(valid)),
                "nan_cells": int(np.count_nonzero(~finite)),
                "min": round(float(np.nanmin(arr)), 4) if finite.any() else None,
                "max": round(float(np.nanmax(arr)), 4) if finite.any() else None,
                "mean": round(float(np.nanmean(arr)), 4) if finite.any() else None,
            },
            "class_breakdown": breakdown,
            "threshold_semantics": (
                f"标准统计字段（areas_km2/thresholds_m）在此图层上作用于产品值"
                f"（单位 {style.unit}），不是水深米；水深阈值键仅为包内契约统一"
            ),
            "derivation": dict(derivation or {}),
            "research_product": True,
            "not_business_warning": True,
        },
    }
    return doc


def threshold_keys(thresholds: list[float]) -> list[str]:
    from ..quality.stats import threshold_key

    return [threshold_key(t) for t in thresholds]
