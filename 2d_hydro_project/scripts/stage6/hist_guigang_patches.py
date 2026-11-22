"""Phase 2+4 斑块尺寸直方图（res.max 复合场 + 末端帧），用于判断小斑块的真实归属。"""
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
    "baseline": "scen_yagi_q11500_100m_dt2p5_choff_realdem",
    "flow_only": "scen_yagi_q11500_100m_dt2p5_choff_flow_only_v1",
    "dualloss": "scen_yagi_q11500_100m_dt2p5_choff_dualloss_v1",
    "rainhigh": "scen_yagi_q11500_100m_dt2p5_choff_rainloss_high_v1",
}
BINS = [(1, 1), (2, 4), (5, 9), (10, 99), (100, 999), (1000, 10**9)]
HEADERS = ["1cell", "2-4", "5-9", "10-99", "100-999", ">=1000"]


def hist(name: str) -> str:
    wd = read_ascii_grid(BASE / RUNS[name] / "results" / "res.max").data
    return _hist_row(wd)


def _hist_row(wd: np.ndarray) -> str:
    wet = np.isfinite(wd) & (wd > 0.05)
    lab, n = connected_components(wet, connectivity=8)
    s = np.bincount(lab.ravel())[1:] if n else np.empty(0, np.int64)
    cols = []
    for lo, hi in BINS:
        cols.append(int(((s >= lo) & (s <= hi)).sum()))
    return n, cols, int(s.sum())


def main() -> None:
    print("=" * 108)
    print("res.max 复合场 斑块尺寸直方图")
    print("=" * 108)
    print(f"{'run':12s}{'total':>7s}" + "".join(f"{h:>9s}" for h in HEADERS) + f"{'sum_cells':>11s}")
    for name in RUNS:
        n, cols, tot = hist(name)
        print(f"{name:12s}{n:7d}" + "".join(f"{c:9d}" for c in cols) + f"{tot:11d}")

    print()
    print("=" * 108)
    print("末端帧 seq=56 (t=336h) 斑块尺寸直方图")
    print("=" * 108)
    print(f"{'run':12s}{'total':>7s}" + "".join(f"{h:>9s}" for h in HEADERS) + f"{'sum_cells':>11s}")
    for name in RUNS:
        wd = read_ascii_grid(BASE / RUNS[name] / "results" / "res-0056.wd").data
        n, cols, tot = _hist_row(wd)
        print(f"{name:12s}{n:7d}" + "".join(f"{c:9d}" for c in cols) + f"{tot:11d}")

    print()
    print("=" * 108)
    print("单像元斑块（孤岛）数：离散水点的最严格口径")
    print("=" * 108)
    for name in RUNS:
        wd = read_ascii_grid(BASE / RUNS[name] / "results" / "res.max").data
        wet = np.isfinite(wd) & (wd > 0.05)
        lab, n = connected_components(wet, connectivity=8)
        s = np.bincount(lab.ravel())[1:] if n else np.empty(0, np.int64)
        n1 = int((s == 1).sum())
        print(f"{name:12s} 单像元斑块={n1:6d}  占斑块总数 {100.0 * n1 / max(n, 1):5.1f}%  "
              f"占湿像元 {100.0 * n1 / max(int(wet.sum()), 1):5.2f}%  "
              f"（1 像元 = 0.01 km²）")


if __name__ == "__main__":
    main()
