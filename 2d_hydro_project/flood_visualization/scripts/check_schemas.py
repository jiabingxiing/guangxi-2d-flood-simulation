"""用 JSON Schema 校验 docs/examples 下的契约示例（离线自检，不需要真实发布包）。

运行：
    uv run python scripts/check_schemas.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[1]
PAIRS = (
    ("docs/examples/catalog.example.json", "schemas/catalog.schema.json"),
    ("docs/examples/timeline.example.json", "schemas/timeline.schema.json"),
    ("docs/examples/images.example.json", "schemas/images.schema.json"),
    ("docs/examples/numeric_manifest.example.json", "schemas/numeric_manifest.schema.json"),
    ("docs/examples/statistics.example.json", "schemas/statistics.schema.json"),
)


def main() -> int:
    failed = 0
    for doc_rel, schema_rel in PAIRS:
        doc = json.loads((ROOT / doc_rel).read_text(encoding="utf-8"))
        schema = json.loads((ROOT / schema_rel).read_text(encoding="utf-8"))
        try:
            jsonschema.validate(doc, schema)
        except jsonschema.ValidationError as exc:
            failed += 1
            path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
            print(f"FAIL {doc_rel}  at {path}: {exc.message}")
        else:
            print(f"OK   {doc_rel}")
    print(f"[check_schemas] {'全部通过' if not failed else f'{failed} 个失败'}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
