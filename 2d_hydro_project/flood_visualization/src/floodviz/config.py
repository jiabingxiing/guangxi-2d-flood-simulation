"""配置加载与结构校验。

配置分三层：
  configs/default.yaml      —— 项目级参数（阈值、色带、底图、案例清单）
  configs/cases/<id>.yaml   —— 每案例的 run 白名单、时间依据、图层状态
  configs/layers.yaml       —— 图层顺序、预设、图例
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SCHEMA_VERSION = "1.0"

ALLOWED_LAYER_STATUS = {
    "remote_verified",
    "local_unchecked",
    "ready",
    "partial",
    "missing",
    "failed",
}
ENABLED_LAYER_STATUS = {"ready", "partial"}
ALLOWED_ORIGIN_STATUS = {
    "model_schedule_confirmed",
    "relative_only_candidate",
    "relative_only",
}


class ConfigError(ValueError):
    """配置结构或取值不合法。"""


@dataclass(frozen=True)
class RunConfig:
    run_id: str
    label: str
    role: str
    source_dir: Path
    save_interval_s: float
    simulation_duration_s: float
    time_origin: str | None
    origin_status: str
    origin_note: str
    notes: str = ""
    optional: bool = False

    @property
    def expected_frame_count(self) -> int:
        return round(self.simulation_duration_s / self.save_interval_s) + 1


@dataclass(frozen=True)
class ProductLayerConfig:
    """Phase H 研究性产品图层（单帧伪 run，role=product；ADR-0015~0018）。

    source 指向 `outputs/products/**` 的 GeoTIFF；kind 决定配色与值语义
    （fat_first_wet_h / fat_exceed_h / fat_duration_h / ensemble_probability /
    class_map）。source_run/mass_gate_run_path 用于质量门引用；audit 指向
    `data/catalog/**` 审计件（项目相对），作为产品证据链。
    """

    product_id: str
    label: str
    kind: str
    source_tif: Path
    save_interval_s: float = 3600.0
    threshold_m: float | None = None
    source_run: str | None = None
    mass_gate_run_path: str | None = None
    audit: str | None = None
    adr: str | None = None
    notes: str = ""
    optional: bool = False


@dataclass(frozen=True)
class LayerConfig:
    layer_id: str
    status: str
    label: str
    kind: str
    summary: str = ""
    note: str = ""
    asset_id: str | None = None
    acquired_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def selectable(self) -> bool:
        return self.status in ENABLED_LAYER_STATUS


@dataclass(frozen=True)
class CaseConfig:
    case_id: str
    name: str
    event_id: str
    event_name: str
    scenario_role: str
    subtitle: str
    model_bbox: tuple[float, float, float, float]  # WGS84 西南东北
    crs: str
    default_run_id: str
    runs: dict[str, RunConfig]
    origin_note: str
    sites: list[dict[str, Any]]
    layers: dict[str, LayerConfig]
    quality: dict[str, Any]
    sources: dict[str, Any]
    time_zone: str = "Asia/Shanghai"
    # 源地形（未修改）与地形修改证据；缺省表示该案例不提供分层
    terrain: dict[str, Any] = field(default_factory=dict)
    # Phase H 研究性产品图层（FAT/归因/概率）；缺省为空
    product_layers: tuple[ProductLayerConfig, ...] = field(default_factory=tuple)

    def run(self, run_id: str | None = None) -> RunConfig:
        rid = run_id or self.default_run_id
        try:
            return self.runs[rid]
        except KeyError as exc:  # pragma: no cover - 配置错误路径
            raise ConfigError(f"案例 {self.case_id} 无 run {rid!r}") from exc


@dataclass(frozen=True)
class Config:
    path: Path
    root: Path  # 子项目根目录 flood_visualization/
    parent_root: Path  # 父项目根目录 2d_hydro_project/
    project: dict[str, Any]
    thresholds_m: tuple[float, ...]
    default_threshold_m: float
    depth_classes: tuple[dict[str, Any], ...]
    basemaps: tuple[dict[str, Any], ...]
    layer_order: tuple[str, ...]
    presets: tuple[dict[str, Any], ...]
    legends: dict[str, Any]
    cases: dict[str, CaseConfig]
    default_case: str
    bundle_root: Path
    image_max_pixels: int
    block_size: int
    license_notice: str
    area_threshold_note: str = ""
    temporal_note: str = "6 h/帧离散回放；播放速度不改变物理时间分辨率"
    max_envelope_title: str = "全程最大水深包络（非某一时刻）"
    current_frame_title: str = "当前帧模拟水深"

    def case(self, case_id: str) -> CaseConfig:
        try:
            return self.cases[case_id]
        except KeyError as exc:  # pragma: no cover
            raise ConfigError(f"未知案例 {case_id!r}；可选：{sorted(self.cases)}") from exc

    @property
    def case_ids(self) -> tuple[str, ...]:
        return tuple(self.cases)


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping or mapping[key] in (None, ""):
        raise ConfigError(f"{where} 缺少必填字段 {key!r}")
    return mapping[key]


def _parse_iso_datetime(value: str, where: str) -> _dt.datetime:
    try:
        parsed = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigError(f"{where} 的 time_origin 不是合法 ISO8601：{value!r}") from exc
    if parsed.tzinfo is None:
        raise ConfigError(f"{where} 的 time_origin 必须带时区偏移：{value!r}")
    return parsed


def _load_layer(layer_id: str, raw: Any, where: str) -> LayerConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}.layers.{layer_id} 必须是对象")
    status = _require(raw, "status", f"{where}.layers.{layer_id}")
    if status not in ALLOWED_LAYER_STATUS:
        raise ConfigError(f"{where}.layers.{layer_id}.status={status!r} 非法；允许 {sorted(ALLOWED_LAYER_STATUS)}")
    known = {"status", "label", "kind", "summary", "note", "asset_id", "acquired_at"}
    return LayerConfig(
        layer_id=layer_id,
        status=status,
        label=str(raw.get("label", layer_id)),
        kind=str(raw.get("kind", "raster")),
        summary=str(raw.get("summary", "")),
        note=str(raw.get("note", "")),
        asset_id=raw.get("asset_id"),
        acquired_at=raw.get("acquired_at"),
        extra={k: v for k, v in raw.items() if k not in known},
    )


def _load_terrain(raw: Any, where: str, parent_root: Path) -> dict[str, Any]:
    """读案例 terrain 段：源地形栅格与地形修改证据。

    缺省返回空字典（该案例不提供地形分层）。
    """
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}.terrain 必须是 YAML 对象")
    out: dict[str, Any] = {}
    source = raw.get("source_dem")
    if source:
        path = Path(str(source))
        out["source_dem"] = path if path.is_absolute() else (parent_root / path)
    out["source_label"] = str(raw.get("source_label", "源 DEM（未修改）"))
    out["source_note"] = str(raw.get("source_note", ""))
    edit = raw.get("edit_mask")
    if edit:
        path = Path(str(edit))
        out["edit_mask"] = path if path.is_absolute() else (parent_root / path)
    return out


def _load_case(path: Path, parent_root: Path) -> CaseConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} 必须是 YAML 对象")
    where = path.name
    case_id = str(_require(raw, "case_id", where))
    bbox = raw.get("model_bbox")
    if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
        raise ConfigError(f"{where}.model_bbox 必须是 4 个数")
    west, south, east, north = (float(v) for v in bbox)
    if not (west < east and south < north):
        raise ConfigError(f"{where}.model_bbox 必须为 [西,南,东,北]")
    runs: dict[str, RunConfig] = {}
    for run_raw in _require(raw, "runs", where):
        rid = str(_require(run_raw, "run_id", f"{where}.runs"))
        source = Path(str(_require(run_raw, "source", f"{where}.runs.{rid}")))
        source_dir = source if source.is_absolute() else (parent_root / source)
        time_origin = run_raw.get("time_origin", raw.get("time_origin"))
        origin_status = str(run_raw.get("origin_status", raw.get("origin_status", "relative_only")))
        if origin_status not in ALLOWED_ORIGIN_STATUS:
            raise ConfigError(f"{where}.runs.{rid}.origin_status={origin_status!r} 非法")
        if time_origin is not None:
            _parse_iso_datetime(str(time_origin), f"{where}.runs.{rid}")
        runs[rid] = RunConfig(
            run_id=rid,
            label=str(run_raw.get("label", rid)),
            role=str(run_raw.get("role", "scenario")),
            source_dir=source_dir,
            save_interval_s=float(_require(run_raw, "save_interval_s", f"{where}.runs.{rid}")),
            simulation_duration_s=float(_require(run_raw, "simulation_duration_s", f"{where}.runs.{rid}")),
            time_origin=str(time_origin) if time_origin is not None else None,
            origin_status=origin_status,
            origin_note=str(run_raw.get("origin_note", raw.get("origin_note", ""))),
            notes=str(run_raw.get("notes", "")),
            optional=bool(run_raw.get("optional", False)),
        )
    default_run_id = str(_require(raw, "default_run_id", where))
    if default_run_id not in runs:
        raise ConfigError(f"{where}.default_run_id={default_run_id!r} 不在 runs 中")
    product_layers: list[ProductLayerConfig] = []
    known_kinds = {"fat_first_wet_h", "fat_exceed_h", "fat_duration_h",
                   "ensemble_probability", "class_map"}
    for pl_raw in raw.get("product_layers") or []:
        pid = str(_require(pl_raw, "product_id", f"{where}.product_layers"))
        kind = str(_require(pl_raw, "kind", f"{where}.product_layers.{pid}"))
        if kind not in known_kinds:
            raise ConfigError(
                f"{where}.product_layers.{pid}.kind={kind!r} 非法；允许 {sorted(known_kinds)}")
        if kind in ("fat_exceed_h", "fat_duration_h") and pl_raw.get("threshold_m") is None:
            raise ConfigError(f"{where}.product_layers.{pid}: kind={kind} 必须给 threshold_m")
        psource = Path(str(_require(pl_raw, "source", f"{where}.product_layers.{pid}")))
        product_layers.append(ProductLayerConfig(
            product_id=pid,
            label=str(pl_raw.get("label", pid)),
            kind=kind,
            source_tif=psource if psource.is_absolute() else (parent_root / psource),
            save_interval_s=float(pl_raw.get("save_interval_s", 3600.0)),
            threshold_m=(float(pl_raw["threshold_m"]) if pl_raw.get("threshold_m") is not None else None),
            source_run=(str(pl_raw["source_run"]) if pl_raw.get("source_run") else None),
            mass_gate_run_path=(str(pl_raw["mass_gate_run_path"]) if pl_raw.get("mass_gate_run_path") else None),
            audit=(str(pl_raw["audit"]) if pl_raw.get("audit") else None),
            adr=(str(pl_raw["adr"]) if pl_raw.get("adr") else None),
            notes=str(pl_raw.get("notes", "")),
            optional=bool(pl_raw.get("optional", False)),
        ))
    layers: dict[str, LayerConfig] = {}
    for layer_id, layer_raw in (raw.get("layers") or {}).items():
        layers[str(layer_id)] = _load_layer(str(layer_id), layer_raw, where)
    return CaseConfig(
        case_id=case_id,
        name=str(_require(raw, "name", where)),
        event_id=str(_require(raw, "event_id", where)),
        event_name=str(raw.get("event_name", "")),
        scenario_role=str(raw.get("scenario_role", "")),
        subtitle=str(raw.get("subtitle", "")),
        model_bbox=(west, south, east, north),
        crs=str(_require(raw, "crs", where)),
        default_run_id=default_run_id,
        runs=runs,
        origin_note=str(raw.get("origin_note", "")),
        sites=list(raw.get("sites") or []),
        layers=layers,
        quality=dict(raw.get("quality") or {}),
        sources=dict(raw.get("sources") or {}),
        time_zone=str(raw.get("time_zone", "Asia/Shanghai")),
        terrain=_load_terrain(raw.get("terrain"), where, parent_root),
        product_layers=tuple(product_layers),
    )


def load_config(config_path: str | Path) -> Config:
    path = Path(config_path).resolve()
    if not path.is_file():
        raise ConfigError(f"配置文件不存在：{path}")
    root = path.parent.parent  # configs/default.yaml -> 子项目根
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} 必须是 YAML 对象")
    project = dict(_require(raw, "project", "default.yaml"))
    parent_root = (root / str(project.get("parent_root", ".."))).resolve()
    if not parent_root.is_dir():
        raise ConfigError(f"parent_root 不存在：{parent_root}")

    thresholds = tuple(float(v) for v in _require(raw, "thresholds_m", "default.yaml"))
    if (
        list(thresholds) != sorted(set(thresholds))
        or len(set(thresholds)) != len(thresholds)
        or any(t <= 0 for t in thresholds)
    ):
        raise ConfigError(f"thresholds_m 必须是严格递增的正数（不得重复）：{list(thresholds)}")
    depth_classes = tuple(raw.get("depth_classes") or ())
    if not depth_classes:
        raise ConfigError("default.yaml 缺少 depth_classes（固定色带）")

    cases: dict[str, CaseConfig] = {}
    for entry in _require(raw, "cases", "default.yaml"):
        case_path = (root / str(entry["config"])).resolve()
        case = _load_case(case_path, parent_root)
        if case.case_id in cases:
            raise ConfigError(f"重复案例 {case.case_id!r}")
        cases[case.case_id] = case

    layers_raw = raw.get("layers_file")
    layers_doc: dict[str, Any] = {}
    if layers_raw:
        layers_doc = yaml.safe_load((root / str(layers_raw)).read_text(encoding="utf-8")) or {}

    basemaps_doc: dict[str, Any] = {}
    basemaps_file = raw.get("basemaps_file")
    if basemaps_file:
        basemap_path = (root / str(basemaps_file)).resolve()
        if not basemap_path.is_file():
            raise ConfigError(f"basemaps_file 不存在：{basemap_path}")
        basemaps_doc = yaml.safe_load(basemap_path.read_text(encoding="utf-8")) or {}

    basemaps = tuple(
        raw.get("basemaps") or basemaps_doc.get("basemaps") or layers_doc.get("basemaps") or ()
    )
    if not basemaps:
        raise ConfigError("未配置底图：请在 configs/basemaps.yaml 或 default.yaml 中定义")
    for bm in basemaps:
        for key in ("id", "label", "tiles", "tile_size", "attribution"):
            if key not in bm:
                raise ConfigError(f"basemap {bm.get('id')!r} 缺少 {key!r}")

    default_case = str(raw.get("default_case") or next(iter(cases)))
    if default_case not in cases:
        raise ConfigError(f"default_case={default_case!r} 不在案例清单中")

    bundle_root = (root / str(project.get("bundle_root", "outputs/web_bundle"))).resolve()
    display = dict(raw.get("display") or {})
    return Config(
        path=path,
        root=root,
        parent_root=parent_root,
        project=project,
        thresholds_m=thresholds,
        default_threshold_m=float(raw.get("default_threshold_m", thresholds[0])),
        depth_classes=depth_classes,
        basemaps=basemaps,
        layer_order=tuple(
            layers_doc.get("layer_order")
            or (
                "basemap", "terrain_source", "terrain_edit", "dem",
                "optical", "sar", "flood", "max", "boundary", "sites",
            )
        ),
        presets=tuple(layers_doc.get("presets") or ()),
        legends=dict(layers_doc.get("legends") or {}),
        cases=cases,
        default_case=default_case,
        bundle_root=bundle_root,
        image_max_pixels=int(raw.get("image_max_pixels", 4096)),
        block_size=int(raw.get("block_size", 256)),
        license_notice=str(project.get("license_notice", "")),
        area_threshold_note=str(raw.get("area_threshold_note") or display.get("area_threshold_note") or ""),
        temporal_note=str(display.get("temporal_note") or "6 h/帧离散回放；播放速度不改变物理时间分辨率"),
        max_envelope_title=str(display.get("max_envelope_title") or "全程最大水深包络（非某一时刻）"),
        current_frame_title=str(display.get("current_frame_title") or "当前帧模拟水深"),
    )
