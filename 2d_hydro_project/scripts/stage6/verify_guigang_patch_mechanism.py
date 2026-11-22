"""验证「斑块数悖论」的机制假说。

假说：loss 情景的斑块数不降反升，不是因为「产生了新的水」，而是因为
**主水体（郁江漫滩）的湿面积被削减后，原本与主水体连成一片的滩地被切断成孤岛**。
证据链要求：
  1. 所有情景的湿像元都是基线的**真子集**（不存在「新增水」）——已由 C 表证明；
  2. 基线的主水体（最大斑块）在 loss 情景里被削掉的边缘像元，正是外围斑块的来源；
  3. 斑块数上升伴随**主斑块面积下降**（543.97 vs 614.96 km²）；
  4. 主水体萎缩量与外围斑块面积增量方向一致。

本脚本量化第 2、4 条：把基线主水体的像元，按 loss 情景下的归属分类。
"""
from __future__ import annotations

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
    "dualloss_v1": "scen_yagi_q11500_100m_dt2p5_choff_dualloss_v1",
    "rainloss_high_v1": "scen_yagi_q11500_100m_dt2p5_choff_rainloss_high_v1",
}
WET = 0.05


def main() -> None:
    print("=" * 100)
    print("假说验证：基线主水体在 loss 情景下的归属")
    print("=" * 100)

    bwd = read_ascii_grid(BASE / RUNS["baseline_realdem"] / "results" / "res.max").data
    bwet = np.isfinite(bwd) & (bwd > WET)
    blab, bn = connected_components(bwet, connectivity=8)
    bsizes = np.bincount(blab.ravel())[1:]
    bmain_label = int(np.argmax(bsizes)) + 1
    bmain = blab == bmain_label
    print(f"基线主水体: {int(bmain.sum())} 像元 = {bmain.sum() * 0.01:.2f} km²")
    print(f"基线主水体占基线湿像元: {100.0 * bmain.sum() / bwet.sum():.1f}%")

    for tag in ("dualloss_v1", "rainloss_high_v1"):
        wd = read_ascii_grid(BASE / RUNS[tag] / "results" / "res.max").data
        wet = np.isfinite(wd) & (wd > WET)
        lab, n = connected_components(wet, connectivity=8)
        sizes = np.bincount(lab.ravel())[1:]
        main_label = int(np.argmax(sizes)) + 1
        main = lab == main_label

        print()
        print("-" * 100)
        print(f"{tag}")
        print("-" * 100)
        # 基线主水体的像元，在 loss 情景里如何归属
        still_wet = wet & bmain
        still_in_main = main & bmain
        dried = bmain & ~wet
        # 仍湿但不在 loss 主水体里 → 被切出去的滩地
        orphan = still_wet & ~main
        print(f"  基线主水体 {int(bmain.sum()):6d} 像元中：")
        print(f"    仍湿且留在 {tag} 主水体 : {int(still_in_main.sum()):6d} "
              f"({100.0 * still_in_main.sum() / bmain.sum():5.1f}%)")
        print(f"    仍湿但被切到外围小斑块 : {int(orphan.sum()):6d} "
              f"({100.0 * orphan.sum() / bmain.sum():5.1f}%)")
        print(f"    完全干掉（不再湿）     : {int(dried.sum()):6d} "
              f"({100.0 * dried.sum() / bmain.sum():5.1f}%)")
        # 外围斑块中有多少落在基线主水体范围内
        fringe = wet & ~main
        fringe_from_bmain = int((fringe & bmain).sum())
        print(f"  该情景外围斑块共 {int(fringe.sum()):6d} 像元，其中 "
              f"{fringe_from_bmain} ({100.0 * fringe_from_bmain / max(int(fringe.sum()), 1):.1f}%) "
              f"落在基线主水体范围内")
        print(f"  主斑块面积: {sizes.max() * 0.01:.2f} km² "
              f"(基线 {bsizes.max() * 0.01:.2f} km²，"
              f"差 {(sizes.max() - bsizes.max()) * 0.01:+.2f} km²)")
        print(f"  外围斑块面积合计: {fringe.sum() * 0.01:.2f} km²，"
              f"斑块数 {int((sizes > 1).sum() - 1)}")

    print()
    print("=" * 100)
    print("对照：flow_only_v1 同一分解（纯来水，无雨）")
    print("=" * 100)
    wd = read_ascii_grid(BASE / "scen_yagi_q11500_100m_dt2p5_choff_flow_only_v1"
                         / "results" / "res.max").data
    wet = np.isfinite(wd) & (wd > WET)
    lab, n = connected_components(wet, connectivity=8)
    sizes = np.bincount(lab.ravel())[1:]
    main = lab == (int(np.argmax(sizes)) + 1)
    print(f"  主斑块 {sizes.max() * 0.01:.2f} km²，外围 {(sizes.sum() - sizes.max()) * 0.01:.2f} km²，"
          f"斑块数 {n}")
    print(f"  基线主水体在 flow_only 下仍湿的比例: "
          f"{100.0 * (wet & bmain).sum() / bmain.sum():.1f}%")


if __name__ == "__main__":
    main()
