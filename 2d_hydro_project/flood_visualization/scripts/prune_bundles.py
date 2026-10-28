"""发布包保留策略：强制保留最近 N 个 bundle_id，并拒绝对最低保留数以下的删除。

为什么需要这个脚本：仓库约定「必须同时保留最近两个 bundle_id，回滚时只改启动器目标」，
但这条规则原先只写在文档里，靠人记住。2026-10-28 删除 v1-20261027 后，
v2 变成唯一的回滚目标；如果下次构建 v4 时再顺手删 v2，就只剩当前版本，回滚必须先重建。

用法：
    uv run python scripts/prune_bundles.py --keep 2 --dry-run
    uv run python scripts/prune_bundles.py --keep 2 --apply

安全约束（硬编码，不可用参数放宽）：
    MIN_KEEP = 2   —— 任何情况下不得少于 2 个可服务发布包
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ROOT = ROOT / "outputs" / "web_bundle"

#: 绝对下限：不允许少于 2 个发布包（回滚必须有目标）
MIN_KEEP = 2

#: 视为「可服务发布包」的必要文件
REQUIRED = ("index.html", "manifest.json", "catalog.json")


def _servable(d: Path) -> bool:
    return d.is_dir() and all((d / name).is_file() for name in REQUIRED)


def _created_utc(d: Path) -> str:
    """按 catalog.json 的 created_utc 排序；缺失时回落到目录名。"""
    import json

    try:
        with (d / "catalog.json").open(encoding="utf-8") as f:
            doc = json.load(f)
        return str(doc.get("created_utc") or "")
    except (OSError, ValueError):
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", type=int, default=MIN_KEEP,
                        help=f"保留最近几个发布包（不得小于 {MIN_KEEP}）")
    parser.add_argument("--apply", action="store_true", help="实际删除；省略则只打印计划")
    parser.add_argument("--dry-run", action="store_true", help="显式 dry-run（默认行为）")
    args = parser.parse_args()

    if args.keep < MIN_KEEP:
        print(f"[错误] --keep={args.keep} 低于硬下限 {MIN_KEEP}。", file=sys.stderr)
        print("       回滚必须有目标版本，不允许把发布包删到只剩 1 个。", file=sys.stderr)
        return 2
    if args.apply and args.dry_run:
        print("[错误] --apply 与 --dry-run 互斥。", file=sys.stderr)
        return 2
    if not BUNDLE_ROOT.is_dir():
        print(f"[错误] 发布包目录不存在：{BUNDLE_ROOT}", file=sys.stderr)
        return 2

    bundles = sorted(
        (d for d in BUNDLE_ROOT.iterdir() if _servable(d)),
        key=lambda d: (_created_utc(d), d.name),
        reverse=True,
    )
    if not bundles:
        print(f"[错误] {BUNDLE_ROOT} 下无可用发布包。", file=sys.stderr)
        return 2

    keep = bundles[: args.keep]
    prune = bundles[args.keep:]

    print(f"[prune_bundles] 发现 {len(bundles)} 个可服务发布包（按 created_utc 新→旧）：")
    for i, d in enumerate(bundles):
        mark = "保留" if i < args.keep else "删除"
        print(f"  [{mark}] {d.name}  created_utc={_created_utc(d) or '(未知)'}")
    print(f"[prune_bundles] 保留下限 MIN_KEEP={MIN_KEEP}；本次保留 {len(keep)} 个、"
          f"候选删除 {len(prune)} 个")

    if not prune:
        print("[prune_bundles] 无需删除。")
        return 0

    # 兜底：即使 --keep 很大，删除后也不能少于 MIN_KEEP
    if len(bundles) - len(prune) < MIN_KEEP:
        print(f"[错误] 删除后只剩 {len(bundles) - len(prune)} 个，低于下限 {MIN_KEEP}。",
              file=sys.stderr)
        return 3

    for d in prune:
        size = sum(p.stat().st_size for p in d.rglob("*") if p.is_file())
        if not args.apply:
            print(f"  (dry-run) 将删除 {d.name}  {size / 1024 / 1024:.0f} MB")
            continue
        print(f"  删除 {d.name}  {size / 1024 / 1024:.0f} MB")
        shutil.rmtree(d)

    remaining = [d for d in BUNDLE_ROOT.iterdir() if _servable(d)]
    if len(remaining) < MIN_KEEP:
        print(f"[错误] 操作后只剩 {len(remaining)} 个发布包，违反保留下限。", file=sys.stderr)
        return 4
    print(f"[prune_bundles] 完成；现存 {len(remaining)} 个：{sorted(d.name for d in remaining)}")
    if not args.apply:
        print("[prune_bundles] 这是 dry-run，未删除任何文件。加 --apply 才执行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
