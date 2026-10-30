"""生成 docs/examples/ 下的契约示例 JSON（由 config + 真实网格几何推导，不手抄）。

用途：给前端/管道实现者一份可对照的字段样例；同时作为 catalog/timeline 字段名的单点来源。
运行：
    uv run python scripts/make_contract_examples.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pyproj import Transformer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from floodviz.catalog.catalog import build_case_entry, geometry_for_grid
from floodviz.catalog.quality import load_mass_gate
from floodviz.catalog.timeline import build_timeline
from floodviz.config import load_config
from floodviz.quality.frames import FrameInfo
from floodviz.raster.grid import header_to_grid, read_ascii_header

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "examples"

FRAME_COUNTS = {
    "jiulong": 21,
    "ningming": 19,
    "guigang": 57,
}


def geometry(case, run):
    header = read_ascii_header(run.source_dir / "results" / "res-0000.wd")
    grid = header_to_grid(header, case.crs)
    epsg = int(case.crs.split(":")[1])
    transformer = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    return grid, geometry_for_grid(grid, crs_to_wgs84=transformer.transform)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    config = load_config(ROOT / "configs" / "default.yaml")
    case_entries = []
    example_timeline = None
    example_images = None
    example_numeric = None
    example_stats = None

    for case_id in config.case_ids:
        case = config.cases[case_id]
        run = case.run()
        grid, geom = geometry(case, run)
        count = FRAME_COUNTS[case_id]
        frames = [
            FrameInfo(
                seq=i,
                path=run.source_dir / "results" / f"res-{i:04d}.wd",
                sha256="0" * 64,
                header=read_ascii_header(run.source_dir / "results" / "res-0000.wd"),
                size_bytes=0,
            )
            for i in range(count)
        ]
        run_dir_posix = run.source_dir.as_posix().split("2d_hydro_project/", 1)[-1]
        run_rel = f"outputs/{run_dir_posix}" if not run_dir_posix.startswith("outputs/") else run_dir_posix
        mass = load_mass_gate(config.parent_root, run_rel)
        quality = dict(case.quality)
        quality["mass_gate"] = mass["mass_gate"]
        quality["e_mass"] = mass["e_mass"]
        if mass.get("cause"):
            quality["mass_gate_cause"] = mass["cause"]
        timeline = build_timeline(
            case,
            run,
            frames,
            images_prefix=f"frames/{case_id}/{run.run_id}",
            blocks_prefix=f"numeric/{case_id}/{run.run_id}",
            statistics_path=f"cases/{case_id}/{run.run_id}/statistics.json",
            numeric_manifest_path=f"numeric/{case_id}/{run.run_id}/manifest.json",
        )
        layer_docs = {}
        for layer_id, layer in case.layers.items():
            doc = {"status": layer.status, "label": layer.label, "kind": layer.kind}
            if layer.summary:
                doc["summary"] = layer.summary
            if layer.note:
                doc["note"] = layer.note
            if layer.asset_id:
                doc["asset_id"] = layer.asset_id
            if layer.acquired_at:
                doc["acquired_at"] = layer.acquired_at
            if layer_id == "flood":
                doc["images"] = f"frames/{case_id}/{run.run_id}/images.json"
            elif layer_id == "max":
                doc["image"] = f"frames/{case_id}/{run.run_id}/max.png"
                doc["geometry"] = geom
            elif layer_id == "dem":
                doc["image"] = f"frames/{case_id}/dem.png"
                doc["geometry"] = geom
                doc["legend"] = "terrain"
            elif layer_id in {"optical", "sar"}:
                doc["image"] = f"frames/{case_id}/{layer_id}.png"
                doc["geometry"] = geom
                doc["legend"] = "binary_water" if layer_id == "sar" else "rgb"
                doc["footprint"] = f"cases/{case_id}/{layer_id}_footprint.geojson"
            layer_docs[layer_id] = doc
        layer_docs.setdefault(
            "boundary",
            {
                "status": "ready",
                "label": "模型域边界",
                "kind": "vector",
                "geojson": f"cases/{case_id}/boundary.geojson",
            },
        )
        layer_docs.setdefault(
            "sites",
            {"status": "ready", "label": "站点/参考点", "kind": "vector", "geojson": f"cases/{case_id}/sites.geojson"},
        )

        entry = build_case_entry(
            case,
            grid_geometry=geom,
            run_entries=[
                {
                    "run_id": run.run_id,
                    "label": run.label,
                    "role": run.role,
                    "frame_count": count,
                    "save_interval_s": run.save_interval_s,
                    "simulation_duration_s": run.simulation_duration_s,
                    "timeline": f"cases/{case_id}/{run.run_id}/timeline.json",
                    "statistics": f"cases/{case_id}/{run.run_id}/statistics.json",
                    "numeric_manifest": f"numeric/{case_id}/{run.run_id}/manifest.json",
                    "optional": run.optional,
                    "notes": run.notes,
                    "quality": {
                        "mass_gate": quality["mass_gate"],
                        "e_mass": quality["e_mass"],
                        "validation_status": case.quality.get("validation_status", ""),
                        "vertical_datum_status": case.quality.get("vertical_datum_status", ""),
                        "warnings": list(case.quality.get("warnings") or []),
                        "mass_gate_cause": mass.get("cause"),
                    },
                }
            ],
            layer_docs=layer_docs,
            quality=quality,
        )
        case_entries.append(entry)

        if example_timeline is None:
            example_timeline = timeline
            example_images = {
                "schema_version": "1.0",
                "case_id": case_id,
                "run_id": run.run_id,
                "kind": "depth_frames",
                "grid": {
                    "crs": grid.crs,
                    "rows": grid.rows,
                    "cols": grid.cols,
                    "transform": list(grid.transform),
                    "cell_size_m": grid.cell_size_m,
                    "cell_area_m2": grid.cell_area_m2,
                },
                "color_scale": {
                    "transparent_below_m": config.default_threshold_m,
                    "classes": [dict(c) for c in config.depth_classes],
                },
                "split": {"rows": 1, "cols": 1},
                "frames": [
                    {
                        "frame_id": f"f{i:04d}",
                        "max_depth_m": 0.0,
                        "wet_pixels": 0,
                        "parts": [
                            {
                                "id": f"f{i:04d}_r0c0",
                                "row": 0,
                                "col": 0,
                                "path": f"frames/{case_id}/{run.run_id}/f{i:04d}_r0c0.png",
                                "width": grid.cols,
                                "height": grid.rows,
                                "coordinates": geom["coordinates"],
                                "max_depth_m": 0.0,
                                "wet_pixels": 0,
                            }
                        ],
                    }
                    for i in range(2)
                ],
                "max": {
                    "max_depth_m": 0.0,
                    "wet_pixels": 0,
                    "parts": [
                        {
                            "id": "max_r0c0",
                            "row": 0,
                            "col": 0,
                            "path": f"frames/{case_id}/{run.run_id}/max_r0c0.png",
                            "width": grid.cols,
                            "height": grid.rows,
                            "coordinates": geom["coordinates"],
                            "max_depth_m": 0.0,
                            "wet_pixels": 0,
                        }
                    ],
                },
            }
            example_numeric = {
                "schema_version": "1.0",
                "case_id": case_id,
                "run_id": run.run_id,
                "grid": {
                    "crs": grid.crs,
                    "rows": grid.rows,
                    "cols": grid.cols,
                    "transform": list(grid.transform),
                    "cell_size_m": grid.cell_size_m,
                    "cell_area_m2": grid.cell_area_m2,
                },
                "block_size": 256,
                "blocks": {"rows": (grid.rows + 255) // 256, "cols": (grid.cols + 255) // 256},
                "dtype": "<f4",
                "byte_order": "little",
                "nodata": "nan",
                "layout": (
                    "block rows [by*256, min(rows,(by+1)*256)) x cols [bx*256, min(cols,(bx+1)*256)); "
                    "C order; index=(r-r0)*w+(c-c0)"
                ),
                "mask_note": "mask=non-NaN；海域像元仅从统计剔除，点查仍返回真实值（模型已钳 0）",
                "mask_png": f"numeric/{case_id}/{run.run_id}/mask.png",
                "mask_encoding": "1-bit PNG, full grid 1:1, row 0 north, pixel value 255=valid",
                "frames": [
                    {
                        "frame_id": f"f{i:04d}",
                        "path_template": f"numeric/{case_id}/{run.run_id}/f{i:04d}/{{block_y}}/{{block_x}}.bin",
                    }
                    for i in range(2)
                ],
                "max": {"path_template": f"numeric/{case_id}/{run.run_id}/max/{{block_y}}/{{block_x}}.bin"},
            }
            example_stats = {
                "schema_version": "1.0",
                "case_id": case_id,
                "run_id": run.run_id,
                "unit_area": "km2",
                "thresholds_m": list(config.thresholds_m),
                "grid_cell_area_m2": grid.cell_area_m2,
                "valid_cell_count": 0,
                "sea_cell_count": 0,
                "frames": [
                    {
                        "frame_id": f"f{i:04d}",
                        "sim_seconds": i * run.save_interval_s,
                        "timestamp": timeline["frames"][i]["timestamp"],
                        "areas_km2": {f"{t}": 0.0 for t in config.thresholds_m},
                        "wetted_cells": {f"{t}": 0 for t in config.thresholds_m},
                    }
                    for i in range(2)
                ],
                "max_envelope": {
                    "areas_km2": {f"{t}": 0.0 for t in config.thresholds_m},
                    "wetted_cells": {f"{t}": 0 for t in config.thresholds_m},
                    "peak_frame_id": "f0000",
                    "peak_time_s": 0.0,
                },
            }

    catalog = {
        "schema_version": "1.0",
        "bundle_id": "v3-20261028",
        "created_utc": "2026-10-27T10:00:00+00:00",
        "source_manifest_hash": "sha256:" + "0" * 64,
        "license_notice": config.license_notice,
        "default_case": config.default_case,
        "display": {
            "project_title": config.project.get("title", ""),
            "subtitle": config.project.get("subtitle", ""),
            "default_threshold_m": config.default_threshold_m,
            "thresholds_m": list(config.thresholds_m),
            "depth_classes": [dict(c) for c in config.depth_classes],
            "area_threshold_note": config.project.get("area_threshold_note", ""),
            "temporal_note": "6 h/帧离散回放；播放速度不改变物理时间分辨率",
            "max_envelope_title": "全程最大水深包络（非某一时刻）",
            "current_frame_title": "当前帧模拟水深",
        },
        "basemaps": [dict(b) for b in config.basemaps],
        "layer_order": list(config.layer_order),
        "presets": [dict(p) for p in config.presets],
        "legends": dict(config.legends),
        "cases": case_entries,
        "attribution": {
            "online_basemaps": "在线底图署名随服务元数据动态加载；不得声称离线授权",
            "sentinel": "Contains modified Copernicus Sentinel data 2024/2026（裁剪与遮云处理）",
        },
        "environment": {"offline_capable": True, "requires_network_layers": [b["id"] for b in config.basemaps]},
    }

    files = {
        "catalog.example.json": catalog,
        "timeline.example.json": example_timeline,
        "images.example.json": example_images,
        "numeric_manifest.example.json": example_numeric,
        "statistics.example.json": example_stats,
    }
    for name, doc in files.items():
        (OUT / name).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[examples] {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
