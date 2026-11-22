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
                 "ensemble_probability", "class_map"):
        style = style_for(kind, threshold_m=0.3)
        assert style.depth_classes and style.value_semantics
        assert style.not_depth_note
    from floodviz.raster.paint import PaintError

    with pytest.raises(PaintError):
        style_for("no_such_kind")


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
