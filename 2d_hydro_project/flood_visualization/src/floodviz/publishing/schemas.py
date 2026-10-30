"""发布包契约校验：用 JSON Schema 校验真实包内的 JSON（供 validate --full 复用）。

与 `scripts/check_schemas.py` 的区别：那个校验 `docs/examples` 示例；
本模块校验 `outputs/web_bundle/<id>` 实际产物。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema

ROOT = Path(__file__).resolve().parents[3]  # src/floodviz/publishing/schemas.py -> 子项目根
SCHEMA_DIR = ROOT / "schemas"

# (包内相对路径模式, schema 文件, 是否必需)
SCHEMA_TARGETS: tuple[tuple[str, str, bool], ...] = (
    ("catalog.json", "catalog.schema.json", True),
    ("cases/*/*/timeline.json", "timeline.schema.json", True),
    ("cases/*/*/statistics.json", "statistics.schema.json", True),
    ("frames/*/*/images.json", "images.schema.json", True),
    ("numeric/*/*/manifest.json", "numeric_manifest.schema.json", True),
)


def load_schema(name: str) -> dict[str, Any]:
    path = SCHEMA_DIR / name
    return json.loads(path.read_text(encoding="utf-8"))


def validate_document(doc: dict[str, Any], schema_name: str) -> list[str]:
    """返回错误信息列表（空列表 = 通过）。"""
    schema = load_schema(schema_name)
    validator = jsonschema.Draft202012Validator(schema)
    errors: list[str] = []
    for err in sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(p) for p in err.absolute_path) or "<root>"
        errors.append(f"{where}: {err.message}")
    return errors


def validate_bundle_documents(bundle: str | Path) -> dict[str, list[str]]:
    """对包内所有受 schema 约束的文档做校验；返回 {相对路径: 错误列表}（仅含失败项）。"""
    bundle = Path(bundle)
    failures: dict[str, list[str]] = {}
    for pattern, schema_name, required in SCHEMA_TARGETS:
        matched = sorted(bundle.glob(pattern))
        if not matched:
            if required:
                failures[pattern] = ["未找到匹配文件（必需）"]
            continue
        for path in matched:
            rel = path.relative_to(bundle).as_posix()
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                failures[rel] = [f"JSON 解析失败：{exc}"]
                continue
            errors = validate_document(doc, schema_name)
            if errors:
                failures[rel] = errors
    return failures
