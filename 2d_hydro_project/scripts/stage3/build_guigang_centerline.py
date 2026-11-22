"""阶段3：贵港郁江河道中心线构建（OSM Overpass -> Sentinel-2 低水景 -> 参数化直线回退链）。

依据 ADR-0012 / plans/无实测水下地形条件下的河槽水力刻蚀与几何重构算法（针对贵港干流漫滩）.txt：
- S1 常水掩膜与 DEM 山谷追踪两条路线已被 ADR-0011 决策 3 定量否决，本脚本不使用它们做骨架；
- 中心线首选 OSM Overpass（用户 2026-11-07 决策）。实测 https 直连被重置（SSL exit 35），
  但明文 http://overpass-api.de 可用（2026-11-07 HTTP 200 实证），故 Overpass 查询走 http；
- 回退链：OSM 不可达 -> Sentinel-2 2024-10-03 低水景 NDWI 水面骨架（AWS COG 匿名可达，
  见 plans/宁明_贵港_公开可见光影像检索结果.md 的 206 实证）-> 参数化直线（v3 口径）。
  回退只影响中心线来源登记，不改变下游刻蚀参数（priors JSON 预注册）。

产出：
  data/interim/guigang/yujiang_centerline.geojson   LineString（EPSG:4326），属性含 32649 投影
                                                    上下游端点与注入点建议；
  data/catalog/guigang_centerline_audit.json        来源、链组装、长度、边界穿越、锚点距离、
                                                    与 S1 常水的距离分布（QA 独立核验）、回退登记。

用法：uv run python scripts/stage3/build_guigang_centerline.py [--source osm|s2|line] [--refresh]
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))
from scripts.common.grid_io import AuditLog, sha256_file, write_json  # noqa: E402

OSM_RAW = PROJ / "data" / "raw" / "osm_guigang" / "rivers_aoi_20261107.json"
OUT_GEOJSON = PROJ / "data" / "interim" / "guigang" / "yujiang_centerline.geojson"
OUT_AUDIT = PROJ / "data" / "catalog" / "guigang_centerline_audit.json"
S1W_PRE = PROJ / "data" / "processed" / "s1_water" / "guigang_20240905T105022_water20m.tif"

AOI = (109.30, 22.98, 109.95, 23.32)  # 西 南 东 北（与 guigang_scenario 一致）
ANCHOR = ("贵港市区郁江段(暂定)", 109.60, 23.11)  # 诊断用，不作硬门（实测偏离真河 ~5 km）
# 基线 dem.asc 走廊范围（100 m 网格，xll/yll 来自基线 run 头，实测 2026-11-07）
CORRIDOR_32649 = (325737.9, 2542315.3, 325737.9 + 669 * 100.0, 2542315.3 + 370 * 100.0)
OVERPASS_URLS = (
    "http://overpass-api.de/api/interpreter",  # https 直连被重置（实测 SSL exit 35）
)
OVERPASS_QUERY = (
    "[out:json][timeout:120];"
    'way["waterway"="river"](22.90,109.15,23.40,110.10);out geom;'
)
S2_B03 = ("https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/"
          "49/Q/CF/2024/10/S2B_49QCF_20241003_0_L2A/B03.tif")
S2_B08 = ("https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/"
          "49/Q/CF/2024/10/S2B_49QCF_20241003_0_L2A/B08.tif")
INJECTION_INSET_M = 500.0  # 注入点沿河入域后内推距离（D4 教训：不贴 FREE 边界）


def _dist_m(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)


def _dist_ll(alon: float, alat: float, blon: float, blat: float) -> float:
    """经纬度（度）空间的等距圆柱近似距离（米）。仅用于 way 端点匹配与诊断。"""
    dx = (alon - blon) * 111320.0 * math.cos(math.radians(23.1))
    dy = (alat - blat) * 110570.0
    return math.hypot(dx, dy)


# ---------------------------------------------------------------- OSM 路线 ----
def fetch_osm(refresh: bool) -> dict:
    if OSM_RAW.is_file() and not refresh:
        return json.loads(OSM_RAW.read_text(encoding="utf-8"))
    import urllib.parse
    import urllib.request

    OSM_RAW.parent.mkdir(parents=True, exist_ok=True)
    data = urllib.parse.urlencode({"data": OVERPASS_QUERY}).encode()
    last_err: Exception | None = None
    for url in OVERPASS_URLS:
        try:
            req = urllib.request.Request(
                url, data=data,
                headers={"User-Agent": "gx-hydro2d/0.1 (channel prior research)"},
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                payload = resp.read()
            doc = json.loads(payload)
            OSM_RAW.write_bytes(payload)
            return doc
        except Exception as exc:  # noqa: BLE001 —— 回退链要捕获全部网络错误
            last_err = exc
    raise SystemExit(f"Overpass 全部地址失败：{last_err!r}")


def osm_yujiang_chain(doc: dict) -> tuple[list[list[tuple[float, float]]], list[int]]:
    """按名称取郁江 way，端点匹配组装为上下游有序链（本域 4 段，2026-11-07 实测）。"""
    ways = []
    for el in doc.get("elements", []):
        tags = el.get("tags", {})
        if tags.get("name") != "郁江" or tags.get("waterway") != "river":
            continue
        pts = [(p["lon"], p["lat"]) for p in el.get("geometry", [])]
        if len(pts) >= 2:
            ways.append({"id": el["id"], "pts": pts})
    if not ways:
        raise SystemExit("Overpass 结果中没有 name=郁江 的 river way")
    # 端点匹配（共享 OSM 节点 => 0 m；放宽 5 m；注意这里是经纬度空间，用 _dist_ll）
    TOL = 5.0
    used = [False] * len(ways)
    chain = [ways[0]]
    used[0] = True
    while True:
        head_s, head_e = chain[0]["pts"][0], chain[-1]["pts"][-1]
        extended = False
        for i, w in enumerate(ways):
            if used[i]:
                continue
            ws, we = w["pts"][0], w["pts"][-1]
            if _dist_ll(*head_s, *we) < TOL:
                chain.insert(0, w)
            elif _dist_ll(*head_e, *ws) < TOL:
                chain.append(w)
            elif _dist_ll(*head_s, *ws) < TOL:
                chain.insert(0, {"id": w["id"], "pts": list(reversed(w["pts"]))})
            elif _dist_ll(*head_e, *we) < TOL:
                chain.append({"id": w["id"], "pts": list(reversed(w["pts"]))})
            else:
                continue
            used[i] = True
            extended = True
            break
        if not extended:
            break
    unused = [ways[i]["id"] for i, u in enumerate(used) if not u]
    return [[(p[0], p[1]) for p in w["pts"]] for w in chain], unused


# ------------------------------------------------------- Sentinel-2 路线 ----
def s2_ndwi_centerline(log: AuditLog) -> list[tuple[float, float]]:
    """Sentinel-2 2024-10-03 低水景 NDWI 水面 + 直线脊柱吸附质心（回退路线 2）。

    脊柱 = 参数化直线（回退路线 3 的几何），逐节点在 ±1.5 km 盘内取 NDWI>0.25 水像元
    质心作修正；无水节点保持脊柱位置并登记。产出 4326 经纬度点列。
    """
    tf = Transformer.from_crs(4326, 32649, always_xy=True)
    tf_inv = Transformer.from_crs(32649, 4326, always_xy=True)
    spine_ll = param_line_ll(200.0)
    spine = np.array([tf.transform(x, y) for x, y in spine_ll])
    x0, y0, x1, y1 = CORRIDOR_32649
    pad = 1500.0

    def read_band(url: str) -> tuple[np.ndarray, object]:
        with rasterio.open(f"/vsicurl/{url}") as src:
            if src.crs is None or src.crs.to_epsg() != 32649:
                raise SystemExit(f"{url} 坐标系非 EPSG:32649（实得 {src.crs}），窗口换算不成立")
            win = rasterio.windows.from_bounds(x0 - pad, y0 - pad, x1 + pad, y1 + pad,
                                               transform=src.transform).round_offsets().round_lengths()
            win = win.intersection(rasterio.windows.Window(0, 0, src.width, src.height))
            arr = src.read(1, window=win).astype("float32")
            tr = src.window_transform(win)
        return arr, tr
    log.step("s2_read_start", b03=S2_B03, b08=S2_B08)
    green, tr_g = read_band(S2_B03)
    nir, tr_n = read_band(S2_B08)
    if green.shape != nir.shape:
        raise SystemExit("B03/B08 窗口形状不一致（重采样差异），S2 回退需人工复核")
    ndwi = (green - nir) / np.where((green + nir) == 0, np.nan, green + nir)
    water = np.nan_to_num(ndwi, nan=-1.0) > 0.25
    wys, wxs = np.nonzero(water)
    wpx = np.column_stack([tr_g.c + (wxs + 0.5) * tr_g.a,
                           tr_g.f + (wys + 0.5) * tr_g.e])
    log.step("s2_water_px", count=int(water.sum()))
    out: list[tuple[float, float]] = []
    no_water_nodes = 0
    for sx, sy in spine:
        near = wpx[(np.abs(wpx[:, 0] - sx) < 1500) & (np.abs(wpx[:, 1] - sy) < 1500)]
        if len(near) == 0:
            out.append(tf_inv.transform(sx, sy))
            no_water_nodes += 1
            continue
        cx, cy = float(near[:, 0].mean()), float(near[:, 1].mean())
        out.append(tf_inv.transform(cx, cy))
    log.step("s2_snap_done", no_water_nodes=no_water_nodes)
    return out


# ------------------------------------------------------ 参数化直线（回退） ----
def param_line_ll(step_m: float = 200.0) -> list[tuple[float, float]]:
    """v3 参数化直线（过锚点走向 100°/280°），裁到 AOI，返回 4326 点列。

    与 guigang_scenario.py prep() 的直线几何一致（锚点、走向同源）。
    """
    tf = Transformer.from_crs(4326, 32649, always_xy=True)
    tf_inv = Transformer.from_crs(32649, 4326, always_xy=True)
    ax, ay = tf.transform(ANCHOR[1], ANCHOR[2])
    x0, y0, x1, y1 = CORRIDOR_32649
    dc, dr = math.sin(math.radians(100.0)), -math.cos(math.radians(100.0))
    L = 2.0 * max(x1 - x0, y1 - y0)
    n = int(L / step_m)
    out = []
    for k in range(-n, n + 1):
        x, y = ax + k * step_m * dc, ay + k * step_m * dr
        if x0 <= x <= x1 and y0 <= y <= y1:
            out.append(tf_inv.transform(x, y))
    return out


# ---------------------------------------------------------------- 共用处理 ----
def clip_polyline(pts: np.ndarray, rect: tuple[float, float, float, float]) -> np.ndarray:
    """Liang-Barsky 逐段裁剪到矩形（投影坐标，米）。"""
    x0, y0, x1, y1 = rect
    out: list[tuple[float, float]] = []
    for (xa, ya), (xb, yb) in zip(pts, pts[1:]):
        t0, t1 = 0.0, 1.0
        dx, dy = xb - xa, yb - ya
        ok = True
        for p, q in ((-dx, xa - x0), (dx, x1 - xa), (-dy, ya - y0), (dy, y1 - ya)):
            if p == 0:
                if q < 0:
                    ok = False
                    break
            else:
                t = q / p
                if p < 0:
                    t0 = max(t0, t)
                else:
                    t1 = min(t1, t)
        if ok and t0 < t1:
            out.append((xa + t0 * dx, ya + t0 * dy))
            out.append((xa + t1 * dx, ya + t1 * dy))
    if not out:
        return np.empty((0, 2))
    dedup = [out[0]]
    for p in out[1:]:
        if _dist_m(p[0], p[1], *dedup[-1]) > 1e-6:
            dedup.append(p)
    return np.array(dedup)


def densify_smooth(pts_32649: np.ndarray, step_m: float = 100.0,
                   smooth_win: int = 5) -> np.ndarray:
    seg = []
    for (x1, y1), (x2, y2) in zip(pts_32649, pts_32649[1:]):
        L = _dist_m(x1, y1, x2, y2)
        n = max(1, int(round(L / step_m)))
        for k in range(n):
            seg.append((x1 + (x2 - x1) * k / n, y1 + (y2 - y1) * k / n))
    seg.append(tuple(pts_32649[-1]))
    arr = np.array(seg)
    if smooth_win >= 3:
        ker = np.ones(smooth_win) / smooth_win
        pad = smooth_win // 2
        for dim in (0, 1):
            col = arr[:, dim]
            col_p = np.pad(col, pad, mode="edge")
            arr[:, dim] = np.convolve(col_p, ker, mode="valid")[: len(arr)]
    # 平滑后两端点吸附回原端点（避免边界内缩）
    arr[0], arr[-1] = pts_32649[0], pts_32649[-1]
    return arr


def s1_distance_qa(pts_32649: np.ndarray, log: AuditLog) -> dict:
    """走廊内 S1 09-05 常水像元到中心线的距离分布（独立 QA，不参与几何构建）。"""
    if not S1W_PRE.is_file():
        return {"status": "s1_file_missing"}
    with rasterio.open(S1W_PRE) as src:
        w = src.read(1)
        b = src.bounds
        tr = src.transform
    ys, xs = np.nonzero(w == 1)
    wx = b.left + (xs + 0.5) * tr.a
    wy = b.top + (ys + 0.5) * tr.e
    x0, y0, x1, y1 = CORRIDOR_32649
    inbox = (wx >= x0) & (wx < x1) & (wy >= y0) & (wy < y1)
    P = np.column_stack([wx[inbox], wy[inbox]])
    if len(P) == 0:
        return {"status": "no_water_in_corridor"}
    seg = []
    for (xa, ya), (xb, yb) in zip(pts_32649, pts_32649[1:]):
        L = _dist_m(xa, ya, xb, yb)
        n = max(1, int(L / 50))
        for k in range(n + 1):
            seg.append((xa + (xb - xa) * k / n, ya + (yb - ya) * k / n))
    seg = np.array(seg)
    dmin = np.full(len(P), np.inf)
    for i in range(0, len(seg), 2000):
        chunk = seg[i:i + 2000]
        dmin = np.minimum(dmin, np.sqrt(((P[:, None, :] - chunk[None, :, :]) ** 2).sum(-1)).min(1))
    fin = dmin[np.isfinite(dmin)]
    qa = {
        "s1_water_px_in_corridor": int(len(P)),
        "s1_water_area_km2": round(float(len(P)) * 0.02 * 0.02, 3),
        "median_dist_m": round(float(np.median(fin)), 1),
        "p90_dist_m": round(float(np.percentile(fin, 90)), 1),
        "share_within_300m": round(float((fin <= 300).mean()), 4),
        "px_beyond_500m": int((fin > 500).sum()),
        "note": ">500m 像元为坑塘等非河道水体（ADR-0011 决策 3 碎块结论一致）",
    }
    log.step("s1_qa", **qa)
    return qa


def boundary_crossings(pts_32649: np.ndarray) -> dict:
    x0, y0, x1, y1 = CORRIDOR_32649
    first, last = pts_32649[0], pts_32649[-1]
    def edge_of(p):
        tol = 1.0
        tags = []
        if abs(p[0] - x0) < tol: tags.append("W")
        if abs(p[0] - x1) < tol: tags.append("E")
        if abs(p[1] - y0) < tol: tags.append("S")
        if abs(p[1] - y1) < tol: tags.append("N")
        return "+".join(tags) or "interior"
    return {"first_point_edge": edge_of(first), "last_point_edge": edge_of(last)}


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=("auto", "osm", "s2", "line"), default="auto")
    ap.add_argument("--refresh", action="store_true", help="强制重新下载 Overpass 数据")
    args = ap.parse_args()

    log = AuditLog("scripts/stage3/build_guigang_centerline.py")
    log.add_input("s1_water_pre", S1W_PRE)
    tf = Transformer.from_crs(4326, 32649, always_xy=True)
    tf_inv = Transformer.from_crs(32649, 4326, always_xy=True)

    source = args.source
    fallbacks_tried: list[str] = []
    pts_ll: list[tuple[float, float]] | None = None
    meta_src: dict = {}

    if source in ("auto", "osm"):
        try:
            doc = fetch_osm(args.refresh)
            log.add_input("osm_raw", OSM_RAW)
            chain, unused = osm_yujiang_chain(doc)
            log.step("osm_chain", ways=len(chain), nodes=sum(len(c) for c in chain),
                     unused_named_ways=unused)
            pts_ll = [p for c in chain for p in c]
            meta_src = {"source": "osm_overpass", "raw_file": str(OSM_RAW),
                        "ways_in_chain": [len(c) for c in chain],
                        "note": "name=郁江 river way 端点匹配成链（投影后裁剪到走廊）"}
        except SystemExit as exc:
            if source == "osm":
                raise
            fallbacks_tried.append(f"osm: {exc}")
            log.warn(f"OSM 路线失败，回退：{exc}")

    if pts_ll is None and source in ("auto", "s2"):
        try:
            pts_ll = s2_ndwi_centerline(log)
            meta_src = {"source": "sentinel2_ndwi_20241003", "scene": "S2B_49QCF_20241003_0_L2A",
                        "note": "NDWI>0.25 水面 + 参数化直线脊柱 ±1.5 km 吸附质心"}
        except SystemExit as exc:
            if source == "s2":
                raise
            fallbacks_tried.append(f"s2: {exc}")
            log.warn(f"S2 路线失败，回退：{exc}")

    if pts_ll is None:
        pts_ll = param_line_ll()
        meta_src = {"source": "param_line_v3",
                    "note": "参数化直线（锚点 109.60,23.11 走向 100°，guigang_scenario v3 同源）"}

    pts = np.array([tf.transform(x, y) for x, y in pts_ll])
    pts = clip_polyline(pts, CORRIDOR_32649)
    if len(pts) < 2:
        raise SystemExit("中心线在走廊内不足 2 点")
    pts = densify_smooth(pts, step_m=100.0, smooth_win=5)
    log.step("centerline_built", nodes=len(pts),
             length_km=round(float(sum(_dist_m(*a, *b) for a, b in zip(pts, pts[1:]))) / 1000, 3))

    # 注入点：沿路径从上游端入域 500 m 处（D4 教训：不贴 FREE 边界）
    acc = 0.0
    inj = pts[0]
    for (xa, ya), (xb, yb) in zip(pts, pts[1:]):
        L = _dist_m(xa, ya, xb, yb)
        if acc + L >= INJECTION_INSET_M:
            t = (INJECTION_INSET_M - acc) / L
            inj = (xa + (xb - xa) * t, ya + (yb - ya) * t)
            break
        acc += L

    ax, ay = tf.transform(ANCHOR[1], ANCHOR[2])
    anchor_d = min(_dist_m(px, py, ax, ay) for px, py in pts)
    crossings = boundary_crossings(pts)
    qa = s1_distance_qa(pts, log)

    # 长度与端点
    length_m = float(sum(_dist_m(*a, *b) for a, b in zip(pts, pts[1:])))
    up_ll, dn_ll = tf_inv.transform(*pts[0]), tf_inv.transform(*pts[-1])
    inj_ll = tf_inv.transform(*inj)

    props = {
        "name": "郁江贵港段水力约束重构中心线",
        "source": meta_src,
        "fallbacks_tried": fallbacks_tried,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "crs_4326_note": "GeoJSON 坐标为 EPSG:4326；消费方用 pyproj 转 EPSG:32649",
        "upstream_endpoint_32649": [round(pts[0][0], 2), round(pts[0][1], 2)],
        "downstream_endpoint_32649": [round(pts[-1][0], 2), round(pts[-1][1], 2)],
        "upstream_injection_32649": [round(float(inj[0]), 2), round(float(inj[1]), 2)],
        "injection_inset_m": INJECTION_INSET_M,
        "length_m": round(length_m, 1),
        "node_count": len(pts),
        "densify_step_m": 100.0,
        "smooth_window": 5,
        "boundary_crossings": crossings,
        "anchor_distance_m": round(float(anchor_d), 1),
        "anchor_note": "预注册锚点 (109.60,23.11) 为暂定值；与中心线最近距离见 anchor_distance_m（OSM 实测郁江不在锚点处），锚点仅诊断，不作硬门",
        "s1_qa": qa,
    }
    feature = {
        "type": "Feature",
        "properties": props,
        # geometry 存最终（投影裁剪+稠密化+平滑后）折线的 4326 顶点，消费方转回 32649 可复现同一几何
        "geometry": {"type": "LineString",
                     "coordinates": [
                         [round(float(x), 7), round(float(y), 7)]
                         for x, y in (tf_inv.transform(px, py) for px, py in pts)
                     ]},
    }
    fc = {"type": "FeatureCollection", "features": [feature]}
    OUT_GEOJSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_GEOJSON.write_text(json.dumps(fc, ensure_ascii=False, indent=1), encoding="utf-8")

    log.add_input("output_geojson", OUT_GEOJSON, hash_file=True)
    log.param(source=meta_src.get("source"), fallbacks=fallbacks_tried,
              nodes=len(pts), length_m=round(length_m, 1),
              anchor_distance_m=round(float(anchor_d), 1),
              injection_32649=[round(float(inj[0]), 2), round(float(inj[1]), 2)],
              boundary_crossings=crossings, s1_qa=qa)
    log.step("done", geojson=str(OUT_GEOJSON))
    OUT_AUDIT.parent.mkdir(parents=True, exist_ok=True)
    log.write(OUT_AUDIT)

    print(f"中心线: 来源={meta_src.get('source')} 节点={len(pts)} 长度={length_m/1000:.2f} km")
    print(f"  上游端 {up_ll[0]:.4f},{up_ll[1]:.4f} -> 下游端 {dn_ll[0]:.4f},{dn_ll[1]:.4f}")
    print(f"  注入点(入域500m): {inj_ll[0]:.4f},{inj_ll[1]:.4f}  边界: {crossings}")
    print(f"  锚点距离(诊断): {anchor_d:.0f} m  S1 QA: {qa}")
    print(f"  产出: {OUT_GEOJSON}")
    print(f"  审计: {OUT_AUDIT}")


if __name__ == "__main__":
    main()
