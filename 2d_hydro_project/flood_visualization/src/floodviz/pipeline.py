"""发布包构建：prepare_bundle。

用户 2026-10-27 决定：**不逐帧生成 XYZ 瓦片金字塔**，直接复用已有 6 h 间隔的真实推演帧；
每帧只渲染原生网格 1:1 的预着色 PNG（必要时按整数像元边界切成少量 part），
数值保真层写 float32 256 分块。整条链路**流式**：逐帧读 → 写块/图 → 丢弃，常量内存。

产物（SPEC §3）：
    catalog.json
    cases/<case>/<run>/timeline.json|statistics.json
    cases/<case>/boundary.geojson|sites.geojson
    frames/<case>/<run>/images.json
    frames/<case>/<run>/<frame_id>_r<row>c<col>.png
    frames/<case>/<run>/max_r<row>c<col>.png
    frames/<case>/dem_r<row>c<col>.png + dem_images.json
    numeric/<case>/<run>/manifest.json|mask.png|<frame_id>/<by>/<bx>.bin|max/<by>/<bx>.bin
    licenses/LICENSES.md
    manifest.json
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from dataclasses import replace as _replace
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from PIL import Image
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.transform import Affine
from rasterio.vrt import WarpedVRT
from rasterio.warp import reproject

from .catalog.catalog import (
    apply_assets,
    build_case_entry,
    build_catalog,
    build_run_entry,
    geometry_for_grid,
)
from .catalog.quality import load_mass_gate
from .catalog.timeline import build_timeline, format_timestamp
from .config import CaseConfig, Config, ProductLayerConfig, RunConfig
from .publishing import products as products_mod
from .quality import stats as stats_mod
from .quality.frames import RunAudit, audit_run, sha256_file
from .raster import blocks as blocks_mod
from .raster import paint as paint_mod
from .raster.grid import Grid, header_to_grid, read_ascii_header, read_native_frame, read_sea_mask

SCHEMA_VERSION = "1.0"
BUILD_DIR_SUFFIX = ".building"
ASSET_MISSING_REASON = "尚未导入发布包（等待 scripts/prepare_optical.py 产出）"
LICENSE_TEXT = """# 许可与授权（LICENSES）

本发布包为**研究性情景成果**，不得用于业务告警、防洪调度决策或资产安全判据。

## 1. 模型成果数据

- 来源：LISFLOOD-FP 8.1.0 二维水动力模型情景计算输出（父项目 `2d_hydro_project/outputs/**`）。
- 用途限制：情景级研究复现；`mass_gate=PASS` 不等于“预测正确”；垂直基准未闭合（G7）。
- 再分发：随本可视化工程一并分发；引用须注明模型版本、情景与降级模式限制。

## 2. 在线底图

- Esri World Imagery / World Street Map：仅运行时在线加载，**未**缓存或再分发瓦片；
  署名保留在页面右下角。`exportTilesAllowed=false`。离线时底图不可用，业务图层不受影响。

## 3. 卫星影像（若包内存在）

- Sentinel-1 / Sentinel-2：`Contains modified Copernicus Sentinel data`（裁剪、重采样、遮云处理）。
- GF-3：按获取渠道条款使用，仅作水体范围证据。

## 4. 软件

- MapLibre GL JS、Proj4js 及其许可证文本随 `assets/vendor/**` 与 `licenses/*.txt` 一并分发。
- 本工程代码：见仓库 `README.md`。

## 5. 数据缺口与降级说明

- 各案例质量门、验证状态、垂直基准状态与告警见 `catalog.json` 的 `cases[].quality` 与 `runs[].quality`。
- 帧为 6 h 间隔离散快照（`temporal_interpolation: "none"`），播放速度不改变物理时间分辨率。
- 面积为原生网格单帧有效像元（非 NaN、非海域）`depth>阈值` 的面积，不是资产安全等级。
- 显示层为原生网格 1:1 预着色 PNG（必要时按整数像元边界切 part），无 XYZ 金字塔、无插值；
  定量查询一律走 `numeric/**` 的 float32 数据块，不得由 PNG 颜色反算水深。
"""


class PipelineError(RuntimeError):
    """发布包构建失败（不可发布）。"""


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _write_json(path: Path, doc: Any) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def _write_text(path: Path, text: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rel_posix(path: Path, root: Path) -> str:
    return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()


def _transformer(crs: str) -> Transformer:
    return Transformer.from_crs(crs, "EPSG:4326", always_xy=True)


# --------------------------------------------------------------------------- #
# 溯源字段（计划 §3.2 契约：运行 source_asset_ids/source_hashes、网格 grid_hash）
# --------------------------------------------------------------------------- #
MODEL_VERSION_FALLBACK = "LISFLOOD-FP 8.1.0 (double, _CALCULATE_Q_MODE 1)"

# 输入清单里与各案例相关的资产键（前缀/包含匹配），用于 source_asset_ids。
# 只列"确属该案例模型输入"的键；不把其它案例的 DEM/曼宁/SAR 混进来。
ASSET_KEY_RULES: dict[str, tuple[str, ...]] = {
    "jiulong": (
        "dem_source_jiulong_clip",
        "mannings_jiulong",
        "era5_jiulong_sep2024",
        "era5_jiulong_sep2024_ext",
        "s1_jiulong_0905_water",
        "s1_jiulong_0917_water",
        "config_jiulong",
        "config_base",
        "config_model_manifest",
        "lisflood_exe",
    ),
    "ningming": (
        "dem_ningming_interim",
        "dem_ningming_raw_clip",
        "mannings_ningming",
        "water_0806_ningming",
        "water_new_0824_ningming",
        "era5_ningming_aug2026",
        "config_ningming",
        "config_base",
        "config_model_manifest",
        "lisflood_exe",
    ),
    "guigang": (
        "dem_guigang_clip",
        "mannings_guigang",
        "era5_guigang_sep2024",
        "s1_guigang_0905_water",
        "s1_guigang_0912_water",
        "s1_guigang_0912_sigma",
        "config_guigang",
        "config_base",
        "config_model_manifest",
        "lisflood_exe",
    ),
}


def _parent_catalog_json(config: Config, name: str) -> dict[str, Any] | None:
    """读父项目 data/catalog/<name>.json（只读；容错 UTF-8/GBK）。"""
    path = config.parent_root / "data" / "catalog" / name
    if not path.is_file():
        return None
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            loaded = json.loads(raw.decode(encoding))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(loaded, dict):
            return loaded
    return None


def _sha256_of(path: Path) -> str | None:
    if not path.is_file():
        return None
    return sha256_file(path)


def grid_hash(grid: Any) -> str:
    """网格指纹：rows/cols/transform/crs 规范化 JSON 后 sha256（32 位十六进制前缀）。"""
    payload = {
        "rows": int(grid.rows),
        "cols": int(grid.cols),
        "transform": [round(float(v), 9) for v in grid.transform],
        "crs": str(grid.crs),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _source_asset_ids(config: Config, case: CaseConfig, input_manifest: dict[str, Any] | None) -> list[str]:
    """该 run 实际依赖的输入资产 id（来自父项目 input_manifest.json 的键名）。"""
    if not input_manifest:
        return []
    known = set((input_manifest.get("files") or {}).keys())
    wanted = ASSET_KEY_RULES.get(case.case_id, ())
    ids = [key for key in wanted if key in known]
    # 案例未覆盖的资产键兜底：按案例前缀匹配，避免漏记
    if not ids:
        ids = sorted(k for k in known if case.case_id in k)
    return ids


def provenance_for_run(
    config: Config,
    case: CaseConfig,
    run: RunConfig,
    frames: Sequence[Any],
) -> dict[str, Any]:
    """构造 runs[] 的 model_version / scenario_role / source_asset_ids / source_hashes。"""
    input_manifest = _parent_catalog_json(config, "input_manifest.json")
    code_manifest = _parent_catalog_json(config, "code_manifest.json")
    inputs = (input_manifest or {}).get("files") or {}

    # frames_manifest_sha256：按 frame_id 排序拼接 "frame_id:sha256"，再取 sha256
    pairs = sorted(f"{frame.frame_id}:{frame.sha256}" for frame in frames)
    frames_manifest_sha256 = _sha256_text("\n".join(pairs)) if pairs else None

    input_manifest_path = config.parent_root / "data" / "catalog" / "input_manifest.json"
    code_manifest_path = config.parent_root / "data" / "catalog" / "code_manifest.json"

    hashes: dict[str, Any] = {
        "code_tree_sha256": (code_manifest or {}).get("tree_sha256"),
        "input_manifest_sha256": _sha256_of(input_manifest_path),
        "frames_manifest_sha256": frames_manifest_sha256,
        "frame_count_hashed": len(pairs),
        "dem_sha256": _sha256_of(run.source_dir / "dem.asc"),
        "run_par_sha256": _sha256_of(run.source_dir / "run.par"),
        "code_manifest_sha256": _sha256_of(code_manifest_path),
        "meta_json_sha256": _sha256_of(run.source_dir / "meta.json"),
    }
    # 输入资产本身的哈希（取该案例相关的清单条目），便于溯源到影像/DEM/ERA5
    asset_ids = _source_asset_ids(config, case, input_manifest)
    asset_hashes = {
        asset_id: inputs[asset_id].get("sha256")
        for asset_id in asset_ids
        if asset_id in inputs and inputs[asset_id].get("sha256")
    }
    hashes["input_assets_sha256"] = asset_hashes

    model_version = MODEL_VERSION_FALLBACK
    model_manifest = config.parent_root / "configs" / "model_manifest.yaml"
    if model_manifest.is_file():  # pragma: no cover - 只在父项目有 yaml 时走
        try:
            import yaml

            doc = yaml.safe_load(model_manifest.read_text(encoding="utf-8")) or {}
            banner = (((doc.get("primary_candidate") or {}).get("build") or {}).get("banner")) or ""
            if "LISFLOOD-FP" in str(banner):
                model_version = str(banner)
        except (OSError, ValueError, ImportError):
            pass

    return {
        "model_version": model_version,
        "scenario_role": case.scenario_role or case.quality.get("scenario_role", ""),
        "source_asset_ids": asset_ids,
        "source_hashes": hashes,
    }


def envelope_reproducibility_crosscheck(
    config: Config,
    case: CaseConfig,
    statistics: dict[str, Any],
) -> dict[str, Any]:
    """把本包自算的 max 包络面积与父项目可复现门记录对照（只做交叉证据，不抄数）。"""
    doc = _parent_catalog_json(config, "reproducibility_test.json")
    entry = ((doc or {}).get("cases") or {}).get(case.case_id) or {}
    base_area = entry.get("wet_area_base_km2")
    our_area = (((statistics.get("max_envelope") or {}).get("areas_km2")) or {}).get(
        stats_mod.threshold_key(config.default_threshold_m)
    )
    if base_area is None or our_area is None:
        return {}
    diff = abs(float(our_area) - float(base_area))
    return {
        "parent_reference_wet_area_km2": float(base_area),
        "our_max_envelope_area_km2": float(our_area),
        "abs_diff_km2": round(diff, 6),
        "match": bool(diff <= 0.01),
        "our_source": "res.max 经本包 pixel 面积统计独立算出（非抄录父项目 JSON）",
        "parent_source": "data/catalog/reproducibility_test.json",
        "parent_verdict": entry.get("verdict"),
    }



def _run_rel(config: Config, run: RunConfig) -> str:
    try:
        return _rel_posix(run.source_dir, config.parent_root)
    except ValueError:  # pragma: no cover - 非标准目录布局
        return run.source_dir.as_posix()


def _rmtree(path: Path, *, retries: int = 6) -> None:
    """删除目录树；Windows 上偶发 "目录不是空的"（文件句柄/杀软扫描延迟），带退避重试。"""
    if not path.exists():
        return
    last: OSError | None = None
    for attempt in range(retries):
        try:
            shutil.rmtree(path)
            return
        except OSError as exc:
            last = exc
            time.sleep(0.25 * (attempt + 1))
            if path.exists() and attempt == retries - 1:
                shutil.rmtree(path, ignore_errors=True)
    if path.exists() and last is not None:
        raise last


def _file_bytes(root: Path) -> tuple[int, int]:
    count = 0
    size = 0
    for item in root.rglob("*"):
        if item.is_file():
            count += 1
            size += item.stat().st_size
    return count, size


def _frame_path(run: RunConfig, seq: int) -> Path:
    return run.source_dir / "results" / f"res-{seq:04d}.wd"


# --------------------------------------------------------------------------- #
# 构建实测数据
# --------------------------------------------------------------------------- #
@dataclass
class BuildStats:
    """构建实测数据（写入 manifest.build 与 docs/build_report.md）。"""

    started_utc: str = ""
    finished_utc: str = ""
    wall_seconds: float = 0.0
    frames_total: int = 0
    png_total: int = 0
    png_bytes_total: int = 0
    numeric_bytes_total: int = 0
    bundle_bytes_total: int = 0
    file_count: int = 0
    per_run: dict[str, Any] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_utc": self.started_utc,
            "finished_utc": self.finished_utc,
            "wall_seconds": round(self.wall_seconds, 3),
            "frames_total": self.frames_total,
            "png_total": self.png_total,
            "png_bytes_total": self.png_bytes_total,
            "numeric_bytes_total": self.numeric_bytes_total,
            "bundle_bytes_total": self.bundle_bytes_total,
            "file_count": self.file_count,
            "per_run": self.per_run,
            "timings": {k: round(v, 3) for k, v in self.timings.items()},
        }


# --------------------------------------------------------------------------- #
# 单 run
# --------------------------------------------------------------------------- #
@dataclass
class RunResult:
    run_id: str
    frame_count: int
    split_rows: int
    split_cols: int
    spatial: dict[str, Any]
    max_parts: list[dict[str, Any]] = field(default_factory=list)
    png_bytes: list[int] = field(default_factory=list)
    numeric_bytes: int = 0
    numeric_files: int = 0
    mask_bytes: int = 0
    max_crosscheck: dict[str, Any] = field(default_factory=dict)
    stats_doc: dict[str, Any] = field(default_factory=dict)
    timeline_doc: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    seconds: float = 0.0
    sea_cell_count: int = 0
    grid_hash: str = ""
    repro: dict[str, Any] = field(default_factory=dict)


def _grid_for_run(run: RunConfig, first_header: dict[str, float], crs: str) -> Grid:
    grid = header_to_grid(first_header, crs)
    if not (run.source_dir / "results").is_dir():
        raise PipelineError(f"{run.run_id}: 缺 results 目录 {run.source_dir / 'results'}")
    return grid


def _render_parts(
    rgba: np.ndarray,
    windows: Sequence[paint_mod.PartWindow],
    *,
    out_dir: Path,
    stem: str,
    image_prefix: str,
    grid: Grid,
    tr_fn: Any,
    depth: np.ndarray,
    transparent_below_m: float,
) -> tuple[list[dict[str, Any]], int]:
    """把整幅 RGBA 按 part 写 PNG；返回 (part 元数据, 总字节数)。"""
    parts: list[dict[str, Any]] = []
    total_bytes = 0
    for win in windows:
        pid = paint_mod.part_id(stem, win)
        path = out_dir / f"{pid}.png"
        crop = paint_mod.crop_rgba(rgba, win)
        nbytes = paint_mod.save_png(crop, path)
        total_bytes += nbytes
        sub = depth[win.r0 : win.r1, win.c0 : win.c1]
        summary = paint_mod.depth_frame_summary(sub, transparent_below_m=transparent_below_m)
        parts.append(
            {
                "id": pid,
                "row": win.part_row,
                "col": win.part_col,
                "path": f"{image_prefix}/{pid}.png",
                "width": int(win.width),
                "height": int(win.height),
                "coordinates": paint_mod.part_corners_lonlat(grid, win, tr_fn),
                "max_depth_m": summary["max_depth_m"],
                "wet_pixels": summary["wet_pixels"],
                "bytes": nbytes,
            }
        )
    return parts, total_bytes


def _process_run(
    config: Config,
    case: CaseConfig,
    run: RunConfig,
    bundle: Path,
    *,
    frames_limit: int | None,
    skip_tiles: bool,
    classes: list[paint_mod.DepthClass],
    transparent_below_m: float,
) -> RunResult:
    """审计 → 逐帧（读→块→PNG→统计→丢弃）→ max → 写 contracts。"""
    t_run = time.perf_counter()
    block_size = config.block_size
    audit: RunAudit = audit_run(
        run.source_dir,
        save_interval_s=run.save_interval_s,
        simulation_duration_s=run.simulation_duration_s,
        expected_count=run.expected_frame_count,
    )
    if audit.reject_reasons:
        raise PipelineError(f"{case.case_id}/{run.run_id} 帧审计拒绝：" + "；".join(audit.reject_reasons))

    grid = _grid_for_run(run, audit.frames[0].header, case.crs)
    tr = _transformer(case.crs)
    split = paint_mod.choose_split(grid, tr.transform, tol_px=paint_mod.DEFAULT_TOL_PX)
    split_rows, split_cols = split["split_rows"], split["split_cols"]
    windows = paint_mod.part_windows(grid.rows, grid.cols, split_rows, split_cols)

    sea_mask = read_sea_mask(run.source_dir, (grid.rows, grid.cols))
    sea_cell_count = int(np.count_nonzero(sea_mask)) if sea_mask is not None else 0

    frames = audit.frames if frames_limit is None else audit.frames[: int(frames_limit)]
    if not frames:
        raise PipelineError(f"{case.case_id}/{run.run_id}: frames_limit 导致无帧可导出")

    case_id, run_id = case.case_id, run.run_id
    image_prefix = f"frames/{case_id}/{run_id}"
    frames_dir = bundle / image_prefix
    frames_dir.mkdir(parents=True, exist_ok=True)
    numeric_run_dir = bundle / "numeric" / case_id / run_id
    numeric_run_dir.mkdir(parents=True, exist_ok=True)

    builder = stats_mod.StatisticsBuilder(
        thresholds=config.thresholds_m,
        cell_area_m2=grid.cell_area_m2,
        default_threshold_m=config.default_threshold_m,
        sea_mask=sea_mask,
    )
    running_max: np.ndarray | None = None
    mask_valid: np.ndarray | None = None
    mask_consistent = True
    frame_entries: list[dict[str, Any]] = []
    png_bytes: list[int] = []
    numeric_bytes = 0
    numeric_files = 0
    warnings: list[str] = []
    t_read = t_render = 0.0

    for frame in frames:
        t0 = time.perf_counter()
        depth = read_native_frame(frame.path)
        t1 = time.perf_counter()
        if depth.shape != (grid.rows, grid.cols):
            raise PipelineError(f"{frame.frame_id} 形状 {depth.shape} 与网格 {(grid.rows, grid.cols)} 不符")
        valid = ~np.isnan(depth)
        if mask_valid is None:
            mask_valid = valid.copy()
        elif not np.array_equal(mask_valid, valid):
            mask_consistent = False

        info = blocks_mod.write_blocks(depth, numeric_run_dir / frame.frame_id, block_size=block_size)
        numeric_bytes += info["bytes"]
        numeric_files += info["block_files"]

        if skip_tiles:
            totals = paint_mod.depth_frame_summary(depth, transparent_below_m=transparent_below_m)
            frame_entries.append(
                {
                    "frame_id": frame.frame_id,
                    "max_depth_m": totals["max_depth_m"],
                    "wet_pixels": totals["wet_pixels"],
                    "parts": [],
                }
            )
        else:
            rgba = paint_mod.colorize_depth(depth, classes, transparent_below_m=transparent_below_m)
            parts, _bytes_written = _render_parts(
                rgba,
                windows,
                out_dir=frames_dir,
                stem=frame.frame_id,
                image_prefix=image_prefix,
                grid=grid,
                tr_fn=tr.transform,
                depth=depth,
                transparent_below_m=transparent_below_m,
            )
            del rgba
            png_bytes.extend(p["bytes"] for p in parts)
            totals = paint_mod.depth_frame_summary(depth, transparent_below_m=transparent_below_m)
            frame_entries.append(
                {
                    "frame_id": frame.frame_id,
                    "max_depth_m": totals["max_depth_m"],
                    "wet_pixels": totals["wet_pixels"],
                    "parts": parts,
                }
            )

        sim_seconds = float(frame.seq) * float(run.save_interval_s)
        timestamp = format_timestamp(run.time_origin, sim_seconds, origin_status=run.origin_status)
        builder.add_frame(
            frame_id=frame.frame_id,
            sim_seconds=sim_seconds,
            timestamp=timestamp,
            depth=depth,
        )

        nan_aware = np.where(np.isnan(depth), -np.inf, depth)
        running_max = nan_aware if running_max is None else np.maximum(running_max, nan_aware)
        del depth, nan_aware
        t_render += time.perf_counter() - t1
        t_read += t1 - t0

    if mask_valid is None:  # pragma: no cover - frames 非空已保证
        raise PipelineError("没有读到任何帧")
    if not mask_consistent:
        warnings.append("各帧 NaN 分布不一致；mask.png 取首帧非 NaN 像元")
    mask_rel = f"numeric/{case_id}/{run_id}/mask.png"
    mask_bytes = blocks_mod.write_mask_png(mask_valid, bundle / mask_rel)

    # ---- res.max：数值块 + PNG + 包络统计 ----
    max_parts: list[dict[str, Any]] = []
    max_env: dict[str, Any] | None = None
    crosscheck: dict[str, Any] = {}
    if audit.max_grid_path is not None:
        max_depth = read_native_frame(audit.max_grid_path)
        if max_depth.shape != (grid.rows, grid.cols):
            raise PipelineError(f"res.max 形状 {max_depth.shape} 与网格 {(grid.rows, grid.cols)} 不符")
        info = blocks_mod.write_blocks(max_depth, numeric_run_dir / "max", block_size=block_size)
        numeric_bytes += info["bytes"]
        numeric_files += info["block_files"]
        if not skip_tiles:
            rgba = paint_mod.colorize_depth(max_depth, classes, transparent_below_m=transparent_below_m)
            max_parts, _nbytes = _render_parts(
                rgba,
                windows,
                out_dir=frames_dir,
                stem="max",
                image_prefix=image_prefix,
                grid=grid,
                tr_fn=tr.transform,
                depth=max_depth,
                transparent_below_m=transparent_below_m,
            )
            del rgba
            png_bytes.extend(p["bytes"] for p in max_parts)
        max_env = stats_mod.envelope_stats(max_depth, config.thresholds_m, grid.cell_area_m2, sea_mask)
        if running_max is not None and frames_limit is None:
            # 口径（Lead 2026-10-27 确认，2026-11-02 修订容差）：
            # res.max 是子步累积包络，逐帧只是 6 h 采样，因此 res.max ≥ 逐帧fmax
            # 应当单向成立；差异是物理正确，只做信息性统计。
            #
            # **容差必须等于 .wd 输出量化步长 0.001 m**（output.cpp:686 以 %.3f 写盘）：
            # 求解器在子步内可能让某像元短暂升到 0.001 m 又退回 0，而 6 h 采样帧恰好
            # 抓到 0.001 m、res.max 写到 0.000 —— 此时 frame > res.max 但差值恰为一个
            # 量化步长，属输出精度伪影而非物理不一致。原用 -1e-9 会把这类像元判为
            # 违反单向性（实测 flow_only_v1 有 9 个像元、差值均 0.0010 m）。
            # 超过一个量化步长仍判失败，断言强度不变。
            _WD_QUANTUM_M = 1e-3
            frame_max = np.where(np.isinf(running_max), np.nan, running_max)
            both = np.isfinite(frame_max) & np.isfinite(max_depth)
            if both.any():
                signed = max_depth[both] - frame_max[both]
                tolerance = -_WD_QUANTUM_M - 1e-9
                crosscheck = {
                    "res_max_ge_frames": bool(np.all(signed >= tolerance)),
                    "min_signed_diff_m": round(float(signed.min()), 6),
                    "max_diff_m": round(float(signed.max()), 6),
                    "max_abs_diff_m": round(float(np.abs(signed).max()), 6),
                    "compared_cells": int(both.sum()),
                    "frames_sampled": len(frames),
                    "tolerance_m": _WD_QUANTUM_M,
                    "tolerance_basis": (
                        ".wd 输出量化步长 0.001 m（output.cpp:686 %.3f）；"
                        "差值超过一个量化步长才判违反单向性"
                    ),
                    "note": "res.max 为子步累积包络，逐帧为 6 h 采样；差异单向且物理正确",
                }
                if not crosscheck["res_max_ge_frames"]:
                    warnings.append(
                        "异常：存在逐帧值大于 res.max 的像元（超出输出量化步长 0.001 m）"
                        f"，最小有符号差 {signed.min():.6f} m"
                    )
            del frame_max
        del max_depth
    else:
        warnings.append("缺 results/res.max：不提供最大包络图层")
    del running_max

    # ---- contracts ----
    run_paths = {
        "run_id": run_id,
        "label": run.label,
        "role": run.role,
        "frame_count": len(frames),
        "save_interval_s": float(run.save_interval_s),
        "simulation_duration_s": float(run.simulation_duration_s),
        "timeline": f"cases/{case_id}/{run_id}/timeline.json",
        "statistics": f"cases/{case_id}/{run_id}/statistics.json",
        "numeric_manifest": f"numeric/{case_id}/{run_id}/manifest.json",
        "optional": run.optional,
        "notes": run.notes,
    }
    timeline = build_timeline(
        case,
        run,
        frames,
        images_prefix=image_prefix,
        blocks_prefix=f"numeric/{case_id}/{run_id}",
        statistics_path=run_paths["statistics"],
        numeric_manifest_path=run_paths["numeric_manifest"],
    )
    statistics = builder.to_dict(case_id=case_id, run_id=run_id, max_envelope=max_env)

    images_doc = paint_mod.build_images_doc(
        case_id=case_id,
        run_id=run_id,
        grid=grid,
        classes=classes,
        transparent_below_m=transparent_below_m,
        split_rows=split_rows,
        split_cols=split_cols,
        frames=frame_entries,
        max_entry=(
            {
                "max_depth_m": (max_env or {}).get("max_depth_m", 0.0),
                "wet_pixels": int(sum(p["wet_pixels"] for p in max_parts)),
                "parts": max_parts,
            }
            if max_parts
            else None
        ),
    )
    images_doc["spatial_validation"] = {
        "gate_px": split["tol_px"],
        "max_px": round(split["max_px"], 6),
        "max_m": round(split["max_m"], 4),
        "samples_per_part": split["samples_per_part"],
        "method": "像元中心 → EPSG:4326 → Web Mercator → part 四角双线性反算 → 回原生像元坐标",
        "parts": [
            {
                "id": f"r{p['part_row']}c{p['part_col']}",
                "max_px": round(p["max_px"], 6),
                "max_m": round(p["max_m"], 4),
            }
            for p in split["parts"]
        ],
        "attempts": split["attempts"],
        "passed": True,
    }

    numeric_manifest = blocks_mod.build_numeric_manifest(
        case_id=case_id,
        run_id=run_id,
        grid=grid,
        block_size=block_size,
        frames=frames,
        mask_png_rel=mask_rel,
        sea_cell_count=sea_cell_count,
        has_max=audit.max_grid_path is not None,
    )

    _write_json(bundle / run_paths["timeline"], timeline)
    _write_json(bundle / run_paths["statistics"], statistics)
    _write_json(bundle / run_paths["numeric_manifest"], numeric_manifest)
    if not skip_tiles:
        _write_json(bundle / image_prefix / "images.json", images_doc)

    seconds = time.perf_counter() - t_run
    return RunResult(
        run_id=run_id,
        frame_count=len(frames),
        split_rows=split_rows,
        split_cols=split_cols,
        spatial={
            "split": {"rows": split_rows, "cols": split_cols},
            "parts": [
                {
                    "id": f"r{p['part_row']}c{p['part_col']}",
                    "max_px": round(p["max_px"], 6),
                    "max_m": round(p["max_m"], 4),
                }
                for p in split["parts"]
            ],
            "max_px": round(split["max_px"], 6),
            "max_m": round(split["max_m"], 4),
            "samples": int(split["samples_per_part"]) * len(split["parts"]),
            "gate_px": split["tol_px"],
            "passed": bool(split["max_px"] <= split["tol_px"]),
            "attempts": split["attempts"],
        },
        max_parts=max_parts,
        png_bytes=png_bytes,
        numeric_bytes=numeric_bytes,
        numeric_files=numeric_files,
        mask_bytes=mask_bytes,
        max_crosscheck=crosscheck,
        stats_doc=statistics,
        timeline_doc=timeline,
        warnings=warnings,
        seconds=seconds,
        sea_cell_count=sea_cell_count,
        grid_hash=grid_hash(grid),
        repro=envelope_reproducibility_crosscheck(config, case, statistics),
    )


# --------------------------------------------------------------------------- #
# Phase H 研究性产品图层（FAT/归因/概率）：单帧伪 run，role=product
# --------------------------------------------------------------------------- #

FRAME_ID = "f0000"  # schema 契约 frame_id 须 ^f[0-9]{4,}$；单帧静态占位


def _grid_header_dict(grid: Grid) -> dict[str, float]:
    """Grid → ESRI ASCII 6 行头 dict（FrameInfo.header 口径）。"""
    _a, _b, c, _d, e, f = (float(v) for v in grid.transform)
    return {
        "ncols": float(grid.cols),
        "nrows": float(grid.rows),
        "xllcorner": c,
        "yllcorner": f + e * grid.rows,  # f=顶边 y；yll = 顶 - rows*cell（e 为负）
        "cellsize": float(grid.cell_size_m),
        "nodata_value": -9999.0,
    }


def _process_product_layer(
    config: Config,
    case: CaseConfig,
    product: ProductLayerConfig,
    grid: Grid,
    tr: Transformer,
    split_rows: int,
    split_cols: int,
    bundle: Path,
    *,
    skip_tiles: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """把 `outputs/products/**` 的研究性产品 GeoTIFF 发布为单帧伪 run。

    返回 (catalog run_entry, BuildStats.per_run 记录)。复用深度管线的数值块、
    分幅 PNG、timeline/statistics/images 契约；产品语义由 statistics.product
    与 run_entry.product 声明（research_product=true，禁业务告警）。
    """
    from types import SimpleNamespace


    t0 = time.perf_counter()
    warnings: list[str] = []
    style = products_mod.style_for(product.kind, threshold_m=product.threshold_m)
    if not product.source_tif.is_file():
        raise PipelineError(
            f"{case.case_id}/{product.product_id}: 产品 GeoTIFF 不存在 {product.source_tif}；"
            "先运行父项目 make_fat_products.py / make_genesis_attribution.py / ensemble_probability.py"
        )
    arr = products_mod.read_product_array(product.source_tif, grid)
    if arr.shape != (grid.rows, grid.cols):
        raise PipelineError(f"{product.product_id}: 产品重投影后形状 {arr.shape} 与网格不符")

    case_id, product_id = case.case_id, product.product_id
    image_prefix = f"frames/{case_id}/{product_id}"
    frames_dir = bundle / image_prefix
    frames_dir.mkdir(parents=True, exist_ok=True)
    numeric_dir = bundle / "numeric" / case_id / product_id
    numeric_dir.mkdir(parents=True, exist_ok=True)

    block_size = config.block_size
    block_info = blocks_mod.write_blocks(arr, numeric_dir / FRAME_ID, block_size=block_size)
    mask_valid = np.isfinite(arr)
    mask_rel = f"numeric/{case_id}/{product_id}/mask.png"
    mask_bytes = blocks_mod.write_mask_png(mask_valid, bundle / mask_rel)

    windows = paint_mod.part_windows(grid.rows, grid.cols, split_rows, split_cols)
    # 渲染值：小时类把精确 0 h 平移进 0–6 h 分级（schema transparent_below_m ≥ 0）；
    # 数值块保真原值，点查不受影响
    render_arr = (
        np.where(arr == 0.0, np.float32(1e-4), arr) if style.render_zero_epsilon else arr
    )
    png_bytes: list[int] = []
    if skip_tiles:
        parts: list[dict[str, Any]] = []
        summary = paint_mod.depth_frame_summary(
            render_arr, transparent_below_m=style.transparent_below)
        frame_entry = {
            "frame_id": FRAME_ID,
            "max_depth_m": summary["max_depth_m"],
            "wet_pixels": summary["wet_pixels"],
            "parts": [],
        }
        max_entry = None
    else:
        rgba = paint_mod.colorize_depth(
            render_arr, style.depth_classes, transparent_below_m=style.transparent_below)
        parts, _part_bytes = _render_parts(
            rgba, windows, out_dir=frames_dir, stem=FRAME_ID, image_prefix=image_prefix,
            grid=grid, tr_fn=tr.transform, depth=render_arr,
            transparent_below_m=style.transparent_below)
        png_bytes = [p["bytes"] for p in parts]
        frame_entry = {
            "frame_id": FRAME_ID,
            "max_depth_m": max((p["max_depth_m"] for p in parts), default=0.0),
            "wet_pixels": int(sum(p["wet_pixels"] for p in parts)),
            "parts": parts,
        }
        # 单帧静态产品：max 分幅 = 同一幅图（schema 要求 images.json 必有 max）
        max_parts, _max_bytes = _render_parts(
            rgba, windows, out_dir=frames_dir, stem="max", image_prefix=image_prefix,
            grid=grid, tr_fn=tr.transform, depth=render_arr,
            transparent_below_m=style.transparent_below)
        max_entry = {
            "max_depth_m": max((p["max_depth_m"] for p in max_parts), default=0.0),
            "wet_pixels": int(sum(p["wet_pixels"] for p in max_parts)),
            "parts": max_parts,
        }
        png_bytes.extend(p["bytes"] for p in max_parts)
        del rgba

    split = paint_mod.choose_split(grid, tr.transform, tol_px=paint_mod.DEFAULT_TOL_PX)
    if (split["split_rows"], split["split_cols"]) != (split_rows, split_cols):
        warnings.append("产品层 split 与案例主 run 不一致（同网格应一致）")
    images_doc = paint_mod.build_images_doc(
        case_id=case_id, run_id=product_id, grid=grid,
        classes=style.depth_classes, transparent_below_m=style.transparent_below,
        split_rows=split_rows, split_cols=split_cols, frames=[frame_entry],
        max_entry=max_entry, kind="depth_frames",
    )
    images_doc["color_scale"]["note"] = (
        f"研究性产品固定分级（单位 {style.unit}）；{style.not_depth_note}"
        + ("；精确 0 h 渲染进 0–6 h 分级（渲染值 1e-4），数值块保真 0"
           if style.render_zero_epsilon else "")
    )
    images_doc["spatial_validation"] = {
        "gate_px": split["tol_px"],
        "max_px": round(split["max_px"], 6),
        "max_m": round(split["max_m"], 4),
        "samples_per_part": split["samples_per_part"],
        "method": "像元中心 → EPSG:4326 → Web Mercator → part 四角双线性反算 → 回原生像元坐标",
        "parts": [
            {"id": f"r{p['part_row']}c{p['part_col']}", "max_px": round(p["max_px"], 6),
             "max_m": round(p["max_m"], 4)}
            for p in split["parts"]
        ],
        "attempts": split["attempts"],
        "passed": True,
    }
    images_doc["product"] = {
        "kind": product.kind,
        "unit": style.unit,
        "value_semantics": style.value_semantics,
        "not_depth_note": style.not_depth_note,
    }

    tif_sha = sha256_file(product.source_tif)
    frame_info = SimpleNamespace(
        seq=0, frame_id=FRAME_ID, sha256=tif_sha,
        header=_grid_header_dict(grid), size_bytes=product.source_tif.stat().st_size,
    )
    product_run = RunConfig(
        run_id=product_id, label=product.label, role="product",
        source_dir=product.source_tif.parent,
        save_interval_s=float(product.save_interval_s),
        simulation_duration_s=float(product.save_interval_s),
        time_origin=None,
        origin_status="relative_only",
        origin_note="研究性产品图层：相对时刻 T+h；单帧静态派生，无绝对时轴",
        notes=product.notes,
    )
    timeline = build_timeline(
        case, product_run, [frame_info],
        images_prefix=image_prefix,
        blocks_prefix=f"numeric/{case_id}/{product_id}",
        statistics_path=f"cases/{case_id}/{product_id}/statistics.json",
        numeric_manifest_path=f"numeric/{case_id}/{product_id}/manifest.json",
    )
    timeline["product"] = {
        "kind": product.kind,
        "unit": style.unit,
        "value_semantics": style.value_semantics,
        "single_frame_static": True,
        "note": "单帧静态产品：frame sim_seconds=0 仅作契约占位；产品无时间轴",
    }

    audit_rel = product.audit
    derivation: dict[str, Any] = {
        "research_product": True,
        "not_business_warning": True,
        "adr": product.adr,
        "audit": audit_rel,
        "source_sha256": tif_sha,
        "source_run": product.source_run,
    }
    statistics = products_mod.product_stats_doc(
        case_id=case_id, product_id=product_id, arr=arr,
        cell_area_m2=grid.cell_area_m2,
        thresholds=[float(t) for t in config.thresholds_m],
        style=style, derivation=derivation,
    )
    numeric_manifest = blocks_mod.build_numeric_manifest(
        case_id=case_id, run_id=product_id, grid=grid, block_size=block_size,
        frames=[frame_info], mask_png_rel=mask_rel, sea_cell_count=0, has_max=False,
    )
    numeric_manifest["product"] = {
        "kind": product.kind,
        "note": "数值块保存产品真值（" + style.unit + "）；点查直接读块，不走 PNG 颜色",
    }

    # 质量门引用：优先配置的源 run（须是案例配置 run），否则显式 mass_gate_run_path
    mass: dict[str, Any] = {}
    if product.source_run and product.source_run in case.runs:
        mass = load_mass_gate(config.parent_root, _run_rel(config, case.runs[product.source_run]))
    elif product.mass_gate_run_path:
        mass = load_mass_gate(config.parent_root, product.mass_gate_run_path)

    run_entry: dict[str, Any] = {
        "run_id": product_id,
        "label": product.label,
        "role": "product",
        "frame_count": 1,
        "save_interval_s": float(product.save_interval_s),
        "simulation_duration_s": float(product.save_interval_s),
        "timeline": f"cases/{case_id}/{product_id}/timeline.json",
        "statistics": f"cases/{case_id}/{product_id}/statistics.json",
        "numeric_manifest": f"numeric/{case_id}/{product_id}/manifest.json",
        "optional": product.optional,
        "notes": product.notes,
        "quality": {
            "mass_gate": mass.get("mass_gate", "unrecorded"),
            "e_mass": mass.get("e_mass"),
            "validation_status": case.quality.get("validation_status", ""),
            "vertical_datum_status": case.quality.get("vertical_datum_status", ""),
            "warnings": list(case.quality.get("warnings") or []),
            "mass_gate_cause": mass.get("cause"),
            "research_product": True,
            "not_business_warning": True,
        },
        "product": {
            "kind": product.kind,
            "unit": style.unit,
            "value_semantics": style.value_semantics,
            "not_depth_note": style.not_depth_note,
            "source_sha256": tif_sha,
            "source_bytes": int(product.source_tif.stat().st_size),
            "audit": audit_rel,
            "adr": product.adr,
            "source_run": product.source_run,
            "derivation": derivation,
        },
        "grid_hash": grid_hash(grid),
    }

    per_run = {
        "frame_count": 1,
        "split": {"rows": split_rows, "cols": split_cols},
        "spatial": {
            "split": {"rows": split_rows, "cols": split_cols},
            "parts": [
                {"id": f"r{w.part_row}c{w.part_col}", "max_px": round(split["max_px"], 6),
                 "max_m": round(split["max_m"], 4)}
                for w in windows
            ],
            "max_px": round(split["max_px"], 6),
            "max_m": round(split["max_m"], 4),
            "samples": int(split["samples_per_part"]) * len(windows),
            "gate_px": split["tol_px"],
            "passed": bool(split["max_px"] <= split["tol_px"]),
            "attempts": split["attempts"],
        },
        "png_count": len(png_bytes),
        "png_bytes_total": sum(png_bytes),
        "png_bytes_min": min(png_bytes) if png_bytes else 0,
        "png_bytes_median": int(np.median(png_bytes)) if png_bytes else 0,
        "png_bytes_max": max(png_bytes) if png_bytes else 0,
        "numeric_bytes": int(block_info["bytes"]),
        "numeric_files": int(block_info["block_files"]),
        "mask_bytes": mask_bytes,
        "max_crosscheck": {},
        "sea_cell_count": 0,
        "seconds": round(time.perf_counter() - t0, 3),
        "warnings": warnings,
    }

    _write_json(bundle / run_entry["timeline"], timeline)
    _write_json(bundle / run_entry["statistics"], statistics)
    _write_json(bundle / run_entry["numeric_manifest"], numeric_manifest)
    if not skip_tiles:
        _write_json(bundle / image_prefix / "images.json", images_doc)
    return run_entry, per_run


# --------------------------------------------------------------------------- #
# 地形分层：源地形（未修改）与地形修改证据
# --------------------------------------------------------------------------- #
TERRAIN_SOURCE_MAX_EDGE = 2048
TERRAIN_EDIT_COLORS = (
    # (label, rgb, 说明)
    ("河道刻蚀（情景假设）", (214, 48, 49), "把地形下切成河槽；非实测断面"),
    ("空洞填补", (0, 137, 123), "源 DEM 0 值/空洞经邻域插值"),
    ("海面或零值钳制", (30, 136, 229), "负高程或海面像元被改写"),
)


def _terrain_edit_raster(
    case: CaseConfig, run: RunConfig, grid: Grid, bundle: Path
) -> tuple[np.ndarray, np.ndarray] | None:
    """返回 (编辑掩膜 bool, 高程差 float) 于模型网格；无留痕证据时返回 None。

    只用 run 目录的地形修改留痕文件（掩膜 + 差异栅格，或旧版差异文本）。
    不从「模型地形 减 源 DEM」反推：两者的重采样相位差会被误判成大量假修改。
    """
    mask_path = run.source_dir / "dem_edit_mask.tif"
    diff_path = run.source_dir / "dem_edit_diff.tif"
    if mask_path.is_file() and diff_path.is_file():
        with rasterio.open(mask_path) as ms, rasterio.open(diff_path) as ds_:
            mask = np.zeros((grid.rows, grid.cols), dtype="float32")
            diff = np.zeros((grid.rows, grid.cols), dtype="float32")
            reproject(ms.read(1).astype("float32"), mask, src_transform=ms.transform,
                      src_crs=ms.crs, dst_transform=grid.transform, dst_crs=grid.crs,
                      resampling=Resampling.nearest)
            reproject(ds_.read(1).astype("float32"), diff, src_transform=ds_.transform,
                      src_crs=ds_.crs, dst_transform=grid.transform, dst_crs=grid.crs,
                      resampling=Resampling.nearest)
        return mask > 0.5, diff

    text = run.source_dir / "dem_pre_carve.diff.txt"
    if not text.is_file():
        return None

    lines = text.read_text(encoding="utf-8", errors="replace").splitlines()
    header = {}
    body_start = 0
    for i, line in enumerate(lines):
        parts = line.split()
        if len(parts) >= 2 and parts[0].lower() in (
            "ncols", "nrows", "xllcorner", "yllcorner", "xllcenter",
            "yllcenter", "cellsize", "nodata_value",
        ):
            header[parts[0].lower()] = parts[1]
            body_start = i + 1
        else:
            break
    if "ncols" not in header or "cellsize" not in header:
        return None
    diff_src = np.loadtxt(text, skiprows=body_start)
    cell = float(header["cellsize"])
    xll = float(header.get("xllcorner") or header.get("xllcenter"))
    yll = float(header.get("yllcorner") or header.get("yllcenter"))
    src_transform = Affine(cell, 0, xll, 0, -cell, yll + diff_src.shape[0] * cell)
    diff = np.zeros((grid.rows, grid.cols), dtype="float32")
    reproject(diff_src.astype("float32"), diff, src_transform=src_transform,
              src_crs=case.crs, dst_transform=grid.transform, dst_crs=grid.crs,
              resampling=Resampling.nearest)
    return np.abs(diff) > 1e-6, diff


def _process_terrain_source(
    config: Config,
    case: CaseConfig,
    grid: Grid,
    tr: Transformer,
    bundle: Path,
) -> dict[str, Any]:
    """渲染原始源 DEM（未裁剪），采用 ArcGIS Elevation 经典色带；返回 layer doc（含 observed 范围）。"""
    path = (case.terrain or {}).get("source_dem")
    doc: dict[str, Any] = {
        "status": "missing",
        "label": (case.terrain or {}).get("source_label", "原始 DEM（未裁剪）"),
        "kind": "raster",
        "legend": "terrain_source",
        "summary": "原始源 DEM 数据，未做模型网格裁切；ArcGIS Elevation 色带渲染",
        "note": (case.terrain or {}).get("source_note", ""),
    }
    if not path or not Path(path).is_file():
        doc["summary"] = "未配置或缺少源地形栅格"
        return doc

    with rasterio.open(path) as ds:
        # 若投影与案例不同则通过 WarpedVRT 重投影
        vrt = WarpedVRT(ds, crs=case.crs, resampling=Resampling.average) if str(ds.crs) != str(case.crs) else ds

        try:
            max_edge = max(vrt.height, vrt.width)
            scale = min(1.0, float(TERRAIN_SOURCE_MAX_EDGE) / float(max_edge))
            out_rows = max(1, round(vrt.height * scale))
            out_cols = max(1, round(vrt.width * scale))
            arr = vrt.read(1, out_shape=(out_rows, out_cols), resampling=Resampling.average).astype("float64")

            nodata = vrt.nodata
            if nodata is not None:
                arr = np.where(arr == nodata, np.nan, arr)
            arr = np.where(arr <= -9000.0, np.nan, arr)
            arr = np.where(np.isfinite(arr), arr, np.nan)

            left, bottom, right, top = vrt.bounds.left, vrt.bounds.bottom, vrt.bounds.right, vrt.bounds.top
            cell_x = (right - left) / out_cols
            cell_y = (top - bottom) / out_rows
            out_transform = Affine(cell_x, 0.0, left, 0.0, -cell_y, top)

            # MapLibre image source coordinates format: [NW, NE, SE, SW] in lon/lat
            nw = tr.transform(left, top)
            ne = tr.transform(right, top)
            se = tr.transform(right, bottom)
            sw = tr.transform(left, bottom)
            corners = [
                [round(float(nw[0]), 7), round(float(nw[1]), 7)],
                [round(float(ne[0]), 7), round(float(ne[1]), 7)],
                [round(float(se[0]), 7), round(float(se[1]), 7)],
                [round(float(sw[0]), 7), round(float(sw[1]), 7)],
            ]
        finally:
            if vrt is not ds:
                vrt.close()

    finite = np.isfinite(arr)
    if not finite.any():
        doc["summary"] = "源地形无有效高程值"
        return doc
    values = arr[finite]
    # 色带自适应：p1–p99，避免高空区饱和、低值异常被放大
    vmin = float(np.percentile(values, 1))
    vmax = float(np.percentile(values, 99))
    if not (vmax > vmin):
        vmax = vmin + 1.0
    stops_def = (
        (config.legends.get("terrain_source") or {}).get("stops")
        or (config.legends.get("terrain") or {}).get("stops")
        or getattr(paint_mod, "DEFAULT_ARCGIS_ELEVATION_STOPS", ["#267300", "#ffd300", "#ffffff"])
    )
    stops = paint_mod.parse_color_stops(stops_def)
    rgba = paint_mod.colorize_gradient(arr, stops, value_min=vmin, value_max=vmax)
    out_dir = bundle / "frames" / case.case_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "dem_source.png"
    Image.fromarray(rgba, mode="RGBA").save(out_path, format="PNG", optimize=True)

    doc.update({
        "status": "ready",
        "image": _rel_posix(out_path, bundle),
        "geometry": {
            "crs": case.crs,
            "rows": int(out_rows),
            "cols": int(out_cols),
            "transform": [float(v) for v in out_transform],
            "coordinates": corners,
        },
        "value_min_m": round(vmin, 2),
        "value_max_m": round(vmax, 2),
        "observed_min_m": round(float(values.min()), 2),
        "observed_max_m": round(float(values.max()), 2),
        "clip_percentiles": [1, 99],
        "source_label": (case.terrain or {}).get("source_label", "原始 DEM（未裁剪）"),
        "source_resolution_note": "原始源 DEM 数据，未做模型网格裁切；采用 ArcGIS Elevation 色带",
        "hint": "该层为未经裁切的原始数字高程（DEM）底图，采用 ArcGIS Elevation 经典色带显示完整地貌形态",
    })
    return doc


def _terrain_edit_from_meta(run: RunConfig) -> dict[str, Any] | None:
    """run 无栅格留痕时，从 meta.json 读出已登记的地形修改计数。

    只给数字，不造栅格：没有逐像元证据就不应画「修改范围」。
    """
    meta_path = run.source_dir / "meta.json"
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    out: dict[str, Any] = {}
    channel = meta.get("channel") or {}
    if channel.get("carved_px"):
        out["carved_cells_registered"] = int(channel["carved_px"])
        if channel.get("width_actual_m"):
            out["channel_width_m"] = float(channel["width_actual_m"])
        if channel.get("method_version"):
            out["channel_method_version"] = str(channel["method_version"])
    if meta.get("filled_px"):
        out["filled_cells_registered"] = int(meta["filled_px"])
    if meta.get("sea_px"):
        out["sea_clamped_cells_registered"] = int(meta["sea_px"])
    if meta.get("unfilled_px"):
        out["unfilled_cells_registered"] = int(meta["unfilled_px"])
    if meta.get("dem_v2"):
        out["dem_note_registered"] = str(meta["dem_v2"])
    return out or None


def _process_terrain_edit(
    config: Config,
    case: CaseConfig,
    run: RunConfig,
    grid: Grid,
    tr: Transformer,
    bundle: Path,
) -> dict[str, Any]:
    """渲染地形修改掩膜，返回 layer doc。"""
    doc: dict[str, Any] = {
        "status": "missing",
        "label": "地形修改证据（模型预处理）",
        "kind": "raster",
        "legend": "terrain_edit",
        "summary": "被模型预处理改写的像元；不是观测，也不是地形误差图",
    }
    evidence = _terrain_edit_raster(case, run, grid, bundle)
    if evidence is None:
        registered = _terrain_edit_from_meta(run)
        # 无逐像元证据 = 没有可显示的栅格，状态必须是 missing（不能标 partial 让前端当可用）
        doc["status"] = "missing"
        doc["summary"] = "该 run 未产出逐像元地形修改留痕，无法绘制修改范围"
        if registered:
            doc["registered_counts"] = registered
            doc["note"] = (
                "meta.json 登记了修改计数，但没有逐像元证据，因此不生成掩膜图，"
                "避免暗示修改范围。用新版预处理脚本重跑后会输出逐像元修改掩膜。"
            )
        return doc
    mask, diff = evidence
    if not mask.any():
        doc["status"] = "ready"
        doc["summary"] = "该 run 未修改任何地形像元"
        doc["edited_cells"] = 0
        return doc

    rgba = np.zeros((grid.rows, grid.cols, 4), dtype=np.uint8)
    # 只有一类证据时全部记为刻蚀；有符号差异小于 0 视为下切
    carve = mask & (diff < 0)
    fill = mask & (diff > 0)
    rgba[carve] = (*TERRAIN_EDIT_COLORS[0][1], 255)
    rgba[fill] = (*TERRAIN_EDIT_COLORS[1][1], 255)
    out_dir = bundle / "frames" / case.case_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "terrain_edit.png"
    Image.fromarray(rgba, mode="RGBA").save(out_path, format="PNG", optimize=True)
    corners = paint_mod.part_corners_lonlat(
        grid, paint_mod.part_windows(grid.rows, grid.cols, 1, 1)[0], tr.transform
    )
    cell_area = float(grid.cell_area_m2)
    edited = diff[mask]
    doc.update({
        "status": "ready",
        "image": _rel_posix(out_path, bundle),
        "geometry": {
            "crs": grid.crs,
            "rows": int(grid.rows),
            "cols": int(grid.cols),
            "transform": [float(v) for v in grid.transform],
            "coordinates": corners,
        },
        "edited_cells": int(mask.sum()),
        "edited_area_km2": round(float(mask.sum()) * cell_area / 1e6, 3),
        "carved_cells": int(carve.sum()),
        "raised_cells": int(fill.sum()),
        "diff_min_m": round(float(edited.min()), 2),
        "diff_p50_m": round(float(np.median(edited)), 2),
        "diff_max_m": round(float(edited.max()), 2),
        "classes": [
            {"label": lbl, "color": "#{:02x}{:02x}{:02x}".format(*rgb), "note": note}
            for lbl, rgb, note in TERRAIN_EDIT_COLORS[:2]
        ],
        "summary": (
            f"预处理修改 {int(mask.sum())} 像元 / {mask.sum() * cell_area / 1e6:.2f} km²；"
            f"最大下切 {abs(float(edited.min())):.1f} m"
        ),
        "audit": "详细门禁判定见父项目 data/catalog/dem_channel_audit.json",
    })
    return doc


# --------------------------------------------------------------------------- #
# DEM（每案例一份，网格 = 默认 run）
# --------------------------------------------------------------------------- #
def _process_dem(
    config: Config,
    case: CaseConfig,
    run: RunConfig,
    grid: Grid,
    tr: Transformer,
    windows: Sequence[paint_mod.PartWindow],
    split_rows: int,
    split_cols: int,
    bundle: Path,
) -> tuple[list[dict[str, Any]], str, tuple[float, float]]:
    """渲染 DEM（连续色带、最近邻原值、NaN 透明）；返回 (parts, images_rel, (min,max))。"""
    dem_images_rel = f"frames/{case.case_id}/dem_images.json"
    legend = dict(config.legends.get("terrain") or {})
    value_min = float(legend.get("value_min_m", 0.0))
    value_max = float(legend.get("value_max_m", 300.0))
    candidates = [run.source_dir / "dem.asc", run.source_dir / "results" / "res.dem"]
    dem_path = next((p for p in candidates if p.is_file()), None)
    if dem_path is None:
        return [], dem_images_rel, (value_min, value_max)

    header = read_ascii_header(dem_path)
    dem_grid = header_to_grid(header, case.crs)
    if (dem_grid.rows, dem_grid.cols) != (grid.rows, grid.cols):
        raise PipelineError(
            f"{case.case_id} DEM 网格 {dem_grid.cols}x{dem_grid.rows} 与模型网格 "
            f"{grid.cols}x{grid.rows} 不一致（不重采样造高）"
        )
    dem = read_native_frame(dem_path)
    finite = np.isfinite(dem)
    observed = (
        (round(float(np.nanmin(dem)), 4), round(float(np.nanmax(dem)), 4)) if finite.any() else (value_min, value_max)
    )
    if legend.get("auto_range") and finite.any():
        valid_dem = dem[finite]
        value_min = round(float(np.percentile(valid_dem, 1)), 2)
        value_max = round(float(np.percentile(valid_dem, 99)), 2)
        if not (value_max > value_min):
            value_max = value_min + 1.0
    stops_def = legend.get("stops") or getattr(
        paint_mod, "DEFAULT_ARCGIS_ELEVATION_STOPS", ["#267300", "#ffd300", "#ffffff"]
    )
    stops = paint_mod.parse_color_stops(stops_def)
    rgba = paint_mod.colorize_gradient(dem, stops, value_min=value_min, value_max=value_max)
    parts, _ = _render_parts(
        rgba,
        windows,
        out_dir=bundle / "frames" / case.case_id,
        stem="dem",
        image_prefix=f"frames/{case.case_id}",
        grid=grid,
        tr_fn=tr.transform,
        depth=dem,
        transparent_below_m=float("-inf"),
    )
    doc = {
        "schema_version": "1.0",
        "case_id": case.case_id,
        "run_id": None,
        "kind": "single",
        "grid": {
            "crs": grid.crs,
            "rows": int(grid.rows),
            "cols": int(grid.cols),
            "transform": [float(v) for v in grid.transform],
            "cell_size_m": float(grid.cell_size_m),
            "cell_area_m2": float(grid.cell_area_m2),
            "coordinates": paint_mod.part_corners_lonlat(
                grid, paint_mod.part_windows(grid.rows, grid.cols, 1, 1)[0], tr.transform
            ),
        },
        "color_scale": {
            "type": "gradient",
            "stops": list(stops_def),
            "value_min_m": value_min,
            "value_max_m": value_max,
            "observed_min_m": observed[0],
            "observed_max_m": observed[1],
            "note": "连续色带；NaN 透明；最近邻原值（未双线性造高）",
        },
        "split": {"rows": split_rows, "cols": split_cols},
        "source": "dem.asc（模型输入 DEM，垂直基准未闭合 G7）",
        "parts": parts,
    }
    _write_json(bundle / dem_images_rel, doc)
    return parts, dem_images_rel, (value_min, value_max)


# --------------------------------------------------------------------------- #
# 资产（SAR / 光学）
# --------------------------------------------------------------------------- #
def load_assets_manifest(config: Config) -> dict[str, Any] | None:
    path = config.root / "data" / "processed" / "assets" / "manifest.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):  # pragma: no cover - 上游产物损坏
        return None


def _resolve_asset_path(config: Config, value: Any) -> Path | None:
    if not value:
        return None
    src = Path(str(value))
    if src.is_absolute():
        return src if src.is_file() else None
    for base in (config.root, config.parent_root):
        candidate = base / src
        if candidate.is_file():
            return candidate
    return None


def _asset_source_image(asset: dict[str, Any]) -> Any:
    """兼容 optical 组的两种写法：`png.path`（当前）或 `image`/`file`。"""
    png = asset.get("png")
    if isinstance(png, dict) and png.get("path"):
        return png["path"]
    return asset.get("image") or asset.get("file") or asset.get("source_png")


def _asset_source_footprint(asset: dict[str, Any]) -> Any:
    return (
        asset.get("footprint_geojson")
        or asset.get("footprint")
        or asset.get("footprint_file")
    )


def _asset_note(asset: dict[str, Any]) -> str:
    """用资产实测信息合成人类可读 note（覆盖配置里的通用默认文案）。"""
    parts: list[str] = []
    acquisition = asset.get("acquisition") or {}
    observed = acquisition.get("observed_state")
    moment = acquisition.get("datetime_local") or acquisition.get("datetime_utc")
    if moment:
        text = f"采集 {moment}"
        if observed:
            text += f"（{observed}）"
        parts.append(text)
    elif observed:
        parts.append(f"采集时刻未核实（{observed}）")

    coverage = asset.get("coverage") or {}
    pct = coverage.get("coverage_percent_of_model_grid")
    if pct is not None:
        if float(pct) >= 99.5:
            parts.append(f"覆盖模型域 {float(pct):.1f}%")
        else:
            parts.append(f"仅覆盖模型域 {float(pct):.2f}%（必须显示足迹，不得宣称全覆盖）")
    water_area = coverage.get("water_area_km2")
    if water_area is not None:
        parts.append(f"二值水体像元 {coverage.get('water_cells')} 格≈{water_area} km²（显示参考，不是淹没面积）")

    parts.append("SAR 不是水深，不能共用深度图例")
    license_doc = asset.get("license") or {}
    attribution = license_doc.get("attribution_modified")
    if attribution:
        parts.append(f"署名：{attribution}")
    note = asset.get("note")
    if note and "不是水深" not in note:
        parts.append(note)
    return "；".join(parts)


def _normalise_asset(asset: dict[str, Any]) -> dict[str, Any]:
    """把 prepare_optical 产出的资产记录归一化成 apply_assets 认识的字段。"""
    copy = dict(asset)
    # 1) catalog_entry 打底（label/summary/kind/legend/role/note/asset_id/acquired_at…）
    entry = asset.get("catalog_entry") or {}
    for key, value in entry.items():
        if key in {"image", "footprint"}:
            continue
        copy.setdefault(key, value)
    # 2) 采集时刻 → acquired_at
    acquisition = asset.get("acquisition") or {}
    if not copy.get("acquired_at"):
        copy["acquired_at"] = (
            acquisition.get("datetime_local") or acquisition.get("datetime_utc")
        )
    # 3) PNG 元信息 → 图像几何/尺寸/字节
    png = asset.get("png") or {}
    if png.get("coordinates_tl_tr_br_bl"):
        copy["coordinates"] = png["coordinates_tl_tr_br_bl"]
    for dim in ("width", "height", "bytes", "sha256"):
        if png.get(dim) is not None:
            copy[dim] = png[dim]
    # 4) 实测 note（覆盖配置默认文案）
    layer_id = str(asset.get("layer"))
    if layer_id == "optical":
        copy["note"] = _optical_note(asset)
        copy.setdefault("role", "background")
    elif layer_id == "sar":
        copy["note"] = _asset_note(asset)
    return copy


def _stash_assets(
    config: Config,
    bundle: Path,
    assets_manifest: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """把资产清单里的 PNG/足迹复制进发布包（只复制小图，不复制原始 TIFF/整景）。

    资产清单来自 `scripts/prepare_optical.py`（optical 组），字段形如
    `{case_id, layer, asset_id, status, png:{path,width,height,coordinates_tl_tr_br_bl},
      footprint_geojson, coverage, license, catalog_entry}`。
    本函数把 PNG/足迹落到包内约定位置，并把 `catalog_entry`/实测元信息归一化，
    供 `catalog.apply_assets()` 合并进 layers。
    """
    if not assets_manifest:
        return None
    items: list[dict[str, Any]] = []
    for asset in assets_manifest.get("assets", []):
        case_id = str(asset.get("case_id", ""))
        layer = str(asset.get("layer", ""))
        asset_id = str(asset.get("asset_id", ""))
        copy = _normalise_asset(asset)
        if layer not in {"sar", "optical"}:
            items.append(copy)
            continue
        if layer == "sar":
            target_img = bundle / "frames" / case_id / "sar.png"
            target_fp = bundle / "cases" / case_id / "sar_footprint.geojson"
        else:
            target_img = bundle / "frames" / case_id / "optical" / f"{asset_id}.png"
            target_fp = bundle / "cases" / case_id / f"optical_{asset_id}_footprint.geojson"

        src_img_value = _asset_source_image(asset)
        src_img = _resolve_asset_path(config, src_img_value)
        if src_img is not None:
            target_img.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_img, target_img)
            copy["image"] = _rel_posix(target_img, bundle)
        elif copy.get("status") in {"ready", "partial"}:
            copy["status"] = "failed"
            copy["summary"] = f"资产图片缺失，未能导入发布包：{src_img_value}"
            copy.pop("image", None)
            copy.setdefault("qa", {})["status_reason"] = f"PNG 源文件缺失：{src_img_value}"
        else:
            copy.pop("image", None)

        src_fp = _resolve_asset_path(config, _asset_source_footprint(asset))
        if src_fp is not None:
            target_fp.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_fp, target_fp)
            copy["footprint"] = _rel_posix(target_fp, bundle)
        else:
            copy.pop("footprint", None)
            if layer == "sar" and copy.get("status") in {"ready", "partial"}:
                copy["status"] = "failed"
                copy["summary"] = "SAR 足迹缺失，不能发布（partial 必须画足迹）"
                copy.pop("image", None)
        items.append(copy)
    out = dict(assets_manifest)
    out["assets"] = items
    return out


# --------------------------------------------------------------------------- #
# 图层文档
# --------------------------------------------------------------------------- #
def _base_layer_docs(case: CaseConfig) -> dict[str, dict[str, Any]]:
    docs: dict[str, dict[str, Any]] = {}
    for layer_id, layer in case.layers.items():
        doc: dict[str, Any] = {"status": layer.status, "label": layer.label, "kind": layer.kind}
        if layer.summary:
            doc["summary"] = layer.summary
        if layer.note:
            doc["note"] = layer.note
        if layer.asset_id:
            doc["asset_id"] = layer.asset_id
        if layer.acquired_at:
            doc["acquired_at"] = layer.acquired_at
        docs[layer_id] = doc
    return docs


def _layer_docs_for_run(
    case: CaseConfig,
    run: RunConfig,
    *,
    geom: dict[str, Any],
    images_rel: str,
    run_result: RunResult,
    dem_parts: list[dict[str, Any]],
    dem_images_rel: str,
    dem_values: tuple[float, float],
    terrain_source_doc: dict[str, Any] | None = None,
    terrain_edit_doc: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """把配置图层状态 + 本次实际产物合成为 catalog.layers。"""
    docs = _base_layer_docs(case)
    split_rows, split_cols = run_result.split_rows, run_result.split_cols
    split_note = (
        "整幅单图"
        if (split_rows, split_cols) == (1, 1)
        else f"按整数像元边界切 {split_rows}×{split_cols} 个 part 贴图（不重采样、不插值）"
    )
    residual_note = (
        f"四角贴图残差 ≤{run_result.spatial['max_px']:.2f} 源像元（≈{run_result.spatial['max_m']:.0f} m，门禁 ≤0.5 px）"
    )

    flood = docs.setdefault("flood", {"status": "ready", "label": "模型水深（逐帧）", "kind": "frame"})
    flood["status"] = "ready"
    flood["images"] = images_rel
    flood["note"] = f"{split_note}；{residual_note}；原生网格 1:1 预着色 PNG，无 XYZ 金字塔、无插值"
    flood["frame_count"] = run_result.frame_count

    maxdoc = docs.setdefault("max", {"status": "ready", "label": "全程最大水深包络", "kind": "max_envelope"})
    if run_result.max_parts:
        maxdoc["status"] = "ready"
        maxdoc["geometry"] = geom
        maxdoc["images"] = images_rel
        maxdoc["parts"] = run_result.max_parts
        if (split_rows, split_cols) == (1, 1):
            maxdoc["image"] = run_result.max_parts[0]["path"]
        else:
            maxdoc.pop("image", None)
        maxdoc["legend"] = "depth"
        maxdoc["legend_note"] = "与当前帧水深共用同一套深度图例（catalog.display.depth_classes）"
        maxdoc["summary"] = (
            "取自 run 目录 res.max（模型自带全程最大水深包络），不是某一时刻；"
            "含 6 h 采样间隙内的子步瞬时峰值，因此可能大于任一可见帧的最大值"
        )
        maxdoc["note"] = f"{split_note}；{residual_note}"
        maxdoc["envelope_semantics"] = (
            "res.max 为模型内部子步累积包络，逐帧为 6 h 采样；res.max ≥ 逐帧采样值（单向，已验证）"
        )
    else:
        maxdoc["status"] = "missing"
        maxdoc["summary"] = "run 目录缺 results/res.max，最大包络图层不可用"

    dem = docs.setdefault("dem", {"status": "ready", "label": "DEM 高程", "kind": "raster"})
    if dem_parts:
        dem["status"] = "ready"
        dem["geometry"] = geom
        dem["legend"] = "terrain"
        dem["value_min_m"], dem["value_max_m"] = dem_values
        dem["images"] = dem_images_rel
        dem["parts"] = dem_parts
        if (split_rows, split_cols) == (1, 1):
            dem["image"] = dem_parts[0]["path"]
        else:
            dem.pop("image", None)
        dem["note"] = (
            f"模型输入 DEM（最近邻原值，未双线性造高）；垂直基准未闭合（G7）；"
            f"{split_note}；{residual_note}"
        )
    else:
        dem["status"] = "missing"
        dem["summary"] = "未找到 dem.asc / res.dem，DEM 图层不可用"

    # 模型输入地形层：改名并附地形修改摘要，避免被当作源地形真值
    dem["label"] = "模型输入地形（含预处理修改）"
    edit_note = ""
    edit_doc = terrain_edit_doc or {}
    if edit_doc.get("status") == "ready" and edit_doc.get("edited_cells"):
        edit_note = (
            f"；经预处理修改 {edit_doc['edited_cells']} 像元 "
            f"（{edit_doc.get('edited_area_km2', 0):.2f} km²，"
            f"最大下切 {abs(float(edit_doc.get('diff_min_m', 0))):.1f} m）"
        )
        dem["modification_summary"] = {
            "edited_cells": edit_doc["edited_cells"],
            "edited_area_km2": edit_doc.get("edited_area_km2"),
            "carved_cells": edit_doc.get("carved_cells"),
            "max_downcut_m": abs(float(edit_doc.get("diff_min_m", 0.0))),
            "audit_ref": "2d_hydro_project/data/catalog/dem_channel_audit.json",
        }
    elif edit_doc.get("status") == "ready":
        edit_note = "；本 run 未修改任何地形像元"
    elif edit_doc.get("registered_counts"):
        counts = edit_doc["registered_counts"]
        parts = []
        if counts.get("carved_cells_registered"):
            parts.append(f"河槽刻蚀 {counts['carved_cells_registered']} 格")
        if counts.get("filled_cells_registered"):
            parts.append(f"空洞填补 {counts['filled_cells_registered']} 格")
        if counts.get("sea_clamped_cells_registered"):
            parts.append(f"海面钳零 {counts['sea_clamped_cells_registered']} 格")
        if parts:
            edit_note = "；meta 登记的地形修改：" + "、".join(parts) + "（无逐像元掩膜）"
        dem["modification_summary"] = {
            "registered_counts_only": True,
            **counts,
        }
    dem["note"] = (
        f"本层是**模型输入地形**，可能含情景河槽刻蚀/空洞填补/钳零{edit_note}；"
        f"不是未修改的源 DEM，也不是测量地形。垂直基准未闭合（G7）；"
        f"{split_note}；{residual_note}"
    )

    if terrain_source_doc is not None:
        docs["terrain_source"] = terrain_source_doc
    if terrain_edit_doc is not None:
        docs["terrain_edit"] = terrain_edit_doc

    boundary = docs.setdefault("boundary", {"status": "ready", "label": "模型域边界", "kind": "vector"})
    boundary["status"] = "ready"
    boundary["geojson"] = f"cases/{case.case_id}/boundary.geojson"

    sites = docs.setdefault("sites", {"status": "ready", "label": "站点/参考点", "kind": "vector"})
    if case.sites:
        sites["status"] = "ready"
        sites["geojson"] = f"cases/{case.case_id}/sites.geojson"
    else:
        sites["status"] = "missing"
        sites["summary"] = "配置未提供站点/参考点"
    return docs


def _optical_note(asset: dict[str, Any]) -> str:
    """光学层 note：必须能看出"采集时间不随模拟时间变化"和"背景角色"。"""
    parts: list[str] = ["可见光背景影像（不随模拟时间滑块变化）"]
    acquisition = asset.get("acquisition") or {}
    moment = acquisition.get("datetime_local") or acquisition.get("datetime_utc")
    if moment:
        parts.append(f"采集 {moment}")
    coverage = asset.get("coverage") or {}
    pct = coverage.get("coverage_percent_of_model_grid")
    if pct is not None:
        if float(pct) >= 99.5:
            parts.append(f"有效覆盖模型域 {float(pct):.1f}%")
        else:
            parts.append(f"仅覆盖模型域 {float(pct):.2f}%（未覆盖处透明，不得宣称全覆盖）")
    if asset.get("status") == "partial":
        parts.append("背景参考，非事件同期观测")
    elif acquisition.get("observed_state"):
        parts.append(str(acquisition["observed_state"]))
    attribution = (asset.get("license") or {}).get("attribution_modified")
    if attribution:
        parts.append(f"署名：{attribution}")
    return "；".join(parts)


def _assert_assets_imported(
    case: CaseConfig,
    bundle: Path,
    layer_docs: dict[str, dict[str, Any]],
    stashed: dict[str, Any] | None,
) -> None:
    """硬门禁：资产清单说 ready/partial ⇒ catalog 图层必须 ready/partial 且图片真实存在。

    这条防住"字段名解析失败 → 静默降级成 failed/missing"的缺陷。
    """
    if not stashed:
        return
    enabled = {"ready", "partial"}
    for asset in stashed.get("assets") or []:
        if str(asset.get("case_id")) != case.case_id:
            continue
        layer_id = str(asset.get("layer"))
        if layer_id not in {"sar", "optical"}:
            continue
        expected = str(asset.get("status") or "")
        if expected not in enabled:
            continue
        doc = layer_docs.get(layer_id)
        if doc is None:
            raise PipelineError(
                f"{case.case_id}.layers.{layer_id} 缺失，但资产清单声明 {expected}；拒绝发布"
            )
        actual = str(doc.get("status"))
        if actual not in enabled:
            raise PipelineError(
                f"{case.case_id}.layers.{layer_id} 状态 {actual} 与资产清单 {expected} 不一致"
                f"（资产 id={asset.get('asset_id')}）；拒绝发布"
            )
        image = doc.get("image")
        if not image:
            raise PipelineError(
                f"{case.case_id}.layers.{layer_id} 为 {actual} 但缺 image；拒绝发布"
            )
        if not (bundle / str(image)).is_file():
            raise PipelineError(
                f"{case.case_id}.layers.{layer_id}.image 文件不存在：{image}；拒绝发布"
            )
        footprint = doc.get("footprint")
        if layer_id == "sar" and not footprint:
            raise PipelineError(
                f"{case.case_id}.layers.sar 缺 footprint（SAR 必须画真实足迹）；拒绝发布"
            )
        if footprint and not (bundle / str(footprint)).is_file():
            raise PipelineError(
                f"{case.case_id}.layers.{layer_id}.footprint 文件不存在：{footprint}；拒绝发布"
            )
        if not doc.get("note"):
            raise PipelineError(
                f"{case.case_id}.layers.{layer_id} 缺 note（必须带实测采集/覆盖/署名信息）；拒绝发布"
            )


def _write_boundary_geojson(case: CaseConfig, grid: Grid, tr: Transformer, path: Path) -> int:
    corners = [tr.transform(x, y) for x, y in grid.corners_native()]
    ring = [[round(float(lon), 7), round(float(lat), 7)] for lon, lat in corners]
    ring.append(ring[0])
    doc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "case_id": case.case_id,
                    "name": f"{case.name}模型域",
                    "kind": "model_domain",
                    "crs": case.crs,
                    "rows": grid.rows,
                    "cols": grid.cols,
                    "note": "模型域外框（四角），非行政边界",
                },
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
        ],
    }
    return _write_json(path, doc)


def _write_sites_geojson(case: CaseConfig, path: Path) -> int:
    features = []
    for site in case.sites:
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "name": site.get("name", ""),
                    "label": site.get("label", ""),
                    "case_id": case.case_id,
                },
                "geometry": {
                    "type": "Point",
                    "coordinates": [round(float(site["lon"]), 7), round(float(site["lat"]), 7)],
                },
            }
        )
    return _write_json(path, {"type": "FeatureCollection", "features": features})


# --------------------------------------------------------------------------- #
# 案例 & 全包
# --------------------------------------------------------------------------- #
def _select_runs(case: CaseConfig, only_runs: Sequence[str] | None = None) -> list[RunConfig]:
    if only_runs:
        missing = [r for r in only_runs if r not in case.runs]
        if missing:
            raise PipelineError(f"{case.case_id} 无 run {missing}")
        return [case.runs[r] for r in only_runs]
    ordered = [case.runs[case.default_run_id]]
    for run in case.runs.values():
        if run.run_id in {r.run_id for r in ordered} or run.optional:
            continue
        ordered.append(run)
    return ordered


def _build_case(
    config: Config,
    case: CaseConfig,
    bundle: Path,
    *,
    only_runs: Sequence[str] | None,
    frames_limit: int | None,
    skip_tiles: bool,
    classes: list[paint_mod.DepthClass],
    transparent_below_m: float,
    stats: BuildStats,
) -> dict[str, Any]:
    t_case = time.perf_counter()
    runs = _select_runs(case, only_runs)
    run_entries: list[dict[str, Any]] = []
    first_result: RunResult | None = None
    grid: Grid | None = None
    tr: Transformer | None = None
    geom: dict[str, Any] | None = None
    dem_parts: list[dict[str, Any]] = []
    dem_images_rel = f"frames/{case.case_id}/dem_images.json"
    dem_values = (0.0, 300.0)
    terrain_source_doc: dict[str, Any] | None = None
    terrain_edit_doc: dict[str, Any] | None = None

    for run in runs:
        result = _process_run(
            config,
            case,
            run,
            bundle,
            frames_limit=frames_limit,
            skip_tiles=skip_tiles,
            classes=classes,
            transparent_below_m=transparent_below_m,
        )
        mass = load_mass_gate(config.parent_root, _run_rel(config, run))
        # 计划 §3.2 契约：model_version / scenario_role / source_asset_ids / source_hashes
        audit_frames = audit_run(
            run.source_dir,
            save_interval_s=run.save_interval_s,
            simulation_duration_s=run.simulation_duration_s,
            expected_count=run.expected_frame_count,
        ).frames
        provenance = provenance_for_run(config, case, run, audit_frames[: result.frame_count])
        run_entry = build_run_entry(
            case,
            {
                "run_id": run.run_id,
                "label": run.label,
                "role": run.role,
                "frame_count": result.frame_count,
                "save_interval_s": float(run.save_interval_s),
                "simulation_duration_s": float(run.simulation_duration_s),
                "timeline": f"cases/{case.case_id}/{run.run_id}/timeline.json",
                "statistics": f"cases/{case.case_id}/{run.run_id}/statistics.json",
                "numeric_manifest": f"numeric/{case.case_id}/{run.run_id}/manifest.json",
                "optional": run.optional,
                "notes": run.notes,
            },
            mass,
        )
        run_entry.update(provenance)
        run_entry["grid_hash"] = result.grid_hash
        if result.repro:
            run_entry["reproducibility_crosscheck"] = result.repro
        run_entries.append(run_entry)
        stats.per_run[f"{case.case_id}/{run.run_id}"] = {
            "frame_count": result.frame_count,
            "split": {"rows": result.split_rows, "cols": result.split_cols},
            "spatial": result.spatial,
            "png_count": len(result.png_bytes),
            "png_bytes_total": sum(result.png_bytes),
            "png_bytes_min": min(result.png_bytes) if result.png_bytes else 0,
            "png_bytes_median": int(np.median(result.png_bytes)) if result.png_bytes else 0,
            "png_bytes_max": max(result.png_bytes) if result.png_bytes else 0,
            "numeric_bytes": result.numeric_bytes,
            "numeric_files": result.numeric_files,
            "mask_bytes": result.mask_bytes,
            "max_crosscheck": result.max_crosscheck,
            "sea_cell_count": result.sea_cell_count,
            "seconds": round(result.seconds, 3),
            "warnings": list(result.warnings),
        }
        stats.frames_total += result.frame_count
        stats.png_total += len(result.png_bytes)
        stats.png_bytes_total += sum(result.png_bytes)
        stats.numeric_bytes_total += result.numeric_bytes

        if first_result is None:
            first_result = result
            header = read_ascii_header(_frame_path(run, 0))
            grid = header_to_grid(header, case.crs)
            tr = _transformer(case.crs)
            geom = geometry_for_grid(grid, crs_to_wgs84=tr.transform)
            # 计划 §3.2 契约：网格指纹（rows/cols/transform/crs 规范化 sha256 前缀）
            geom["grid_hash"] = result.grid_hash
            geom["shape"] = [int(grid.rows), int(grid.cols)]
            if not skip_tiles:
                windows = paint_mod.part_windows(grid.rows, grid.cols, result.split_rows, result.split_cols)
                dem_parts, dem_images_rel, dem_values = _process_dem(
                    config,
                    case,
                    run,
                    grid,
                    tr,
                    windows,
                    result.split_rows,
                    result.split_cols,
                    bundle,
                )
                terrain_source_doc = _process_terrain_source(config, case, grid, tr, bundle)
                terrain_edit_doc = _process_terrain_edit(config, case, run, grid, tr, bundle)

    if first_result is None or grid is None or tr is None or geom is None:  # pragma: no cover
        raise PipelineError(f"{case.case_id}: 未构建任何 run")

    # ---- Phase H 研究性产品图层（单帧伪 run，role=product；ADR-0015~0018）----
    for product in case.product_layers:
        if product.optional:
            continue
        entry, per_run_doc = _process_product_layer(
            config, case, product, grid, tr,
            first_result.split_rows, first_result.split_cols,
            bundle, skip_tiles=skip_tiles,
        )
        run_entries.append(entry)
        stats.per_run[f"{case.case_id}/{product.product_id}"] = per_run_doc
        stats.frames_total += 1
        stats.png_total += per_run_doc["png_count"]
        stats.png_bytes_total += per_run_doc["png_bytes_total"]
        stats.numeric_bytes_total += per_run_doc["numeric_bytes"]

    assets_doc = load_assets_manifest(config)
    stashed = _stash_assets(config, bundle, assets_doc)
    layer_docs = _layer_docs_for_run(
        case,
        runs[0],
        geom=geom,
        images_rel=f"frames/{case.case_id}/{runs[0].run_id}/images.json",
        run_result=first_result,
        dem_parts=dem_parts,
        dem_images_rel=dem_images_rel,
        dem_values=dem_values,
        terrain_source_doc=terrain_source_doc,
        terrain_edit_doc=terrain_edit_doc,
    )
    layer_docs = apply_assets(case, layer_docs, stashed)
    # 资产实测的 label 比配置默认值更准确（含传感器/日期/产品类型），实测优先
    if stashed:
        asset_by_layer = {
            str(a.get("layer")): a
            for a in stashed.get("assets") or []
            if str(a.get("case_id")) == case.case_id and str(a.get("layer")) in {"sar", "optical"}
        }
        for layer_id, asset in asset_by_layer.items():
            doc = layer_docs.get(layer_id)
            if doc is not None and asset.get("label"):
                doc["label"] = str(asset["label"])
    # 资产缺失时给出人类可读原因（不写异常堆栈）
    for layer_id in ("optical", "sar"):
        doc = layer_docs.get(layer_id)
        if doc is not None and doc.get("status") in {"missing", "failed", "remote_verified", "local_unchecked"}:
            doc.setdefault("missing_reason", ASSET_MISSING_REASON)
    # 硬断言：资产清单声明 ready/partial 的图层面板必须同为 ready/partial 且图片存在，
    # 防止字段名/路径解析失败导致静默降级（v1 曾把三地 SAR 全判 failed）
    _assert_assets_imported(case, bundle, layer_docs, stashed)

    _write_boundary_geojson(case, grid, tr, bundle / "cases" / case.case_id / "boundary.geojson")
    if case.sites:
        _write_sites_geojson(case, bundle / "cases" / case.case_id / "sites.geojson")

    mass = load_mass_gate(config.parent_root, _run_rel(config, runs[0]))
    quality = dict(case.quality)
    quality["mass_gate"] = mass.get("mass_gate", "unrecorded")
    quality["e_mass"] = mass.get("e_mass")
    if mass.get("cause"):
        quality["mass_gate_cause"] = mass["cause"]
    warnings = list(quality.get("warnings") or [])
    for key in list(stats.per_run):
        if key.startswith(f"{case.case_id}/"):
            for warning in stats.per_run[key]["warnings"]:
                warnings.append(f"{key}: {warning}")
    quality["warnings"] = warnings
    quality["numeric_fidelity"] = "数值块与源 .wd 逐位一致（float32）；点查不走 PNG 颜色"

    entry = build_case_entry(
        case,
        grid_geometry=geom,
        run_entries=run_entries,
        layer_docs=layer_docs,
        quality=quality,
    )
    entry["frames_rendering"] = {
        "scheme": "native_grid_1to1_precolored_png",
        "xyz_tiles": False,
        "split": {"rows": first_result.split_rows, "cols": first_result.split_cols},
        "note": "用户 2026-10-27 决定：复用 6 h 真实推演帧，不建 XYZ 金字塔、不插值",
    }
    stats.timings[f"{case.case_id}.total_s"] = time.perf_counter() - t_case
    return entry


def _assert_assets_in_catalog(
    config: Config,
    case_ids: Sequence[str],
    case_entries: list[dict[str, Any]],
    build: Path,
) -> None:
    """包级门禁：资产清单里 ready/partial 的资产必须体现在 catalog 的对应图层上。"""
    assets_doc = load_assets_manifest(config)
    if not assets_doc:
        return
    by_case = {str(entry["case_id"]): entry for entry in case_entries}
    enabled = {"ready", "partial"}
    problems: list[str] = []
    for asset in assets_doc.get("assets") or []:
        expected = str(asset.get("status") or "")
        if expected not in enabled:
            continue
        case_id = str(asset.get("case_id"))
        layer_id = str(asset.get("layer"))
        if layer_id not in {"sar", "optical"}:
            continue
        if case_id not in case_ids:
            # --only 子集构建：不在本包的案例不参与门禁
            continue
        entry = by_case.get(case_id)
        if entry is None:  # pragma: no cover - case_ids 与 case_entries 同源
            problems.append(f"{case_id}.layers.{layer_id}: 案例未包含在本包")
            continue
        layer = (entry.get("layers") or {}).get(layer_id) or {}
        actual = str(layer.get("status"))
        if actual not in enabled:
            problems.append(
                f"{case_id}.layers.{layer_id}: 资产清单 {expected} → catalog {actual}"
            )
            continue
        image = layer.get("image")
        if not image or not (build / str(image)).is_file():
            problems.append(f"{case_id}.layers.{layer_id}: 图片缺失 {image!r}")
        if not layer.get("note"):
            problems.append(f"{case_id}.layers.{layer_id}: 缺实测 note")
    if problems:
        raise PipelineError(
            "资产导入门禁失败（不得静默降级为 missing/failed）：\n  " + "\n  ".join(problems)
        )


def prepare_bundle(
    config: Config,
    bundle_id: str,
    *,
    only: Sequence[str] | None = None,
    frames_limit: int | None = None,
    skip_tiles: bool = False,
) -> Path:
    """生成 `outputs/web_bundle/<bundle_id>/` 发布包；返回包路径。"""
    if not bundle_id or any(ch in bundle_id for ch in '\\/:*?"<>|'):
        raise PipelineError(f"非法 bundle_id：{bundle_id!r}")
    stats = BuildStats(started_utc=_dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"))
    t_start = time.perf_counter()

    case_ids = list(only) if only else list(config.case_ids)
    unknown = [c for c in case_ids if c not in config.cases]
    if unknown:
        raise PipelineError(f"未知案例：{unknown}；可选 {list(config.case_ids)}")
    if not case_ids:
        raise PipelineError("case_ids 为空")

    final = config.bundle_root / bundle_id
    # 临时目录带 PID：同名 .building 仍存在说明别的进程在构建同一 bundle，直接拒绝
    build = config.bundle_root / f".{bundle_id}{BUILD_DIR_SUFFIX}-{os.getpid()}"
    for stale in config.bundle_root.glob(f".{bundle_id}{BUILD_DIR_SUFFIX}-*"):
        if stale != build:
            raise PipelineError(
                f"检测到另一个构建进程正在写 {stale.name}（PID 见后缀）；"
                "同一 bundle_id 只允许单点构建，请等它结束或删除该临时目录"
            )
    for legacy in (config.bundle_root / f".{bundle_id}{BUILD_DIR_SUFFIX}",):
        if legacy.exists():
            _rmtree(legacy)
    _rmtree(build)
    build.mkdir(parents=True, exist_ok=True)

    classes = paint_mod.depth_classes_from_config(config)
    transparent_below_m = float(config.default_threshold_m)

    try:
        case_entries = [
            _build_case(
                config,
                config.cases[case_id],
                build,
                only_runs=None,
                frames_limit=frames_limit,
                skip_tiles=skip_tiles,
                classes=classes,
                transparent_below_m=transparent_below_m,
                stats=stats,
            )
            for case_id in case_ids
        ]

        content: dict[str, dict[str, Any]] = {}
        for item in sorted(build.rglob("*")):
            if item.is_file():
                rel = _rel_posix(item, build)
                content[rel] = {"sha256": sha256_file(item), "bytes": item.stat().st_size}

        # 包级硬门禁：资产清单声明 ready/partial 的图层，必须在每个 case 的 catalog 里同为可用。
        # （单项断言在 _assert_assets_imported，这里兜住"资产在包内但整个 case 未构建"的情况）
        _assert_assets_in_catalog(config, case_ids, case_entries, build)

        source_pairs: list[str] = []
        for case_id in case_ids:
            for run in _select_runs(config.cases[case_id]):
                numeric_path = build / "numeric" / case_id / run.run_id / "manifest.json"
                if not numeric_path.is_file():  # pragma: no cover
                    continue
                numeric = json.loads(numeric_path.read_text(encoding="utf-8"))
                for frame in numeric.get("frames", []):
                    src = _frame_path(run, int(frame["frame_id"][1:]))
                    if src.is_file():
                        source_pairs.append(f"{case_id}/{run.run_id}/{frame['frame_id']}:{sha256_file(src)}")
        source_manifest_hash = "sha256:" + _sha256_text("\n".join(sorted(source_pairs)))

        # 开发用 --only 时 default_case 可能不在包内；catalog 自检要求它必须存在
        catalog_config = config if config.default_case in case_ids else _replace(config, default_case=case_ids[0])
        catalog = build_catalog(
            catalog_config,
            bundle_id=bundle_id,
            created_utc=stats.started_utc,
            source_manifest_hash=source_manifest_hash,
            case_entries=case_entries,
        )
        if catalog_config is not config:
            catalog["dev_build_partial"] = True
            catalog["dev_note"] = (
                f"开发用子集构建（--only {list(case_ids)}）；default_case 已临时置为 {case_ids[0]}；"
                "正式发布必须全量三地"
            )
        _write_json(build / "catalog.json", catalog)
        _write_text(build / "licenses" / "LICENSES.md", LICENSE_TEXT)

        file_count, bundle_bytes = _file_bytes(build)
        stats.file_count = file_count
        stats.bundle_bytes_total = bundle_bytes
        stats.finished_utc = _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")
        stats.wall_seconds = time.perf_counter() - t_start

        manifest = build_manifest_doc(
            config,
            bundle_id=bundle_id,
            stats=stats,
            case_entries=case_entries,
            content=content,
            source_manifest_hash=source_manifest_hash,
            frames_limit=frames_limit,
            skip_tiles=skip_tiles,
            only=case_ids,
        )
        _write_json(build / "manifest.json", manifest)

        if final.exists():
            _rmtree(final)
        build.rename(final)
    except Exception:
        _rmtree(build)
        raise
    return final


# --------------------------------------------------------------------------- #
# 校验入口（转调 quality.validate，并在 full 通过后回填 manifest.validation）
# --------------------------------------------------------------------------- #
def validate_bundle(
    bundle_dir: str | Path,
    config: Config | None = None,
    full: bool = False,
    *,
    backfill: bool = True,
) -> Any:
    """校验发布包；返回 `ValidationReport`（含 `.ok` 与 `.to_dict()`）。

    `full=True` 时做数值回读、统计复算、空间复算，并把结果回填进包内
    `manifest.json` 的 `validation.numeric` / `validation.statistics` /
    `validation.max_crosscheck`（保留 `web` 占位不动）。
    """
    from .quality.validate import validate_bundle as _validate

    report = _validate(Path(bundle_dir), config, full=full)
    if full and backfill:
        try:
            backfill_manifest(Path(bundle_dir), report)
        except (OSError, ValueError, KeyError) as exc:  # pragma: no cover - 回填不应阻断校验
            report.warnings.append(f"manifest 回填失败：{exc}")
    return report


def backfill_manifest(bundle: Path, report: Any) -> Path:
    """把 validate 的数值/统计/空间门禁结果写回包根 manifest.json。"""
    bundle = Path(bundle)
    manifest_path = bundle / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"缺 {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validation = dict(manifest.get("validation") or {})
    groups = {group.name: group for group in getattr(report, "groups", [])}

    numeric = groups.get("numeric")
    if numeric is not None:
        validation["numeric"] = {
            "samples": numeric.details.get("samples", 0),
            "max_abs_err_m": numeric.details.get("max_abs_err_m"),
            "max_rel_err": numeric.details.get("max_rel_err"),
            "gate": numeric.details.get("gate"),
            "per_run": numeric.details.get("per_run"),
            "passed": bool(numeric.passed),
        }
    recompute = groups.get("statistics_recompute")
    if recompute is not None:
        validation["statistics"] = {
            "frames_checked": recompute.details.get("frames_checked", 0),
            "max_rel_diff": recompute.details.get("max_rel_diff"),
            "gate": recompute.details.get("gate"),
            "per_run": recompute.details.get("per_run"),
            "passed": bool(recompute.passed),
        }
    spatial = groups.get("spatial")
    if spatial is not None and spatial.details.get("per_run"):
        # per_run 键为 "<case>/<run>"
        for key, info in (spatial.details.get("per_run") or {}).items():
            case_id, _, run_id = key.partition("/")
            if not run_id:
                continue
            current = ((validation.get("spatial") or {}).get(case_id) or {}).get(run_id) or {}
            current.update(
                {
                    "split": info.get("split"),
                    "parts": info.get("parts"),
                    "max_px": info.get("max_px"),
                    "max_m": info.get("max_m"),
                    "samples": info.get("samples"),
                    "gate_px": info.get("gate_px"),
                    "method": (
                        "源像元中心 → EPSG:4326 → Web Mercator → part 四角双线性反算 → 回原生像元；"
                        "门禁：每 part ≤0.5 源像元"
                    ),
                    "passed": bool(info.get("passed")) and bool(current.get("passed", True)),
                }
            )
            validation.setdefault("spatial", {}).setdefault(case_id, {})[run_id] = current

    timeline = groups.get("timeline")
    structure = groups.get("structure")
    completeness = groups.get("completeness")
    images = groups.get("images")
    validation["gate_summary"] = {
        "ok": bool(getattr(report, "ok", False)),
        "full": bool(getattr(report, "full", False)),
        "groups": {
            name: {"passed": group.passed, "checks": group.checks, "failures": len(group.failures)}
            for name, group in groups.items()
        },
    }
    for label, group in (
        ("timeline", timeline),
        ("structure", structure),
        ("completeness", completeness),
        ("images", images),
    ):
        if group is not None:
            validation[f"{label}_checks"] = {
                "passed": bool(group.passed),
                "checks": group.checks,
                "failures": len(group.failures),
            }
    validation.pop("pending", None)
    validation["validated_utc"] = _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")
    manifest["validation"] = validation
    # web 占位保持原样（由 scripts/build_web.py 置 ready 并补 files）
    manifest.setdefault("web", {"ready": False, "files": []})
    _write_json(manifest_path, manifest)
    return manifest_path


def build_manifest_doc(
    config: Config,
    *,
    bundle_id: str,
    stats: BuildStats,
    case_entries: list[dict[str, Any]],
    content: dict[str, dict[str, Any]],
    source_manifest_hash: str,
    frames_limit: int | None,
    skip_tiles: bool,
    only: Sequence[str],
) -> dict[str, Any]:
    """包根 manifest.json：内容哈希 + 构建参数 + 质量摘要 + created_utc。"""
    spatial: dict[str, Any] = {}
    for key, info in stats.per_run.items():
        case_id, run_id = key.split("/", 1)
        spatial.setdefault(case_id, {})[run_id] = {
            "split": info["split"],
            "parts": info["spatial"]["parts"],
            "max_px": info["spatial"]["max_px"],
            "max_m": info["spatial"]["max_m"],
            "samples": info["spatial"]["samples"],
            "gate_px": info["spatial"]["gate_px"],
            "passed": info["spatial"]["passed"],
        }
    quality_summary = {
        entry["case_id"]: {
            "mass_gate": entry["quality"].get("mass_gate"),
            "e_mass": entry["quality"].get("e_mass"),
            "validation_status": entry["quality"].get("validation_status"),
            "vertical_datum_status": entry["quality"].get("vertical_datum_status"),
            "frame_count": {run["run_id"]: run["frame_count"] for run in entry["runs"]},
        }
        for entry in case_entries
    }
    crosscheck_by_case: dict[str, dict[str, Any]] = {}
    ge_all = True
    max_diff_by_case: dict[str, float] = {}
    for key, info in stats.per_run.items():
        case_id, run_id = key.split("/", 1)
        cross = info.get("max_crosscheck") or {}
        if not cross:
            continue
        crosscheck_by_case.setdefault(case_id, {})[run_id] = cross
        ge_all = ge_all and bool(cross.get("res_max_ge_frames"))
        max_diff_by_case.setdefault(case_id, 0.0)
        max_diff_by_case[case_id] = max(max_diff_by_case[case_id], float(cross.get("max_diff_m", 0.0)))
    max_crosscheck = {
        "res_max_ge_frames": bool(ge_all),
        "max_diff_m_by_case": {k: round(v, 4) for k, v in max_diff_by_case.items()},
        "by_run": crosscheck_by_case,
        "note": "res.max 为子步累积包络，逐帧为 6 h 采样；差异单向且物理正确（仅断言 res.max ≥ 逐帧fmax）",
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "bundle_id": bundle_id,
        "created_utc": stats.started_utc,
        "source_manifest_hash": source_manifest_hash,
        "license_notice": config.license_notice,
        "generator": "floodviz.pipeline.prepare_bundle",
        "build": {
            "params": {
                "only": list(only),
                "frames_limit": frames_limit,
                "skip_tiles": bool(skip_tiles),
                "block_size": config.block_size,
                "thresholds_m": list(config.thresholds_m),
                "default_threshold_m": config.default_threshold_m,
                "image_max_pixels": config.image_max_pixels,
                "rendering": "native_grid_1to1_precolored_png_no_xyz_pyramid",
            },
            "stats": stats.to_dict(),
            "hardware_note": "Windows 10 / Ryzen 7 4800U / 16 GB / NVMe；单进程流式",
        },
        "validation": {
            "spatial": spatial,
            "spatial_method": (
                "源像元中心 → EPSG:4326 → Web Mercator → part 四角双线性反算 → 回原生像元；门禁：每 part ≤0.5 源像元"
            ),
            "numeric": None,
            "numeric_method": "float32 小端 256 块；回读容差 max(1e-5, 1e-6*|src|)",
            "statistics": None,
            "statistics_method": "原生网格有效像元 depth>阈值（严格大于）；剔除 NaN 与海域；相对差 ≤0.1%",
            "max_crosscheck": max_crosscheck,
            "pending": "运行 python -m floodviz validate --full 回填 numeric/statistics 门禁结果",
        },
        "quality_summary": quality_summary,
        "content": content,
        "web": {"ready": False, "files": []},
    }
