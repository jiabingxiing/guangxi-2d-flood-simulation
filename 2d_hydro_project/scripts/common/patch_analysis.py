"""连通域与时空斑块分析（Phase 0/1 共用，纯 numpy 实现）。

为什么不用 scipy.ndimage / skimage：当前 uv 环境只有 numpy + rasterio。
计划 §2.1 把 scikit-image 列为"候选组件"，但引入前须复核许可证且未安装；
本模块用 numpy 实现 8 邻域连通域，行为可测、无新增依赖。

关键口径（计划 §3.3 / §3.4）：

- **物理面积优先**：所有阈值用 m²/km² 表达，像元数由 `cells_for_area` 换算。
  100 m 网格 1 格 = 0.01 km²；200 m 网格 1 格 = 0.04 km²。
- **连通域用 8 邻域**：4 邻域会把对角相连的真实积水切断成碎斑。
- 斑块统计含面积、最大/均值/分位水深、体积、边界框、质心。
- 时序追踪按斑块重叠（同格号）关联，输出出现帧数、连续帧数。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np

# 8 邻域偏移
_NEIGHBORS8 = (
    (-1, -1), (-1, 0), (-1, 1),
    (0, -1), (0, 1),
    (1, -1), (1, 0), (1, 1),
)


@dataclass
class Patch:
    """单个连通斑块。"""

    label: int
    n_cells: int
    area_km2: float
    max_depth_m: float
    mean_depth_m: float
    p95_depth_m: float
    volume_m3: float
    row_min: int
    row_max: int
    col_min: int
    col_max: int
    centroid_row: float
    centroid_col: float
    cell_indices: np.ndarray = field(repr=False, default_factory=lambda: np.empty(0, np.int64))

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "n_cells": self.n_cells,
            "area_km2": round(self.area_km2, 6),
            "max_depth_m": round(self.max_depth_m, 4),
            "mean_depth_m": round(self.mean_depth_m, 4),
            "p95_depth_m": round(self.p95_depth_m, 4),
            "volume_m3": round(self.volume_m3, 1),
            "bbox": [self.row_min, self.col_min, self.row_max, self.col_max],
            "centroid_rowcol": [round(self.centroid_row, 2), round(self.centroid_col, 2)],
        }


def connected_components(
    mask: np.ndarray, *, connectivity: int = 8
) -> tuple[np.ndarray, int]:
    """标记连通域；返回 (labels, n_labels)。背景为 0，斑块从 1 开始。

    用迭代式 BFS（显式栈），不递归，避免大斑块触发 RecursionError。
    """
    mask = np.asarray(mask, dtype=bool)
    labels = np.zeros(mask.shape, dtype=np.int32)
    if not mask.any():
        return labels, 0
    if connectivity == 4:
        offsets = ((-1, 0), (1, 0), (0, -1), (0, 1))
    elif connectivity == 8:
        offsets = _NEIGHBORS8
    else:
        raise ValueError(f"connectivity 只支持 4 或 8，收到 {connectivity}")

    height, width = mask.shape
    label = 0
    for r0 in range(height):
        row_mask = mask[r0]
        for c0 in range(width):
            if not row_mask[c0] or labels[r0, c0]:
                continue
            label += 1
            labels[r0, c0] = label
            queue = deque([(r0, c0)])
            while queue:
                cr, cc = queue.popleft()
                for dr, dc in offsets:
                    nr, nc = cr + dr, cc + dc
                    if 0 <= nr < height and 0 <= nc < width and mask[nr, nc] and not labels[nr, nc]:
                        labels[nr, nc] = label
                        queue.append((nr, nc))
    return labels, label


def patch_stats(
    depth: np.ndarray,
    labels: np.ndarray,
    n_labels: int,
    *,
    cell_area_m2: float,
) -> list[Patch]:
    """按标签汇总斑块统计。`cell_area_m2` 必须来自栅格头，不得写死。"""
    patches: list[Patch] = []
    if n_labels == 0:
        return patches
    flat_labels = labels.ravel()
    flat_depth = depth.ravel().astype(np.float64, copy=False)
    order = np.argsort(flat_labels, kind="stable")
    sorted_labels = flat_labels[order]
    boundaries = np.flatnonzero(np.diff(sorted_labels)) + 1
    groups = np.split(order, boundaries)
    height, width = labels.shape
    for group in groups:
        if group.size == 0:
            continue
        lab = int(flat_labels[group[0]])
        if lab == 0:
            continue
        rows, cols = np.divmod(group, width)
        vals = flat_depth[group]
        patches.append(
            Patch(
                label=lab,
                n_cells=int(group.size),
                area_km2=float(group.size) * cell_area_m2 / 1e6,
                max_depth_m=float(np.max(vals)),
                mean_depth_m=float(np.mean(vals)),
                p95_depth_m=float(np.percentile(vals, 95)),
                volume_m3=float(np.sum(vals)) * cell_area_m2,
                row_min=int(rows.min()), row_max=int(rows.max()),
                col_min=int(cols.min()), col_max=int(cols.max()),
                centroid_row=float(rows.mean()), centroid_col=float(cols.mean()),
                cell_indices=group.astype(np.int64),
            )
        )
    patches.sort(key=lambda p: (-p.area_km2, p.label))
    return patches


def depth_quantiles(depth: np.ndarray, mask: np.ndarray, *, prefix: str = "") -> dict[str, float]:
    """掩膜内水深的常用分位（供审计 JSON）。"""
    vals = np.asarray(depth, dtype=np.float64)[mask]
    if vals.size == 0:
        return {f"{prefix}p50": 0.0, f"{prefix}p90": 0.0, f"{prefix}p99": 0.0,
                f"{prefix}max": 0.0, f"{prefix}mean": 0.0}
    return {
        f"{prefix}p50": round(float(np.percentile(vals, 50)), 4),
        f"{prefix}p90": round(float(np.percentile(vals, 90)), 4),
        f"{prefix}p99": round(float(np.percentile(vals, 99)), 4),
        f"{prefix}max": round(float(np.max(vals)), 4),
        f"{prefix}mean": round(float(np.mean(vals)), 4),
    }


@dataclass
class FramePatchSummary:
    """单帧斑块摘要。"""

    seq: int
    n_patches: int
    wet_cells: int
    wet_area_km2: float
    wet_volume_m3: float
    max_depth_m: float
    patches: list[Patch] = field(repr=False, default_factory=list)
    depth_stats: dict[str, float] = field(default_factory=dict)

    def to_dict(self, *, top_n: int | None = None) -> dict[str, Any]:
        picked = self.patches if top_n is None else self.patches[:top_n]
        return {
            "seq": self.seq,
            "n_patches": self.n_patches,
            "wet_cells": self.wet_cells,
            "wet_area_km2": round(self.wet_area_km2, 4),
            "wet_volume_m3": round(self.wet_volume_m3, 1),
            "max_depth_m": round(self.max_depth_m, 4),
            **self.depth_stats,
            "patches": [p.to_dict() for p in picked],
        }


def summarize_frame(
    depth: np.ndarray,
    seq: int,
    *,
    wet_threshold_m: float,
    cell_area_m2: float,
    sea_mask: np.ndarray | None = None,
    connectivity: int = 8,
    keep_patches: bool = True,
) -> FramePatchSummary:
    """对一帧水深做斑块统计。

    湿地判定：`depth > wet_threshold_m` 且非 NaN、非海域。
    """
    finite = np.isfinite(depth)
    wet = finite & (depth > wet_threshold_m)
    if sea_mask is not None:
        wet &= ~np.asarray(sea_mask, dtype=bool)
    labels, n_labels = connected_components(wet, connectivity=connectivity)
    patches = patch_stats(depth, labels, n_labels, cell_area_m2=cell_area_m2)
    wet_volume = float(np.sum(np.where(wet, depth, 0.0))) * cell_area_m2
    return FramePatchSummary(
        seq=seq,
        n_patches=n_labels,
        wet_cells=int(np.count_nonzero(wet)),
        wet_area_km2=float(np.count_nonzero(wet)) * cell_area_m2 / 1e6,
        wet_volume_m3=wet_volume,
        max_depth_m=float(np.max(depth[wet])) if wet.any() else 0.0,
        patches=patches if keep_patches else [],
        depth_stats=depth_quantiles(depth, wet),
    )


@dataclass
class PatchTrack:
    """跨帧斑块轨迹：按像元重叠关联。"""

    track_id: int
    frames: list[int] = field(default_factory=list)
    areas_km2: list[float] = field(default_factory=list)
    max_depths_m: list[float] = field(default_factory=list)
    volume_m3: list[float] = field(default_factory=list)
    centroid_row: list[float] = field(default_factory=list)
    centroid_col: list[float] = field(default_factory=list)

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    @property
    def longest_run(self) -> int:
        """最长连续帧数。"""
        if not self.frames:
            return 0
        best = run = 1
        for prev, cur in zip(self.frames, self.frames[1:]):
            run = run + 1 if cur == prev + 1 else 1
            best = max(best, run)
        return best

    @property
    def max_area_km2(self) -> float:
        return max(self.areas_km2) if self.areas_km2 else 0.0

    @property
    def max_depth_m(self) -> float:
        return max(self.max_depths_m) if self.max_depths_m else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "frames": self.frames,
            "frame_count": self.frame_count,
            "longest_consecutive_run": self.longest_run,
            "max_area_km2": round(self.max_area_km2, 6),
            "max_depth_m": round(self.max_depth_m, 4),
            "first_frame": self.frames[0] if self.frames else None,
            "last_frame": self.frames[-1] if self.frames else None,
            "mean_area_km2": round(float(np.mean(self.areas_km2)), 6) if self.areas_km2 else 0.0,
            "volume_peak_m3": round(max(self.volume_m3), 1) if self.volume_m3 else 0.0,
            "centroid_first": (
                [round(self.centroid_row[0], 2), round(self.centroid_col[0], 2)]
                if self.centroid_row else None
            ),
        }


class PersistenceTracker:
    """按时序把斑块串成轨迹。

    关联规则：当前帧斑块与上一帧轨迹的**任一成员斑块共享像元**即继承该轨迹。
    一个斑块可继承多个轨迹时，合并到最早建立的轨迹（并记录 merge 事件）。
    """

    def __init__(self) -> None:
        self.tracks: dict[int, PatchTrack] = {}
        self._next_id = 1
        self._cell_owner: dict[int, int] = {}
        self.merge_events: list[dict[str, Any]] = []

    def update(self, summary: FramePatchSummary, *, width: int) -> None:
        assigned: dict[int, int] = {}
        claimed: dict[int, list[int]] = {}
        for patch in summary.patches:
            owners = {self._cell_owner[c] for c in patch.cell_indices.tolist() if c in self._cell_owner}
            if not owners:
                track_id = self._next_id
                self._next_id += 1
                self.tracks[track_id] = PatchTrack(track_id=track_id)
            else:
                track_id = min(owners)
                if len(owners) > 1:
                    self.merge_events.append({
                        "frame": summary.seq, "kept": track_id,
                        "merged": sorted(owners - {track_id}),
                    })
                    for other in sorted(owners - {track_id}):
                        if other in self.tracks:
                            self.tracks[other].frames.append(summary.seq)
            assigned[patch.label] = track_id
            claimed.setdefault(track_id, []).extend(patch.cell_indices.tolist())
            track = self.tracks[track_id]
            if not track.frames or track.frames[-1] != summary.seq:
                track.frames.append(summary.seq)
                track.areas_km2.append(patch.area_km2)
                track.max_depths_m.append(patch.max_depth_m)
                track.volume_m3.append(patch.volume_m3)
                track.centroid_row.append(patch.centroid_row)
                track.centroid_col.append(patch.centroid_col)
            else:
                track.areas_km2[-1] = max(track.areas_km2[-1], patch.area_km2)
                track.max_depths_m[-1] = max(track.max_depths_m[-1], patch.max_depth_m)
                track.volume_m3[-1] += patch.volume_m3
        self._cell_owner = {c: t for t, cells in claimed.items() for c in cells}
        _ = width  # 保留参数以便将来做 2D 邻域关联

    def summaries(self) -> list[dict[str, Any]]:
        return [t.to_dict() for t in sorted(self.tracks.values(), key=lambda x: x.track_id)]


def classify_patch(
    track: dict[str, Any],
    *,
    area_threshold_km2: float,
    persistence_frames: int,
    shallow_threshold_m: float,
    exemption_area_km2: float,
) -> tuple[str, str]:
    """斑块分类：返回 (verdict, reason_code)。

    verdict:
      - `exempt`      豁免（大、持续、深）
      - `filter`      过滤候选（小且短暂/浅）
      - `keep`        保留（不满足过滤条件）

    规则顺序严格按计划 §3.3 第 4–5 条：先判豁免，再判过滤。
    """
    if track["max_area_km2"] >= exemption_area_km2:
        return "exempt", "EXEMPT_LARGE_AREA"
    if track["longest_consecutive_run"] >= persistence_frames:
        return "exempt", "EXEMPT_PERSISTENT"
    if track["max_depth_m"] >= shallow_threshold_m * 3:
        return "exempt", "EXEMPT_DEEP"
    if track["max_area_km2"] < area_threshold_km2:
        return "filter", "FILTER_SMALL_AREA"
    if track["max_depth_m"] < shallow_threshold_m:
        return "filter", "FILTER_SHALLOW"
    return "keep", "KEEP_OTHER"
