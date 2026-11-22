"""阶段6：贵港滩地积水诊断（只读，ADR-0014 预诊断）。

对象：r2 组合 run（默认 chhyd_d14_r2_ga）的 res.max。量化「要排多少水」：
湿面积/蓄水体积/水深谱 + 湿格到主槽的距离分布。结果只作诊断对照基准，
不用于布沟（循环论证隔离，ADR-0014）。

用法：uv run python scripts/stage6/diagnose_guigang_ponding.py [--run <run_id>]
"""
from __future__ import annotations

import json
import sys
from collections import deque
from pathlib import Path

import numpy as np

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))
from scripts.common.grid_io import utc_now, write_json  # noqa: E402

RUNS_ROOT = PROJ / "outputs" / "guigang" / "2024-09-yagi"
OUT_JSON = PROJ / "data" / "catalog" / "guigang_ponding_diagnosis.json"
WET_THR = 0.05


def _read_asc(p: Path) -> tuple[np.ndarray, dict]:
    lines = p.read_text().splitlines()
    hdr = {ln.split()[0].lower(): float(ln.split()[1]) for ln in lines[:6]}
    return np.loadtxt(p, skiprows=6, dtype=np.float32, ndmin=2), hdr


def _chamfer_dist(src_mask: np.ndarray) -> np.ndarray:
    """8 邻域 chamfer 距离格数（BFS）。"""
    h, w = src_mask.shape
    dist = np.full((h, w), -1, dtype=np.int32)
    q: deque[tuple[int, int]] = deque()
    for r, c in zip(*np.nonzero(src_mask)):
        dist[r, c] = 0
        q.append((r, c))
    while q:
        r, c = q.popleft()
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                nr, nc = r + dr, c + dc
                if 0 <= nr < h and 0 <= nc < w and dist[nr, nc] < 0:
                    dist[nr, nc] = dist[r, c] + 1
                    q.append((nr, nc))
    return dist


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="scen_yagi_q11500_100m_dt2p5_chhyd_d14_r2_ga")
    ap.add_argument("--frame", default="res.max")
    args = ap.parse_args()

    run_dir = RUNS_ROOT / args.run
    res_path = run_dir / "results" / args.frame
    if not res_path.is_file():
        raise SystemExit(f"缺 {res_path}")
    dem, hdr = _read_asc(res_path)
    cell = float(hdr["cellsize"])
    area_cell = cell * cell
    wet = dem > WET_THR

    with np.errstate(invalid="ignore"):
        vol_m3 = float(np.where(wet, dem, 0.0).astype("float64").sum() * area_cell)
    depths = dem[wet].astype("float64")
    q = np.percentile(depths, [50, 90, 99]) if depths.size else [0, 0, 0]

    # 距主槽距离（主槽 = run 的 dem_edit_mask）
    mask_p = run_dir / "dem_edit_mask.tif"
    dist_stats: dict = {"status": "mask_missing"}
    if mask_p.is_file():
        import rasterio
        with rasterio.open(mask_p) as src:
            chan = src.read(1) == 1
        if chan.shape == wet.shape and chan.any():
            d = _chamfer_dist(chan)
            wd = d[wet]
            dist_stats = {
                "metric": "8邻域chamfer格数（×cellsize=米）",
                "wet_cells": int(wd.size),
                "median_cells": round(float(np.median(wd)), 1) if wd.size else None,
                "p90_cells": round(float(np.percentile(wd, 90)), 1) if wd.size else None,
                "median_m": round(float(np.median(wd)) * cell, 1) if wd.size else None,
                "share_within_1km": round(float((wd <= 10).mean()), 4) if wd.size else None,
                "share_beyond_2km": round(float((wd > 20).mean()), 4) if wd.size else None,
            }

    payload = {
        "script": "scripts/stage6/diagnose_guigang_ponding.py",
        "generated_utc": utc_now(),
        "read_only": True,
        "run_id": args.run,
        "frame": args.frame,
        "wet_threshold_m": WET_THR,
        "wet_cells": int(wet.sum()),
        "wet_area_km2": round(int(wet.sum()) * area_cell / 1e6, 2),
        "ponded_volume_m3": round(vol_m3, 1),
        "depth_quantiles_m": {"p50": round(float(q[0]), 3), "p90": round(float(q[1]), 3),
                              "p99": round(float(q[2]), 3), "max": round(float(depths.max()), 3) if depths.size else 0.0},
        "distance_to_main_channel": dist_stats,
        "usage_note": "诊断对照基准（需要多少排水能力）；不用于布沟（ADR-0014 循环论证隔离）",
    }
    write_json(OUT_JSON, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
