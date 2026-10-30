"""catalog 组装与校验单元测试：几何四角、资产降级、结构/状态机/路径安全门禁。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from conftest import PROJECT_ROOT
from floodviz.catalog.catalog import (
    apply_assets,
    build_case_entry,
    build_catalog,
    build_run_entry,
    bundle_rel,
    geometry_for_grid,
    validate_catalog,
)
from floodviz.catalog.quality import load_mass_gate, merge_quality
from floodviz.raster.grid import Grid, header_to_grid

CATALOG_SCHEMA = PROJECT_ROOT / "schemas" / "catalog.schema.json"
SYNTH_HEADER = {
    "ncols": 8,
    "nrows": 6,
    "xllcorner": 246900.0,
    "yllcorner": 2456400.0,
    "cellsize": 200.0,
    "nodata_value": -9999.0,
}


def _grid(crs: str = "EPSG:32649") -> Grid:
    return header_to_grid(SYNTH_HEADER, crs)


def _affine(x: float, y: float) -> tuple[float, float]:
    """确定性伪投影：北向上 UTM 量级 → 经纬度量级（仅用于几何顺序断言）。"""
    return (x * 1e-5 + 100.0, y * 1e-5)


# ---------------------------------------------------------------- 最小合法文档


def _valid_run() -> dict[str, Any]:
    return {
        "run_id": "run_a",
        "label": "默认情景",
        "role": "default",
        "frame_count": 3,
        "save_interval_s": 21600.0,
        "simulation_duration_s": 43200.0,
        "timeline": "cases/demo/run_a/timeline.json",
        "statistics": "cases/demo/run_a/statistics.json",
        "numeric_manifest": "numeric/demo/run_a/manifest.json",
        "optional": False,
        "notes": "",
        "quality": {
            "mass_gate": "PASS",
            "e_mass": 0.0005,
            "validation_status": "降级模式情景级",
            "vertical_datum_status": "未闭合",
            "warnings": ["情景级成果"],
        },
    }


def _valid_layers() -> dict[str, Any]:
    return {
        "flood": {
            "status": "ready",
            "label": "模型水深（逐帧）",
            "kind": "frame",
            "images": "frames/demo/run_a/images.json",
        },
        "max": {
            "status": "ready",
            "label": "全程最大水深包络",
            "kind": "max_envelope",
            "image": "frames/demo/run_a/max_r0c0.png",
        },
        "dem": {
            "status": "ready",
            "label": "DEM 高程",
            "kind": "raster",
            "image": "frames/demo/dem.png",
        },
        "sar": {"status": "missing", "label": "SAR 水体", "kind": "raster", "summary": "资产未导入"},
        "optical": {
            "status": "partial",
            "label": "可见光背景",
            "kind": "raster",
            "summary": "部分覆盖模型域",
            "image": "frames/demo/optical/asset_a.png",
            "footprint": "cases/demo/optical_footprint_asset_a.geojson",
        },
        "boundary": {
            "status": "ready",
            "label": "模型域外框",
            "kind": "vector",
            "geojson": "cases/demo/boundary.geojson",
        },
    }


def _valid_case_entry() -> dict[str, Any]:
    return {
        "case_id": "demo",
        "name": "演示",
        "event_id": "2024-09-test",
        "event_name": "测试事件",
        "scenario_role": "研究性情景",
        "subtitle": "200 m 降级情景 · 3 帧",
        "model_bbox": [108.6, 21.94, 108.99, 22.22],
        "grid": {
            "rows": 6,
            "cols": 8,
            "transform": [200.0, 0.0, 246900.0, 0.0, -200.0, 2457600.0],
            "crs": "EPSG:32649",
            "cell_size_m": 200.0,
            "cell_area_m2": 40000.0,
            "coordinates": [[108.6, 22.22], [108.99, 22.22], [108.99, 21.94], [108.6, 21.94]],
        },
        "time": {
            "time_origin": None,
            "time_zone": "Asia/Shanghai",
            "origin_status": "relative_only_candidate",
            "origin_note": "测试用候选起点",
            "save_interval_s": 21600.0,
            "temporal_interpolation": "none",
        },
        "default_run_id": "run_a",
        "runs": [_valid_run()],
        "layers": _valid_layers(),
        "sites": [],
        "quality": {
            "mass_gate": "PASS",
            "e_mass": 0.0005,
            "validation_status": "降级模式情景级",
            "vertical_datum_status": "未闭合",
            "warnings": ["情景级成果"],
        },
        "sources": {"model": "LISFLOOD-FP 8.1.0"},
    }


def _valid_catalog() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "bundle_id": "test-20261027",
        "created_utc": "2026-10-27T10:00:00+00:00",
        "source_manifest_hash": "sha256:" + "a" * 64,
        "license_notice": "研究性情景成果，非业务预警",
        "default_case": "demo",
        "display": {
            "project_title": "演示项目",
            "subtitle": "研究性情景成果",
            "default_threshold_m": 0.05,
            "thresholds_m": [0.05, 0.15],
            "depth_classes": [
                {"min_m": 0.05, "max_m": 0.15, "color": "#b7e4c7", "label": "0.05–0.15 m"},
                {"min_m": 0.15, "max_m": None, "color": "#74c69d", "label": "≥0.15 m"},
            ],
            "temporal_note": "6 h/帧离散回放；播放速度不改变物理时间分辨率",
        },
        "basemaps": [
            {
                "id": "esri_world_imagery",
                "label": "Esri World Imagery（在线）",
                "tiles": [
                    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
                ],
                "tile_size": 256,
                "attribution": "Source: Esri",
                "requires_network": True,
            }
        ],
        "cases": [_valid_case_entry()],
    }


def _expect_invalid(doc: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        validate_catalog(doc)


# ---------------------------------------------------------------- 结构门禁


def test_valid_catalog_passes() -> None:
    doc = _valid_catalog()
    validate_catalog(doc)
    jsonschema.validate(doc, json.loads(CATALOG_SCHEMA.read_text(encoding="utf-8")))


def test_reject_missing_top_level_field() -> None:
    doc = _valid_catalog()
    del doc["bundle_id"]
    _expect_invalid(doc, "缺少必填顶层字段")


def test_validate_catalog_covers_schema_required_fields() -> None:
    doc = _valid_catalog()
    del doc["license_notice"]
    with pytest.raises(ValueError, match="缺少必填顶层字段"):
        validate_catalog(doc)
    doc = _valid_catalog()
    doc["cases"][0]["layers"]["dem"]["image"] = "\\\\server\\share\\dem.png"
    with pytest.raises(ValueError, match="必须是包内相对路径"):
        validate_catalog(doc)


def test_reject_wrong_schema_version() -> None:
    doc = _valid_catalog()
    doc["schema_version"] = "2.0"
    _expect_invalid(doc, "schema_version 必须为 1.0")


def test_reject_missing_case_field() -> None:
    doc = _valid_catalog()
    del doc["cases"][0]["sources"]
    _expect_invalid(doc, "case 缺少字段 'sources'")


def test_reject_duplicate_case_id() -> None:
    doc = _valid_catalog()
    doc["cases"].append(copy.deepcopy(doc["cases"][0]))
    _expect_invalid(doc, "重复 case_id")


def test_reject_unordered_bbox() -> None:
    doc = _valid_catalog()
    doc["cases"][0]["model_bbox"] = [109.0, 22.2, 108.5, 21.9]
    _expect_invalid(doc, "model_bbox")


def test_reject_empty_runs() -> None:
    doc = _valid_catalog()
    doc["cases"][0]["runs"] = []
    _expect_invalid(doc, "缺少字段 'runs'")


def test_reject_default_run_not_in_runs() -> None:
    doc = _valid_catalog()
    doc["cases"][0]["default_run_id"] = "run_missing"
    _expect_invalid(doc, "default_run_id 不在 runs 中")


def test_reject_default_case_not_in_cases() -> None:
    doc = _valid_catalog()
    doc["default_case"] = "nowhere"
    _expect_invalid(doc, "default_case 不在 cases 中")


def test_reject_non_positive_frame_count() -> None:
    doc = _valid_catalog()
    doc["cases"][0]["runs"][0]["frame_count"] = 0
    _expect_invalid(doc, "frame_count 非正")


def test_reject_illegal_mass_gate() -> None:
    doc = _valid_catalog()
    doc["cases"][0]["runs"][0]["quality"]["mass_gate"] = "OK"
    _expect_invalid(doc, "mass_gate 非法")


# ---------------------------------------------------------------- 状态机


def test_reject_illegal_layer_status() -> None:
    doc = _valid_catalog()
    doc["cases"][0]["layers"]["sar"]["status"] = "processing"
    _expect_invalid(doc, r"status='processing' 非法")


def test_reject_unavailable_layer_without_reason() -> None:
    doc = _valid_catalog()
    doc["cases"][0]["layers"]["sar"] = {"status": "failed", "label": "SAR 水体", "kind": "raster"}
    _expect_invalid(doc, "不可用但未给出原因")


def test_reject_missing_flood_layer() -> None:
    doc = _valid_catalog()
    del doc["cases"][0]["layers"]["flood"]
    _expect_invalid(doc, "缺少可用 flood 图层")


def test_reject_flood_layer_not_ready() -> None:
    doc = _valid_catalog()
    doc["cases"][0]["layers"]["flood"]["status"] = "partial"
    doc["cases"][0]["layers"]["flood"]["summary"] = "部分"
    _expect_invalid(doc, "缺少可用 flood 图层")


@pytest.mark.parametrize("layer_id", ["max", "dem"])
def test_reject_missing_required_layer_doc(layer_id: str) -> None:
    doc = _valid_catalog()
    del doc["cases"][0]["layers"][layer_id]
    _expect_invalid(doc, f"缺少 {layer_id} 图层文档")


# ---------------------------------------------------------------- 路径安全


@pytest.mark.parametrize(
    "bad_path",
    [
        "https://example.com/dem.png",
        "/frames/demo/dem.png",
        "C:/data/frames/demo/dem.png",
    ],
)
def test_reject_absolute_layer_path(bad_path: str) -> None:
    doc = _valid_catalog()
    doc["cases"][0]["layers"]["dem"]["image"] = bad_path
    _expect_invalid(doc, "必须是包内相对路径")


@pytest.mark.parametrize(
    "bad_path",
    ["C:/bundle/cases/demo/run_a/timeline.json", "/cases/demo/run_a/timeline.json"],
)
def test_reject_absolute_run_path(bad_path: str) -> None:
    doc = _valid_catalog()
    doc["cases"][0]["runs"][0]["timeline"] = bad_path
    _expect_invalid(doc, "必须是包内相对路径")


def test_reject_numeric_manifest_outside_numeric() -> None:
    doc = _valid_catalog()
    doc["cases"][0]["runs"][0]["numeric_manifest"] = "cases/demo/run_a/manifest.json"
    _expect_invalid(doc, "numeric_manifest 必须是包内相对路径")


# ---------------------------------------------------------------- 几何


def test_geometry_for_grid_corner_order() -> None:
    geom = geometry_for_grid(_grid(), crs_to_wgs84=_affine)
    coords = geom["coordinates"]
    assert len(coords) == 4
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    # TL: 西、北；TR: 东、北；BR: 东、南；BL: 西、南
    assert coords[0] == pytest.approx([min(lons), max(lats)])
    assert coords[1] == pytest.approx([max(lons), max(lats)])
    assert coords[2] == pytest.approx([max(lons), min(lats)])
    assert coords[3] == pytest.approx([min(lons), min(lats)])


def test_geometry_for_grid_bbox_contains_corners() -> None:
    geom = geometry_for_grid(_grid(), crs_to_wgs84=_affine)
    west, south, east, north = geom["bbox_wgs84"]
    assert west < east and south < north
    for lon, lat in geom["coordinates"]:
        assert west <= lon <= east
        assert south <= lat <= north
    assert west == pytest.approx(min(c[0] for c in geom["coordinates"]))
    assert north == pytest.approx(max(c[1] for c in geom["coordinates"]))


def test_geometry_for_grid_keeps_grid_metadata() -> None:
    grid = _grid()
    geom = geometry_for_grid(grid, crs_to_wgs84=_affine)
    assert geom["rows"] == grid.rows
    assert geom["cols"] == grid.cols
    assert geom["crs"] == "EPSG:32649"
    assert geom["transform"] == list(grid.transform)
    assert geom["cell_size_m"] == pytest.approx(grid.cell_size_m)
    assert geom["cell_area_m2"] == pytest.approx(grid.cell_area_m2)


def test_geometry_for_grid_with_real_projection() -> None:
    """真实 pyproj 变换：UTM 49N → WGS84，四角落在久隆量级经纬度，且可反算回原生坐标。"""
    pyproj = pytest.importorskip("pyproj")
    transformer = pyproj.Transformer.from_crs("EPSG:32649", "EPSG:4326", always_xy=True)
    inverse = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:32649", always_xy=True)
    grid = _grid()
    geom = geometry_for_grid(grid, crs_to_wgs84=transformer.transform)
    west, south, east, north = geom["bbox_wgs84"]
    assert 108.2 < west < east < 109.2
    assert 21.8 < south < north < 22.6
    for (lon, lat), (x, y) in zip(geom["coordinates"], grid.corners_native(), strict=True):
        back_x, back_y = inverse.transform(lon, lat)
        # 四角经纬度按 7 位小数写入（约 1 cm），反算误差应远小于 1 个像元
        assert back_x == pytest.approx(x, abs=0.02)
        assert back_y == pytest.approx(y, abs=0.02)


# ---------------------------------------------------------------- 资产降级


def _layer_docs_for_assets() -> dict[str, dict[str, Any]]:
    return {
        "dem": {"status": "ready", "label": "DEM", "kind": "raster", "image": "frames/demo/dem.png"},
        "optical": {
            "status": "partial",
            "label": "可见光",
            "kind": "raster",
            "asset_id": "demo_optical",
            "summary": "配置预期部分覆盖",
        },
        "sar": {"status": "ready", "label": "SAR", "kind": "raster", "asset_id": "demo_sar"},
    }


def test_apply_assets_without_manifest_degrades_to_missing(config) -> None:
    out = apply_assets(config.case("jiulong"), _layer_docs_for_assets(), None)
    for layer_id in ("optical", "sar"):
        assert out[layer_id]["status"] == "missing"
        assert out[layer_id]["missing_reason"]
        assert "image" not in out[layer_id]
    # 没有 asset_id 的图层保持原样
    assert out["dem"]["status"] == "ready"
    assert out["dem"]["image"] == "frames/demo/dem.png"
    # 输入不被就地修改
    assert _layer_docs_for_assets()["optical"]["status"] == "partial"


def test_apply_assets_merges_measured_state(config) -> None:
    manifest = {
        "assets": [
            {
                "case_id": "jiulong",
                "layer": "optical",
                "asset_id": "demo_optical",
                "status": "partial",
                "image": "frames/jiulong/optical/demo_optical.png",
                "footprint": "cases/jiulong/optical_footprint_demo_optical.geojson",
                "acquired_at": "2026-04-03T00:00:00+08:00",
                "summary": "实测：覆盖模型域 62%",
                "role": "background",
            },
            {
                "case_id": "jiulong",
                "layer": "sar",
                "asset_id": "demo_sar",
                "status": "ready",
                "image": "frames/jiulong/sar.png",
                "footprint": "cases/jiulong/sar_footprint.geojson",
                "acquired_at": "2024-09-17T10:50:23+08:00",
                "sensor": "Sentinel-1",
                "legend": "binary_water",
            },
        ]
    }
    out = apply_assets(config.case("jiulong"), _layer_docs_for_assets(), manifest)
    assert out["optical"]["status"] == "partial"
    assert out["optical"]["image"] == "frames/jiulong/optical/demo_optical.png"
    assert out["optical"]["summary"] == "实测：覆盖模型域 62%"
    assert out["optical"]["role"] == "background"
    assert out["sar"]["status"] == "ready"
    assert out["sar"]["sensor"] == "Sentinel-1"
    assert out["sar"]["legend"] == "binary_water"
    assert "missing_reason" not in out["sar"]


def test_apply_assets_failed_asset_is_not_selectable(config) -> None:
    docs = {"sar": {"status": "ready", "label": "SAR", "kind": "raster", "asset_id": "demo_sar"}}
    manifest = {
        "assets": [
            {
                "case_id": "jiulong",
                "layer": "sar",
                "asset_id": "demo_sar",
                "status": "failed",
                "summary": "资产质检失败：投影未知",
            }
        ]
    }
    out = apply_assets(config.case("jiulong"), docs, manifest)
    assert out["sar"]["status"] == "failed"
    assert out["sar"]["summary"].startswith("资产质检失败")
    assert out["sar"]["status"] not in {"ready", "partial"}


def test_apply_assets_unknown_asset_id_is_missing(config) -> None:
    docs = {"sar": {"status": "ready", "label": "SAR", "kind": "raster", "asset_id": "other_case_asset"}}
    manifest = {"assets": [{"case_id": "guigang", "layer": "sar", "asset_id": "other_case_asset", "status": "ready"}]}
    out = apply_assets(config.case("jiulong"), docs, manifest)
    assert out["sar"]["status"] == "missing"


# ---------------------------------------------------------------- bundle 相对路径


def test_bundle_rel_posix_and_guard(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    target = bundle / "frames" / "demo" / "run_a" / "f0000_r0c0.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"png")
    assert bundle_rel(target, bundle) == "frames/demo/run_a/f0000_r0c0.png"
    outside = tmp_path / "elsewhere" / "x.png"
    outside.parent.mkdir()
    outside.write_bytes(b"x")
    with pytest.raises(ValueError, match="不在发布包"):
        bundle_rel(outside, bundle)


# ---------------------------------------------------------------- 组装真实配置


def _real_case_entry(config, parent_root: Path, case_id: str) -> dict[str, Any]:
    case = config.case(case_id)
    run = case.run()
    grid = _grid(case.crs)
    geometry = geometry_for_grid(grid, crs_to_wgs84=_affine)
    run_rel = run.source_dir.relative_to(parent_root).as_posix()
    quality = merge_quality(case.quality, load_mass_gate(parent_root, run_rel))
    run_doc = {
        "run_id": run.run_id,
        "label": run.label,
        "role": run.role,
        "frame_count": run.expected_frame_count,
        "save_interval_s": run.save_interval_s,
        "simulation_duration_s": run.simulation_duration_s,
        "timeline": f"cases/{case_id}/{run.run_id}/timeline.json",
        "statistics": f"cases/{case_id}/{run.run_id}/statistics.json",
        "numeric_manifest": f"numeric/{case_id}/{run.run_id}/manifest.json",
        "optional": run.optional,
        "notes": run.notes,
    }
    run_entry = build_run_entry(case, run_doc, quality)
    layer_docs = {
        "flood": {
            "status": "ready",
            "label": "模型水深（逐帧）",
            "kind": "frame",
            "images": f"frames/{case_id}/{run.run_id}/images.json",
        },
        "max": {
            "status": "ready",
            "label": "全程最大水深包络",
            "kind": "max_envelope",
            "image": f"frames/{case_id}/{run.run_id}/max_r0c0.png",
        },
        "dem": {"status": "ready", "label": "DEM 高程", "kind": "raster", "image": f"frames/{case_id}/dem.png"},
    }
    for layer_id, layer in case.layers.items():
        if layer_id in layer_docs:
            continue
        doc = {"status": layer.status, "label": layer.label, "kind": layer.kind}
        if layer.summary:
            doc["summary"] = layer.summary
        if layer.note:
            doc["note"] = layer.note
        if layer.status in {"ready", "partial"}:
            # 真实链路里 apply_assets 会补 image/geojson；这里用占位相对路径满足契约
            doc["image"] = f"frames/{case_id}/{layer_id}.png"
        layer_docs[layer_id] = doc
    return build_case_entry(
        case, grid_geometry=geometry, run_entries=[run_entry], layer_docs=layer_docs, quality=quality
    )


def test_build_catalog_for_real_config(config, parent_root: Path) -> None:
    entries = [_real_case_entry(config, parent_root, case_id) for case_id in config.case_ids]
    doc = build_catalog(
        config,
        bundle_id="qa-test-20261027",
        created_utc="2026-10-27T10:00:00+00:00",
        source_manifest_hash="sha256:" + "b" * 64,
        case_entries=entries,
    )
    validate_catalog(doc)
    jsonschema.validate(doc, json.loads(CATALOG_SCHEMA.read_text(encoding="utf-8")))
    assert doc["default_case"] == "jiulong"
    assert [c["case_id"] for c in doc["cases"]] == ["jiulong", "ningming", "guigang"]
    assert len(doc["display"]["thresholds_m"]) == 8
    assert doc["display"]["temporal_note"].startswith("6 h/帧")
    assert doc["display"]["max_envelope_title"]
    assert len(doc["basemaps"]) == 2
    assert {b["id"] for b in doc["basemaps"]} == {"esri_world_imagery", "esri_world_street"}
    assert all("https://" in b["tiles"][0] for b in doc["basemaps"])
    assert [p["id"] for p in doc["presets"]] == [
        "optical_only",
        "sar_only",
        "dem_only",
        "terrain_source_only",
        "terrain_edit_only",
        "flood_only",
        "all_available",
        "custom",
    ]
    # 久隆质量门取真实复核值（PASS），宁明/贵港同样只允许四态
    jiulong_run = doc["cases"][0]["runs"][0]
    assert jiulong_run["quality"]["mass_gate"] == "PASS"
    assert jiulong_run["quality"]["e_mass"] <= 1e-3
    for entry in doc["cases"]:
        for run in entry["runs"]:
            assert run["quality"]["mass_gate"] in {"PASS", "FAIL-EXPLAINED", "FAIL", "unrecorded"}


def test_build_catalog_display_area_note_is_filled(config, parent_root: Path) -> None:
    """SPEC §4 要求 display 的 area_threshold_note 与三个标题字段有内容。"""
    assert config.area_threshold_note, "Config.area_threshold_note 必须从 default.yaml 顶层读入"
    assert config.project.get("area_threshold_note") is None  # 说明它在 project 之外
    entries = [_real_case_entry(config, parent_root, case_id) for case_id in config.case_ids]
    doc = build_catalog(
        config,
        bundle_id="qa-note-20261027",
        created_utc="2026-10-27T10:00:00+00:00",
        source_manifest_hash="sha256:" + "c" * 64,
        case_entries=entries,
    )
    display = doc["display"]
    assert display["area_threshold_note"] == config.area_threshold_note
    assert display["area_threshold_note"].startswith("面积为原生模型网格")
    assert display["temporal_note"].startswith("6 h/帧")
    assert "全程最大水深包络" in display["max_envelope_title"]
    assert display["current_frame_title"]


def test_catalog_case_entries_have_no_absolute_paths(config, parent_root: Path) -> None:
    entries = [_real_case_entry(config, parent_root, case_id) for case_id in config.case_ids]

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            assert not node.startswith(("http://", "/", "\\")), node
            assert "C:\\" not in node and "D:\\" not in node, node

    walk(entries)
