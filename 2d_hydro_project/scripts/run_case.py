"""业务化驱动器（阶段 6 雏形，降级模式）：统一入口调度三案例脚本。

用法（在 2d_hydro_project 目录下，uv 环境）：
  uv run python scripts/run_case.py ningming [--qpeak 4000] [--fric 0.035] [--rain-scale 1.0] [--run-tag tag]
  uv run python scripts/run_case.py jiulong <free|z0|z05|z10|ztide> [--run-tag tag]
  uv run python scripts/run_case.py guigang [--qpeak 11500] [--run-tag tag]

`--run-tag` 通过 RUN_TAG 环境变量传给案例脚本，生成新目录；已有 res.mass 的目录禁止覆盖。

产出：各案例 outputs/<site>/<event>/... 与 verify/summary JSON（见 docs/RUNBOOK.md）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]
STAGE3 = PROJ / "scripts" / "stage3"
STAGE5 = PROJ / "scripts" / "stage5"
PY = sys.executable


def arg(name: str, default: str | None = None) -> list[str]:
    return [f"--{name}", sys.argv[sys.argv.index(f"--{name}") + 1]] if f"--{name}" in sys.argv else (
        [f"--{name}", default] if default else [])


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return
    site = sys.argv[1]
    if site == "ningming":
        cmd = [PY, str(STAGE3 / "ningming_scenario.py"), "all",
               *arg("qpeak", "4000"), *arg("fric", "0.035"), *arg("rain-scale", "1.0"), *arg("res", "100"),
               *arg("channel", "off"), *arg("channel-file", None)]
    elif site == "jiulong":
        scen = sys.argv[2] if len(sys.argv) > 2 else "ztide"
        cmd = [PY, str(STAGE3 / "jiulong_scenarios.py"), "one", scen]
    elif site == "guigang":
        cmd = [PY, str(STAGE5 / "guigang_scenario.py"), "all", *arg("qpeak", "11500")]
    else:
        print(f"未知案例: {site}（ningming|jiulong|guigang）")
        raise SystemExit(2)
    env = os.environ.copy()
    if "--run-tag" in sys.argv:
        i = sys.argv.index("--run-tag")
        if i + 1 >= len(sys.argv):
            raise SystemExit("--run-tag 需要值")
        env["RUN_TAG"] = sys.argv[i + 1]
    print("[run_case]", " ".join(cmd), flush=True)
    raise SystemExit(subprocess.run(cmd, cwd=str(PROJ), env=env).returncode)


if __name__ == "__main__":
    main()
