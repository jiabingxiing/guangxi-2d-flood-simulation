"""贵港成因归因产品（计划 T03 / 成功标准 S2；ADR-0015 §3 口径）。

规则（`data/catalog/guigang_genesis_rules.json`）**先冻结后计算**：脚本拒绝在
规则文件缺失或未标 frozen 时运行；改规则必须重新冻结并留痕（ADR-0015）。

- 判据：event-max（res.max）> 0.05 m（ADR-0006 湿判据）；
- 0 never-wet ｜ 1 river-origin（full 湿 ∧ rain_off 湿）｜
  2 pluvial-origin（full 湿 ∧ rain_off 干）｜
  3 edge-band（域边界 1 格环带内任一 run 湿，优先标记）｜
  4 disagreement（full 干 ∧ rain_off 湿，水量再分配，单独统计）；
- 已注册混淆项：full=d14 河槽+入渗 3.0 mm/h，rain_off=d10 河槽无雨——
  结论只称「有雨/无雨情景差分归因」，不得称外洪/内涝精确分解。

产出：`outputs/products/guigang/genesis/<run_tag 或 rules 指纹>/origin_class.tif`
+ `data/catalog/guigang_genesis_products.json`（审计）。只读 run 目录，
不调用 lisflood.exe。

用法（先设 `$env:PYTHONUTF8="1"`）::

    uv --directory 2d_hydro_project run python scripts/stage6/make_genesis_attribution.py --dry-run
    uv --directory 2d_hydro_project run python scripts/stage6/make_genesis_attribution.py
"""
from __future__ import annotations

import argparse
import json
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
    sha256_file,
    utc_now,
    write_json,
)
from scripts.common.paths import rel_posix  # noqa: E402

CLASS_NAMES = {
    0: "never_wet",
    1: "river_origin",
    2: "pluvial_origin",
    3: "edge_band",
    4: "disagreement",
}
DEFAULT_RULES = PROJ / "data" / "catalog" / "guigang_genesis_rules.json"
EVIDENCE_LEVEL = "E2（情景差分派生，非实测；含已注册混淆项）"


def _rel(path: Path) -> str:
    """审计记录用项目相对路径；项目外（测试 tmp）退回原样。"""
    try:
        return rel_posix(path)
    except Exception:
        return str(path)


def load_rules(path: Path) -> dict[str, Any]:
    """读规则文件；未冻结或缺关键字段 fail-closed（ADR-0015 纪律）。"""
    if not path.is_file():
        raise FileNotFoundError(
            f"归因规则文件不存在：{path}。ADR-0015 要求先冻结后计算；"
            "请先按 ADR-0015 §3 冻结规则再运行本脚本。"
        )
    rules = json.loads(path.read_text(encoding="utf-8"))
    if not rules.get("frozen"):
        raise ValueError(f"{path} 未标记 frozen=true；规则未冻结，拒绝计算")
    for key in ("full_run", "rain_off_run", "wet_threshold_m"):
        if key not in rules:
            raise ValueError(f"{path} 缺关键字段 {key}")
    return rules


def classify(
    full_max: np.ndarray,
    rainoff_max: np.ndarray,
    *,
    wet_thr: float = 0.05,
    edge_band_cells: int = 1,
) -> np.ndarray:
    """四分支 + 边界环带分类（uint8 0–4）。输入两 run 的 event-max 同形状。"""
    if full_max.shape != rainoff_max.shape:
        raise GridError(
            f"两 run 网格形状不一致：full {full_max.shape} vs rain_off {rainoff_max.shape}；"
            "归因差分必须同网格"
        )
    full_wet = full_max > np.float32(wet_thr)
    off_wet = rainoff_max > np.float32(wet_thr)
    cls = np.zeros(full_max.shape, dtype=np.uint8)
    cls[full_wet & off_wet] = 1
    cls[full_wet & ~off_wet] = 2
    cls[~full_wet & off_wet] = 4
    if edge_band_cells > 0:
        nrows, ncols = full_max.shape
        band = np.zeros((nrows, ncols), dtype=bool)
        for k in range(edge_band_cells):
            band[k, :] = True
            band[nrows - 1 - k, :] = True
            band[:, k] = True
            band[:, ncols - 1 - k] = True
        cls[band & (full_wet | off_wet)] = 3
    return cls


def class_stats(cls: np.ndarray, cell_area_m2: float) -> dict[str, Any]:
    """逐类像元数与面积（km²）。"""
    out: dict[str, Any] = {}
    for code, name in CLASS_NAMES.items():
        n = int((cls == code).sum())
        out[name] = {"code": code, "cells": n,
                     "area_km2": round(n * cell_area_m2 / 1e6, 3)}
    return out


def write_origin_tif(path: Path, cls: np.ndarray, hdr: dict[str, float], epsg: int) -> Path:
    """写 uint8 分类 GeoTIFF（nodata=255）。"""
    import rasterio

    path.parent.mkdir(parents=True, exist_ok=True)
    cell = hdr["cellsize"]
    with rasterio.open(
        path, "w", driver="GTiff", height=cls.shape[0], width=cls.shape[1], count=1,
        dtype="uint8", crs=f"EPSG:{epsg}",
        transform=rasterio.Affine(cell, 0, hdr["xllcorner"], 0, -cell,
                                  hdr["yllcorner"] + cls.shape[0] * cell),
        compress="deflate", nodata=255,
    ) as dst:
        dst.write(cls, 1)
    dst_meta = {
        "class_codes": {str(k): v for k, v in CLASS_NAMES.items()},
        "note": "情景差分归因（ADR-0015 §3）；非外洪/内涝精确分解",
    }
    write_json(path.with_suffix(".tif.meta.json"), dst_meta)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES,
                        help="已冻结规则 JSON（默认 data/catalog/guigang_genesis_rules.json）")
    parser.add_argument("--run-tag", default=None,
                        help="输出子目录后缀（默认取规则文件的 sha256 前 8 位）")
    from scripts.common.grid_io import add_common_args

    add_common_args(parser)
    args = parser.parse_args()

    rules = load_rules(args.rules)
    log = AuditLog("make_genesis_attribution.py", run_tag=args.run_tag, dry_run=args.dry_run)
    log.add_input("rules", args.rules)
    log.param(rules_sha256=sha256_file(args.rules), wet_thr=rules["wet_threshold_m"])

    full_dir = PROJ / rules["full_run"]
    off_dir = PROJ / rules["rain_off_run"]
    for label, d in (("full", full_dir), ("rain_off", off_dir)):
        if not (d / "results" / "res.max").is_file():
            raise FileNotFoundError(f"{label} run 缺 results/res.max：{d}")
        log.add_input(f"{label}_resmax", d / "results" / "res.max")
    full_grid = read_ascii_grid(full_dir / "results" / "res.max", dtype=np.float32)
    off_grid = read_ascii_grid(off_dir / "results" / "res.max", dtype=np.float32)
    cls = classify(full_grid.data, off_grid.data,
                   wet_thr=float(rules["wet_threshold_m"]),
                   edge_band_cells=int(rules.get("edge_band_cells", 1)))
    stats = class_stats(cls, full_grid.cell_area_m2)
    log.step("classified", stats=stats)

    epsg = 32649
    tag = args.run_tag or sha256_file(args.rules)[:8]
    out_dir = args.out_dir or (PROJ / "outputs" / "products" / "guigang" / "genesis" / tag)
    audit = {
        "script": "make_genesis_attribution.py",
        "generated_utc": utc_now(),
        "evidence_level": EVIDENCE_LEVEL,
        "rules_path": _rel(args.rules),
        "rules_sha256": sha256_file(args.rules),
        "full_run": _rel(full_dir),
        "rain_off_run": _rel(off_dir),
        "wet_threshold_m": rules["wet_threshold_m"],
        "basis": rules.get("basis"),
        "confounder_note": rules.get("confounder_note"),
        "prohibited_readings": rules.get("prohibited_readings", []),
        "class_stats": stats,
        "output_dir": _rel(out_dir),
        "dry_run": bool(args.dry_run),
    }
    if not args.dry_run:
        tif = write_origin_tif(Path(out_dir) / "origin_class.tif",
                               cls, full_grid.header, epsg)
        audit["outputs"] = [_rel(tif), _rel(tif.with_suffix(".tif.meta.json"))]
        log.step("written", output_dir=_rel(out_dir))
    out_json = args.log_json or (PROJ / "data" / "catalog" / "guigang_genesis_products.json")
    log.payload.update(audit)  # 审计字段并入单一审计件（原子写一次）
    log.write(out_json)
    print(json.dumps({"class_stats": stats, "out": str(out_json)}, ensure_ascii=False))
    print("->", out_json if args.dry_run else out_dir)


if __name__ == "__main__":
    main()
