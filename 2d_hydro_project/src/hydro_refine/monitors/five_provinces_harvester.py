"""南方五省区（粤、桂、黔、滇、琼）官方公开水情采集、阈值对照与 GloFAS 连续水量拉取模块。

严格遵循合规边界：
1. 仅对公开免登录 HTTP/HTML/Open-API 门户执行常规 GET 采集；
2. 遇到 `#FontTag` 字体加密（如水利部）、HTTP 412 JS WAF（如珠江委）或账号鉴权平台（省数据开放平台）
   一律 fail-closed 如实登记受阻状态，绝不执行任何绕过或破解。
"""
from __future__ import annotations

import gzip
import json
import re
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _fetch_text_clean(url: str, timeout_s: float = 10.0) -> dict[str, Any]:
    """安全拉取公开网页，自动解压 gzip 并识别 gb18030/utf-8；遇到 WAF/混淆严格标记阻塞。"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Encoding": "gzip, deflate",
    }
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s, context=ctx) as resp:
            raw = resp.read(600_000)
            if resp.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            text = None
            for enc in ("utf-8", "gb18030", "gbk"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            if text is None:
                text = raw.decode("utf-8", errors="ignore")

            low = text.lower()
            if "#fonttag" in low or "fonttag" in low or "otltag" in low or "script r=" in low:
                return {
                    "url": url,
                    "status": "protected_or_obfuscated",
                    "http_status": int(resp.status),
                    "text": "",
                }
            return {
                "url": url,
                "status": "ok",
                "http_status": int(resp.status),
                "text": text,
            }
    except urllib.error.HTTPError as e:
        return {"url": url, "status": "http_error", "http_status": int(e.code), "reason": str(e.reason), "text": ""}
    except Exception as e:
        return {"url": url, "status": "unreachable", "error": f"{type(e).__name__}: {str(e)[:120]}", "text": ""}


def parse_hainan_daily_homepage(html: str) -> dict[str, Any]:
    """从海南省水务厅官网首页【每日水雨情】提取大型水库与三大江河公开实时水位。"""
    idx = html.find("每日水雨情")
    if idx < 0:
        return {"available": False, "records": []}
    snippet = re.sub(r"<[^>]+>", " ", html[idx : idx + 2200])
    snippet = re.sub(r"\s+", " ", snippet).strip()
    end_m = re.search(r"new Marquee|</marquee>", snippet, flags=re.I)
    if end_m:
        snippet = snippet[: end_m.start()].strip()

    date_m = re.search(r"(\d{1,2}月\d{1,2}日\d{1,2}时至\d{1,2}月\d{1,2}日\d{1,2}时)", snippet)
    window_text = date_m.group(1) if date_m else ""
    rain_m = re.search(r"最大降雨出现在([^，,]+)，累计雨量为([0-9.]+)\s*mm", snippet)

    records: list[dict[str, Any]] = []
    # 水库水位（例：松涛水库186.06米）
    for name, val in re.findall(
        r"(?:^|[、，,。；：\s])([\u4e00-\u9fa5]{2,8}水库)\s*([0-9]+(?:\.[0-9]+)?)\s*米",
        snippet,
    ):
        records.append({
            "station_name": name,
            "station_type": "reservoir",
            "water_level_m": float(val),
            "window_text": window_text,
            "province": "海南",
        })
    # 江河水位（例：其中三大江河南渡江龙塘水位7.58米、万泉河加积水位1.05米）
    for river_site, val in re.findall(
        r"(?:其中三大江河|[、，,。；：\s])([\u4e00-\u9fa5]{3,10})水位\s*([0-9]+(?:\.[0-9]+)?)\s*米",
        snippet,
    ):
        records.append({
            "station_name": river_site,
            "station_type": "river",
            "water_level_m": float(val),
            "window_text": window_text,
            "province": "海南",
        })

    return {
        "available": len(records) > 0,
        "window_text": window_text,
        "max_rain_station": rain_m.group(1) if rain_m else None,
        "max_rain_mm": float(rain_m.group(2)) if rain_m else None,
        "raw_text": snippet[:500],
        "records": records,
    }


def parse_hainan_sqmap_thresholds(html: str) -> dict[str, Any]:
    """从海南三防实时水情页 sq_map.asp 提取全岛水文/水库站警戒、保证/汛限水位阈值档案。"""
    markers = re.findall(r"marker\.attr\('title','([^']+)'\)", html)
    stations: list[dict[str, Any]] = []
    for raw in markers:
        clean = raw.replace("\\r\\n", "\n").replace("\r\n", "\n")
        name_m = re.search(r"站名：([^\n]+)", clean)
        warn_m = re.search(r"警戒水位：([0-9]+(?:\.[0-9]+)?)", clean)
        guar_m = re.search(r"保证水位：([0-9]+(?:\.[0-9]+)?)", clean)
        flood_lim_m = re.search(r"汛限水位：([0-9]+(?:\.[0-9]+)?)", clean)
        if not name_m:
            continue
        st_name = name_m.group(1).strip()
        stations.append({
            "station_label": st_name,
            "station_type": "reservoir" if "水库" in st_name else "river",
            "warning_level_m": float(warn_m.group(1)) if warn_m else None,
            "guarantee_level_m": float(guar_m.group(1)) if guar_m else None,
            "flood_limit_level_m": float(flood_lim_m.group(1)) if flood_lim_m else None,
        })

    # 读取超警戒/超汛限实时计数
    def _extract_cnt(var_name: str) -> int | None:
        m = re.search(rf"var\s+{var_name}\s*=\s*(\d+)", html)
        return int(m.group(1)) if m else None

    return {
        "n_stations_cataloged": len(stations),
        "exceed_guarantee_river_count": _extract_cnt("count_hdcwx"),
        "exceed_warning_river_count": _extract_cnt("count_hdcjj"),
        "exceed_flood_limit_reservoir_count": _extract_cnt("count_skcxx"),
        "stations_sample": stations[:20],
        "all_stations": stations,
    }


def parse_guangxi_emergency_responses(html: str) -> list[dict[str, Any]]:
    """从广西水利厅预警响应栏目提取带日期与链接的洪水防御响应清单。"""
    items: list[dict[str, Any]] = []
    # 常见格式：<li>...<span>2026-09-01</span><a href="./t28089361.shtml">标题</a>
    for m in re.finditer(
        r"(20\d{2}-\d{2}-\d{2}).{0,120}?href=[\"']([^\"']+\.shtml)[\"'][^>]*>\s*([^<]{6,120})\s*<",
        html,
        flags=re.S,
    ):
        items.append({
            "date": m.group(1),
            "href": m.group(2),
            "title": re.sub(r"\s+", " ", m.group(3)).strip(),
        })
    if not items:
        for href, title in re.findall(r'href=["\']([^"\']+\.shtml)["\'][^>]*>\s*([^<]{6,120})\s*<', html):
            if any(k in title for k in ("洪水", "应急响应", "预警", "台风", "防汛")):
                items.append({"date": None, "href": href, "title": re.sub(r"\s+", " ", title).strip()})
    return items[:25]


def parse_generic_link_index(html: str) -> list[dict[str, str]]:
    """提取公开水利水文栏目内的最新水文/防汛公报链接清单。"""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for href, title in re.findall(r'href=["\']([^"\']+)["\'][^>]*>\s*([^<\r\n]{4,100})\s*<', html):
        t = re.sub(r"\s+", " ", title).strip()
        if t in seen or not any(k in t for k in ("水情", "水位", "洪水", "水文", "公报", "防汛", "预警", "暴雨")):
            continue
        seen.add(t)
        out.append({"title": t, "href": href})
        if len(out) >= 15:
            break
    return out


def fetch_glofas_discharge_series(
    stations: list[dict[str, Any]],
    *,
    start_date: str = "2024-04-01",
    end_date: str = "2024-09-25",
    api_url: str = "https://flood-api.open-meteo.com/v1/flood",
    snap_grid_deg: float = 0.05,
) -> dict[str, Any]:
    """从 Open-Meteo Flood API (GloFAS v4) 拉取跨五省区控制断面的免费公开日流量过程（m³/s）。

    支持围绕测站坐标做 3×3（±snap_grid_deg）干流像元自动吸附（选择多年/窗均流量最大网格，
    避免 0.05° 粗网格坐标微偏落入旁侧支流坡面导致流量低估两个数量级）。
    """
    results: dict[str, Any] = {}
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    headers = {"User-Agent": "gx-hydro2d/0.1 (five-province research verification)"}

    for st in stations:
        sid = st["station_id"]
        lat0 = float(st["lat"])
        lon0 = float(st["lon"])
        offsets = [0.0] if snap_grid_deg <= 0 else [-snap_grid_deg, 0.0, snap_grid_deg]
        lats = [lat0 + dy for dy in offsets for _dx in offsets]
        lons = [lon0 + dx for _dy in offsets for dx in offsets]
        lat_q = ",".join(f"{v:.4f}" for v in lats)
        lon_q = ",".join(f"{v:.4f}" for v in lons)
        url = (
            f"{api_url}?latitude={lat_q}&longitude={lon_q}"
            f"&daily=river_discharge&start_date={start_date}&end_date={end_date}"
        )
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                raw = resp.read(5_000_000)
                if resp.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                payload = json.loads(raw.decode("utf-8"))

            items = payload if isinstance(payload, list) else [payload]
            best_item = None
            best_mean = -1.0
            raw_center_mean = None
            center_idx = len(items) // 2
            for idx_i, item in enumerate(items):
                q_list = [float(v) for v in ((item.get("daily") or {}).get("river_discharge") or []) if v is not None]
                m_q = sum(q_list) / len(q_list) if q_list else -1.0
                if idx_i == center_idx:
                    raw_center_mean = m_q
                if m_q > best_mean:
                    best_mean = m_q
                    best_item = item

            chosen = best_item or items[0]
            daily = chosen.get("daily") or {}
            dates = daily.get("time") or []
            q_vals = [float(v) if v is not None else None for v in (daily.get("river_discharge") or [])]
            valid_pairs = [(d, q) for d, q in zip(dates, q_vals) if q is not None]
            peak_d, peak_q = max(valid_pairs, key=lambda x: x[1]) if valid_pairs else (None, None)
            results[sid] = {
                "status": "ok",
                "province": st["province"],
                "name": st["name"],
                "river": st["river"],
                "grid_lat": chosen.get("latitude"),
                "grid_lon": chosen.get("longitude"),
                "snapped_from_center_mean_m3s": round(raw_center_mean, 2) if raw_center_mean is not None and raw_center_mean >= 0 else None,
                "n_days": len(valid_pairs),
                "peak_date": peak_d,
                "peak_q_m3s": round(peak_q, 2) if peak_q is not None else None,
                "mean_q_m3s": round(sum(q for _, q in valid_pairs) / len(valid_pairs), 2) if valid_pairs else None,
                "dates": dates,
                "river_discharge_m3s": q_vals,
            }
        except Exception as e:
            results[sid] = {
                "status": "error",
                "province": st["province"],
                "name": st["name"],
                "error": f"{type(e).__name__}: {str(e)[:120]}",
            }
    return results


def harvest_five_provinces(
    portal_cfg: dict[str, Any],
    *,
    live_http: bool = True,
    fetch_glofas: bool = True,
    glofas_start: str = "2024-04-01",
    glofas_end: str = "2024-09-25",
) -> dict[str, Any]:
    """执行南方五省区公开水情接口探测、明文抓取与 GloFAS 连续流量拉取。"""
    portal_results: list[dict[str, Any]] = []
    hainan_live: dict[str, Any] = {}
    hainan_thresholds: dict[str, Any] = {}
    guangxi_responses: list[dict[str, Any]] = []

    for group in portal_cfg.get("portals", []):
        prov = group["province"]
        prov_code = group["province_code"]
        for ep in group.get("endpoints", []):
            rec: dict[str, Any] = {
                "province": prov,
                "province_code": prov_code,
                "id": ep["id"],
                "url": ep["url"],
                "kind": ep["kind"],
                "note": ep.get("note", ""),
            }
            if not live_http:
                rec["probe_status"] = "skipped_offline"
                portal_results.append(rec)
                continue

            if ep["kind"] in ("login_gated_opendata",):
                # 仅做首页连通状态探测（不尝试登录/认证绕过）
                st = _fetch_text_clean(ep["url"], timeout_s=8.0)
                rec["probe_status"] = "login_required_open_platform" if st.get("status") == "ok" else st.get("status")
                rec["http_status"] = st.get("http_status")
                if st.get("error"):
                    rec["detail"] = st["error"]
                portal_results.append(rec)
                continue

            resp = _fetch_text_clean(ep["url"])
            rec["probe_status"] = resp["status"]
            rec["http_status"] = resp.get("http_status")
            if resp.get("error"):
                rec["detail"] = resp["error"]
            elif resp.get("reason"):
                rec["detail"] = resp["reason"]

            if resp["status"] == "ok":
                parser_name = ep.get("parser", "status_only")
                if parser_name == "hainan_daily_homepage":
                    hainan_live = parse_hainan_daily_homepage(resp["text"])
                    rec["parsed_count"] = len(hainan_live.get("records", []))
                    rec["window_text"] = hainan_live.get("window_text")
                elif parser_name == "hainan_sqmap_thresholds":
                    hainan_thresholds = parse_hainan_sqmap_thresholds(resp["text"])
                    rec["parsed_count"] = hainan_thresholds.get("n_stations_cataloged", 0)
                elif parser_name == "guangxi_emergency_responses":
                    guangxi_responses = parse_guangxi_emergency_responses(resp["text"])
                    rec["parsed_count"] = len(guangxi_responses)
                elif parser_name == "generic_link_index":
                    links = parse_generic_link_index(resp["text"])
                    rec["parsed_count"] = len(links)
                    rec["sample_links"] = links[:6]
            portal_results.append(rec)

    glofas_series: dict[str, Any] = {}
    if fetch_glofas:
        glofas_series = fetch_glofas_discharge_series(
            portal_cfg.get("benchmark_stations", []),
            start_date=glofas_start,
            end_date=glofas_end,
            api_url=str((portal_cfg.get("policy") or {}).get("open_glofas_api", "https://flood-api.open-meteo.com/v1/flood")),
        )

    # 海南实测站 vs 警戒/汛限水位静态档案交叉联检
    hainan_cross_check: list[dict[str, Any]] = []
    if hainan_live.get("records") and hainan_thresholds.get("all_stations"):
        all_th = hainan_thresholds["all_stations"]
        for r in hainan_live["records"]:
            st_name = r["station_name"]
            st_type = r["station_type"]
            core = st_name.replace("南渡江", "").replace("万泉河", "").replace("昌化江", "").replace("水库", "")
            matched = [
                t for t in all_th
                if t.get("station_type") == st_type and core and core in t["station_label"]
            ]
            if not matched and core:
                matched = [t for t in all_th if core in t["station_label"]]
            warn_lvl = matched[0]["warning_level_m"] if matched else None
            limit_lvl = (
                warn_lvl
                if warn_lvl is not None
                else (matched[0].get("flood_limit_level_m") if matched else None)
            )
            wse = float(r["water_level_m"])
            hainan_cross_check.append({
                "station_name": st_name,
                "station_type": st_type,
                "observed_wse_m": wse,
                "matched_catalog_station": matched[0]["station_label"] if matched else None,
                "warning_or_flood_limit_m": limit_lvl,
                "margin_to_limit_m": round(wse - limit_lvl, 2) if limit_lvl is not None else None,
                "above_limit": (wse >= limit_lvl) if limit_lvl is not None else None,
            })

    return {
        "policy": portal_cfg.get("policy", {}),
        "portal_probe_summary": portal_results,
        "hainan_live_daily": hainan_live,
        "hainan_threshold_catalog": {k: v for k, v in hainan_thresholds.items() if k != "all_stations"},
        "hainan_live_threshold_check": hainan_cross_check,
        "guangxi_emergency_responses": guangxi_responses[:15],
        "glofas_five_province_discharge": glofas_series,
    }
