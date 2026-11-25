"""hydro_refine 核心基础层：位置无关路径解析与栅格通用工具。"""
from __future__ import annotations

from hydro_refine.core.paths import (
    CONFIG_LOCAL,
    PROJ,
    PathResolutionError,
    external,
    rel_posix,
    resolve_legacy,
    resolve_or_none,
)

__all__ = [
    "CONFIG_LOCAL",
    "PROJ",
    "PathResolutionError",
    "external",
    "rel_posix",
    "resolve_legacy",
    "resolve_or_none",
]
