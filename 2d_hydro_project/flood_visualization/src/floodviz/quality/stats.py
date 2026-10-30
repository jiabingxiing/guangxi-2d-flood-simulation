"""逐帧面积统计与全程最大包络统计（SPEC §6）。

口径（不可变）：
- 面积为**原生模型网格**有效像元（非 NaN、非海域 mask）中 `depth > threshold` 的像元数 × 格面积；
  不是资产安全等级，也不是从 PNG 颜色反算的面积。
- 阈值键用字符串（`"0.05"`、`"1.0"`…，与 Python `str(float(t))` 一致）。
- 最大包络取自 run 目录 `results/res.max`（模型自带的全程最大水深），
  不是"把逐帧取最大"重算——两者在真实输出上应一致，validate 会交叉核对。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

SCHEMA_VERSION = "1.0"
UNIT_AREA = "km2"


class StatsError(ValueError):
    """统计输入不合法。"""


def threshold_key(threshold_m: float) -> str:
    """阈值 → 字符串键，与 yaml 浮点写法一致（0.05 / 0.3 / 1.0 / 10.0）。"""
    return str(float(threshold_m))


def threshold_keys(thresholds: Sequence[float]) -> list[str]:
    keys = [threshold_key(t) for t in thresholds]
    if len(set(keys)) != len(keys):
        raise StatsError(f"阈值去重后仍有重复键：{keys}")
    return keys


def valid_cell_mask(depth: np.ndarray, sea_mask: np.ndarray | None = None) -> np.ndarray:
    """有效像元 = 非 NaN 且非海域。"""
    mask = ~np.isnan(depth)
    if sea_mask is not None:
        if sea_mask.shape != depth.shape:
            raise StatsError(f"海域 mask 形状 {sea_mask.shape} 与网格 {depth.shape} 不符")
        mask &= ~sea_mask
    return mask


def compute_areas(
    depth: np.ndarray,
    thresholds: Sequence[float],
    cell_area_m2: float,
    sea_mask: np.ndarray | None = None,
) -> dict[str, Any]:
    """单幅水深 → {areas_km2, wetted_cells, valid_cell_count, sea_cell_count, max_depth_m}。"""
    arr = np.asarray(depth)
    if arr.ndim != 2:
        raise StatsError(f"水深必须是二维，实得 {arr.shape}")
    if cell_area_m2 <= 0:
        raise StatsError(f"格面积必须为正：{cell_area_m2}")
    valid = valid_cell_mask(arr, sea_mask)
    areas: dict[str, float] = {}
    cells: dict[str, int] = {}
    for threshold in thresholds:
        wet = valid & (arr > np.float32(threshold))
        count = int(np.count_nonzero(wet))
        cells[threshold_key(threshold)] = count
        areas[threshold_key(threshold)] = round(count * float(cell_area_m2) / 1e6, 6)
    finite = np.isfinite(arr)
    return {
        "areas_km2": areas,
        "wetted_cells": cells,
        "valid_cell_count": int(np.count_nonzero(valid)),
        "sea_cell_count": int(np.count_nonzero(sea_mask)) if sea_mask is not None else 0,
        "max_depth_m": float(np.nanmax(arr)) if finite.any() else 0.0,
    }


def envelope_stats(
    max_depth: np.ndarray,
    thresholds: Sequence[float],
    cell_area_m2: float,
    sea_mask: np.ndarray | None = None,
) -> dict[str, Any]:
    """全程最大包络面积统计（输入：res.max 或逐帧取最大的结果）。"""
    result = compute_areas(max_depth, thresholds, cell_area_m2, sea_mask)
    return {
        "areas_km2": result["areas_km2"],
        "wetted_cells": result["wetted_cells"],
        "max_depth_m": result["max_depth_m"],
    }


@dataclass
class StatisticsBuilder:
    """流式累积器：逐帧喂水深，常量内存（只保留标量统计）。"""

    thresholds: Sequence[float]
    cell_area_m2: float
    default_threshold_m: float
    sea_mask: np.ndarray | None = None
    frames: list[dict[str, Any]] = field(default_factory=list)
    _valid_cell_count: int | None = None
    _sea_cell_count: int = 0
    _peak_area: float = -1.0
    _peak_frame_id: str | None = None
    _peak_time_s: float | None = None
    _peak_depth: float = -1.0
    _peak_depth_frame_id: str | None = None

    def __post_init__(self) -> None:
        threshold_keys(self.thresholds)

    def add_frame(
        self,
        *,
        frame_id: str,
        sim_seconds: float,
        timestamp: str | None,
        depth: np.ndarray,
    ) -> dict[str, Any]:
        """累计一帧；返回该帧的完整统计记录（供 images.json 复用摘要）。"""
        result = compute_areas(depth, self.thresholds, self.cell_area_m2, self.sea_mask)
        if self._valid_cell_count is None:
            self._valid_cell_count = result["valid_cell_count"]
            self._sea_cell_count = result["sea_cell_count"]
        key = threshold_key(self.default_threshold_m)
        area = float(result["areas_km2"][key])
        if area > self._peak_area:
            self._peak_area = area
            self._peak_frame_id = frame_id
            self._peak_time_s = float(sim_seconds)
        if result["max_depth_m"] > self._peak_depth:
            self._peak_depth = float(result["max_depth_m"])
            self._peak_depth_frame_id = frame_id
        record = {
            "frame_id": frame_id,
            "sim_seconds": float(sim_seconds),
            "timestamp": timestamp,
            "areas_km2": result["areas_km2"],
            "wetted_cells": result["wetted_cells"],
            "max_depth_m": round(float(result["max_depth_m"]), 4),
        }
        self.frames.append(record)
        return record

    def to_dict(
        self,
        *,
        case_id: str,
        run_id: str,
        max_envelope: dict[str, Any] | None,
    ) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "case_id": case_id,
            "run_id": run_id,
            "unit_area": UNIT_AREA,
            "thresholds_m": [float(t) for t in self.thresholds],
            "grid_cell_area_m2": float(self.cell_area_m2),
            "valid_cell_count": int(self._valid_cell_count or 0),
            "valid_cell_count_basis": "首帧有效像元数（非 NaN 且非海域）",
            "sea_cell_count": int(self._sea_cell_count),
            "area_note": (
                "面积为原生网格单帧有效像元 depth>阈值 的面积；剔除 NaN 与海域像元；"
                "不是资产安全等级，也不从显示图颜色反算"
            ),
            "frames": self.frames,
            "envelope_source": "results/res.max（模型自带全程最大水深）",
            "peak_criterion": f"默认阈值 {self.default_threshold_m} m 下淹没面积最大的帧",
            "peak_frame_id": self._peak_frame_id,
            "peak_time_s": self._peak_time_s,
            "peak_max_depth_frame_id": self._peak_depth_frame_id,
            "peak_max_depth_m": round(float(self._peak_depth), 4) if self._peak_depth >= 0 else None,
        }
        if max_envelope is not None:
            doc["max_envelope"] = {
                "areas_km2": max_envelope["areas_km2"],
                "wetted_cells": max_envelope["wetted_cells"],
                "max_depth_m": round(float(max_envelope.get("max_depth_m", 0.0)), 4),
                "peak_frame_id": self._peak_frame_id,
                "peak_time_s": self._peak_time_s,
                "peak_criterion": f"默认阈值 {self.default_threshold_m} m 下淹没面积最大的帧",
            }
        return doc

    @property
    def max_depth_seen(self) -> float:
        return float(self._peak_depth) if self._peak_depth >= 0 else 0.0


def compare_stats(expected: dict[str, Any], actual: dict[str, Any], *, rel_tol: float = 0.001) -> list[str]:
    """比较两份逐帧统计；返回差异说明（空列表 = 一致）。用于 validate 复算门禁。"""
    problems: list[str] = []
    exp_frames = {f["frame_id"]: f for f in expected.get("frames", [])}
    act_frames = {f["frame_id"]: f for f in actual.get("frames", [])}
    if set(exp_frames) != set(act_frames):
        problems.append(
            f"帧集合不一致：缺 {sorted(set(exp_frames) - set(act_frames))[:5]} "
            f"多 {sorted(set(act_frames) - set(exp_frames))[:5]}"
        )
    for frame_id in sorted(set(exp_frames) & set(act_frames)):
        exp, act = exp_frames[frame_id], act_frames[frame_id]
        for key in exp.get("areas_km2", {}):
            e = float(exp["areas_km2"][key])
            a = float(act.get("areas_km2", {}).get(key, float("nan")))
            if not np.isfinite(a):
                problems.append(f"{frame_id} 阈值 {key} 缺统计")
                continue
            diff = abs(a - e)
            scale = max(abs(e), 1e-9)
            if diff > max(rel_tol * scale, 1e-9):
                problems.append(f"{frame_id} 阈值 {key} 面积 {a} vs 源 {e}（相对差 {diff / scale:.4%}）")
        for key in exp.get("wetted_cells", {}):
            e = int(exp["wetted_cells"][key])
            a = int(act.get("wetted_cells", {}).get(key, -1))
            if a != e:
                problems.append(f"{frame_id} 阈值 {key} 像元数 {a} vs 源 {e}")
    return problems


def envelope_from_frames(frames: Iterable[dict[str, Any]], thresholds: Sequence[float]) -> dict[str, dict[str, Any]]:
    """把逐帧记录合成包络统计（用于与 res.max 交叉核对）。"""
    keys = threshold_keys(thresholds)
    return {
        "areas_km2": {k: round(float(max((f["areas_km2"][k] for f in frames), default=0.0)), 6) for k in keys},
        "wetted_cells": {k: int(max((f["wetted_cells"][k] for f in frames), default=0)) for k in keys},
    }
