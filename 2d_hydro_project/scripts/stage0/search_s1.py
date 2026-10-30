"""阶段0：Copernicus Dataspace 公开目录检索 Sentinel-1（无需登录；下载才需凭证）。

用法：uv --directory . run python scripts/stage0/search_s1.py
输出：data/catalog/s1_catalog.json（三 AOI × 摩羯事件窗口）
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

PROJ_ROOT = Path(__file__).resolve().parents[2]
OUT = PROJ_ROOT / "data" / "catalog" / "s1_catalog.json"

BASE = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"

# 摩羯事件窗口（终稿事件候选）
WINDOW = ("2024-09-05T00:00:00.000Z", "2024-09-17T00:00:00.000Z")

AOIS = {
    "jiulong": (108.55, 21.92, 108.90, 22.20),
    "ningming": (106.90, 21.95, 107.60, 22.45),
    "guigang": (109.20, 22.95, 110.00, 23.40),
}


def polygon_wkt(bbox: tuple[float, float, float, float]) -> str:
    w, s, e, n = bbox
    return f"POLYGON(({w} {s},{e} {s},{e} {n},{w} {n},{w} {s}))"


def search(aoi_name: str, bbox: tuple[float, float, float, float]) -> dict:
    filt = (
        "Collection/Name eq 'SENTINEL-1' "
        "and contains(Name,'IW_GRDH_1S') "
        f"and OData.CSC.Intersects(area=geography'SRID=4326;{polygon_wkt(bbox)}') "
        f"and ContentDate/Start gt {WINDOW[0]} "
        f"and ContentDate/Start lt {WINDOW[1]}"
    )
    url = f"{BASE}?$filter={quote(filt)}&$top=50&$orderby=ContentDate/Start"
    rec: dict = {"aoi": aoi_name, "bbox": list(bbox), "url": url}
    try:
        resp = requests.get(url, timeout=(10, 40))
        rec["http_status"] = resp.status_code
        resp.raise_for_status()
        items = resp.json().get("value", [])
        rec["n_products"] = len(items)
        rec["products"] = [
            {
                "name": it["Name"],
                "id": it["Id"],
                "sensing_start": it["ContentDate"]["Start"],
                "size_gb": round(it.get("ContentLength", 0) / 1e9, 2),
            }
            for it in items
        ]
    except Exception as exc:
        rec["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    return rec


def main() -> None:
    results = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "script": "scripts/stage0/search_s1.py",
        "window": list(WINDOW),
        "note": "目录检索公开；下载需 Dataspace 凭证（用户已注册）",
        "aois": {},
    }
    for name, bbox in AOIS.items():
        rec = search(name, bbox)
        results["aois"][name] = rec
        prods = rec.get("products", [])
        print(f"{name}: n={rec.get('n_products', '?')} status={rec.get('http_status', rec.get('error'))}")
        for p in prods[:6]:
            print(f"    {p['sensing_start'][:16]}  {p['size_gb']:5.2f}GB  {p['name'][:70]}")
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
