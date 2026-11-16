"""路径解析层：让项目脱离固定盘符、固定父目录与固定用户名。

设计约束（对应可移植化计划 §2.1）：

1. **优先项目相对**。项目内资源一律用 `PROJ / ...`，不写盘符。
2. **历史绝对路径可解析**。清单里已冻结的宿主绝对路径是 provenance 证据，
   不改写历史值；读取时经 `resolve_legacy()` 重锚到当前项目根。
3. **锚点重挂，不做字符串替换**。历史路径的盘符与中间目录名都变过
   （旧位置叫「2026年工作」，现位置叫「2026」），只换盘符修不好。
   本模块在路径尾部查找锚点（`2d_hydro_project` / `flood_visualization`），
   按锚点重新挂到当前 `PROJ`；因此再换盘、再改上级目录名都不需要改代码。
4. **fail-closed**。缺关键输入时抛 `PathResolutionError` 并给出可执行提示，
   不回退到旧盘符，不静默产出半成品。

真外部资源（当前只有源 DEM 与光学根）走 `external()`：
`GX_<KEY>` 环境变量 → `configs/paths.local.yaml` → 项目内默认位置 → None。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

PROJ = Path(__file__).resolve().parents[2]
CONFIG_LOCAL = PROJ / "configs" / "paths.local.yaml"

# 历史清单里可能出现的项目锚点目录名；按长度降序匹配，避免前缀误命中。
ANCHORS = ("2d_hydro_project", "flood_visualization")

# 真外部资源的项目内默认位置（相对 PROJ）。文件不存在时 external() 返回 None。
EXTERNAL_DEFAULTS = {
    "DEM_SOURCE": ("data", "raw", "dem_source", "广西.tif"),
    "OPTICAL_ROOT": ("data", "raw", "optical"),
}


class PathResolutionError(FileNotFoundError):
    """历史路径无法映射到当前位置。消息必须含原路径与可执行修复提示。"""


def _anchor_split(parts: list[str]) -> tuple[str, list[str]] | None:
    """在路径分量里找锚点，返回 (锚点名, 锚点之后的分量)。取最靠前的锚点。"""
    best: tuple[int, str] | None = None
    for i, part in enumerate(parts):
        for anchor in ANCHORS:
            if part == anchor and (best is None or i < best[0]):
                best = (i, anchor)
    if best is None:
        return None
    i, anchor = best
    return anchor, parts[i + 1:]


def _reanchor(path: Path) -> Path | None:
    """把历史绝对路径按锚点重新挂到当前项目。返回存在的候选，否则 None。

    `2d_hydro_project` 后仍保留完整尾部（含 `flood_visualization/...`），
    `flood_visualization` 锚点用于直接引用子工程文件的清单条目。
    """
    anchor_hit = _anchor_split(list(path.parts))
    if anchor_hit is None:
        return None
    anchor, tail = anchor_hit
    # `2d_hydro_project` 锚点本身就是 PROJ；`flood_visualization` 锚点是 PROJ 的子目录。
    base = PROJ if anchor == "2d_hydro_project" else PROJ / "flood_visualization"
    candidate = base.joinpath(*tail) if tail else base
    return candidate if candidate.exists() else None


def resolve_legacy(p: str | Path) -> Path:
    """解析历史登记路径到当前位置。

    解析顺序（关键：**当前项目优先于任何碰巧存在的旧盘符路径**）：
    1. 相对路径 → `PROJ / p`
    2. 按锚点重挂到当前项目，结果存在 → 返回重挂结果
    3. 原绝对路径存在 → 返回（真外部资源，如源 DEM）
    4. 全部失败 → 抛 `PathResolutionError`（不静默回退）

    第 2 步必须早于第 3 步：旧盘符上可能同时存在一份过期副本
    （实测 F: 曾有与 E: 哈希相同的完整旧拷贝）。若先判"原路径存在"，
    就会静默读到旧副本而不是当前项目文件。
    """
    path = Path(p)
    if not path.is_absolute():
        candidate = PROJ / path
        if candidate.exists():
            return candidate
        raise PathResolutionError(
            f"项目内路径不存在：{path}（解析为 {candidate}）。"
            "请确认文件是否随项目一起迁移。"
        )
    reanchored = _reanchor(path)
    if reanchored is not None:
        return reanchored
    if path.exists():
        return path
    # 该项目外文件可能是已登记的"真外部资源"（如源 DEM）：
    # 若其文件名与某个外部资源的项目内默认文件名相同，则回退到 external()。
    # 这样清单里的历史 DEM 路径在 DEM 经目录联接/环境变量就位后仍可解析。
    for key in EXTERNAL_DEFAULTS:
        found = external(key)
        if found is not None and found.name == path.name:
            return found
    raise PathResolutionError(
        f"历史路径无法映射到当前位置：{p}。\n"
        f"  当前项目根：{PROJ}\n"
        "  处理方式：① 若该文件已随项目迁移，确认其所在项目子目录名是否改了；"
        "② 若属真外部资源，复制到项目内并用 GX_DEM_SOURCE / GX_OPTICAL_ROOT 覆盖；"
        "③ 若该文件已废弃，从清单移除而不是留死路径。"
    )


def resolve_legacy_strict(p: str | Path) -> Path:
    """审计用严格版：重挂结果与原路径同时存在但指向不同文件时抛错。

    专供清单一致性检查使用，防止"两份副本并存"被静默吞掉。
    """
    path = Path(p)
    if not path.is_absolute() or not path.exists():
        return resolve_legacy(p)
    reanchored = _reanchor(path)
    if reanchored is not None and reanchored.resolve() != path.resolve():
        raise PathResolutionError(
            f"登记路径 {p} 与当前项目文件 {reanchored} 同时存在，"
            "无法判定哪份有效。请删除或移走旧副本，或更新清单。"
        )
    return resolve_legacy(p)


def resolve_or_none(p: str | Path | None) -> Path | None:
    """`resolve_legacy` 的容错版本：解析失败返回 None，供可选资源与审计遍历使用。"""
    if p is None or str(p).strip() == "":
        return None
    try:
        return resolve_legacy(p)
    except PathResolutionError:
        return None


def rel_posix(p: str | Path) -> str:
    """转成项目相对 POSIX 路径，相对于 `PROJ`（即 `2d_hydro_project/`）。

    与既有清单约定一致：`input_manifest.json` 里 `configs/base.yaml`、
    `data/interim/...` 都是相对 `PROJ` 的；消费者（测试与 floodviz）用
    `p if p.is_absolute() else PROJ / p` 解析。不带 `2d_hydro_project/` 前缀。
    """
    path = Path(p).resolve()
    for base in (PROJ.resolve(), PROJ.parent.resolve()):
        try:
            return path.relative_to(base).as_posix()
        except ValueError:
            continue
    raise PathResolutionError(
        f"{path} 不在项目内（项目根 {PROJ}），拒绝写入清单："
        "项目内资源不得记录宿主绝对路径。"
    )


def _load_local_config() -> dict[str, Any]:
    """读 `configs/paths.local.yaml`（本机映射，已被 .gitignore 排除）。"""
    if not CONFIG_LOCAL.is_file():
        return {}
    try:
        import yaml
    except ImportError:  # 无 yaml 时只依赖环境变量，不阻断
        return {}
    try:
        doc = yaml.safe_load(CONFIG_LOCAL.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def external(key: str) -> Path | None:
    """解析真外部资源。顺序：env `GX_<KEY>` → paths.local.yaml → 项目内默认 → None。

    返回 None 表示资源缺失；调用方须降级或明确报错，不得假装成功。
    """
    env_name = f"GX_{key.upper()}"
    from_env = os.environ.get(env_name)
    if from_env:
        candidate = Path(from_env)
        if candidate.exists():
            return candidate

    local = _load_local_config()
    raw = local.get(key) or local.get(key.lower())
    if raw:
        candidate = Path(str(raw))
        if candidate.exists():
            return candidate

    default_parts = EXTERNAL_DEFAULTS.get(key.upper())
    if default_parts:
        candidate = PROJ.joinpath(*default_parts)
        if candidate.exists():
            return candidate
    return None


def require_external(key: str) -> Path:
    """取必需外部资源；缺失时 fail-closed 并给出可执行提示。"""
    found = external(key)
    if found is not None:
        return found
    default_parts = EXTERNAL_DEFAULTS.get(key.upper())
    default_hint = str(PROJ.joinpath(*default_parts)) if default_parts else "（无默认位置）"
    raise PathResolutionError(
        f"缺少必需外部资源 {key}。\n"
        f"  默认位置：{default_hint}\n"
        f"  或设置环境变量 GX_{key.upper()}=<实际路径>\n"
        f"  或在 {CONFIG_LOCAL} 写 {key}: <实际路径>"
    )


def dem_source() -> Path:
    """源 DEM（广西.tif）。当前由用户复制到项目内 data/raw/dem_source/。"""
    return require_external("DEM_SOURCE")