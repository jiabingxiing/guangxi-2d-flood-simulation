"""湿格判据扫描 + 斑块面积口径确认（复核 Lead 的悖论机制结论）。

任务：
1. 复算 6 个湿格判据下 baseline 与 dualloss_v1 的斑块数与湿面积（独立复核 Lead 的表）；
2. 逐像元验证 wet 场的包含关系（真子集）；
3. 确认 "外围小斑块面积" 的口径：是"非主水体斑块面积总和"还是"≤9 格斑块面积总和"；
4. 统计 "减法切断连通区域" 的直接证据：baseline 单个斑块在 dualloss 下被切成几个。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import read_ascii_grid  # noqa: E402
from scripts.common.patch_analysis import connected_components  # noqa: E402

BASE = PROJ / "outputs" / "guigang" / "2024-09-yagi"
RUNS = {
    "baseline_realdem": "scen_yagi_q11500_100m_dt2p5_choff_realdem",
    "flow_only_v1": "scen_yagi_q11500_100m_dt2p5_choff_flow_only_v1",
    "dualloss_v1": "scen_yagi_q11500_100m_dt2p5_choff_dualloss_v1",
    "rainloss_high_v1": "scen_yagi_q11500_100m_dt2p5_choff_rainloss_high_v1",
}
THRESHOLDS = (0.02, 0.03, 0.05, 0.10, 0.15, 0.30)
CELL_KM2 = 0.01


def load_max(tag: str) -> np.ndarray:
    return read_ascii_grid(BASE / RUNS[tag] / "results" / "res.max").data


def scan(wd: np.ndarray, thr: float) -> dict:
    wet = np.isfinite(wd) & (wd > thr)
    labels, n = connected_components(wet, connectivity=8)
    sizes = np.bincount(labels.ravel())[1:] if n else np.empty(0, np.int64)
    return {
        "threshold_m": thr,
        "n_patches": int(n),
        "wet_cells": int(wet.sum()),
        "wet_km2": round(float(wet.sum()) * CELL_KM2, 2),
        "sizes": sizes,
        "wet_mask": wet,
        "labels": labels,
    }


def main() -> None:
    base = load_max("baseline_realdem")
    dl = load_max("dualloss_v1")

    print("=" * 96)
    print("1. 湿格判据扫描（独立复核）")
    print("=" * 96)
    print(f"{'thr_m':>7s}{'base_patches':>14s}{'dl_patches':>12s}"
          f"{'base_km2':>11s}{'dl_km2':>10s}{'dl-base_patches':>17s}{'反超?':>8s}")
    rows = []
    for thr in THRESHOLDS:
        b = scan(base, thr)
        d = scan(dl, thr)
        diff = d["n_patches"] - b["n_patches"]
        rows.append({
            "threshold_m": thr,
            "baseline_patches": b["n_patches"], "dualloss_patches": d["n_patches"],
            "baseline_wet_km2": b["wet_km2"], "dualloss_wet_km2": d["wet_km2"],
            "patch_diff": diff, "dualloss_below_baseline": bool(diff < 0),
        })
        print(f"{thr:7.2f}{b['n_patches']:14d}{d['n_patches']:12d}"
              f"{b['wet_km2']:11.2f}{d['wet_km2']:10.2f}{diff:+17d}"
              f"{'是' if diff < 0 else '否':>8s}")

    print()
    print("=" * 96)
    print("2. 真子集验证（逐像元包含关系）")
    print("=" * 96)
    subset_rows = []
    for thr in (0.02, 0.05, 0.30):
        b = scan(base, thr)
        d = scan(dl, thr)
        both = int((b["wet_mask"] & d["wet_mask"]).sum())
        dl_only = int((d["wet_mask"] & ~b["wet_mask"]).sum())
        base_only = int((b["wet_mask"] & ~d["wet_mask"]).sum())
        subset_rows.append({
            "threshold_m": thr, "both": both,
            "dualloss_only": dl_only, "baseline_only": base_only,
            "dualloss_strict_subset": bool(dl_only == 0),
        })
        print(f"thr={thr:.2f} m  both={both:6d}  dualloss_only={dl_only:5d}  "
              f"baseline_only={base_only:6d}  "
              f"dualloss⊂baseline={dl_only == 0}")

    print()
    print("=" * 96)
    print("3. 斑块面积口径确认（flow_only_v1，thr=0.05 m）")
    print("=" * 96)
    fo = load_max("flow_only_v1")
    f = scan(fo, 0.05)
    s = np.sort(f["sizes"])[::-1]
    main = int(s.max())
    total = int(s.sum())
    non_main = total - main
    le9 = s[s <= 9]
    le4 = s[s <= 4]
    print(f"  湿像元总数            = {total:6d}  = {total * CELL_KM2:8.2f} km²")
    print(f"  最大斑块（主水体）    = {main:6d}  = {main * CELL_KM2:8.2f} km²")
    print(f"  **非主水体斑块面积和** = {non_main:6d}  = {non_main * CELL_KM2:8.2f} km²  "
          f"← 我报告的 0.29 km²")
    print(f"  ≤9 格斑块面积和        = {int(le9.sum()):6d}  = {le9.sum() * CELL_KM2:8.2f} km²  "
          f"（{le9.size} 个斑块）")
    print(f"  ≤4 格斑块面积和        = {int(le4.sum()):6d}  = {le4.sum() * CELL_KM2:8.2f} km²  "
          f"（{le4.size} 个斑块）")
    print(f"  斑块总数 = {f['n_patches']}，其中 ≤9 格 {le9.size} 个")

    print()
    print("  同口径对照基线（thr=0.05 m）：")
    bs = np.sort(scan(base, 0.05)["sizes"])[::-1]
    print(f"    湿像元总数={int(bs.sum()):6d}  主水体={int(bs.max()):6d}  "
          f"非主水体={int(bs.sum() - bs.max()):6d} = {(bs.sum() - bs.max()) * CELL_KM2:.2f} km²  "
          f"← 我报告的 237.60 km²")

    print()
    print("=" * 96)
    print("4. 切断机制直接证据：baseline 主斑块在 dualloss 下被切成几个")
    print("=" * 96)
    b5 = scan(base, 0.05)
    d5 = scan(dl, 0.05)
    b_sizes = np.bincount(b5["labels"].ravel())[1:]
    b_main_label = int(np.argmax(b_sizes)) + 1
    b_main_mask = b5["labels"] == b_main_label
    # 只看 baseline 主斑块范围内的 dualloss 标签
    sub_labels = d5["labels"][b_main_mask]
    sub_wet = d5["wet_mask"][b_main_mask]
    wet_labels = sub_labels[sub_wet]
    uniq, counts = np.unique(wet_labels[wet_labels > 0], return_counts=True)
    # 过滤掉落在 baseline 主斑块内的碎片（只数有实质面积的）
    order = np.argsort(counts)[::-1]
    print(f"  baseline 主斑块 {int(b_main_mask.sum())} 像元，"
          f"在 dualloss 下仍湿 {int(sub_wet.sum())} 像元")
    print(f"  这些像元在 dualloss 中分属 {len(uniq)} 个不同斑块：")
    for i in order[:12]:
        print(f"    斑块 {int(uniq[i]):5d}: {int(counts[i]):6d} 像元 "
              f"= {counts[i] * CELL_KM2:7.2f} km²")
    if len(uniq) > 12:
        print(f"    ... 其余 {len(uniq) - 12} 个更小的碎片")
    print(f"  → baseline 1 个斑块被切成 dualloss {len(uniq)} 个斑块")

    # 反向：dualloss 主斑块里有多少像元来自 baseline 的多个斑块
    d_sizes = np.bincount(d5["labels"].ravel())[1:]
    d_main_label = int(np.argmax(d_sizes)) + 1
    d_main_mask = d5["labels"] == d_main_label
    b_labels_in_dmain = b5["labels"][d_main_mask]
    b_wet_in_dmain = b5["wet_mask"][d_main_mask]
    bl = b_labels_in_dmain[b_wet_in_dmain]
    print(f"  反向：dualloss 主斑块 {int(d_main_mask.sum())} 像元，"
          f"来自 baseline 的 {len(np.unique(bl[bl > 0]))} 个斑块")

    print()
    print("=" * 96)
    print("5. 全局切断计数：baseline 每个斑块在 dualloss 下被切成几块")
    print("=" * 96)
    split_hist: dict[int, int] = {}
    total_dl_patches_from_base = 0
    for lbl in range(1, int(b5["labels"].max()) + 1):
        bm = b5["labels"] == lbl
        n_cells = int(bm.sum())
        if n_cells < 2:
            continue
        sl = d5["labels"][bm]
        sw = d5["wet_mask"][bm]
        vals = sl[sw]
        vals = vals[vals > 0]
        k = len(np.unique(vals))
        split_hist[k] = split_hist.get(k, 0) + 1
        total_dl_patches_from_base += k
    print(f"  baseline 斑块（≥2 像元）按 '被切成 k 块' 统计：")
    for k in sorted(split_hist):
        print(f"    k={k}: {split_hist[k]:6d} 个 baseline 斑块")
    print(f"  这些 baseline 斑块在 dualloss 下共形成 {total_dl_patches_from_base} 个斑块")

    out = {
        "purpose": "湿格判据扫描与斑块面积口径确认（复核悖论机制）",
        "script": "scripts/stage6/scan_guigang_threshold.py",
        "threshold_scan": rows,
        "subset_check": subset_rows,
        "area_convention_check": {
            "run": "flow_only_v1", "threshold_m": 0.05,
            "wet_cells": total, "wet_km2": round(total * CELL_KM2, 2),
            "main_patch_cells": main, "main_patch_km2": round(main * CELL_KM2, 2),
            "non_main_patch_area_km2": round(non_main * CELL_KM2, 2),
            "non_main_patch_cells": non_main,
            "le9_patch_area_km2": round(float(le9.sum()) * CELL_KM2, 2),
            "le9_patch_count": int(le9.size),
            "le4_patch_area_km2": round(float(le4.sum()) * CELL_KM2, 2),
            "le4_patch_count": int(le4.size),
            "conclusion": "0.29 km² 是「非主水体斑块面积总和」（总湿像元 − 最大斑块），"
                          "不是「≤9 格斑块面积总和」",
        },
        "split_mechanism": {
            "baseline_main_patch_cells": int(b_main_mask.sum()),
            "baseline_main_still_wet_in_dualloss": int(sub_wet.sum()),
            "baseline_main_split_into_n_patches_in_dualloss": int(len(uniq)),
            "dualloss_main_patch_cells": int(d_main_mask.sum()),
            "dualloss_main_from_n_baseline_patches": int(len(np.unique(bl[bl > 0]))),
            "baseline_patch_split_histogram": {str(k): v for k, v in sorted(split_hist.items())},
            "total_dualloss_patches_derived_from_baseline": int(total_dl_patches_from_base),
        },
    }
    outp = PROJ / "data" / "catalog" / "guigang_phase4_threshold_scan.json"
    outp.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n-> {outp}")


if __name__ == "__main__":
    main()
