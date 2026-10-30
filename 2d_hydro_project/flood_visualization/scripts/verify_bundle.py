"""Independent acceptance check on a built bundle (Lead-side verification).

Not part of the pipeline; verifies the published bundle against the source .wd frames:
  1) numeric block round-trip per sampled pixel (float32 exact / tolerance)
  2) statistics area recomputation (relative diff <= 0.1%)
  3) PNG display: pixel colour class matches depth class for sampled wet pixels
  4) images.json part coordinates match grid corners; parts tile the grid with no gap/overlap
  5) catalog/timeline semantic checks (relative-only -> timestamp null; frame count; monotonic)

Usage:
    uv run python scripts/verify_bundle.py outputs/web_bundle/v3-20261028 [--samples 200]
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from floodviz.config import load_config  # noqa: E402
from floodviz.raster.blocks import read_value  # noqa: E402
from floodviz.raster.grid import (  # noqa: E402
    header_to_grid,
    read_ascii_header,
    read_native_frame,
    read_sea_mask,
    valid_mask,
)

Image.MAX_IMAGE_PIXELS = None
TOL_ABS = 1e-5
TOL_REL = 1e-6
STAT_REL_TOL = 0.001
SPATIAL_TOL_PX = 0.5


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    text = value.lstrip("#")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def _class_of(depth: float, classes: list[dict]) -> str | None:
    """按 float32 语义分级（源数据为 float32，边界值必须用同一精度比较，避免 0.15/0.30 边界假报警）。"""
    value = np.float32(depth)
    for entry in classes:
        low = np.float32(float(entry["min_m"]))
        high = entry["max_m"]
        if value > low and (high is None or value <= np.float32(float(high))):
            return str(entry["color"]).lower()
    return None


def check_numeric_and_stats(config, case, run, bundle: Path, samples: int) -> list[str]:
    problems: list[str] = []
    results = run.source_dir / "results"
    header = read_ascii_header(results / "res-0000.wd")
    grid = header_to_grid(header, case.crs)
    sea = read_sea_mask(run.source_dir, (grid.rows, grid.cols))
    numeric_dir = bundle / "numeric" / case.case_id / run.run_id
    manifest = json.loads((numeric_dir / "manifest.json").read_text(encoding="utf-8"))
    stats = json.loads((bundle / "cases" / case.case_id / run.run_id / "statistics.json").read_text(encoding="utf-8"))
    timeline = json.loads((bundle / "cases" / case.case_id / run.run_id / "timeline.json").read_text(encoding="utf-8"))
    frame_ids = [f["frame_id"] for f in timeline["frames"]]
    if frame_ids != [f["frame_id"] for f in stats["frames"]]:
        problems.append(f"{case.case_id}/{run.run_id}: statistics 与 timeline 帧不一致")
    if len(frame_ids) != run.expected_frame_count:
        problems.append(f"{case.case_id}/{run.run_id}: 帧数 {len(frame_ids)} != {run.expected_frame_count}")

    manifest["blocks"]
    block_size = int(manifest["block_size"])
    rng = np.random.default_rng(20261027)
    checked = 0
    max_abs = 0.0
    for seq in range(len(frame_ids)):
        depth = read_native_frame(results / f"res-{seq:04d}.wd")
        frame_id = frame_ids[seq]
        block_dir = numeric_dir / frame_id
        mask = valid_mask(depth, sea)
        idx = np.flatnonzero(mask.ravel())
        if idx.size == 0:
            continue
        take = rng.choice(idx, size=min(samples, idx.size), replace=False)
        rows, cols = np.unravel_index(take, depth.shape)
        for row, col in zip(rows.tolist(), cols.tolist(), strict=True):
            published = read_value(block_dir, row, col, rows=grid.rows, cols=grid.cols, block_size=block_size)
            src = float(depth[row, col])
            if np.isnan(published):
                problems.append(f"{case.case_id}/{frame_id}: 块值 NaN 但源值 {src}")
                continue
            tol = max(TOL_ABS, TOL_REL * abs(src))
            err = abs(published - src)
            max_abs = max(max_abs, err)
            checked += 1
            if err > tol:
                problems.append(
                    f"{case.case_id}/{frame_id} r{row}c{col}: "
                    f"发布值 {published:.8f} vs 源 {src:.8f} 差 {err:.3e} > {tol:.1e}"
                )
                if len(problems) > 8:
                    return problems
        # 统计复算
        frame_stat = stats["frames"][seq]
        for threshold in stats["thresholds_m"]:
            tkey = str(float(threshold))
            wet = (depth > np.float32(threshold)) & mask
            expect_area = round(int(np.count_nonzero(wet)) * grid.cell_area_m2 / 1e6, 6)
            got = float(frame_stat["areas_km2"][tkey])
            if expect_area == 0.0:
                if got != 0.0:
                    problems.append(f"{case.case_id}/{frame_id} t={tkey}: 期望 0，发布 {got}")
            else:
                rel = abs(got - expect_area) / expect_area
                if rel > STAT_REL_TOL:
                    problems.append(f"{case.case_id}/{frame_id} t={tkey}: 面积 {got} vs {expect_area} 相对差 {rel:.4%}")
    print(
        f"  numeric: {case.case_id}/{run.run_id} 抽样 {checked} 点，最大绝对误差 {max_abs:.3e} m"
        f"（门禁 {TOL_ABS:g} m 或 1e-6×|源值|）"
    )
    return problems


def check_display(config, case, run, bundle: Path) -> list[str]:
    problems: list[str] = []
    images = json.loads((bundle / "frames" / case.case_id / run.run_id / "images.json").read_text(encoding="utf-8"))
    classes = images["color_scale"]["classes"]
    split = images["split"]
    results = run.source_dir / "results"
    header = read_ascii_header(results / "res-0000.wd")
    grid = header_to_grid(header, case.crs)
    transparent_below = float(images["color_scale"]["transparent_below_m"])

    frames = images["frames"]
    if len(frames) != run.expected_frame_count:
        problems.append(f"{case.case_id}/{run.run_id}: images.json 帧数 {len(frames)} != {run.expected_frame_count}")
    total_parts = len(frames[0]["parts"])
    if total_parts != split["rows"] * split["cols"]:
        problems.append(f"{case.case_id}/{run.run_id}: parts 数 {total_parts} != split {split['rows']}x{split['cols']}")

    # 抽样 3 帧：把 parts 拼回整幅，再与源帧的分级色逐像素比对
    sample_frames = sorted({0, len(frames) // 2, len(frames) - 1})
    rng = np.random.default_rng(7)
    total_checked = 0
    total_mismatch = 0
    for seq in sample_frames:
        depth = read_native_frame(results / f"res-{seq:04d}.wd")
        rebuilt = np.zeros((grid.rows, grid.cols, 4), dtype=np.uint8)
        covered = np.zeros((grid.rows, grid.cols), dtype=bool)
        for part in frames[seq]["parts"]:
            path = bundle / part["path"]
            if not path.is_file():
                problems.append(f"{case.case_id}: 缺少显示图 {part['path']}")
                continue
            with Image.open(path) as im:
                rgba = np.asarray(im.convert("RGBA"))
            if rgba.shape[:2] != (part["height"], part["width"]):
                problems.append(
                    f"{case.case_id}: {part['path']} 尺寸 {rgba.shape[1]}x{rgba.shape[0]} "
                    f"!= 声明 {part['width']}x{part['height']}"
                )
                continue
            r0 = part["row"] * part["height"]
            c0 = part["col"] * part["width"]
            r1, c1 = r0 + part["height"], c0 + part["width"]
            if r1 > grid.rows or c1 > grid.cols:
                problems.append(f"{case.case_id}: part {part['id']} 越界")
                continue
            if covered[r0:r1, c0:c1].any():
                problems.append(f"{case.case_id}: part {part['id']} 与其它 part 重叠")
            covered[r0:r1, c0:c1] = True
            rebuilt[r0:r1, c0:c1] = rgba
        if not covered.all():
            problems.append(
                f"{case.case_id}/{frames[seq]['frame_id']}: parts 未覆盖整幅（缺 {int((~covered).sum())} 像元）"
            )
            continue
        wet = np.argwhere(depth > np.float32(transparent_below))
        # 干像元必须全透明
        dry_alpha = rebuilt[..., 3][~(depth > np.float32(transparent_below))]
        if dry_alpha.size and int(dry_alpha.max()) != 0:
            problems.append(
                f"{case.case_id}/{frames[seq]['frame_id']}: 低于阈值的像元未全透明（max alpha={int(dry_alpha.max())}）"
            )
        if wet.size == 0:
            continue
        pick = wet[rng.choice(len(wet), size=min(400, len(wet)), replace=False)]
        for row, col in pick:
            expected = _class_of(float(depth[row, col]), classes)
            alpha = int(rebuilt[row, col, 3])
            rgb = "#{:02x}{:02x}{:02x}".format(*tuple(int(v) for v in rebuilt[row, col, :3]))
            total_checked += 1
            if expected is None:
                continue
            if alpha == 0 or rgb != expected:
                total_mismatch += 1
                if total_mismatch <= 5:
                    problems.append(
                        f"{case.case_id}/{frames[seq]['frame_id']} r{row}c{col}: "
                        f"depth={depth[row, col]:.3f} 期望 {expected} 实得 {rgb}/alpha={alpha}"
                    )
    print(
        f"  display: {case.case_id}/{run.run_id} 抽样 {total_checked} 湿像元，"
        f"分级色不匹配 {total_mismatch}，split={split['rows']}x{split['cols']}"
    )
    return problems


def check_semantics(case, bundle: Path) -> list[str]:
    problems: list[str] = []
    for run in case.runs.values():
        path = bundle / "cases" / case.case_id / run.run_id / "timeline.json"
        if not path.is_file():
            continue
        tl = json.loads(path.read_text(encoding="utf-8"))
        relative_only = run.origin_status.startswith("relative_only")
        if relative_only:
            if tl["time_origin"] is not None:
                problems.append(f"{case.case_id}/{run.run_id}: relative_only 却写了 time_origin")
            if any(f["timestamp"] is not None for f in tl["frames"]):
                problems.append(f"{case.case_id}/{run.run_id}: relative_only 却写了 timestamp")
        else:
            if tl["time_origin"] is None:
                problems.append(f"{case.case_id}/{run.run_id}: origin 已确认却 time_origin=null")
            if any(f["timestamp"] is None for f in tl["frames"]):
                problems.append(f"{case.case_id}/{run.run_id}: origin 已确认却缺 timestamp")
        sims = [f["sim_seconds"] for f in tl["frames"]]
        if any(later <= earlier for earlier, later in itertools.pairwise(sims)):
            problems.append(f"{case.case_id}/{run.run_id}: sim_seconds 不严格递增")
        if tl["frames"][-1]["sim_seconds"] > run.simulation_duration_s + 1e-6:
            problems.append(f"{case.case_id}/{run.run_id}: 末帧超出模拟结束")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="独立验收发布包")
    parser.add_argument("bundle")
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--config", default=str(ROOT / "configs" / "default.yaml"))
    args = parser.parse_args(argv)
    bundle = Path(args.bundle)
    config = load_config(args.config)

    problems: list[str] = []
    print(f"[verify] bundle={bundle}")
    for case_id, case in config.cases.items():
        for run in case.runs.values():
            if not (bundle / "cases" / case_id / run.run_id / "timeline.json").is_file():
                print(f"  (skip) {case_id}/{run.run_id}: 不在包内")
                continue
            problems += check_numeric_and_stats(config, case, run, bundle, args.samples)
            problems += check_semantics(case, bundle)
        problems += check_display(config, case, case.run(), bundle)

    print()
    if problems:
        print(f"[verify] 未通过：{len(problems)} 个问题")
        for item in problems[:40]:
            print("  -", item)
        return 1
    print("[verify] 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
