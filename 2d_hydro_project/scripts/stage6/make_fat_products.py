"""FAT / 超阈时间产品与站址暴露三元组表（ADR-0015 口径；零重跑）。

对应计划 T02/T04（成功标准 S1：PASS run 覆盖率 100% + 三地各 ≥1 份站点级
超阈时间表；S7 业务登记的输入侧）。口径（ADR-0015 §2，湿判据与 ADR-0006 一致）：

- 首湿时刻 = 像元首次 wd>0.05 m 的模型时刻（h）；时基 = 帧序 × saveint
  （run.par 校验，禁止写死 6 h）；
- 超阈时刻 = 首次 wd>情景阈值（0.3/0.5/1.0 m 三档）；**情景假设，禁止称设备安全值**；
- 超阈历时 = 累计超阈帧数 × saveint；
- 站点表 = 站址像元 ±2 格（ADR-0006）；中心未湿报 NA 并附最近湿像元
  （Chebyshev 环搜）距离与其首湿时刻（久隆站址 0.008 m 先例在案）；
- 三级窗（<6 h / 6–24 h / >24 h）为**研究性分组，非应急等级**；
- 覆盖 = `data/catalog/mass_recheck_g15.json` 全部 PASS run；非 PASS 不产出。

只读 `outputs/**` 的 run 目录（不改写、不调用 lisflood.exe）；产品写
`outputs/products/<site>/fat/<run_id>/`；站表写 `.../fat/station_tables/`；
审计写 `data/catalog/fat_products_audit.json`（含证据等级与未验证字段）。

用法（先设 `$env:PYTHONUTF8="1"`）::

    uv --directory 2d_hydro_project run python scripts/stage6/make_fat_products.py --dry-run
    uv --directory 2d_hydro_project run python scripts/stage6/make_fat_products.py --site guigang
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import (  # noqa: E402
    AuditLog,
    GridError,
    load_sea_mask,
    parse_run_par,
    read_ascii_grid,
    run_frames,
    utc_now,
    write_json,
)
from scripts.common.paths import PathResolutionError, rel_posix  # noqa: E402

WET_THR = 0.05
DEFAULT_THRESHOLDS = (0.3, 0.5, 1.0)
SITE_RADIUS_CELLS = 2
NEAREST_CAP_CELLS = 100
NA_VALUE = -9999.0
NO_TIME = -1.0  # 内部「未发生」哨兵（不写入 tif）
# 三级窗（钟鸣 2025 FAT 思想降级；研究性分组，非应急等级）
WINDOWS_H = ((0.0, 6.0, "w1_lt6h"), (6.0, 24.0, "w2_6to24h"), (24.0, float("inf"), "w3_gt24h"))
EVIDENCE_LEVEL = "E2（模拟情景派生，非实测）"

SITES = {
    "guigang": {"name": "贵港市区", "lon": 109.60, "lat": 23.11, "epsg": 32649},
    "jiulong": {"name": "久隆500kV站", "lon": 108.7161, "lat": 22.0638, "epsg": 32649},
    "ningming": {"name": "宁明县城站", "lon": 107.0719, "lat": 22.1403, "epsg": 32648},
}

# 各站点主表锚点 run（站表单列展示用；其余 PASS run 仍全量产出）
ANCHOR_RUNS = {
    "guigang": "scen_yagi_q11500_100m_dt2p5_chhyd_d14_r4_infil3",
    "jiulong": "scen_free_200m_sv6h",
    "ningming": "scen_q6000_n100_100m",
}


def thr_tag(thr: float) -> str:
    """0.3 → '0p3'（tif 命名去歧义）。"""
    return f"{thr:g}".replace(".", "p")


def _rel_or_abs(path: Path) -> str:
    """审计记录用相对路径；目录不在项目内（测试 tmp）时退回原样字符串。"""
    try:
        return rel_posix(path)
    except PathResolutionError:
        return str(path)


@dataclass
class FatResult:
    """一次 run 的 FAT 数组（内部口径：NO_TIME=-1 表示未发生）。"""

    saveint_h: float
    first_wet_h: np.ndarray
    exceed_first_h: dict[float, np.ndarray] = field(default_factory=dict)
    exceed_frames: dict[float, np.ndarray] = field(default_factory=dict)

    def duration_h(self, thr: float) -> np.ndarray:
        return self.exceed_frames[thr] * self.saveint_h


def read_frame_wd(path: str | Path, expected_shape: tuple[int, int]) -> np.ndarray:
    """读单帧 wd（ESRI ASCII，float32）。"""
    grid = read_ascii_grid(path, dtype=np.float32, expected_shape=expected_shape)
    return grid.data


def compute_fat(
    frames: list[tuple[int, Path]],
    saveint_h: float,
    thresholds: tuple[float, ...],
    shape: tuple[int, int],
    *,
    sea_mask: np.ndarray | None = None,
    progress: bool = False,
) -> FatResult:
    """逐帧扫描，更新首湿 / 各档超阈时刻与累计帧数。

    sea_mask 非 None 时被掩膜像元恒记未发生（不计首湿/超阈/历时）。
    """
    first_wet = np.full(shape, NO_TIME, dtype=np.float64)
    exceed_first = {thr: np.full(shape, NO_TIME, dtype=np.float64) for thr in thresholds}
    exceed_cnt = {thr: np.zeros(shape, dtype=np.int32) for thr in thresholds}
    for n, (seq, path) in enumerate(frames):
        wd = read_frame_wd(path, shape)
        if sea_mask is not None:
            wd = np.where(sea_mask, np.float32(-1.0), wd)
        t = seq * saveint_h
        wet = (wd > np.float32(WET_THR)) & (first_wet < 0)
        first_wet[wet] = t
        for thr in thresholds:
            exc = wd > np.float32(thr)
            exceed_cnt[thr] += exc
            newly = exc & (exceed_first[thr] < 0)
            exceed_first[thr][newly] = t
        if progress and (n + 1) % 10 == 0:
            print(f"  frame {n + 1}/{len(frames)}", flush=True)
    return FatResult(
        saveint_h=saveint_h, first_wet_h=first_wet,
        exceed_first_h=exceed_first, exceed_frames=exceed_cnt,
    )


def nearest_wet(
    wet_any: np.ndarray, first_wet: np.ndarray, row: int, col: int, cap: int = NEAREST_CAP_CELLS
) -> dict[str, Any] | None:
    """站址像元未湿时找最近湿像元（Chebyshev 环搜，同距取首湿最早）。

    返回 None 表示 cap 环内无湿像元（如实记 not_found_within）。
    """
    if wet_any[row, col]:
        return {"distance_cells": 0, "row": row, "col": col,
                "first_wet_h": float(first_wet[row, col])}
    nrows, ncols = wet_any.shape
    for r in range(1, cap + 1):
        candidates: list[tuple[float, int, int]] = []
        for dr in range(-r, r + 1):
            for dc in range(-r, r + 1):
                if max(abs(dr), abs(dc)) != r:
                    continue  # 只扫当前环
                rr, cc = row + dr, col + dc
                if 0 <= rr < nrows and 0 <= cc < ncols and wet_any[rr, cc]:
                    candidates.append((float(first_wet[rr, cc]), rr, cc))
        if candidates:
            candidates.sort(key=lambda item: (item[0], item[1], item[2]))
            t, rr, cc = candidates[0]
            return {"distance_cells": r, "row": rr, "col": cc, "first_wet_h": t}
    return None


def window_label(t_h: float) -> str:
    """三级窗标签；NO_TIME 记 never。"""
    if t_h < 0:
        return "never"
    for lo, hi, name in WINDOWS_H:
        if lo <= t_h < hi:
            return name
    return "w3_gt24h"


def window_counts(arr: np.ndarray) -> dict[str, int]:
    """超阈时刻数组的窗分布（未发生单独计 never）。"""
    out: dict[str, int] = {"never": 0}
    for _lo, _hi, name in WINDOWS_H:
        out[name] = 0
    never = arr < 0
    out["never"] = int(never.sum())
    for lo, hi, name in WINDOWS_H:
        out[name] = int(((arr >= lo) & (arr < hi)).sum())
    return out


def site_rowcol(
    hdr: dict[str, float], lon: float, lat: float, epsg: int
) -> tuple[int, int]:
    """经纬度 → 网格 (row, col)，越界抛 GridError（不静默裁剪）。"""
    from pyproj import Transformer

    tf = Transformer.from_crs(4326, epsg, always_xy=True)
    x, y = tf.transform(lon, lat)
    cell = hdr["cellsize"]
    col = int((x - hdr["xllcorner"]) / cell)
    row = int((hdr["yllcorner"] + hdr["nrows"] * cell - y) / cell)
    if not (0 <= row < hdr["nrows"] and 0 <= col < hdr["ncols"]):
        raise GridError(
            f"站址 ({lon},{lat}) → 网格外 (row={row},col={col})；检查站址坐标或网格范围"
        )
    return row, col


def _win(
    arr: np.ndarray, row: int, col: int, rad: int = SITE_RADIUS_CELLS
) -> np.ndarray:
    """站址 ±rad 格子窗视图（边界自动截断）。"""
    r0, r1 = max(0, row - rad), min(arr.shape[0], row + rad + 1)
    c0, c1 = max(0, col - rad), min(arr.shape[1], col + rad + 1)
    return arr[r0:r1, c0:c1]


def _round_or_none(v: float, nd: int = 2) -> float | None:
    return round(float(v), nd) if v >= 0 else None


def station_record(
    fat: FatResult,
    resmax: np.ndarray,
    hdr: dict[str, float],
    site_key: str,
    run_name: str,
    thresholds: tuple[float, ...],
    *,
    nearest_cap: int = NEAREST_CAP_CELLS,
) -> dict[str, Any]:
    """站址暴露三元组记录：水深（res.max r2）— 到达（首湿/超阈）— 历时。"""
    cfg = SITES[site_key]
    row, col = site_rowcol(hdr, cfg["lon"], cfg["lat"], cfg["epsg"])
    wet_any = fat.first_wet_h >= 0
    first_sub = _win(fat.first_wet_h, row, col)
    first_pos = first_sub[first_sub >= 0]
    rec: dict[str, Any] = {
        "site": site_key, "site_name": cfg["name"], "run": run_name,
        "center_row": int(row), "center_col": int(col),
        "radius_cells": SITE_RADIUS_CELLS,
        "depth_max_center_m": round(float(resmax[row, col]), 3),
        "depth_max_r2_m": round(float(np.max(_win(resmax, row, col))), 3),
        "center_first_wet_h": _round_or_none(fat.first_wet_h[row, col]),
        "r2_first_wet_h": round(float(first_pos.min()), 2) if first_pos.size else None,
    }
    for thr in thresholds:
        tag = thr_tag(thr)
        arr = fat.exceed_first_h[thr]
        dur = fat.duration_h(thr)
        center = arr[row, col]
        sub = _win(arr, row, col)
        pos = sub[sub >= 0]
        rec[f"center_exceed_{tag}_h"] = _round_or_none(center)
        rec[f"r2_exceed_{tag}_h"] = round(float(pos.min()), 2) if pos.size else None
        rec[f"center_duration_{tag}_h"] = round(float(dur[row, col]), 2)
        rec[f"r2_duration_{tag}_max_h"] = round(float(np.max(_win(dur, row, col))), 2)
        rec[f"r2_exceed_{tag}_cells"] = int((sub >= 0).sum())
        if center >= 0:
            rec[f"center_window_{tag}"] = window_label(float(center))
    if fat.first_wet_h[row, col] < 0:
        near = nearest_wet(wet_any, fat.first_wet_h, row, col, cap=nearest_cap)
        if near is None:
            rec["nearest_wet"] = {"found": False, "cap_cells": nearest_cap}
        else:
            rec["nearest_wet"] = {"found": True, **{k: (round(v, 2) if isinstance(v, float) else v)
                                                    for k, v in near.items()}}
    return rec


def write_tif(
    path: Path, arr: np.ndarray, hdr: dict[str, float], epsg: int
) -> Path:
    """写 float32 GeoTIFF（deflate；未发生/掩膜 → NA_VALUE）。"""
    import rasterio

    path.parent.mkdir(parents=True, exist_ok=True)
    cell = hdr["cellsize"]
    data = np.where(arr >= 0, arr, NA_VALUE).astype("float32")
    with rasterio.open(
        path, "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1,
        dtype="float32", crs=f"EPSG:{epsg}",
        transform=rasterio.Affine(cell, 0, hdr["xllcorner"], 0, -cell,
                                  hdr["yllcorner"] + arr.shape[0] * cell),
        compress="deflate", nodata=NA_VALUE,
    ) as dst:
        dst.write(data, 1)
    return path


def load_gate_runs(catalog: Path) -> dict[str, dict[str, Any]]:
    """读质量门 SSOT，返回 {相对路径: 记录}（只保留 PASS）。"""
    data = json.loads(catalog.read_text(encoding="utf-8"))
    return {p: rec for p, rec in data["runs"].items() if rec.get("gate_1e-3") == "PASS"}


def saveint_hours(run_dir: Path) -> float:
    """run.par 的 saveint（秒）→ 小时；缺失/非法 fail-closed。"""
    par = parse_run_par(run_dir)
    raw = par.get("saveint")
    if raw is None or float(raw) <= 0:
        raise GridError(f"{run_dir} run.par 缺 saveint；帧时基口径不唯一，拒绝产出")
    return float(raw) / 3600.0


def process_run(
    run_path: Path,
    site: str,
    thresholds: tuple[float, ...],
    out_root: Path,
    *,
    dry_run: bool = False,
    progress: bool = False,
) -> dict[str, Any]:
    """单 run FAT 产出（含站点表）；返回审计记录。"""
    run_name = run_path.name
    results = run_path / "results"
    frames = run_frames(results)
    rec: dict[str, Any] = {"run": run_name, "path": _rel_or_abs(run_path), "site": site}
    if not frames:
        rec["status"] = "skipped_no_frames"
        return rec
    if not (results / "res.max").is_file():
        rec["status"] = "skipped_no_resmax"
        return rec
    rmax_grid = read_ascii_grid(results / "res.max", dtype=np.float32)
    shape = rmax_grid.shape
    saveint_h = saveint_hours(run_path)
    sea = None
    mask_path = run_path / "sea_mask.npy"
    if mask_path.is_file():
        sea = load_sea_mask(run_path, shape)
    fat = compute_fat(frames, saveint_h, thresholds, shape, sea_mask=sea, progress=progress)
    rec.update({
        "status": "ok", "n_frames": len(frames),
        "saveint_h": saveint_h,
        "frame_t_range_h": [round(frames[0][0] * saveint_h, 2), round(frames[-1][0] * saveint_h, 2)],
        "wet_cells_event_max": int((fat.first_wet_h >= 0).sum()),
        "sea_masked": sea is not None,
        "thresholds_m": list(thresholds),
    })
    out_dir = out_root / site / "fat" / run_name
    if not dry_run:
        epsg = SITES[site]["epsg"]
        write_tif(out_dir / "first_wet_h.tif", fat.first_wet_h, rmax_grid.header, epsg)
        outputs = ["first_wet_h.tif"]
        for thr in thresholds:
            tag = thr_tag(thr)
            write_tif(out_dir / f"exceed_h_thr{tag}.tif", fat.exceed_first_h[thr],
                      rmax_grid.header, epsg)
            write_tif(out_dir / f"duration_h_thr{tag}.tif", fat.duration_h(thr),
                      rmax_grid.header, epsg)
            outputs += [f"exceed_h_thr{tag}.tif", f"duration_h_thr{tag}.tif"]
        rec["outputs"] = outputs
        rec["output_dir"] = _rel_or_abs(out_dir)
    # 窗分布（研究性三级窗）与站表
    for thr in thresholds:
        rec[f"window_counts_thr{thr_tag(thr)}"] = window_counts(fat.exceed_first_h[thr])
    resmax = np.where(sea, np.float32(-1.0), rmax_grid.data) if sea is not None else rmax_grid.data
    st = station_record(fat, resmax, rmax_grid.header, site, run_name, thresholds)
    rec["station"] = st
    if not dry_run:
        st_dir = out_root / site / "fat" / "station_tables"
        st_dir.mkdir(parents=True, exist_ok=True)
        _write_station_csv(st_dir / f"{run_name}_stations.csv", [st])
        write_json(st_dir / f"{run_name}_stations.json", {
            "generated_utc": utc_now(), "evidence_level": EVIDENCE_LEVEL,
            "note": "站址值 ±2 格面积级（ADR-0006）；超阈阈值为情景假设，非设备安全值；"
                    "三级窗为研究性分组，非应急等级",
            "stations": [st],
        })
    return rec


def _write_station_csv(path: Path, rows: list[dict[str, Any]]) -> Path:
    """站表 CSV（NaN/None → NA；一行一站）。"""
    if not rows:
        return path
    keys = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(keys)
        for row in rows:
            vals = []
            for k in keys:
                v = row.get(k)
                if isinstance(v, dict):
                    v = json.dumps(v, ensure_ascii=False)
                vals.append("NA" if v is None else v)
            writer.writerow(vals)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", choices=["all", *SITES.keys()], default="all")
    parser.add_argument("--run", default=None, help="run 名过滤（子串）")
    parser.add_argument("--thresholds", default="0.3,0.5,1.0")
    parser.add_argument("--limit", type=int, default=None, help="每站点最多处理 N 个 run（调试）")
    parser.add_argument("--progress", action="store_true", help="逐帧进度打印")
    from scripts.common.grid_io import add_common_args

    add_common_args(parser)
    args = parser.parse_args()
    thresholds = tuple(sorted(float(t) for t in args.thresholds.split(",") if t.strip()))
    if any(t <= WET_THR for t in thresholds):
        raise SystemExit(f"超阈阈值必须 > 湿判据 {WET_THR} m；实得 {thresholds}")

    catalog = PROJ / "data" / "catalog" / "mass_recheck_g15.json"
    gate = load_gate_runs(catalog)
    out_root = args.out_dir or (PROJ / "outputs" / "products")
    log = AuditLog("make_fat_products.py", run_tag=args.run_tag, dry_run=args.dry_run)
    log.param(site=args.site, thresholds=list(thresholds), run_filter=args.run,
              nearest_cap_cells=NEAREST_CAP_CELLS, wet_thr=WET_THR)
    log.payload["evidence_level"] = EVIDENCE_LEVEL
    log.payload["notes"] = [
        "超阈阈值为情景假设，禁止称设备安全值",
        "三级窗为研究性分组，非应急等级",
        "覆盖=mass_recheck_g15.json 全部 PASS run；FAIL-EXPLAINED 不产出",
    ]
    log.add_input("mass_recheck_g15", catalog)

    sites = list(SITES) if args.site == "all" else [args.site]
    summary: dict[str, Any] = {"sites": {}}
    for site in sites:
        want = [p for p, rec in gate.items() if f"/{site}/" in p.replace("\\", "/")]
        if args.run:
            want = [p for p in want if args.run in Path(p).name]
        want.sort()
        if args.limit:
            want = want[: args.limit]
        ok, skipped = [], []
        for rel in want:
            run_path = PROJ / rel
            print(f"[{site}] {Path(rel).name}", flush=True)
            try:
                rec = process_run(run_path, site, thresholds, out_root,
                                  dry_run=args.dry_run, progress=args.progress)
                log.step(f"{site}/{Path(rel).name}", status=rec["status"])
            except (GridError, FileNotFoundError, ValueError) as exc:
                rec = {"run": Path(rel).name, "status": "error", "error": str(exc)}
                log.error(f"{rel}: {exc}")
            (skipped if rec["status"] != "ok" else ok).append(rec)
        anchor = ANCHOR_RUNS.get(site)
        anchor_ok = any(r["run"] == anchor and r["status"] == "ok" for r in ok)
        summary["sites"][site] = {
            "gate_pass_total": len([p for p in gate if f"/{site}/" in p.replace("\\", "/")]),
            "processed": len(ok), "skipped": len(skipped),
            "anchor_run": anchor, "anchor_ok": anchor_ok,
            "records": ok + skipped,
        }
        print(f"[{site}] ok={len(ok)} skipped={len(skipped)} anchor_ok={anchor_ok}", flush=True)
    log.payload["coverage"] = summary
    log.param(dry_run=args.dry_run, out_root=rel_posix(out_root) if not args.out_dir else str(out_root))
    out_json = args.log_json or (PROJ / "data" / "catalog" / "fat_products_audit.json")
    log.write(out_json)
    print("->", out_json, flush=True)


if __name__ == "__main__":
    main()
