"""M0 单元测试：hydro_refine 路径解析契约与包导入位置校验。"""
from __future__ import annotations

from pathlib import Path

import pytest

import hydro_refine
from hydro_refine.core.paths import (
    PROJ,
    PathResolutionError,
    external,
    rel_posix,
    resolve_legacy,
)


def test_package_imported_from_current_proj() -> None:
    pkg_file = Path(hydro_refine.__file__).resolve()
    assert pkg_file.is_relative_to(PROJ.resolve())
    assert (PROJ / "pyproject.toml").is_file()


def test_rel_posix_and_resolve_legacy_roundtrip() -> None:
    rel = rel_posix(PROJ / "configs" / "base.yaml")
    assert rel == "configs/base.yaml"
    resolved = resolve_legacy(rel)
    assert resolved.resolve() == (PROJ / "configs" / "base.yaml").resolve()


def test_external_osm_pbf_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_pbf = tmp_path / "china.osm.pbf"
    fake_pbf.write_bytes(b"pbf")
    monkeypatch.setenv("GX_OSM_PBF", str(fake_pbf))
    assert external("OSM_PBF") == fake_pbf


def test_resolve_legacy_missing_raises() -> None:
    with pytest.raises(PathResolutionError):
        resolve_legacy("data/catalog/__definitely_missing_xyz__.json")
