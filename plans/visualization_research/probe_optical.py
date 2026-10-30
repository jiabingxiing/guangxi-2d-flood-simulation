"""Read-only STAC research and small Range probes; never download full scenes."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "plans" / "visualization_research"
CASES = {
    "ningming": {
        "aoi": [106.96, 22.06, 107.20, 22.26],
        "query_window": "2026-08-01T00:00:00Z/2026-09-15T23:59:59Z",
        "selected": {
            "S2C_48QYK_20260812_0_L2A": "pre_event_background",
            "S2A_48QYK_20260903_0_L2A": "post_event_background",
        },
    },
    "guigang": {
        "aoi": [109.30, 22.98, 109.95, 23.32],
        "query_window": "2024-09-01T00:00:00Z/2024-10-31T23:59:59Z",
        "selected": {
            "S2B_49QCF_20240903_0_L2A": "event_start_candidate",
            "S2B_49QCF_20240913_0_L2A": "near_peak_cloudy_candidate",
            "S2B_49QCF_20241003_0_L2A": "post_event_background",
            "S2A_49QCF_20241031_0_L2A": "post_event_gap_fill_other_date",
        },
    },
}
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def small_probe(url: str) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": "floodviz-plan-research/1.0",
        "Range": "bytes=0-1023",
    })
    try:
        with OPENER.open(req, timeout=35) as response:
            head = response.read(1024)
            return {
                "status": response.status,
                "content_type": response.headers.get("Content-Type"),
                "content_range": response.headers.get("Content-Range"),
                "content_length": response.headers.get("Content-Length"),
                "bytes_read": len(head),
                "tiff_header": head[:4].hex(),
                "is_tiff": head[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"),
            }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def main() -> None:
    report = {"fetched_utc": datetime.now(timezone.utc).isoformat(), "cases": {}}
    for name, config in CASES.items():
        source = Path(os.environ["TEMP"]) / f"optical-{name}-search.json"
        doc = json.loads(source.read_text(encoding="utf-8-sig"))
        entry = {"aoi": config["aoi"], "query_window": config["query_window"],
                 "collection": "sentinel-2-l2a", "matched": doc.get("numberMatched"),
                 "query_url": "https://earth-search.aws.element84.com/v1/search?collections=sentinel-2-l2a&bbox="
                              + ",".join(map(str, config["aoi"])) + "&datetime=" + config["query_window"] + "&limit=100",
                 "all_items": [], "selected": []}
        for item in doc["features"]:
            p = item["properties"]
            base = {"id": item["id"], "datetime_utc": p["datetime"],
                    "tile_cloud_percent": p["eo:cloud_cover"], "bbox": item.get("bbox"),
                    "geometry": item["geometry"]}
            entry["all_items"].append(base)
            if item["id"] not in config["selected"]:
                continue
            selected = {**base, "role": config["selected"][item["id"]],
                        "epsg": p.get("proj:epsg"), "aoi_cloud_status": "not_yet_checked",
                        "item_url": next(x["href"] for x in item["links"] if x["rel"] == "self"),
                        "license_url_from_stac": next(x["href"] for x in item["links"] if x["rel"] == "license"),
                        "assets": {}}
            for key in ("visual", "red", "green", "blue", "scl"):
                asset = item["assets"][key]
                selected["assets"][key] = {"href": asset["href"], "gsd": asset.get("gsd"),
                    "shape": asset.get("proj:shape"), "transform": asset.get("proj:transform"),
                    "raster_bands": asset.get("raster:bands"), "probe": small_probe(asset["href"])}
                print(name, item["id"], key, selected["assets"][key]["probe"], flush=True)
            entry["selected"].append(selected)
        report["cases"][name] = entry
    (OUT / "optical_search_evidence.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
