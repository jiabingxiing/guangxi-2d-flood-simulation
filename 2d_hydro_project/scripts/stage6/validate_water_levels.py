"""阶段6：基于官方监测站点的二维水动力连续水面高程与涨水幅度核验（只读）。

用途：
1. 针对指定 run 或三案例代表性历史情景批量提取虚拟水尺水面高程序列 WSE_sim(t)；
2. 对接 `ingest_official_gauges.py` 产出的官方站点实测档案，计算匹配误差、涨水幅度 ΔH、
   洪峰时刻差 ΔT_peak（在 G7 闭合条件下计算 NSE/KGE/RMSE）；
3. 输出 `data/catalog/gauge_validation_results.json`，不覆盖任何 run 目录。

用法：
    uv --directory 2d_hydro_project run python scripts/stage6/validate_water_levels.py --all-cases
    uv --directory 2d_hydro_project run python scripts/stage6/validate_water_levels.py --case guigang --run-dir outputs/guigang/2024-09-yagi/scen_yagi_q11500_100m_dt2p5_chhyd_d14_r2_ga
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from hydro_refine.core.paths import rel_posix, resolve_legacy  # noqa: E402
from hydro_refine.monitors.boundary_assimilation import diagnose_assimilation_adjustment  # noqa: E402
from hydro_refine.monitors.gauge_extractor import extract_run_virtual_gauges  # noqa: E402
from hydro_refine.monitors.hydro_evaluator import evaluate_station_fit  # noqa: E402
from scripts.common.grid_io import AuditLog, utc_now, write_json  # noqa: E402

GAUGE_CATALOG = PROJ / "data" / "catalog" / "gauges"
OUT_JSON = PROJ / "data" / "catalog" / "gauge_validation_results.json"

DEFAULT_REPRESENTATIVE_RUNS: dict[str, list[str]] = {
    "guigang": [
        "outputs/guigang/2024-09-yagi/scen_yagi_q11500_100m_dt2p5_choff_realdem",
        "outputs/guigang/2024-09-yagi/scen_yagi_q11500_100m_dt2p5_chhyd_d14",
        "outputs/guigang/2024-09-yagi/scen_yagi_q11500_100m_dt2p5_chhyd_d14_r2_ga",
        "outputs/guigang/2024-09-yagi/scen_yagi_q11500_100m_dt2p5_chhyd_d14_r4_infil3",
    ],
    "ningming": [
        "outputs/ningming/2026-08-zitan/scen_q4000_100m",
        "outputs/ningming/2026-08-zitan/scen_q6000_n100_100m",
        "outputs/ningming/2026-08-zitan/scen_q6000_n100_100m_choff_eclosed",
    ],
    "jiulong": [
        "outputs/jiulong/2024-09-yagi/scen_free_200m_sv6h",
        "outputs/jiulong/2024-09-yagi/scen_r3_dual_sgc_200m_sv6h",
    ],
}


def validate_case_runs(
    case: str,
    run_dirs: list[Path],
    gauges_json: Path,
    *,
    mode: str = "wet_max",
    half_window: int = 3,
) -> dict[str, Any]:
    if not gauges_json.is_file():
        raise FileNotFoundError(
            f"缺少归一化水位档案 {gauges_json}，请先运行 scripts/stage3/ingest_official_gauges.py --case {case}"
        )
    gdoc = json.loads(gauges_json.read_text(encoding="utf-8"))
    epsg = int(gdoc["crs_epsg"])
    stations = gdoc["stations"]
    st_by_id = {s["station_id"]: s for s in stations}

    run_reports: list[dict[str, Any]] = []
    for run_dir in run_dirs:
        if not run_dir.is_dir():
            run_reports.append({"run_dir": rel_posix(run_dir) if run_dir.is_relative_to(PROJ) else str(run_dir), "available": False})
            continue
        extracted = extract_run_virtual_gauges(
            run_dir, stations, epsg=epsg, half_window=half_window, mode=mode
        )
        if not extracted.get("available"):
            run_reports.append({"run_id": run_dir.name, **extracted})
            continue

        probe_evals: dict[str, Any] = {}
        for key, pr in extracted["probes"].items():
            st = st_by_id[pr["station_id"]]
            valid_obs = [o for o in st.get("observations", []) if o.get("qc_flag", 0) <= 1]
            if not valid_obs:
                probe_evals[key] = {
                    "station_id": st["station_id"],
                    "station_name": st["name"],
                    "probe_kind": pr["probe_kind"],
                    "status": "no_valid_observations",
                    "sim_frames_wet": sum(1 for v in pr["wse_m"] if v is not None),
                }
                continue
            datum_closed = bool((st.get("alignment") or {}).get("datum_closed", False))
            fit = evaluate_station_fit(
                pr["t_h"],
                pr["wse_m"],
                [o["t_h"] for o in valid_obs],
                [o["water_level_m"] for o in valid_obs],
                datum_closed=datum_closed,
                obs_base_m=st.get("baseflow_level_est_m"),
            )
            fit["station_id"] = st["station_id"]
            fit["station_name"] = st["name"]
            fit["probe_kind"] = pr["probe_kind"]
            fit["snap_dist_m"] = pr["snap_dist_m"]
            fit["ground_dem_m"] = pr["ground_dem_m"]
            fit["adjustment_hint"] = diagnose_assimilation_adjustment(fit)
            probe_evals[key] = fit

        run_reports.append({
            "run_id": run_dir.name,
            "run_dir": rel_posix(run_dir) if run_dir.is_relative_to(PROJ) else str(run_dir),
            "available": True,
            "cellsize_m": extracted["cellsize_m"],
            "saveint_s": extracted["saveint_s"],
            "n_frames": extracted["n_frames"],
            "probes": probe_evals,
        })

    return {
        "case": case,
        "event_id": gdoc["event_id"],
        "crs_epsg": epsg,
        "n_runs": len(run_reports),
        "runs": run_reports,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--case", choices=list(DEFAULT_REPRESENTATIVE_RUNS), default="guigang")
    ap.add_argument("--all-cases", action="store_true")
    ap.add_argument("--run-dir", type=Path, default=None)
    ap.add_argument("--gauges", type=Path, default=None)
    ap.add_argument("--mode", choices=("wet_max", "center", "thalweg_wet"), default="wet_max")
    ap.add_argument("--out-json", type=Path, default=OUT_JSON)
    args = ap.parse_args()

    log = AuditLog("validate_water_levels")
    cases = list(DEFAULT_REPRESENTATIVE_RUNS) if args.all_cases else [args.case]
    all_results: dict[str, Any] = {}
    for c in cases:
        gauges_p = args.gauges or (GAUGE_CATALOG / f"{c}_gauges_normalized.json")
        if args.run_dir and not args.all_cases:
            runs = [resolve_legacy(args.run_dir) if not args.run_dir.is_absolute() else args.run_dir]
        else:
            runs = [PROJ / r for r in DEFAULT_REPRESENTATIVE_RUNS[c]]
        res = validate_case_runs(c, runs, gauges_p, mode=args.mode)
        all_results[c] = res
        log.step("validated_case", case=c, n_runs=res["n_runs"])

    payload = {
        "script": "scripts/stage6/validate_water_levels.py",
        "generated_utc": utc_now(),
        "read_only": True,
        "cases": all_results,
    }
    write_json(args.out_json, payload)
    log.write(PROJ / "logs" / "stage6" / "validate_water_levels.json")
    print(f"wrote -> {rel_posix(args.out_json) if args.out_json.is_relative_to(PROJ) else args.out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
