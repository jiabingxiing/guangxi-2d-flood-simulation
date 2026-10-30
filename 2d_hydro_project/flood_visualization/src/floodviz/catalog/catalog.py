"""catalog.json 组装原语与结构自检（SPEC §4）。

分工：pipeline 负责产出实际文件，本模块提供
- `geometry_for_grid(grid, transformer)`：把原生网格换算成 web 贴图所需的几何/四角经纬度；
- `apply_assets()`：把 optical/SAR 资产清单（data/processed/assets/manifest.json）合并进图层状态；
- `build_case_entry()` / `build_catalog()`：组装顶层文档；
- `validate_catalog()`：结构 + 状态机 + 路径安全自检，任何不合法直接抛错（阻止发布）。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..config import CaseConfig, Config
from ..raster.grid import Grid

SCHEMA_VERSION = "1.0"
LAYER_STATUSES = {"remote_verified", "local_unchecked", "ready", "partial", "missing", "failed"}
ENABLED_STATUSES = {"ready", "partial"}
REQUIRED_CASE_KEYS = (
    "case_id",
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
)


def geometry_for_grid(grid: Grid, *, crs_to_wgs84: Any, precision: int = 7) -> dict[str, Any]:
    """返回 web 贴图几何：rows/cols/transform/crs + 四角经纬度（左上、右上、右下、左下）。"""
    corners = [crs_to_wgs84(x, y) for x, y in grid.corners_native()]
    coordinates = [[round(float(lon), precision), round(float(lat), precision)] for lon, lat in corners]
    lons = [c[0] for c in coordinates]
    lats = [c[1] for c in coordinates]
    return {
        "rows": grid.rows,
        "cols": grid.cols,
        "transform": [float(v) for v in grid.transform],
        "crs": grid.crs,
        "cell_size_m": grid.cell_size_m,
        "cell_area_m2": grid.cell_area_m2,
        "coordinates": coordinates,
        "bbox_wgs84": [
            round(min(lons), precision),
            round(min(lats), precision),
            round(max(lons), precision),
            round(max(lats), precision),
        ],
    }


def bbox_wgs84_for_grid(grid: Grid, *, crs_to_wgs84: Any) -> list[float]:
    return geometry_for_grid(grid, crs_to_wgs84=crs_to_wgs84)["bbox_wgs84"]


def _asset_index(assets_manifest: dict[str, Any] | None) -> dict[tuple[str, str, str], dict[str, Any]]:
    index: dict[tuple[str, str, str], dict[str, Any]] = {}
    if not assets_manifest:
        return index
    for asset in assets_manifest.get("assets", []):
        key = (str(asset.get("case_id")), str(asset.get("layer")), str(asset.get("asset_id")))
        index[key] = asset
    return index


def apply_assets(
    case: CaseConfig,
    layer_docs: dict[str, dict[str, Any]],
    assets_manifest: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """把资产清单合并进图层文档；资产缺失时把状态降级为 missing/failed 并写明原因。

    设计原则：配置给出"预期状态"，资产清单给出"实测状态"；实测优先。
    只有 ready/partial 可勾选，其余必须带原因文案。
    """
    index = _asset_index(assets_manifest)
    out: dict[str, dict[str, Any]] = {}
    for layer_id, doc in layer_docs.items():
        out[layer_id] = dict(doc)
    for layer_id in ("optical", "sar"):
        doc = out.get(layer_id)
        if doc is None:
            continue
        asset_id = doc.get("asset_id")
        if not asset_id:
            continue
        asset = index.get((case.case_id, layer_id, str(asset_id)))
        if asset is None:
            doc["status"] = "missing"
            doc["summary"] = doc.get("summary") or "资产未导入发布包"
            doc["missing_reason"] = "data/processed/assets/manifest.json 中没有该资产记录"
            doc.pop("image", None)
            continue
        doc["status"] = str(asset.get("status", "missing"))
        for key in (
            "image",
            "footprint",
            "geometry",
            "acquired_at",
            "summary",
            "coverage",
            "attribution",
            "license",
            "source_url",
            "sensor",
            "legend",
            "role",
            "note",
            "value_min_m",
            "value_max_m",
        ):
            if asset.get(key) is not None:
                doc[key] = asset[key]
        if doc["status"] not in ENABLED_STATUSES and not doc.get("summary"):
            doc["summary"] = doc.get("note") or "图层不可用"
    return out


def build_run_entry(
    case: CaseConfig,
    run_doc: dict[str, Any],
    quality: dict[str, Any],
) -> dict[str, Any]:
    return {
        "run_id": run_doc["run_id"],
        "label": run_doc["label"],
        "role": run_doc["role"],
        "frame_count": run_doc["frame_count"],
        "save_interval_s": run_doc["save_interval_s"],
        "simulation_duration_s": run_doc["simulation_duration_s"],
        "timeline": run_doc["timeline"],
        "statistics": run_doc["statistics"],
        "numeric_manifest": run_doc["numeric_manifest"],
        "optional": bool(run_doc.get("optional", False)),
        "notes": run_doc.get("notes", ""),
        "quality": {
            "mass_gate": quality.get("mass_gate", "unrecorded"),
            "e_mass": quality.get("e_mass"),
            "validation_status": case.quality.get("validation_status", ""),
            "vertical_datum_status": case.quality.get("vertical_datum_status", ""),
            "warnings": list(case.quality.get("warnings") or []),
            "mass_gate_cause": quality.get("cause"),
        },
    }


def build_case_entry(
    case: CaseConfig,
    *,
    grid_geometry: dict[str, Any],
    run_entries: list[dict[str, Any]],
    layer_docs: dict[str, dict[str, Any]],
    quality: dict[str, Any],
) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "name": case.name,
        "event_id": case.event_id,
        "event_name": case.event_name,
        "scenario_role": case.scenario_role,
        "subtitle": case.subtitle,
        "model_bbox": [float(v) for v in case.model_bbox],
        "grid": grid_geometry,
        "time": {
            "time_origin": (
                case.runs[case.default_run_id].time_origin
                if not case.runs[case.default_run_id].origin_status.startswith("relative_only")
                else None
            ),
            "time_zone": case.time_zone,
            "origin_status": case.runs[case.default_run_id].origin_status,
            "origin_note": case.origin_note,
            "save_interval_s": float(case.runs[case.default_run_id].save_interval_s),
            "temporal_interpolation": "none",
        },
        "default_run_id": case.default_run_id,
        "runs": run_entries,
        "layers": layer_docs,
        "sites": list(case.sites),
        "quality": quality,
        "sources": dict(case.sources),
    }


def build_catalog(
    config: Config,
    *,
    bundle_id: str,
    created_utc: str,
    source_manifest_hash: str,
    case_entries: Iterable[dict[str, Any]],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "bundle_id": bundle_id,
        "created_utc": created_utc,
        "source_manifest_hash": source_manifest_hash,
        "license_notice": config.license_notice,
        "default_case": config.default_case,
        "display": {
            "project_title": config.project.get("title", ""),
            "subtitle": config.project.get("subtitle", ""),
            "default_threshold_m": config.default_threshold_m,
            "thresholds_m": list(config.thresholds_m),
            "depth_classes": [dict(c) for c in config.depth_classes],
            "area_threshold_note": config.area_threshold_note,
            "temporal_note": config.temporal_note,
            "max_envelope_title": config.max_envelope_title,
            "current_frame_title": config.current_frame_title,
        },
        "basemaps": [dict(b) for b in config.basemaps],
        "layer_order": list(config.layer_order),
        "presets": [dict(p) for p in config.presets],
        "legends": dict(config.legends),
        "cases": list(case_entries),
        "attribution": {
            "online_basemaps": "在线底图署名随服务元数据动态加载；不得声称离线授权",
            "sentinel": "Contains modified Copernicus Sentinel data 2024/2026（裁剪与遮云处理）",
            "note": "底图与影像授权见 licenses/LICENSES.md",
        },
        "environment": {"offline_capable": True, "requires_network_layers": [b["id"] for b in config.basemaps]},
    }
    if extra:
        doc.update(extra)
    validate_catalog(doc)
    return doc


def validate_catalog(doc: dict[str, Any]) -> None:
    """结构 + 状态机 + 路径安全自检。"""
    for key in (
        "schema_version",
        "bundle_id",
        "created_utc",
        "source_manifest_hash",
        "license_notice",
        "display",
        "basemaps",
        "cases",
    ):
        if not doc.get(key):
            raise ValueError(f"catalog 缺少必填顶层字段 {key!r}")
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"catalog.schema_version 必须为 {SCHEMA_VERSION}")
    for key in ("default_threshold_m", "thresholds_m", "depth_classes", "temporal_note"):
        if key not in (doc.get("display") or {}):
            raise ValueError(f"catalog.display 缺少必填字段 {key!r}")
    case_ids = set()
    for entry in doc["cases"]:
        for key in REQUIRED_CASE_KEYS:
            if key not in entry or entry[key] in (None, "", []):
                raise ValueError(f"case 缺少字段 {key!r}：{entry.get('case_id')}")
        cid = str(entry["case_id"])
        if cid in case_ids:
            raise ValueError(f"重复 case_id：{cid}")
        case_ids.add(cid)
        bbox = entry["model_bbox"]
        if not (isinstance(bbox, list) and len(bbox) == 4 and bbox[0] < bbox[2] and bbox[1] < bbox[3]):
            raise ValueError(f"{cid}.model_bbox 必须为 [西,南,东,北] 且有序")
        if not entry["runs"]:
            raise ValueError(f"{cid}.runs 为空")
        run_ids = [r["run_id"] for r in entry["runs"]]
        if entry["default_run_id"] not in run_ids:
            raise ValueError(f"{cid}.default_run_id 不在 runs 中")
        for run in entry["runs"]:
            if int(run.get("frame_count", 0)) <= 0:
                raise ValueError(f"{cid}/{run['run_id']} frame_count 非正")
            if not run.get("numeric_manifest", "").startswith("numeric/"):
                raise ValueError(f"{cid}/{run['run_id']} numeric_manifest 必须是包内相对路径")
            if run["quality"]["mass_gate"] not in {"PASS", "FAIL-EXPLAINED", "FAIL", "unrecorded"}:
                raise ValueError(f"{cid}/{run['run_id']} mass_gate 非法")
        layers = entry["layers"]
        for layer_id, layer in layers.items():
            status = layer.get("status")
            if status not in LAYER_STATUSES:
                raise ValueError(f"{cid}.layers.{layer_id}.status={status!r} 非法")
            if status not in ENABLED_STATUSES and not (layer.get("summary") or layer.get("note")):
                raise ValueError(f"{cid}.layers.{layer_id} 不可用但未给出原因")
            for key in ("image", "footprint", "geojson", "images"):
                value = layer.get(key)
                if value is None:
                    continue
                _require_relative_path(str(value), f"{cid}.layers.{layer_id}.{key}")
        if "flood" not in layers or layers["flood"].get("status") != "ready":
            raise ValueError(f"{cid} 缺少可用 flood 图层")
        for name in ("max", "dem"):
            if name not in layers:
                raise ValueError(f"{cid} 缺少 {name} 图层文档")
    if doc.get("default_case") not in case_ids:
        raise ValueError("default_case 不在 cases 中")
    # 禁止任何绝对路径或外链泄漏进案例路径字段
    for entry in doc["cases"]:
        for run in entry["runs"]:
            for key in ("timeline", "statistics", "numeric_manifest"):
                _require_relative_path(str(run[key]), f"run.{key}")


_BAD_REL_CHARS = ("\\", ":", "://", "..")


def _require_relative_path(value: str, where: str) -> None:
    """包内相对路径自检：禁绝对路径、盘符、UNC（\\\\server\\share）、反斜杠、上级目录与外链。"""
    text = str(value).strip()
    if not text:
        raise ValueError(f"{where} 不能为空")
    if text.startswith(("/", "\\")):
        raise ValueError(f"{where} 必须是包内相对路径（不能是绝对路径或 UNC）：{value!r}")
    if any(token in text for token in _BAD_REL_CHARS):
        raise ValueError(f"{where} 必须是包内相对路径（POSIX 风格，禁反斜杠/盘符/UNC/外链/..）：{value!r}")


def bundle_rel(path: Path, bundle_root: Path) -> str:
    """把包内绝对路径转成 POSIX 风格相对路径。"""
    try:
        rel = Path(path).resolve().relative_to(Path(bundle_root).resolve())
    except ValueError as exc:
        raise ValueError(f"{path} 不在发布包 {bundle_root} 内") from exc
    return rel.as_posix()
