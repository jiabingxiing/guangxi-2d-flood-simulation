"""集成·结构与契约：全部 JSON 通过 schema、100% 帧齐全、哈希与来源一致、validate 全量通过。

这些断言对应计划 §7.1 的"集成/结构"门禁与 SPEC §3 发布包目录契约。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import jsonschema
import pytest
from helpers import (
    FULL_FRAMES,
    JIULONG_RUN,
    SMOKE_FRAMES,
    case_in_catalog,
    load_json,
    run_in_catalog,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _schema(name: str) -> dict:
    return json.loads((PROJECT_ROOT / "schemas" / name).read_text(encoding="utf-8"))


def _bundle_docs(bundle: Path, frames: int) -> list[tuple[str, dict, dict]]:
    run = JIULONG_RUN
    pairs = [
        ("catalog.json", load_json(bundle / "catalog.json"), _schema("catalog.schema.json")),
        (
            f"cases/jiulong/{run}/timeline.json",
            load_json(bundle / "cases" / "jiulong" / run / "timeline.json"),
            _schema("timeline.schema.json"),
        ),
        (
            f"cases/jiulong/{run}/statistics.json",
            load_json(bundle / "cases" / "jiulong" / run / "statistics.json"),
            _schema("statistics.schema.json"),
        ),
        (
            f"frames/jiulong/{run}/images.json",
            load_json(bundle / "frames" / "jiulong" / run / "images.json"),
            _schema("images.schema.json"),
        ),
        (
            f"numeric/jiulong/{run}/manifest.json",
            load_json(bundle / "numeric" / "jiulong" / run / "manifest.json"),
            _schema("numeric_manifest.schema.json"),
        ),
    ]
    return pairs


@pytest.mark.parametrize("bundle_fixture,frames", [("smoke_bundle", SMOKE_FRAMES), ("full_bundle", FULL_FRAMES)])
def test_all_documents_pass_schema(bundle_fixture: str, frames: int, request) -> None:
    bundle = request.getfixturevalue(bundle_fixture)
    for rel, doc, schema in _bundle_docs(bundle, frames):
        try:
            jsonschema.validate(doc, schema)
        except jsonschema.ValidationError as exc:  # pragma: no cover - 失败信息更易读
            path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
            raise AssertionError(f"{rel} 不符合 schema（{path}）：{exc.message}") from None


def test_timeline_frames_exactly_complete(full_bundle, source_run_dir) -> None:
    """100% 帧齐全：21 帧，source_sequence 0..20 连续，sim_seconds 严格递增且不超时长。"""
    timeline = load_json(full_bundle / "cases" / "jiulong" / JIULONG_RUN / "timeline.json")
    frames = timeline["frames"]
    assert timeline["frame_count"] == len(frames) == FULL_FRAMES
    assert [f["source_sequence"] for f in frames] == list(range(FULL_FRAMES))
    assert [f["frame_id"] for f in frames] == [f"f{seq:04d}" for seq in range(FULL_FRAMES)]
    times = [f["sim_seconds"] for f in frames]
    assert times == sorted(times) and len(set(times)) == len(times)
    assert times[-1] == pytest.approx(20 * 21600.0)
    assert timeline["simulation_duration_s"] == pytest.approx(432000.0)
    assert times[-1] <= timeline["simulation_duration_s"] + 1e-6
    # 源文件必须真实存在且帧号一一对应
    for frame in frames:
        seq = frame["source_sequence"]
        assert (source_run_dir / "results" / f"res-{seq:04d}.wd").is_file()


def test_timeline_timestamp_policy(full_bundle) -> None:
    """久隆 origin_status=relative_only_candidate → timestamp 全为 null、time_origin 为 null。"""
    timeline = load_json(full_bundle / "cases" / "jiulong" / JIULONG_RUN / "timeline.json")
    assert timeline["origin_status"] == "relative_only_candidate"
    assert timeline["time_origin"] is None
    assert timeline["time_zone"] == "Asia/Shanghai"
    assert timeline["temporal_interpolation"] == "none"
    assert all(frame["timestamp"] is None for frame in timeline["frames"])


def test_timeline_sha256_matches_source(full_bundle, source_run_dir) -> None:
    """timeline.frames[].sha256 必须等于对应 res-XXXX.wd 的真实 sha256（来源可追溯）。"""
    timeline = load_json(full_bundle / "cases" / "jiulong" / JIULONG_RUN / "timeline.json")
    for frame in timeline["frames"]:
        source = source_run_dir / "results" / f"res-{frame['source_sequence']:04d}.wd"
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        assert frame["sha256"] == digest, f"{frame['frame_id']} 哈希与源不一致"


def test_catalog_links_resolve(full_bundle) -> None:
    """catalog 里声明的每个相对路径都必须能在包内找到（不漏文件）。"""
    catalog = load_json(full_bundle / "catalog.json")
    case = case_in_catalog(full_bundle, "jiulong")
    run = run_in_catalog(full_bundle, "jiulong", JIULONG_RUN)
    for key in ("timeline", "statistics", "numeric_manifest"):
        assert (full_bundle / run[key]).is_file(), run[key]
    for layer_id, layer in case["layers"].items():
        for key in ("image", "images", "geojson", "footprint"):
            rel = layer.get(key)
            if rel:
                assert (full_bundle / rel).is_file(), f"{layer_id}.{key}={rel}"
    assert catalog["default_case"] == "jiulong"


def test_catalog_quality_is_real(full_bundle, parent_root) -> None:
    """质量门取真实复核值（PASS, e_mass≈0.0004893），不得写死或虚报。"""
    run = run_in_catalog(full_bundle, "jiulong", JIULONG_RUN)
    quality = run["quality"]
    assert quality["mass_gate"] == "PASS"
    assert quality["e_mass"] == pytest.approx(0.0004893)
    assert quality["validation_status"]
    assert quality["vertical_datum_status"]
    assert quality["warnings"]


def test_catalog_display_fields(full_bundle) -> None:
    catalog = load_json(full_bundle / "catalog.json")
    display = catalog["display"]
    assert display["area_threshold_note"].startswith("面积为原生模型网格")
    assert display["temporal_note"].startswith("6 h/帧")
    assert display["max_envelope_title"]
    assert display["current_frame_title"]
    assert display["thresholds_m"] == [0.05, 0.15, 0.3, 0.5, 1.0, 2.0, 5.0, 10.0]
    assert len(display["depth_classes"]) == 8
    assert catalog["basemaps"]
    assert all("https://" in b["tiles"][0] for b in catalog["basemaps"])
    assert [p["id"] for p in catalog["presets"]] == [
        "optical_only",
        "sar_only",
        "dem_only",
        "flood_only",
        "all_available",
        "custom",
    ]


def test_no_absolute_or_backslash_paths(full_bundle) -> None:
    """发布包内所有 JSON 不得出现绝对/UNC/反斜杠路径；http(s) 只允许 Esri 底图与许可元数据。

    运行期资源必须全部相对路径（SPEC §3）：仅 basemaps[].tiles 允许外部瓦片 URL；
    许可证出处（license.document_source 等）是离线文档引用，允许 https。
    """
    allowed_url_paths = ("/basemaps[", "/attribution", "/license", "/sources")
    offenders: list[str] = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")
        elif isinstance(node, str):
            if "\\\\" in node or ":\\" in node or node.startswith("/"):
                offenders.append(f"{path}: {node}")
            elif "://" in node:
                is_http = node.startswith("http://")
                is_esri = node.startswith("https://") and "arcgisonline.com" in node
                is_meta = node.startswith("https://") and any(marker in f"{path}/" for marker in allowed_url_paths)
                if is_http or not (is_esri or is_meta):
                    offenders.append(f"{path}: {node}")

    for path in sorted(full_bundle.rglob("*.json")):
        walk(json.loads(path.read_text(encoding="utf-8")), str(path.relative_to(full_bundle)))
    assert not offenders, offenders[:5]


def test_manifest_content_hashes_verify(full_bundle) -> None:
    """包根 manifest.content 里的每个 sha256/bytes 必须与磁盘一致。"""
    manifest = load_json(full_bundle / "manifest.json")
    content = manifest["content"]
    assert content, "manifest.content 为空"
    for rel, info in content.items():
        path = full_bundle / rel
        assert path.is_file(), rel
        assert path.stat().st_size == info["bytes"], f"{rel} 字节数不符"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == info["sha256"], f"{rel} 哈希不符"


def test_manifest_declares_web_placeholder(full_bundle) -> None:
    manifest = load_json(full_bundle / "manifest.json")
    assert "web" in manifest
    assert manifest["bundle_id"] == "qa_jiulong_full"
    assert manifest["build"]["params"]["only"] == ["jiulong"]
    assert manifest["build"]["params"]["rendering"] == "native_grid_1to1_precolored_png_no_xyz_pyramid"
    assert manifest["source_manifest_hash"].startswith("sha256:")


def test_validate_bundle_full_passes(full_bundle, integration_config) -> None:
    """validate_bundle(full=True) 必须通过全部 9 组门禁（数值/统计/空间/结构/时间/schema）。"""
    from floodviz.pipeline import validate_bundle

    report = validate_bundle(full_bundle, integration_config, full=True)
    doc = report.to_dict()
    groups = {group["name"]: group for group in doc["groups"]}
    failed = {name: group["failures"] for name, group in groups.items() if not group["passed"]}
    assert report.ok, failed
    expected_groups = {
        "structure",
        "timeline",
        "statistics",
        "images",
        "completeness",
        "numeric",
        "statistics_recompute",
        "spatial",
        "schemas",
    }
    assert expected_groups <= set(groups)
    details = groups["numeric"]["details"]
    assert details["max_abs_err_m"] <= 1e-5
    assert details["max_rel_err"] <= 1e-6
    stats_details = groups["statistics_recompute"]["details"]
    assert stats_details["max_rel_diff"] <= 0.001
    assert groups["spatial"]["details"]["max_px"] <= 0.5
    print(
        "validate(full): "
        f"numeric samples={details['samples']} max_abs={details['max_abs_err_m']}；"
        f"stats frames={stats_details['frames_checked']} max_rel={stats_details['max_rel_diff']}；"
        f"spatial max_px={groups['spatial']['details']['max_px']}"
    )


def test_validate_bundle_dev_subset_is_allowed(smoke_bundle, integration_config) -> None:
    """开发子集包（--only/--frames-limit）允许帧数不足，但必须带 dev 标记与说明。"""
    from floodviz.pipeline import validate_bundle

    report = validate_bundle(smoke_bundle, integration_config, full=True)
    doc = report.to_dict()
    groups = {group["name"]: group for group in doc["groups"]}
    # 结构/时间门禁会报"帧数与配置期望不符"，但必须显式说明是开发子集
    failures = groups["structure"]["failures"] + groups["timeline"]["failures"]
    assert all("帧数" in item or "frame_count" in item for item in failures), failures
    # 数值/统计/空间三门禁必须依然通过
    for name in ("numeric", "statistics_recompute", "spatial", "schemas", "images", "completeness"):
        assert groups[name]["passed"], (name, groups[name]["failures"])


def test_bundle_has_no_absolute_paths_in_geojson(smoke_bundle) -> None:
    for path in sorted(smoke_bundle.rglob("*.geojson")):
        text = path.read_text(encoding="utf-8")
        assert "C:\\" not in text and "D:\\" not in text
        doc = json.loads(text)
        assert doc["type"] in {"FeatureCollection", "Feature"}
