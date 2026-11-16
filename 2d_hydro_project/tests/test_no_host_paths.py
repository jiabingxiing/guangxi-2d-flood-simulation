"""静态扫描门：运行期代码/配置不得绑定宿主盘符（可移植化计划 SC-01）。

守的是"换一块移动硬盘就坏"这一类缺陷：把 `D:\\...`、`F:\\...` 写进
代码或配置后，盘符一变就失效，而测试可能因为 cwd 碰巧正确而通过。

扫描范围只含**运行期**资产：
  - scripts/**（含 maintenance、common、stage*）
  - configs/**（YAML）
  - src/**（包本体）
  - 根目录的生成脚本（build_*.py、generate_*.py、update_*.py）

不扫描：
  - data/catalog/*.json —— 历史清单是 provenance 证据，允许保留旧路径，
    读取时由 scripts/common/paths.py 重锚；改写它们会破坏可追溯性。
  - docs/、plans/、*.md —— 文档叙述可提及历史路径。
  - tests/ 自身 —— 测试里要构造"历史路径"样本。

白名单：http(s) 链接、JSON Schema $id、盘符出现在注释或字符串说明中
一律不算违规，由正则先剔除 URL 再判断。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJ = Path(__file__).resolve().parents[1]
ROOT = PROJ.parent

# 运行期资产：这些位置出现宿主盘符即为缺陷
SCAN_DIRS = [PROJ / "scripts", PROJ / "configs", PROJ / "src"]
SCAN_ROOT_GLOBS = ("build_*.py", "generate_*.py", "update_*.py")

# 盘符形式：D:\  D:/  d:\\ （转义）
DRIVE_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]{1,2}")
URL_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"'<>)]*")

# 已知例外：仅出现在注释/说明中、且不参与路径解析的行可以豁免。
# 每条必须写明理由，且尽量收窄到具体文件。
ALLOWLIST = {
    # 说明性注释：告诉读者历史上的盘符是什么，便于理解迁移背景
    "scripts/stage2/setup_toolchain.py": "MSYS2 安装脚本面向本机 D: 工具链，属一次性环境脚本",
    "scripts/stage2/setup_w64devkit.py": "同上，一次性工具链安装脚本",
}


def _iter_files() -> list[Path]:
    files: list[Path] = []
    for d in SCAN_DIRS:
        if d.is_dir():
            files += [p for p in d.rglob("*") if p.is_file() and p.suffix in (".py", ".yaml", ".yml")]
    for pattern in SCAN_ROOT_GLOBS:
        files += [p for p in ROOT.glob(pattern) if p.is_file()]
    return sorted(set(files))


def _strip_urls(line: str) -> str:
    return URL_RE.sub("", line)


def _is_comment_or_docstring_noise(line: str) -> bool:
    """注释里的历史说明不算缺陷：迁移背景需要文字记录旧盘符。

    只豁免**整行注释**（Python `#`、YAML `#`）里出现的盘符；
    代码行与 YAML 值里出现盘符仍然算违规。
    """
    return line.lstrip().startswith("#")


def _scan(path: Path) -> list[tuple[int, str]]:
    """扫描可执行语句与配置值中的宿主盘符。

    跳过两类"说明性文本"，它们不参与路径解析，且迁移文档需要保留历史盘符：
      1. 整行注释（Python 井号、YAML 井号）；
      2. 模块 docstring（文件开头的三引号块）内部。

    代码字符串字面量与 YAML 值里的盘符**仍然算违规**——那才是会失效的东西。
    """
    hits: list[tuple[int, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return hits

    in_docstring = False
    quote = ""
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()

        # 跟踪模块级 docstring（仅文件开头的三引号块）
        if i <= _docstring_scan_limit(text):
            if not in_docstring and (stripped.startswith('"""') or stripped.startswith("'''")):
                quote = stripped[:3]
                # 单行 docstring（同时闭合）
                if stripped.count(quote) >= 2 and len(stripped) > 3:
                    continue
                in_docstring = True
                continue
            if in_docstring:
                if quote and quote in line:
                    in_docstring = False
                continue

        if _is_comment_or_docstring_noise(line):
            continue
        cleaned = _strip_urls(line)
        if DRIVE_RE.search(cleaned):
            hits.append((i, line.strip()))
    return hits


def _docstring_scan_limit(text: str) -> int:
    """模块 docstring 只会出现在文件开头；给一个宽松上限避免误判正文。"""
    return 60


def test_scan_targets_are_nonempty() -> None:
    """防止扫描范围写错导致"零违规"变成假通过。"""
    files = _iter_files()
    assert len(files) >= 20, f"扫描文件过少（{len(files)}），检查 SCAN_DIRS 是否失效"
    assert any(p.suffix == ".py" for p in files)
    assert any(p.suffix in (".yaml", ".yml") for p in files)


@pytest.mark.parametrize("path", _iter_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_host_drive_paths(path: Path) -> None:
    rel = path.relative_to(PROJ).as_posix() if path.is_relative_to(PROJ) else str(path.relative_to(ROOT))
    if rel in ALLOWLIST:
        pytest.skip(f"白名单：{ALLOWLIST[rel]}")
    hits = _scan(path)
    assert not hits, (
        f"{rel} 含宿主盘符（换盘后必失效）：\n"
        + "\n".join(f"  L{i}: {t}" for i, t in hits[:5])
        + "\n修复：改用 scripts/common/paths.py 的 resolve_legacy() / external() / rel_posix()，"
        "或用 PROJ 相对路径。"
    )


def test_catalog_json_may_keep_legacy_paths_but_is_resolvable() -> None:
    """历史清单允许保留旧路径，但必须能被 paths 解析（证明重锚机制覆盖到）。

    只抽查 input_manifest.json 这一份被测试与发布包实际消费的清单。
    """
    import json
    import sys

    if str(PROJ) not in sys.path:
        sys.path.insert(0, str(PROJ))
    from scripts.common import paths

    manifest = PROJ / "data" / "catalog" / "input_manifest.json"
    if not manifest.is_file():
        pytest.skip("input_manifest.json 不存在")
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    files = doc.get("files") or {}
    if not files:
        pytest.skip("清单无条目")

    absolute_legacy = [
        (k, v.get("path")) for k, v in files.items()
        if isinstance(v, dict) and isinstance(v.get("path"), str) and DRIVE_RE.search(v["path"])
    ]
    if not absolute_legacy:
        return  # 已全部相对化，更好

    failed: list[tuple[str, str]] = []
    for key, legacy in absolute_legacy:
        if paths.resolve_or_none(legacy) is None:
            failed.append((key, legacy))

    if paths.external("DEM_SOURCE") is not None:
        # 源 DEM 已就位时，清单里的它也必须可解析
        dem_failed = [desc for key, desc in failed if key == "dem_source_guangxi"]
        assert not dem_failed, f"源 DEM 已就位但清单路径不可解析：{dem_failed}"

    # 真外部资源（DEM/光学）未就位时允许解析失败，由 external 语义承担；
    # 其余条目必须能重锚到当前项目。
    external_ok = {"dem_source_guangxi"}
    unexpected = [
        f"{key}: {desc}" for key, desc in failed
        if key not in external_ok and "dem_source" not in key and "optical" not in key
    ]
    assert not unexpected, "以下清单条目的历史路径无法重锚到当前项目：\n" + "\n".join(unexpected)