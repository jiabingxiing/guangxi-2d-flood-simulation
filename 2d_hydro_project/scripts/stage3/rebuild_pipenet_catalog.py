"""从现有 run 目录重建 `data/catalog/pipenet_coupled_rerun.json`（补回被覆盖的案例）。

编排脚本原先每次运行都会整体覆盖 catalog，导致 `--case ningming` 抹掉 jiulong 记录。
合并写入已修（`merged_from_prior`），本脚本用于补齐历史缺口：扫描
`outputs/pipenet/<case>/run_{baseline,coupled}_*/`，按目录实际内容重算 E_mass 与
forcing 摘要，重写 catalog。**不重跑求解器**，只读现有产物。

用法：
    $env:PYTHONUTF8 = "1"
    uv --directory 2d_hydro_project run python scripts/stage3/rebuild_pipenet_catalog.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from hydro_refine.core.paths import rel_posix  # noqa: E402
from scripts.common.grid_io import utc_now, write_json  # noqa: E402
from scripts.stage3.run_pipenet_coupled_lisflood import (  # noqa: E402
    CASES,
    compute_e_mass,
)

OUT_JSON = PROJ / "data" / "catalog" / "pipenet_coupled_rerun.json"


def _read_forcing(case: str) -> dict[str, Any]:
    """从 run_coupled_* 的 meta/bci 复原 forcing 摘要（不重算几何）。"""
    case_root = PROJ / "outputs" / "pipenet" / case
    bci = case_root / "lisflood_coupled_inputs" / "drainage_points.bci"
    bdy = case_root / "lisflood_coupled_inputs" / "drainage_points.bdy"
    if not bci.is_file():
        return {}
    lines = [ln for ln in bci.read_text(encoding="utf-8", errors="replace").splitlines()
             if "QVAR" in ln]
    names = [ln.split()[-1] for ln in lines]
    sinks = [n for n in names if n.startswith(("dr_bin", "dr_jl"))]
    outs = [n for n in names if n.startswith("dr_out")]
    return {
        "n_sinks": len(sinks),
        "n_outfall_sources": len(outs),
        "n_unique_sink_series": len(set(sinks)),
        "unique_series_per_cell": bool(sinks) and all(n.startswith("dr_jl") for n in sinks),
        "bci_points": len(lines),
        "bdy_bytes": bdy.stat().st_size if bdy.is_file() else 0,
        "note": "由 rebuild_pipenet_catalog.py 从 drainage_points.bci 复原的摘要",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-json", type=Path, default=OUT_JSON)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    prior: dict[str, Any] = {}
    if args.out_json.is_file():
        try:
            prior = json.loads(args.out_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            prior = {}

    cases: dict[str, Any] = dict(prior.get("cases") or {})
    for case, cfg in CASES.items():
        case_root = PROJ / "outputs" / "pipenet" / case
        if not case_root.is_dir():
            continue
        runs: dict[str, Any] = {}
        for role in ("baseline", "coupled"):
            cands = sorted(case_root.glob(f"run_{role}_*"))
            cands = [c for c in cands if (c / "results" / "res.mass").is_file()]
            if not cands:
                runs = {}
                break
            run_dir = cands[-1]
            rec: dict[str, Any] = {
                "run_dir": rel_posix(run_dir),
                "coupled": role == "coupled",
                "mass": compute_e_mass(run_dir),
                "mass_gate": None,
                "wall_s": None,
            }
            rec["mass_gate"] = (
                "PASS" if rec["mass"]["e_mass_132"] <= 1e-3 else "FAIL"
            )
            log = run_dir / "run.log"
            if log.is_file():
                for ln in log.read_text(encoding="utf-8", errors="replace").splitlines():
                    if "loop time" in ln:
                        try:
                            rec["wall_s"] = float(ln.split()[-1])
                        except ValueError:
                            pass
            runs[role] = rec
        if len(runs) != 2:
            continue

        prev = (prior.get("cases") or {}).get(case) or {}
        cases[case] = {
            "case": case,
            "SYNTHETIC_DATA_NOT_REAL_ASSET": True,
            "physical_recalculation": True,
            "baseline_run": rel_posix(cfg["baseline_run"]),
            "run_tag": "c100m" if case != "jiulong" else "c200m",
            "note": cfg["note"],
            "dry_run": False,
            "executed": True,
            "failed": any(r["mass_gate"] != "PASS" for r in runs.values()),
            "forcing": prev.get("forcing") or _read_forcing(case),
            "runs": runs,
            "rebuilt_from_disk": True,
        }
        print(
            f"[{case}] baseline E={runs['baseline']['mass']['e_mass_132']} "
            f"({runs['baseline']['mass_gate']}) / coupled E={runs['coupled']['mass']['e_mass_132']} "
            f"({runs['coupled']['mass_gate']})"
        )

    payload = {
        "script": "scripts/stage3/run_pipenet_coupled_lisflood.py",
        "rebuilt_by": "scripts/stage3/rebuild_pipenet_catalog.py",
        "generated_utc": utc_now(),
        "run_tag": None,
        "dry_run": False,
        "safety_factor": prior.get("safety_factor", 0.85),
        "n_capacity_bins": prior.get("n_capacity_bins", 10),
        "SYNTHETIC_DATA_NOT_REAL_ASSET": True,
        "isolated_from_g15_ssot": True,
        "cases": {c: cases[c] for c in sorted(cases)},
    }
    if args.dry_run:
        print("dry-run: 未写 catalog")
        return 0
    write_json(args.out_json, payload)
    print(f"wrote -> {rel_posix(args.out_json)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())