"""阶段0：按 AOI 核验三案例 UTM 投影带（终稿 0.3 争议项决策 / 2.1 基准约定）。

用法：uv --directory . run python scripts/stage0/check_crs.py
输出：data/catalog/crs_check.json + 控制台摘要。
注意：AOI 为暂定框；业务确认精确 AOI 后必须复跑本脚本。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pyproj import CRS
from pyproj.aoi import AreaOfInterest
from pyproj.database import query_utm_crs_info

PROJ_ROOT = Path(__file__).resolve().parents[2]
OUT_PATH = PROJ_ROOT / "data" / "catalog" / "crs_check.json"

# 暂定 AOI（WGS84 经纬度 [west, south, east, north]）；业务确认后更新
AOIS: dict[str, dict] = {
    "jiulong": {
        "bbox": (108.55, 21.92, 108.90, 22.20),
        "plan_expected_epsg": 32649,
        "note": "锚点：久隆镇 108.7161E 22.0638N（暂定框）",
    },
    "ningming": {
        "bbox": (106.90, 21.95, 107.60, 22.45),
        "plan_expected_epsg": 32648,
        "note": "锚点：宁明县城 107.07E 22.14N（暂定框）",
    },
    "guigang": {
        "bbox": (109.20, 22.95, 110.00, 23.40),
        "plan_expected_epsg": 32649,
        "note": "锚点：贵港市区郁江段 109.60E 23.11N（暂定框）",
    },
}


def utm_zone_from_lon(lon: float) -> int:
    return int((lon + 180.0) // 6.0) + 1


def check_case(name: str, spec: dict) -> dict:
    west, south, east, north = spec["bbox"]
    infos = query_utm_crs_info(
        datum_name="WGS 84",
        area_of_interest=AreaOfInterest(
            west_lon_degree=west, south_lat_degree=south,
            east_lon_degree=east, north_lat_degree=north,
        ),
    )
    zones = {utm_zone_from_lon(west), utm_zone_from_lon(east)}
    rec: dict = {
        "bbox_wgs84": list(spec["bbox"]),
        "note": spec["note"],
        "utm_zone_by_lon_formula": sorted(zones),
        "n_pyproj_candidates": len(infos),
    }
    if len(infos) == 1:
        info = infos[0]
        crs = CRS.from_epsg(int(info.code))
        rec["epsg"] = int(info.code)
        rec["crs_name"] = info.name
        rec["unit"] = crs.axis_info[0].unit_name
        rec["is_projected"] = crs.is_projected
        rec["single_zone"] = len(zones) == 1
        rec["matches_plan_expectation"] = int(info.code) == spec["plan_expected_epsg"]
        rec["plan_expected_epsg"] = spec["plan_expected_epsg"]
    else:
        rec["epsg"] = None
        rec["candidates"] = [{"code": i.code, "name": i.name} for i in infos]
        rec["matches_plan_expectation"] = False
        rec["flag"] = "AOI 跨带或 pyproj 返回多候选，需人工裁定/收窄 AOI"
    return rec


def main() -> None:
    results = {}
    for name, spec in AOIS.items():
        results[name] = check_case(name, spec)

    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "script": "scripts/stage0/check_crs.py",
        "aoi_status": "provisional（暂定框，业务确认后复跑）",
        "cases": results,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"CRS check -> {OUT_PATH}")
    for name, rec in results.items():
        epsg = rec.get("epsg")
        ok = rec.get("matches_plan_expectation")
        print(
            f"{name:9s} bbox={rec['bbox_wgs84']} zone(lon)={rec['utm_zone_by_lon_formula']} "
            f"-> pyproj EPSG:{epsg} unit={rec.get('unit')} matches_plan={ok} "
            f"{rec.get('flag', '')}"
        )


if __name__ == "__main__":
    main()
