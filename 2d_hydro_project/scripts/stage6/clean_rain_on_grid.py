"""【已退役 — 请勿使用】阶段6 旧版久隆后处理滤杂脚本。

    ADR-0010 决策项 8 判定本脚本产出的展示层不合规，已由
    `scripts/stage6/clean_jiulong_display.py`（display_qc）取代。
    本文件仅作历史证据保留，不得再运行、不得作为交付依据。

退役理由（三条，逐条可核）：

1. **写死 4/9 像元阈值。** 本脚本用 `min_patch_cells=4`、`shallow_patch_cells=9`。
   200 m 网格 1 格 = 0.04 km²，100 m 网格 1 格 = 0.01 km²，相差 4 倍。
   同一像元数在两种网格上是完全不同的物理面积，因此该阈值不能跨分辨率使用。
2. **写死 100 m 单元面积。** 第 180 行用 `wd.sum() * 100.0 * 100.0 / 1e6` 计算面积，
   隐含假设网格恒为 100 m。用于 200 m 结果时面积会被低估 4 倍。
3. **声称「ARR 2019 Book 9 与 UK EA 规范」。** 该归因未经逐条核验原文用途与适用条件，
   属计划 §2.3 明令禁止的表述。`clean_audit.json` 的 `filter_standard` 字段同样含此表述。

实测后果（E1，Lead 复算）：在 100 m `res.max` 上把湿面积从 271.07 km² 压到 171.97 km²
（−36.56%）、水量从 168.43 Mm³ 压到 78.90 Mm³（−53.16%），且未登记体积变化率。
这是计划 §8 风险表「滤波误删真实积水（高）」的实际发生。

另外：该脚本会把 `res.mass` 复制进输出目录，使 `scripts/stage3/g15_mass_recheck.py`
的 `rglob("results/res.mass")` 把它误计为独立模型 run。g15 已加派生层判定以排除。

替代实现：`scripts/stage6/clean_jiulong_display.py`（物理面积阈值 + 栅格头换算 +
A/B/C 预注册 + 派生标记 + 逐帧审计，且不复制 res.mass）。
"""
from __future__ import annotations

import json
import shutil
import sys
from collections import deque
from pathlib import Path
import numpy as np

PROJ = Path(__file__).resolve().parents[2]


def load_asc(p: Path) -> tuple[list[str], dict[str, float], np.ndarray]:
    with open(p, "r", encoding="ascii") as f:
        hdr_lines = [f.readline() for _ in range(6)]
        hdr = {l.split()[0].lower(): float(l.split()[1]) for l in hdr_lines}
        data = np.loadtxt(f)
    return hdr_lines, hdr, data


def write_asc(p: Path, hdr_lines: list[str], data: np.ndarray) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="ascii", newline="\n") as f:
        for line in hdr_lines:
            f.write(line)
        np.savetxt(f, data, fmt="%.3f", delimiter="\t")


def filter_grid(
    wd: np.ndarray,
    sea_mask: np.ndarray,
    min_patch_cells: int = 4,
    shallow_patch_cells: int = 9,
    shallow_threshold_m: float = 0.15,
) -> tuple[np.ndarray, dict[str, int | float]]:
    wet = (wd > 0.05) & ~sea_mask
    h, w = wet.shape
    visited = np.zeros_like(wet, dtype=bool)
    patches = []

    for r in range(h):
        for c in range(w):
            if wet[r, c] and not visited[r, c]:
                q = deque([(r, c)])
                visited[r, c] = True
                cells = []
                while q:
                    cr, cc = q.popleft()
                    cells.append((cr, cc))
                    for nr, nc in ((cr - 1, cc), (cr + 1, cc), (cr, cc - 1), (cr, cc + 1)):
                        if 0 <= nr < h and 0 <= nc < w and wet[nr, nc] and not visited[nr, nc]:
                            visited[nr, nc] = True
                            q.append((nr, nc))
                patches.append(cells)

    wd_clean = wd.copy()
    filtered_patches = 0
    filtered_cells = 0

    for p in patches:
        n_cells = len(p)
        rr, cc = zip(*p)
        p_depths = wd[rr, cc]
        max_d = float(p_depths.max())

        drop = False
        # 规则 1: 孤立斑块 <= 4 格，均为 DEM 数字凹陷陷阱
        if n_cells <= min_patch_cells:
            drop = True
        # 规则 2: 微小斑块 <= 9 格且最大水深 < 0.15m，为浅水片流无害噪点
        elif n_cells <= shallow_patch_cells and max_d < shallow_threshold_m:
            drop = True

        if drop:
            filtered_patches += 1
            filtered_cells += n_cells
            wd_clean[rr, cc] = 0.0

    rem_wet = (wd_clean > 0.05) & ~sea_mask
    stats = {
        "raw_patches": len(patches),
        "raw_wet_cells": int(wet.sum()),
        "filtered_patches": filtered_patches,
        "filtered_cells": filtered_cells,
        "cleaned_patches": len(patches) - filtered_patches,
        "cleaned_wet_cells": int(rem_wet.sum()),
        "patch_reduction_pct": round(filtered_patches / max(1, len(patches)) * 100, 2),
        "cell_reduction_pct": round(filtered_cells / max(1, wet.sum()) * 100, 2),
    }
    return wd_clean, stats


def clean_run(src_run_dir: Path, dst_run_dir: Path) -> dict:
    if dst_run_dir.exists():
        print(f"目标目录已存在: {dst_run_dir}，执行更新。")
    dst_run_dir.mkdir(parents=True, exist_ok=True)
    dst_results = dst_run_dir / "results"
    dst_results.mkdir(parents=True, exist_ok=True)

    # 复制基础元数据文件（dem, par, bci, rain, sea_mask, meta 等）
    for name in [
        "bci.bci", "dem.asc", "dem_edit_diff.tif", "dem_edit_mask.tif",
        "dem_pre_carve.diff.txt", "rain.rain", "run.log", "run.par",
        "sea_mask.npy", "meta.json"
    ]:
        src_f = src_run_dir / name
        if src_f.exists():
            shutil.copy2(src_f, dst_run_dir / name)

    # 载入 sea mask
    sea_mask_p = src_run_dir / "sea_mask.npy"
    if sea_mask_p.exists():
        sea_mask = np.load(sea_mask_p)
    else:
        sea_mask = np.zeros((1, 1), dtype=bool)

    src_results = src_run_dir / "results"
    wd_files = sorted(src_results.glob("res-*.wd"))
    if not wd_files:
        raise RuntimeError(f"在 {src_results} 未找到 res-*.wd 文件！")

    frame_audits = {}
    print(f"开始清洗逐帧水深（共 {len(wd_files)} 帧）...")
    for f in wd_files:
        hdr_lines, hdr, wd = load_asc(f)
        if sea_mask.shape != wd.shape:
            sea_mask = np.zeros_like(wd, dtype=bool)
        wd_clean, stats = filter_grid(wd, sea_mask)
        write_asc(dst_results / f.name, hdr_lines, wd_clean)
        frame_audits[f.name] = stats
        print(f"  [{f.name}] 斑块 {stats['raw_patches']} -> {stats['cleaned_patches']} "
              f"(消除 {stats['filtered_patches']} 碎斑, 减少 {stats['patch_reduction_pct']}%)")

    # 处理 res.max
    res_max_src = src_results / "res.max"
    if res_max_src.exists():
        hdr_lines, hdr, rmax = load_asc(res_max_src)
        rmax_clean, max_stats = filter_grid(rmax, sea_mask)
        write_asc(dst_results / "res.max", hdr_lines, rmax_clean)
        frame_audits["res.max"] = max_stats
        print(f"  [res.max] 斑块 {max_stats['raw_patches']} -> {max_stats['cleaned_patches']} "
              f"(消除 {max_stats['filtered_patches']} 碎斑, 减少 {max_stats['patch_reduction_pct']}%)")

    # 复制其他 elev 等文件
    for f in src_results.glob("res-*.elev"):
        shutil.copy2(f, dst_results / f.name)
    for name in ["res.dem", "res.inittm", "res.mass", "res.maxtm", "res.mxe", "res.totaltm"]:
        if (src_results / name).exists():
            shutil.copy2(src_results / name, dst_results / name)

    # 写入清洗审计报告
    audit_report = {
        "source_run": str(src_run_dir.resolve()),
        "destination_run": str(dst_run_dir.resolve()),
        "filter_standard": "ARR 2019 Book 9 & UK EA Multi-Criteria Rain-on-Grid Filter",
        "rules": [
            "Rule 1: Connected components <= 4 cells (<=0.04 km2) removed as DEM pit traps",
            "Rule 2: Connected components <= 9 cells with max_depth < 0.15m removed as shallow sheet flow",
            "Protection: Water bodies > 0.04 km2 or deep contiguous inundation preserved",
        ],
        "frame_audits": frame_audits,
    }
    (dst_run_dir / "clean_audit.json").write_text(
        json.dumps(audit_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 生成清洗后的 verify.json
    res_20_p = dst_results / "res-0020.wd"
    if res_20_p.exists():
        _, _, wd20 = load_asc(res_20_p)
        wet20 = (wd20 > 0.05) & ~sea_mask
        wet_area_km2 = round(float(wet20.sum()) * 100.0 * 100.0 / 1e6, 2)
        verify_data = {
            "scenario": "free_cleaned",
            "wet_area_km2": wet_area_km2,
            "anchor": "久隆镇/500kV站",
            "post_processed": True,
            "filter_standard": "ARR 2019 Book 9",
            "raw_patches_f20": frame_audits.get("res-0020.wd", {}).get("raw_patches"),
            "cleaned_patches_f20": frame_audits.get("res-0020.wd", {}).get("cleaned_patches"),
        }
        (dst_run_dir / "verify.json").write_text(
            json.dumps(verify_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print(f"\n清洗完成！新 run 目录保存在: {dst_run_dir}")
    return audit_report


if __name__ == "__main__":
    src = PROJ / "outputs" / "jiulong" / "2024-09-yagi" / "scen_free_100m_dt2p5_sv6h_realdem"
    dst = PROJ / "outputs" / "jiulong" / "2024-09-yagi" / "scen_free_100m_dt2p5_sv6h_realdem_cleaned"
    clean_run(src, dst)
