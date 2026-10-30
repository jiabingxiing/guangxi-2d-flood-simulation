"""目录子包：时间轴、质量门、catalog 组装。"""

from __future__ import annotations

from .catalog import (
    apply_assets,
    build_case_entry,
    build_catalog,
    build_run_entry,
    bundle_rel,
    geometry_for_grid,
    validate_catalog,
)
from .quality import load_mass_gate, merge_quality
from .timeline import build_timeline, compute_sim_seconds, format_timestamp, validate_timeline

__all__ = [
    "apply_assets",
    "build_case_entry",
    "build_catalog",
    "build_run_entry",
    "build_timeline",
    "bundle_rel",
    "compute_sim_seconds",
    "format_timestamp",
    "geometry_for_grid",
    "load_mass_gate",
    "merge_quality",
    "validate_catalog",
    "validate_timeline",
]
