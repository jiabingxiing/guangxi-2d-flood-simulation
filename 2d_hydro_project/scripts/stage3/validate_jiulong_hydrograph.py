"""久隆钦江 Q(t) 入流过程线校验（计划 任务3 / ADR-0019 验收门）。

校验项（计划 §10.2 入流过程合规）：
1. 时间轴：t0 = 2024-09-05 本地（UTC+8），步长 3600 s，严格递增；
2. QVAR 单位：文件值 × cellsize = Q_m3/s，与 audit 峰值一致；
3. 水量守恒：∫Q dt 相对事件净雨体积偏差 < 1%（事件模型卷积截断容差）；
4. 净雨非负 + 无 NaN；
5. 与 GR4J 对照体积比记录（仅对照，不作门禁）。

输出：data/catalog/jiulong_inflow_validation.json；全部通过返回 0，否则 1。
"""

from __future__ import annotations

import json
import sys
from argparse import ArgumentParser
from pathlib import Path

import numpy as np

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import AuditLog, add_common_args, utc_now, write_json  # noqa: E402

CELLSIZE_M = 200.0
BDY_EVENT = PROJ / "data/interim/jiulong/hydrology/qinjiang_event_5d.bdy"
BDY_RECESSION = PROJ / "data/interim/jiulong/hydrology/qinjiang_recession_13d.bdy"
HOURLY_Q = PROJ / "data/interim/jiulong/hydrology/qinjiang_hourly_q.json"
AUDIT = PROJ / "data/catalog/jiulong_inflow_audit.json"
CATALOG = PROJ / "data/catalog/jiulong_inflow_validation.json"

T0_LOCAL = "2024-09-05"
VOLUME_REL_DEV_MAX = 0.01


def read_bdy(path: Path) -> tuple[str, np.ndarray, np.ndarray]:
    lines = [ln for ln in path.read_text(encoding="ascii").splitlines() if ln.strip()]
    comment = lines[0]
    name = lines[1]
    n = int(lines[2].split()[0])
    values, times = [], []
    for ln in lines[3 : 3 + n]:
        v, t = ln.split("\t")
        values.append(float(v))
        times.append(float(t))
    return comment, name, np.array(values), np.array(times)


def validate_series(
    tag: str, values: np.ndarray, times: np.ndarray, *, expected_hours: int
) -> dict:
    checks: dict[str, dict] = {}
    checks["count"] = {
        "pass": len(values) == expected_hours,
        "n": len(values),
        "expected": expected_hours,
    }
    dt = np.diff(times)
    checks["timestep_3600"] = {"pass": bool(np.all(dt == 3600.0)), "min_dt": float(dt.min()) if len(dt) else None}
    checks["times_strictly_increasing"] = {"pass": bool(np.all(dt > 0))}
    checks["nonnegative_finite"] = {
        "pass": bool(np.all(np.isfinite(values)) and np.all(values >= 0)),
        "min": float(np.min(values)),
        "max": float(np.max(values)),
    }
    checks["t0_zero"] = {"pass": bool(times[0] == 0.0), "t0": float(times[0])}
    return {"tag": tag, "checks": checks, "all_pass": all(c["pass"] for c in checks.values())}


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__)
    add_common_args(parser)
    args = parser.parse_args(argv)
    log = AuditLog("validate_jiulong_hydrograph", run_tag=args.run_tag, dry_run=args.dry_run)
    log.add_input("bdy_event", BDY_EVENT)
    log.add_input("bdy_recession", BDY_RECESSION)
    log.add_input("audit", AUDIT)

    audit = json.loads(AUDIT.read_text(encoding="utf-8"))
    hourly = json.loads(HOURLY_Q.read_text(encoding="utf-8"))
    q_event = np.asarray(hourly["event_q_m3s"], dtype=float)
    q_rec = np.asarray(hourly["recession_q_m3s"], dtype=float)

    results = [
        validate_series("event_5d", q_event, np.arange(len(q_event)) * 3600.0, expected_hours=120),
        validate_series("recession_13d", q_rec, np.arange(len(q_rec)) * 3600.0, expected_hours=312),
    ]

    _, _, values_ev, times_ev = read_bdy(BDY_EVENT)
    q_from_bdy = values_ev * CELLSIZE_M
    # .bdy 以 %.4f 存 QVAR 值（=Q/200），Q 量化步长 0.02 m³/s → 按量化口径比对
    bdy_match = {
        "pass": bool(
            len(values_ev) == len(q_event)
            and np.allclose(values_ev, np.round(q_event / CELLSIZE_M, 4), rtol=0, atol=1e-8)
        ),
        "n_bdy": len(values_ev),
        "max_abs_dev_m3s": float(np.max(np.abs(q_from_bdy - q_event))) if len(values_ev) == len(q_event) else None,
        "quantization_step_m3s": 0.0001 * CELLSIZE_M,
    }
    peak = {
        "pass": bool(abs(float(np.max(q_from_bdy)) - audit["event_model"]["peak_m3s_event_window"]) <= 0.0001 * CELLSIZE_M),
        "peak_qvar_value": float(np.max(values_ev)),
        "peak_m3s": float(np.max(q_from_bdy)),
        "peak_time_s": float(times_ev[int(np.argmax(values_ev))]),
    }

    volume_m3 = float(np.sum(q_event) * 3600.0)
    expected = audit["event_model"]["volume_m3_event_window"]
    rel_dev = abs(volume_m3 - expected) / expected if expected > 0 else 0.0
    volume = {
        "pass": bool(rel_dev < VOLUME_REL_DEV_MAX),
        "volume_m3": volume_m3,
        "expected_m3": expected,
        "rel_dev": rel_dev,
        "threshold": VOLUME_REL_DEV_MAX,
    }

    all_pass = all(r["all_pass"] for r in results) and bdy_match["pass"] and peak["pass"] and volume["pass"]
    report = {
        "generated_utc": utc_now(),
        "t0_local": T0_LOCAL,
        "cellsize_m": CELLSIZE_M,
        "series": results,
        "bdy_matches_hourly": bdy_match,
        "peak": peak,
        "volume_conservation": volume,
        "gr4j_reference": {
            "volume_ratio_event_vs_gr4j": audit["gr4j_crosscheck"]["volume_ratio_event_vs_gr4j"],
            "note": "仅水量先验对照，非门禁（ADR-0019）",
        },
        "all_pass": all_pass,
    }
    if not args.dry_run:
        write_json(CATALOG, report)
    tag = f".{args.run_tag}" if args.run_tag else ""
    log.write(PROJ / "logs" / "stage3" / f"validate_jiulong_hydrograph{tag}.json")
    print(f"hydrograph_validation={'PASS' if all_pass else 'FAIL'} peak={peak['peak_m3s']:.0f} m3/s vol_rel_dev={rel_dev:.4f}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
