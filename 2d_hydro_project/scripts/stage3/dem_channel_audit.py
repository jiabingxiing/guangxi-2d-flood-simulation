"""刻蚀审计：量化模型输入 DEM 相对未刻蚀地形的人为修改。

只读。不修改 run 目录、不重跑模型。

用法：
    uv run python scripts/stage3/dem_channel_audit.py --all-runs
    uv run python scripts/stage3/dem_channel_audit.py --run outputs/ningming/2026-08-zitan/<run_id>

指标（终稿降级模式登记要求）：
    1. 刻蚀像元数与域内占比；
    2. 距 8/6 常水像元距离 p50/p95/max，以及超 250 m 占比；
    3. 刻蚀处原始高程 p50，以及超 150 m 占比；
    4. 最大下切深度；
    5. 逐列刻蚀值唯一性占比（等宽竖条成因）。

门：距常水 >250 m 占比 >20%，或最大下切 >30 m → FAIL。
"""
from __future__ import annotations

import argparse
import hashlib
import heapq
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import Affine
from rasterio.warp import reproject

PROJ = Path(__file__).resolve().parents[2]
WATER_0806 = PROJ / "data" / "interim" / "ningming" / "water_0806.tif"
OUT = PROJ / "data" / "catalog" / "dem_channel_audit.json"
EPSG = 32648
EXCLUDE_MARKERS = ("_v1invalid", "_interrupted")

GATE = {"far_fraction_max": 0.20, "max_downcut_m": 30.0}
NEAR_WATER_M = 250.0
HIGH_ORIGINAL_M = 150.0


def _read_asc(path: Path) -> tuple[np.ndarray, dict[str, float]]:
    with open(path) as f:
        hdr = [next(f) for _ in range(6)]
    header = {ln.split()[0].lower(): float(ln.split()[1]) for ln in hdr}
    return np.loadtxt(path, skiprows=6), header


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(chunk):
            h.update(block)
    return h.hexdigest()


def _water_on_grid(shape: tuple[int, int], transform: Affine) -> np.ndarray | None:
    """8/6 常水水体聚合到 run 网格：覆盖率 >0.2 记为水体。"""
    if not WATER_0806.is_file():
        return None
    frac = np.zeros(shape, dtype="float32")
    with rasterio.open(WATER_0806) as ds:
        reproject(
            (ds.read(1) == 1).astype("float32"), frac,
            src_transform=ds.transform, src_crs=ds.crs,
            dst_transform=transform, dst_crs=f"EPSG:{EPSG}",
            resampling=Resampling.average,
        )
    return frac > 0.2


def _distance_to_water_m(cells: np.ndarray, wet: np.ndarray, cell_m: float) -> np.ndarray:
    """多源 Dijkstra 求每个刻蚀像元到最近常水像元的近似欧氏距离（米）。"""
    inf = float("inf")
    dist = np.where(wet, 0.0, inf)
    if not wet.any():
        return np.full(cells.shape, np.nan)
    heap = [(0.0, int(r), int(c)) for r, c in zip(*np.nonzero(wet))]
    heapq.heapify(heap)
    diag = 2 ** 0.5
    rows, cols = dist.shape
    while heap:
        d, r, c = heapq.heappop(heap)
        if d > dist[r, c]:
            continue
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                nr, nc = r + dr, c + dc
                if not (0 <= nr < rows and 0 <= nc < cols):
                    continue
                step = diag if (dr and dc) else 1.0
                nd = d + step
                if nd < dist[nr, nc] - 1e-12:
                    dist[nr, nc] = nd
                    heapq.heappush(heap, (nd, nr, nc))
    return dist[cells] * cell_m


def _column_uniqueness(edit: np.ndarray, edited: np.ndarray) -> dict:
    """逐列刻蚀值是否完全相同（真河道不应出现整列常值）。"""
    cols = [c for c in range(edit.shape[1]) if edit[:, c].any()]
    if not cols:
        return {"columns_with_edit": 0, "single_value_columns": 0, "single_value_fraction": None}
    single = 0
    for c in cols:
        values = np.round(edited[edit[:, c], c], 2)
        if np.unique(values).size == 1:
            single += 1
    return {
        "columns_with_edit": len(cols),
        "single_value_columns": single,
        "single_value_fraction": round(single / len(cols), 4),
    }


def audit_run(run_dir: Path) -> dict:
    rel = run_dir.relative_to(PROJ).as_posix()
    dem_path = run_dir / "dem.asc"
    diff_path = run_dir / "dem_pre_carve.diff.txt"
    rec: dict = {"run": rel, "dem_asc": str(dem_path), "status": "no_edit", "gate": None}

    if not dem_path.is_file():
        rec.update(status="missing_dem", reason="dem.asc 不存在")
        return rec
    rec["dem_sha256"] = _sha256(dem_path)
    if not diff_path.is_file():
        rec["reason"] = "无 dem_pre_carve.diff.txt：该 run 未记录地形修改"
        return rec

    dem, header = _read_asc(dem_path)
    diff = np.loadtxt(diff_path, skiprows=6)
    if diff.shape != dem.shape:
        rec.update(status="invalid", reason=f"diff 形状 {diff.shape} 与 dem {dem.shape} 不一致")
        return rec

    edit = np.abs(diff) > 1e-6
    rec["status"] = "edited"
    rec["requested_edit_cells"] = int(edit.sum())
    if not edit.any():
        rec["reason"] = "diff 全零：刻蚀代码被执行但未改动任何像元"
        return rec

    cell_m = float(header["cellsize"])
    original = dem - diff
    values = original[edit]

    wet = _water_on_grid(dem.shape, Affine(
        cell_m, 0, header["xllcorner"], 0, -cell_m,
        header["yllcorner"] + dem.shape[0] * cell_m,
    ))
    distance = _distance_to_water_m(edit, wet, cell_m) if wet is not None else np.full(values.shape, np.nan)
    far_fraction = float(np.mean(distance > NEAR_WATER_M)) if np.isfinite(distance).any() else None

    max_downcut = float(-diff[edit].min())  # 正值表示最大下切
    high_fraction = float(np.mean(values > HIGH_ORIGINAL_M))

    rec.update({
        "channel_mode": "watermask",
        "edited_cells": int(edit.sum()),
        "edited_fraction": round(float(edit.mean()), 6),
        "edited_area_km2": round(float(edit.sum()) * cell_m * cell_m / 1e6, 3),
        "water_cells_on_grid": int(wet.sum()) if wet is not None else None,
        "distance_to_water_m": {
            "p50": round(float(np.percentile(distance, 50)), 1) if np.isfinite(distance).any() else None,
            "p95": round(float(np.percentile(distance, 95)), 1) if np.isfinite(distance).any() else None,
            "max": round(float(np.nanmax(distance)), 1) if np.isfinite(distance).any() else None,
            f"fraction_gt_{int(NEAR_WATER_M)}m": round(far_fraction, 4) if far_fraction is not None else None,
        },
        "original_elevation_m": {
            "min": round(float(values.min()), 2),
            "p50": round(float(np.median(values)), 2),
            "max": round(float(values.max()), 2),
            f"fraction_gt_{int(HIGH_ORIGINAL_M)}m": round(high_fraction, 4),
        },
        "edited_elevation_m": {
            "min": round(float(dem[edit].min()), 2),
            "p50": round(float(np.median(dem[edit])), 2),
            "max": round(float(dem[edit].max()), 2),
        },
        "max_downcut_m": round(max_downcut, 2),
        "columns": _column_uniqueness(edit, dem),
    })

    reasons = []
    if far_fraction is not None and far_fraction > GATE["far_fraction_max"]:
        reasons.append(f"距常水 >{NEAR_WATER_M:.0f} m 占比 {far_fraction:.1%} 超过门 {GATE['far_fraction_max']:.0%}")
    if max_downcut > GATE["max_downcut_m"]:
        reasons.append(f"最大下切 {max_downcut:.1f} m 超过门 {GATE['max_downcut_m']:.0f} m")
    rec["gate"] = {
        "status": "FAIL" if reasons else "PASS",
        "thresholds": GATE,
        "reasons": reasons,
    }
    if rec["columns"]["single_value_fraction"] is not None:
        rec["gate"]["note_columns"] = (
            f"{rec['columns']['single_value_columns']}/{rec['columns']['columns_with_edit']} "
            "列刻蚀值为常值：渲染呈等宽竖条"
        )
    return rec


def discover_runs() -> list[Path]:
    runs = []
    for dem in sorted((PROJ / "outputs" / "ningming").rglob("dem.asc")):
        run_dir = dem.parent
        rel = run_dir.relative_to(PROJ).as_posix()
        if any(m in rel for m in EXCLUDE_MARKERS):
            continue
        runs.append(run_dir)
    return runs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, default=None, help="单个 run 目录（项目根相对）")
    parser.add_argument("--all-runs", action="store_true", help="扫描全部宁明 run")
    args = parser.parse_args()
    if not args.run and not args.all_runs:
        parser.error("必须给 --run 或 --all-runs")

    if args.run:
        run_dir = args.run if args.run.is_absolute() else PROJ / args.run
        records = [audit_run(run_dir)]
    else:
        records = [audit_run(run_dir) for run_dir in discover_runs()]

    counts: dict[str, int] = {}
    for rec in records:
        key = rec["gate"]["status"] if rec.get("gate") else rec["status"]
        counts[key] = counts.get(key, 0) + 1
    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "script": "scripts/stage3/dem_channel_audit.py",
        "water_mask": str(WATER_0806),
        "gate": GATE,
        "counts": counts,
        "runs": {rec["run"]: rec for rec in records},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    for rec in records:
        gate = (rec.get("gate") or {}).get("status", rec["status"])
        print(f"[{gate:12s}] {rec['run']}")
        if rec.get("gate") and rec["gate"].get("reasons"):
            for reason in rec["gate"]["reasons"]:
                print(f"    - {reason}")
            if rec["gate"].get("note_columns"):
                print(f"    - {rec['gate']['note_columns']}")
    print("counts", counts)
    print("->", OUT)


if __name__ == "__main__":
    main()
