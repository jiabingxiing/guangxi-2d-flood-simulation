"""Phase 0 / T1：久隆全帧斑块时空统计（只读诊断）。

对应计划 §5 Phase 0 任务 3「统计全部帧的斑块面积、深度、体积、持续性和连通关系」，
以及 §3.3 / §3.4 的口径要求：

- 连通域用 8 邻域（`scripts/common/patch_analysis.py`，纯 numpy，无 scipy 依赖）。
- 面积一律用 `cell_area_m2`（从栅格头 `cellsize` 平方得出），禁止写死 0.04 km²。
- 斑块分类只做**诊断**，不冻结任何展示阈值：物理面积阈值以 km² 表达，
  像元数由 `cells_for_area` 按当前网格换算（计划 §3.3 参数冻结规则）。
- 结论标证据等级；未测项标 E4，不得给数值结论。

本脚本**只读** `outputs/jiulong/**`；不写入、不移动、不删除其中任何文件；不调用 `lisflood.exe`。

用法（先设 `$env:PYTHONUTF8="1"`）::

    uv --directory 2d_hydro_project run python scripts/stage6/analyze_patch_persistence.py --dry-run
    uv --directory 2d_hydro_project run python scripts/stage6/analyze_patch_persistence.py --run-tag phase0-v1

产出（默认目录 `diagnostics/jiulong/`）：
    patch_persistence.csv    逐帧逐斑块统计（每行 = 一帧的一个斑块）
    patch_tracks.csv         跨帧轨迹（按像元重叠关联）
    patch_frame_summary.csv  逐帧汇总（斑块数、湿面积、体积、薄层像元数）
    patch_summary.json       口径、参数、阈值换算、分类计数、证据等级
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any

import numpy as np

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import (  # noqa: E402
    AuditLog,
    GridError,
    add_common_args,
    guard_no_overwrite,
    load_sea_mask,
    parse_run_par,
    read_ascii_grid,
    run_frames,
    utc_now,
    write_json,
)
from scripts.common.patch_analysis import (  # noqa: E402
    PersistenceTracker,
    classify_patch,
    summarize_frame,
)

JIU = PROJ / "outputs" / "jiulong" / "2024-09-yagi"
DEFAULT_OUT = PROJ / "diagnostics" / "jiulong"
DEFAULT_LOG_DIR = PROJ / "logs" / "diagnostics"

WET_THRESHOLD_M = 0.05
# 诊断用物理面积阈值（km²）。这是**诊断分档**，不是冻结的展示阈值（计划 §1.4 / §5 Phase 0 门禁）。
AREA_BINS_KM2 = (0.04, 0.16, 0.36, 1.0, 4.0, 10.0)
# 诊断分档参数（仅用于给斑块打标签，不产生任何过滤输出）
DIAG_AREA_THRESHOLD_KM2 = 0.16
DIAG_PERSISTENCE_FRAMES = 3
DIAG_SHALLOW_THRESHOLD_M = 0.10
DIAG_EXEMPT_AREA_KM2 = 1.0
# 细薄水膜上限：输出格式量化到 0.001 m（output.cpp:686 `%.3f`），故 (0, 0.005] 记薄层
THIN_FILM_MAX_M = 0.005

DEFAULT_RUNS = {
    "scen_free_200m_sv6h": "production_baseline_200m",
    "scen_free_100m_dt2p5_sv6h_realdem": "sensitivity_100m",
    "scen_free_200m_sv6h_recession": "production_recession_200m",
}

PATCH_COLUMNS = (
    "run_id", "role", "cellsize_m", "cell_area_m2", "seq", "t_h", "patch_label",
    "n_cells", "area_km2", "max_depth_m", "mean_depth_m", "p95_depth_m", "volume_m3",
    "row_min", "row_max", "col_min", "col_max", "bbox_area_km2", "rectangularity",
    "centroid_row", "centroid_col", "depth_band", "area_band",
)
TRACK_COLUMNS = (
    "run_id", "role", "cellsize_m", "track_id", "frame_count", "longest_consecutive_run",
    "first_frame", "last_frame", "max_area_km2", "mean_area_km2", "max_depth_m",
    "volume_peak_m3", "diag_verdict", "diag_reason_code",
)
FRAME_COLUMNS = (
    "run_id", "role", "cellsize_m", "cell_area_m2", "seq", "t_h", "n_patches",
    "wet_cells", "wet_area_km2", "wet_volume_m3", "max_depth_m", "p50_depth_m",
    "p90_depth_m", "p99_depth_m", "thin_film_cells", "single_cell_patches",
    "single_cell_area_km2", "largest_patch_area_km2", "patches_ge_diag_area_threshold",
)


def depth_band(max_depth_m: float) -> str:
    """按最大水深给诊断分档（不用于过滤）。"""
    if max_depth_m <= THIN_FILM_MAX_M:
        return f"le_{THIN_FILM_MAX_M:g}m"
    if max_depth_m <= 0.05:
        return "0.005_0.05m"
    if max_depth_m <= 0.3:
        return "0.05_0.3m"
    if max_depth_m <= 1.0:
        return "0.3_1.0m"
    return "gt_1.0m"


def area_band(area_km2: float) -> str:
    """按物理面积给诊断分档；边界由 AREA_BINS_KM2 定义。"""
    previous = 0.0
    for edge in AREA_BINS_KM2:
        if area_km2 < edge:
            return f"{previous:g}_{edge:g}km2"
        previous = edge
    return f"ge_{AREA_BINS_KM2[-1]:g}km2"


def analyze_run(
    run_dir: Path, role: str, *, keep_patches: bool = True
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """对单个 run 的全部帧做斑块统计与轨迹追踪。"""
    dem_grid = read_ascii_grid(run_dir / "dem.asc")
    shape = dem_grid.shape
    cell_area = dem_grid.cell_area_m2
    res_m = dem_grid.cell_size_m
    sea = load_sea_mask(run_dir, shape)
    if sea is None:
        raise GridError(f"{run_dir} 缺 sea_mask.npy；不参与斑块诊断")
    par = parse_run_par(run_dir)
    saveint = float(par.get("saveint") or 0.0)
    if saveint <= 0:
        raise GridError(f"{run_dir}/run.par 缺 saveint；帧时间口径无法确定")

    patch_rows: list[dict[str, Any]] = []
    frame_rows: list[dict[str, Any]] = []
    tracker = PersistenceTracker()
    depth_threshold_cells = dem_grid.cells_for_area(DIAG_AREA_THRESHOLD_KM2)

    for seq, path in run_frames(run_dir / "results"):
        frame = read_ascii_grid(path)
        if frame.shape != shape:
            raise GridError(f"{path} 形状 {frame.shape} 与 DEM {shape} 不符")
        depth = frame.data
        summary = summarize_frame(
            depth, seq,
            wet_threshold_m=WET_THRESHOLD_M,
            cell_area_m2=cell_area,
            sea_mask=sea,
            keep_patches=keep_patches,
        )
        t_h = round(seq * saveint / 3600.0, 3)
        finite = np.isfinite(depth)
        thin = int((finite & (depth > 0) & (depth <= THIN_FILM_MAX_M) & ~sea).sum())
        single = [p for p in summary.patches if p.n_cells == 1]
        frame_rows.append({
            "run_id": run_dir.name,
            "role": role,
            "cellsize_m": res_m,
            "cell_area_m2": cell_area,
            "seq": seq,
            "t_h": t_h,
            "n_patches": summary.n_patches,
            "wet_cells": summary.wet_cells,
            "wet_area_km2": round(summary.wet_area_km2, 4),
            "wet_volume_m3": round(summary.wet_volume_m3, 1),
            "max_depth_m": round(summary.max_depth_m, 4),
            "p50_depth_m": summary.depth_stats.get("p50", 0.0),
            "p90_depth_m": summary.depth_stats.get("p90", 0.0),
            "p99_depth_m": summary.depth_stats.get("p99", 0.0),
            "thin_film_cells": thin,
            "single_cell_patches": len(single),
            "single_cell_area_km2": round(sum(p.area_km2 for p in single), 4),
            "largest_patch_area_km2": round(summary.patches[0].area_km2, 4) if summary.patches else 0.0,
            "patches_ge_diag_area_threshold": sum(
                1 for p in summary.patches if p.area_km2 >= DIAG_AREA_THRESHOLD_KM2
            ),
        })
        for patch in summary.patches:
            bbox_cells = (patch.row_max - patch.row_min + 1) * (patch.col_max - patch.col_min + 1)
            patch_rows.append({
                "run_id": run_dir.name,
                "role": role,
                "cellsize_m": res_m,
                "cell_area_m2": cell_area,
                "seq": seq,
                "t_h": t_h,
                "patch_label": patch.label,
                "n_cells": patch.n_cells,
                "area_km2": round(patch.area_km2, 6),
                "max_depth_m": round(patch.max_depth_m, 4),
                "mean_depth_m": round(patch.mean_depth_m, 4),
                "p95_depth_m": round(patch.p95_depth_m, 4),
                "volume_m3": round(patch.volume_m3, 1),
                "row_min": patch.row_min,
                "row_max": patch.row_max,
                "col_min": patch.col_min,
                "col_max": patch.col_max,
                "bbox_area_km2": round(bbox_cells * cell_area / 1e6, 6),
                "rectangularity": round(patch.n_cells / bbox_cells, 4),
                "centroid_row": round(patch.centroid_row, 2),
                "centroid_col": round(patch.centroid_col, 2),
                "depth_band": depth_band(patch.max_depth_m),
                "area_band": area_band(patch.area_km2),
            })
        if keep_patches:
            tracker.update(summary, width=shape[1])

    track_rows: list[dict[str, Any]] = []
    if keep_patches:
        for track in tracker.summaries():
            verdict, reason = classify_patch(
                track,
                area_threshold_km2=DIAG_AREA_THRESHOLD_KM2,
                persistence_frames=DIAG_PERSISTENCE_FRAMES,
                shallow_threshold_m=DIAG_SHALLOW_THRESHOLD_M,
                exemption_area_km2=DIAG_EXEMPT_AREA_KM2,
            )
            track_rows.append({
                "run_id": run_dir.name,
                "role": role,
                "cellsize_m": res_m,
                "track_id": track["track_id"],
                "frame_count": track["frame_count"],
                "longest_consecutive_run": track["longest_consecutive_run"],
                "first_frame": track["first_frame"],
                "last_frame": track["last_frame"],
                "max_area_km2": track["max_area_km2"],
                "mean_area_km2": track["mean_area_km2"],
                "max_depth_m": track["max_depth_m"],
                "volume_peak_m3": track["volume_peak_m3"],
                "diag_verdict": verdict,
                "diag_reason_code": reason,
            })

    meta = {
        "run_id": run_dir.name,
        "role": role,
        "grid": dem_grid.to_meta(),
        "cells": int(dem_grid.data.size),
        "domain_area_km2": round(int(dem_grid.data.size) * cell_area / 1e6, 4),
        "sea_cells": int(sea.sum()),
        "saveint_s": saveint,
        "sim_time_s": float(par.get("sim_time") or 0.0),
        "frame_count": len(run_frames(run_dir / "results")),
        "diag_area_threshold_km2": DIAG_AREA_THRESHOLD_KM2,
        "diag_area_threshold_cells": depth_threshold_cells,
        "diag_persistence_frames": DIAG_PERSISTENCE_FRAMES,
        "diag_shallow_threshold_m": DIAG_SHALLOW_THRESHOLD_M,
        "diag_exempt_area_km2": DIAG_EXEMPT_AREA_KM2,
        "track_count": len(track_rows),
        "merge_events": len(tracker.merge_events),
    }
    return patch_rows, track_rows, frame_rows, meta


def write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def aggregate(meta_by_run: dict[str, dict[str, Any]], frame_rows: list[dict[str, Any]],
              track_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """跨 run 汇总：分类计数、面积谱、持续性分布（全部为诊断指标）。"""
    out: dict[str, Any] = {}
    for run_id, meta in meta_by_run.items():
        frames = [r for r in frame_rows if r["run_id"] == run_id]
        tracks = [r for r in track_rows if r["run_id"] == run_id]
        wet_frames = [r for r in frames if r["wet_cells"] > 0]
        peak = max(wet_frames, key=lambda r: r["wet_area_km2"]) if wet_frames else None
        verdicts: dict[str, int] = {}
        reasons: dict[str, int] = {}
        for track in tracks:
            verdicts[track["diag_verdict"]] = verdicts.get(track["diag_verdict"], 0) + 1
            reasons[track["diag_reason_code"]] = reasons.get(track["diag_reason_code"], 0) + 1
        persistent = [t for t in tracks if t["frame_count"] >= 2]
        out[run_id] = {
            "role": meta["role"],
            "cellsize_m": meta["grid"]["cellsize_m"],
            "cell_area_m2": meta["grid"]["cell_area_m2"],
            "frame_count": meta["frame_count"],
            "wet_frame_count": len(wet_frames),
            "peak_frame": {
                "seq": peak["seq"], "t_h": peak["t_h"],
                "wet_area_km2": peak["wet_area_km2"],
                "n_patches": peak["n_patches"],
            } if peak else None,
            "last_frame": {
                "seq": frames[-1]["seq"], "t_h": frames[-1]["t_h"],
                "wet_area_km2": frames[-1]["wet_area_km2"],
                "n_patches": frames[-1]["n_patches"],
            } if frames else None,
            "diag_verdict_counts": verdicts,
            "diag_reason_counts": reasons,
            "track_count": len(tracks),
            "tracks_in_2plus_frames": len(persistent),
            "tracks_spanning_all_wet_frames": sum(
                1 for t in tracks if t["frame_count"] >= len(wet_frames) and wet_frames
            ),
            "area_spectrum_peak_frame": _area_spectrum(peak) if peak else None,
        }
    return out


def _area_spectrum(frame_row: dict[str, Any]) -> dict[str, Any]:
    """峰值帧的面积谱（物理面积阈值下的斑块数与面积）。"""
    return {
        "note": "面积谱按物理面积阈值（km²）给出；像元数随网格变化，不做跨分辨率直接比较",
        "diag_area_threshold_km2": DIAG_AREA_THRESHOLD_KM2,
        "patches_ge_diag_area_threshold": frame_row["patches_ge_diag_area_threshold"],
        "single_cell_patches": frame_row["single_cell_patches"],
        "single_cell_area_km2": frame_row["single_cell_area_km2"],
        "largest_patch_area_km2": frame_row["largest_patch_area_km2"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Phase 0：久隆全帧斑块时空统计（只读诊断，不产生过滤输出）"
    )
    add_common_args(parser)
    parser.add_argument(
        "--run-id", action="append", default=None,
        help="只分析指定 run 目录名（可重复）；缺省分析生产基线 + 100 m 敏感性 + 退水情景",
    )
    parser.add_argument(
        "--no-tracks", action="store_true",
        help="跳过跨帧轨迹追踪（更快，但 patch_tracks.csv 不完整）",
    )
    args = parser.parse_args(argv)

    run_tag = (args.run_tag or "").strip()
    out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_OUT
    if run_tag:
        out_dir = out_dir / run_tag
    log_path = Path(args.log_json) if args.log_json else (
        DEFAULT_LOG_DIR / f"analyze_patch_persistence{'_' + run_tag if run_tag else ''}.json"
    )
    audit = AuditLog("analyze_patch_persistence", run_tag=run_tag or None, dry_run=args.dry_run)
    audit.param(
        wet_threshold_m=WET_THRESHOLD_M,
        area_bins_km2=list(AREA_BINS_KM2),
        diag_area_threshold_km2=DIAG_AREA_THRESHOLD_KM2,
        diag_persistence_frames=DIAG_PERSISTENCE_FRAMES,
        diag_shallow_threshold_m=DIAG_SHALLOW_THRESHOLD_M,
        diag_exempt_area_km2=DIAG_EXEMPT_AREA_KM2,
        thin_film_max_m=THIN_FILM_MAX_M,
        out_dir=str(out_dir),
        note=(
            "全部面积阈值以 km² 表达；像元数由栅格头 cellsize 换算（cells_for_area）；"
            "本脚本不产生任何过滤输出，阈值未冻结"
        ),
    )

    selected = args.run_id or list(DEFAULT_RUNS)
    run_dirs: list[tuple[Path, str]] = []
    for name in selected:
        run_dir = JIU / name
        if not run_dir.is_dir():
            audit.error(f"缺少 run 目录：{run_dir}")
            audit.write(log_path)
            print(f"[FAIL] 缺少 run 目录：{run_dir}", file=sys.stderr)
            return 4
        run_dirs.append((run_dir, DEFAULT_RUNS.get(name, "custom")))

    try:
        guard_no_overwrite(out_dir)
    except Exception as exc:  # OverwriteRefused
        audit.error(str(exc))
        audit.write(log_path)
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 3

    all_patch: list[dict[str, Any]] = []
    all_track: list[dict[str, Any]] = []
    all_frame: list[dict[str, Any]] = []
    meta_by_run: dict[str, dict[str, Any]] = {}
    for run_dir, role in run_dirs:
        print(f"[分析] {run_dir.name} …", flush=True)
        patches, tracks, frames, meta = analyze_run(run_dir, role, keep_patches=not args.no_tracks)
        all_patch.extend(patches)
        all_track.extend(tracks)
        all_frame.extend(frames)
        meta_by_run[run_dir.name] = meta
        peak = max((f["wet_area_km2"] for f in frames), default=0.0)
        print(
            f"   {meta['frame_count']} 帧；单元面积 {meta['grid']['cell_area_m2']} m²；"
            f"斑块行 {len(patches)}；轨迹 {meta['track_count']}；"
            f"峰值湿面积 {peak} km²；诊断面积阈值 {DIAG_AREA_THRESHOLD_KM2} km² = "
            f"{meta['diag_area_threshold_cells']} 格", flush=True,
        )
        audit.step("analyze_run", run_id=run_dir.name, frames=meta["frame_count"],
                   patch_rows=len(patches), track_count=meta["track_count"],
                   cell_area_m2=meta["grid"]["cell_area_m2"],
                   diag_area_threshold_cells=meta["diag_area_threshold_cells"])

    summary = {
        "generated_utc": utc_now(),
        "script": "scripts/stage6/analyze_patch_persistence.py",
        "run_tag": run_tag or None,
        "phase": "Phase 0 / T1（斑块时空统计）",
        "mode": "ADR-0004 公开数据降级、情景级、非业务预警",
        "read_only_declaration": (
            "本文件全部数值由脚本从 outputs/jiulong/** 只读复算；"
            "未写入、移动或删除该目录下任何文件；未调用 lisflood.exe"
        ),
        "threshold_policy": {
            "frozen": False,
            "reason": (
                "方案 A/B 未确认前不冻结展示阈值（计划 §5 Phase 0 门禁）；"
                "本文件阈值仅用于诊断分档"
            ),
            "area_expression": "全部以 km² 表达；像元数由 cells_for_area 按当前网格换算",
            "cell_area_source": "栅格头 cellsize 平方",
        },
        "evidence_levels": {
            "frame_patch_statistics": "E1（脚本从 raw run 逐帧复算）",
            "patch_tracks": "E1（8 邻域连通 + 像元重叠关联）",
            "diagnostic_verdict": "E1 规则判定，但阈值未冻结，不得作为发布依据",
            "physical_cause": "E3（需 Phase 2 四层 DEM 与 Phase 3-4 试验确认）",
            "independent_accuracy_metrics": "E4 未测（无连续观测；S1 仅灾前/退水后两景）",
        },
        "parameters": {
            "wet_threshold_m": WET_THRESHOLD_M,
            "thin_film_max_m": THIN_FILM_MAX_M,
            "output_precision_m": 0.001,
            "output_precision_note": (
                "LISFLOOD-FP 以 %.3f 写 .wd（output.cpp:686），水深量化到 0.001 m；"
                "任何小于 0.001 m 的深度差不可分辨"
            ),
            "area_bins_km2": list(AREA_BINS_KM2),
            "diag_area_threshold_km2": DIAG_AREA_THRESHOLD_KM2,
            "diag_persistence_frames": DIAG_PERSISTENCE_FRAMES,
            "diag_shallow_threshold_m": DIAG_SHALLOW_THRESHOLD_M,
            "diag_exempt_area_km2": DIAG_EXEMPT_AREA_KM2,
            "connectivity": 8,
        },
        "runs": meta_by_run,
        "aggregate": aggregate(meta_by_run, all_frame, all_track),
        "outputs": {
            "patch_persistence_csv": "patch_persistence.csv（逐帧逐斑块）",
            "patch_tracks_csv": "patch_tracks.csv（跨帧轨迹）",
            "patch_frame_summary_csv": "patch_frame_summary.csv（逐帧汇总）",
        },
    }

    if args.dry_run:
        print("\n[DRY-RUN] 不写派生文件。摘要：", flush=True)
        for run_id, agg in summary["aggregate"].items():
            print(f"  {run_id}: 单元面积 {agg['cell_area_m2']} m²，{agg['frame_count']} 帧，"
                  f"峰值湿面积 {agg['peak_frame']['wet_area_km2'] if agg['peak_frame'] else None} km²，"
                  f"轨迹 {agg['track_count']}（≥2 帧 {agg['tracks_in_2plus_frames']}），"
                  f"诊断判定 {agg['diag_verdict_counts']}", flush=True)
        print(f"  patch_persistence.csv 行数 {len(all_patch)}；patch_tracks.csv 行数 {len(all_track)}；"
              f"patch_frame_summary.csv 行数 {len(all_frame)}", flush=True)
        if args.log_json:
            audit.write(log_path)
        return 0

    paths = [
        write_csv(out_dir / "patch_persistence.csv", PATCH_COLUMNS, all_patch),
        write_csv(out_dir / "patch_tracks.csv", TRACK_COLUMNS, all_track),
        write_csv(out_dir / "patch_frame_summary.csv", FRAME_COLUMNS, all_frame),
    ]
    summary["artifacts"] = [str(p) for p in paths] + [str(out_dir / "patch_summary.json")]
    write_json(out_dir / "patch_summary.json", summary)
    audit.step("write_products", out_dir=str(out_dir),
               patch_rows=len(all_patch), track_rows=len(all_track), frame_rows=len(all_frame))
    audit.write(log_path)
    for path in paths:
        print(f"-> {path}")
    print(f"-> {out_dir / 'patch_summary.json'}")
    print(f"-> {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
