"""Phase 2+4 深度诊断：为什么 loss 情景的斑块数不降反升？

核心疑问：计划假设「离散水点由无损失降雨造成」，但 res.max 复合场上
dualloss_v1 / rainloss_high_v1 的斑块数（6,041 / 6,167）**高于**基线（4,422）。
本脚本给出机制证据，不做结论包装。

检查项：
1. 逐帧斑块数序列（判断是否为某一时段集中出现）
2. 斑块面积分布（loss 情景新增斑块是大还是小）
3. 主水体连通性（最大斑块占比）
4. 基线 vs loss 情景的湿像元集合关系（是否嵌套）
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
TAGS = {
    "baseline_realdem": "scen_yagi_q11500_100m_dt2p5_choff_realdem",
    "flow_only_v1": "scen_yagi_q11500_100m_dt2p5_choff_flow_only_v1",
    "dualloss_v1": "scen_yagi_q11500_100m_dt2p5_choff_dualloss_v1",
    "rainloss_high_v1": "scen_yagi_q11500_100m_dt2p5_choff_rainloss_high_v1",
}
WET = 0.05


def load(tag: str, name: str):
    return read_ascii_grid(BASE / TAGS[tag] / "results" / name)


def patch_sizes(wd: np.ndarray) -> np.ndarray:
    wet = np.isfinite(wd) & (wd > WET)
    labels, n = connected_components(wet, connectivity=8)
    if n == 0:
        return np.empty(0, np.int64)
    return np.bincount(labels.ravel())[1:]


def main() -> None:
    print("=" * 100)
    print("A. 逐帧斑块数序列（谁在何时产生斑块）")
    print("=" * 100)
    print(f"{'seq':>4s}{'t_h':>6s}" + "".join(f"{t[:17]:>19s}" for t in TAGS))
    frames = {}
    for tag in TAGS:
        frames[tag] = {}
    for seq in (6, 12, 16, 18, 20, 24, 30, 36, 42, 48, 56):
        row = f"{seq:4d}{seq * 6:6d}"
        for tag in TAGS:
            wd = load(tag, f"res-{seq:04d}.wd").data
            wet = np.isfinite(wd) & (wd > WET)
            labels, n = connected_components(wet, connectivity=8)
            sizes = np.bincount(labels.ravel())[1:] if n else np.empty(0, np.int64)
            frames[tag][seq] = (int(wet.sum()), int(n), sizes)
            row += f"{int(wet.sum()):8d}/{n:<9d}"
        print(row)

    print()
    print("=" * 100)
    print("B. res.max 复合场斑块面积分布（新增斑块有多大）")
    print("=" * 100)
    for tag in TAGS:
        sizes = patch_sizes(load(tag, "res.max").data)
        if sizes.size == 0:
            print(f"{tag:22s} 无斑块")
            continue
        km2 = sizes * 0.01
        print(f"{tag:22s} n={sizes.size:6d}  "
              f"p50={np.percentile(km2, 50):8.4f} km2  p90={np.percentile(km2, 90):8.4f}  "
              f"max={km2.max():9.2f}  sum={km2.sum():9.2f} km2  "
              f"n_ge_1km2={int((km2 >= 1.0).sum()):5d}  n_lt_0.1km2={int((km2 < 0.1).sum()):6d}")

    print()
    print("=" * 100)
    print("C. res.max 复合场湿像元集合关系（嵌套还是交叉）")
    print("=" * 100)
    bwet = load("baseline_realdem", "res.max").data > WET
    print(f"{'tag':22s}{'wet':>8s}{'&base':>9s}{'only_self':>11s}{'base_only':>11s}{'nesting':>22s}")
    for tag in TAGS:
        w = load(tag, "res.max").data > WET
        both = int((w & bwet).sum())
        only_self = int((w & ~bwet).sum())
        base_only = int((~w & bwet).sum())
        nest = "⊂ baseline" if only_self == 0 else (
            "⊃ baseline" if base_only == 0 else "交叉")
        print(f"{tag:22s}{int(w.sum()):8d}{both:9d}{only_self:11d}{base_only:11d}{nest:>22s}")

    print()
    print("=" * 100)
    print("D. 主水体（最大斑块）与外围小斑块拆分")
    print("=" * 100)
    for tag in TAGS:
        wd = load(tag, "res.max").data
        wet = np.isfinite(wd) & (wd > WET)
        labels, n = connected_components(wet, connectivity=8)
        if n == 0:
            continue
        sizes = np.bincount(labels.ravel())[1:]
        main = int(sizes.max())
        rest = int(sizes.sum() - main)
        # 外围斑块的水深
        main_label = int(np.argmax(sizes)) + 1
        fringe = wet & (labels != main_label)
        fd = wd[fringe] if fringe.any() else np.empty(0)
        print(f"{tag:22s} 主斑块={main * 0.01:8.2f} km2  外围={rest * 0.01:8.2f} km2 "
              f"({int((sizes > 1).sum() - 1):5d} 个外围斑块)"
              + (f"  外围水深 p50={np.percentile(fd, 50):.3f} p90={np.percentile(fd, 90):.3f} m"
                 if fd.size else ""))

    print()
    print("=" * 100)
    print("E. 基线 vs dualloss_v1 帧级差异（loss 情景是否消除了基线的离散水点）")
    print("=" * 100)
    print(f"{'seq':>4s}{'t_h':>6s}{'base':>8s}{'dl':>8s}{'both':>8s}"
          f"{'base_only':>10s}{'dl_only':>9s}{'removed%':>10s}")
    for seq in (12, 16, 18, 20, 24, 36, 48, 56):
        bw = load("baseline_realdem", f"res-{seq:04d}.wd").data > WET
        dw = load("dualloss_v1", f"res-{seq:04d}.wd").data > WET
        both = int((bw & dw).sum())
        bo = int((bw & ~dw).sum())
        do = int((dw & ~bw).sum())
        print(f"{seq:4d}{seq * 6:6d}{int(bw.sum()):8d}{int(dw.sum()):8d}{both:8d}"
              f"{bo:10d}{do:9d}{100.0 * bo / max(int(bw.sum()), 1):10.1f}")

    print()
    print("=" * 100)
    print("F. 结论性数据：res.max 斑块数与主水体占比")
    print("=" * 100)
    d = json.loads((PROJ / "data" / "catalog" / "guigang_phase4_scenarios.json")
                   .read_text(encoding="utf-8"))
    for tag in TAGS:
        m = d["runs"][tag]["res_max"]
        print(f"{tag:22s} patches={m['n_patches']:6d}  max_patch_share={m['max_patch_share_pct']:6.1f}%  "
              f"wet={m['wet_area_km2']:8.2f} km2  n_le9={m['n_le9_cells']:6d}")


if __name__ == "__main__":
    main()
