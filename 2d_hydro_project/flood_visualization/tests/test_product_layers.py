"""Phase H 产品图层（FAT/归因/概率）接入发布管线单测。

覆盖：样式/值语义（ADR-0015 口径）、产品 GeoTIFF 读取与重投影、统计文档标准
schema 自洽、`_process_product_layer` 端到端（timeline/statistics/images/numeric
契约逐项过 validate 对应检查组）、未知 kind 拒绝、缺产品文件 fail-closed。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import Affine

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ))

from floodviz.config import CaseConfig, Config, ProductLayerConfig, RunConfig  # noqa: E402
from floodviz.pipeline import _process_product_layer  # noqa: E402
from floodviz.publishing.products import (  # noqa: E402
    product_stats_doc,
    read_product_array,
    style_for,
)
from floodviz.quality.validate import _check_images, _check_statistics, _check_timeline  # noqa: E402
from floodviz.raster import paint as paint_mod  # noqa: E402
from floodviz.raster.grid import Grid  # noqa: E402

HEADER = {
    "ncols": 6, "nrows": 6, "xllcorner": 357000.0, "yllcorner": 2542000.0,
    "cellsize": 100.0, "nodata_value": -9999.0,
}
CRS = "EPSG:32649"


def _grid() -> Grid:
    from floodviz.raster.grid import header_to_grid

    return header_to_grid(HEADER, CRS)


def _write_product_tif(path: Path, arr: np.ndarray, nodata: float = -9999.0) -> None:
    transform = Affine(100.0, 0, HEADER["xllcorner"], 0, -100.0,
                       HEADER["yllcorner"] + HEADER["nrows"] * 100.0)
    with rasterio.open(
        path, "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1],
        count=1, dtype="float32", crs=CRS, transform=transform,
        compress="deflate", nodata=nodata,
    ) as ds:
        data = np.where(np.isnan(arr), nodata, arr).astype("float32")
        ds.write(data, 1)


def _write_uint8_class_tif(path: Path, arr: np.ndarray, nodata: int = 255) -> None:
    """uint8 类别码 GeoTIFF（patch_class 源数据口径：0 未归因 / 1..8 类 / 255 nodata）。"""
    transform = Affine(100.0, 0, HEADER["xllcorner"], 0, -100.0,
                       HEADER["yllcorner"] + HEADER["nrows"] * 100.0)
    with rasterio.open(
        path, "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1],
        count=1, dtype="uint8", crs=CRS, transform=transform,
        compress="deflate", nodata=nodata,
    ) as ds:
        ds.write(arr.astype("uint8"), 1)


def _product(tmp_path: Path, **overrides) -> ProductLayerConfig:
    tif = tmp_path / "first_wet_h.tif"
    arr = np.full((6, 6), np.nan, dtype="float32")
    arr[1:4, 1:4] = 10.0  # 10 h 首湿
    _write_product_tif(tif, arr)
    kwargs = dict(
        product_id="product_fat_firstwet_test",
        label="研究性产品：首湿时刻 FAT（测试）",
        kind="fat_first_wet_h",
        source_tif=tif,
        save_interval_s=21600.0,
        audit="data/catalog/fat_products_audit.json",
        adr="ADR-0015 §2",
        notes="测试产品图层",
    )
    kwargs.update(overrides)
    return ProductLayerConfig(**kwargs)


def test_style_for_kinds_and_reject():
    for kind in ("fat_first_wet_h", "fat_exceed_h", "fat_duration_h",
                 "ensemble_probability", "class_map", "patch_class"):
        style = style_for(kind, threshold_m=0.3)
        assert style.depth_classes and style.value_semantics
        assert style.not_depth_note
    from floodviz.raster.paint import PaintError

    with pytest.raises(PaintError):
        style_for("no_such_kind")


def test_style_for_patch_class_discrete_palette():
    """patch_class：8 类离散色带，类名 P01–P08 与 ADR-0021/class_legend 一致。"""
    style = style_for("patch_class")
    assert len(style.depth_classes) == 8
    assert style.unit == "class"
    assert style.transparent_below == pytest.approx(0.5)
    expected = ("P01", "P02", "P03", "P04", "P05", "P06", "P07", "P08")
    for cls, code in zip(style.depth_classes, expected, strict=True):
        assert cls.label.startswith(code)
        assert cls.color.startswith("#") and len(cls.color) == 7
        assert len(cls.rgb) == 3
    # 类别码互斥无缝：0.5–1.5…7.5–8.5
    assert style.depth_classes[0].min_m == pytest.approx(0.5)
    assert style.depth_classes[-1].max_m == pytest.approx(8.5)
    assert "不是水深" in style.not_depth_note
    colors = {c.color for c in style.depth_classes}
    assert len(colors) == 8  # 每类独立颜色


def test_read_product_array_uint8_class_codes(tmp_path):
    """uint8 类别码源：码值 1..8 与 0 保真；255 nodata → NaN（透明）。"""
    grid = _grid()
    tif = tmp_path / "patch_class.tif"
    arr = np.zeros((6, 6), dtype="uint8")
    arr[0, 0] = 255  # nodata
    arr[1, 1] = 2
    arr[2, 2] = 8
    arr[3, 3] = 0  # 未归因（合法码，靠 transparent_below 透明）
    _write_uint8_class_tif(tif, arr)
    out = read_product_array(tif, grid)
    assert out.shape == (6, 6)
    assert np.isnan(out[0, 0])  # 255 → NaN → 透明
    assert out[1, 1] == pytest.approx(2.0)  # 类别码保真
    assert out[2, 2] == pytest.approx(8.0)
    assert out[3, 3] == pytest.approx(0.0)  # 0 保真，渲染时透明


def test_product_stats_doc_patch_class_breakdown(tmp_path):
    """patch_class 统计：class_breakdown 逐类计数/面积，code 0 不进分级。"""
    grid = _grid()
    tif = tmp_path / "patch_class.tif"
    arr = np.zeros((6, 6), dtype="uint8")
    arr[0:2, :] = 2  # 12 格 P02
    arr[2:4, 0:3] = 1  # 6 格 P01
    arr[4:6, 3:6] = 8  # 6 格 P08
    arr[0, 0] = 255  # nodata → NaN
    _write_uint8_class_tif(tif, arr)
    arr = read_product_array(tif, grid)  # 与管线一致：255 → NaN
    style = style_for("patch_class")
    doc = product_stats_doc(
        case_id="jiulong", product_id="product_jiulong_patch_class", arr=arr,
        cell_area_m2=grid.cell_area_m2, thresholds=[0.05, 0.3, 0.5, 1.0], style=style,
    )
    breakdown = {c["label"].split()[0]: c for c in doc["product"]["class_breakdown"]}
    assert breakdown["P01"]["cells"] == 6
    assert breakdown["P02"]["cells"] == 11  # 12 格中 1 格被 arr[0,0]=255 覆盖
    assert breakdown["P08"]["cells"] == 6
    assert breakdown["P05"]["cells"] == 0
    assert breakdown["P01"]["area_km2"] == pytest.approx(6 * grid.cell_area_m2 / 1e6)
    assert sum(c["cells"] for c in breakdown.values()) == 23
    vs = doc["product"]["value_summary"]
    assert vs["valid_cells"] == 35  # 36 - 1 nodata
    assert vs["rendered_cells"] == 23  # 类别码 1..8；0（12 格）不进分级
    assert "不是水深" in doc["product"]["not_depth_note"]


def test_read_product_array_identity_and_nodata(tmp_path):
    grid = _grid()
    tif = tmp_path / "p.tif"
    arr = np.full((6, 6), 5.0, dtype="float32")
    arr[0, 0] = -9999.0
    _write_product_tif(tif, arr)
    out = read_product_array(tif, grid)
    assert out.shape == (6, 6)
    assert np.isnan(out[0, 0])  # nodata → NaN
    assert out[3, 3] == pytest.approx(5.0)


def test_product_stats_doc_schema_self_consistent(tmp_path):
    grid = _grid()
    tif = tmp_path / "p.tif"
    arr = np.zeros((6, 6), dtype="float32")
    arr[1:4, 1:4] = 10.0
    _write_product_tif(tif, arr)
    style = style_for("fat_first_wet_h")
    thresholds = [0.05, 0.3, 0.5, 1.0]
    doc = product_stats_doc(
        case_id="guigang", product_id="p1", arr=arr,
        cell_area_m2=grid.cell_area_m2, thresholds=thresholds, style=style,
    )
    for key in ("schema_version", "case_id", "run_id", "unit_area", "thresholds_m",
                "grid_cell_area_m2", "frames", "max_envelope", "product"):
        assert key in doc
    assert doc["unit_area"] == "km2"
    assert doc["thresholds_m"] == thresholds
    frame = doc["frames"][0]
    for tkey, area in frame["areas_km2"].items():
        cells = frame["wetted_cells"][tkey]
        assert abs(cells * grid.cell_area_m2 / 1e6 - area) <= 1e-6
    assert doc["max_envelope"]["peak_frame_id"] in {f["frame_id"] for f in doc["frames"]}
    # 诚实语义：product 子文档必须声明非水深
    assert "不是水深" in doc["product"]["not_depth_note"]
    assert doc["product"]["research_product"] is True
    assert doc["product"]["value_summary"]["valid_cells"] == 36


def _mini_config(tmp_path: Path, product: ProductLayerConfig) -> tuple[Config, CaseConfig]:
    run = RunConfig(
        run_id="scen_anchor", label="锚点", role="default",
        source_dir=tmp_path / "anchor_run",
        save_interval_s=21600.0, simulation_duration_s=21600.0,
        time_origin=None, origin_status="relative_only", origin_note="",
    )
    case = CaseConfig(
        case_id="guigang", name="贵港", event_id="2024-09-yagi", event_name="e",
        scenario_role="", subtitle="", model_bbox=(109.0, 22.9, 110.0, 23.4),
        crs=CRS, default_run_id="scen_anchor", runs={"scen_anchor": run},
        origin_note="", sites=[], layers={},
        quality={"validation_status": "情景级"},
        sources={}, product_layers=(product,),
    )
    config = Config(
        path=tmp_path / "default.yaml", root=tmp_path, parent_root=tmp_path.parent,
        project={}, thresholds_m=(0.05, 0.3, 0.5, 1.0), default_threshold_m=0.05,
        depth_classes=({},), basemaps=({},), layer_order=(), presets=(),
        legends={}, cases={"guigang": case}, default_case="guigang",
        bundle_root=tmp_path / "web_bundle", image_max_pixels=4096, block_size=256,
        license_notice="",
    )
    return config, case


def test_process_product_layer_end_to_end(tmp_path):
    """单帧伪 run 端到端：timeline/statistics/images/numeric 全落盘并过检查组。"""
    config, case = _mini_config(tmp_path, _product(tmp_path))
    grid = _grid()
    tr = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
    split = paint_mod.choose_split(grid, tr.transform, tol_px=paint_mod.DEFAULT_TOL_PX)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    entry, per_run = _process_product_layer(
        config, case, case.product_layers[0], grid, tr,
        split["split_rows"], split["split_cols"], bundle, skip_tiles=False,
    )
    assert entry["role"] == "product" and entry["frame_count"] == 1
    assert entry["product"]["source_sha256"]
    assert entry["quality"]["research_product"] is True
    for rel in (entry["timeline"], entry["statistics"], entry["numeric_manifest"]):
        assert (bundle / rel).is_file()
    assert (bundle / "frames" / "guigang" / entry["run_id"] / "images.json").is_file()

    # validate 检查组（无 config → 跳过源比对；结构契约仍强制）
    catalog = {
        "display": {"thresholds_m": [0.05, 0.3, 0.5, 1.0]},
        "cases": [{"case_id": "guigang", "runs": [entry]}],
    }
    tl_group, timelines = _check_timeline(bundle, catalog, None)
    assert tl_group.passed, tl_group.failures
    st_group, _docs = _check_statistics(bundle, catalog, timelines)
    assert st_group.passed, st_group.failures
    im_group, _docs2 = _check_images(bundle, catalog, timelines)
    assert im_group.passed, im_group.failures
    assert per_run["frame_count"] == 1


def test_process_product_layer_missing_tif_fails_closed(tmp_path):
    config, case = _mini_config(tmp_path, _product(tmp_path))
    broken = ProductLayerConfig(
        product_id="p_missing", label="x", kind="fat_first_wet_h",
        source_tif=tmp_path / "cfg" / "nope.tif", save_interval_s=3600.0,
    )
    grid = _grid()
    tr = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
    from floodviz.pipeline import PipelineError

    with pytest.raises(PipelineError, match="不存在"):
        _process_product_layer(
            config, case, broken, grid, tr, 1, 1, tmp_path / "b2", skip_tiles=False)


def test_process_product_layer_class_map(tmp_path):
    """归因类别码图层：0 透明、码值着色、统计含 class_breakdown。"""
    tif = tmp_path / "origin_class.tif"
    arr = np.zeros((6, 6), dtype="float32")
    arr[0:2, :] = 2.0
    arr[2:4, 0:3] = 1.0
    arr[4:6, 3:6] = 4.0
    _write_product_tif(tif, arr, nodata=255.0)
    product = ProductLayerConfig(
        product_id="product_genesis_test", label="归因", kind="class_map",
        source_tif=tif, save_interval_s=21600.0,
        mass_gate_run_path=None, audit="data/catalog/guigang_genesis_products.json",
    )
    config, case = _mini_config(tmp_path, product)
    grid = _grid()
    tr = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
    split = paint_mod.choose_split(grid, tr.transform, tol_px=paint_mod.DEFAULT_TOL_PX)
    bundle = tmp_path / "bundle_cm"
    bundle.mkdir()
    entry, _per = _process_product_layer(
        config, case, product, grid, tr,
        split["split_rows"], split["split_cols"], bundle, skip_tiles=False,
    )
    stats_doc = __import__("json").loads(
        (bundle / entry["statistics"]).read_text(encoding="utf-8"))
    kinds = {c["label"].split()[0] for c in stats_doc["product"]["class_breakdown"]}
    assert {"1", "2", "4"} <= kinds
    # 码 0 是合法值（非 nodata）：36 格全部有效，透明由色带阈值（0.5）处理
    assert stats_doc["product"]["value_summary"]["valid_cells"] == 36
    rendered = sum(c["cells"] for c in stats_doc["product"]["class_breakdown"])
    assert rendered == 24  # code 1/2/4 像元；code 0 不进分级


def test_process_product_layer_patch_class(tmp_path):
    """久隆成因分类图层：uint8 源端到端 → 单帧伪 run + 8 类离散图例/逐类计数。"""
    tif = tmp_path / "patch_class_map.tif"
    arr = np.zeros((6, 6), dtype="uint8")  # 0 = 未归因（合法码）
    arr[0:3, :] = 4  # P04：18 格
    arr[3:6, 0:2] = 2  # P02：6 格
    arr[3:6, 5] = 1  # P01：3 格
    arr[3, 3] = 255  # nodata：1 格
    _write_uint8_class_tif(tif, arr)
    product = ProductLayerConfig(
        product_id="product_jiulong_patch_class", label="成因分类", kind="patch_class",
        source_tif=tif, save_interval_s=21600.0,
        audit="data/catalog/jiulong_patch_attribution_layer.json", adr="ADR-0021 §3",
        notes="仅供成因查询，不是水深",
    )
    config, case = _mini_config(tmp_path, product)
    grid = _grid()
    tr = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
    split = paint_mod.choose_split(grid, tr.transform, tol_px=paint_mod.DEFAULT_TOL_PX)
    bundle = tmp_path / "bundle_pc"
    bundle.mkdir()
    entry, _per = _process_product_layer(
        config, case, product, grid, tr,
        split["split_rows"], split["split_cols"], bundle, skip_tiles=False,
    )
    assert entry["role"] == "product" and entry["frame_count"] == 1
    assert entry["product"]["kind"] == "patch_class"
    assert entry["product"]["adr"] == "ADR-0021 §3"
    stats_doc = __import__("json").loads(
        (bundle / entry["statistics"]).read_text(encoding="utf-8"))
    counts = {c["label"].split()[0]: c["cells"]
              for c in stats_doc["product"]["class_breakdown"]}
    assert counts == {
        "P01": 3, "P02": 6, "P03": 0, "P04": 18, "P05": 0,
        "P06": 0, "P07": 0, "P08": 0,
    }
    # nodata（255）→ NaN：有效像元 = 36 - 1；code 0（8 格）有效但不进分级
    assert stats_doc["product"]["value_summary"]["valid_cells"] == 35
    assert stats_doc["product"]["value_summary"]["rendered_cells"] == 27
    # images.json 图例：8 个离散类别条目（类名 + 独立颜色），transparent_below 0.5
    images_doc = __import__("json").loads(
        (bundle / "frames" / "guigang" / entry["run_id"] / "images.json").read_text(encoding="utf-8"))
    classes = images_doc["color_scale"]["classes"]
    assert len(classes) == 8
    assert [c["label"].split()[0] for c in classes] == [f"P{i:02d}" for i in range(1, 9)]
    assert len({c["color"] for c in classes}) == 8
    assert images_doc["color_scale"]["transparent_below_m"] == pytest.approx(0.5)
    assert images_doc["product"]["kind"] == "patch_class"
