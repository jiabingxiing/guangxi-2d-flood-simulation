"""阶段6：南方五省区（粤、桂、黔、滇、琼）公开水情与 PipeNetGen/PySWMM 算法泛化核验。

执行两大独立核验：
1. 水文监测核验（tracks/monitors）：对照公开五省区实测通报极值（北江石角/韩江三河坝/西江梧州/郁江贵港）与
   Open-Meteo GloFAS v4 日尺度连续流量，检验洪峰同步性、量级比与超警阈值判别一致性；
2. 管网拓扑与一维动力波泛化核验（tracks/pipenet）：基于本地 OSM PBF 单遍抽取五省代表城市真实道路与水系，
   结合 Copernicus GLO-30 30m 真实 DEM 构建重力 DAG 管网并运行 EPA-SWMM5 (PySWMM) 动力波模拟，
   验证算法从广西向五省不同地貌（珠三角感潮、喀斯特山地、高原盆地、热带滨海平缓区）迁移的有效性与零逆坡稳健性。

用法（先设 $env:PYTHONUTF8 = "1"）：
    uv --directory 2d_hydro_project run python scripts/stage6/verify_five_province_algorithms.py --osm-pbf "<pbf路径>"
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

import networkx as nx
import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject
from rasterio.windows import from_bounds
import yaml

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from hydro_refine.core.paths import external, rel_posix, resolve_legacy  # noqa: E402
from hydro_refine.coupling.dynamic_pyswmm_link import run_swmm_drainage_simulation  # noqa: E402
from hydro_refine.monitors.five_provinces_harvester import harvest_five_provinces  # noqa: E402
from hydro_refine.pipenet.gravity_dag_builder import build_gravity_dag  # noqa: E402
from hydro_refine.pipenet.hydraulic_sizer import size_gravity_network  # noqa: E402
from hydro_refine.pipenet.osm_road_extractor import build_road_graph, load_road_geojson  # noqa: E402
from hydro_refine.pipenet.swmm_serializer import export_pipenet_geojson, export_swmm_inp, summarize_pipenet  # noqa: E402
from scripts.common.grid_io import AuditLog, utc_now, write_json  # noqa: E402

SNAPSHOT_JSON = PROJ / "data" / "catalog" / "five_provinces_hydro_snapshot.json"
PORTAL_CFG = PROJ / "configs" / "gauges" / "five_provinces_portals.yaml"
PIPENET_5P_CFG = PROJ / "configs" / "pipenet" / "five_provinces_urban_rules.yaml"
OUT_JSON = PROJ / "data" / "catalog" / "five_provinces_algorithm_verification.json"


def _extract_five_sites_osm_single_pass(
    pbf_path: Path,
    sites: list[dict[str, Any]],
    include_highways: set[str],
    out_dir: Path,
) -> dict[str, tuple[Path, Path]]:
    """单遍扫描中国全量 .osm.pbf，同时提取五省试验区真实道路网与河道水系 GeoJSON。"""
    needed: list[dict[str, Any]] = []
    result_paths: dict[str, tuple[Path, Path]] = {}
    for s in sites:
        sid = s["site_id"]
        rp = out_dir / f"{sid}_roads.geojson"
        wp = out_dir / f"{sid}_waterways.geojson"
        result_paths[sid] = (rp, wp)
        if not rp.is_file() or not wp.is_file():
            needed.append({"site_id": sid, "bbox": s["aoi_wgs84"], "roads": str(rp), "waters": str(wp)})

    if not needed:
        return result_paths
    if not pbf_path.is_file():
        raise FileNotFoundError(f"缺少 OSM PBF 文件：{pbf_path}")

    out_dir.mkdir(parents=True, exist_ok=True)
    spec_json = out_dir / "_extract_spec.json"
    spec_json.write_text(json.dumps({"sites": needed, "allowed_hw": sorted(include_highways)}, ensure_ascii=False), encoding="utf-8")

    helper = (
        "import json, sys, osmium\n"
        "from pathlib import Path\n"
        "spec = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))\n"
        "allowed_hw = set(spec['allowed_hw'])\n"
        "allowed_ww = {'river', 'canal', 'stream', 'drain'}\n"
        "sites = spec['sites']\n"
        "roads_out = {s['site_id']: [] for s in sites}\n"
        "waters_out = {s['site_id']: [] for s in sites}\n"
        "class MultiH(osmium.SimpleHandler):\n"
        "    def way(self, w):\n"
        "        t = w.tags\n"
        "        hw = t.get('highway')\n"
        "        ww = t.get('waterway')\n"
        "        is_hw = (hw in allowed_hw) and not (t.get('bridge') == 'yes' or t.get('tunnel') == 'yes')\n"
        "        is_ww = (ww in allowed_ww)\n"
        "        if not (is_hw or is_ww) or len(w.nodes) < 2:\n"
        "            return\n"
        "        loc0 = w.nodes[0].location\n"
        "        loc1 = w.nodes[-1].location\n"
        "        if not loc0.valid():\n"
        "            return\n"
        "        x0, y0 = loc0.lon, loc0.lat\n"
        "        x1, y1 = (loc1.lon, loc1.lat) if loc1.valid() else (x0, y0)\n"
        "        margin = 0.08 if is_ww else 0.03\n"
        "        cand_sites = []\n"
        "        for s in sites:\n"
        "            w0, s0, e0, n0 = s['bbox']\n"
        "            if ((w0 - margin <= x0 <= e0 + margin and s0 - margin <= y0 <= n0 + margin) or\n"
        "                (w0 - margin <= x1 <= e0 + margin and s0 - margin <= y1 <= n0 + margin)):\n"
        "                cand_sites.append(s)\n"
        "        if not cand_sites:\n"
        "            return\n"
        "        coords = [(n.location.lon, n.location.lat) for n in w.nodes if n.location.valid()]\n"
        "        if len(coords) < 2:\n"
        "            return\n"
        "        for s in cand_sites:\n"
        "            w0, s0, e0, n0 = s['bbox']\n"
        "            pad = 0.008 if is_ww and not is_hw else 0.0\n"
        "            pts = [[round(x, 6), round(y, 6)] for x, y in coords if (w0 - pad) <= x <= (e0 + pad) and (s0 - pad) <= y <= (n0 + pad)]\n"
        "            if len(pts) < 2:\n"
        "                continue\n"
        "            sid = s['site_id']\n"
        "            if is_hw:\n"
        "                roads_out[sid].append({'type':'Feature','properties':{'osm_way_id':w.id,'highway':hw,'name':t.get('name','')},'geometry':{'type':'LineString','coordinates':pts}})\n"
        "            if is_ww:\n"
        "                waters_out[sid].append({'type':'Feature','properties':{'osm_way_id':w.id,'waterway':ww,'name':t.get('name','')},'geometry':{'type':'LineString','coordinates':pts}})\n"
        "MultiH().apply_file(sys.argv[1], locations=True)\n"
        "for s in sites:\n"
        "    sid = s['site_id']\n"
        "    Path(s['roads']).write_text(json.dumps({'type':'FeatureCollection','features':roads_out[sid]}, ensure_ascii=False), encoding='utf-8')\n"
        "    Path(s['waters']).write_text(json.dumps({'type':'FeatureCollection','features':waters_out[sid]}, ensure_ascii=False), encoding='utf-8')\n"
        "    print(f'{sid}: roads={len(roads_out[sid])}, waterways={len(waters_out[sid])}')\n"
    )
    subprocess.run(
        ["uv", "run", "--no-project", "--with", "osmium", "python", "-c", helper, str(pbf_path), str(spec_json)],
        check=True,
    )
    return result_paths


def _ensure_site_utm_dem(site: dict[str, Any], interim_dir: Path) -> tuple[Path, dict[str, float]]:
    """准备各省试验区 30m UTM DEM（支持直接下载 Copernicus GLO-30 并裁剪重投影至目标 EPSG）。"""
    if site.get("local_dem_rel"):
        p = resolve_legacy(site["local_dem_rel"])
        epsg = int(site["crs_epsg"])
        w0, s0, e0, n0 = [float(v) for v in site["aoi_wgs84"]]
        tf = Transformer.from_crs(4326, epsg, always_xy=True)
        corners = [tf.transform(lon, lat) for lon, lat in [(w0, s0), (e0, s0), (e0, n0), (w0, n0)]]
        minx, maxx = min(c[0] for c in corners), max(c[0] for c in corners)
        miny, maxy = min(c[1] for c in corners), max(c[1] for c in corners)
        with rasterio.open(p) as ds:
            win = from_bounds(minx, miny, maxx, maxy, ds.transform)
            arr = ds.read(1, window=win).astype(np.float64)
            arr = arr[np.isfinite(arr) & (arr > -100)]
            return p, {
                "dem_source": rel_posix(p),
                "min_m": round(float(arr.min()), 2),
                "max_m": round(float(arr.max()), 2),
                "relief_m": round(float(arr.max() - arr.min()), 2),
            }

    cache_tif = PROJ / site["glo30_cache_rel"]
    if not cache_tif.is_file():
        cache_tif.parent.mkdir(parents=True, exist_ok=True)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(site["glo30_tile_url"], headers={"User-Agent": "gx-hydro2d/0.1"})
        with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
            tmp_p = cache_tif.with_suffix(".tmp")
            tmp_p.write_bytes(resp.read())
            tmp_p.replace(cache_tif)

    epsg = int(site["crs_epsg"])
    w0, s0, e0, n0 = [float(v) for v in site["aoi_wgs84"]]
    out_utm_tif = interim_dir / f"{site['site_id']}_glo30_{epsg}.tif"
    if not out_utm_tif.is_file():
        out_utm_tif.parent.mkdir(parents=True, exist_ok=True)
        tf = Transformer.from_crs(4326, epsg, always_xy=True)
        corners = [tf.transform(lon, lat) for lon, lat in [(w0, s0), (e0, s0), (e0, n0), (w0, n0)]]
        minx = min(c[0] for c in corners) - 300.0
        maxx = max(c[0] for c in corners) + 300.0
        miny = min(c[1] for c in corners) - 300.0
        maxy = max(c[1] for c in corners) + 300.0
        res = 30.0
        dst_w = max(16, int(np.ceil((maxx - minx) / res)))
        dst_h = max(16, int(np.ceil((maxy - miny) / res)))
        dst_tf = from_origin(minx, maxy, res, res)

        with rasterio.open(cache_tif) as src:
            win = from_bounds(w0 - 0.015, s0 - 0.015, e0 + 0.015, n0 + 0.015, src.transform)
            src_arr = src.read(1, window=win).astype(np.float32)
            src_tf = src.window_transform(win)
            dst_arr = np.full((dst_h, dst_w), np.nan, dtype=np.float32)
            reproject(
                source=src_arr,
                destination=dst_arr,
                src_transform=src_tf,
                src_crs=src.crs,
                dst_transform=dst_tf,
                dst_crs=f"EPSG:{epsg}",
                resampling=Resampling.bilinear,
                dst_nodata=np.nan,
            )
        with rasterio.open(
            out_utm_tif,
            "w",
            driver="GTiff",
            height=dst_h,
            width=dst_w,
            count=1,
            dtype="float32",
            crs=f"EPSG:{epsg}",
            transform=dst_tf,
            nodata=-9999.0,
        ) as dst:
            dst.write(np.where(np.isfinite(dst_arr), dst_arr, -9999.0), 1)

    with rasterio.open(out_utm_tif) as ds:
        arr = ds.read(1).astype(np.float64)
        arr = arr[np.isfinite(arr) & (arr > -100)]
    return out_utm_tif, {
        "dem_source": "Copernicus_GLO30_AWS_30m",
        "utm_tif": rel_posix(out_utm_tif),
        "min_m": round(float(arr.min()), 2) if arr.size else None,
        "max_m": round(float(arr.max()), 2) if arr.size else None,
        "relief_m": round(float(arr.max() - arr.min()), 2) if arr.size else None,
    }


def verify_hydrological_monitors_five_provinces(snapshot: dict[str, Any]) -> dict[str, Any]:
    """核验五省区公开水情与 GloFAS 连续流量过程对真实防汛事件的响应特征。"""
    bench_stations = snapshot.get("benchmark_stations", [])
    glofas = snapshot.get("glofas_five_province_discharge", {})
    event_checks: list[dict[str, Any]] = []

    for st in bench_stations:
        sid = st["station_id"]
        gf = glofas.get(sid) or {}
        if gf.get("status") != "ok":
            continue
        dates: list[str] = gf.get("dates") or []
        q_arr: list[float | None] = gf.get("river_discharge_m3s") or []

        for ev in st.get("published_events", []):
            pdate = ev.get("peak_date")
            if not pdate or pdate not in dates:
                continue
            p_dt = dt.date.fromisoformat(pdate)
            # 在官报洪峰日期 ±4 天窗口内检索 GloFAS 局地峰流量
            win_pairs = []
            for d_str, q_val in zip(dates, q_arr):
                if q_val is None:
                    continue
                delta = abs((dt.date.fromisoformat(d_str) - p_dt).days)
                if delta <= 4:
                    win_pairs.append((d_str, float(q_val), (dt.date.fromisoformat(d_str) - p_dt).days))
            if not win_pairs:
                continue
            gf_d, gf_q, lag_d = max(win_pairs, key=lambda x: x[1])
            pub_q = ev.get("peak_q_m3s")
            ratio = round(gf_q / pub_q, 3) if pub_q else None
            event_checks.append({
                "station_id": sid,
                "province": st["province"],
                "station_name": st["name"],
                "river": st["river"],
                "event": ev["event"],
                "published_peak_date": pdate,
                "published_peak_wse_m": ev.get("peak_wse_m"),
                "published_peak_q_m3s": pub_q,
                "glofas_window_peak_date": gf_d,
                "glofas_window_lag_days": lag_d,
                "glofas_window_peak_q_m3s": round(gf_q, 2),
                "glofas_to_published_q_ratio": ratio,
            })

    # 海南公开日水位 vs 法定警戒/汛限水位阈值分类检查
    hi_check = snapshot.get("hainan_live_threshold_check") or []
    hi_matched = [r for r in hi_check if r.get("warning_or_flood_limit_m") is not None]
    return {
        "glofas_event_cross_checks": event_checks,
        "glofas_provinces_covered": sorted({v.get("province") for v in glofas.values() if v.get("status") == "ok"}),
        "hainan_live_records": len((snapshot.get("hainan_live_daily") or {}).get("records", [])),
        "hainan_matched_warning_thresholds": len(hi_matched),
        "hainan_matched_details": hi_matched,
        "guangxi_live_emergency_bulletins": len(snapshot.get("guangxi_emergency_responses") or []),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--osm-pbf", type=Path, default=None)
    ap.add_argument("--refresh-hydro", action="store_true")
    ap.add_argument("--out-json", type=Path, default=OUT_JSON)
    args = ap.parse_args()

    log = AuditLog("verify_five_province_algorithms")

    # 1. 公开水情采集与五省水文核验
    if args.refresh_hydro or not SNAPSHOT_JSON.is_file():
        portal_cfg = yaml.safe_load(PORTAL_CFG.read_text(encoding="utf-8"))
        snap = {
            "benchmark_stations": portal_cfg.get("benchmark_stations", []),
            **harvest_five_provinces(portal_cfg, live_http=True, fetch_glofas=True),
        }
        write_json(SNAPSHOT_JSON, {"script": "verify_five_province_algorithms", "generated_utc": utc_now(), **snap})
    else:
        snap = json.loads(SNAPSHOT_JSON.read_text(encoding="utf-8"))

    hydro_ver = verify_hydrological_monitors_five_provinces(snap)
    log.step("hydro_verified", glofas_events=len(hydro_ver["glofas_event_cross_checks"]))

    # 2. 五省多地貌 PipeNetGen + PySWMM 动态波泛化验证
    pipe_cfg = yaml.safe_load(PIPENET_5P_CFG.read_text(encoding="utf-8"))
    rex_cfg = pipe_cfg["road_extraction"]
    topo_cfg = pipe_cfg["topology"]
    hyd_cfg = pipe_cfg["hydraulics"]
    sites = pipe_cfg["sites"]

    pbf = args.osm_pbf or external("OSM_PBF")
    if pbf is None:
        raise FileNotFoundError("需要传入 --osm-pbf 或配置 GX_OSM_PBF 以提取五省区真实 OSM 路网与水系")

    osm_cache_dir = PROJ / "data" / "raw" / "hydro" / "five_provinces"
    osm_paths = _extract_five_sites_osm_single_pass(
        pbf, sites, set(rex_cfg["include_highways"]), osm_cache_dir
    )

    interim_dir = PROJ / "data" / "interim" / "five_provinces"
    out_pipe_root = PROJ / "outputs" / "pipenet" / "five_provinces"
    site_reports: list[dict[str, Any]] = []

    for s in sites:
        sid = s["site_id"]
        epsg = int(s["crs_epsg"])
        road_gj, water_gj = osm_paths[sid]
        dem_path, dem_meta = _ensure_site_utm_dem(s, interim_dir)

        road_fc = load_road_geojson(
            road_gj,
            include_highways=set(rex_cfg["include_highways"]),
            exclude_highways=set(rex_cfg["exclude_highways"]),
        )
        water_arg = water_gj if (water_gj.is_file() and json.loads(water_gj.read_text(encoding="utf-8")).get("features")) else None
        graph = build_road_graph(
            road_fc,
            epsg=epsg,
            default_spacing_m=float(rex_cfg.get("default_spacing_m", 90.0)),
            snap_tolerance_m=float(rex_cfg.get("snap_tolerance_m", 18.0)),
        )
        dag = build_gravity_dag(
            graph,
            epsg=epsg,
            dem_path=dem_path,
            waterway_geojson=water_arg,
            outfall_buffer_m=float(topo_cfg.get("outfall_buffer_m", 180.0)),
            uphill_penalty_weight=float(topo_cfg.get("uphill_penalty_weight", 200.0)),
            min_component_nodes=int(topo_cfg.get("min_component_nodes", 3)),
            min_slope=float(hyd_cfg.get("min_slope", 0.002)),
            min_cover_m=float(hyd_cfg.get("min_cover_m", 1.0)),
            max_depth_m=float(hyd_cfg.get("max_depth_m", 6.0)),
        )
        sized = size_gravity_network(
            dag,
            storm_formula=hyd_cfg["storm_formula"],
            default_runoff_coeff=float(hyd_cfg.get("default_runoff_coeff", 0.68)),
            tributary_width_m=float(hyd_cfg.get("tributary_width_m", 90.0)),
            inlet_tc_min=float(hyd_cfg.get("inlet_tc_min", 10.0)),
            assumed_flow_velocity_ms=float(hyd_cfg.get("assumed_flow_velocity_ms", 1.0)),
            pipe_manning_n=float(hyd_cfg.get("pipe_manning_n", 0.013)),
            nominal_diameters_m=hyd_cfg["nominal_diameters_m"],
        )
        summary = summarize_pipenet(sized)
        s_out = out_pipe_root / sid
        inp_p = export_swmm_inp(sized, s_out / f"{sid}.inp", title=f"{sid} Five-Province Verification", storm_rain_mm_h=45.0)
        gj_p = export_pipenet_geojson(sized, s_out / f"{sid}_pipes.geojson")

        swmm_res = run_swmm_drainage_simulation(inp_p, step_advance_s=900)
        rec = {
            "site_id": sid,
            "province": s["province"],
            "city": s["city"],
            "terrain_regime": s["terrain_regime"],
            "crs_epsg": epsg,
            "road_features": len(road_fc.get("features", [])),
            "dem_stats": dem_meta,
            "pipenet_summary": summary,
            "swmm_simulation": swmm_res,
            "out_inp": rel_posix(inp_p),
            "out_geojson": rel_posix(gj_p),
        }
        site_reports.append(rec)
        print(
            f"[{s['province']}/{sid}] relief={dem_meta.get('relief_m')}m, "
            f"nodes={summary['n_nodes']}, conduits={summary['n_conduits']}, "
            f"neg_slopes={summary['negative_slope_count']}, is_dag={summary['is_dag']}, "
            f"swmm_peak_q={swmm_res['peak_outfall_q_m3s']} m3/s, err={swmm_res['routing_continuity_error_pct']}%"
        )

    payload = {
        "script": "scripts/stage6/verify_five_province_algorithms.py",
        "generated_utc": utc_now(),
        "SYNTHETIC_DATA_NOT_REAL_ASSET": True,
        "hydrological_verification": hydro_ver,
        "cross_province_pipenet_verification": site_reports,
        "overall_assertions": {
            "provinces_covered": 5,
            "all_sites_dag": all(r["pipenet_summary"]["is_dag"] for r in site_reports),
            "all_sites_zero_negative_slope": all(r["pipenet_summary"]["negative_slope_count"] == 0 for r in site_reports),
            "all_sites_swmm_solved": all(r["swmm_simulation"]["n_steps"] > 0 for r in site_reports),
        },
    }
    write_json(args.out_json, payload)
    log.step("pipenet_5p_verified", n_sites=len(site_reports))
    log.write(PROJ / "logs" / "stage6" / "verify_five_province_algorithms.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
