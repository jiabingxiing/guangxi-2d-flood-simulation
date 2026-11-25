"""位置无关路径解析入口（复用并扩展 `scripts/common/paths.py`）。

严格遵守项目可移植性四大纪律：
1. 项目内资源一律相对 `PROJ`（即 `2d_hydro_project/`）解析；
2. 真外部资源经 `external()` 按 `GX_<KEY>` → `configs/paths.local.yaml` → 默认相对路径解析；
3. 清单/配置落盘只存相对 POSIX 路径（`rel_posix()`）；
4. 历史绝对路径经 `resolve_legacy()` 按锚点重挂到当前项目根。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parents[3]

if str(PROJ) not in sys.path:
    sys.path.insert(0, str(PROJ))

from scripts.common import paths as _base_paths  # noqa: E402
from scripts.common.paths import (  # noqa: E402
    CONFIG_LOCAL,
    PathResolutionError,
    rel_posix,
    resolve_legacy,
    resolve_or_none,
)

EXTRA_EXTERNAL_DEFAULTS: dict[str, tuple[str, ...]] = {
    "OSM_PBF": ("data", "raw", "osm", "china-latest.osm.pbf"),
    "GAUGE_ARCHIVE": ("data", "raw", "gauges"),
}


def external(key: str, default_rel: str | Path | None = None) -> Path | None:
    """解析真外部资源，优先复用 `scripts.common.paths.external`，并补充 OSM_PBF 等键。"""
    found = _base_paths.external(key)
    if found is not None:
        return found

    env_name = f"GX_{key.upper()}"
    from_env = os.environ.get(env_name)
    if from_env:
        cand = Path(from_env)
        if cand.exists():
            return cand

    default_parts = EXTRA_EXTERNAL_DEFAULTS.get(key.upper())
    if default_parts:
        cand = PROJ.joinpath(*default_parts)
        if cand.exists():
            return cand

    if default_rel is not None:
        cand = PROJ / default_rel
        if cand.exists():
            return cand
    return None
