"""发布包保留策略测试（scripts/prune_bundles.py）。

重点验证「不得把发布包删到只剩 1 个」这条硬约束：
仓库约定必须保留最近两个 bundle_id，否则回滚要先重建。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "scripts" / "prune_bundles.py"
REQUIRED = ("index.html", "manifest.json", "catalog.json")


def _make_bundle(root: Path, name: str, created: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    for fname in ("index.html", "manifest.json"):
        (d / fname).write_text("{}", encoding="utf-8")
    (d / "catalog.json").write_text(
        json.dumps({"bundle_id": name, "created_utc": created}), encoding="utf-8")
    return d


def _run(root: Path, *args: str) -> subprocess.CompletedProcess:
    """在临时根上跑脚本：用 --help 之外的路径注入，避免依赖真实 outputs。"""
    script = SCRIPT.read_text(encoding="utf-8")
    patched = script.replace(
        'ROOT = Path(__file__).resolve().parents[1]',
        f'ROOT = Path(r"{root}")')
    tmp_script = root / "_prune_under_test.py"
    tmp_script.write_text(patched, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(tmp_script), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


@pytest.fixture
def bundle_root(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    bundles = root / "outputs" / "web_bundle"
    bundles.mkdir(parents=True)
    _make_bundle(bundles, "v1", "2026-10-27T09:00:00+00:00")
    _make_bundle(bundles, "v2", "2026-10-27T10:00:00+00:00")
    _make_bundle(bundles, "v3", "2026-10-28T07:00:00+00:00")
    return root


def test_keep_one_is_rejected(bundle_root: Path) -> None:
    """--keep 1 必须被拒绝：删到只剩 1 个就没有回滚目标了。"""
    result = _run(bundle_root, "--keep", "1")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "低于硬下限" in result.stderr
    # 一个都不能少
    bundles = bundle_root / "outputs" / "web_bundle"
    assert sorted(d.name for d in bundles.iterdir()) == ["v1", "v2", "v3"]


def test_keep_zero_is_rejected(bundle_root: Path) -> None:
    result = _run(bundle_root, "--keep", "0")
    assert result.returncode == 2, result.stdout + result.stderr


def test_dry_run_deletes_nothing(bundle_root: Path) -> None:
    """默认（不加 --apply）只打印计划。"""
    result = _run(bundle_root, "--keep", "2")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "dry-run" in result.stdout
    bundles = bundle_root / "outputs" / "web_bundle"
    assert len(list(bundles.iterdir())) == 3


def test_apply_keeps_newest_two(bundle_root: Path) -> None:
    """--keep 2 --apply 删最旧的，留下最新两个。"""
    result = _run(bundle_root, "--keep", "2", "--apply")
    assert result.returncode == 0, result.stdout + result.stderr
    bundles = bundle_root / "outputs" / "web_bundle"
    assert sorted(d.name for d in bundles.iterdir()) == ["v2", "v3"]
    assert not (bundles / "v1").exists()


def test_cannot_prune_below_floor_even_if_allowed(bundle_root: Path) -> None:
    """先删到只剩 2 个，再要求 keep=2 时不得删除（否则只剩 1 个）。"""
    bundles = bundle_root / "outputs" / "web_bundle"
    shutil.rmtree(bundles / "v1")
    result = _run(bundle_root, "--keep", "2", "--apply")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "无需删除" in result.stdout
    assert sorted(d.name for d in bundles.iterdir()) == ["v2", "v3"]


def test_apply_and_dry_run_are_mutually_exclusive(bundle_root: Path) -> None:
    result = _run(bundle_root, "--apply", "--dry-run")
    assert result.returncode == 2, result.stdout + result.stderr


def test_incomplete_bundle_is_not_counted(bundle_root: Path) -> None:
    """缺 catalog.json 的残缺目录不算可服务发布包，也不阻止保留判断。"""
    bundles = bundle_root / "outputs" / "web_bundle"
    broken = bundles / "v9-broken"
    broken.mkdir()
    (broken / "index.html").write_text("{}", encoding="utf-8")
    result = _run(bundle_root, "--keep", "2", "--apply")
    assert result.returncode == 0, result.stdout + result.stderr
    names = sorted(d.name for d in bundles.iterdir())
    assert "v9-broken" in names, "残缺目录不应被当作发布包删除"
    assert names == ["v2", "v3", "v9-broken"]
