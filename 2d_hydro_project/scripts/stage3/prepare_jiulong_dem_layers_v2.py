"""久隆四层 DEM 契约 v2：负值区 GLO-30 基准对齐融合修补 + LAND/SEA/NODATA 三掩膜。

计划 §4.4 / M1 后续（ADR-0022 登记）。对 v1 契约
（`prepare_jiulong_dem_layers.py`，contract jiulong_dem_layers_v1，输出树
`data/interim/jiulong/dem_layers/` 冻结不动）的增补：

1. **负值区融合修补（弃 BFS 平推口径）**：v1 对「来源未闭合」的负值
   （约 312.52 km²，诊断报告 §2.2）不做任何修复；v2 引入 Copernicus GLO-30
   （公开下载，`data/raw/dem_source/glo30/`），在陆域重叠区做**基准对齐**
   （median(raw − glo30)）后，对未解析负值像元以对齐值替换，reason=3。
   填挖体积记账并与「域内洪峰蓄量 3%」上限核对（计划 §4.4 微坑平滑上限口径，
   融合体积单独记账、超限即拒绝）。
2. **LAND/SEA/NODATA 三掩膜**：sea = S1 常水 ∩ 域边 4 连通（G16 口径，
   与 v1 sea_msl_anchor 同源）；land = valid ∧ ¬sea；nodata = ¬valid。
   三掩膜独立落盘（npy + tif），废弃「值域考古式」的海面判定。
   海面负值仍按 ADR-0004 锚 0（模型内部基准约定），但该约定随 sea 掩膜
   与 reason=2 显式携带，不再是隐式钳零。
3. DEM_HYDRO = 选择性微坑填洼（沿用 v1 预注册策略 max_raise 1.0 m /
   max_cells 9）；DEM_HYDRAULIC = REPAIRED（保留真实洼地）；S1 常水保护
   硬检查沿用（抬高即抛 LayerError）。

证据等级：GLO-30 为公开 30 m 产品，垂直基准 EGM2008；对齐用中位数偏移，
不闭合 G7（相对量口径不变）。
"""

from __future__ import annotations

import json
import sys
from argparse import ArgumentParser
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import (  # noqa: E402
    AuditLog,
    add_common_args,
    guard_no_overwrite,
    sha256_file,
    utc_now,
    write_json,
)
from scripts.stage3.prepare_jiulong_dem_layers import (  # noqa: E402
    DEFAULT_OUT,
    NODATA,
    REASON_NONE,
    REASON_NAMES,
    REASON_RESAMPLE_ARTEFACT,
    REASON_SEA_MSL_ANCHOR,
    SELECTIVE_FILL_POLICY,
    LayerError,
    edge_connected,
    model_grid,
    read_source_resampled,
    selective_fill,
    s1_water_on_grid,
)

GLO30_DIR = PROJ / "data" / "raw" / "dem_source" / "glo30"
SOURCE_DEM = PROJ / "data" / "raw" / "dem_source" / "广西.tif"
BASELINE_RECESSION_MASS = (
    PROJ / "outputs/jiulong/2024-09-yagi/scen_free_200m_sv6h_recession/results/res.mass"
)
OUT_ROOT = PROJ / "data" / "interim" / "jiulong" / "dem_layers_v2"
CATALOG = PROJ / "data" / "catalog" / "jiulong_dem_layers_v2.json"
EPSG = 32649

REASON_GLO30_FUSION = 3
REASON_NAMES_V2 = {
    **REASON_NAMES,
    REASON_GLO30_FUSION: "glo30_datum_aligned_fusion",
}
FUSION_VOLUME_CAP_FRACTION = 0.03  # 计划 §4.4：填挖体积 < 域内洪峰蓄量 3%


def glo30_on_grid(grid: dict[str, float]) -> np.ndarray:
    """GLO-30 瓦片重采样（average）到模型网格。"""
    shape = (int(grid["nrows"]), int(grid["ncols"]))
    canvas = np.full(shape, np.nan, dtype="float32")
    tiles = sorted(GLO30_DIR.glob("*.tif"))
    if not tiles:
        raise FileNotFoundError(f"GLO-30 瓦片缺失：{GLO30_DIR}")
    transform = rasterio.Affine(
        grid["cellsize"], 0.0, grid["xllcorner"],
        0.0, -grid["cellsize"], grid["north"],
    )
    for tile in tiles:
        with rasterio.open(tile) as src:
            band = np.full(shape, np.nan, dtype="float32")
            reproject(
                rasterio.band(src, 1), band,
                dst_transform=transform, dst_crs=f"EPSG:{EPSG}",
                dst_nodata=np.nan, resampling=Resampling.average,
            )
            ok = np.isfinite(band) & (band > -9999)
            canvas[ok] = band[ok]
    return canvas.astype("float64")


def peak_storage_m3() -> float | None:
    """域内洪峰蓄量参考：基线退水 run res.mass 的 Vol 列最大值。"""
    if not BASELINE_RECESSION_MASS.exists():
        return None
    lines = [ln for ln in BASELINE_RECESSION_MASS.read_text(
        encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    cols = lines[0].split()
    try:
        vol_i = cols.index("Vol")
    except ValueError:
        return None
    vals = []
    for ln in lines[1:]:
        parts = ln.split()
        try:
            vals.append(float(parts[vol_i]))
        except (ValueError, IndexError):
            continue
    return max(vals) if vals else None


def datum_offset(raw: np.ndarray, glo30: np.ndarray, land: np.ndarray) -> dict:
    """基准对齐偏移：陆域重叠区 median(raw − glo30)。"""
    overlap = land & np.isfinite(glo30) & (raw > 0)
    if int(overlap.sum()) < 50:
        raise LayerError(
            f"陆域重叠样本不足（{int(overlap.sum())} 格），基准对齐不可靠"
        )
    diffs = raw[overlap] - glo30[overlap]
    return {
        "offset_m": float(np.median(diffs)),
        "n_overlap_cells": int(overlap.sum()),
        "mad_m": float(np.median(np.abs(diffs - np.median(diffs)))),
        "p10_m": float(np.percentile(diffs, 10)),
        "p90_m": float(np.percentile(diffs, 90)),
    }


def fuse_negatives(
    repaired: np.ndarray,
    reason: np.ndarray,
    glo30_aligned: np.ndarray,
    valid: np.ndarray,
    sea: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, dict]:
    """对未解析负值像元做 GLO-30 融合（reason=3），返回新 REPAIRED/reason/账目。

    融合后仍为负的像元（GLO-30 对齐值本身 < 0 的近岸带）保留证据值并照常
    标记 reason=3：不钳零、不隐藏，交由三掩膜与海锚规则界定。
    """
    target = valid & (repaired < 0) & (reason == REASON_NONE) & np.isfinite(glo30_aligned)
    fused = repaired.copy()
    fused[target] = glo30_aligned[target]
    reason = reason.copy()
    reason[target] = REASON_GLO30_FUSION
    still_negative = target & (fused < 0)
    ledger = {
        "fused_cells": int(target.sum()),
        "fill_volume_m3": float(np.sum(glo30_aligned[target] - repaired[target])) if target.any() else 0.0,
        "dig_volume_m3": float(np.sum(np.minimum(glo30_aligned[target] - repaired[target], 0.0))) if target.any() else 0.0,
        "max_raise_m": float(np.max(glo30_aligned[target] - repaired[target])) if target.any() else 0.0,
        "still_negative_cells": int(still_negative.sum()),
        "method": "replace with datum-aligned GLO-30 (median offset, land overlap)",
    }
    return fused, reason, ledger


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__)
    add_common_args(parser)
    parser.add_argument("--res", type=float, default=200.0)
    parser.add_argument("--negative-repair", choices=("glo30", "none"), default="glo30")
    args = parser.parse_args(argv)
    log = AuditLog("prepare_jiulong_dem_layers_v2", run_tag=args.run_tag, dry_run=args.dry_run)
    log.add_input_dir("glo30", GLO30_DIR, pattern="*.tif")
    log.add_input("source_dem", SOURCE_DEM)
    (PROJ / "logs" / "stage3").mkdir(parents=True, exist_ok=True)

    grid = model_grid(float(args.res))
    shape = (int(grid["nrows"]), int(grid["ncols"]))
    cell_area = float(grid["cellsize"]) ** 2
    raw = read_source_resampled(SOURCE_DEM, grid, Resampling.bilinear)
    block_mean = read_source_resampled(SOURCE_DEM, grid, Resampling.average)
    valid = np.isfinite(raw) & (raw > NODATA / 2)
    s1_water = s1_water_on_grid(grid)
    sea = edge_connected(s1_water) & valid
    land = valid & ~sea
    nodata_mask = ~valid
    log.step("masks", sea_cells=int(sea.sum()), land_cells=int(land.sum()),
             nodata_cells=int(nodata_mask.sum()))

    glo30 = glo30_on_grid(grid)
    off = datum_offset(raw, glo30, land)
    glo30_aligned = glo30 + off["offset_m"]
    log.step("datum_offset", **off)

    # v1 两规则（欠冲、海锚）+ v2 融合
    repaired = np.where(valid, raw, NODATA)
    reason = np.zeros(shape, dtype=np.uint8)
    artefact = valid & (raw < 0) & np.isfinite(block_mean) & (block_mean >= 0)
    repaired = np.where(artefact, block_mean, repaired)
    reason[artefact] = REASON_RESAMPLE_ARTEFACT
    anchor = valid & (repaired < 0) & sea & (reason == REASON_NONE)
    repaired = np.where(anchor, 0.0, repaired)
    reason[anchor] = REASON_SEA_MSL_ANCHOR

    fusion_ledger: dict = {"mode": args.negative_repair, "fused_cells": 0,
                           "fill_volume_m3": 0.0, "cap_reference": None,
                           "cap_fraction_used": None, "cap_ok": None}
    if args.negative_repair == "glo30":
        repaired, reason, fusion_ledger = fuse_negatives(
            repaired, reason, glo30_aligned, valid, sea
        )
        fusion_ledger["fused_area_km2"] = round(
            fusion_ledger["fused_cells"] * cell_area / 1e6, 4
        )
        peak = peak_storage_m3()
        fusion_ledger["cap_reference"] = {
            "peak_storage_m3": peak,
            "source": str(BASELINE_RECESSION_MASS.relative_to(PROJ)).replace(chr(92), "/"),
            "cap_fraction": FUSION_VOLUME_CAP_FRACTION,
        }
        if peak:
            frac = abs(fusion_ledger["fill_volume_m3"]) / max(peak, 1.0)
            fusion_ledger["cap_fraction_used"] = round(frac, 6)
            fusion_ledger["cap_ok"] = bool(frac < FUSION_VOLUME_CAP_FRACTION)
            if not fusion_ledger["cap_ok"]:
                log.error(
                    f"融合体积 {fusion_ledger['fill_volume_m3']:.1f} m³ 超过洪峰蓄量 3% 上限"
                )
                return 1
        log.step("fusion", **fusion_ledger)

    # 未解析负值余量（融合后）
    residual_neg = valid & (repaired < 0) & (reason == REASON_NONE)

    # HYDRO：选择性微坑填洼（S1 保护沿用）；HYDRAULIC：REPAIRED（保留洼地）
    sel = selective_fill(repaired, valid=valid, protect=s1_water,
                         policy=SELECTIVE_FILL_POLICY, cell_area_m2=cell_area)
    hydro = np.where(valid, sel.filled, NODATA)
    hydraulic = repaired.copy()
    if int(np.count_nonzero((sel.raised > 1e-9) & s1_water)):
        raise LayerError("DEM_HYDRO 抬高了 S1 常水像元，违反真实洼地保护约束")
    if not np.allclose(hydraulic[valid], repaired[valid]):
        raise LayerError("DEM_HYDRAULIC 必须等于 REPAIRED（保留真实洼地）")

    layers = {
        "DEM_RAW": np.where(valid, raw, NODATA),
        "DEM_REPAIRED": repaired,
        "DEM_HYDRO": hydro,
        "DEM_HYDRAULIC": hydraulic,
    }

    outputs: dict[str, str] = {}
    if not args.dry_run:
        OUT_ROOT.mkdir(parents=True, exist_ok=True)
        for name, data in layers.items():
            layer_dir = OUT_ROOT / name.lower()
            guard_no_overwrite(layer_dir)
            layer_dir.mkdir(parents=True, exist_ok=True)
            tif = layer_dir / f"{name.lower()}.tif"
            profile = {
                "driver": "GTiff", "height": shape[0], "width": shape[1],
                "count": 1, "dtype": "float32", "crs": f"EPSG:{EPSG}",
                "transform": rasterio.Affine(
                    grid["cellsize"], 0.0, grid["xllcorner"],
                    0.0, -grid["cellsize"], grid["north"]),
                "compress": "lzw", "nodata": NODATA,
            }
            with rasterio.open(tif, "w", **profile) as dst:
                dst.write(data.astype("float32"), 1)
            write_json(layer_dir / "meta.json", {
                "layer": name, "contract": "jiulong_dem_layers_v2",
                "generated_utc": utc_now(), "run_tag": args.run_tag,
                "sha256": sha256_file(tif),
                "operations": {
                    "DEM_RAW": ["resample_bilinear"],
                    "DEM_REPAIRED": ["resample_undershoot", "sea_msl_anchor",
                                     *(["glo30_datum_aligned_fusion"] if args.negative_repair == "glo30" else [])],
                    "DEM_HYDRO": ["selective_micro_depression_fill"],
                    "DEM_HYDRAULIC": ["none (depressions preserved)"],
                }[name],
            })
            outputs[name] = str(tif.relative_to(PROJ)).replace(chr(92), "/")
        mask_dir = OUT_ROOT / "masks"
        guard_no_overwrite(mask_dir)
        mask_dir.mkdir(parents=True, exist_ok=True)
        for mname, mdata in (("land", land), ("sea", sea), ("nodata", nodata_mask)):
            np.save(mask_dir / f"{mname}_mask.npy", mdata)
            mpath = mask_dir / f"{mname}_mask.tif"
            profile = {
                "driver": "GTiff", "height": shape[0], "width": shape[1],
                "count": 1, "dtype": "uint8", "crs": f"EPSG:{EPSG}",
                "transform": rasterio.Affine(
                    grid["cellsize"], 0.0, grid["xllcorner"],
                    0.0, -grid["cellsize"], grid["north"]),
                "compress": "lzw",
            }
            with rasterio.open(mpath, "w", **profile) as dst:
                dst.write(mdata.astype("uint8"), 1)
            outputs[f"mask_{mname}"] = str(mpath.relative_to(PROJ)).replace(chr(92), "/")

    catalog = {
        "case": "jiulong",
        "contract_version": "jiulong_dem_layers_v2",
        "parent_contract": "jiulong_dem_layers_v1",
        "generated_utc": utc_now(),
        "run_tag": args.run_tag,
        "dry_run": args.dry_run,
        "grid": {k: grid[k] for k in ("ncols", "nrows", "xllcorner", "north", "cellsize")},
        "negative_repair": {
            "mode": args.negative_repair,
            "reason_code": REASON_GLO30_FUSION,
            "reason_name": REASON_NAMES_V2[REASON_GLO30_FUSION],
            "glo30_source": str(GLO30_DIR.relative_to(PROJ)).replace(chr(92), "/"),
            "datum_offset": off,
            "ledger": fusion_ledger,
            "bfs_superseded": True,
            "note": "计划 §4.4：弃「纯 BFS 邻域平推」，改 GLO-30 基准对齐融合；体积记账并受 3% 上限约束",
        },
        "residual_unresolved_negatives": {
            "cells": int(residual_neg.sum()),
            "area_km2": round(float(residual_neg.sum()) * cell_area / 1e6, 4),
        },
        "masks": {
            "land_cells": int(land.sum()),
            "sea_cells": int(sea.sum()),
            "nodata_cells": int(nodata_mask.sum()),
            "sea_rule": "S1 常水 ∩ 域边 4 连通（G16 口径，与 v1 sea_msl_anchor 同源）",
            "note": "三掩膜独立落盘；海面负值锚 0 为 ADR-0004 模型内部基准约定（显式携带，非隐式钳零）",
        },
        "hydro_policy": SELECTIVE_FILL_POLICY,
        "contract_checks": {
            "s1_water_never_raised_in_hydro": True,
            "hydraulic_equals_repaired": True,
            "fusion_only_on_unresolved_negatives": True,
        },
        "outputs": outputs,
    }
    if not args.dry_run:
        write_json(CATALOG, catalog)
    tag = f".{args.run_tag}" if args.run_tag else ""
    log.write(PROJ / "logs" / "stage3" / f"prepare_jiulong_dem_layers_v2{tag}.json")
    print(
        f"v2: fused={fusion_ledger['fused_cells']} cells "
        f"vol={fusion_ledger['fill_volume_m3']:.1f} m3 "
        f"cap_ok={fusion_ledger.get('cap_ok')} "
        f"residual_neg={int(residual_neg.sum())} cells"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
