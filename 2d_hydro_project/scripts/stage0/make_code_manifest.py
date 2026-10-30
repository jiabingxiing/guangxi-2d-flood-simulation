"""生成代码/配置哈希清单（不替代 git commit，作为可追溯版本记录）。"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))
from scripts.common.run_manifest import code_targets, code_tree_hash, sha256  # noqa: E402


def main() -> None:
    tree, per = code_tree_hash()
    out = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "note": "代码/配置 SHA-256；git 未跟踪的代码以本清单作为版本记录。",
        "tree_sha256": tree,
        "files": per,
        "executables": {
            "lisflood_exe": {
                "path": "models/lisflood-fp-8.1/LISFLOOD-FP-trunk/lisflood.exe",
                "sha256": sha256(PROJ / "models/lisflood-fp-8.1/LISFLOOD-FP-trunk/lisflood.exe"),
            }
        },
        "n_files": len(per),
        "targets": [p.relative_to(PROJ).as_posix() for p in code_targets()],
    }
    dst = PROJ / "data" / "catalog" / "code_manifest.json"
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(per)} files, tree={tree[:16]} -> {dst}")


if __name__ == "__main__":
    main()
