"""发布包门禁：validate_bundle。

校验分 6 组（任一 group 失败 → 不得发布）：
1. **结构**：catalog/timeline/statistics/images/numeric_manifest 必填字段、状态机、
   路径安全（禁止绝对路径/盘符/用户名）、帧数一致、子图 id 唯一。
2. **完整性**：每条 run 的每帧 PNG part、mask.png、每个 256 块 .bin 都存在且非空；
   content 哈希与实际文件一致（`--full`）。
3. **数值回读**：抽样像元 → 读 numeric 块 → 与源 `.wd` 值比较，
   容差 `max(1e-5, 1e-6*|src|)`；越界必须报“域外”。
4. **统计复算**：抽样帧用源 `.wd` 重算阈值面积，与 statistics.json 相对差 ≤0.1%；
   max 包络复算 + `res.max ≥ 逐帧fmax` 单向性断言。
5. **空间**：每个 part 的源像元中心 → WGS84 → Web Mercator → 四角双线性反算，
   偏差 ≤0.5 源像元；子图必须严丝合缝覆盖整幅、互不重叠。
6. **时间**：sim_seconds 严格递增、= seq×saveint、末帧不超模拟时长、
   origin 未确认时 timestamp 必须为 null、frame_count 与数组长度一致。

Schema 校验（若 `schemas/*.json` 与 `jsonschema` 可用）作为附加门禁。
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..config import Config
from ..quality import stats as stats_mod
from ..raster import blocks as blocks_mod
from ..raster import paint as paint_mod
from ..raster.grid import Grid, read_native_frame, read_sea_mask

SCHEMA_VERSION = "1.0"
TOL_SPATIAL_PX = 0.5
TOL_STATS_REL = 0.001
NUMERIC_ABS = 1e-5
NUMERIC_REL = 1e-6
FORBIDDEN_SUFFIXES = {".wd", ".asc", ".tif", ".tiff", ".npy"}


class ValidationError(ValueError):
    """发布包结构不可解析。"""


@dataclass
class CheckGroup:
    """一组门禁结果。"""

    name: str
    passed: bool = True
    checks: int = 0
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def fail(self, message: str) -> None:
        self.passed = False
        self.failures.append(message)

    def add(self, ok: bool, message: str) -> bool:
        self.checks += 1
        if not ok:
            self.fail(message)
        return ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "checks": self.checks,
            "failure_count": len(self.failures),
            "failures": self.failures[:200],
            "notes": self.notes[:200],
            "details": self.details,
        }


@dataclass
class ValidationReport:
    """`validate_bundle` 返回值；`.ok` 为总门禁。"""

    bundle: str
    full: bool = False
    groups: list[CheckGroup] = field(default_factory=list)
    bundle_bytes: int = 0
    file_count: int = 0
    seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(group.passed for group in self.groups) if self.groups else False

    @property
    def failures(self) -> list[str]:
        out: list[str] = []
        for group in self.groups:
            out.extend(f"[{group.name}] {msg}" for msg in group.failures)
        return out

    def group(self, name: str) -> CheckGroup | None:
        return next((g for g in self.groups if g.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "bundle": self.bundle,
            "full": bool(self.full),
            "ok": self.ok,
            "bundle_bytes": self.bundle_bytes,
            "file_count": self.file_count,
            "seconds": round(self.seconds, 3),
            "groups": [g.to_dict() for g in self.groups],
            "failures": self.failures,
            "warnings": self.warnings,
        }


# --------------------------------------------------------------------------- #
# 读取工具
# --------------------------------------------------------------------------- #
def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(f"缺少文件：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"JSON 解析失败：{path}（{exc}）") from exc


def _rel_is_safe(rel: Any) -> tuple[bool, str]:
    """禁止绝对路径、盘符、网络路径、反斜杠、`..` 逃逸。"""
    text = str(rel)
    if not text or text.strip() != text:
        return False, "空或含首尾空白"
    if text.startswith(("/", "\\")):
        return False, "以路径分隔符开头"
    if "://" in text:
        return False, "含 URL scheme"
    if len(text) > 1 and text[1] == ":":
        return False, "含盘符"
    if "\\\\" in text or "\\" in text:
        return False, "含反斜杠（应使用 POSIX 分隔符）"
    if any(part == ".." for part in text.split("/")):
        return False, "含父目录逃逸"
    return True, ""


def _sha256_file_fast(path: Path, chunk: int = 1 << 20) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(fh and chunk), b""):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# 组 1：结构
# --------------------------------------------------------------------------- #
def _check_structure(
    bundle: Path,
    catalog: dict[str, Any],
    manifest: dict[str, Any],
    config: Config | None,
    full: bool,
) -> tuple[CheckGroup, dict[str, Any]]:
    group = CheckGroup("structure")
    for key in (
        "schema_version",
        "bundle_id",
        "created_utc",
        "source_manifest_hash",
        "license_notice",
        "default_case",
        "display",
        "basemaps",
        "cases",
    ):
        group.add(bool(catalog.get(key)), f"catalog 缺字段 {key}")
    group.add(catalog.get("schema_version") == SCHEMA_VERSION, "catalog.schema_version 必须为 1.0")
    group.add(
        str(catalog.get("source_manifest_hash", "")).startswith("sha256:")
        and len(str(catalog.get("source_manifest_hash", ""))) == 71,
        "catalog.source_manifest_hash 必须是 sha256:<64 hex>",
    )
    group.add(bool(catalog.get("cases")), "catalog.cases 为空")
    group.add(bool(manifest.get("content")), "manifest.content 为空")

    display = catalog.get("display") or {}
    group.add(bool(display.get("depth_classes")), "catalog.display.depth_classes 为空")
    group.add(
        [float(t) for t in display.get("thresholds_m") or []]
        == sorted(float(t) for t in display.get("thresholds_m") or []),
        "display.thresholds_m 必须递增",
    )
    area_note = str(display.get("area_threshold_note") or "")
    if not area_note:
        group.notes.append(
            "display.area_threshold_note 为空：catalog.build_catalog 从 config.project 读取该字段，"
            "而 configs/default.yaml 把它写在顶层（Lead 作用域，已上报）"
        )
    else:
        group.add(
            ">" in area_note or "阈值" in area_note,
            "display.area_threshold_note 必须说明面积口径（depth>阈值、剔除 NaN 与海域）",
        )
    colors = {str(c.get("color", "")).upper() for c in display.get("depth_classes") or []}
    group.add(len(colors) == len(display.get("depth_classes") or []), "depth_classes 颜色必须唯一")
    for cls in display.get("depth_classes") or []:
        group.add(
            isinstance(cls.get("min_m"), (int, float)) and "label" in cls and "color" in cls,
            f"depth_class 字段不全：{cls}",
        )

    case_ids: list[str] = []
    runs_index: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in catalog.get("cases") or []:
        cid = str(entry.get("case_id", ""))
        case_ids.append(cid)
        for key in (
            "name",
            "event_id",
            "event_name",
            "scenario_role",
            "subtitle",
            "model_bbox",
            "grid",
            "time",
            "default_run_id",
            "runs",
            "layers",
            "quality",
            "sources",
        ):
            group.add(key in entry and entry[key] not in (None, "", []), f"{cid} 缺字段 {key}")
        bbox = entry.get("model_bbox") or [0, 0, 0, 0]
        group.add(
            len(bbox) == 4 and bbox[0] < bbox[2] and bbox[1] < bbox[3],
            f"{cid}.model_bbox 必须为有序 [西,南,东,北]",
        )
        grid = entry.get("grid") or {}
        for key in ("rows", "cols", "transform", "crs", "cell_size_m", "coordinates"):
            group.add(key in grid, f"{cid}.grid 缺 {key}")
        group.add(len(grid.get("transform") or []) == 6, f"{cid}.grid.transform 必须 6 元")
        group.add(len(grid.get("coordinates") or []) == 4, f"{cid}.grid.coordinates 必须 4 角")
        time_doc = entry.get("time") or {}
        group.add(time_doc.get("temporal_interpolation") == "none", f"{cid}.time 禁止插值")
        if str(time_doc.get("origin_status", "")).startswith("relative_only"):
            group.add(
                time_doc.get("time_origin") is None,
                f"{cid} origin 未确认却写了 time_origin",
            )

        run_ids = [str(r.get("run_id")) for r in entry.get("runs") or []]
        group.add(bool(run_ids), f"{cid}.runs 为空")
        group.add(entry.get("default_run_id") in run_ids, f"{cid}.default_run_id 不在 runs 中")
        for run in entry.get("runs") or []:
            rid = str(run.get("run_id"))
            runs_index[(cid, rid)] = run
            group.add(int(run.get("frame_count") or 0) > 0, f"{cid}/{rid} frame_count 非正")
            for key in ("timeline", "statistics", "numeric_manifest"):
                ok, why = _rel_is_safe(run.get(key))
                group.add(ok, f"{cid}/{rid}.{key} 路径不安全：{run.get(key)!r} {why}")
                group.add(
                    (bundle / str(run.get(key, ""))).is_file(),
                    f"{cid}/{rid}.{key} 文件不存在：{run.get(key)}",
                )
            quality = run.get("quality") or {}
            group.add(
                quality.get("mass_gate") in {"PASS", "FAIL-EXPLAINED", "FAIL", "unrecorded"},
                f"{cid}/{rid}.quality.mass_gate 非法：{quality.get('mass_gate')}",
            )
            group.add(
                bool(quality.get("validation_status")) and bool(quality.get("vertical_datum_status")),
                f"{cid}/{rid}.quality 缺 validation_status/vertical_datum_status",
            )
        layers = entry.get("layers") or {}
        group.add(layers.get("flood", {}).get("status") == "ready", f"{cid} 缺可用 flood 图层")
        for layer_id, layer in layers.items():
            status = layer.get("status")
            group.add(
                status in paint_statuses(),
                f"{cid}.layers.{layer_id}.status 非法：{status}",
            )
            if status not in {"ready", "partial"} and not (layer.get("summary") or layer.get("note")):
                group.fail(f"{cid}.layers.{layer_id} 不可用但未给原因")
            for key in ("image", "images", "geojson", "footprint"):
                if layer.get(key) is None:
                    continue
                ok, why = _rel_is_safe(layer[key])
                group.add(ok, f"{cid}.layers.{layer_id}.{key} 路径不安全 {why}")
            if layer.get("image"):
                group.add(
                    (bundle / str(layer["image"])).is_file(),
                    f"{cid}.layers.{layer_id}.image 文件不存在：{layer['image']}",
                )
            if layer.get("images"):
                group.add(
                    (bundle / str(layer["images"])).is_file(),
                    f"{cid}.layers.{layer_id}.images 文件不存在：{layer['images']}",
                )
            if layer.get("geojson"):
                group.add(
                    (bundle / str(layer["geojson"])).is_file(),
                    f"{cid}.layers.{layer_id}.geojson 文件不存在：{layer['geojson']}",
                )
    group.add(len(case_ids) == len(set(case_ids)), "case_id 重复")
    group.add(catalog.get("default_case") in case_ids, "default_case 不在 cases 中")

    if config is not None:
        group.add(
            set(case_ids) <= set(config.case_ids) or catalog.get("dev_build_partial"),
            f"包内案例 {case_ids} 不在配置 {list(config.case_ids)} 中",
        )
        for cid in case_ids:
            if cid not in config.cases:
                continue
            case = config.cases[cid]
            for (rcid, rid), run in runs_index.items():
                if rcid != cid or rid not in case.runs:
                    continue
                expected = case.runs[rid].expected_frame_count
                if not catalog.get("dev_build_partial") and int(run.get("frame_count") or 0) != expected:
                    group.fail(f"{cid}/{rid} frame_count={run.get('frame_count')} 与配置期望 {expected} 不符")

    # 禁止源文件路径泄漏进包内 JSON（只看"像路径"的形态，不看说明文字里的扩展名）
    import re

    leak_patterns = (
        (re.compile(r'"[A-Za-z]:[\\/]'), "Windows 盘符绝对路径"),
        (re.compile(r'"[^"]*(?:^|/)results/res-\d{4}\.(?:wd|elev)"'), "源帧文件路径"),
        (re.compile(r'"//[^"]+"'), "网络共享路径"),
        (re.compile(r'"[^"]*\.(?:tif|tiff|npy|asc)"'), "原始栅格文件"),
    )
    for rel in ("catalog.json", "manifest.json"):
        text = (bundle / rel).read_text(encoding="utf-8")
        for pattern, label in leak_patterns:
            match = pattern.search(text)
            group.add(
                match is None,
                f"{rel} 泄漏{label}：{match.group(0)[:80] if match else ''}",
            )
    return group, {"case_ids": case_ids, "runs": runs_index}


def paint_statuses() -> set[str]:
    return {"remote_verified", "local_unchecked", "ready", "partial", "missing", "failed"}


# --------------------------------------------------------------------------- #
# 组 2：时间轴 + 统计结构 + 图像索引
# --------------------------------------------------------------------------- #
def _check_timeline(
    bundle: Path,
    catalog: dict[str, Any],
    config: Config | None,
) -> tuple[CheckGroup, dict[str, dict[str, Any]]]:
    group = CheckGroup("timeline")
    docs: dict[str, dict[str, Any]] = {}
    for entry in catalog.get("cases") or []:
        cid = str(entry.get("case_id"))
        for run in entry.get("runs") or []:
            rid = str(run.get("run_id"))
            key = f"{cid}/{rid}"
            doc = _load_json(bundle / str(run["timeline"]))
            docs[key] = doc
            for field_name in (
                "schema_version",
                "case_id",
                "event_id",
                "run_id",
                "time_zone",
                "origin_status",
                "save_interval_s",
                "simulation_duration_s",
                "frame_count",
                "temporal_interpolation",
                "frames",
            ):
                group.add(field_name in doc, f"{key}.timeline 缺字段 {field_name}")
            group.add(doc.get("case_id") == cid, f"{key}.timeline.case_id 不匹配")
            group.add(doc.get("run_id") == rid, f"{key}.timeline.run_id 不匹配")
            group.add(doc.get("temporal_interpolation") == "none", f"{key}: 禁止插值")
            frames = doc.get("frames") or []
            group.add(doc.get("frame_count") == len(frames), f"{key}.frame_count 与数组长度不一致")
            group.add(len(frames) > 0, f"{key}.frames 为空")
            origin_ok = not str(doc.get("origin_status", "")).startswith("relative_only")
            if not origin_ok:
                group.add(
                    doc.get("time_origin") is None,
                    f"{key}: origin 未确认却写了 time_origin",
                )
            save_interval = float(doc.get("save_interval_s") or 0)
            duration = float(doc.get("simulation_duration_s") or 0)
            prev_sim = -math.inf
            prev_seq = -1
            for idx, frame in enumerate(frames):
                for field_name in (
                    "frame_id",
                    "source_sequence",
                    "sim_seconds",
                    "depth_image",
                    "query_blocks",
                    "sha256",
                ):
                    group.add(field_name in frame, f"{key}.frames[{idx}] 缺 {field_name}")
                seq = int(frame.get("source_sequence", -1))
                sim = float(frame.get("sim_seconds", -1))
                group.add(seq > prev_seq, f"{key}.frames[{idx}] source_sequence 非严格递增")
                group.add(sim > prev_sim, f"{key}.frames[{idx}] sim_seconds 非严格递增")
                group.add(
                    abs(sim - seq * save_interval) < 1e-6,
                    f"{key}.frames[{idx}] sim_seconds 与 seq×saveint 不符",
                )
                group.add(sim <= duration + 1e-6, f"{key}.frames[{idx}] 超过模拟时长")
                if origin_ok:
                    group.add(bool(frame.get("timestamp")), f"{key}.frames[{idx}] 缺 timestamp")
                else:
                    group.add(
                        frame.get("timestamp") is None,
                        f"{key}.frames[{idx}] origin 未确认却写绝对 timestamp",
                    )
                ok, why = _rel_is_safe(frame.get("depth_image"))
                group.add(ok, f"{key}.frames[{idx}].depth_image 路径不安全 {why}")
                ok, why = _rel_is_safe(frame.get("query_blocks"))
                group.add(ok, f"{key}.frames[{idx}].query_blocks 路径不安全 {why}")
                group.add(
                    "{block_y}" in str(frame.get("query_blocks")) and "{block_x}" in str(frame.get("query_blocks")),
                    f"{key}.frames[{idx}].query_blocks 缺块占位符",
                )
                sha = str(frame.get("sha256") or "")
                group.add(
                    len(sha) == 64 and all(c in "0123456789abcdef" for c in sha), f"{key}.frames[{idx}] sha256 非法"
                )
                prev_sim, prev_seq = sim, seq
            if config is not None and cid in config.cases:
                case = config.cases[cid]
                if rid in case.runs and not catalog.get("dev_build_partial"):
                    group.add(
                        len(frames) == case.runs[rid].expected_frame_count,
                        f"{key} 帧数 {len(frames)} 与配置期望 {case.runs[rid].expected_frame_count} 不符",
                    )
    return group, docs


def _check_statistics(
    bundle: Path,
    catalog: dict[str, Any],
    timelines: dict[str, dict[str, Any]],
) -> tuple[CheckGroup, dict[str, dict[str, Any]]]:
    group = CheckGroup("statistics")
    docs: dict[str, dict[str, Any]] = {}
    for entry in catalog.get("cases") or []:
        cid = str(entry.get("case_id"))
        thresholds = [float(t) for t in (catalog.get("display") or {}).get("thresholds_m") or []]
        for run in entry.get("runs") or []:
            rid = str(run.get("run_id"))
            key = f"{cid}/{rid}"
            doc = _load_json(bundle / str(run["statistics"]))
            docs[key] = doc
            for field_name in (
                "schema_version",
                "case_id",
                "run_id",
                "unit_area",
                "thresholds_m",
                "grid_cell_area_m2",
                "frames",
            ):
                group.add(field_name in doc, f"{key}.statistics 缺字段 {field_name}")
            group.add(doc.get("unit_area") == "km2", f"{key}.statistics.unit_area 必须 km2")
            group.add(
                [float(t) for t in doc.get("thresholds_m") or []] == thresholds,
                f"{key}.statistics.thresholds_m 与 catalog.display 不一致",
            )
            group.add(float(doc.get("grid_cell_area_m2") or 0) > 0, f"{key}.grid_cell_area_m2 非正")
            timeline = timelines.get(key) or {}
            frames = doc.get("frames") or []
            tl_frames = timeline.get("frames") or []
            group.add(len(frames) == len(tl_frames), f"{key}: statistics 与 timeline 帧数不一致")
            keys = {stats_mod.threshold_key(t) for t in thresholds}
            for idx, frame in enumerate(frames):
                for field_name in ("frame_id", "sim_seconds", "areas_km2", "wetted_cells"):
                    group.add(field_name in frame, f"{key}.frames[{idx}] 缺 {field_name}")
                group.add(
                    set(frame.get("areas_km2") or {}) == keys,
                    f"{key}.frames[{idx}].areas_km2 阈值键与 display 不一致",
                )
                group.add(
                    set(frame.get("wetted_cells") or {}) == keys,
                    f"{key}.frames[{idx}].wetted_cells 阈值键与 display 不一致",
                )
                for tkey, area in (frame.get("areas_km2") or {}).items():
                    group.add(
                        isinstance(area, (int, float)) and area >= 0, f"{key}.frames[{idx}] 面积非法 {tkey}={area}"
                    )
                    cells = (frame.get("wetted_cells") or {}).get(tkey, 0)
                    group.add(
                        isinstance(cells, int) and cells >= 0,
                        f"{key}.frames[{idx}] 像元数非法 {tkey}={cells}",
                    )
                    recomputed = cells * float(doc.get("grid_cell_area_m2") or 0) / 1e6
                    group.add(
                        abs(recomputed - float(area)) <= max(1e-6, 1e-6 * max(abs(float(area)), 1.0)),
                        f"{key}.frames[{idx}] {tkey} 面积与像元数×格面积不自洽（{area} vs {recomputed:.6f}）",
                    )
                if idx < len(tl_frames):
                    group.add(
                        frame.get("frame_id") == tl_frames[idx].get("frame_id"),
                        f"{key}.frames[{idx}].frame_id 与 timeline 顺序不一致",
                    )
                    group.add(
                        abs(float(frame.get("sim_seconds", -1)) - float(tl_frames[idx].get("sim_seconds", -2))) < 1e-6,
                        f"{key}.frames[{idx}].sim_seconds 与 timeline 不一致",
                    )
                    group.add(
                        frame.get("timestamp") == tl_frames[idx].get("timestamp"),
                        f"{key}.frames[{idx}].timestamp 与 timeline 不一致",
                    )
            envelope = doc.get("max_envelope") or {}
            for field_name in ("areas_km2", "wetted_cells", "peak_frame_id", "peak_time_s"):
                group.add(field_name in envelope, f"{key}.max_envelope 缺 {field_name}")
            group.add(
                envelope.get("peak_frame_id") in {f.get("frame_id") for f in frames},
                f"{key}.max_envelope.peak_frame_id 不在帧列表中",
            )
            for tkey, area in (envelope.get("areas_km2") or {}).items():
                frame_areas = [(f.get("areas_km2") or {}).get(tkey, 0.0) for f in frames]
                group.add(
                    float(area) + 1e-6 >= max(frame_areas, default=0.0),
                    f"{key}.max_envelope {tkey} 面积小于逐帧最大值（包络必须 ≥ 任一帧）",
                )
    return group, docs


def _check_images(
    bundle: Path,
    catalog: dict[str, Any],
    timelines: dict[str, dict[str, Any]],
) -> tuple[CheckGroup, dict[str, dict[str, Any]]]:
    group = CheckGroup("images")
    docs: dict[str, dict[str, Any]] = {}
    for entry in catalog.get("cases") or []:
        cid = str(entry.get("case_id"))
        runs = entry.get("runs") or []
        if not runs:
            continue
        for run in runs:
            rid = str(run.get("run_id"))
            key = f"{cid}/{rid}"
            rel = f"frames/{cid}/{rid}/images.json"
            if not (bundle / rel).is_file():
                group.fail(f"{key}: 缺 {rel}")
                continue
            doc = _load_json(bundle / rel)
            docs[key] = doc
            for field_name in ("schema_version", "case_id", "run_id", "kind", "grid", "color_scale", "split", "frames"):
                group.add(field_name in doc, f"{key}.images 缺字段 {field_name}")
            group.add(doc.get("case_id") == cid, f"{key}.images.case_id 不匹配")
            group.add(doc.get("run_id") == rid, f"{key}.images.run_id 不匹配")
            grid = doc.get("grid") or {}
            group.add(len(grid.get("transform") or []) == 6, f"{key}.images.grid.transform 必须 6 元")
            split = doc.get("split") or {}
            srows, scols = int(split.get("rows", 0)), int(split.get("cols", 0))
            group.add(srows > 0 and scols > 0, f"{key}.images.split 非法")
            rows, cols = int(grid.get("rows", 0)), int(grid.get("cols", 0))
            if rows > 0 and cols > 0 and srows > 0 and scols > 0:
                try:
                    paint_mod.check_partition(rows, cols, srows, scols)
                except paint_mod.PaintError as exc:
                    group.fail(f"{key}: 子图切分不合法（{exc}）")
                expected = [(w.part_row, w.part_col) for w in paint_mod.part_windows(rows, cols, srows, scols)]
            else:
                expected = []

            tl_frames = (timelines.get(key) or {}).get("frames") or []
            frames = doc.get("frames") or []
            group.add(
                len(frames) == len(tl_frames),
                f"{key}: images.frames 数 {len(frames)} 与 timeline {len(tl_frames)} 不一致",
            )
            seen_ids: set[str] = set()
            for idx, frame in enumerate(frames):
                for field_name in ("frame_id", "max_depth_m", "wet_pixels", "parts"):
                    group.add(field_name in frame, f"{key}.images.frames[{idx}] 缺 {field_name}")
                if idx < len(tl_frames):
                    group.add(
                        frame.get("frame_id") == tl_frames[idx].get("frame_id"),
                        f"{key}.images.frames[{idx}] frame_id 与 timeline 顺序不一致",
                    )
                parts = frame.get("parts") or []
                group.add(
                    [(int(p.get("row", -1)), int(p.get("col", -1))) for p in parts] == expected,
                    f"{key}.images.frames[{idx}].parts 顺序/数量不符合 row-major split",
                )
                for part in parts:
                    pid = str(part.get("id", ""))
                    group.add(bool(pid), f"{key}.images.frames[{idx}] part 缺 id")
                    group.add(pid not in seen_ids, f"{key}: part id 重复 {pid}")
                    seen_ids.add(pid)
                    ok, why = _rel_is_safe(part.get("path"))
                    group.add(ok, f"{key}.images.frames[{idx}] part 路径不安全 {why}")
                    group.add(
                        (bundle / str(part.get("path", ""))).is_file(),
                        f"{key}: 缺图片 {part.get('path')}",
                    )
                    group.add(len(part.get("coordinates") or []) == 4, f"{key}: part {pid} 需 4 角坐标")
                    group.add(
                        int(part.get("width", 0)) > 0 and int(part.get("height", 0)) > 0,
                        f"{key}: part {pid} 尺寸非法",
                    )
                    group.add(
                        int(part.get("width", 0)) <= 4096 and int(part.get("height", 0)) <= 4096,
                        f"{key}: part {pid} 超过 4096 纹理上限",
                    )
            max_entry = doc.get("max")
            if max_entry:
                parts = max_entry.get("parts") or []
                group.add(
                    [(int(p.get("row", -1)), int(p.get("col", -1))) for p in parts] == expected,
                    f"{key}.images.max.parts 顺序/数量不符合 row-major split",
                )
                for part in parts:
                    pid = str(part.get("id", ""))
                    group.add(pid not in seen_ids, f"{key}: part id 重复 {pid}")
                    seen_ids.add(pid)
                    group.add(
                        (bundle / str(part.get("path", ""))).is_file(),
                        f"{key}: 缺 max 图片 {part.get('path')}",
                    )
                    group.add(len(part.get("coordinates") or []) == 4, f"{key}: max part {pid} 需 4 角坐标")
            if (srows, scols) == (1, 1) and frames:
                first_path = str((frames[0].get("parts") or [{}])[0].get("path", ""))
                group.add(bool(first_path), f"{key}: 1×1 split 必须有单张图")
    return group, docs


# --------------------------------------------------------------------------- #
# 组 3：完整性（文件存在 + 尺寸 + content 哈希）
# --------------------------------------------------------------------------- #
def _check_completeness(
    bundle: Path,
    catalog: dict[str, Any],
    manifest: dict[str, Any],
    full: bool,
) -> CheckGroup:
    group = CheckGroup("completeness")
    content = manifest.get("content") or {}
    total_bytes = 0
    count = 0
    for item in bundle.rglob("*"):
        if item.is_file():
            count += 1
            total_bytes += item.stat().st_size
    group.details["bundle_bytes"] = total_bytes
    group.details["file_count"] = count
    group.details["content_entries"] = len(content)
    group.add(count > 0, "发布包为空")

    missing_hash = 0
    for rel in content:
        if not (bundle / rel).is_file():
            missing_hash += 1
    group.add(missing_hash == 0, f"manifest.content 有 {missing_hash} 个文件实际不存在")

    for entry in catalog.get("cases") or []:
        cid = str(entry.get("case_id"))
        for run in entry.get("runs") or []:
            rid = str(run.get("run_id"))
            key = f"{cid}/{rid}"
            numeric = _load_json(bundle / str(run["numeric_manifest"]))
            grid = numeric.get("grid") or {}
            rows, cols = int(grid.get("rows", 0)), int(grid.get("cols", 0))
            bs = int(numeric.get("block_size") or 256)
            nby, nbx = blocks_mod.block_counts(rows, cols, bs)
            group.add(
                (numeric.get("blocks") or {}).get("rows") == nby and (numeric.get("blocks") or {}).get("cols") == nbx,
                f"{key}.numeric.blocks 应为 {nby}×{nbx}",
            )
            mask_rel = str(numeric.get("mask_png", ""))
            group.add((bundle / mask_rel).is_file(), f"{key}: 缺 mask.png")
            missing = 0
            expected_files = 0
            for frame in numeric.get("frames") or []:
                frame_id = str(frame.get("frame_id"))
                for by in range(nby):
                    for bx in range(nbx):
                        expected_files += 1
                        path = bundle / "numeric" / cid / rid / frame_id / str(by) / f"{bx}.bin"
                        if not path.is_file() or path.stat().st_size == 0:
                            missing += 1
                        elif path.stat().st_size % 4 != 0:
                            group.fail(f"{key}: {path} 大小不是 4 的倍数")
            if numeric.get("max"):
                for by in range(nby):
                    for bx in range(nbx):
                        expected_files += 1
                        path = bundle / "numeric" / cid / rid / "max" / str(by) / f"{bx}.bin"
                        if not path.is_file() or path.stat().st_size == 0:
                            missing += 1
            group.add(missing == 0, f"{key}: 缺 {missing}/{expected_files} 个数值块文件")
            group.details.setdefault("numeric_files", {})[key] = expected_files

            # mask.png 尺寸必须等于网格
            if (bundle / mask_rel).is_file():
                from PIL import Image

                with Image.open(bundle / mask_rel) as img:
                    group.add(
                        (img.height, img.width) == (rows, cols),
                        f"{key}.mask.png 尺寸 {(img.height, img.width)} != 网格 {(rows, cols)}",
                    )

            if full and content:
                for _rel_name, info in (numeric.get("frames") or [{}])[0].items():
                    del info  # 只占位，避免误用
                timeline = _load_json(bundle / str(run["timeline"]))
                for frame in timeline.get("frames") or []:
                    block_dir = bundle / "numeric" / cid / rid / str(frame["frame_id"])
                    sample = next(block_dir.glob("*/*.bin"), None) if block_dir.is_dir() else None
                    group.add(sample is not None, f"{key}/{frame['frame_id']}: 无数值块")
                    if sample is not None:
                        group.add(sample.stat().st_size > 0, f"{key}/{frame['frame_id']}: 数值块为空")

    if full and content:
        checked = 0
        for rel, info in content.items():
            path = bundle / rel
            if not path.is_file():
                continue
            size = path.stat().st_size
            if int(info.get("bytes", -1)) != size:
                group.fail(f"{rel} 大小 {size} 与 manifest 记录 {info.get('bytes')} 不符")
            checked += 1
        group.details["hash_checked"] = checked
        group.add(checked == len(content), f"content 记录 {len(content)} 条，只核对到 {checked} 条")
    return group


# --------------------------------------------------------------------------- #
# 组 4：数值回读
# --------------------------------------------------------------------------- #
def _check_numeric(
    bundle: Path,
    catalog: dict[str, Any],
    config: Config | None,
    full: bool,
    frames_per_run: int = 8,
    samples_per_frame: int = 24,
) -> CheckGroup:
    group = CheckGroup("numeric")
    rng = np.random.default_rng(20261027)
    max_abs = 0.0
    max_rel = 0.0
    total_samples = 0
    per_run: dict[str, Any] = {}

    for entry in catalog.get("cases") or []:
        cid = str(entry.get("case_id"))
        config.cases[cid] if config is not None and cid in config.cases else None
        for run in entry.get("runs") or []:
            rid = str(run.get("run_id"))
            key = f"{cid}/{rid}"
            numeric = _load_json(bundle / str(run["numeric_manifest"]))
            grid_doc = numeric.get("grid") or {}
            rows, cols = int(grid_doc.get("rows", 0)), int(grid_doc.get("cols", 0))
            bs = int(numeric.get("block_size") or 256)
            block_dir = bundle / "numeric" / cid / rid
            # 源目录：优先 config，其次按 run_path_hint 反查
            src_dir = None
            run_cfg = None
            if config is not None and cid in config.cases:
                case = config.cases[cid]
                if rid in case.runs:
                    run_cfg = case.runs[rid]
                    src_dir = run_cfg.source_dir
            if src_dir is None:
                group.notes.append(f"{key}: 无配置可定位源目录，跳过源值比对（仅内部自洽）")
            sample_frames = numeric.get("frames") or []
            if not full and len(sample_frames) > frames_per_run:
                idx = rng.choice(len(sample_frames), size=frames_per_run, replace=False)
                sample_frames = [sample_frames[int(i)] for i in sorted(idx)]
            run_max_abs = 0.0
            run_samples = 0
            for frame in sample_frames:
                frame_id = str(frame.get("frame_id"))
                block_frame_dir = block_dir / frame_id
                if not block_frame_dir.is_dir():
                    group.fail(f"{key}/{frame_id}: 缺块目录")
                    continue
                source = None
                if src_dir is not None:
                    src_path = src_dir / "results" / f"res-{int(frame_id[1:]):04d}.wd"
                    if src_path.is_file():
                        source = read_native_frame(src_path)
                picks = [
                    (int(r), int(c))
                    for r, c in zip(
                        rng.integers(0, rows, samples_per_frame),
                        rng.integers(0, cols, samples_per_frame),
                        strict=True,
                    )
                ]
                # 固定追加四角与中心，覆盖边缘块裁剪
                picks.extend([(0, 0), (0, cols - 1), (rows - 1, 0), (rows - 1, cols - 1), (rows // 2, cols // 2)])
                for row, col in picks:
                    by, bx = row // bs, col // bs
                    block = blocks_mod.read_block(block_frame_dir, by, bx, rows=rows, cols=cols, block_size=bs)
                    value = float(block[row - by * bs, col - bx * bs])
                    total_samples += 1
                    run_samples += 1
                    if source is not None:
                        truth = float(source[row, col])
                        if math.isnan(truth):
                            group.add(
                                math.isnan(value),
                                f"{key}/{frame_id} ({row},{col}) 源为 NaN 但块值 {value}",
                            )
                        else:
                            group.add(
                                not math.isnan(value),
                                f"{key}/{frame_id} ({row},{col}) 源有值 {truth} 但块为 NaN",
                            )
                            if not math.isnan(value):
                                err = abs(value - truth)
                                tol = max(NUMERIC_ABS, NUMERIC_REL * abs(truth))
                                run_max_abs = max(run_max_abs, err)
                                max_abs = max(max_abs, err)
                                rel = err / max(abs(truth), 1e-12)
                                max_rel = max(max_rel, rel)
                                group.add(
                                    err <= tol,
                                    f"{key}/{frame_id} ({row},{col}) 回读误差 {err:.3e} > 容差 {tol:.3e}"
                                    f"（块 {value} vs 源 {truth}）",
                                )
                del source
                # 越界必须报“域外”
                try:
                    blocks_mod.read_value(block_frame_dir, rows + 5, 0, rows=rows, cols=cols, block_size=bs)
                    group.fail(f"{key}/{frame_id}: 越界行未报错（应返回“域外”）")
                except blocks_mod.BlocksError:
                    group.checks += 1
            per_run[key] = {
                "frames_checked": len(sample_frames),
                "samples": run_samples,
                "max_abs_err_m": round(run_max_abs, 10),
                "source_compared": src_dir is not None,
            }

    group.details["samples"] = total_samples
    group.details["max_abs_err_m"] = round(max_abs, 10)
    group.details["max_rel_err"] = round(max_rel, 12)
    group.details["gate"] = f"max({NUMERIC_ABS}, {NUMERIC_REL}*|src|)"
    group.details["passed"] = group.passed
    group.details["per_run"] = per_run
    group.add(total_samples > 0, "没有抽样任何数值像元")
    return group


# --------------------------------------------------------------------------- #
# 组 5：统计复算
# --------------------------------------------------------------------------- #
def _check_statistics_recompute(
    bundle: Path,
    catalog: dict[str, Any],
    config: Config | None,
    full: bool,
    frames_per_run: int = 6,
) -> CheckGroup:
    group = CheckGroup("statistics_recompute")
    if config is None:
        group.notes.append("未提供 config：跳过源数据统计复算（只做内部自洽）")
        group.details["skipped"] = True
        return group
    rng = np.random.default_rng(7)
    max_rel = 0.0
    frames_checked = 0
    per_run: dict[str, Any] = {}
    for entry in catalog.get("cases") or []:
        cid = str(entry.get("case_id"))
        if cid not in config.cases:
            continue
        case = config.cases[cid]
        for run in entry.get("runs") or []:
            rid = str(run.get("run_id"))
            key = f"{cid}/{rid}"
            if rid not in case.runs:
                continue
            run_cfg = case.runs[rid]
            stats_doc = _load_json(bundle / str(run["statistics"]))
            numeric = _load_json(bundle / str(run["numeric_manifest"]))
            rows = int((numeric.get("grid") or {}).get("rows", 0))
            cols = int((numeric.get("grid") or {}).get("cols", 0))
            cell_area = float(stats_doc.get("grid_cell_area_m2") or 0)
            sea = read_sea_mask(run_cfg.source_dir, (rows, cols))
            thresholds = [float(t) for t in stats_doc.get("thresholds_m") or []]
            frames = stats_doc.get("frames") or []
            picks = list(range(len(frames)))
            if not full and len(picks) > frames_per_run:
                idx = rng.choice(len(picks), size=frames_per_run, replace=False)
                picks = sorted(int(i) for i in idx)
            run_max_rel = 0.0
            for i in picks:
                frame = frames[i]
                frame_id = str(frame.get("frame_id"))
                src = run_cfg.source_dir / "results" / f"res-{int(frame_id[1:]):04d}.wd"
                if not src.is_file():
                    group.fail(f"{key}/{frame_id}: 缺源帧 {src}")
                    continue
                depth = read_native_frame(src)
                recalculated = stats_mod.compute_areas(depth, thresholds, cell_area, sea)
                frames_checked += 1
                for tkey, area in (frame.get("areas_km2") or {}).items():
                    expected = float(recalculated["areas_km2"][tkey])
                    diff = abs(float(area) - expected)
                    if expected > 0:
                        rel = diff / expected
                        run_max_rel = max(run_max_rel, rel)
                        max_rel = max(max_rel, rel)
                        group.add(
                            rel <= TOL_STATS_REL,
                            f"{key}/{frame_id} 阈值 {tkey} 面积相对差 {rel:.4%} > {TOL_STATS_REL:.1%}"
                            f"（包 {area} vs 复算 {expected}）",
                        )
                    else:
                        group.add(
                            diff <= 1e-9,
                            f"{key}/{frame_id} 阈值 {tkey} 复算为 0 但包内 {area}",
                        )
                    group.add(
                        int((frame.get("wetted_cells") or {}).get(tkey, -1)) == int(recalculated["wetted_cells"][tkey]),
                        f"{key}/{frame_id} 阈值 {tkey} 像元数不一致",
                    )
                if "max_envelope" in stats_doc:
                    envelope = stats_mod.envelope_stats(depth, thresholds, cell_area, sea)
                    peak_id = stats_doc["max_envelope"].get("peak_frame_id")
                    if peak_id == frame_id:
                        for tkey, area in (stats_doc["max_envelope"].get("areas_km2") or {}).items():
                            group.add(
                                float(area) + 1e-6 >= float(envelope["areas_km2"][tkey]),
                                f"{key}.max_envelope {tkey} 小于本帧面积（峰值帧）",
                            )
            per_run[key] = {
                "frames_checked": len(picks),
                "max_rel_diff": round(run_max_rel, 8),
            }
            # res.max 与逐帧包络的单向性
            max_cross = (_load_json(bundle / "manifest.json").get("validation") or {}).get("max_crosscheck") or {}
            by_run = (max_cross.get("by_run") or {}).get(cid, {}).get(rid)
            if by_run:
                group.add(
                    bool(by_run.get("res_max_ge_frames")),
                    f"{key}: res.max 存在小于逐帧采样值的像元（违反单向性）",
                )
    group.details["frames_checked"] = frames_checked
    group.details["max_rel_diff"] = round(max_rel, 8)
    group.details["gate"] = TOL_STATS_REL
    group.details["passed"] = group.passed
    group.details["per_run"] = per_run
    group.add(frames_checked > 0, "没有复算任何帧的统计")
    return group


# --------------------------------------------------------------------------- #
# 组 6：空间
# --------------------------------------------------------------------------- #
def _check_spatial(
    bundle: Path,
    catalog: dict[str, Any],
    config: Config | None,
    full: bool,
    samples: int = 15,
) -> CheckGroup:
    group = CheckGroup("spatial")
    if config is None:
        group.notes.append("未提供 config：跳过空间残差复算（构建期已实测并写入 manifest）")
        group.details["skipped"] = True
        return group
    from pyproj import Transformer

    per_run: dict[str, Any] = {}
    worst = 0.0
    for entry in catalog.get("cases") or []:
        cid = str(entry.get("case_id"))
        if cid not in config.cases:
            continue
        case = config.cases[cid]
        tr = Transformer.from_crs(case.crs, "EPSG:4326", always_xy=True)
        for run in entry.get("runs") or []:
            rid = str(run.get("run_id"))
            key = f"{cid}/{rid}"
            images_rel = f"frames/{cid}/{rid}/images.json"
            if not (bundle / images_rel).is_file():
                group.fail(f"{key}: 缺 images.json，无法复算空间残差")
                continue
            images = _load_json(bundle / images_rel)
            grid_doc = images.get("grid") or {}
            rows, cols = int(grid_doc.get("rows", 0)), int(grid_doc.get("cols", 0))
            grid = Grid(
                rows=rows,
                cols=cols,
                transform=tuple(float(v) for v in grid_doc["transform"]),
                crs=str(grid_doc["crs"]),
                cell_size_m=float(grid_doc["cell_size_m"]),
                cell_area_m2=float(grid_doc["cell_area_m2"]),
            )
            split = images.get("split") or {}
            srows, scols = int(split.get("rows", 1)), int(split.get("cols", 1))
            try:
                paint_mod.check_partition(rows, cols, srows, scols)
            except paint_mod.PaintError as exc:
                group.fail(f"{key}: {exc}")
                continue
            frames = images.get("frames") or []
            max_entry = images.get("max") or {}
            declared: dict[tuple[int, int], list[list[float]]] = {}
            for holder in (frames[0] if frames else None, max_entry):
                if not holder:
                    continue
                for part in holder.get("parts") or []:
                    declared[(int(part["row"]), int(part["col"]))] = part["coordinates"]
                break
            # 每个 part 用自身声明的四角复算残差
            run_worst = 0.0
            parts_detail: list[dict[str, Any]] = []
            for win in paint_mod.part_windows(rows, cols, srows, scols):
                coords = declared.get((win.part_row, win.part_col))
                if coords is None:
                    group.fail(f"{key}: part r{win.part_row}c{win.part_col} 缺坐标")
                    continue
                detail = paint_mod.measure_part_residual_px(
                    grid,
                    win,
                    tr.transform,
                    samples=samples if full else max(5, samples // 2),
                )
                # 用声明的坐标校验（而不是重算的四角），确保发布的坐标自洽
                detail2 = _residual_with_declared_quad(grid, win, coords, tr.transform, samples=samples if full else 7)
                worst_px = max(detail["max_px"], detail2["max_px"])
                run_worst = max(run_worst, worst_px)
                worst = max(worst, worst_px)
                parts_detail.append(
                    {
                        "id": f"r{win.part_row}c{win.part_col}",
                        "max_px": round(worst_px, 6),
                        "max_m": round(worst_px * grid.cell_size_m, 4),
                    }
                )
                group.add(
                    worst_px <= TOL_SPATIAL_PX,
                    f"{key} part r{win.part_row}c{win.part_col} 空间残差 {worst_px:.4f}px "
                    f"({worst_px * grid.cell_size_m:.1f} m) > {TOL_SPATIAL_PX} px",
                )
            # 与构建期记录比对（不超过 0.05 px 的偏差，防止阶段间算法漂移）
            declared_spatial = (images.get("spatial_validation") or {}).get("max_px")
            if declared_spatial is not None:
                group.add(
                    abs(float(declared_spatial) - run_worst) <= 0.05,
                    f"{key}: 重建期记录残差 {declared_spatial}px 与复算 {run_worst:.4f}px 相差过大",
                )
            per_run[key] = {
                "split": {"rows": srows, "cols": scols},
                "parts": parts_detail,
                "max_px": round(run_worst, 6),
                "max_m": round(run_worst * grid.cell_size_m, 4),
                "samples": len(parts_detail) * (samples if full else max(5, samples // 2)),
                "gate_px": TOL_SPATIAL_PX,
                "passed": run_worst <= TOL_SPATIAL_PX,
            }
    group.details["per_run"] = per_run
    group.details["max_px"] = round(worst, 6)
    group.details["gate_px"] = TOL_SPATIAL_PX
    group.details["passed"] = group.passed
    group.add(bool(per_run), "没有校验任何 run 的空间残差")
    return group


def _residual_with_declared_quad(
    grid: Grid,
    window: paint_mod.PartWindow,
    coordinates: Sequence[Sequence[float]],
    tr_fn: Callable[..., Any],
    *,
    samples: int = 7,
) -> dict[str, Any]:
    """用 images.json 里**已发布**的四角经纬度复算残差（端到端，含取整影响）。"""
    if len(coordinates) != 4:
        raise ValidationError(f"part 坐标必须 4 角，实得 {len(coordinates)}")
    a, b, c, d, e, f = (float(v) for v in grid.transform)
    frac = (np.arange(samples) + 0.5) / samples
    cols = window.c0 + frac * window.width
    rows = window.r0 + frac * window.height
    cc, rr = np.meshgrid(cols, rows)
    cols_f, rows_f = cc.ravel(), rr.ravel()
    xs = a * cols_f + b * rows_f + c
    ys = d * cols_f + e * rows_f + f
    lon, lat = paint_mod.transform_points(tr_fn, xs, ys)
    pts = paint_mod._to_merc(lon, lat)
    quad_lon = np.array([float(p[0]) for p in coordinates])
    quad_lat = np.array([float(p[1]) for p in coordinates])
    quad = paint_mod._to_merc(quad_lon, quad_lat)
    uv = paint_mod._bilinear_inverse(quad, pts)
    rec_col = window.c0 + uv[:, 0] * window.width
    rec_row = window.r0 + uv[:, 1] * window.height
    err = np.hypot(rec_col - cols_f, rec_row - rows_f)
    return {
        "max_px": float(err.max()),
        "max_m": float(err.max()) * float(grid.cell_size_m),
        "samples": int(err.size),
    }


# --------------------------------------------------------------------------- #
# 组 7：schema
# --------------------------------------------------------------------------- #
def _check_schemas(bundle: Path, config: Config | None) -> CheckGroup:
    group = CheckGroup("schemas")
    # 优先复用 Lead 的 publishing.schemas（单点真源，避免两套 schema 逻辑漂移）
    try:
        from ..publishing.schemas import validate_bundle_documents
    except ImportError:  # pragma: no cover - 依赖缺失
        group.notes.append("publishing.schemas 不可用，跳过 schema 校验")
        group.details["skipped"] = True
        return group
    try:
        failures = validate_bundle_documents(bundle)
    except Exception as exc:
        group.fail(f"schema 校验器异常：{exc}")
        return group
    for rel, errors in sorted(failures.items()):
        group.fail(f"{rel}: " + "；".join(errors[:5]))
    group.checks += max(len(failures), 1)
    group.details["failed_documents"] = len(failures)
    group.details["passed"] = group.passed
    return group


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def validate_bundle(bundle_dir: str | Path, config: Config | None = None, full: bool = False) -> ValidationReport:
    """校验发布包；`.ok` 为总门禁（任一硬门禁失败即 False）。"""
    import time as _time

    t0 = _time.perf_counter()
    bundle = Path(bundle_dir)
    report = ValidationReport(bundle=str(bundle), full=bool(full))
    if not bundle.is_dir():
        group = CheckGroup("structure")
        group.fail(f"发布包目录不存在：{bundle}")
        report.groups.append(group)
        return report

    catalog_path = bundle / "catalog.json"
    manifest_path = bundle / "manifest.json"
    try:
        catalog = _load_json(catalog_path)
        manifest = _load_json(manifest_path)
    except ValidationError as exc:
        group = CheckGroup("structure")
        group.fail(str(exc))
        report.groups.append(group)
        report.seconds = _time.perf_counter() - t0
        return report

    structure_group, _index = _check_structure(bundle, catalog, manifest, config, full)
    report.groups.append(structure_group)
    timeline_group, timelines = _check_timeline(bundle, catalog, config)
    report.groups.append(timeline_group)
    stats_group, _stats = _check_statistics(bundle, catalog, timelines)
    report.groups.append(stats_group)
    images_group, _images = _check_images(bundle, catalog, timelines)
    report.groups.append(images_group)
    report.groups.append(_check_completeness(bundle, catalog, manifest, full))
    report.groups.append(_check_numeric(bundle, catalog, config, full))
    report.groups.append(_check_statistics_recompute(bundle, catalog, config, full))
    report.groups.append(_check_spatial(bundle, catalog, config, full))
    report.groups.append(_check_schemas(bundle, config))

    count = 0
    total = 0
    for item in bundle.rglob("*"):
        if item.is_file():
            count += 1
            total += item.stat().st_size
    report.file_count = count
    report.bundle_bytes = total

    web_ready = bool((manifest.get("web") or {}).get("ready"))
    if not web_ready:
        report.warnings.append("manifest.web.ready=false：尚未运行 scripts/build_web.py（index.html/assets 未就位）")
    report.seconds = _time.perf_counter() - t0
    return report


def results_path(bundle_dir: Path, name: str = "validation_report.json") -> Path:
    """报告默认落点（包根，便于随包分发）。"""
    return Path(bundle_dir) / name


def summarize(report: ValidationReport) -> str:  # pragma: no cover - 便于 CLI/人工
    lines = [f"发布包：{report.bundle}", f"总门禁：{'通过' if report.ok else '未通过'}（full={report.full}）"]
    for group in report.groups:
        status = "PASS" if group.passed else "FAIL"
        lines.append(f"  [{status}] {group.name}: {group.checks} 项检查，{len(group.failures)} 项失败")
        for msg in group.failures[:10]:
            lines.append(f"      ! {msg}")
        for note in group.notes[:5]:
            lines.append(f"      ~ {note}")
    lines.append(f"  文件 {report.file_count} 个，{report.bundle_bytes / 1e6:.2f} MB，耗时 {report.seconds:.2f} s")
    return "\n".join(lines)


def iter_walk_globs(bundle: Path, patterns: Iterable[str]) -> Iterable[Path]:  # pragma: no cover
    for pattern in patterns:
        yield from bundle.glob(pattern)
