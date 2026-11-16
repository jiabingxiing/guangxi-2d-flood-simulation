"""DEM NoData、零值和条带诊断。

本脚本只读栅格。脚本不修改源 DEM，不生成修正 DEM。
输出：data/catalog/dem_artifact_qa.json 和按案例分文件的 QA JSON。

示例：
    uv run python scripts/stage0/dem_artifact_qa.py --case all
    uv run python scripts/stage0/dem_artifact_qa.py --case ningming --points configs/qa_points.yaml
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
import yaml
from pyproj import Transformer

PROJ_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ_ROOT))
from scripts.common.paths import external  # noqa: E402  可移植：源 DEM 由 paths 解析

# 源 DEM 属可选外部资源：缺失时本脚本仍产出案例内 DEM 的 QA，仅跳过源点回读。
SOURCE = external("DEM_SOURCE")
CASE_SPECS = {
    "jiulong": {
        "path": PROJ_ROOT / "data/interim/jiulong/dem_12p5m_32649.tif",
        "epsg": 32649,
    },
    "ningming": {
        "path": PROJ_ROOT / "data/interim/ningming/dem_12p5m_32648.tif",
        "filled": PROJ_ROOT / "data/interim/ningming/dem_filled_12p5m_32648.tif",
        "epsg": 32648,
    },
    "guigang": {
        "path": PROJ_ROOT / "data/interim/guigang/dem_12p5m_32649.tif",
        "epsg": 32649,
    },
}
DEFAULT_POINTS = {
    "ningming": [
        {"id": "west_1", "lon": 106.966772, "lat": 22.102942},
        {"id": "west_2", "lon": 106.990978, "lat": 22.148786},
        {"id": "west_3", "lon": 107.014109, "lat": 22.168215},
    ],
    "guigang": [
        {"id": "east_1", "lon": 109.421226, "lat": 23.135074},
        {"id": "east_2", "lon": 109.761781, "lat": 23.083198},
    ],
    "jiulong": [],
}


def _stats(values: np.ndarray) -> dict[str, float | int | None]:
    values = np.asarray(values, dtype="float64")
    values = values[np.isfinite(values)]
    if not values.size:
        return {"n": 0, "min": None, "p50": None, "p95": None, "max": None}
    return {
        "n": int(values.size),
        "min": round(float(np.min(values)), 4),
        "p50": round(float(np.percentile(values, 50)), 4),
        "p95": round(float(np.percentile(values, 95)), 4),
        "max": round(float(np.max(values)), 4),
    }


def _profile(arr: np.ndarray, valid: np.ndarray, *, rows: bool) -> dict[str, Any]:
    n = arr.shape[0] if rows else arr.shape[1]
    profile = np.full(n, np.nan, dtype="float64")
    counts = np.zeros(n, dtype="int64")
    for idx in range(n):
        values = arr[idx, :][valid[idx, :]] if rows else arr[:, idx][valid[:, idx]]
        counts[idx] = values.size
        if values.size:
            profile[idx] = float(np.median(values))
    good = np.isfinite(profile)
    x = np.arange(n, dtype="float64")
    if good.sum() >= 2:
        coeff = np.polyfit(x[good], profile[good], 1)
        trend = np.polyval(coeff, x)
    else:
        coeff = [0.0, float(np.nanmedian(profile)) if good.any() else 0.0]
        trend = np.full(n, coeff[1], dtype="float64")
    residual = profile - trend
    residual[~good] = np.nan
    diffs = np.diff(profile[good]) if good.sum() > 1 else np.array([], dtype="float64")
    finite_res = residual[np.isfinite(residual)]
    if finite_res.size:
        mad = float(np.median(np.abs(finite_res - np.median(finite_res))))
        flag_threshold = max(0.30, 6.0 * mad)
        flagged = np.isfinite(residual) & (np.abs(residual) > flag_threshold)
    else:
        mad = 0.0
        flag_threshold = 0.30
        flagged = np.zeros(n, dtype=bool)
    return {
        "n_profile": int(n),
        "valid_profile_count": int(good.sum()),
        "median_valid_count": int(np.median(counts[good])) if good.any() else 0,
        "trend_slope_per_index": round(float(coeff[0]), 8),
        "residual": _stats(finite_res),
        "residual_mad": round(mad, 6),
        "flag_threshold_m": round(flag_threshold, 4),
        "flagged_profile_count": int(flagged.sum()),
        "flagged_profile_fraction": round(float(flagged.mean()), 6),
        "adjacent_jump": _stats(np.abs(diffs)),
    }


def _point_record(ds: rasterio.DatasetReader, point: dict[str, Any]) -> dict[str, Any]:
    tf = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True)
    x, y = tf.transform(float(point["lon"]), float(point["lat"]))
    inv = ~ds.transform
    col_f, row_f = inv * (x, y)
    col, row = math.floor(col_f), math.floor(row_f)
    center_x, center_y = ds.transform * (col + 0.5, row + 0.5)
    rec: dict[str, Any] = {
        "id": str(point.get("id", f"{point['lon']},{point['lat']}")),
        "lon": float(point["lon"]),
        "lat": float(point["lat"]),
        "x": round(float(x), 3),
        "y": round(float(y), 3),
        "row": int(row),
        "col": int(col),
        "row_f": round(float(row_f), 6),
        "col_f": round(float(col_f), 6),
        "row_residual_px": round(float(row_f - (row + 0.5)), 6),
        "col_residual_px": round(float(col_f - (col + 0.5)), 6),
        "center_dx_m": round(float(x - center_x), 6),
        "center_dy_m": round(float(y - center_y), 6),
        "inside": bool(0 <= row < ds.height and 0 <= col < ds.width),
    }
    if not rec["inside"]:
        return rec
    win = rasterio.windows.Window(max(0, col - 2), max(0, row - 2), 5, 5)
    values = ds.read(1, window=win, boundless=True, fill_value=ds.nodata)
    center = ds.read(1, window=rasterio.windows.Window(col, row, 1, 1))[0, 0]
    valid = np.isfinite(values)
    if ds.nodata is not None:
        valid &= values != ds.nodata
    rec.update(
        {
            "center_value_m": None if not np.isfinite(center) else round(float(center), 4),
            "window_stats_m": _stats(values[valid]),
            "window_zero_px": int(np.count_nonzero(values == 0)),
            "window_negative_px": int(np.count_nonzero((values < 0) & valid)),
            "window_nodata_px": int(np.count_nonzero(~valid)),
        }
    )
    return rec


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(chunk):
            h.update(block)
    return h.hexdigest()


def _profile_record(
    ds: rasterio.DatasetReader,
    coords: list[float],
    step_m: float | None,
) -> dict[str, Any]:
    tf = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True)
    x0, y0 = tf.transform(coords[0], coords[1])
    x1, y1 = tf.transform(coords[2], coords[3])
    length = float(np.hypot(x1 - x0, y1 - y0))
    step = float(step_m or max(abs(ds.transform.a), abs(ds.transform.e)))
    n = 1 if length == 0 else int(np.ceil(length / step)) + 1
    xs = np.linspace(x0, x1, n)
    ys = np.linspace(y0, y1, n)
    inv = ~ds.transform
    rows_f, cols_f = [], []
    samples: list[float] = []
    counts = {"outside": 0, "valid": 0, "nodata": 0, "zero_valid": 0, "negative": 0}
    for x, y in zip(xs, ys):
        col_f, row_f = inv * (float(x), float(y))
        col, row = math.floor(col_f), math.floor(row_f)
        rows_f.append(row_f); cols_f.append(col_f)
        if not (0 <= row < ds.height and 0 <= col < ds.width):
            counts["outside"] += 1
            continue
        value = float(ds.read(1, window=rasterio.windows.Window(col, row, 1, 1))[0, 0])
        if not np.isfinite(value) or (ds.nodata is not None and value == ds.nodata):
            counts["nodata"] += 1
            continue
        counts["valid"] += 1
        if value == 0:
            counts["zero_valid"] += 1
        if value < 0:
            counts["negative"] += 1
        samples.append(value)
    return {
        "start_wgs84": coords[:2], "end_wgs84": coords[2:],
        "start_projected": [round(x0, 3), round(y0, 3)],
        "end_projected": [round(x1, 3), round(y1, 3)],
        "distance_m": round(length, 3), "step_m": round(step, 3),
        "sample_count": n, **counts, "stats": _stats(np.asarray(samples)),
        "max_abs_row_residual_px": round(max(abs(v - (math.floor(v) + 0.5)) for v in rows_f), 6) if rows_f else 0.0,
        "max_abs_col_residual_px": round(max(abs(v - (math.floor(v) + 0.5)) for v in cols_f), 6) if cols_f else 0.0,
    }


def _source_point_record(point: dict[str, Any]) -> dict[str, Any]:
    if not SOURCE.is_file():
        return {"id": point.get("id"), "source_exists": False}
    with rasterio.open(SOURCE) as ds:
        rec = _point_record(ds, point)
        rec["source_exists"] = True
        rec["source_nodata"] = None if ds.nodata is None else float(ds.nodata)
        if rec.get("inside"):
            rec["source_zero_is_nodata"] = bool(rec.get("center_value_m") == 0.0 and ds.nodata == 0)
        return rec


def _load_points(path: Path | None) -> dict[str, list[dict[str, Any]]]:
    if path is None:
        return DEFAULT_POINTS
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "points" in raw:
        raw = raw["points"]
    if isinstance(raw, list):
        points = raw
    elif isinstance(raw, dict):
        points = [dict(v, id=k) if isinstance(v, dict) else {"id": k, **v} for k, v in raw.items()]
    else:
        raise ValueError("points 文件必须是列表、字典或包含 points 的字典")
    return {case: [dict(p) for p in points] for case in CASE_SPECS}


def analyze_case(case: str, points: list[dict[str, Any]], profiles: list[list[float]], profile_step_m: float | None) -> dict[str, Any]:
    spec = CASE_SPECS[case]
    path: Path = spec["path"]
    if not path.is_file():
        return {"case": case, "path": str(path), "status": "missing"}
    with rasterio.open(path) as ds:
        arr = ds.read(1).astype("float64")
        valid = np.isfinite(arr)
        if ds.nodata is not None:
            valid &= arr != ds.nodata
        values = arr[valid]
        qa = {
            "valid_px": int(valid.sum()),
            "nodata_px": int((~valid).sum()),
            "zero_px": int(np.count_nonzero((arr == 0) & valid)),
            "negative_px": int(np.count_nonzero((arr < 0) & valid)),
            "zero_fraction_valid": round(float(np.count_nonzero((arr == 0) & valid) / max(valid.sum(), 1)), 8),
            "negative_fraction_valid": round(float(np.count_nonzero((arr < 0) & valid) / max(valid.sum(), 1)), 8),
            "elevation_m": _stats(values),
        }
        row_profile = _profile(arr, valid, rows=True)
        col_profile = _profile(arr, valid, rows=False)
        ratio = max(
            float(row_profile["residual"].get("p95") or 0.0),
            float(col_profile["residual"].get("p95") or 0.0),
        ) / max(min(
            float(row_profile["residual"].get("p95") or 0.0),
            float(col_profile["residual"].get("p95") or 0.0),
        ), 1e-9)
        gradient_source = np.where(valid, arr, np.nanmedian(values) if values.size else 0.0)
        gy, gx = np.gradient(gradient_source)
        gradient = np.hypot(gx, gy)[valid]
        result: dict[str, Any] = {
            "case": case,
            "path": str(path),
            "sha256": _sha256(path),
            "read_only": True,
            "status": "diagnostic_only",
            "metadata": {
                "crs": str(ds.crs),
                "width": ds.width,
                "height": ds.height,
                "resolution_m": [float(abs(ds.res[0])), float(abs(ds.res[1]))],
                "nodata": None if ds.nodata is None else float(ds.nodata),
                "bounds": [float(v) for v in ds.bounds],
            },
            "qa": qa,
            "axis_profiles": {
                "rows": row_profile,
                "columns": col_profile,
                "direction_energy_ratio": round(ratio, 4),
                "gradient_m_per_px": _stats(gradient),
            },
            "points": [_point_record(ds, p) for p in points],
            "profiles": [_profile_record(ds, profile, profile_step_m) for profile in profiles],
            "source_points": [_source_point_record(p) for p in points],
            "thresholds_recommended_not_frozen": {
                "seam_jump_p95_m": 0.20,
                "profile_residual_p95_m": 0.30,
                "affected_area_fraction": 0.01,
                "direction_energy_ratio": 1.5,
            },
            "notes": [
                "行列剖面只用于诊断，不能单独证明条带是错误。",
                "真实河谷、堤顶、道路、建筑和河槽刻蚀必须单独核查。",
                "源 DEM 的 0 值语义仍未闭合。",
            ],
        }
    filled = spec.get("filled")
    if filled and Path(filled).is_file():
        with rasterio.open(filled) as filled_ds:
            filled_arr = filled_ds.read(1).astype("float64")
        diff = filled_arr - arr
        changed = np.isfinite(diff) & (np.abs(diff) > 1e-6)
        result["filled_comparison"] = {
            "path": str(filled),
            "changed_px": int(changed.sum()),
            "changed_fraction": round(float(changed.mean()), 8),
            "absolute_difference_m": _stats(np.abs(diff[changed])),
            "signed_difference_m": _stats(diff[changed]),
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=["all", *CASE_SPECS], default="all")
    parser.add_argument("--points", type=Path, default=None)
    parser.add_argument("--point", nargs=2, action="append", type=float, metavar=("LON", "LAT"))
    parser.add_argument("--profile", nargs=4, action="append", type=float, metavar=("LON1", "LAT1", "LON2", "LAT2"))
    parser.add_argument("--profile-step-m", type=float, default=None)
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args()
    points = _load_points(args.points)
    if args.point:
        points = {case: [{"id": f"cli_{i}", "lon": p[0], "lat": p[1]} for i, p in enumerate(args.point)] for case in CASE_SPECS}
    profiles = args.profile or []
    cases = list(CASE_SPECS) if args.case == "all" else [args.case]
    results = {case: analyze_case(case, points.get(case, []), profiles, args.profile_step_m) for case in cases}
    generated = datetime.now(timezone.utc).isoformat()
    for case, result in results.items():
        result["generated_utc"] = generated
        (PROJ_ROOT / "data/catalog" / f"dem_artifact_qa_{case}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    payload = {
        "generated_utc": generated,
        "script": "scripts/stage0/dem_artifact_qa.py",
        "source": str(SOURCE),
        "cases": results,
    }
    out = PROJ_ROOT / "data/catalog/dem_artifact_qa.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for case, rec in results.items():
            print(f"{case}: {rec['status']} {rec['path']}")
            print(f"  valid={rec.get('qa', {}).get('valid_px')} zero={rec.get('qa', {}).get('zero_px')} negative={rec.get('qa', {}).get('negative_px')}")
        print(f"-> {out}")


if __name__ == "__main__":
    main()
