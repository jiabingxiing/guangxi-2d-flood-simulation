"""换盘/换机后的环境自检（可移植化计划 §2.2 / SC-03）。

用途：判断"项目复制到新位置后能否直接跑"。只读检查，不改任何文件。
发现陈旧 editable 安装时给出**可执行修复指令**，不自动修改。

退出码：0 = 无阻断问题；1 = 有阻断问题（需按提示修复后重跑）。

用法：
    uv --directory 2d_hydro_project run python scripts/maintenance/preflight.py
    uv --directory 2d_hydro_project run python scripts/maintenance/preflight.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
FV = PROJ / "flood_visualization"
VENVS = (("父项目", PROJ), ("floodviz", FV))

# 项目运行所需的最小磁盘余量（GB）。低于此值时大文件中间件会失败。
MIN_FREE_GB = 5.0


class Report:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, level: str, name: str, detail: str, fix: str | None = None) -> None:
        self.items.append({"level": level, "name": name, "detail": detail, "fix": fix})

    def ok(self, name: str, detail: str) -> None:
        self.add("ok", name, detail)

    def warn(self, name: str, detail: str, fix: str | None = None) -> None:
        self.add("warn", name, detail, fix)

    def fail(self, name: str, detail: str, fix: str | None = None) -> None:
        self.add("fail", name, detail, fix)

    @property
    def blocked(self) -> bool:
        return any(i["level"] == "fail" for i in self.items)

    def render(self) -> str:
        icon = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
        lines = []
        for it in self.items:
            lines.append(f"[{icon[it['level']]}] {it['name']}: {it['detail']}")
            if it["fix"]:
                lines.append(f"        修复：{it['fix']}")
        return "\n".join(lines)


def check_paths_alive(rep: Report) -> None:
    """真实读测试：不用 Test-Path（掉盘时它会假阳性），直接读首字节。"""

    probes = [
        PROJ / "pyproject.toml",
        PROJ / "data" / "catalog" / "input_manifest.json",
        PROJ / "models" / "lisflood-fp-8.1" / "LISFLOOD-FP-trunk" / "lisflood.exe",
    ]
    bad = []
    for p in probes:
        try:
            with open(p, "rb") as f:
                f.read(16)
        except OSError as exc:
            bad.append(f"{p.name}({type(exc).__name__})")
    if bad:
        rep.fail("关键文件可读性", f"{len(bad)} 个文件读取失败：{', '.join(bad)}",
                 "若报『设备硬件出现致命错误』，先查盘是否掉线（Get-Disk），"
                 "重插或换口后再跑，不要当作代码问题")
    else:
        rep.ok("关键文件可读性", f"{len(probes)} 个关键文件均可读")


def check_editable(rep: Report) -> None:
    """扫描 venv 的 editable 安装记录，路径不在当前项目下即为陈旧。

    2026-11-16 实测（重要）：
    - `uv run` **不会**修复陈旧的 .pth（它认为包已装好）；
    - `uv sync` 也**不会**（同一原因，包已在正确版本上）；
    - 唯一可靠修复是显式 `sync --reinstall-package <name>`。
    """
    fix_tpl = 'uv --directory "{root}" sync --reinstall-package {pkg}'
    for label, root in VENVS:
        venv = root / ".venv"
        pkg = "gx-hydro2d" if root == PROJ else "floodviz"
        if not venv.is_dir():
            rep.warn(f"{label} .venv", "不存在（首次使用需 uv sync，属正常）",
                     f'uv --directory "{root}" sync')
            continue
        stale = []
        for p in (venv / "Lib" / "site-packages").glob("*.pth"):
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for line in text.splitlines():
                s = line.strip()
                if not s or s.startswith(("import ", "#")):
                    continue
                cand = Path(s)
                if not cand.is_absolute():
                    continue
                try:
                    cand.relative_to(root)
                except ValueError:
                    stale.append(f"{p.name} -> {s}")
        if stale:
            rep.fail(f"{label} editable 安装",
                     f"{len(stale)} 处指向项目外（换位置后未重装）：" + "; ".join(stale[:3]),
                     fix_tpl.format(root=root, pkg=pkg))
        else:
            rep.ok(f"{label} editable 安装", "路径均指向当前项目")


def check_imports(rep: Report) -> None:
    """真实 import 探针：不光看导入成功，还要看**导入自哪个位置**。

    2026-11-16 演练实测的陷阱：项目复制到新盘后，`.pth` 仍指向原盘，
    `import acquisition` 会成功——但读的是**原盘的旧副本**。
    若只判 returncode，会误报"迁移成功"。因此这里比对 `__file__` 是否
    落在当前项目根内。
    """
    py = PROJ / ".venv" / "Scripts" / "python.exe"
    if not py.is_file():
        rep.warn("父项目 import", "无 .venv python，跳过",
                 f'uv --directory "{PROJ}" sync')
        return
    probe = ("import acquisition, model, sys;"
             "print(acquisition.__file__);print(model.__file__)")
    r = subprocess.run([str(py), "-B", "-c", probe],
                       capture_output=True, text=True, cwd=str(PROJ))
    if r.returncode != 0:
        rep.fail("父项目 import", r.stderr.strip().splitlines()[-1] if r.stderr else "失败",
                 f'uv --directory "{PROJ}" sync --reinstall-package gx-hydro2d')
        return
    outside = []
    for line in r.stdout.strip().splitlines():
        p = Path(line.strip())
        if not p.is_absolute():
            continue
        try:
            p.relative_to(PROJ)
        except ValueError:
            outside.append(line.strip())
    if outside:
        rep.fail("父项目 import", "导入自项目之外（静默读了别处的副本）：" + "; ".join(outside[:2]),
                 f'uv --directory "{PROJ}" sync --reinstall-package gx-hydro2d')
    else:
        rep.ok("父项目 import", "acquisition / model 导入自当前项目")

    pyf = FV / ".venv" / "Scripts" / "python.exe"
    if not pyf.is_file():
        rep.warn("floodviz import", "无 .venv python，跳过",
                 f'uv --directory "{FV}" sync')
        return
    r = subprocess.run([str(pyf), "-B", "-c", "import floodviz;print(floodviz.__file__)"],
                       capture_output=True, text=True, cwd=str(FV))
    if r.returncode != 0:
        rep.fail("floodviz import", r.stderr.strip().splitlines()[-1] if r.stderr else "失败",
                 f'uv --directory "{FV}" sync --reinstall-package floodviz')
        return
    line = (r.stdout.strip().splitlines() or [""])[-1]
    p = Path(line)
    if p.is_absolute():
        try:
            p.relative_to(FV)
        except ValueError:
            rep.fail("floodviz import", f"导入自项目之外：{line}",
                     f'uv --directory "{FV}" sync --reinstall-package floodviz')
            return
    rep.ok("floodviz import", "floodviz 导入自当前项目")


def check_space(rep: Report) -> None:
    target = PROJ
    while not target.exists() and target != target.parent:
        target = target.parent
    du = shutil.disk_usage(target)
    free_gb = du.free / 1024**3
    if free_gb < MIN_FREE_GB:
        rep.fail("磁盘余量", f"{target.drive} 仅剩 {free_gb:.1f} GB（门槛 {MIN_FREE_GB} GB）",
                 "清理空间或迁移到更大容量的盘")
    else:
        rep.ok("磁盘余量", f"{target.drive} 剩余 {free_gb:.1f} GB")


def check_external(rep: Report) -> None:
    sys.path.insert(0, str(PROJ))
    from scripts.common.paths import external  # noqa: PLC0415

    dem = external("DEM_SOURCE")
    if dem is None:
        rep.warn("源 DEM（可选）", "未就位；仅影响重现脚本，不影响既有成果",
                 'cmd /c mklink /J "'
                 + str(PROJ / "data" / "raw" / "dem_source")
                 + '" "<含 广西.tif 的目录>"')
    else:
        size_gb = dem.stat().st_size / 1024**3
        rep.ok("源 DEM（可选）", f"{dem}（{size_gb:.2f} GB）")

    opt = external("OPTICAL_ROOT")
    if opt is None:
        rep.warn("光学影像根（可选）", "未就位；光学层会降级为 missing",
                 "设置 GX_OPTICAL_ROOT 或放入 data/raw/optical")
    else:
        rep.ok("光学影像根（可选）", str(opt))


def check_quality_gate(rep: Report) -> None:
    g15 = PROJ / "data" / "catalog" / "mass_recheck_g15.json"
    if not g15.is_file():
        rep.warn("质量门 SSOT", "mass_recheck_g15.json 缺失",
                 "uv --directory \"%s\" run python scripts/stage3/g15_mass_recheck.py" % PROJ)
        return
    try:
        doc = json.loads(g15.read_text(encoding="utf-8"))
        counts = doc.get("counts") or {}
        total = sum(int(counts.get(k) or 0) for k in ("PASS", "FAIL-EXPLAINED", "FAIL"))
        rep.ok("质量门 SSOT", f"PASS={counts.get('PASS')} "
                             f"FAIL-EXPLAINED={counts.get('FAIL-EXPLAINED')} "
                             f"FAIL={counts.get('FAIL')} 合计={total}")
        if int(counts.get("FAIL") or 0) > 0:
            rep.fail("质量门 SSOT", f"{counts.get('FAIL')} 个 run 质量门 FAIL",
                     "检查对应 run 的 results/res.mass 与台账")
    except (OSError, ValueError) as exc:
        rep.fail("质量门 SSOT", f"无法解析：{exc}", "重跑 g15_mass_recheck.py")


def check_uv(rep: Report) -> None:
    if shutil.which("uv"):
        rep.ok("uv", "在 PATH 上")
    else:
        rep.fail("uv", "未找到 uv",
                 "安装 uv 或在 PATH 中加入；本机镜像已配 %APPDATA%\\uv\\uv.toml")

    if os.environ.get("PYTHONUTF8") == "1":
        rep.ok("PYTHONUTF8", "=1")
    else:
        rep.warn("PYTHONUTF8", "未设为 1；中文控制台可能出现 cp936 编码错误",
                 "$env:PYTHONUTF8 = '1'")


def main() -> int:
    ap = argparse.ArgumentParser(description="换盘/换机后环境自检（只读）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    rep = Report()
    check_paths_alive(rep)
    check_editable(rep)
    check_imports(rep)
    check_space(rep)
    check_external(rep)
    check_quality_gate(rep)
    check_uv(rep)

    if args.json:
        print(json.dumps({"blocked": rep.blocked, "items": rep.items},
                         ensure_ascii=False, indent=2))
    else:
        print(f"项目根：{PROJ}\n")
        print(rep.render())
        print()
        if rep.blocked:
            print("结论：有阻断问题，请按上方『修复』处理。")
        else:
            print("结论：无阻断问题，可直接运行。")
    return 1 if rep.blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())