"""久隆水淹斑块物理归因（P01–P08）与 15 项指标（计划 §4.1/§4.2 任务 1）。

对 200 m 生产基线（scen_free_200m_sv6h）``results/res.max`` 的全部湿斑块
（depth > 0.05 m，剔除 sea_mask）做确定性分类；逐斑块 15 项指标、分类栅格与
类别汇总写入 ``diagnostics/jiulong/patch_attribution/``。派生层不含
``res.mass``，不进入 g15 质量门；证据等级 E1（基于冻结的诊断快照与 run 产物派生）。

分类规则（预注册、确定性，按优先级自上而下命中即停；阈值回显到 class_summary.json）::

    P05 海面钳零     class==1 像元占比 ≥ 0.5
    P02 地形数据异常 class∈{2,3,5} 像元占比 ≥ 0.5（在 P05 之后判定）
    P07 边界异常     斑块触及网格边缘（在 P05/P02 之后判定）
    P06 数值薄水     median_depth < 0.10 m 且 persistence_frames < 3
                     （轨迹未匹配记 1，同样视为短持续）
    P03 河网断裂积水 dist_river ≤ 1000 m 且 HAND ≤ 3 m 且
                     area < 0.5 × 最大斑块面积（近河孤立水体）
    P01 自然洼地蓄水 median ≥ 0.10 m 且 edit_overlap == 0 且 HAND ≤ 5 m
                     且 flow_acc ≥ 100 格
    P04 局地降雨积水 median ≥ 0.10 m（其余情形）
    P08 地表排水受阻 兜底（以上全部未命中）

置信度 = 该类预注册支持条件（``_RULES`` 第二元组，说明文字回显到
class_summary.json）的命中数：≥3 → high，2 → medium，≤1 → low。

verdict（计划 §4.2 展示质控）：P02 → ``repair_first``，P06 → ``filter_eligible``，
其余 → ``preserve``（禁止直接删除）。

时间指标来自 ``patch_tracks.csv`` 的空间近似匹配：轨迹表无质心，以
``patch_persistence.csv`` 逐帧斑块质心（与 patch 质心距离 ≤ 1 格）为桥梁，
命中帧序号落在轨迹 [first_frame, last_frame] 窗口即候选，多候选取
frame_count 最大（并列取 track_id 最小）；未匹配记 -1、persistence=1（诚实降级）。
不重扫任何帧文件。

distance_to_drainage 为 drainage_mask（汇流累积 ≥ 1250 格或河道像元）的
BFS 距离派生值（S1 排水数据不可得）；HAND / 汇流累积为 Priority-Flood(+ε)
填洼后的派生诊断量，只用于归因，不得送入水动力求解器。

用法（先设 ``PYTHONUTF8=1``）::

    uv --directory 2d_hydro_project run python scripts/stage6/diagnose_jiulong_patch_attribution.py --dry-run
    uv --directory 2d_hydro_project run python scripts/stage6/diagnose_jiulong_patch_attribution.py --run-tag v1
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import deque
from pathlib import Path
from typing import Any, Callable

import numpy as np

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import (  # noqa: E402
    AsciiGrid,
    AuditLog,
    GridError,
    add_common_args,
    guard_no_overwrite,
    load_sea_mask,
    parse_run_par,
    read_ascii_grid,
    sha256_file,
    utc_now,
    write_json,
)
from scripts.common.patch_analysis import connected_components, patch_stats  # noqa: E402
from scripts.common.terrain_hydro import (  # noqa: E402
    d8_receiver,
    flow_accumulation,
    hand,
    priority_flood,
)

JIU = PROJ / "outputs" / "jiulong" / "2024-09-yagi"
RUN_BASELINE = JIU / "scen_free_200m_sv6h"
ARTIFACT_MAP = PROJ / "diagnostics" / "jiulong" / "dem_artifact_map.tif"
PATCH_TRACKS = PROJ / "diagnostics" / "jiulong" / "patch_tracks.csv"
PATCH_PERSIST = PROJ / "diagnostics" / "jiulong" / "patch_persistence.csv"
RIVER_CENTERLINE = PROJ / "data" / "interim" / "jiulong" / "channel" / "river_centerline.geojson"
DEFAULT_OUT = PROJ / "diagnostics" / "jiulong" / "patch_attribution"
DEFAULT_LOG_DIR = PROJ / "logs" / "diagnostics"

WET_THRESHOLD_M = 0.05
FRAME_HOURS = 6.0  # run.par saveint=21600 s；不一致时告警并按实际帧距换算
SEA_FRAC_MIN = 0.5  # P05：class==1 像元占比
DEM_EDIT_FRAC_MIN = 0.5  # P02：class∈{2,3,5} 像元占比
P06_MEDIAN_MAX_M = 0.10
P06_PERSISTENCE_MAX = 3  # 严格小于
P03_DIST_RIVER_MAX_M = 1000.0
P03_HAND_MAX_M = 3.0
P03_ISOLATION_FRAC = 0.5  # area < 0.5 × 最大斑块面积
P01_HAND_MAX_M = 5.0
P01_ACC_MIN_CELLS = 100
DRAINAGE_ACC_MIN_CELLS = 1250  # 200 m 格 1250 格 = 50 km²
P08_DIST_DRAIN_MIN_M = 500.0  # 仅作 P08 支持条件，非门槛
RIVER_DENSIFY_STEP_M = 50.0

# class → (中文名, 栅格码 1..8, verdict)
CLASS_INFO: dict[str, tuple[str, int, str]] = {
    "P01": ("自然洼地蓄水", 1, "preserve"),
    "P02": ("地形数据异常", 2, "repair_first"),
    "P03": ("河网断裂积水", 3, "preserve"),
    "P04": ("局地降雨积水", 4, "preserve"),
    "P05": ("海面钳零", 5, "preserve"),
    "P06": ("数值薄水", 6, "filter_eligible"),
    "P07": ("边界异常", 7, "preserve"),
    "P08": ("地表排水受阻", 8, "preserve"),
}
CLASS_ORDER = ("P05", "P02", "P07", "P06", "P03", "P01", "P04", "P08")  # 判定优先级
OUTPUT_NAMES = ("patch_attribution.csv", "patch_class_map.tif", "class_summary.json")

CSV_COLUMNS = (
    "patch_id", "patch_area_km2", "max_depth_m", "mean_depth_m", "median_depth_m",
    "water_volume_m3", "persistence_frames", "continuous_duration_h",
    "appearance_time_h", "disappearance_time_h", "distance_to_river_m",
    "distance_to_drainage_m", "HAND_m", "flow_acc_cells", "terrain_edit_overlap",
    "boundary_distance_m", "matched_track_id", "artifact_class", "confidence", "verdict",
)

# 预注册规则：class → (门槛, 支持条件清单[(说明文字, 判定函数)])。
# 判定函数输入是 _normalize 后的指标 dict；支持条件只用于置信度计数，不是门槛。
_RULES: dict[str, tuple[Callable[[dict], bool], tuple[tuple[str, Callable[[dict], bool]], ...]]] = {
    "P05": (
        lambda v: v["sea_frac"] >= SEA_FRAC_MIN,
        (
            ("sea_frac ≥ 0.5", lambda v: v["sea_frac"] >= SEA_FRAC_MIN),
            ("sea_frac ≥ 0.9", lambda v: v["sea_frac"] >= 0.9),
            ("median_depth_m < 0.10", lambda v: v["median_depth_m"] < P06_MEDIAN_MAX_M),
            ("HAND_m ≤ 3", lambda v: v["HAND_m"] <= P03_HAND_MAX_M),
        ),
    ),
    "P02": (
        lambda v: v["dem_edit_frac"] >= DEM_EDIT_FRAC_MIN,
        (
            ("dem_edit_frac ≥ 0.5", lambda v: v["dem_edit_frac"] >= DEM_EDIT_FRAC_MIN),
            ("HAND_m ≤ 5", lambda v: v["HAND_m"] <= P01_HAND_MAX_M),
            ("flow_acc_cells ≥ 100", lambda v: v["flow_acc_cells"] >= P01_ACC_MIN_CELLS),
        ),
    ),
    "P07": (
        lambda v: v["touches_edge"],
        (
            ("touches_edge", lambda v: v["touches_edge"]),
            ("terrain_edit_overlap == 0", lambda v: v["terrain_edit_overlap"] == 0.0),
            ("flow_acc_cells < 100", lambda v: v["flow_acc_cells"] < P01_ACC_MIN_CELLS),
            ("distance_to_river_m > 1000",
             lambda v: v["distance_to_river_m"] > P03_DIST_RIVER_MAX_M),
        ),
    ),
    "P06": (
        lambda v: (v["median_depth_m"] < P06_MEDIAN_MAX_M
                   and v["persistence_frames"] < P06_PERSISTENCE_MAX),
        (
            ("median_depth_m < 0.10", lambda v: v["median_depth_m"] < P06_MEDIAN_MAX_M),
            ("persistence_frames < 3", lambda v: v["persistence_frames"] < P06_PERSISTENCE_MAX),
            ("terrain_edit_overlap == 0", lambda v: v["terrain_edit_overlap"] == 0.0),
            ("distance_to_river_m > 1000",
             lambda v: v["distance_to_river_m"] > P03_DIST_RIVER_MAX_M),
        ),
    ),
    "P03": (
        lambda v: (v["distance_to_river_m"] <= P03_DIST_RIVER_MAX_M
                   and v["HAND_m"] <= P03_HAND_MAX_M
                   and v["patch_area_km2"] < P03_ISOLATION_FRAC * v["largest_patch_area_km2"]),
        (
            ("distance_to_river_m ≤ 1000",
             lambda v: v["distance_to_river_m"] <= P03_DIST_RIVER_MAX_M),
            ("HAND_m ≤ 3", lambda v: v["HAND_m"] <= P03_HAND_MAX_M),
            ("patch_area_km2 < 0.5×最大斑块",
             lambda v: v["patch_area_km2"] < P03_ISOLATION_FRAC * v["largest_patch_area_km2"]),
        ),
    ),
    "P01": (
        lambda v: (v["median_depth_m"] >= P06_MEDIAN_MAX_M
                   and v["terrain_edit_overlap"] == 0.0
                   and v["HAND_m"] <= P01_HAND_MAX_M
                   and v["flow_acc_cells"] >= P01_ACC_MIN_CELLS),
        (
            ("median_depth_m ≥ 0.10", lambda v: v["median_depth_m"] >= P06_MEDIAN_MAX_M),
            ("terrain_edit_overlap == 0", lambda v: v["terrain_edit_overlap"] == 0.0),
            ("HAND_m ≤ 5", lambda v: v["HAND_m"] <= P01_HAND_MAX_M),
            ("flow_acc_cells ≥ 100", lambda v: v["flow_acc_cells"] >= P01_ACC_MIN_CELLS),
        ),
    ),
    "P04": (
        lambda v: v["median_depth_m"] >= P06_MEDIAN_MAX_M,
        (
            ("median_depth_m ≥ 0.10", lambda v: v["median_depth_m"] >= P06_MEDIAN_MAX_M),
            ("terrain_edit_overlap == 0", lambda v: v["terrain_edit_overlap"] == 0.0),
            ("distance_to_river_m > 1000",
             lambda v: v["distance_to_river_m"] > P03_DIST_RIVER_MAX_M),
            ("flow_acc_cells < 100", lambda v: v["flow_acc_cells"] < P01_ACC_MIN_CELLS),
        ),
    ),
    "P08": (
        lambda v: True,
        (
            ("terrain_edit_overlap == 0", lambda v: v["terrain_edit_overlap"] == 0.0),
            ("flow_acc_cells < 100", lambda v: v["flow_acc_cells"] < P01_ACC_MIN_CELLS),
            ("distance_to_river_m > 1000",
             lambda v: v["distance_to_river_m"] > P03_DIST_RIVER_MAX_M),
            ("distance_to_drainage_m > 500",
             lambda v: v["distance_to_drainage_m"] > P08_DIST_DRAIN_MIN_M),
        ),
    ),
}


def _normalize(indicators: dict[str, Any]) -> dict[str, Any]:
    """补齐分类所需指标的缺省值（缺失距离按无穷大、占比按 0 的保守口径）。"""
    return {
        "sea_frac": float(indicators.get("sea_frac", 0.0)),
        "dem_edit_frac": float(indicators.get("dem_edit_frac", 0.0)),
        "terrain_edit_overlap": float(indicators.get("terrain_edit_overlap", 0.0)),
        "touches_edge": bool(indicators.get("touches_edge", False)),
        "median_depth_m": float(indicators.get("median_depth_m", 0.0)),
        "persistence_frames": int(indicators.get("persistence_frames", 1)),
        "distance_to_river_m": float(indicators.get("distance_to_river_m", np.inf)),
        "distance_to_drainage_m": float(indicators.get("distance_to_drainage_m", np.inf)),
        "HAND_m": float(indicators.get("HAND_m", np.inf)),
        "flow_acc_cells": float(indicators.get("flow_acc_cells", 0.0)),
        "patch_area_km2": float(indicators.get("patch_area_km2", 0.0)),
        "largest_patch_area_km2": float(indicators.get("largest_patch_area_km2", np.inf)),
    }


def classify_patch_indicators(indicators: dict[str, Any]) -> tuple[str, str]:
    """按预注册优先级返回 (artifact_class, confidence)。纯函数，无 IO。

    ``persistence_frames`` 在轨迹未匹配时传 1（诚实降级），天然满足 P06 的
    「< 3 帧」候选条件。P08 为兜底门槛（恒真），循环必然返回。
    """
    v = _normalize(indicators)
    for cls in CLASS_ORDER:
        gate, support = _RULES[cls]
        if gate(v):
            count = sum(1 for _label, fn in support if fn(v))
            return cls, "high" if count >= 3 else "medium" if count == 2 else "low"
    raise AssertionError("unreachable：P08 门槛恒真")  # pragma: no cover


def verdict_for(cls: str) -> str:
    """展示质控处置：P02 先修复、P06 可过滤，其余禁止直接删除（计划 §4.2）。"""
    return CLASS_INFO[cls][2]


def bfs_distance_cells(source: np.ndarray) -> np.ndarray:
    """8 邻域 BFS 距离变换（格数；源像元为 0）。纯 numpy + deque，无 scipy。"""
    source = np.asarray(source, dtype=bool)
    if not source.any():
        raise ValueError("BFS 距离变换的源掩膜为空")
    height, width = source.shape
    dist = np.full(height * width, -1, dtype=np.int32)
    seeds = np.flatnonzero(source.ravel())
    dist[seeds] = 0
    queue: deque[int] = deque(seeds.tolist())
    while queue:
        cur = queue.popleft()
        d = dist[cur] + 1
        row, col = divmod(cur, width)
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                nr, nc = row + dr, col + dc
                if 0 <= nr < height and 0 <= nc < width:
                    nxt = nr * width + nc
                    if dist[nxt] == -1:
                        dist[nxt] = d
                        queue.append(nxt)
    return dist.reshape((height, width))


def river_mask_from_centerline(header: dict[str, float], shape: tuple[int, int]) -> np.ndarray:
    """把钦江合并中心线（WGS84 LineString）栅格化到基线网格。

    取 ``properties.name == "qinjiang_centerline_merged"`` 的最后一个要素
    （文件后续追加了 osm 版要素，不能按末位假设）；按 ``RIVER_DENSIFY_STEP_M``
    沿每段加密取点，落入的像元记 True（越界点丢弃）。
    """
    from pyproj import Transformer

    doc = json.loads(RIVER_CENTERLINE.read_text(encoding="utf-8"))
    feat = None
    for candidate in doc.get("features", []):
        props = candidate.get("properties") or {}
        if props.get("name") == "qinjiang_centerline_merged":
            feat = candidate
    if feat is None:
        raise GridError("river_centerline.geojson 缺少 qinjiang_centerline_merged 要素")
    coords = feat["geometry"]["coordinates"]
    tf = Transformer.from_crs(4326, 32649, always_xy=True)
    xs, ys = tf.transform([c[0] for c in coords], [c[1] for c in coords])
    xll, yll = float(header["xllcorner"]), float(header["yllcorner"])
    cell = float(header["cellsize"])
    nrows, ncols = shape
    ymax = yll + nrows * cell
    mask = np.zeros(shape, dtype=bool)
    for x0, y0, x1, y1 in zip(xs[:-1], ys[:-1], xs[1:], ys[1:]):
        steps = max(1, int(np.ceil(float(np.hypot(x1 - x0, y1 - y0)) / RIVER_DENSIFY_STEP_M)))
        for k in range(steps + 1):
            t = k / steps
            col = int((x0 + (x1 - x0) * t - xll) / cell)
            row = int((ymax - (y0 + (y1 - y0) * t)) / cell)
            if 0 <= row < nrows and 0 <= col < ncols:
                mask[row, col] = True
    if not mask.any():
        raise GridError("钦江中心线未落入基线网格内")
    return mask


def load_artifact_classes() -> np.ndarray:
    """读 dem_artifact_map.tif 波段 1（DEM 异常分类码 0–5）。"""
    import rasterio

    with rasterio.open(ARTIFACT_MAP) as src:
        return np.asarray(src.read(1))


def load_track_tables() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """读轨迹表与逐帧斑块表，过滤到 200 m 生产基线 run。"""
    run_id = RUN_BASELINE.name
    tracks: list[dict[str, Any]] = []
    with PATCH_TRACKS.open("r", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["run_id"] == run_id:
                tracks.append({
                    "track_id": int(row["track_id"]),
                    "frame_count": int(row["frame_count"]),
                    "longest_consecutive_run": int(row["longest_consecutive_run"]),
                    "first_frame": int(row["first_frame"]),
                    "last_frame": int(row["last_frame"]),
                })
    frames: list[dict[str, Any]] = []
    with PATCH_PERSIST.open("r", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["run_id"] == run_id:
                frames.append({
                    "seq": int(row["seq"]),
                    "centroid_row": float(row["centroid_row"]),
                    "centroid_col": float(row["centroid_col"]),
                })
    return tracks, frames


def match_tracks(
    patch_centroids: np.ndarray,
    frames: list[dict[str, Any]],
    tracks: list[dict[str, Any]],
) -> np.ndarray:
    """把每个 res.max 斑块匹配到空间近似的轨迹 id（int64，未匹配 -1）。

    桥梁：逐帧斑块质心与 patch 质心欧氏距离 ≤ 1 格；命中帧序号落在轨迹
    [first_frame, last_frame] 窗口内即候选；多候选取 frame_count 最大、
    并列取 track_id 最小。
    """
    matched = np.full(len(patch_centroids), -1, dtype=np.int64)
    if not frames or not tracks or len(patch_centroids) == 0:
        return matched
    seq_best: dict[int, dict[str, Any]] = {}
    for track in tracks:
        for seq in range(track["first_frame"], track["last_frame"] + 1):
            cur = seq_best.get(seq)
            key = (track["frame_count"], -track["track_id"])
            if cur is None or key > (cur["frame_count"], -cur["track_id"]):
                seq_best[seq] = track
    buckets: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for frame in frames:
        key = (int(np.floor(frame["centroid_row"])), int(np.floor(frame["centroid_col"])))
        buckets.setdefault(key, []).append(frame)
    for i, (prow, pcol) in enumerate(np.asarray(patch_centroids, dtype=float)):
        best: dict[str, Any] | None = None
        base = (int(np.floor(prow)), int(np.floor(pcol)))
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                for frame in buckets.get((base[0] + dr, base[1] + dc), ()):
                    d2 = (frame["centroid_row"] - prow) ** 2 + (frame["centroid_col"] - pcol) ** 2
                    if d2 > 1.0:
                        continue
                    cand = seq_best.get(frame["seq"])
                    if cand is not None and (
                        best is None
                        or (cand["frame_count"], -cand["track_id"])
                        > (best["frame_count"], -best["track_id"])
                    ):
                        best = cand
        if best is not None:
            matched[i] = best["track_id"]
    return matched


def write_class_map(path: Path, class_map: np.ndarray, grid: AsciiGrid) -> Path:
    """写 patch_class_map.tif（uint8 单波段 LZW，EPSG:32649，与基线同网格）。

    值：湿地斑块像元 = 类别码 1..8（P01..P08），其余 0；nodata 声明为 255。
    """
    import rasterio
    from rasterio.transform import from_origin

    height, width = class_map.shape
    res_m = grid.cell_size_m
    transform = from_origin(
        float(grid.header["xllcorner"]),
        float(grid.header["yllcorner"]) + height * res_m, res_m, res_m,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path, "w", driver="GTiff", height=height, width=width, count=1,
        dtype="uint8", crs="EPSG:32649", transform=transform, compress="lzw", nodata=255,
    ) as dst:
        dst.write(class_map, 1)
        dst.set_band_description(1, "patch_artifact_class")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="久隆水淹斑块物理归因（P01–P08）与 15 项指标（只读诊断）"
    )
    add_common_args(parser)
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir) if args.out_dir else (
        DEFAULT_OUT / args.run_tag if args.run_tag else DEFAULT_OUT
    )
    log_path = Path(args.log_json) if args.log_json else (
        DEFAULT_LOG_DIR
        / f"diagnose_jiulong_patch_attribution{'' if not args.run_tag else '.' + args.run_tag}.json"
    )
    audit = AuditLog("diagnose_jiulong_patch_attribution", run_tag=args.run_tag, dry_run=args.dry_run)
    audit.param(
        wet_threshold_m=WET_THRESHOLD_M, frame_hours_expected=FRAME_HOURS,
        sea_frac_min=SEA_FRAC_MIN, dem_edit_frac_min=DEM_EDIT_FRAC_MIN,
        p06_median_max_m=P06_MEDIAN_MAX_M, p06_persistence_max_frames=P06_PERSISTENCE_MAX,
        p03_dist_river_max_m=P03_DIST_RIVER_MAX_M, p03_hand_max_m=P03_HAND_MAX_M,
        p03_isolation_frac=P03_ISOLATION_FRAC, p01_hand_max_m=P01_HAND_MAX_M,
        p01_acc_min_cells=P01_ACC_MIN_CELLS, drainage_acc_min_cells=DRAINAGE_ACC_MIN_CELLS,
        p08_dist_drain_min_m=P08_DIST_DRAIN_MIN_M, river_densify_step_m=RIVER_DENSIFY_STEP_M,
        out_dir=str(out_dir), confidence_rule="支持条件命中数 ≥3 high / 2 medium / ≤1 low",
    )
    required = {
        "dem_asc": RUN_BASELINE / "dem.asc",
        "res_max": RUN_BASELINE / "results" / "res.max",
        "sea_mask": RUN_BASELINE / "sea_mask.npy",
        "dem_artifact_map": ARTIFACT_MAP,
        "patch_tracks": PATCH_TRACKS,
        "patch_persistence": PATCH_PERSIST,
        "river_centerline": RIVER_CENTERLINE,
    }
    for key, path in required.items():
        audit.add_input(key, path)

    def fail(msg: str, code: int) -> int:
        audit.error(msg)
        audit.write(log_path)
        print(f"[FAIL] {msg}", file=sys.stderr)
        return code

    missing = [str(p) for p in required.values() if not p.is_file()]
    if missing:
        return fail(f"缺少必需输入：{missing}", 2)
    existing = [] if args.dry_run else [
        name for name in OUTPUT_NAMES if (out_dir / name).exists()
    ]
    if not args.run_tag and existing:
        return fail(
            f"{out_dir} 已存在派生输出 {existing}，拒绝覆盖；如需并存请加 --run-tag <tag>", 3
        )
    try:
        guard_no_overwrite(out_dir)
    except Exception as exc:  # OverwriteRefused
        return fail(str(exc), 3)

    print("[1/5] 基线栅格与斑块统计 …", flush=True)
    grid = read_ascii_grid(required["dem_asc"])
    sea = load_sea_mask(RUN_BASELINE, grid.shape)
    if sea is None:
        return fail(f"{RUN_BASELINE} 缺 sea_mask.npy", 2)
    depth = read_ascii_grid(required["res_max"]).data
    if depth.shape != grid.shape:
        return fail(f"res.max 形状 {depth.shape} 与 DEM {grid.shape} 不符", 2)
    saveint = float(parse_run_par(RUN_BASELINE).get("saveint") or 0.0)
    if saveint <= 0:
        return fail(f"{RUN_BASELINE}/run.par 缺 saveint；帧时间口径无法确定", 2)
    saveint_h = saveint / 3600.0
    if abs(saveint_h - FRAME_HOURS) > 1e-9:
        audit.warn(f"run.par saveint={saveint}s ≠ 21600s；时间指标按实际帧距换算")
    wet = np.isfinite(depth) & (depth > WET_THRESHOLD_M) & ~sea
    labels, n_labels = connected_components(wet)
    patches = patch_stats(depth, labels, n_labels, cell_area_m2=grid.cell_area_m2)
    audit.step("patches", count=len(patches), wet_cells=int(wet.sum()))

    print("[2/5] 地形水文派生（填洼/D8/HAND）…", flush=True)
    filled = priority_flood(grid.data.astype(np.float64), eps=1e-5)
    acc = flow_accumulation(filled, d8_receiver(filled))
    river = river_mask_from_centerline(grid.header, grid.shape)
    drainage = (acc >= DRAINAGE_ACC_MIN_CELLS) | river
    hand_grid = hand(filled, drainage)
    audit.step("terrain_hydro", river_cells=int(river.sum()), drainage_cells=int(drainage.sum()),
               hand_max_m=round(float(hand_grid.max()), 3))

    print("[3/5] BFS 距离场（河/排水/边界）…", flush=True)
    cell = grid.cell_size_m
    edge = np.zeros(grid.shape, dtype=bool)
    edge[0, :] = edge[-1, :] = True
    edge[:, 0] = edge[:, -1] = True
    dist_river = bfs_distance_cells(river) * cell
    dist_drain = bfs_distance_cells(drainage) * cell
    dist_edge = bfs_distance_cells(edge) * cell

    print("[4/5] 轨迹空间近似匹配与逐斑块归因 …", flush=True)
    tracks, frames = load_track_tables()
    centroids = (
        np.array([[p.centroid_row, p.centroid_col] for p in patches], dtype=float)
        if patches else np.zeros((0, 2), dtype=float)
    )
    matched = match_tracks(centroids, frames, tracks)
    track_by_id = {t["track_id"]: t for t in tracks}
    n_matched = int((matched >= 0).sum())
    audit.step("track_match", tracks=len(tracks), frame_rows=len(frames), matched=n_matched)
    if patches and n_matched == 0:
        audit.warn("无任何斑块匹配到轨迹；时间指标全部按未匹配口径（persistence=1）诚实降级")

    artifact_classes = load_artifact_classes()
    if artifact_classes.shape != grid.shape:
        return fail(f"dem_artifact_map.tif 形状 {artifact_classes.shape} 与网格 {grid.shape} 不符", 2)
    classes_flat = artifact_classes.ravel()
    depth_flat = depth.ravel().astype(np.float64)
    hand_flat = hand_grid.ravel()
    acc_flat = acc.ravel()
    nrows, ncols = grid.shape
    largest_area = patches[0].area_km2 if patches else 0.0
    class_map = np.zeros(grid.shape, dtype=np.uint8)
    rows: list[dict[str, Any]] = []
    class_counts = {cls: 0 for cls in sorted(CLASS_INFO)}
    class_area = {cls: 0.0 for cls in sorted(CLASS_INFO)}

    for order, patch in enumerate(patches, start=1):
        cells = patch.cell_indices
        cls_cells = classes_flat[cells]
        cell_rows, cell_cols = cells // ncols, cells % ncols
        tid = int(matched[order - 1])
        track = track_by_id.get(tid)
        indicators: dict[str, Any] = {
            "patch_area_km2": patch.area_km2,
            "max_depth_m": patch.max_depth_m,
            "mean_depth_m": patch.mean_depth_m,
            "median_depth_m": float(np.median(depth_flat[cells])),
            "water_volume_m3": patch.volume_m3,
            "persistence_frames": track["frame_count"] if track else 1,
            "continuous_duration_h": track["longest_consecutive_run"] * saveint_h if track else 0.0,
            "appearance_time_h": track["first_frame"] * saveint_h if track else 0.0,
            "disappearance_time_h": track["last_frame"] * saveint_h if track else 0.0,
            "distance_to_river_m": float(dist_river.ravel()[cells].min()) * cell,
            "distance_to_drainage_m": float(dist_drain.ravel()[cells].min()) * cell,
            "HAND_m": float(np.median(hand_flat[cells])),
            "flow_acc_cells": int(acc_flat[cells].max()),
            "terrain_edit_overlap": float((cls_cells >= 1).mean()),
            "boundary_distance_m": float(dist_edge.ravel()[cells].min()) * cell,
            "matched_track_id": tid,
            "sea_frac": float((cls_cells == 1).mean()),
            "dem_edit_frac": float(np.isin(cls_cells, (2, 3, 5)).mean()),
            "touches_edge": bool(cell_rows.min() == 0 or cell_rows.max() == nrows - 1
                                 or cell_cols.min() == 0 or cell_cols.max() == ncols - 1),
            "largest_patch_area_km2": largest_area,
        }
        cls, conf = classify_patch_indicators(indicators)
        class_counts[cls] += 1
        class_area[cls] += patch.area_km2
        class_map.ravel()[cells] = CLASS_INFO[cls][1]
        rows.append({
            "patch_id": f"P{order:04d}",
            "patch_area_km2": round(patch.area_km2, 4),
            "max_depth_m": round(patch.max_depth_m, 4),
            "mean_depth_m": round(patch.mean_depth_m, 4),
            "median_depth_m": round(float(indicators["median_depth_m"]), 4),
            "water_volume_m3": round(patch.volume_m3, 1),
            "persistence_frames": indicators["persistence_frames"],
            "continuous_duration_h": round(float(indicators["continuous_duration_h"]), 2),
            "appearance_time_h": round(float(indicators["appearance_time_h"]), 2),
            "disappearance_time_h": round(float(indicators["disappearance_time_h"]), 2),
            "distance_to_river_m": round(float(indicators["distance_to_river_m"]), 1),
            "distance_to_drainage_m": round(float(indicators["distance_to_drainage_m"]), 1),
            "HAND_m": round(float(indicators["HAND_m"]), 3),
            "flow_acc_cells": indicators["flow_acc_cells"],
            "terrain_edit_overlap": round(float(indicators["terrain_edit_overlap"]), 3),
            "boundary_distance_m": round(float(indicators["boundary_distance_m"]), 1),
            "matched_track_id": tid,
            "artifact_class": cls,
            "confidence": conf,
            "verdict": verdict_for(cls),
        })
    audit.step("classify", **{f"n_{k}": v for k, v in class_counts.items()})

    if args.dry_run:
        counts_text = " ".join(f"{k}={class_counts[k]}" for k in sorted(class_counts))
        print(f"\n[DRY-RUN] 不写派生文件。斑块总数 {len(rows)}；分类计数 {counts_text}", flush=True)
        if args.log_json:
            audit.write(log_path)
        return 0

    print("[5/5] 写产物 …", flush=True)
    summary = {
        "generated_utc": utc_now(),
        "script": "scripts/stage6/diagnose_jiulong_patch_attribution.py",
        "run_tag": args.run_tag,
        "run_id": RUN_BASELINE.name,
        "phase": "久隆斑块物理归因（计划 §4.1/§4.2 任务 1）",
        "mode": "ADR-0004 公开数据降级、情景级、非业务预警",
        "evidence_level": "E1（基于冻结的诊断快照与 run 产物派生）",
        "no_double_counting": "派生层不含 res.mass，不进入 g15 质量门",
        "read_only_declaration": "outputs/jiulong/** 只读；未调用 lisflood.exe；未修改 models/",
        "patch_count": len(rows),
        "wet_cells": int(wet.sum()),
        "wet_area_km2": round(int(wet.sum()) * grid.cell_area_m2 / 1e6, 4),
        "largest_patch_area_km2": round(largest_area, 4),
        "matched_track_count": n_matched,
        "class_counts": class_counts,
        "class_area_km2": {k: round(v, 4) for k, v in class_area.items()},
        "class_legend": {
            str(info[1]): {"class": key, "name": info[0], "verdict": info[2]}
            for key, info in CLASS_INFO.items()
        },
        "class_map_note": "0=非湿地；类别码 1..8=P01..P08；nodata 声明 255（数据中不出现）",
        "class_support_conditions": {
            key: [label for label, _fn in _RULES[key][1]] for key in sorted(CLASS_INFO)
        },
        "thresholds": {
            "wet_threshold_m": WET_THRESHOLD_M, "saveint_s_actual": saveint,
            "sea_frac_min": SEA_FRAC_MIN, "dem_edit_frac_min": DEM_EDIT_FRAC_MIN,
            "p06_median_max_m": P06_MEDIAN_MAX_M,
            "p06_persistence_max_frames": P06_PERSISTENCE_MAX,
            "p03_dist_river_max_m": P03_DIST_RIVER_MAX_M, "p03_hand_max_m": P03_HAND_MAX_M,
            "p03_isolation_frac": P03_ISOLATION_FRAC, "p01_hand_max_m": P01_HAND_MAX_M,
            "p01_acc_min_cells": P01_ACC_MIN_CELLS,
            "drainage_acc_min_cells": DRAINAGE_ACC_MIN_CELLS,
            "p08_dist_drain_min_m": P08_DIST_DRAIN_MIN_M,
            "river_densify_step_m": RIVER_DENSIFY_STEP_M,
        },
        "inputs_sha256": {
            name: sha256_file(path) for name, path in (
                ("dem.asc", required["dem_asc"]),
                ("results/res.max", required["res_max"]),
                ("dem_artifact_map.tif", ARTIFACT_MAP),
                ("patch_tracks.csv", PATCH_TRACKS),
                ("river_centerline.geojson", RIVER_CENTERLINE),
            )
        },
        "notes": [
            "时间指标来自 patch_tracks.csv 的空间近似匹配（逐帧斑块质心 ≤1 格作桥梁）；"
            "未匹配记 -1、persistence=1，是诚实降级不是观测值",
            "distance_to_drainage 为 drainage_mask（汇流累积 ≥1250 格或河道像元）的 BFS "
            "距离派生值；S1 派生排水数据不可得",
            "HAND/汇流累积为 Priority-Flood(+ε) 填洼后的派生诊断量，只用于归因",
            "分类规则与置信度条件为预注册确定性规则；质量门 PASS 不等于预测正确",
        ],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "patch_attribution.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)
    tif_path = write_class_map(out_dir / "patch_class_map.tif", class_map, grid)
    write_json(out_dir / "class_summary.json", summary)
    audit.step("write_products", out_dir=str(out_dir), rows=len(rows))
    audit.write(log_path)
    for path in (csv_path, tif_path, out_dir / "class_summary.json", log_path):
        print(f"-> {path}")
    counts_text = " ".join(f"{k}={class_counts[k]}" for k in sorted(class_counts))
    print(f"斑块总数 {len(rows)}；分类计数 {counts_text}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
