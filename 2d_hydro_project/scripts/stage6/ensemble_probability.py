"""集合超越概率图（计划 T12 / S6；ADR-0018 §1 口径）。

- 成员 = 宁明 3Q×3n 矩阵 run（默认名样匹配），**强制质量门过滤**（非 PASS 禁入）；
- P(wd>thr|集合) = 成员超阈占比（event-max 口径，res.max）；
- thr ∈ {0.05（湿判据）, 0.3（情景阈值）}；float32 0–1 GeoTIFF；
- 成员清单（run、质量门状态、参数档）强制入审计 JSON（ADR-0015 §4）。

产出：`outputs/products/ningming/probability/exceedance_probability_thr*.tif`
+ `data/catalog/ningming_probability.json`。

用法（先设 `$env:PYTHONUTF8="1"`）::

    uv --directory 2d_hydro_project run python scripts/stage6/ensemble_probability.py --dry-run
    uv --directory 2d_hydro_project run python scripts/stage6/ensemble_probability.py
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import numpy as np

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import (  # noqa: E402
    AuditLog,
    GridError,
    read_ascii_grid,
    utc_now,
    write_json,
)
from scripts.common.paths import rel_posix  # noqa: E402

CATALOG = PROJ / "data" / "catalog"
DEFAULT_THRESHOLDS = (0.05, 0.3)
SITE = "ningming"
EVENT_DIR = PROJ / "outputs" / "ningming" / "2026-08-zitan"
MATRIX_PATTERN = re.compile(r"^scen_q(\d+)_n(\d+)_100m$")
EVIDENCE_LEVEL = "E3（参数枚举情景集合，非贝叶斯后验；禁业务告警）"


def _rel(path: Path) -> str:
    try:
        return rel_posix(path)
    except Exception:
        return str(path)


def exceedance_probability(
    resmax_stack: list[np.ndarray], thr: float, *, sea_mask: np.ndarray | None = None
) -> np.ndarray:
    """逐像元超阈成员占比（float64 0–1）；sea 掩膜格恒 0 并由 tif NA 处理。"""
    if not resmax_stack:
        raise GridError("空集合：无成员 res.max")
    shape = resmax_stack[0].shape
    exceed_count = np.zeros(shape, dtype=np.int32)
    for arr in resmax_stack:
        if arr.shape != shape:
            raise GridError(f"成员网格形状不一致：{arr.shape} vs {shape}")
        exceed_count += (arr > np.float32(thr))
    p = exceed_count.astype(np.float64) / len(resmax_stack)
    if sea_mask is not None:
        p = np.where(sea_mask, 0.0, p)
    return p


def write_prob_tif(path: Path, p: np.ndarray, hdr: dict[str, float], epsg: int,
                   sea_mask: np.ndarray | None) -> Path:
    """写 float32 概率 GeoTIFF（sea 掩膜格 → NA）。"""
    import rasterio

    path.parent.mkdir(parents=True, exist_ok=True)
    cell = hdr["cellsize"]
    data = p.astype("float32")
    nodata = -1.0
    if sea_mask is not None:
        data = np.where(sea_mask, nodata, data)
    with rasterio.open(
        path, "w", driver="GTiff", height=p.shape[0], width=p.shape[1], count=1,
        dtype="float32", crs=f"EPSG:{epsg}",
        transform=rasterio.Affine(cell, 0, hdr["xllcorner"], 0, -cell,
                                  hdr["yllcorner"] + p.shape[0] * cell),
        compress="deflate", nodata=nodata,
    ) as dst:
        dst.write(data, 1)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--thresholds", default="0.05,0.3")
    parser.add_argument("--pattern", default=MATRIX_PATTERN.pattern,
                        help="成员 run 名匹配模式（含 2 个捕获组：Q、n）")
    from scripts.common.grid_io import add_common_args

    add_common_args(parser)
    args = parser.parse_args()
    thresholds = tuple(sorted(float(t) for t in args.thresholds.split(",") if t.strip()))
    pattern = re.compile(args.pattern)
    log = AuditLog("ensemble_probability.py", run_tag=args.run_tag, dry_run=args.dry_run)
    log.param(site=SITE, thresholds=list(thresholds), pattern=args.pattern)

    gate_all = json.loads((CATALOG / "mass_recheck_g15.json").read_text(encoding="utf-8"))["runs"]
    members: list[dict[str, Any]] = []
    for path, rec in gate_all.items():
        name = Path(path).name
        m = pattern.match(name)
        if not m or f"/{SITE}/" not in path.replace("\\", "/"):
            continue
        if rec.get("gate_1e-3") != "PASS":
            log.warn(f"非 PASS run 拒绝入集合：{name}（gate={rec.get('gate_1e-3')}）")
            continue
        members.append({
            "run": name, "path": path, "gate": "PASS",
            "q_m3s": int(m.group(1)) if pattern.groups >= 1 else None,
            "n_manning": (int(m.group(2)) / 100.0) if pattern.groups >= 2 else None,
        })
    members.sort(key=lambda d: (d["q_m3s"] or 0, d["n_manning"] or 0))
    if not members:
        raise SystemExit("无合格集合成员（矩阵 run 须全部质量门 PASS）")
    log.step("members", n=len(members), names=[m["run"] for m in members])

    stack: list[np.ndarray] = []
    shapes = set()
    for m in members:
        resmax_p = PROJ / m["path"] / "results" / "res.max"
        if not resmax_p.is_file():
            raise FileNotFoundError(f"成员缺 res.max：{resmax_p}")
        grid = read_ascii_grid(resmax_p, dtype=np.float32)
        shapes.add(grid.shape)
        stack.append(grid.data)
        log.add_input(f"resmax_{m['run']}", resmax_p)
    if len(shapes) > 1:
        raise GridError(f"成员网格形状不一致：{shapes}")
    sea_mask = None  # 宁明域无海掩膜；接口保留（久隆复用时启用）
    # 取第一成员的网格头（同形状集合共用几何）
    from scripts.common.grid_io import read_ascii_header

    hdr = read_ascii_header(PROJ / members[0]["path"] / "results" / "res.max")

    summary = {
        "script": "ensemble_probability.py",
        "generated_utc": utc_now(),
        "adr": "ADR-0018",
        "site": SITE,
        "evidence_level": EVIDENCE_LEVEL,
        "n_members": len(members),
        "members": members,
        "thresholds_m": list(thresholds),
        "grid": {"nrows": int(hdr["nrows"]), "ncols": int(hdr["ncols"]),
                 "cellsize": hdr["cellsize"]},
        "outputs": [],
        "not_business_warning": True,
        "note": "情景集合参数枚举（3Q×3n）；概率=成员超阈占比，非年超越概率/预报概率",
        "dry_run": bool(args.dry_run),
    }
    if not args.dry_run:
        epsg = 32648
        out_dir = PROJ / "outputs" / "products" / SITE / "probability"
        for thr in thresholds:
            p = exceedance_probability(stack, thr, sea_mask=sea_mask)
            tag = f"{thr:g}".replace(".", "p")
            tif = write_prob_tif(out_dir / f"exceedance_probability_thr{tag}.tif",
                                 p, hdr, epsg, sea_mask)
            summary["outputs"].append(_rel(tif))
            # 面积摘要
            wet = p > 0
            summary[f"p_area_thr{tag}"] = {
                "cells_p_gt0": int(wet.sum()),
                "cells_p_ge_0p5": int((p >= 0.5).sum()),
                "cells_p_eq_1": int((p >= 1.0 - 1e-9).sum()),
                "area_km2_p_gt0": round(float(wet.sum()) * hdr["cellsize"] ** 2 / 1e6, 2),
            }
        log.step("written", outputs=summary["outputs"])
    out_json = args.log_json or (CATALOG / "ningming_probability.json")
    log.payload["probability"] = summary
    log.write(out_json)
    print(json.dumps({k: v for k, v in summary.items() if k != "members"},
                     ensure_ascii=False)[:800])
    print("members:", [m["run"] for m in members])
    print("->", out_json)


if __name__ == "__main__":
    main()
