"""下载并固定前端 vendor 资源（MapLibre GL JS、Proj4js）。

设计要点：
- 版本与 tarball sha256 写在 configs/vendor.yaml；哈希不符直接失败，不"升级到 latest"。
- 解包只取需要的文件，写 web/vendor/<pkg>/，同时输出 VENDOR_MANIFEST.json 记录版本/来源/哈希。
- 二级缓存放 data/external/vendor-cache/，离线重建时可直接复用（仍校验哈希）。

用法：
    uv run python scripts/fetch_vendor.py --config configs/default.yaml [--offline]
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import tarfile
import urllib.request
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
VENDOR_CONFIG = ROOT / "configs" / "vendor.yaml"
CACHE_DIR = ROOT / "data" / "external" / "vendor-cache"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path) -> bytes:
    with urllib.request.urlopen(url, timeout=180) as resp:
        payload = resp.read()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(payload)
    return payload


def fetch_package(pkg: dict[str, Any], *, offline: bool) -> dict[str, Any]:
    name, version = pkg["name"], pkg["version"]
    expected = pkg.get("tarball_sha256")
    cache_path = CACHE_DIR / f"{name}-{version}.tgz"
    if cache_path.is_file():
        payload = cache_path.read_bytes()
        digest = sha256_bytes(payload)
        if expected and digest != expected:
            raise SystemExit(f"{name}@{version} 缓存哈希不符：{digest} != {expected}")
    elif offline:
        raise SystemExit(f"离线模式但缓存缺失：{cache_path}")
    else:
        payload = download(pkg["url"], cache_path)
        digest = sha256_bytes(payload)
        if expected and digest != expected:
            raise SystemExit(f"{name}@{version} tarball 哈希不符：{digest} != {expected}")
    if not expected:
        raise SystemExit(f"{name}@{version} 缺少 tarball_sha256，拒绝安装未固定版本")

    target_dir = ROOT / str(pkg["target_dir"])
    target_dir.mkdir(parents=True, exist_ok=True)
    written: list[dict[str, Any]] = []
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tf:
        for entry in pkg["files"]:
            src, dst = entry["src"], entry["dst"]
            try:
                member = tf.getmember(src)
            except KeyError as exc:
                raise SystemExit(f"{name}@{version} 缺少文件 {src}") from exc
            data = tf.extractfile(member).read()
            out = target_dir / dst
            out.write_bytes(data)
            written.append(
                {
                    "file": str(out.relative_to(ROOT)).replace("\\", "/"),
                    "bytes": len(data),
                    "sha256": sha256_bytes(data),
                }
            )
    return {
        "name": name,
        "version": version,
        "license": pkg["license"],
        "url": pkg["url"],
        "tarball_sha256": digest,
        "target_dir": str(pkg["target_dir"]),
        "files": written,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下载固定版本前端资源")
    parser.add_argument("--config", default=str(ROOT / "configs" / "default.yaml"), help="主配置（仅用于定位项目根）")
    parser.add_argument("--vendor-config", default=str(VENDOR_CONFIG))
    parser.add_argument("--offline", action="store_true", help="只用本地缓存，不联网")
    args = parser.parse_args(argv)

    vendor_path = Path(args.vendor_config)
    doc = yaml.safe_load(vendor_path.read_text(encoding="utf-8"))
    records = [fetch_package(pkg, offline=args.offline) for pkg in doc["packages"]]
    manifest = {
        "schema_version": "1.0",
        "vendored_utc": __import__("datetime")
        .datetime.now(__import__("datetime").timezone.utc)
        .isoformat(timespec="seconds"),
        "note": "固定版本本地资源；运行时禁止 CDN。重新生成请运行 scripts/fetch_vendor.py",
        "packages": records,
    }
    out = ROOT / "web" / "vendor" / "VENDOR_MANIFEST.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for rec in records:
        print(f"[vendor] {rec['name']}@{rec['version']} -> {rec['target_dir']} ({len(rec['files'])} files)")
    print(f"[vendor] manifest: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
