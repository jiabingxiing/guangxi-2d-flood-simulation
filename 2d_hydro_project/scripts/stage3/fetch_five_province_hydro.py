"""阶段3：南方五省区（粤、桂、黔、滇、琼）公开水情发布信息采集与 GloFAS 连续流量归档。

用法（先设 $env:PYTHONUTF8 = "1"）：
    uv --directory 2d_hydro_project run python scripts/stage3/fetch_five_province_hydro.py
    uv --directory 2d_hydro_project run python scripts/stage3/fetch_five_province_hydro.py --offline
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from hydro_refine.core.paths import rel_posix  # noqa: E402
from hydro_refine.monitors.five_provinces_harvester import harvest_five_provinces  # noqa: E402
from scripts.common.grid_io import AuditLog, utc_now, write_json  # noqa: E402

PORTAL_CFG = PROJ / "configs" / "gauges" / "five_provinces_portals.yaml"
DEFAULT_OUT = PROJ / "data" / "catalog" / "five_provinces_hydro_snapshot.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, default=PORTAL_CFG)
    ap.add_argument("--out-json", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--offline", action="store_true", help="离线演练模式，不发外部 HTTP 请求")
    ap.add_argument("--glofas-start", default="2024-04-01")
    ap.add_argument("--glofas-end", default="2024-09-25")
    args = ap.parse_args()

    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    log = AuditLog("fetch_five_province_hydro")
    res = harvest_five_provinces(
        cfg,
        live_http=not args.offline,
        fetch_glofas=not args.offline,
        glofas_start=args.glofas_start,
        glofas_end=args.glofas_end,
    )
    payload = {
        "script": "scripts/stage3/fetch_five_province_hydro.py",
        "generated_utc": utc_now(),
        "config": rel_posix(args.config) if args.config.is_relative_to(PROJ) else str(args.config),
        "benchmark_stations": cfg.get("benchmark_stations", []),
        **res,
    }
    write_json(args.out_json, payload)
    ok_portals = sum(1 for p in res["portal_probe_summary"] if p["probe_status"] == "ok")
    total_portals = len(res["portal_probe_summary"])
    log.step("harvested", ok_portals=ok_portals, total_portals=total_portals)
    log.write(PROJ / "logs" / "stage3" / "fetch_five_province_hydro.json")

    hi_n = len((res.get("hainan_live_daily") or {}).get("records", []))
    hi_th = (res.get("hainan_threshold_catalog") or {}).get("n_stations_cataloged", 0)
    gx_n = len(res.get("guangxi_emergency_responses") or [])
    glofas_ok = sum(1 for v in (res.get("glofas_five_province_discharge") or {}).values() if v.get("status") == "ok")
    print(
        f"[5-provinces] portals_ok={ok_portals}/{total_portals}, "
        f"hainan_live={hi_n} (catalog_thresholds={hi_th}), "
        f"guangxi_responses={gx_n}, glofas_stations_ok={glofas_ok} -> {rel_posix(args.out_json)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
