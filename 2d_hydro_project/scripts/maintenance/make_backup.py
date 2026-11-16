"""建立项目备份：代码包 + 工作区快照 + 不可再生成果包 + 可选全量数据包。

背景：项目随移动硬盘迁移，该盘会整盘掉线（2026-11-16 实测），
且仓库无远程（`git remote -v` 为空）。一旦掉盘即成真损失，因此必须有
落在**另一块物理盘**上的可校验备份。

四个层级：

1. `code.bundle`  —— `git bundle --all`，含全部提交历史，可独立克隆（约 31 MB）。
2. `worktree.zip` —— 工作区代码/文档/配置/清单 + data 下的中间件与证据（约 0.8 GB）。
3. `results.zip`  —— **不可再生成果**：模型算例输出、诊断证据、网页发布包与归档包、
   standalone 包（约 13 GB）。这是本项目最需要保护的部分。
   理由：AGENTS.md 硬约束「禁止重跑 lisflood.exe（除非用户单独批准）」，
   因此 `outputs/**` 的 res.mass / res.max / 帧数据**不能**用代码重建；
   `web_bundle/**` 与 `_archive_bundles/**` 是数据级回滚源，同样不可再生。
4. `data_full.zip` —— `--include-heavy` 时追加完整 data/（含原始 SAR/WorldCover/S1 ZIP）。

默认（不加参数）**包含** `results.zip`：丢弃成果的备份对项目没有实质价值。
如确认成果另有独立保管，可用 `--skip-results` 显式跳过。

`manifest.json` 记录所有产物的 SHA-256 与体积，供 verify_backup.py 校验。

用法：
    uv --directory 2d_hydro_project run python scripts/maintenance/make_backup.py
    ... --include-heavy     # 再追加完整 data/（含 9.81 GB S1 ZIP）
    ... --skip-results      # 明确不要成果包（仅在成果别处已有副本时）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
ROOT = PROJ.parent

# 不可再生成果：模型算例、诊断证据、发布包与回滚包。
# 这些**不能**靠重跑恢复（项目禁止未批准重跑模型），必须进备份。
RESULTS_PATHS = (
    "2d_hydro_project/outputs",
    "2d_hydro_project/diagnostics",
    "2d_hydro_project/flood_visualization/outputs",
    "2d_hydro_project/flood_visualization/standalone",
)


def _default_dest() -> Path:
    """备份根目录：env GX_BACKUP_ROOT → 用户目录下 Backups。

    不在源码里写死盘符：换机器后盘符会变，而备份必须落在**另一块物理盘**上，
    具体位置由使用者用 GX_BACKUP_ROOT 指定（或接受用户目录默认值）。
    """
    env = os.environ.get("GX_BACKUP_ROOT")
    if env:
        return Path(env)
    return Path.home() / "Backups" / "gx-hydro2d"

# 工作区快照排除项：可重建的运行产物，或含凭证
# 注意：data/ 下的 DEM 片段、S1 掩膜、diagnostics 证据是**不可再生**的，
# 必须备份，故 data/ 不在排除项里（仅排除体积巨大且另行保管的 S1 原始 ZIP）。
EXCLUDE_DIRS = {
    ".venv", "__pycache__", ".pytest_cache", ".ruff_cache",
    "outputs", "logs", "models", ".git",
    "S1_gxhydro",  # data/S1_gxhydro：9.81 GB 原始 ZIP，用 --include-heavy 单独处理
    "web_bundle", "_archive_bundles", "standalone",
    "node_modules", ".mypy_cache",
}
EXCLUDE_FILES = {".env", "Thumbs.db", ".DS_Store"}


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while blk := f.read(chunk):
            h.update(blk)
    return h.hexdigest()


def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def make_bundle(dest: Path) -> dict:
    out = dest / "code.bundle"
    if out.exists():
        out.unlink()
    r = run_git(["bundle", "create", str(out), "--all"], ROOT)
    if r.returncode != 0:
        raise SystemExit(f"git bundle 失败：{r.stderr.strip()}")
    head = run_git(["rev-parse", "HEAD"], ROOT).stdout.strip()
    status = run_git(["status", "--porcelain=v1"], ROOT).stdout
    return {
        "file": out.name,
        "bytes": out.stat().st_size,
        "sha256": sha256_file(out),
        "head_commit": head,
        "dirty_entries": len([ln for ln in status.splitlines() if ln.strip()]),
    }


def _iter_worktree() -> list[Path]:
    files: list[Path] = []
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(ROOT).parts
        if any(part in EXCLUDE_DIRS for part in rel_parts):
            continue
        if p.name in EXCLUDE_FILES:
            continue
        files.append(p)
    return files


def make_worktree(dest: Path, max_mb: float = 200.0) -> dict:
    """打包工作区代码/文档/清单。跳过超过 max_mb 的单文件并记录。"""
    out = dest / "worktree.zip"
    skipped: list[str] = []
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in _iter_worktree():
            rel = p.relative_to(ROOT).as_posix()
            size_mb = p.stat().st_size / 1e6
            if size_mb > max_mb:
                skipped.append(f"{rel} ({size_mb:.1f} MB)")
                continue
            try:
                zf.write(p, rel)
                count += 1
            except OSError as exc:
                skipped.append(f"{rel} (读取失败 {type(exc).__name__})")
    return {
        "file": out.name,
        "bytes": out.stat().st_size,
        "sha256": sha256_file(out),
        "entry_count": count,
        "skipped": skipped[:50],
        "skipped_count": len(skipped),
    }


def make_results(dest: Path) -> dict:
    """打包不可再生成果：模型算例 + 诊断证据 + 发布包 + 回滚包 + standalone。

    这些不能靠重跑恢复（AGENTS.md 禁止未批准重跑 lisflood.exe），
    也不在 git 里（outputs/ 被 .gitignore 排除），必须单独归档。
    """
    out = dest / "results.zip"
    count = 0
    skipped: list[str] = []
    per_root: dict[str, int] = {}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for rel_root in RESULTS_PATHS:
            base = ROOT / rel_root
            if not base.is_dir():
                continue
            n = 0
            for p in base.rglob("*"):
                if not p.is_file():
                    continue
                try:
                    zf.write(p, p.relative_to(ROOT).as_posix())
                    count += 1
                    n += 1
                except OSError as exc:
                    skipped.append(f"{p.relative_to(ROOT).as_posix()} ({type(exc).__name__})")
            per_root[rel_root] = n
    return {
        "file": out.name,
        "bytes": out.stat().st_size,
        "sha256": sha256_file(out),
        "entry_count": count,
        "per_root": per_root,
        "skipped": skipped[:50],
        "skipped_count": len(skipped),
        "note": "不可再生：模型算例/诊断证据/发布包；禁止重跑模型故不能用代码重建",
    }


def make_data_full(dest: Path) -> dict:
    """完整 data/ 快照（含原始 SAR/WorldCover/S1 ZIP），用于换盘前的整体归档。"""
    out = dest / "data_full.zip"
    data_root = PROJ / "data"
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in data_root.rglob("*"):
            if not p.is_file():
                continue
            try:
                zf.write(p, p.relative_to(ROOT).as_posix())
                count += 1
            except OSError:
                continue
    return {"file": out.name, "bytes": out.stat().st_size,
            "sha256": sha256_file(out), "entry_count": count}


def main() -> int:
    ap = argparse.ArgumentParser(description="建立可校验备份（代码 + 快照 + 成果 + 可选全量数据）")
    ap.add_argument("--dest", default=None,
                    help="备份根目录（默认取环境变量 GX_BACKUP_ROOT，否则 ~/Backups/gx-hydro2d）")
    ap.add_argument("--label", default=None, help="备份批次名（默认时间戳）")
    ap.add_argument("--skip-results", action="store_true",
                    help="跳过不可再生成果包（仅在成果别处已有独立副本时使用）")
    ap.add_argument("--include-heavy", action="store_true",
                    help="追加完整 data/ 快照（含原始影像与 S1 ZIP，体积大）")
    args = ap.parse_args()

    label = args.label or datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = Path(args.dest) / label if args.dest else _default_dest() / label
    dest.mkdir(parents=True, exist_ok=True)

    print(f"项目根：{ROOT}")
    print(f"备份到：{dest}\n")

    # 防止备份落到同一块盘：仅提示，不阻断（用户可能有意为之）
    try:
        same_drive = dest.resolve().drive.upper() == ROOT.resolve().drive.upper()
    except OSError:
        same_drive = False
    if same_drive:
        print("[警告] 备份目标与项目在同一盘符；该盘掉线时备份同时丢失。\n")

    steps = ["git bundle", "工作区快照"]
    if not args.skip_results:
        steps.append("不可再生成果")
    if args.include_heavy:
        steps.append("完整 data")
    total = len(steps)
    idx = 0

    idx += 1
    print(f"{idx}/{total} 生成 git bundle ...")
    bundle = make_bundle(dest)
    print(f"     {bundle['file']}  {bundle['bytes']/1e6:.1f} MB  head={bundle['head_commit'][:8]}")

    idx += 1
    print(f"{idx}/{total} 打包工作区快照（代码/文档/配置 + data 中间件与证据）...")
    worktree = make_worktree(dest)
    print(f"     {worktree['file']}  {worktree['bytes']/1e6:.1f} MB  "
          f"{worktree['entry_count']} 个文件")
    if worktree["skipped_count"]:
        print(f"     跳过 {worktree['skipped_count']} 个大文件/不可读文件（见 manifest）")

    artifacts = [bundle, worktree]

    if not args.skip_results:
        idx += 1
        print(f"{idx}/{total} 打包不可再生成果（模型算例 + 发布包 + 回滚包 + standalone）...")
        print("     这些不能靠重跑恢复：项目禁止未批准重跑 lisflood.exe")
        results = make_results(dest)
        print(f"     {results['file']}  {results['bytes']/1e6:.1f} MB  "
              f"{results['entry_count']} 个文件")
        for rel_root, n in results["per_root"].items():
            print(f"       {rel_root}: {n} 个")
        if results["skipped_count"]:
            print(f"     跳过 {results['skipped_count']} 个不可读文件（见 manifest）")
        artifacts.append(results)

    if args.include_heavy:
        idx += 1
        print(f"{idx}/{total} 追加完整 data/ 快照（耗时较长）...")
        data_full = make_data_full(dest)
        print(f"     {data_full['file']}  {data_full['bytes']/1e6:.1f} MB  "
              f"{data_full['entry_count']} 个文件")
        artifacts.append(data_full)

    total_bytes = sum(int(a.get("bytes") or 0) for a in artifacts)
    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "label": label,
        "project_root": str(ROOT),
        "project_drive": ROOT.resolve().drive,
        "backup_dir": str(dest),
        "include_heavy": bool(args.include_heavy),
        "skip_results": bool(args.skip_results),
        "total_bytes": total_bytes,
        "artifacts": artifacts,
        "note": "校验：uv --directory 2d_hydro_project run python scripts/maintenance/verify_backup.py "
                f"--dir \"{dest}\"",
    }
    if args.skip_results:
        manifest["warning"] = (
            "本批次未包含不可再生成果（outputs/、web_bundle/、_archive_bundles/、standalone/）。"
            "若成果没有独立副本，掉盘后将无法恢复——项目禁止未批准重跑模型。"
        )

    mf = dest / "manifest.json"
    mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n合计 {total_bytes/1e9:.2f} GB，清单：{mf}")
    if args.skip_results:
        print("[警告] 已跳过不可再生成果包（--skip-results）。请确认成果另有副本。")
    print(f"下一步校验：uv --directory {PROJ} run python scripts/maintenance/verify_backup.py --dir \"{dest}\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())