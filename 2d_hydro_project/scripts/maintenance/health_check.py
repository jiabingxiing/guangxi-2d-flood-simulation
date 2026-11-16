"""健康检查：盘可读性 + 备份新旧 + 关键计数自洽（可移植化计划 §2.5）。

与 preflight 的分工：
  - preflight：**换盘/换机后**判断能否直接跑（editable 安装、import、外部资源）。
  - health_check：**日常**巡检数据与备份状态（真实读测试、备份时效、计数一致）。

只读操作，不改文件。退出码 0 = 健康；1 = 有需处理项。

用法：
    uv --directory 2d_hydro_project run python scripts/maintenance/health_check.py
    ... --json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
ROOT = PROJ.parent


def _backup_root() -> Path:
    """备份根目录：env GX_BACKUP_ROOT → 用户目录 Backups（不写死盘符）。"""
    env = os.environ.get("GX_BACKUP_ROOT")
    return Path(env) if env else (Path.home() / "Backups" / "gx-hydro2d")


BACKUP_ROOT = _backup_root()

# 备份超过此天数视为过期（移动硬盘随时可能掉线）
BACKUP_MAX_AGE_DAYS = 7
# 关键文件首次读尝试的字节数
PROBE_BYTES = 4096


class Report:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, level: str, name: str, detail: str, fix: str | None = None) -> None:
        self.items.append({"level": level, "name": name, "detail": detail, "fix": fix})

    def ok(self, n: str, d: str) -> None:
        self.add("ok", n, d)

    def warn(self, n: str, d: str, fix: str | None = None) -> None:
        self.add("warn", n, d, fix)

    def fail(self, n: str, d: str, fix: str | None = None) -> None:
        self.add("fail", n, d, fix)

    @property
    def unhealthy(self) -> bool:
        return any(i["level"] in ("fail", "warn") for i in self.items)

    def render(self) -> str:
        icon = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
        out = []
        for it in self.items:
            out.append(f"[{icon[it['level']]}] {it['name']}: {it['detail']}")
            if it["fix"]:
                out.append(f"        处理：{it['fix']}")
        return "\n".join(out)


def check_drive_readable(rep: Report) -> None:
    """真实读测试。掉盘时 Test-Path/exists() 可能返回 True 但读失败。"""
    probes = [
        PROJ / "models" / "lisflood-fp-8.1" / "LISFLOOD-FP-trunk" / "lisflood.exe",
        PROJ / "data" / "catalog" / "input_manifest.json",
        PROJ / "data" / "catalog" / "mass_recheck_g15.json",
        PROJ / "scripts" / "common" / "paths.py",
    ]
    bad: list[str] = []
    for p in probes:
        try:
            with open(p, "rb") as f:
                f.read(PROBE_BYTES)
        except OSError as exc:
            bad.append(f"{p.name}({type(exc).__name__})")
    if bad:
        rep.fail("盘可读性", f"{len(bad)}/{len(probes)} 个关键文件读取失败：{', '.join(bad)}",
                 "若报『设备硬件出现致命错误』：盘掉线。重插/换 USB 口后重跑本检查，"
                 "不要当成代码问题去修")
    else:
        rep.ok("盘可读性", f"{len(probes)} 个关键文件均可读")


def check_space(rep: Report) -> None:
    try:
        du = shutil.disk_usage(PROJ)
    except OSError as exc:
        rep.warn("磁盘余量", f"无法读取：{exc}")
        return
    free_gb = du.free / 1024**3
    if free_gb < 5:
        rep.fail("磁盘余量", f"仅剩 {free_gb:.1f} GB（门槛 5 GB）",
                 "清理空间；大文件中间件写入会失败")
    else:
        rep.ok("磁盘余量", f"剩余 {free_gb:.1f} GB")


def check_backup(rep: Report) -> None:
    if not BACKUP_ROOT.is_dir():
        rep.warn("备份", f"未找到备份目录 {BACKUP_ROOT}",
                 f'uv --directory "{PROJ}" run python scripts/maintenance/make_backup.py')
        return
    batches = sorted(p for p in BACKUP_ROOT.iterdir()
                     if p.is_dir() and (p / "manifest.json").is_file())
    if not batches:
        rep.warn("备份", f"{BACKUP_ROOT} 下无有效批次",
                 f'uv --directory "{PROJ}" run python scripts/maintenance/make_backup.py')
        return

    latest = batches[-1]
    try:
        doc = json.loads((latest / "manifest.json").read_text(encoding="utf-8"))
        created = datetime.fromisoformat(str(doc.get("created_utc")))
    except (OSError, ValueError):
        rep.warn("备份", f"最新批次 {latest.name} 清单不可解析", "重跑 make_backup.py")
        return

    age_days = (datetime.now(timezone.utc) - created).days
    if age_days > BACKUP_MAX_AGE_DAYS:
        rep.warn("备份时效", f"最新备份已 {age_days} 天（门槛 {BACKUP_MAX_AGE_DAYS} 天）",
                 "移动硬盘随时可能掉线，建议重新备份")
    else:
        rep.ok("备份时效", f"最新备份 {age_days} 天前（{created:%Y-%m-%d %H:%M} UTC）")

    # 成果是否进备份：项目禁止未批准重跑模型，outputs/ 无法用代码重建。
    # 若最新批次缺 results.zip，掉盘后算例与网页包将永久丢失。
    artifacts = {str(a.get("file")) for a in (doc.get("artifacts") or [])}
    if "results.zip" in artifacts:
        results_art = next(a for a in doc["artifacts"] if a.get("file") == "results.zip")
        rep.ok("备份含成果", f"results.zip {int(results_art.get('bytes') or 0)/1e6:.0f} MB，"
                             f"{results_art.get('entry_count')} 个文件（算例/发布包/回滚包）")
    else:
        rep.warn("备份含成果", "最新批次未包含 results.zip（模型算例与发布包）",
                 "项目禁止未批准重跑模型，这些无法用代码重建；"
                 f'执行 uv --directory "{PROJ}" run python '
                 "scripts/maintenance/make_backup.py（不要加 --skip-results）")


def check_counts(rep: Report) -> None:
    """质量门 SSOT 与磁盘上 res.mass 数量是否自洽。"""
    g15 = PROJ / "data" / "catalog" / "mass_recheck_g15.json"
    if not g15.is_file():
        rep.warn("质量门 SSOT", "mass_recheck_g15.json 缺失",
                 f'uv --directory "{PROJ}" run python scripts/stage3/g15_mass_recheck.py')
        return
    try:
        counts = (json.loads(g15.read_text(encoding="utf-8")).get("counts") or {})
    except (OSError, ValueError) as exc:
        rep.fail("质量门 SSOT", f"无法解析：{exc}", "重跑 g15_mass_recheck.py")
        return

    total = sum(int(counts.get(k) or 0) for k in ("PASS", "FAIL-EXPLAINED", "FAIL"))
    rep.ok("质量门 SSOT", f"PASS={counts.get('PASS')} "
                          f"FAIL-EXPLAINED={counts.get('FAIL-EXPLAINED')} "
                          f"FAIL={counts.get('FAIL')} 合计={total} run")
    if int(counts.get("FAIL") or 0) > 0:
        rep.fail("质量门 SSOT", f"{counts.get('FAIL')} 个 run FAIL", "检查对应 run 的质量台账")

    # res.mass 只做计数（磁盘遍历，不读内容）；与 SSOT 口径不同，仅提示差异
    try:
        n_mass = sum(1 for _ in (PROJ / "outputs").rglob("res.mass"))
    except OSError:
        n_mass = -1
    if n_mass < 0:
        rep.warn("res.mass 计数", "遍历 outputs/ 失败", "检查盘可读性")
    elif n_mass < total:
        rep.warn("res.mass 计数", f"磁盘 {n_mass} 个 res.mass < SSOT {total} run",
                 "可能有 run 目录被清理或未迁移；确认是否影响结论")
    else:
        rep.ok("res.mass 计数", f"磁盘 {n_mass} 个（SSOT 计 {total} run；"
                                "差额为无 res.mass 的派生/展示目录，属正常）")


def check_external(rep: Report) -> None:
    sys.path.insert(0, str(PROJ))
    from scripts.common.paths import external  # noqa: PLC0415

    dem = external("DEM_SOURCE")
    if dem is None:
        rep.warn("源 DEM（可选）", "未就位；仅影响重现脚本",
                 f'cmd /c mklink /J "{PROJ / "data" / "raw" / "dem_source"}" "<DEM 目录>"')
    else:
        try:
            with open(dem, "rb") as f:
                f.read(PROBE_BYTES)
            rep.ok("源 DEM（可选）", f"可读 {dem.name}（{dem.stat().st_size/1024**3:.2f} GB）")
        except OSError as exc:
            rep.fail("源 DEM（可选）", f"存在但读失败：{type(exc).__name__}",
                     "Junction 目标盘可能掉线；检查目标盘")


def main() -> int:
    ap = argparse.ArgumentParser(description="项目健康检查（只读）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rep = Report()
    check_drive_readable(rep)
    check_space(rep)
    check_external(rep)
    check_counts(rep)
    check_backup(rep)

    if args.json:
        print(json.dumps({"unhealthy": rep.unhealthy, "items": rep.items},
                         ensure_ascii=False, indent=2))
    else:
        print(f"项目根：{PROJ}\n")
        print(rep.render())
        print()
        print("结论：需要处理上方 WARN/FAIL 项。" if rep.unhealthy else "结论：健康。")
    return 1 if rep.unhealthy else 0


if __name__ == "__main__":
    raise SystemExit(main())