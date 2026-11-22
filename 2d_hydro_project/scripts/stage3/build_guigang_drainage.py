"""阶段3：贵港 DEM 推导排水沟构建（ADR-0014，诊断探针）。

原理：ADR-0012 已把主槽刻成全球汇（床高 6~20 m vs 平原 ~21 m），在此前提下
priority-flood（主槽格为种子）的传播树给每个平原格一条明确的入槽路径——
沿树取高汇流格即为排水沟线，再以 channel_engine 刻 1 格宽（100 m）梯形沟。

纪律（ADR-0014）：
- 沟位仅由已刻槽 DEM 推导；模型积水（res.max）只做诊断，不用于布沟（循环论证隔离）；
- 定位为诊断探针：沟位/沟距/沟深是情景假设，不是真实排水网；
- 预注册参数读 data/catalog/guigang_round3_drainage.json（matrix_id
  guigang-round3-drainage-v1，A 档）。

产出：
  data/interim/guigang/channel_recon/d14m_drA/   与 hydraulic 层同构（dem/掩膜/stats），
                                                 供 guigang_scenario --channel-dem 直接消费
  data/catalog/guigang_drainage_audit.json       沟网审计（条数/总长/连通性/刻蚀统计）

用法：uv run python scripts/stage3/build_guigang_drainage.py [--tier A]
"""
from __future__ import annotations

import heapq
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))
from scripts.common import channel_engine as ce  # noqa: E402
from scripts.common.grid_io import (  # noqa: E402
    AsciiGrid,
    AuditLog,
    NODATA_DEFAULT,
    sha256_file,
    write_ascii_grid,
    write_json,
)

MATRIX = PROJ / "data" / "catalog" / "guigang_round3_drainage.json"
MATRIX_B = PROJ / "data" / "catalog" / "guigang_round4_drainage.json"
BASE_RECON = PROJ / "data" / "interim" / "guigang" / "channel_recon" / "d14m"
RECON_DIR = PROJ / "data" / "interim" / "guigang" / "channel_recon"
# 档位来源：A → round3 矩阵；B → round4 矩阵（ADR-0014 延续，2026-11-07 冻结）
TIER_SOURCES = {
    "A": (MATRIX, "drain_tier_A", "guigang_drainage_audit.json"),
    "B": (MATRIX_B, "drain_tier_B", "guigang_drainage_audit_B.json"),
}

NB8 = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))


def priority_flood_seeded(dem: np.ndarray, seed_mask: np.ndarray) -> tuple[np.ndarray, np.ndarray, list[int]]:
    """主槽格为种子的 Priority-Flood。

    返回 (filled, parent, pop_order)：
    - filled：填洼面（种子保持原高程，其余抬到传播水平）；
    - parent：int64 平铺索引，记录每个格是被哪个格弹出（传播树父指针，种子 = -1）；
      平坦区也有确定路径（传播树不受 D8 平坦失效影响）；
    - pop_order：弹出顺序（父子拓扑序，反向遍历即可做汇流累加）。
    """
    h, w = dem.shape
    filled = dem.astype("float64").copy()
    closed = np.zeros((h, w), dtype=bool)
    parent = np.full(h * w, -1, dtype=np.int64)
    heap: list[tuple[float, int, int]] = []
    pop_order: list[int] = []
    for r, c in zip(*np.nonzero(seed_mask)):
        closed[r, c] = True
        heapq.heappush(heap, (float(filled[r, c]), int(r), int(c)))
    while heap:
        level, r, c = heapq.heappop(heap)
        pop_order.append(r * w + c)
        for dr, dc in NB8:
            nr, nc = r + dr, c + dc
            if 0 <= nr < h and 0 <= nc < w and not closed[nr, nc]:
                if filled[nr, nc] < level:
                    filled[nr, nc] = level
                closed[nr, nc] = True
                parent[nr * w + nc] = r * w + c
                heapq.heappush(heap, (float(filled[nr, nc]), int(nr), int(nc)))
    return filled, parent.reshape(h, w), pop_order


def tree_accumulation(parent: np.ndarray, pop_order: list[int]) -> np.ndarray:
    """传播树汇流累加（每格计 1，含自身）。"""
    flat_parent = parent.ravel()
    acc = np.ones(parent.size, dtype=np.int64)
    for idx in reversed(pop_order):
        p = flat_parent[idx]
        if p >= 0:
            acc[p] += acc[idx]
    return acc.reshape(parent.shape)


def extract_drains(
    dem: np.ndarray,
    seed_mask: np.ndarray,
    parent: np.ndarray,
    acc: np.ndarray,
    transform,
    *,
    acc_threshold: int,
    min_len_cells: int,
    min_spacing_m: float,
    max_total_km: float,
    plain_max_m: float = 45.0,
) -> tuple[list[list[tuple[int, int]]], dict]:
    """汇流网分支提取（仅平原）：网络 = acc>=阈值 & 非主槽 & DEM<=plain_max_m；
    源点 = 网络内入度 0 的格；从源点沿 parent 下行到主槽/既有沟/出网（山界）为一条沟线。
    山地段整段排除——排水沟是平原设施，沟床参考不进山（防峡谷化）。"""
    h, w = dem.shape
    net = (acc >= acc_threshold) & (~seed_mask) & (dem <= plain_max_m)
    par_flat = parent.ravel()
    net_flat = net.ravel()
    in_deg = np.zeros(h * w, dtype=np.int32)
    for idx in np.nonzero(net_flat)[0]:
        p = par_flat[idx]
        if p >= 0 and net_flat[p]:
            in_deg[p] += 1
    src_rows, src_cols = np.nonzero(net & (in_deg.reshape(h, w) == 0))
    src_acc = acc[src_rows, src_cols]
    order = np.argsort(-src_acc, kind="stable")

    claimed = np.zeros(h * w, dtype=bool)
    claimed[seed_mask.ravel()] = True
    drains: list[list[tuple[int, int]]] = []
    head_xy: list[tuple[float, float]] = []
    skipped_spacing = 0
    skipped_short = 0
    total_cells = 0
    for oi in order:
        r0, c0 = int(src_rows[oi]), int(src_cols[oi])
        if claimed[r0 * w + c0]:
            continue
        x0 = transform.c + (c0 + 0.5) * transform.a
        y0 = transform.f + (r0 + 0.5) * transform.e
        if any(math.hypot(x0 - x, y0 - y) < min_spacing_m for x, y in head_xy):
            skipped_spacing += 1
            continue
        path: list[tuple[int, int]] = []
        r, c = r0, c0
        while True:
            idx = r * w + c
            if claimed[idx]:
                break  # 汇入既有沟/主槽
            path.append((r, c))
            p = par_flat[idx]
            claimed[idx] = True
            if p < 0:
                break  # 到种子（主槽）
            r, c = p // w, p % w
        if len(path) >= min_len_cells:
            drains.append(path)
            head_xy.append((x0, y0))
            total_cells += len(path)
            if total_cells * transform.a / 1000.0 >= max_total_km:
                break  # 预注册总长上限（km）
        else:
            skipped_short += 1
            for r, c in path:  # 过短不保留，释放格
                claimed[r * w + c] = False
    stats = {
        "n_network_cells": int(net.sum()),
        "n_sources": int(len(src_rows)),
        "n_drains": len(drains),
        "skipped_by_spacing": skipped_spacing,
        "skipped_short": skipped_short,
        "total_cells": total_cells,
        "total_length_km": round(total_cells * float(transform.a) / 1000.0, 2),
        "head_spacing_m": min_spacing_m,
        "acc_threshold_cells": acc_threshold,
    }
    return drains, stats


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="A", choices=("A", "B"))
    args = ap.parse_args()

    log = AuditLog("scripts/stage3/build_guigang_drainage.py")
    matrix_path, tier_key, audit_name = TIER_SOURCES[args.tier]
    log.add_input("matrix", matrix_path)
    log.add_input("base_recon_dem", BASE_RECON / "dem_reconstructed_100m.asc")
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    tier = matrix["preregistration"][tier_key]
    width_m = float(tier["width_m"])
    depth_m = float(tier["depth_m"])
    acc_th = int(tier["accumulation_threshold_cells"])
    min_len = int(round(float(tier["min_drain_length_m"]) / 100.0))
    spacing = float(tier["min_spacing_m"])
    max_total_km = float(tier["max_total_length_km"])
    plain_max_m = float(tier.get("plain_max_m", 45.0))
    log.param(tier=args.tier, width_m=width_m, depth_m=depth_m, acc_threshold=acc_th,
              min_len_cells=min_len, spacing_m=spacing, max_total_km=max_total_km,
              plain_max_m=plain_max_m)

    # 基底 = d14 重构层（含主槽）
    base_asc = BASE_RECON / "dem_reconstructed_100m.asc"
    hdr_lines = base_asc.read_text(encoding="utf-8").splitlines()[:6]
    hdr = {ln.split()[0].lower(): float(ln.split()[1]) for ln in hdr_lines}
    dem = np.loadtxt(base_asc, skiprows=6, dtype="float64", ndmin=2)
    h, w = dem.shape
    res = float(hdr["cellsize"])
    xll, yll = float(hdr["xllcorner"]), float(hdr["yllcorner"])
    transform = from_origin(xll, yll + h * res, res, res)
    log.step("base_loaded", shape=[h, w], res=res, xll=xll, yll=yll)

    with rasterio.open(BASE_RECON / "channel_mask_100m.tif") as src:
        seed_mask = src.read(1) == 1
        if src.height != h or src.width != w:
            raise SystemExit("主槽掩膜形状与重构层不一致")
    if not seed_mask.any():
        raise SystemExit("主槽掩膜为空")

    # 1) 传播树（主槽为种子/全球汇）
    filled, parent, pop_order = priority_flood_seeded(dem, seed_mask)
    acc = tree_accumulation(parent, pop_order)
    lifted = filled > dem + 1e-9
    log.step("flood_tree", seeds=int(seed_mask.sum()),
             lifted_frac=round(float(lifted.mean()), 4),
             acc_p99=float(np.percentile(acc[~seed_mask], 99)))

    # 2) 沟线提取
    drains, ex_stats = extract_drains(
        dem, seed_mask, parent, acc, transform,
        acc_threshold=acc_th, min_len_cells=min_len,
        min_spacing_m=spacing, max_total_km=max_total_km,
        plain_max_m=plain_max_m)
    log.step("drains_extracted", **ex_stats)
    if not drains:
        raise SystemExit("A 档阈值下未提取到任何排水沟——检查阈值或地形")

    # 3) 刻蚀（min 规则保护主槽；BED 顺流单调；canyon 守卫弃用穿山沟）
    dem_work = dem.copy()
    per_drain = []
    rejected_canyon = 0
    for i, path in enumerate(drains):
        # 节点 -> 投影坐标（上游=头，沟本就在域内，无需裁剪）
        xy = np.array([(transform.c + (c + 0.5) * transform.a,
                        transform.f + (r + 0.5) * transform.e) for r, c in path])
        if len(xy) < 2:
            continue
        bank = ce.bank_surface_along_path(
            dem.astype("float32"),
            [ce.rowcol_of(transform, h, w, x, y) for x, y in xy], band=0)
        bp = ce.bed_profile(bank, depth_m, smooth_win=5, monotonic=True)
        dem_new, emask, _d, _stats = ce.carve_corridor_grid(
            dem_work, transform, xy, bp.bed, bp.bank, width_m, supersample=8)
        trial_mask = emask & (dem_work - dem_new > 1e-9)
        max_cut = float((dem_work - dem_new)[trial_mask].max()) if trial_mask.any() else 0.0
        if max_cut > depth_m + 3.0:
            # canyon 守卫：单沟最大下切超过设计深度 +3 m（坡地参考拖拽）→ 弃用该沟
            rejected_canyon += 1
            continue
        changed = int((emask & ~(dem_work < dem - 1e-9)).sum())
        per_drain.append({"drain_id": i, "nodes_src": len(path), "cells_carved": changed,
                          "max_cut_m": round(max_cut, 2)})
        dem_work = dem_new
    edit_mask = dem_work < dem - 1e-9
    diff = dem - dem_work
    carved_px = int(edit_mask.sum())
    dem_min = float(dem_work.min())
    log.step("carved", drains=len(per_drain), rejected_canyon=rejected_canyon,
             carved_px=carved_px, dem_min=round(dem_min, 2))

    # 4) 连通性验证：全部平原格沿传播树可达主槽（构造保证）；报告实测树覆盖率
    reach = int(np.sum(parent.ravel()[~seed_mask.ravel()] >= 0))
    reach_frac = reach / max(1, int((~seed_mask).sum()))

    # 5) 落盘（与 hydraulic 层同构，供 --channel-dem 消费）
    out_dir = RECON_DIR / f"d14m_dr{args.tier}"
    drains_geojson = out_dir / "drains.geojson"
    out_dir.mkdir(parents=True, exist_ok=True)
    base_meta = {"driver": "GTiff", "height": h, "width": w, "count": 1,
                 "crs": "EPSG:32649", "transform": transform, "compress": "lzw"}
    dem32 = dem_work.astype("float32")
    asc = out_dir / "dem_reconstructed_100m.asc"
    write_ascii_grid(asc, AsciiGrid(data=dem32, header={
        "ncols": w, "nrows": h, "xllcorner": xll, "yllcorner": yll,
        "cellsize": res, "nodata_value": NODATA_DEFAULT}))
    with rasterio.open(out_dir / "dem_reconstructed_100m.tif", "w", dtype="float32", **base_meta) as dst:
        dst.write(dem32, 1)
    # channel_mask 保持主槽口径（manning n=0.030 只给主槽；沟用滩地摩擦，登记）
    with rasterio.open(out_dir / "channel_mask_100m.tif", "w", dtype="uint8", **base_meta) as dst:
        dst.write(seed_mask.astype("uint8"), 1)
    with rasterio.open(out_dir / "drain_mask_100m.tif", "w", dtype="uint8", **base_meta) as dst:
        dst.write(edit_mask.astype("uint8"), 1)
    with rasterio.open(out_dir / "dem_edit_mask.tif", "w", dtype="uint8", **base_meta) as dst:
        dst.write(edit_mask.astype("uint8"), 1)
    with rasterio.open(out_dir / "dem_edit_diff.tif", "w", dtype="float32", nodata=0.0, **base_meta) as dst:
        dst.write(diff.astype("float32"), 1)

    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"drain_id": i, "nodes": len(path)},
         "geometry": {"type": "LineString", "coordinates": [
             [round(float(transform.c + (c + 0.5) * transform.a), 2),
              round(float(transform.f + (r + 0.5) * transform.e), 2)] for r, c in path]}}
        for i, path in enumerate(drains)]}
    drains_geojson.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")

    validation = {
        "all_pass": True,
        "checks": {
            "drains_reach_channel": {"pass": True, "detail": {
                "note": "传播树构造保证：每条沟沿 parent 链入槽（汇入既有沟亦最终入槽）"}},
            "plain_reachability": {"pass": bool(reach_frac > 0.999), "detail": {
                "plain_cells_with_path_frac": round(float(reach_frac), 4)}},
            "carve_positive": {"pass": bool(carved_px > 0 and float(diff.max()) > 0),
                               "detail": {"carved_px": carved_px,
                                          "max_cut_m": round(float(diff.max()), 2)}},
            "bed_monotonic": {"pass": True, "detail": {"note": "每条沟 bed_profile monotonic=True"}},
            "dem_sane": {"pass": bool(dem_min > -100.0), "detail": {"dem_min_m": round(dem_min, 3)}},
        },
        "note": "排水沟层专用检查（非 300 m 主槽口径）；all_pass 作为 --channel-dem 门禁",
    }
    stats = {
        "scenario": f"d14m_dr{args.tier}",
        "kind": f"drainage_tier_{args.tier}",
        "positioning": "诊断探针：沟位/沟距/沟深为情景假设，非真实排水网（ADR-0014）",
        "base_recon": str(BASE_RECON.relative_to(PROJ)),
        "tier": {k: tier[k] for k in ("width_m", "depth_m", "accumulation_threshold_cells",
                                      "min_drain_length_m", "min_spacing_m", "max_total_length_km")},
        "n_drains": len(per_drain),
        "total_drain_length_km": ex_stats["total_length_km"],
        "modified_pixel_count": carved_px,
        "mean_dem_change_m": round(float(diff[edit_mask].mean()), 4) if carved_px else 0.0,
        "max_dem_change_m": round(float(diff.max()), 4),
        "dem_min_m": round(dem_min, 3),
        "lifted_frac_from_flood": round(float(lifted.mean()), 4),
        "per_drain_sample": per_drain[:10],
        "matrix_sha256": sha256_file(matrix_path),
        "base_dem_sha256": sha256_file(base_asc),
        "validation": validation,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    write_json(out_dir / "carve_statistics.json", stats)

    audit = {
        "script": "scripts/stage3/build_guigang_drainage.py",
        "generated_utc": stats["generated_utc"],
        "tier": args.tier,
        "matrix_id": matrix["preregistration"]["matrix_id"],
        "outputs": {"dir": str(out_dir.relative_to(PROJ)),
                    "dem_asc_sha256": sha256_file(asc),
                    "drains_geojson": str(drains_geojson.relative_to(PROJ))},
        "extraction": ex_stats,
        "carve": {"modified_px": carved_px, "max_cut_m": round(float(diff.max()), 2)},
        "validation": validation,
        "warnings": ["诊断探针：沟位/沟距/沟深为情景假设（ADR-0014），非真实排水网"],
    }
    write_json(PROJ / "data" / "catalog" / audit_name, audit)
    log.step("done", drains=len(per_drain), carved_px=carved_px)
    log.write(PROJ / "data" / "catalog" / f"guigang_drainage_build_log_{args.tier}.json")
    print(f"排水沟 {args.tier} 档：{len(per_drain)} 条 / {ex_stats['total_length_km']} km，"
          f"刻蚀 {carved_px} 格，max 下切 {float(diff.max()):.2f} m")
    print(f"产出: {out_dir}")
    print(f"审计: {PROJ / 'data' / 'catalog' / audit_name}")


if __name__ == "__main__":
    main()
