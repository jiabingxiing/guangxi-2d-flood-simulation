"""阶段0：外部数据源可达性探测（直连 vs 环境代理双路径）。

用法：uv --directory . run python scripts/stage0/probe_endpoints.py
规则（终稿 四章精神）：超时明确、无重试（探测非下载）、结果与时间戳落盘。
输出：data/catalog/reachability.json；Open-Meteo 功能样例存 data/catalog/probes/。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

PROJ_ROOT = Path(__file__).resolve().parents[2]
CAT_DIR = PROJ_ROOT / "data" / "catalog"
PROBE_DIR = CAT_DIR / "probes"

ENDPOINTS: list[tuple[str, str, str]] = [
    ("pypi_aliyun", "https://mirrors.aliyun.com/pypi/simple/numpy/", "Python 依赖镜像（国内直连基准）"),
    ("open_meteo_docs", "https://open-meteo.com/en/docs/historical-weather-api", "Open-Meteo 历史天气文档"),
    ("open_meteo_api", "https://archive-api.open-meteo.com/v1/archive", "Open-Meteo 归档 API（ERA5 再分析）"),
    ("copernicus_dataspace", "https://dataspace.copernicus.eu", "Copernicus Dataspace（COP-DEM / Sentinel-1）"),
    ("esa_worldcover", "https://esa-worldcover.org/en/data-access", "ESA WorldCover"),
    ("fabdem", "https://research-information.bris.ac.uk/en/datasets/fabdem-v1-2/", "FABDEM v1.2 数据页"),
    ("seamlesswave", "https://www.seamlesswave.com/LISFLOOD8.0.html", "LISFLOOD-FP 主候选模型"),
    ("hec_usace", "https://www.hec.usace.army.mil/software/hec-ras/", "HEC-RAS 备选模型"),
    ("unspider", "https://un-spider.org/advisory-support/recommended-practices/flood-mapping/step-by-step", "SAR 洪水制图实践"),
]

# 功能性样例：久隆锚点 2024-09-06~09 逐时降水（台风摩羯窗口）
API_SAMPLE_PARAMS = {
    "latitude": 22.0638,
    "longitude": 108.7161,
    "start_date": "2024-09-06",
    "end_date": "2024-09-09",
    "hourly": "precipitation",
    "timezone": "Asia/Shanghai",
}


def probe(url: str, params: dict | None = None, direct: bool = True) -> dict:
    session = requests.Session()
    if direct:
        session.trust_env = False  # 绕过 HTTP(S)_PROXY，走直连
    t0 = time.monotonic()
    try:
        resp = session.get(url, params=params, timeout=(10, 20))
        return {
            "ok": 200 <= resp.status_code < 400,
            "status": resp.status_code,
            "seconds": round(time.monotonic() - t0, 2),
            "bytes": len(resp.content),
        }
    except Exception as exc:  # 探测脚本：记录一切失败原因
        return {
            "ok": False,
            "status": None,
            "seconds": round(time.monotonic() - t0, 2),
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
        }


def main() -> None:
    results = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "script": "scripts/stage0/probe_endpoints.py",
        "env_proxy": __import__("os").environ.get("HTTPS_PROXY", ""),
        "endpoints": {},
        "open_meteo_api_sample": {},
    }

    for ep_id, url, purpose in ENDPOINTS:
        rec = {
            "url": url,
            "purpose": purpose,
            "direct": probe(url, direct=True),
            "via_env_proxy": probe(url, direct=False),
        }
        results["endpoints"][ep_id] = rec
        d, p = rec["direct"], rec["via_env_proxy"]

        def _fmt(r: dict) -> str:
            val = r["status"] if r.get("status") is not None else r.get("error", "?")
            return repr(val)[:60]

        print(f"{ep_id:22s} direct={_fmt(d)} proxy={_fmt(p)}")

    # Open-Meteo 功能样例（带参数的 API 调用）
    for mode, direct in (("direct", True), ("via_env_proxy", False)):
        rec = probe("https://archive-api.open-meteo.com/v1/archive", params=API_SAMPLE_PARAMS, direct=direct)
        results["open_meteo_api_sample"][mode] = rec
        if rec.get("ok"):
            session = requests.Session()
            session.trust_env = not direct
            resp = session.get("https://archive-api.open-meteo.com/v1/archive", params=API_SAMPLE_PARAMS, timeout=(10, 20))
            PROBE_DIR.mkdir(parents=True, exist_ok=True)
            out = PROBE_DIR / f"open_meteo_sample_{mode}.json"
            out.write_text(resp.text, encoding="utf-8")
            print(f"open_meteo sample ({mode}) saved -> {out}")

    CAT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CAT_DIR / "reachability.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"reachability -> {out_path}")


if __name__ == "__main__":
    main()
