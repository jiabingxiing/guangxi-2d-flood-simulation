"""发布保真回归：用 `data/splits/sampling_index.json` 的**固定抽样点**校验发布包。

与 `validate --full` 的区别：
- `validate` 每次构建自己随机抽样，适合构建期门禁；
- 本测试用**入库的固定索引**，因此可判断「产品变了」还是「数据变了」——同一索引在任一次
  重建后都必须给出相同结论。这是计划 §4 要求 `data/splits/` 入库的原因。

标记 `slow`（全量 226 帧，约 40–90 s），默认不随单元测试跑。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.slow

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INDEX_PATH = PROJECT_ROOT / "data" / "splits" / "sampling_index.json"

ABS_TOL = 1e-5
REL_TOL = 1e-6


def _load_index() -> dict:
    if not INDEX_PATH.is_file():
        pytest.skip(f"缺少固定抽样索引：{INDEX_PATH}（先跑 scripts/make_sampling_index.py）")
    return json.loads(INDEX_PATH.read_text(encoding="utf-8"))


def _bundle_runs(bundle: Path) -> dict[str, set[str]]:
    """发布包里实际存在的 case/run。"""
    catalog = json.loads((bundle / "catalog.json").read_text(encoding="utf-8"))
    return {case["case_id"]: {run["run_id"] for run in case["runs"]} for case in catalog["cases"]}


@pytest.fixture(scope="module")
def release_bundle(project_root: Path) -> Path:
    bundles = sorted(
        (project_root / "outputs" / "web_bundle").glob("v[0-9]*-*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not bundles:
        pytest.skip("没有正式发布包（outputs/web_bundle/v*-*）")
    return bundles[0]


def test_fixed_sampling_gate(release_bundle: Path, project_root: Path) -> None:
    """固定抽样点：发布包数值必须与源 `.wd` 一致（误差 ≤ max(1e-5, 1e-6×|源|)）。"""
    from floodviz.config import load_config
    from floodviz.raster.blocks import read_frame
    from floodviz.raster.grid import header_to_grid, read_ascii_header, read_native_frame

    index = _load_index()
    config = load_config(project_root / "configs" / "default.yaml")
    present = _bundle_runs(release_bundle)

    checked = 0
    checked_frames = 0
    max_abs = 0.0
    problems: list[str] = []

    for case_id, runs in index["cases"].items():
        case = config.case(case_id)
        for run_id, run_doc in runs.items():
            if run_id not in present.get(case_id, set()):
                continue  # 可选 run 未入包：跳过（不冒充已发布）
            run = case.run(run_id)
            results = run.source_dir / "results"
            header = read_ascii_header(results / "res-0000.wd")
            grid = header_to_grid(header, case.crs)
            numeric_dir = release_bundle / "numeric" / case_id / run_id
            numeric_manifest = json.loads((numeric_dir / "manifest.json").read_text(encoding="utf-8"))
            block_size = int(numeric_manifest["block_size"])

            for frame_doc in run_doc["frames"]:
                frame_id = frame_doc["frame_id"]
                seq = int(frame_id[1:])
                source = read_native_frame(results / f"res-{seq:04d}.wd")
                published = read_frame(numeric_dir / frame_id, rows=grid.rows, cols=grid.cols, block_size=block_size)
                checked_frames += 1

                points: list[tuple[int, int]] = []
                for kind in ("random", "boundary", "nodata", "max"):
                    points.extend((int(r), int(c)) for r, c in frame_doc.get(kind, []))
                for row, col in points:
                    src = float(source[row, col])
                    pub = float(published[row, col])
                    checked += 1
                    if np.isnan(src) and np.isnan(pub):
                        continue
                    if np.isnan(src) != np.isnan(pub):
                        problems.append(f"{case_id}/{run_id}/{frame_id} r{row}c{col}: NaN 不一致")
                        continue
                    err = abs(pub - src)
                    max_abs = max(max_abs, err)
                    tol = max(ABS_TOL, REL_TOL * abs(src))
                    if err > tol:
                        problems.append(
                            f"{case_id}/{run_id}/{frame_id} r{row}c{col}: "
                            f"发布 {pub:.8f} vs 源 {src:.8f} 差 {err:.3e} > {tol:.1e}"
                        )
                        if len(problems) > 10:
                            break

    print(
        f"\n[fixed-sampling] bundle={release_bundle.name} 帧 {checked_frames} 个、"
        f"固定抽样点 {checked} 个，最大绝对误差 {max_abs:.3e} m"
    )
    assert checked > 2000, f"抽样点过少：{checked}"
    assert not problems, problems[:10]


def test_fixed_sampling_envelope(release_bundle: Path, project_root: Path) -> None:
    """最大包络层的固定抽样点：`res.max` 与发布包 `numeric/<run>/max` 一致。"""
    from floodviz.config import load_config
    from floodviz.raster.blocks import read_frame
    from floodviz.raster.grid import header_to_grid, read_ascii_header, read_native_frame

    index = _load_index()
    config = load_config(project_root / "configs" / "default.yaml")
    present = _bundle_runs(release_bundle)

    checked = 0
    max_abs = 0.0
    problems: list[str] = []

    for case_id, runs in index["cases"].items():
        case = config.case(case_id)
        for run_id, run_doc in runs.items():
            envelope = run_doc.get("max_envelope")
            if envelope is None or run_id not in present.get(case_id, set()):
                continue
            run = case.run(run_id)
            header = read_ascii_header(run.source_dir / "results" / "res-0000.wd")
            grid = header_to_grid(header, case.crs)
            source = read_native_frame(run.source_dir / "results" / "res.max")
            numeric_dir = release_bundle / "numeric" / case_id / run_id
            manifest = json.loads((numeric_dir / "manifest.json").read_text(encoding="utf-8"))
            published = read_frame(
                numeric_dir / "max",
                rows=grid.rows,
                cols=grid.cols,
                block_size=int(manifest["block_size"]),
            )
            points = [(int(r), int(c)) for r, c in envelope["max_pixels"]]
            points += [(int(r), int(c)) for r, c in envelope["above_top_threshold"]]
            for row, col in points:
                src = float(source[row, col])
                pub = float(published[row, col])
                checked += 1
                if np.isnan(src) and np.isnan(pub):
                    continue
                err = abs(pub - src)
                max_abs = max(max_abs, err)
                if err > max(ABS_TOL, REL_TOL * abs(src)):
                    problems.append(f"{case_id}/{run_id} max r{row}c{col}: {pub} vs {src}")

    print(f"[fixed-sampling] 包络抽样 {checked} 点，最大绝对误差 {max_abs:.3e} m")
    assert checked > 10, f"包络抽样点过少：{checked}"
    assert not problems, problems[:10]
