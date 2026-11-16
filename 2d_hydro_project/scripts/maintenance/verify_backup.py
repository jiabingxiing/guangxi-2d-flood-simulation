"""校验备份完整性：逐项复核 manifest.json 的 SHA-256 与体积。

只读操作，不修改备份内容。退出码 0 = 全部通过；1 = 有失配。

用法：
    uv --directory 2d_hydro_project run python scripts/maintenance/verify_backup.py \
        --dir "<备份批次目录>"
    # 或指定备份根目录（默认取 GX_BACKUP_ROOT），校验最新批次
    ... verify_backup.py --root "<备份根>"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while blk := f.read(chunk):
            h.update(blk)
    return h.hexdigest()


def latest_batch(root: Path) -> Path | None:
    batches = sorted(
        (p for p in root.iterdir() if p.is_dir() and (p / "manifest.json").is_file()),
        key=lambda p: p.name,
    )
    return batches[-1] if batches else None


def verify(batch: Path) -> tuple[bool, list[str], list[str]]:
    ok: list[str] = []
    bad: list[str] = []
    manifest_path = batch / "manifest.json"
    if not manifest_path.is_file():
        return False, [], [f"缺 manifest.json：{batch}"]

    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    for art in doc.get("artifacts") or []:
        name = art.get("file")
        path = batch / str(name)
        if not path.is_file():
            bad.append(f"{name}: 文件缺失")
            continue
        size = path.stat().st_size
        if size != art.get("bytes"):
            bad.append(f"{name}: 体积不符（磁盘 {size} vs 清单 {art.get('bytes')}）")
            continue
        digest = sha256_file(path)
        if digest != art.get("sha256"):
            bad.append(f"{name}: SHA-256 不符（磁盘 {digest[:12]} vs 清单 "
                       f"{str(art.get('sha256'))[:12]}）— 备份已损坏或非原始文件")
            continue
        ok.append(f"{name}: {size/1e6:.1f} MB  SHA-256 通过")

    # zip 内部完整性（结构检查，不解压）
    worktree = batch / "worktree.zip"
    if worktree.is_file():
        try:
            with zipfile.ZipFile(worktree) as zf:
                broken = zf.testzip()
                if broken:
                    bad.append(f"worktree.zip: 内部成员损坏（首个：{broken}）")
                else:
                    ok.append(f"worktree.zip: {len(zf.namelist())} 个成员结构完好")
        except (OSError, zipfile.BadZipFile) as exc:
            bad.append(f"worktree.zip: 无法读取（{type(exc).__name__}: {exc}）")

    return not bad, ok, bad


def main() -> int:
    ap = argparse.ArgumentParser(description="校验备份完整性（只读）")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--dir", help="具体批次目录（含 manifest.json）")
    group.add_argument("--root", help="备份根目录，自动取最新批次")
    args = ap.parse_args()

    batch = Path(args.dir) if args.dir else None
    if batch is None:
        root = Path(args.root)
        if not root.is_dir():
            print(f"[FAIL] 备份根目录不存在：{root}")
            return 1
        batch = latest_batch(root)
        if batch is None:
            print(f"[FAIL] {root} 下没有含 manifest.json 的批次")
            return 1

    print(f"校验批次：{batch}\n")
    passed, ok, bad = verify(batch)
    for line in ok:
        print(f"[OK  ] {line}")
    for line in bad:
        print(f"[FAIL] {line}")
    print()
    if passed:
        print("结论：备份完整，可用于恢复。")
        return 0
    print("结论：备份存在问题，不要依赖它恢复；请重新执行 make_backup.py。")
    return 1


if __name__ == "__main__":
    sys.exit(main())