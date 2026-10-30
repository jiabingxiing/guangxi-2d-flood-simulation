"""生成固定验证抽样索引（入库，只存行列号，不存数值）。

计划 §4 要求 `data/splits/` 是「固定验证抽样索引（入库）」：发布保真回归必须用**同一批**
抽样点，才能判断「产品变了」还是「数据变了」。

抽样口径（每 run 每帧，确定性随机，种子 = case/run/frame 的稳定哈希）：

| 类别 | 上限 | 选取规则 |
|---|---:|---|
| `random`   | 24 | 有效像元（非 NaN、非海域）中随机 |
| `boundary` | 40 | 干湿边界：`depth` 在阈值 0.05 m 两侧相邻的像元对（取湿侧） |
| `nodata`   | 10 | NaN 与有效像元相邻处（取有效侧） |
| `max`      | 1  | 该帧 `depth` 最大值像元 |

另加 run 级：
- `max_envelope`: `res.max` 的最大值像元（1 个）
- `max_envelope_wet`: `res.max` 中 ≥ 最高阈值的像元（最多 5 个，验证极端值）

索引只含行/列，数值由测试运行时从源 `.wd` 与发布包数值块分别读取比较。

用法：
    uv run python scripts/make_sampling_index.py [--seed 20261027]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from floodviz.config import load_config
from floodviz.quality.frames import list_frame_files
from floodviz.raster.grid import read_native_frame, read_sea_mask, valid_mask

ROOT = Path(__file__).resolve().parents[1]


def _seed_for(*parts: object) -> int:
    """把任意标识映射成稳定整数种子（不用内置 hash，避免 PYTHONHASHSEED 影响）。"""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def _pairs(depth: np.ndarray, threshold: float, valid: np.ndarray) -> list[tuple[int, int]]:
    """干湿边界像元对：取湿侧像元（其 4 邻域里至少有 1 个非湿的）。"""
    wet = valid & (depth > np.float32(threshold))
    non_wet = valid & ~wet
    rows, cols = depth.shape
    out: list[tuple[int, int]] = []
    for row, col in np.argwhere(wet):
        r0, r1 = max(0, row - 1), min(rows, row + 2)
        c0, c1 = max(0, col - 1), min(cols, col + 2)
        if non_wet[r0:r1, c0:c1].any():
            out.append((int(row), int(col)))
    return out


def _sample(items: list[tuple[int, int]], count: int, rng: np.random.Generator) -> list[list[int]]:
    if not items:
        return []
    take = min(count, len(items))
    idx = rng.choice(len(items), size=take, replace=False)
    return [[int(items[i][0]), int(items[i][1])] for i in idx]


def build_index(config, *, seed: int) -> dict:
    cases_doc: dict = {}
    for case_id, case in config.cases.items():
        runs_doc: dict = {}
        for run in case.runs.values():
            results = run.source_dir / "results"
            if not results.is_dir():
                continue
            header_shape = None
            frames_out: list[dict] = []
            for seq, path in list_frame_files(run.source_dir):
                depth = read_native_frame(path)
                if header_shape is None:
                    header_shape = depth.shape
                sea = read_sea_mask(run.source_dir, depth.shape)
                valid = valid_mask(depth, sea)
                frame_id = f"f{seq:04d}"
                rng = np.random.default_rng(_seed_for(seed, case_id, run.run_id, frame_id))

                valid_idx = np.flatnonzero(valid.ravel())
                random_points: list[list[int]] = []
                if valid_idx.size:
                    take = min(24, valid_idx.size)
                    chosen = rng.choice(valid_idx, size=take, replace=False)
                    rows, cols = np.unravel_index(chosen, depth.shape)
                    random_points = [[int(r), int(c)] for r, c in zip(rows, cols, strict=True)]

                nodata_items: list[tuple[int, int]] = []
                nan_mask = np.isnan(depth)
                if nan_mask.any() and valid.any():
                    # NaN 邻域里的有效像元（取有效侧）
                    dilated = np.zeros_like(nan_mask)
                    dilated[1:, :] |= nan_mask[:-1, :]
                    dilated[:-1, :] |= nan_mask[1:, :]
                    dilated[:, 1:] |= nan_mask[:, :-1]
                    dilated[:, :-1] |= nan_mask[:, 1:]
                    nodata_items = [(int(r), int(c)) for r, c in np.argwhere(dilated & valid)]

                boundary_items = _pairs(depth, config.default_threshold_m, valid)
                if valid.any():
                    hottest = float(np.nanmax(depth))
                    hot_mask = depth == np.float32(hottest)
                    max_point = [[int(r), int(c)] for r, c in np.argwhere(hot_mask)[:1]]
                else:
                    max_point = []

                frames_out.append(
                    {
                        "frame_id": frame_id,
                        "sim_seconds": seq * run.save_interval_s,
                        "random": random_points,
                        "boundary": _sample(boundary_items, 40, rng),
                        "nodata": _sample(nodata_items, 10, rng),
                        "max": max_point,
                        "valid_cell_count": int(valid.sum()),
                        "wet_cell_count": int((valid & (depth > np.float32(config.default_threshold_m))).sum()),
                    }
                )

            envelope_doc = None
            max_path = results / "res.max"
            if max_path.is_file() and header_shape is not None:
                max_depth = read_native_frame(max_path)
                sea = read_sea_mask(run.source_dir, max_depth.shape)
                valid = valid_mask(max_depth, sea)
                rng = np.random.default_rng(_seed_for(seed, case_id, run.run_id, "max"))
                highest = float(np.nanmax(max_depth)) if np.isfinite(max_depth).any() else 0.0
                hot = [[int(r), int(c)] for r, c in np.argwhere(max_depth == np.float32(highest))[:5]]
                top_threshold = float(config.thresholds_m[-1])
                high_items = [(int(r), int(c)) for r, c in np.argwhere(valid & (max_depth > np.float32(top_threshold)))]
                envelope_doc = {
                    "max_depth_m": highest,
                    "max_pixels": hot,
                    "above_top_threshold": _sample(high_items, 5, rng),
                    "top_threshold_m": top_threshold,
                }

            runs_doc[run.run_id] = {
                "split_interval_s": float(run.save_interval_s),
                "frame_count": len(frames_out),
                "frames": frames_out,
                "max_envelope": envelope_doc,
            }
        if runs_doc:
            cases_doc[case_id] = runs_doc
    return {
        "schema_version": "1.0",
        "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "seed": seed,
        "note": (
            "固定验证抽样索引：只含行列号，不含数值。发布保真回归必须用本索引，"
            "数值由测试从源 .wd 与发布包数值块分别读取比较。"
        ),
        "threshold_m": float(config.default_threshold_m),
        "cases": cases_doc,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成固定验证抽样索引")
    parser.add_argument("--config", default=str(ROOT / "configs" / "default.yaml"))
    parser.add_argument("--seed", type=int, default=20261027)
    parser.add_argument("--out", default=str(ROOT / "data" / "splits" / "sampling_index.json"))
    args = parser.parse_args(argv)

    config = load_config(args.config)
    doc = build_index(config, seed=args.seed)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    total = sum(
        len(f["random"]) + len(f["boundary"]) + len(f["nodata"]) + len(f["max"])
        for runs in doc["cases"].values()
        for run in runs.values()
        for f in run["frames"]
    )
    print(f"[sampling] {out} ({out.stat().st_size / 1024:.0f} KB)，固定抽样点 {total} 个")
    for case_id, runs in doc["cases"].items():
        for run_id, run in runs.items():
            per_frame = sum(
                len(f["random"]) + len(f["boundary"]) + len(f["nodata"]) + len(f["max"])
                for f in run["frames"]
            )
            print(f"  {case_id}/{run_id}: {run['frame_count']} 帧，{per_frame} 点")
    return 0


if __name__ == "__main__":
    sys.exit(main())
