"""Assess candidate optical coverage with small SCL rasters, not full RGB scenes."""
from __future__ import annotations

import json
import math
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.io import MemoryFile
from rasterio.warp import transform_bounds, transform_geom
from rasterio.windows import Window, from_bounds

BASE = Path(__file__).resolve().parent
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def main() -> None:
    evidence = json.loads((BASE / "optical_search_evidence.json").read_text(encoding="utf-8"))
    rows = []
    for case, entry in evidence["cases"].items():
        w, s, e, n = entry["aoi"]
        polygon = {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}
        for item in entry["selected"]:
            row = {"case": case, "id": item["id"], "aoi": entry["aoi"], "method": "full-resolution 20 m SCL; projected AOI polygon; pixel centers", "scl_url": item["assets"]["scl"]["href"]}
            try:
                req = urllib.request.Request(row["scl_url"], headers={"User-Agent": "floodviz-plan-research/1.0"})
                with OPENER.open(req, timeout=40) as response:
                    raw = response.read(10_000_001)
                    if len(raw) > 10_000_000:
                        raise ValueError("SCL size exceeds research cap")
                    row["downloaded_scl_bytes"] = len(raw)
                with MemoryFile(raw) as mem, mem.open() as src:
                    bounds = transform_bounds("EPSG:4326", src.crs, w, s, e, n, densify_pts=21)
                    f = from_bounds(*bounds, src.transform)
                    c0, r0 = math.floor(f.col_off), math.floor(f.row_off)
                    c1, r1 = math.ceil(f.col_off+f.width), math.ceil(f.row_off+f.height)
                    win = Window(c0, r0, c1-c0, r1-r0)
                    arr = src.read(1, window=win, boundless=True, fill_value=0)
                    geom = transform_geom("EPSG:4326", src.crs, polygon)
                    mask = geometry_mask([geom], arr.shape, src.window_transform(win), invert=True)
                    vals = arr[mask]
                    total = int(vals.size)
                    valid = int(np.count_nonzero(vals != 0))
                    counts = {str(i): int(np.count_nonzero(vals == i)) for i in range(12)}
                    cloud = sum(counts[str(i)] for i in (8, 9, 10))
                    row.update({"crs": str(src.crs), "resolution_m": list(src.res), "aoi_pixels": total,
                                "valid_coverage_percent": round(100*valid/total, 4),
                                "cloud_percent_of_valid": round(100*cloud/valid, 4) if valid else None,
                                "cloud_shadow_percent_of_valid": round(100*counts["3"]/valid, 4) if valid else None,
                                "clear_4_5_6_percent_of_aoi": round(100*sum(counts[str(i)] for i in (4, 5, 6))/total, 4),
                                "scl_counts": counts,
                                "status": "checked_scl_not_full_rgb_visual_review"})
            except Exception as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    out = {"checked_utc": datetime.now(timezone.utc).isoformat(), "candidates": rows,
           "notes": ["SCL is an automatic classification, not manual cloud validation.", "No full RGB scenes were downloaded or imported into the project."]}
    (BASE / "optical_aoi_quality.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
