"""Run manifest / no-overwrite / input manifest completeness。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ))
from scripts.common.run_manifest import REQUIRED_KEYS, prepare_run_dir, write_run_manifest  # noqa: E402


def test_prepare_run_dir_refuses_existing_results(tmp_path):
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "res.mass").write_text("Time Vol\n0 0\n")
    with pytest.raises(FileExistsError):
        prepare_run_dir(tmp_path)


def test_run_manifest_required_fields(tmp_path):
    rec = write_run_manifest(
        tmp_path, config_snapshot={"case": "unit-test"}, model_version="8.1.0",
        peak_rss_mb=1.0, mass_gate={"gate_1e-3": "PASS"}, warnings=[])
    assert (tmp_path / "run_manifest.json").exists()
    for key in REQUIRED_KEYS:
        assert key in rec
    assert rec["code_hashes"]["tree_sha256"]
    assert rec["model_version"] == "8.1.0"


def test_input_manifest_complete():
    d = json.loads((PROJ / "data/catalog/input_manifest.json").read_text(encoding="utf-8"))
    required = {
        "dem_guigang_clip", "dem_source_jiulong_clip", "dem_ningming_interim",
        "s1_guigang_0912_water", "s1_guigang_0905_water",
        "gf3_water_0806", "gf3_water_0824", "gf3_sar_0824",
        "config_base", "config_jiulong", "config_ningming", "config_guigang",
        "lisflood_exe",
    }
    missing = [k for k in required if k not in d["files"] or d["files"][k].get("missing")]
    assert not missing, f"input_manifest 缺项: {missing}"


def test_code_manifest_matches_tree():
    d = json.loads((PROJ / "data/catalog/code_manifest.json").read_text(encoding="utf-8"))
    assert d["tree_sha256"]
    assert d["n_files"] >= 40
    assert "scripts/stage5/guigang_scenario.py" in d["files"]
