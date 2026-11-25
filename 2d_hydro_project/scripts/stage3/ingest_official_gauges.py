"""阶段3：官方水位监测站数据接入、清洗、插值与基准对齐脚本。

用法（先设 $env:PYTHONUTF8 = "1"）：
    uv --directory 2d_hydro_project run python scripts/stage3/ingest_official_gauges.py --case guigang
    uv --directory 2d_hydro_project run python scripts/stage3/ingest_official_gauges.py --all
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from hydro_refine.core.paths import rel_posix  # noqa: E402
from hydro_refine.monitors.gauge_ingestor import (  # noqa: E402
    ingest_case_gauges,
    interpolate_hourly_series,
)
from scripts.common.grid_io import AuditLog, utc_now, write_json  # noqa: E402

GAUGE_CONFIGS = {
    "guigang": PROJ / "configs" / "gauges" / "guigang_stations.yaml",
    "ningming": PROJ / "configs" / "gauges" / "ningming_stations.yaml",
    "jiulong": PROJ / "configs" / "gauges" / "jiulong_stations.yaml",
}
ATL13_PROBE = PROJ / "data" / "catalog" / "gx_datum_probe.json"
OUT_DIR = PROJ / "data" / "catalog" / "gauges"


def run_case(
    case: str,
    *,
    extra_csv: Path | None = None,
    probe_url: str | None = None,
    interpolate_gap_h: float = 0.0,
) -> Path:
    cfg_p = GAUGE_CONFIGS[case]
    res = ingest_case_gauges(
        cfg_p,
        extra_csv=extra_csv,
        atl13_probe_json=ATL13_PROBE,
        probe_url=probe_url,
    )
    if interpolate_gap_h > 0:
        for st in res.stations:
            st.observations = interpolate_hourly_series(
                st.observations, max_gap_h=interpolate_gap_h
            )

    out_json = OUT_DIR / f"{case}_gauges_normalized.json"
    payload = {
        "script": "scripts/stage3/ingest_official_gauges.py",
        "generated_utc": utc_now(),
        **res.to_dict(),
    }
    write_json(out_json, payload)
    print(
        f"[{case}] stations={len(res.stations)} "
        f"obs={sum(len(s.observations) for s in res.stations)} -> {rel_posix(out_json)}"
    )
    return out_json


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--case", choices=list(GAUGE_CONFIGS), default="guigang")
    ap.add_argument("--all", action="store_true", help="接入全部三案例站点配置")
    ap.add_argument("--extra-csv", type=Path, default=None, help="附加官方水位 CSV 归档")
    ap.add_argument("--probe-url", default=None, help="公开无加密水情网页/JSON 探测地址（可选）")
    ap.add_argument("--interpolate-gap-h", type=float, default=0.0, help="对缺测间隔 <= 阈值补齐整点插值")
    args = ap.parse_args()

    log = AuditLog("ingest_official_gauges")
    cases = list(GAUGE_CONFIGS) if args.all else [args.case]
    for c in cases:
        out_p = run_case(
            c,
            extra_csv=args.extra_csv,
            probe_url=args.probe_url,
            interpolate_gap_h=args.interpolate_gap_h,
        )
        log.step("ingested", case=c, out=rel_posix(out_p))
    log.write(PROJ / "logs" / "stage3" / "ingest_official_gauges.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
