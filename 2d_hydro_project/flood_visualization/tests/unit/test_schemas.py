"""契约示例对 JSON Schema 的校验（离线自检，不需要真实发布包）。

与 scripts/check_schemas.py 同一对清单，保证 CI 与人工自检口径一致。
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from conftest import PROJECT_ROOT

PAIRS = (
    ("docs/examples/catalog.example.json", "schemas/catalog.schema.json"),
    ("docs/examples/timeline.example.json", "schemas/timeline.schema.json"),
    ("docs/examples/images.example.json", "schemas/images.schema.json"),
    ("docs/examples/numeric_manifest.example.json", "schemas/numeric_manifest.schema.json"),
    ("docs/examples/statistics.example.json", "schemas/statistics.schema.json"),
)


def _load(rel: str) -> Any:
    path = PROJECT_ROOT / rel
    assert path.is_file(), f"缺少契约文件：{rel}"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(("doc_rel", "schema_rel"), PAIRS)
def test_example_matches_schema(doc_rel: str, schema_rel: str) -> None:
    jsonschema.validate(_load(doc_rel), _load(schema_rel))


@pytest.mark.parametrize(("_doc_rel", "schema_rel"), PAIRS)
def test_schema_is_valid_draft_2020_12(_doc_rel: str, schema_rel: str) -> None:
    jsonschema.Draft202012Validator.check_schema(_load(schema_rel))


def test_all_schema_files_covered() -> None:
    """schemas/ 下每个 .schema.json 都必须有对应示例，避免契约文档漂移。"""
    shipped = {path.name for path in (PROJECT_ROOT / "schemas").glob("*.schema.json")}
    covered = {Path(schema_rel).name for _, schema_rel in PAIRS}
    assert shipped == covered


# ---------------------------------------------------------------- 语义不变量


def test_timeline_example_is_monotonic() -> None:
    doc = _load("docs/examples/timeline.example.json")
    frames = doc["frames"]
    assert doc["frame_count"] == len(frames)
    assert doc["temporal_interpolation"] == "none"
    seqs = [f["source_sequence"] for f in frames]
    times = [f["sim_seconds"] for f in frames]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert all(b > a for a, b in itertools.pairwise(times))
    # origin 未确认则 timestamp 必须全为 null；确认则必须非空
    if doc["origin_status"].startswith("relative_only"):
        assert doc["time_origin"] is None
        assert all(f["timestamp"] is None for f in frames)
    else:
        assert all(isinstance(f["timestamp"], str) and "+08:00" in f["timestamp"] for f in frames)


def test_images_example_parts_are_sound() -> None:
    doc = _load("docs/examples/images.example.json")
    split = doc["split"]
    all_ids: list[str] = []
    for frame in doc["frames"] + [doc["max"]]:
        parts = frame["parts"]
        assert len(parts) == split["rows"] * split["cols"]
        for part in parts:
            assert len(part["coordinates"]) == 4
            assert part["width"] >= 1 and part["height"] >= 1
            assert part["path"].startswith("frames/")
            assert part["path"].endswith(".png")
            all_ids.append(part["id"])
    assert len(all_ids) == len(set(all_ids)), "parts[].id 必须全文件唯一"
    # 每帧 parts 面积并集 = 整幅：按行/列窗口拼回来
    grid = doc["grid"]
    covered = sum(part["width"] * part["height"] for part in doc["frames"][0]["parts"])
    assert covered == grid["rows"] * grid["cols"]


def test_statistics_example_uses_string_threshold_keys() -> None:
    doc = _load("docs/examples/statistics.example.json")
    thresholds = [str(float(t)) for t in doc["thresholds_m"]]
    for frame in doc["frames"]:
        assert set(frame["areas_km2"]) == set(thresholds)
        assert set(frame["wetted_cells"]) == set(thresholds)
        assert all(value >= 0 for value in frame["areas_km2"].values())
        assert all(isinstance(v, int) and v >= 0 for v in frame["wetted_cells"].values())


def test_numeric_manifest_example_contract() -> None:
    doc = _load("docs/examples/numeric_manifest.example.json")
    assert doc["dtype"] == "<f4"
    assert doc["byte_order"] == "little"
    assert doc["nodata"] == "nan"
    assert doc["block_size"] == 256
    grid = doc["grid"]
    assert doc["blocks"]["rows"] == -(-grid["rows"] // doc["block_size"])
    assert doc["blocks"]["cols"] == -(-grid["cols"] // doc["block_size"])
    for frame in doc["frames"]:
        assert frame["path_template"].startswith("numeric/")


def test_catalog_example_status_and_paths() -> None:
    doc = _load("docs/examples/catalog.example.json")
    allowed = {"remote_verified", "local_unchecked", "ready", "partial", "missing", "failed"}
    for case in doc["cases"]:
        assert case["default_run_id"] in {r["run_id"] for r in case["runs"]}
        assert case["layers"]["flood"]["status"] == "ready"
        for layer_id, layer in case["layers"].items():
            assert layer["status"] in allowed, f"{case['case_id']}.{layer_id}"
            for key in ("image", "images", "footprint", "geojson"):
                value = layer.get(key)
                if value is not None:
                    assert not value.startswith(("/", "http://", "https://", "\\\\")), value
                    assert ":" not in value, value
        for run in case["runs"]:
            assert run["quality"]["mass_gate"] in {"PASS", "FAIL-EXPLAINED", "FAIL", "unrecorded"}
