"""GR4J 上游边界 vs GloFAS 日流量先验对照（计划 T07 / 成功标准 S3；ADR-0016）。

口径（ADR-0016 §1，先验对照不作拟合目标、防循环论证）：

- 主指标 = 事件窗日流量 Pearson r；r ≥ 0.7 达标；r < 0.5 → 退回三角情景线；
  其间或数据缺失 → 如实记「未验证」；
- 先验通道：优先 cdsapi（GloFAS-ERA5 再分析 v4，需 CDS API key）；无 key 时用
  Open-Meteo flood API（GloFAS v4 数据，无凭证，格点 ~5–10 km）抽样——
  两者都属「GloFAS 再分析先验」，量级偏差只登记、不校正；
- 日界口径：GR4J 为 Asia/Shanghai 日界，GloFAS 为 UTC 日界，相差 8 h，
  日尺度对比可接受（随审计登记）；
- 对比窗口：事件窗（t0 起）为主，全窗为辅。

输入：`data/interim/inflow/<site>_gr4j_daily_q.json`（build_inflow_hydrology.py 落盘）。
输出：`data/catalog/inflow_glofas_check.json`（r、峰现时差、量级比、未验证字段）。

用法（先设 `$env:PYTHONUTF8="1"`）::

    uv --directory 2d_hydro_project run python scripts/stage3/verify_inflow_vs_glofas.py --site guigang
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import AuditLog, utc_now, write_json  # noqa: E402

CATALOG = PROJ / "data" / "catalog"
INTERIM = PROJ / "data" / "interim" / "inflow"

GLOFAS_CELL = {
    "guigang": {"lat": 23.125, "lon": 109.625,
                "note": "郁江贵港下游河段格点；priors JSON 已登记 2024-09 抽样"},
    "ningming": {"lat": 22.125, "lon": 107.075,
                 "note": "明江宁明县城格点；峰值时点与公开报道洪峰一致"},
}


def _rel(path: Path) -> str:
    try:
        from scripts.common.paths import rel_posix

        return rel_posix(path)
    except Exception:
        return str(path)


def load_gr4j_daily(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(
            f"GR4J 日序列不存在：{path}。先运行 "
            "scripts/stage3/build_inflow_hydrology.py 生成（含日序列落盘）。"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def fetch_glofas_open_meteo(lat: float, lon: float, start: str, end: str,
                            cache_path: Path, *, use_cache: bool = True) -> dict[str, Any]:
    """Open-Meteo flood API（GloFAS v4）日 river_discharge；带缓存。"""
    if cache_path.is_file() and use_cache:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("window") == [start, end] and cached.get("cell") == [lat, lon]:
            return cached
    import requests

    url = (f"https://flood-api.open-meteo.com/v1/flood/?latitude={lat}&longitude={lon}"
           f"&daily=river_discharge&start_date={start}&end_date={end}")
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"GloFAS 先验获取失败（Open-Meteo flood API）：{exc}") from exc
    rec = {"window": [start, end], "cell": [lat, lon], "snapped_cell": [
        data.get("latitude"), data.get("longitude")],
        "time": data.get("daily", {}).get("time", []),
        "river_discharge_m3s": data.get("daily", {}).get("river_discharge", []),
        "source": "open-meteo flood API (GloFAS v4)",
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(cache_path, rec)
    return rec


def fetch_glofas_cdsapi(lat: float, lon: float, start: str, end: str) -> dict[str, Any]:
    """GloFAS-ERA5 再分析（CDS）；需 CDS API key，无 key fail-closed。"""
    key = None
    cdsapirc = Path.home() / ".cdsapirc"
    if cdsapirc.is_file():
        key = "file"
    import os

    if os.environ.get("CDSAPI_KEY"):
        key = "env"
    if key is None:
        raise RuntimeError(
            "未找到 CDS API key（~/.cdsapirc 或 CDSAPI_KEY）。"
            "注册 https://cds.climate.copernicus.eu/ 后配置，"
            "或改用 --glofas-source open-meteo（无凭证，已登记为等价先验通道）。"
        )
    raise RuntimeError(
        "cdsapi 通道需下载 GloFAS-ERA5 v4 再分析（grib），本脚本未实现该解析；"
        "请使用 --glofas-source open-meteo（同一 GloFAS v4 数据源）。"
    )


def pearson_r(a: np.ndarray, b: np.ndarray) -> float:
    """Pearson r；方差为 0 时返回 nan（调用方记未验证）。"""
    if a.size < 3 or np.std(a) == 0 or np.std(b) == 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def compare(gr4j: dict[str, Any], glofas: dict[str, Any], window: tuple[str, str] | None,
            area_km2: float) -> dict[str, Any]:
    """对齐日期轴并计算 r / 峰现时差 / 量级比。"""
    g_dates = gr4j["dates"]
    g_q = np.asarray(gr4j["q_m3s"], dtype=np.float64)
    f_dates = glofas["time"]
    f_q = np.asarray(glofas["river_discharge_m3s"], dtype=np.float64)
    f_map = dict(zip(f_dates, f_q.tolist()))
    lo, hi = window if window else (g_dates[0], g_dates[-1])
    pairs_g, pairs_f, used_dates = [], [], []
    for d, q in zip(g_dates, g_q):
        if lo <= d <= hi and d in f_map and f_map[d] is not None:
            pairs_g.append(float(q))
            pairs_f.append(float(f_map[d]))
            used_dates.append(d)
    if len(pairs_g) < 5:
        return {"status": "insufficient_overlap", "n_days": len(pairs_g),
                "note": "重叠日 < 5，无法计算 r；记未验证"}
    ga = np.asarray(pairs_g)
    fa = np.asarray(pairs_f)
    r = pearson_r(ga, fa)
    g_peak_i = int(np.argmax(ga))
    f_peak_i = int(np.argmax(fa))
    g_peak_date = used_dates[g_peak_i]
    f_peak_date = used_dates[f_peak_i]
    from datetime import date

    lag_days = (date.fromisoformat(g_peak_date) - date.fromisoformat(f_peak_date)).days
    out = {
        "status": "ok",
        "n_days": len(pairs_g),
        "window": [lo, hi],
        "pearson_r": round(r, 4) if np.isfinite(r) else None,
        "gr4j_peak_m3s": round(float(ga[g_peak_i]), 1),
        "glofas_peak_m3s": round(float(fa[f_peak_i]), 1),
        "gr4j_peak_date": g_peak_date,
        "glofas_peak_date": f_peak_date,
        "peak_lag_days": lag_days,
        "magnitude_ratio_glofas_over_gr4j": round(float(fa.mean() / ga.mean()), 3)
        if ga.mean() > 0 else None,
        "area_km2_prior": area_km2,
    }
    if not np.isfinite(r):
        out["verdict"] = "未验证（序列方差为 0 或重叠不足）"
    elif r >= 0.7:
        out["verdict"] = "达标（r ≥ 0.7；与再分析先验一致，不证明真值）"
    elif r < 0.5:
        out["verdict"] = "r < 0.5：按 ADR-0016 退回三角情景线并登记"
    else:
        out["verdict"] = "未验证（0.5 ≤ r < 0.7，未达 S3 门限）"
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", required=True, choices=list(GLOFAS_CELL))
    parser.add_argument("--glofas-source", default="open-meteo",
                        choices=["open-meteo", "cdsapi"])
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument("--window", default=None,
                        help="对比窗 start,end（ISO；默认 GR4J 事件窗）")
    from scripts.common.grid_io import add_common_args

    add_common_args(parser)
    args = parser.parse_args()
    log = AuditLog("verify_inflow_vs_glofas.py", run_tag=args.run_tag, dry_run=args.dry_run)
    log.param(site=args.site, glofas_source=args.glofas_source)

    daily_path = INTERIM / f"{args.site}_gr4j_daily_q.json"
    gr4j = load_gr4j_daily(daily_path)
    log.add_input("gr4j_daily_q", daily_path)
    cell = GLOFAS_CELL[args.site]
    start, end = gr4j["dates"][0], gr4j["dates"][-1]
    if args.glofas_source == "cdsapi":
        glofas = fetch_glofas_cdsapi(cell["lat"], cell["lon"], start, end)
    else:
        cache = INTERIM / f"glofas_{args.site}_{start}_{end}.json"
        glofas = fetch_glofas_open_meteo(cell["lat"], cell["lon"], start, end, cache,
                                         use_cache=not args.no_fetch)
    log.add_input("glofas_series", cache if args.glofas_source != "cdsapi" else "(cdsapi)")
    window = tuple(args.window.split(",")) if args.window else tuple(gr4j["event_window"])
    result = compare(gr4j, glofas, window, gr4j["area_km2"])
    full = compare(gr4j, glofas, None, gr4j["area_km2"])

    payload = {
        "script": "verify_inflow_vs_glofas.py",
        "generated_utc": utc_now(),
        "site": args.site,
        "glofas_source": glofas.get("source", args.glofas_source),
        "glofas_cell_requested": [cell["lat"], cell["lon"]],
        "glofas_cell_snapped": glofas.get("snapped_cell"),
        "caveats": [
            "GloFAS 为再分析先验，不作实测/拟合目标（防循环论证，ADR-0016）",
            "GR4J Asia/Shanghai 日界 vs GloFAS UTC 日界，相差 8 h（日尺度可接受）",
            "r 达标只证明与先验一致，不证明真值（S3 口径）",
            cell["note"],
        ],
        "event_window_result": result,
        "full_window_result": full,
        "verdict": result.get("verdict", "未验证"),
        "evidence_level": "E3（先验对照，情景级）",
        "dry_run": bool(args.dry_run),
    }
    out_json = args.log_json or (CATALOG / "inflow_glofas_check.json")
    log.payload["comparison"] = payload
    log.write(out_json)
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    print("->", out_json)


if __name__ == "__main__":
    main()
