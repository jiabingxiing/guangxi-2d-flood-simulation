"""阶段0/2：从 Zenodo 下载 LISFLOOD-FP 8.1（开放获取，GPL v3 非商业）。

用法：uv --directory . run python scripts/stage0/download_lisflood.py
规则（终稿 四章）：超时明确、流式下载、MD5 校验、元数据与哈希落盘；幂等（已存在且校验通过则跳过）。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import requests

PROJ_ROOT = Path(__file__).resolve().parents[2]
DEST_DIR = PROJ_ROOT / "data" / "raw" / "lisflood-fp"
RECORD_API = "https://zenodo.org/api/records/6912932"  # LISFLOOD-FP 8.1
META_OUT = PROJ_ROOT / "data" / "catalog" / "lisflood_fp_download.json"


def main() -> None:
    DEST_DIR.mkdir(parents=True, exist_ok=True)
    resp = requests.get(RECORD_API, timeout=(10, 30))
    resp.raise_for_status()
    record = resp.json()
    files = [
        {
            "name": f["key"],
            "size": f["size"],
            "md5": f["checksum"].replace("md5:", ""),
            "url": f["links"]["self"],
        }
        for f in record.get("files", [])
    ]
    meta = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "zenodo_record": RECORD_API,
        "title": record.get("metadata", {}).get("title"),
        "version": record.get("metadata", {}).get("version"),
        "license": (record.get("metadata", {}).get("license") or {}).get("id"),
        "doi": record.get("doi"),
        "files": files,
    }
    print(f"记录: {meta['title']} v{meta['version']} 许可={meta['license']} DOI={meta['doi']}")
    if not files:
        raise SystemExit("Zenodo 记录无文件，人工检查")

    target = files[0]  # 主发布包
    dest = DEST_DIR / target["name"].replace(" ", "_")
    print(f"目标文件: {target['name']}  {target['size']/1e6:.1f} MB -> {dest}")

    need_download = True
    if dest.exists():
        md5 = hashlib.md5(dest.read_bytes()).hexdigest()
        if md5 == target["md5"]:
            print("已存在且 MD5 一致，跳过下载（幂等）")
            need_download = False
        else:
            print("已存在但 MD5 不一致，重新下载")

    if need_download:
        with requests.get(target["url"], stream=True, timeout=(15, 60)) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", target["size"]))
            done = 0
            with open(dest, "wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
                    done += len(chunk)
                    if done % (16 << 20) < (1 << 20):
                        print(f"  {done/1e6:6.1f} / {total/1e6:.1f} MB")
        md5 = hashlib.md5(dest.read_bytes()).hexdigest()
        if md5 != target["md5"]:
            dest.unlink(missing_ok=True)
            raise SystemExit(f"MD5 校验失败：期望 {target['md5']} 实得 {md5}，已删除残包")
        print("MD5 校验通过")

    meta["downloaded_file"] = str(dest)
    meta["verified_md5"] = target["md5"]
    META_OUT.parent.mkdir(parents=True, exist_ok=True)
    META_OUT.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"元数据 -> {META_OUT}")


if __name__ == "__main__":
    main()
