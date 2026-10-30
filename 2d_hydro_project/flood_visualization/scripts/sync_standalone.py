"""把发布包同步到 standalone 双击打开目录，并保留启动器文件。

为什么需要这个脚本：`robocopy /MIR` 会删除目标目录中源目录没有的文件，
包括手工维护的 `打开可视化.cmd`、`server.ps1`、`使用说明.txt`、`README.md`。
2026-10-28 已因此误删过一次。本脚本显式保护这 4 个文件。

为什么还要清理残留：发布包换 run 时（如 200 m 旧 run → 100 m 真实DEM 新 run），
`numeric/<case>/<old_run>/`、`frames/<case>/<old_run>/` 在源包里已不存在，
但目标目录仍保留。实测 2026-10-30 残留 2 876 个文件、639.8 MB，
双击打开会看到与发布包不一致的旧数据。本脚本按「源包没有就删」补齐清理。

用法：
    uv run python scripts/sync_standalone.py --bundle-id v6-20261029
    uv run python scripts/sync_standalone.py --bundle-id v6-20261029 --dry-run
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ROOT = ROOT / "outputs" / "web_bundle"
STANDALONE = ROOT / "standalone" / "广西二维洪涝可视化_双击打开"
#: 启动器真源：纳入版本控制；standalone 目录里的副本每次同步时从这里重新生成
LAUNCHER_SOURCE = ROOT / "standalone" / "_launcher"

#: 启动器文件；缺失即报错。README.md 含 {{BUNDLE_ID}} 占位符，同步时替换
LAUNCHER_FILES = ("打开可视化.cmd", "server.ps1", "使用说明.txt", "README.md")
BUNDLE_ID_PLACEHOLDER = "{{BUNDLE_ID}}"

#: 发布包内容，同步时镜像覆盖
BUNDLE_ENTRIES = ("assets", "cases", "frames", "licenses", "numeric",
                  "catalog.json", "index.html", "manifest.json")


def _human(n: int) -> str:
    return f"{n / 1024 / 1024:.0f} MB" if n >= 1024 * 1024 else f"{n / 1024:.0f} kB"


def _sync_launcher(bundle_id: str, dry_run: bool) -> int:
    """从 _launcher 复制启动器到 standalone 目录；返回写入数量。

    按字节处理：只替换 {{BUNDLE_ID}} 占位符，不改动行尾。
    .cmd/.ps1 在 Windows 上必须是 CRLF，任何文本模式读写都可能把它改成 LF。
    """
    copied = 0
    placeholder = BUNDLE_ID_PLACEHOLDER.encode()
    for name in LAUNCHER_FILES:
        source = LAUNCHER_SOURCE / name
        target = STANDALONE / name
        rendered = source.read_bytes().replace(placeholder, bundle_id.encode())
        if target.is_file() and target.read_bytes() == rendered:
            continue
        copied += 1
        if dry_run:
            continue
        target.write_bytes(rendered)
    return copied


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-id", required=True, help="发布包 id，如 v3-20261028")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写文件")
    args = parser.parse_args()

    src = BUNDLE_ROOT / args.bundle_id
    if not (src / "index.html").is_file():
        print(f"[错误] 发布包不存在或缺 index.html：{src}", file=sys.stderr)
        return 2
    # standalone 目录可能被整体删除（例如误用 robocopy /MIR 之后再清理），允许重建
    if not STANDALONE.is_dir():
        print(f"[创建] standalone 目录不存在，新建：{STANDALONE}")
        if not args.dry_run:
            STANDALONE.mkdir(parents=True)

    missing = [f for f in LAUNCHER_FILES if not (LAUNCHER_SOURCE / f).is_file()]
    if missing:
        print(f"[错误] 启动器真源缺文件：{LAUNCHER_SOURCE} -> {missing}", file=sys.stderr)
        print("       真源在版本控制内，用 git checkout 恢复。", file=sys.stderr)
        return 3

    # 1) 删除发布包内容中已不在源里的旧条目（等价于 /MIR，但不碰保护名单）
    for name in BUNDLE_ENTRIES:
        target = STANDALONE / name
        if not target.exists():
            continue
        source = src / name
        if source.exists():
            continue
        print(f"[删除] {name}（源包已无此项）")
        if not args.dry_run:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()

    # 2) 复制发布包内容
    copied = 0
    for name in BUNDLE_ENTRIES:
        source = src / name
        if not source.exists():
            continue
        target = STANDALONE / name
        if source.is_dir():
            for item in source.rglob("*"):
                if not item.is_file():
                    continue
                rel = item.relative_to(src)
                out = STANDALONE / rel
                if (out.is_file() and out.stat().st_size == item.stat().st_size
                        and out.stat().st_mtime >= item.stat().st_mtime):
                    continue
                copied += 1
                if not args.dry_run:
                    out.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, out)
        else:
            if (target.is_file() and target.stat().st_size == source.stat().st_size
                    and target.stat().st_mtime >= source.stat().st_mtime):
                continue
            copied += 1
            if not args.dry_run:
                shutil.copy2(source, target)

    # 2b) 清理残留：删除 standalone 镜像目录中源包已不存在的文件
    #
    # 为什么需要这一步：第 1 步只处理「整个顶层条目在源里缺失」的情况。
    # 但发布包换 run（例如 200 m 旧 run → 100 m 真实DEM 新 run）时，
    # numeric/<case>/<old_run>/、frames/<case>/<old_run>/ 这些子目录在源里
    # 已经不存在，而第 1 步看到 numeric/、frames/ 仍存在就跳过了。
    # 实测（2026-10-30）：standalone 残留 2876 个旧文件、639.8 MB。
    pruned_files = 0
    pruned_bytes = 0
    for name in BUNDLE_ENTRIES:
        target_root = STANDALONE / name
        source_root = src / name
        if not target_root.is_dir():
            continue
        if source_root.is_dir():
            for item in sorted(target_root.rglob("*"), reverse=True):
                if not item.is_file():
                    continue
                rel = item.relative_to(STANDALONE)
                if (src / rel).exists():
                    continue
                pruned_bytes += item.stat().st_size
                pruned_files += 1
                if not args.dry_run:
                    item.unlink()
        elif source_root.is_file():
            continue
        else:
            # 源包没有这个顶层条目，第 1 步已处理
            continue

    # 2c) 删除清理后留下的空目录（自底向上，不碰 STANDALONE 根）
    pruned_dirs = 0
    if not args.dry_run:
        for name in BUNDLE_ENTRIES:
            target_root = STANDALONE / name
            if not target_root.is_dir():
                continue
            for item in sorted(target_root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                if item.is_dir() and not any(item.iterdir()):
                    item.rmdir()
                    pruned_dirs += 1
    else:
        for name in BUNDLE_ENTRIES:
            target_root = STANDALONE / name
            if not target_root.is_dir():
                continue
            for item in sorted(target_root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                if item.is_dir() and not any(item.iterdir()):
                    pruned_dirs += 1

    # 3) 从版本控制的真源重新生成启动器（README 内替换发布包 id）
    launcher_written = _sync_launcher(args.bundle_id, args.dry_run)

    # 4) 兜底检查：启动器仍在，且不含未替换占位符
    still_missing = [f for f in LAUNCHER_FILES if not (STANDALONE / f).is_file()]
    if still_missing and not args.dry_run:
        print(f"[错误] 同步后启动器丢失：{still_missing}", file=sys.stderr)
        return 4
    if not args.dry_run:
        leftover = [
            f for f in LAUNCHER_FILES
            if BUNDLE_ID_PLACEHOLDER.encode() in (STANDALONE / f).read_bytes()
        ]
        if leftover:
            print(f"[错误] 启动器仍有未替换占位符：{leftover}", file=sys.stderr)
            return 5

    total = sum(p.stat().st_size for p in STANDALONE.rglob("*") if p.is_file())
    print(f"[sync_standalone] {'（dry-run）' if args.dry_run else ''}"
          f"bundle={args.bundle_id} 复制 {copied} 个文件；"
          f"目录共 {len(list(STANDALONE.rglob('*')))} 项 / {_human(total)}")
    if pruned_files or pruned_dirs:
        print(f"[sync_standalone] 清理残留 {'（dry-run 未执行）' if args.dry_run else ''}"
              f"{pruned_files} 个文件 / {_human(pruned_bytes)}，"
              f"{pruned_dirs} 个空目录")
    print(f"[sync_standalone] 启动器来自版本控制真源：{LAUNCHER_SOURCE.name}/"
          f"（本次写入 {launcher_written} 个）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
