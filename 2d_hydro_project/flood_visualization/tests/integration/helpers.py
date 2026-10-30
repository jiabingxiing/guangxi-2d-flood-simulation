"""集成测试共享常量与读取工具（同目录模块，避免与 conftest 名称冲突）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]

SMOKE_BUNDLE = "qa_smoke"
SMOKE_FRAMES = 3
FULL_BUNDLE = "qa_jiulong_full"
FULL_FRAMES = 21
JIULONG_RUN = "scen_free_200m_sv6h"


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def case_in_catalog(bundle: Path, case_id: str = "jiulong") -> dict[str, Any]:
    catalog = load_json(Path(bundle) / "catalog.json")
    for entry in catalog["cases"]:
        if entry["case_id"] == case_id:
            return entry
    raise AssertionError(f"catalog 中缺少案例 {case_id}")


def run_in_catalog(bundle: Path, case_id: str = "jiulong", run_id: str = JIULONG_RUN) -> dict[str, Any]:
    for run in case_in_catalog(bundle, case_id)["runs"]:
        if run["run_id"] == run_id:
            return run
    raise AssertionError(f"catalog 中缺少 run {case_id}/{run_id}")


def walk_json_strings(node: Any):
    """递归产出文档中的全部字符串值。"""
    if isinstance(node, dict):
        for value in node.values():
            yield from walk_json_strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk_json_strings(item)
    elif isinstance(node, str):
        yield node
