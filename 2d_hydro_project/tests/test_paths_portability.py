"""路径解析层契约测试（可移植化计划 §2.1 / SC-01 / SC-02）。

本测试守护三条规则：

1. **当前项目优先**。旧盘符上可能同时存在过期副本（实测 F: 曾有完整旧拷贝），
   解析必须落到当前项目，不得静默读旧副本。
2. **锚点重挂**。历史路径的盘符与中间目录名都可能变
   （`D:\\NWKYYSJZX\\2026年工作\\...` → `E:\\NWKYYSJZX\\2026\\...`），
   解析不得依赖具体盘符或目录名映射表。
3. **fail-closed**。不可解析时报错并给提示，不静默返回空路径。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJ = Path(__file__).resolve().parents[1]
if str(PROJ) not in sys.path:
    sys.path.insert(0, str(PROJ))

from scripts.common import paths  # noqa: E402

# 历史登记形式：盘符 D、中间目录 2026年工作（现为 E、2026）
LEGACY = (
    r"D:\NWKYYSJZX\2026年工作\1023 久隆 贵港 宁明 二维水动力模拟"
    r"\2d_hydro_project\pyproject.toml"
)
LEGACY_CHILD = (
    r"F:\NWKYYSJZX\2026年工作\1023 久隆 贵港 宁明 二维水动力模拟"
    r"\2d_hydro_project\flood_visualization\configs\default.yaml"
)


def test_proj_root_contains_pyproject() -> None:
    assert paths.PROJ == PROJ
    assert (paths.PROJ / "pyproject.toml").is_file()


def test_relative_path_resolves_under_proj() -> None:
    got = paths.resolve_legacy("data/catalog/input_manifest.json")
    assert got == paths.PROJ / "data" / "catalog" / "input_manifest.json"
    assert got.is_file()


def test_relative_missing_raises() -> None:
    with pytest.raises(paths.PathResolutionError):
        paths.resolve_legacy("data/catalog/__no_such_file__.json")


def test_legacy_absolute_reanchors_to_current_project() -> None:
    """旧 D 盘路径必须重挂到当前项目，而不是回到 D 盘。"""
    got = paths.resolve_legacy(LEGACY)
    assert got.is_file()
    assert got.resolve() == (paths.PROJ / "pyproject.toml").resolve()


def test_legacy_child_project_reanchors() -> None:
    got = paths.resolve_legacy(LEGACY_CHILD)
    assert got.is_file()
    expected = paths.PROJ / "flood_visualization" / "configs" / "default.yaml"
    assert got.resolve() == expected.resolve()


def test_current_project_wins_over_stale_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """核心回归：旧盘符路径若仍存在（陈旧副本），仍须解析到当前项目。

    不依赖真实旧盘是否插着：在 tmp_path 下伪造一份"旧副本"，
    用它的绝对路径充当"当年登记的路径"。
    """
    stale_root = tmp_path / "OLDDRIVE" / "2026年工作" / "1023 case" / "2d_hydro_project"
    stale_root.mkdir(parents=True)
    (stale_root / "pyproject.toml").write_text("# stale\n", encoding="utf-8")
    stale_file = stale_root / "pyproject.toml"
    assert stale_file.is_file()

    got = paths.resolve_legacy(str(stale_file))
    assert got.resolve() == (paths.PROJ / "pyproject.toml").resolve(), (
        "解析结果不是当前项目的同名文件：可能静默读到了旧副本"
    )
    assert got.read_text(encoding="utf-8") != "# stale\n"


def test_strict_detects_dual_copy(tmp_path: Path) -> None:
    """严格版在双副本并存时必须报错，不得静默择一。"""
    stale_root = tmp_path / "OLDDRIVE" / "2026年工作" / "1023 case" / "2d_hydro_project"
    stale_root.mkdir(parents=True)
    stale_file = stale_root / "pyproject.toml"
    stale_file.write_text("# stale\n", encoding="utf-8")

    with pytest.raises(paths.PathResolutionError):
        paths.resolve_legacy_strict(str(stale_file))


def test_resolve_or_none_is_tolerant() -> None:
    assert paths.resolve_or_none(None) is None
    assert paths.resolve_or_none("") is None
    assert paths.resolve_or_none("data/catalog/__no_such__.json") is None
    assert paths.resolve_or_none("data/catalog/input_manifest.json") is not None


def test_rel_posix_returns_project_relative() -> None:
    got = paths.rel_posix(paths.PROJ / "pyproject.toml")
    assert got == "pyproject.toml"
    assert "\\" not in got


def test_rel_posix_of_nested_file() -> None:
    got = paths.rel_posix(paths.PROJ / "configs" / "base.yaml")
    assert got == "configs/base.yaml"


def test_rel_posix_resolves_with_proj_join() -> None:
    """消费者约定：`p if p.is_absolute() else PROJ / p` 必须能还原到真实文件。"""
    rel = paths.rel_posix(paths.PROJ / "data" / "catalog" / "input_manifest.json")
    assert not Path(rel).is_absolute()
    assert (paths.PROJ / rel).is_file()


def test_rel_posix_refuses_outside_project(tmp_path: Path) -> None:
    outsider = tmp_path / "x.txt"
    outsider.write_text("x", encoding="utf-8")
    with pytest.raises(paths.PathResolutionError):
        paths.rel_posix(outsider)


def test_unresolvable_absolute_raises_with_hint() -> None:
    with pytest.raises(paths.PathResolutionError) as ei:
        paths.resolve_legacy(r"Z:\no\such\2d_hydro_project\data\x.json")
    msg = str(ei.value)
    assert "GX_DEM_SOURCE" in msg or "当前项目根" in msg


def test_external_returns_none_when_absent() -> None:
    """源 DEM 未到位时应返回 None（可选资源语义），由 require 版报错。"""
    got = paths.external("DEM_SOURCE")
    assert got is None or got.is_file()


def test_dem_source_resolves_when_linked() -> None:
    """源 DEM 就位后（目录联接或直接放置），dem_source() 必须可解析且可读。

    `data/raw/dem_source` 允许是 Junction（无需管理员权限的目录联接）；
    本测试只要求能解析到文件并读到字节数，不约束它是不是链接。
    """
    got = paths.external("DEM_SOURCE")
    if got is None:
        pytest.skip("源 DEM 未就位；缺失场景由 require 版覆盖")
    assert got.is_file()
    assert got.stat().st_size > 0


def test_dem_source_bytes_match_manifest() -> None:
    """源 DEM 字节数必须与 input_manifest 冻结值一致（防链接指向错文件）。"""
    import json

    manifest = paths.PROJ / "data" / "catalog" / "input_manifest.json"
    if not manifest.is_file():
        pytest.skip("input_manifest.json 不存在")
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    rec = (doc.get("files") or {}).get("dem_source_guangxi") or {}
    frozen = rec.get("bytes")
    if not frozen:
        pytest.skip("清单未冻结 dem_source 字节数")
    got = paths.external("DEM_SOURCE")
    if got is None:
        pytest.skip("源 DEM 未就位")
    assert got.stat().st_size == frozen, (
        f"源 DEM 字节数 {got.stat().st_size} 与清单冻结值 {frozen} 不一致："
        "链接可能指向了别的文件"
    )


def test_require_external_raises_when_absent() -> None:
    if paths.external("DEM_SOURCE") is not None:
        pytest.skip("源 DEM 已就位，缺失场景未成立")
    with pytest.raises(paths.PathResolutionError) as ei:
        paths.require_external("DEM_SOURCE")
    assert "GX_DEM_SOURCE" in str(ei.value)


def test_external_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "dem.tif"
    fake.write_bytes(b"x")
    monkeypatch.setenv("GX_DEM_SOURCE", str(fake))
    assert paths.external("DEM_SOURCE") == fake