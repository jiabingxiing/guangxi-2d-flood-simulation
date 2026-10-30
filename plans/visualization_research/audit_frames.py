"""Read selected run metadata and six-line ASCII headers only."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROJ = ROOT / "2d_hydro_project"
RUNS = {
    "jiulong_default": "outputs/jiulong/2024-09-yagi/scen_free_200m_sv6h",
    "jiulong_extended": "outputs/jiulong/2024-09-yagi/scen_free_200m_sv6h_recession",
    "ningming_reference": "outputs/ningming/2026-08-zitan/scen_q6000_n100_100m",
    "ningming_baseline": "outputs/ningming/2026-08-zitan/scen_q4000_100m",
    "guigang_default": "outputs/guigang/2024-09-yagi/scen_yagi_q11500_30m_b5",
    "guigang_legacy": "outputs/guigang/2024-09-yagi/scen_yagi_q11500_200m",
}


def main() -> None:
    result = {"checked_utc": datetime.now(timezone.utc).isoformat(), "scope": "file inventory, all selected headers and parameters; not full cell-value validation", "runs": {}}
    for key, relative in RUNS.items():
        run = PROJ / relative
        pars = dict(line.split(None, 1) for line in (run / "run.par").read_text().splitlines() if len(line.split(None, 1)) == 2)
        frames = sorted((int(p.stem.split("-")[1]), p) for p in (run / "results").glob("res-*.wd") if re.fullmatch(r"res-\d+\.wd", p.name))
        headers = []
        for _, path in frames:
            with path.open() as f:
                headers.append([next(f).strip() for _ in range(6)])
        expected = int(float(pars["sim_time"]) // float(pars["saveint"])) + 1
        entry = {"path": "2d_hydro_project/" + relative, "count": len(frames),
                 "first": frames[0][1].name, "last": frames[-1][1].name,
                 "saveint_s": float(pars["saveint"]), "sim_time_s": float(pars["sim_time"]),
                 "expected_count_under_zero_origin_convention": expected,
                 "indices_contiguous": [i for i, _ in frames] == list(range(expected)),
                 "headers_identical": all(h == headers[0] for h in headers),
                 "header": headers[0], "total_wd_bytes": sum(p.stat().st_size for _, p in frames),
                 "meta": json.loads((run / "meta.json").read_text(encoding="utf-8"))}
        result["runs"][key] = entry
        print(key, len(frames), expected, entry["headers_identical"], entry["indices_contiguous"], flush=True)
    result["mass_counts"] = json.loads((PROJ / "data/catalog/mass_recheck_g15.json").read_text(encoding="utf-8"))["counts"]
    (Path(__file__).parent / "frame_audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
