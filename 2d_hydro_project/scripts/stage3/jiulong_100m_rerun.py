"""Phase E 100 m 完整物理重跑调度器（用户 2026-10-31 批准）。

授权范围（用户明确选择）：
  - 时间窗：5 天事件窗（09-05T00→09-10T00）与 13 天含退水段（→09-18T00）都跑
  - 边界：同时试 N/W/E 闭合、只留南缘 FREE 的候选处置

四个 run（全部 100 m，dt=2.5，saveint=21600，fpfric=0.035）：
  1. scen_free_100m_dt2p5_sv6h_r5d_bdallfree     5 天窗 + 四边 FREE（对照现有 100 m run）
  2. scen_free_100m_dt2p5_sv6h_r5d_bdsouthonly   5 天窗 + 三边闭合
  3. scen_free_100m_dt2p5_sv13d_bdallfree        13 天窗 + 四边 FREE（对照 200 m 退水基线）
  4. scen_free_100m_dt2p5_sv13d_bdsouthonly      13 天窗 + 三边闭合

硬约束：
  - 不覆盖任何已有 res.mass（prepare_run_dir / guard_no_overwrite）
  - 不改 models/ 源码，只调用既有 lisflood.exe
  - 不写任何已有 run 目录

用法：
  uv --directory 2d_hydro_project run python scripts/stage3/jiulong_100m_rerun.py --dry-run
  uv --directory 2d_hydro_project run python scripts/stage3/jiulong_100m_rerun.py --run-tag e100m-v1
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
SCEN = PROJ / "scripts" / "stage3" / "jiulong_scenarios.py"
OUT = PROJ / "data" / "catalog" / "jiulong_100m_rerun.json"

# (run_tag_suffix, sim_days, boundary_mode)
JOBS = (
    ("r5d_allfree", 5.0, "allfree"),
    ("r5d_southonly", 5.0, "southonly"),
    ("r13d_allfree", 13.0, "allfree"),
    ("r13d_southonly", 13.0, "southonly"),
)
RES = "100"
DT = "2.5"
SAVEINT = "21600"


def _dir_for(tag: str, boundary_mode: str) -> Path:
    """用 jiulong_scenarios.run_dir 推导目标目录，避免手写命名与实现漂移。

    通过子进程把参数传给脚本后导入其 run_dir，保证唯一真源。
    """
    import importlib.util
    import os

    old_argv = sys.argv
    old_mode = os.environ.get("RUN_TAG")
    try:
        sys.argv = ["jiulong_scenarios.py", "prep", "free",
                    "--res", RES, "--dt", DT, "--saveint", SAVEINT,
                    "--boundary-mode", boundary_mode]
        os.environ["RUN_TAG"] = tag
        spec = importlib.util.spec_from_file_location(
            f"_js_{tag}_{boundary_mode}", SCEN)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.run_dir("free")
    finally:
        sys.argv = old_argv
        if old_mode is None:
            os.environ.pop("RUN_TAG", None)
        else:
            os.environ["RUN_TAG"] = old_mode


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    run_tag = None
    if "--run-tag" in sys.argv:
        run_tag = sys.argv[sys.argv.index("--run-tag") + 1]

    plan = []
    for tag, days, bmode in JOBS:
        target = _dir_for(tag, bmode)
        has_mass = (target / "results" / "res.mass").is_file()
        plan.append({
            "tag": tag,
            "sim_days": days,
            "boundary_mode": bmode,
            "target_dir": target.relative_to(PROJ).as_posix(),
            "already_has_res_mass": has_mass,
            "skip": has_mass,
        })

    print(json.dumps({"dry_run": dry_run, "run_tag": run_tag, "plan": plan},
                     ensure_ascii=False, indent=2))
    if dry_run:
        return 0
    if run_tag is None:
        print("必须给 --run-tag（避免与既有 run 混淆）", file=sys.stderr)
        return 2

    results = {}
    for item in plan:
        tag = item["tag"]
        if item["skip"]:
            results[tag] = {"status": "skipped_existing_res_mass",
                            "target_dir": item["target_dir"]}
            print(f"[skip] {tag}: 目标已有 res.mass，不覆盖", flush=True)
            continue
        env = {"RUN_TAG": tag, **{k: v for k, v in __import__("os").environ.items()}}
        cmd_prep = [sys.executable, str(SCEN), "prep", "free",
                    "--res", RES, "--dt", DT, "--sim-days", str(item["sim_days"]),
                    "--saveint", SAVEINT, "--boundary-mode", item["boundary_mode"]]
        # run/verify 也依赖模块级 BOUNDARY_MODE 推导 run_dir，
        # 必须传同一组 --res/--dt/--saveint/--boundary-mode，否则路径会漂移。
        common = ["--res", RES, "--dt", DT,
                  "--sim-days", str(item["sim_days"]),
                  "--saveint", SAVEINT,
                  "--boundary-mode", item["boundary_mode"]]
        cmd_run = [sys.executable, str(SCEN), "run", "free", *common]
        cmd_verify = [sys.executable, str(SCEN), "verify", "free", *common]
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        t0 = time.monotonic()
        rec = {"status": "running", "started_utc": started,
               "target_dir": item["target_dir"]}
        for label, cmd in (("prep", cmd_prep), ("run", cmd_run), ("verify", cmd_verify)):
            proc = subprocess.run(cmd, cwd=str(PROJ), env=env,
                                  capture_output=True, text=True)
            rec[label] = {"exit": proc.returncode,
                          "tail": (proc.stdout or "")[-800:]}
            if proc.returncode != 0:
                rec["status"] = f"failed_at_{label}"
                rec[label]["stderr_tail"] = (proc.stderr or "")[-800:]
                break
        rec["wall_s"] = round(time.monotonic() - t0, 1)
        if rec["status"] == "running":
            rec["status"] = "completed"
        results[tag] = rec
        print(f"[{rec['status']}] {tag} wall={rec['wall_s']}s", flush=True)
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(
            {"generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "run_tag": run_tag, "authorization": "用户 2026-10-31 批准 100 m 完整物理重跑",
             "runs": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"\n-> {OUT}")
    return 0 if all(r.get("status") in ("completed", "skipped_existing_res_mass")
                    for r in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
