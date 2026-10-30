"""把 web/ 静态页面与本地 vendor 复制进发布包，并生成包内 index.html。

包结构（SPEC §3）：
    <bundle>/index.html                  ← web/index.html
    <bundle>/assets/styles.css|app.js|map.js|timeline.js
    <bundle>/assets/vendor/**            ← web/vendor/**（含 VENDOR_MANIFEST.json）
    <bundle>/assets/licenses/**          ← vendor 许可证副本

用法：
    uv run python scripts/build_web.py --bundle-id v3-20261028
    uv run python scripts/build_web.py --bundle outputs/web_bundle/v3-20261028
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
BUNDLE_ROOT = ROOT / "outputs" / "web_bundle"

JS_FILES = ("app.js", "map.js", "timeline.js")
ASSET_FILES = ("styles.css", *JS_FILES)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_tree(src: Path, dst: Path, *, skip: tuple[str, ...] = ()) -> list[Path]:
    copied: list[Path] = []
    for item in sorted(src.rglob("*")):
        rel = item.relative_to(src)
        if any(part in skip for part in rel.parts):
            continue
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            copied.append(target)
    return copied


def build_web(bundle: Path) -> dict:
    if not bundle.is_dir():
        raise SystemExit(f"发布包不存在：{bundle}（先运行 python -m floodviz prepare）")
    index_src = WEB / "index.html"
    if not index_src.is_file():
        raise SystemExit(f"缺少页面入口：{index_src}")

    assets = bundle / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    shutil.copy2(index_src, bundle / "index.html")

    missing = [name for name in ASSET_FILES if not (WEB / name).is_file()]
    if missing:
        raise SystemExit(f"web/ 缺少文件：{missing}")
    for name in ASSET_FILES:
        shutil.copy2(WEB / name, assets / name)

    images_src = WEB / "assets" / "images"
    image_copies: list[Path] = []
    if images_src.is_dir():
        image_copies = copy_tree(images_src, assets / "images")

    vendor_src = WEB / "vendor"
    if not vendor_src.is_dir():
        raise SystemExit(f"缺少 vendor 目录：{vendor_src}（先运行 scripts/fetch_vendor.py）")
    vendor_dst = assets / "vendor"
    if vendor_dst.exists():
        shutil.rmtree(vendor_dst)
    copied = copy_tree(vendor_src, vendor_dst)

    # vendor 许可证单独归集，便于发布审计
    licenses_dir = bundle / "licenses"
    licenses_dir.mkdir(parents=True, exist_ok=True)
    for lic in vendor_src.rglob("LICENSE*"):
        pkg = lic.parent.name
        shutil.copy2(lic, licenses_dir / f"{pkg}-LICENSE.txt")

    # 页面资源清单（供 validate 与排查）
    files = []
    for item in [bundle / "index.html", *(assets / n for n in ASSET_FILES), *image_copies, *copied]:
        if item.is_file():
            files.append(
                {
                    "path": item.relative_to(bundle).as_posix(),
                    "bytes": item.stat().st_size,
                    "sha256": sha256_file(item),
                }
            )
    doc = {
        "schema_version": "1.0",
        "source": "web/",
        "entry": "index.html",
        "assets": files,
        "vendor_manifest_present": (vendor_dst / "VENDOR_MANIFEST.json").is_file(),
    }
    (assets / "web_manifest.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    update_bundle_manifest(bundle, doc)
    print(f"[build_web] 已复制 {len(files)} 个页面资源 -> {assets}")
    return doc


def update_bundle_manifest(bundle: Path, doc: dict) -> None:
    """把页面资源清单合并进包根 manifest.json 的 `web` 字段（保留 pipeline 写的其它字段）。

    约定（SPEC §8）：pipeline 的 prepare 写 `web: {ready: false, files: []}` 占位；
    本步骤把它置为 `ready: true` 并填 files；validate 依赖该标记判断页面是否就绪。
    """
    manifest_path = bundle / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(f"缺少包根 manifest.json：{manifest_path}（先运行 prepare）")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["web"] = {
        "ready": True,
        "entry": doc["entry"],
        "source": doc["source"],
        "files": doc["assets"],
        "vendor_manifest_present": doc["vendor_manifest_present"],
        "built_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(
            timespec="seconds"
        ),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[build_web] manifest.web.ready=true（{len(doc['assets'])} 个文件已登记哈希）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="复制 web/ 与 vendor 到发布包")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--bundle-id", help="bundle id，包位于 outputs/web_bundle/<id>")
    group.add_argument("--bundle", help="发布包绝对/相对路径")
    args = parser.parse_args(argv)
    bundle = Path(args.bundle) if args.bundle else BUNDLE_ROOT / str(args.bundle_id)
    build_web(bundle.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
