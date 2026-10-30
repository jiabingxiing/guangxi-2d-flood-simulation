"""生成 Web 前端联调用的 mock 发布包（仅前端开发用，不属于正式交付）。

数据来源：父项目真实模型输出（只读）
  - outputs/jiulong/2024-09-yagi/scen_free_200m_sv6h       21 帧
  - outputs/ningming/2026-08-zitan/scen_q6000_n100_100m    19 帧
  - outputs/ningming/2026-08-zitan/scen_q4000_100m         19 帧（run 切换测试）
  - outputs/guigang/2024-09-yagi/scen_yagi_q11500_200m     57 帧（2×2 parts 测试）
真实水深帧 → 预着色 PNG（按 catalog 固定色带）+ float32 分块 + 统计；
DEM 用 run 目录 dem.asc；SAR 用 data/processed/s1_water/*_water20m.tif 重投影到模型网格。
光学层为 mock 合成 RGB（仅联调图层渲染用，catalog 里明确标注）。

用法：
  .venv/Scripts/python.exe web/.dev/make_mock_bundle.py
输出：web/.dev/mock_bundle/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from pyproj import Transformer
from rasterio.warp import Resampling, reproject

BLOCK = 256
ROOT = Path(__file__).resolve().parents[4]  # 任务根目录：……/1023 久隆 贵港 宁明 二维水动力模拟
PARENT = ROOT / "2d_hydro_project"
EXAMPLES = ROOT / "2d_hydro_project" / "flood_visualization" / "docs" / "examples"

TIME_ZONE = "Asia/Shanghai"

CASE_CONFIG = {
    "jiulong": {
        "run": "scen_free_200m_sv6h",
        "run_dir": PARENT / "outputs/jiulong/2024-09-yagi/scen_free_200m_sv6h",
        "crs": "EPSG:32649",
        "time_origin": None,
        "origin_status": "relative_only_candidate",
        "origin_note": "旧 meta 无规范 t0；该起点来自父项目脚本构造（2024-09-05 00:00 北京时间），仅作显示用",
        "timestamps": False,
        "split": (1, 1),
        "sar": {
            "status": "local_unchecked",
            "asset_id": "jiulong_s1_20240917",
            "label": "SAR 后向散射（2024-09-17 事后）",
            "summary": "S1 20 m 后向散射；不是水深，不能共用深度图例",
            "note": "过境时刻与模拟事件不同期；仅范围证据；mock 包未导出图像",
            "acquired_at": "2024-09-17T10:50:23+08:00",
            "tif": PARENT / "data/processed/s1_water/jiulong_20240917T105023_water20m.tif",
        },
        "optical": {
            "status": "ready",  # mock：临时置 ready 以便联调图层渲染；真实基线为 local_unchecked
            "asset_id": "jiulong_rgb_20260403",
            "label": "可见光背景（2026-04-03 本地影像）【mock】",
            "summary": "【mock】合成 RGB，仅用于联调图层渲染与透明度；真实包应为 local_unchecked 并禁用",
            "note": "2026 影像不能当作 2024 事件同期观测；mock 数据",
            "acquired_at": "2026-04-03T00:00:00+08:00",
        },
        "extra_runs": [],
    },
    "ningming": {
        "run": "scen_q6000_n100_100m",
        "run_dir": PARENT / "outputs/ningming/2026-08-zitan/scen_q6000_n100_100m",
        "crs": "EPSG:32648",
        "time_origin": None,
        "origin_status": "relative_only_candidate",
        "origin_note": "上游 Q 三角线构造起点（2026-08-21 00:00 北京时间），旧 meta 无规范 t0；仅作显示用",
        "timestamps": False,
        "split": (1, 1),
        "sar": {
            "status": "ready",
            "asset_id": "ningming_gf3_20260824",
            "label": "SAR 水体（2026-08-24）",
            "summary": "mock 包用 S1 2024-09-12 水体掩膜重投影；真实包应为 GF-3 2026-08-24 水体掩膜",
            "note": "SAR 后向散射不是水深，不能共用深度图例",
            "acquired_at": "2026-08-24T00:00:00+08:00",
            "tif": PARENT / "data/processed/s1_water/ningming_20240912T225045_water20m.tif",
        },
        "optical": {
            "status": "remote_verified",
            "asset_id": "ningming_s2_20260903",
            "label": "可见光背景（S2 2026-09-03）",
            "summary": "Sentinel-2 L2A TCI，AOI 有效覆盖 100%、SCL 云占比 0%（自动分类）；晚于洪峰，仅作背景",
            "note": "尚未导入本地发布包，状态 remote_verified，默认禁用",
            "acquired_at": "2026-09-03T11:32:14+08:00",
        },
        "extra_runs": [
            {
                "run": "scen_q4000_100m",
                "run_dir": PARENT / "outputs/ningming/2026-08-zitan/scen_q4000_100m",
                "label": "基线对照 Q4000（T+0–108 h）",
                "role": "comparison",
            }
        ],
    },
    "guigang": {
        "run": "scen_yagi_q11500_200m",
        "run_dir": PARENT / "outputs/guigang/2024-09-yagi/scen_yagi_q11500_200m",
        "crs": "EPSG:32649",
        "time_origin": "2024-09-03T00:00:00+08:00",
        "origin_status": "model_schedule_confirmed",
        "origin_note": "meta.json 记录 t0=2024-09-03T00（前 2 日基流充槽），为模型排程确认起点",
        "timestamps": True,
        "split": (2, 2),  # mock：用 200 m 网格模拟 2×2 分块贴图
        "sar": {
            "status": "partial",
            "asset_id": "guigang_s1_20240912",
            "label": "SAR 后向散射（S1 2024-09-12）",
            "summary": "S1 20 m 后向散射，仅覆盖模型域东部一部分（mock 包按真实 SAR 足迹裁剪）",
            "note": "必须显示足迹；不宣称全市覆盖；SAR 后向散射不是水深",
            "acquired_at": "2024-09-12T10:42:22+08:00",
            "tif": PARENT / "data/processed/s1_water/guigang_20240912T104222_water20m.tif",
        },
        "optical": {
            "status": "remote_verified",
            "asset_id": "guigang_s2_20241003",
            "label": "可见光背景（S2 2024-10-03）",
            "summary": "Sentinel-2 L2A TCI，AOI 有效覆盖 96.84%；事件后约 3 周背景，不是洪峰观测",
            "note": "尚未导入本地发布包，状态 remote_verified，默认禁用",
            "acquired_at": "2024-10-03T11:21:43+08:00",
        },
        "extra_runs": [],
    },
}


# ---------------------------------------------------------------- ASCII 读写
def read_ascii_header(path: Path) -> dict:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        head = {}
        for _ in range(6):
            parts = f.readline().split()
            head[parts[0].strip().lower()] = float(parts[1])
    return {
        "cols": int(head["ncols"]),
        "rows": int(head["nrows"]),
        "xll": head["xllcorner"],
        "yll": head["yllcorner"],
        "cell": head["cellsize"],
        "nodata": head["nodata_value"],
    }


def read_ascii_grid(path: Path, header: dict) -> np.ndarray:
    arr = np.loadtxt(path, skiprows=6, dtype=np.float64)
    return arr.reshape(header["rows"], header["cols"])


def transform_of(header: dict) -> list[float]:
    ytop = header["yll"] + header["rows"] * header["cell"]
    return [header["cell"], 0.0, header["xll"], 0.0, -header["cell"], ytop]


def corners_lonlat(crs: str, tr6: list[float], rows: int, cols: int) -> list[list[float]]:
    a, _, c, _, e, f = tr6
    x0 = c
    x1 = c + a * cols
    y0 = f
    y1 = f + e * rows
    tr = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return [[round(lon, 8), round(lat, 8)] for lon, lat in (tr.transform(x, y) for x, y in pts)]


def part_corners(crs: str, tr6: list[float], r0: int, r1: int, c0: int, c1: int) -> list[list[float]]:
    a, b, c, d, e, f = tr6
    x0 = a * c0 + b * r0 + c
    x1 = a * c1 + b * r0 + c
    y0 = d * c0 + e * r0 + f
    y1 = d * c0 + e * r1 + f
    tr = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return [[round(lon, 8), round(lat, 8)] for lon, lat in (tr.transform(x, y) for x, y in pts)]


# ---------------------------------------------------------------- 着色
def hex_to_rgb(h: str) -> tuple[int, int, int]:
    return (int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16))


def depth_rgba(values: np.ndarray, classes: list[dict], transparent_below: float) -> np.ndarray:
    rgba = np.zeros((*values.shape, 4), dtype=np.uint8)
    v = np.where(np.isfinite(values), values, np.nan)
    for cls in classes:
        lo = cls["min_m"]
        hi = cls["max_m"]
        m = (v >= lo) & (v > transparent_below)
        if hi is not None:
            m &= v < hi
        rgba[m, 0:3] = hex_to_rgb(cls["color"])
        rgba[m, 3] = 235
    # 介于 transparent_below 与首类下界之间的值给首类色但 alpha 减弱
    first = classes[0]
    m = (v > transparent_below) & (v < first["min_m"])
    rgba[m, 0:3] = hex_to_rgb(first["color"])
    rgba[m, 3] = 120
    return rgba


def terrain_rgba(dem: np.ndarray, valid: np.ndarray, stops: list[str]) -> np.ndarray:
    rows, cols = dem.shape
    rgba = np.zeros((rows, cols, 4), dtype=np.uint8)
    if not valid.any():
        return rgba
    vmin = float(np.nanmin(dem[valid]))
    vmax = float(np.nanmax(dem[valid]))
    span = max(vmax - vmin, 1e-6)
    norm = np.clip((dem - vmin) / span, 0.0, 1.0)
    cols_rgb = np.zeros((rows, cols, 3), dtype=np.float64)
    rgb_stops = [np.array(hex_to_rgb(s), dtype=np.float64) for s in stops]
    n = len(rgb_stops) - 1
    for k in range(n):
        lo = k / n
        hi = (k + 1) / n
        m = (norm >= lo) & (norm <= hi)
        t = ((norm - lo) / max(hi - lo, 1e-9))[..., None]
        cols_rgb[m] = rgb_stops[k] * (1 - t[m]) + rgb_stops[k + 1] * t[m]
    rgba[..., :3] = np.clip(cols_rgb, 0, 255).astype(np.uint8)
    rgba[..., 3] = np.where(valid, 215, 0).astype(np.uint8)
    return rgba


# ---------------------------------------------------------------- 分块写入
def write_blocks(arr: np.ndarray, out_dir: Path) -> None:
    rows, cols = arr.shape
    for by in range(math.ceil(rows / BLOCK)):
        r0, r1 = by * BLOCK, min(rows, (by + 1) * BLOCK)
        for bx in range(math.ceil(cols / BLOCK)):
            c0, c1 = bx * BLOCK, min(cols, (bx + 1) * BLOCK)
            blk = np.ascontiguousarray(arr[r0:r1, c0:c1], dtype="<f4")
            p = out_dir / str(by) / f"{bx}.bin"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(blk.tobytes(order="C"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ts_iso(origin: str | None, sim_seconds: float) -> str | None:
    if origin is None:
        return None
    return (datetime.fromisoformat(origin) + timedelta(seconds=sim_seconds)).isoformat()


def load_run(run_dir: Path, crs: str, examples: dict):
    """读取 run：header、逐帧水深、DEM、res.max、海域掩膜、SAR 掩膜。"""
    results = run_dir / "results"
    wds = sorted(results.glob("res-*.wd"))
    if not wds:
        raise SystemExit(f"no res-*.wd in {results}")
    header = read_ascii_header(wds[0])
    rows, cols = header["rows"], header["cols"]
    frames, shas = [], []
    for wd in wds:
        g = read_ascii_grid(wd, header)
        g = np.where(np.isclose(g, header["nodata"]), np.nan, g)
        frames.append(g.astype(np.float32))
        shas.append(sha256_file(wd))
    dem = read_ascii_grid(run_dir / "dem.asc", read_ascii_header(run_dir / "dem.asc"))
    dem = np.where(np.isclose(dem, -9999.0), np.nan, dem).astype(np.float32)
    # 有效像元：DEM 非 NaN 且非海域
    sea = np.zeros((rows, cols), dtype=bool)
    sea_path = run_dir / "sea_mask.npy"
    if sea_path.exists():
        sea = np.load(sea_path).astype(bool)
    valid = np.isfinite(dem) & (~sea)
    # 全程最大包络：优先 res.max，否则逐帧 nanmax
    max_path = results / "res.max"
    if max_path.exists():
        mx = read_ascii_grid(max_path, header)
        mx = np.where(np.isclose(mx, -9999.0), np.nan, mx).astype(np.float32)
    else:
        mx = np.nanmax(np.stack(frames), axis=0).astype(np.float32)
    return {
        "header": header,
        "rows": rows,
        "cols": cols,
        "frames": frames,
        "shas": shas,
        "dem": dem,
        "valid": valid,
        "sea": sea,
        "max": mx,
    }


def sar_on_grid(sar_cfg: dict, tr6: list[float], crs: str, rows: int, cols: int):
    """把 SAR 水体 tif 用最近邻重投影到模型网格；返回 (掩膜, 足迹 float 列表, 覆盖比例)。"""
    tif = sar_cfg["tif"]
    with rasterio.open(tif) as ds:
        src = ds.read(1)
        src_crs = ds.crs
        src_tr = ds.transform
        src_nodata = ds.nodata
        bounds = ds.bounds
    mask_src = np.isfinite(src)
    if src_nodata is not None:
        mask_src &= src != src_nodata
    mask_src &= src > 0.5
    dst = np.zeros((rows, cols), dtype=np.uint8)
    reproject(
        source=mask_src.astype(np.uint8),
        destination=dst,
        src_transform=src_tr,
        src_crs=src_crs,
        dst_transform=rasterio.Affine(*tr6),
        dst_crs=crs,
        resampling=Resampling.nearest,
    )
    mask = dst.astype(bool)
    tr = Transformer.from_crs(src_crs, "EPSG:4326", always_xy=True)
    pts = [
        (bounds.left, bounds.top),
        (bounds.right, bounds.top),
        (bounds.right, bounds.bottom),
        (bounds.left, bounds.bottom),
    ]
    footprint = [[round(lon, 8), round(lat, 8)] for lon, lat in (tr.transform(x, y) for x, y in pts)]
    return mask, footprint, float(mask.sum()) / max(rows * cols, 1)


def make_optical_mock(dem: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """合成 RGB（hillshade + 植被/裸地色调 + 云块），仅联调图层渲染。"""
    rows, cols = dem.shape
    d = np.where(np.isfinite(dem), dem, np.nanmean(dem[np.isfinite(dem)]))
    gy, gx = np.gradient(d)
    shade = np.clip(0.55 - 0.05 * gx - 0.05 * gy, 0.2, 1.0)
    rgba = np.zeros((rows, cols, 4), dtype=np.uint8)
    rgba[..., 0] = np.clip(150 * shade + 40, 0, 255).astype(np.uint8)
    rgba[..., 1] = np.clip(170 * shade + 45, 0, 255).astype(np.uint8)
    rgba[..., 2] = np.clip(120 * shade + 35, 0, 255).astype(np.uint8)
    # 云块（几个椭圆斑，模拟遮云透明区）与角落空缺，测试 alpha=0
    yy, xx = np.mgrid[0:rows, 0:cols]
    holes = np.zeros((rows, cols), dtype=bool)
    for cx, cy, rx, ry in [(0.22, 0.28, 0.10, 0.07), (0.55, 0.62, 0.09, 0.06), (0.76, 0.24, 0.07, 0.05)]:
        holes |= (((xx / cols) - cx) / rx) ** 2 + (((yy / rows) - cy) / ry) ** 2 < 1.0
    holes |= (xx > cols * 0.9) & (yy > rows * 0.85)
    rgba[..., 3] = np.where(valid & ~holes, 225, 0).astype(np.uint8)
    return rgba


def main() -> int:
    ap = argparse.ArgumentParser()
    here = Path(__file__).resolve().parent
    ap.add_argument("--out", default=str(here / "mock_bundle"))
    args = ap.parse_args()
    out = Path(args.out).resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    catalog = json.loads((EXAMPLES / "catalog.example.json").read_text(encoding="utf-8"))
    depth_classes = catalog["display"]["depth_classes"]
    terrain_stops = catalog["legends"]["terrain"]["stops"]
    transparent_below = 0.05
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    for case in catalog["cases"]:
        cid = case["case_id"]
        cfg = CASE_CONFIG[cid]
        crs = cfg["crs"]
        run_main = load_run(cfg["run_dir"], crs, catalog)
        h = run_main["header"]
        tr6 = transform_of(h)
        rows, cols = run_main["rows"], run_main["cols"]
        cell = h["cell"]
        cell_area = cell * cell
        full_coords = corners_lonlat(crs, tr6, rows, cols)
        bbox = [
            round(min(p[0] for p in full_coords), 6),
            round(min(p[1] for p in full_coords), 6),
            round(max(p[0] for p in full_coords), 6),
            round(max(p[1] for p in full_coords), 6),
        ]
        split_r, split_c = cfg["split"]
        case_dir = out / "cases" / cid
        (case_dir / "optical").mkdir(parents=True, exist_ok=True) if False else None
        case_dir.mkdir(parents=True, exist_ok=True)
        (out / "frames" / cid).mkdir(parents=True, exist_ok=True)
        (out / "numeric" / cid).mkdir(parents=True, exist_ok=True)

        # ---- DEM ----
        Image.fromarray(terrain_rgba(run_main["dem"], run_main["valid"], terrain_stops), "RGBA").save(
            out / "frames" / cid / "dem.png"
        )
        # ---- 光学（mock）----
        Image.fromarray(make_optical_mock(run_main["dem"], run_main["valid"]), "RGBA").save(
            out / "frames" / cid / "optical.png"
        )
        # ---- SAR ----
        sar_mask, sar_footprint, sar_cover = sar_on_grid(cfg["sar"], tr6, crs, rows, cols)
        sar_rgba = np.zeros((rows, cols, 4), dtype=np.uint8)
        cls = catalog["legends"]["binary_water"]["classes"][0]
        sar_rgba[sar_mask & run_main["valid"], 0:3] = hex_to_rgb(cls["color"])
        sar_rgba[sar_mask & run_main["valid"], 3] = 210
        Image.fromarray(sar_rgba, "RGBA").save(out / "frames" / cid / "sar.png")
        (case_dir / "sar_footprint.geojson").write_text(
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "case_id": cid,
                                "coverage_fraction": round(sar_cover, 4),
                                "asset_id": cfg["sar"]["asset_id"],
                            },
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [[*sar_footprint, sar_footprint[0]]],
                            },
                        }
                    ],
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        (case_dir / "optical_footprint.geojson").write_text(
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"case_id": cid, "asset_id": cfg["optical"]["asset_id"], "coverage_fraction": 0.9684},
                            "geometry": {"type": "Polygon", "coordinates": [[*full_coords, full_coords[0]]]},
                        }
                    ],
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        # ---- 边界与站点 ----
        (case_dir / "boundary.geojson").write_text(
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"case_id": cid, "kind": "model_domain"},
                            "geometry": {"type": "Polygon", "coordinates": [[*full_coords, full_coords[0]]]},
                        }
                    ],
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        site_features = []
        for site in case.get("sites", []):
            site_features.append(
                {
                    "type": "Feature",
                    "properties": {"name": site["name"], "label": site.get("label", "")},
                    "geometry": {"type": "Point", "coordinates": [site["lon"], site["lat"]]},
                }
            )
        if not site_features:
            tr = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
            for name, fx, fy in [("参考点 A", 0.18, 0.22), ("河道断面 B", 0.52, 0.55), ("下游出口 C", 0.8, 0.78)]:
                lon, lat = tr.transform(tr6[2] + cols * cell * fx, tr6[5] + tr6[4] * rows * fy)
                site_features.append(
                    {
                        "type": "Feature",
                        "properties": {"name": name, "label": name},
                        "geometry": {"type": "Point", "coordinates": [round(lon, 8), round(lat, 8)]},
                    }
                )
        (case_dir / "sites.geojson").write_text(
            json.dumps({"type": "FeatureCollection", "features": site_features}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )

        # ---- 逐 run 输出 ----
        run_defs = [
            {
                "run_id": cfg["run"],
                "run_dir": cfg["run_dir"],
                "label": case["runs"][0]["label"],
                "role": "default",
                "data": run_main,
            }
        ]
        for extra in cfg["extra_runs"]:
            run_defs.append(
                {
                    "run_id": extra["run"],
                    "run_dir": extra["run_dir"],
                    "label": extra["label"],
                    "role": extra["role"],
                    "data": load_run(extra["run_dir"], crs, catalog),
                }
            )
        runs_catalog = []
        for rdef in run_defs:
            data = rdef["data"]
            run_id = rdef["run_id"]
            run_rows, run_cols = data["rows"], data["cols"]
            run_h = data["header"]
            run_tr6 = transform_of(run_h)
            run_va = run_rows * run_cols
            frame_dir = out / "frames" / cid / run_id
            frame_dir.mkdir(parents=True, exist_ok=True)
            num_dir = out / "numeric" / cid / run_id
            num_dir.mkdir(parents=True, exist_ok=True)
            Image.fromarray((data["valid"].astype(np.uint8) * 255), "L").convert("1").save(num_dir / "mask.png")

            n = len(data["frames"])
            images_frames = []
            timeline_frames = []
            stats_frames = []
            for i, depth in enumerate(data["frames"]):
                fid = f"f{i:04d}"
                parts = []
                for pr in range(split_r):
                    r0, r1 = pr * run_rows // split_r, (pr + 1) * run_rows // split_r
                    for pc in range(split_c):
                        c0, c1 = pc * run_cols // split_c, (pc + 1) * run_cols // split_c
                        sub = depth[r0:r1, c0:c1]
                        pth = f"frames/{cid}/{run_id}/{fid}_r{pr}c{pc}.png"
                        Image.fromarray(depth_rgba(sub, depth_classes, transparent_below), "RGBA").save(out / pth)
                        parts.append(
                            {
                                "id": f"{fid}_r{pr}c{pc}",
                                "row": pr,
                                "col": pc,
                                "path": pth,
                                "width": c1 - c0,
                                "height": r1 - r0,
                                "coordinates": part_corners(crs, run_tr6, r0, r1, c0, c1),
                                "max_depth_m": round(float(np.nanmax(sub)) if np.isfinite(sub).any() else 0.0, 3),
                                "wet_pixels": int(np.isfinite(sub).sum() and int(np.sum(np.asarray(sub) > transparent_below))),
                            }
                        )
                images_frames.append(
                    {
                        "frame_id": fid,
                        "max_depth_m": round(float(np.nanmax(depth)) if np.isfinite(depth).any() else 0.0, 3),
                        "wet_pixels": int(np.sum(depth > transparent_below)),
                        "parts": parts,
                    }
                )
                write_blocks(depth, num_dir / fid)
                sim_seconds = i * 21600.0
                timeline_frames.append(
                    {
                        "frame_id": fid,
                        "source_sequence": i,
                        "sim_seconds": sim_seconds,
                        "timestamp": ts_iso(cfg["time_origin"], sim_seconds) if cfg["timestamps"] else None,
                        "depth_image": f"frames/{cid}/{run_id}/{fid}.png",
                        "query_blocks": f"numeric/{cid}/{run_id}/{fid}/{{block_y}}/{{block_x}}.bin",
                        "sha256": data["shas"][i],
                    }
                )
                areas, wet = {}, {}
                for thr in catalog["display"]["thresholds_m"]:
                    m = np.isfinite(depth) & data["valid"] & (depth > thr)
                    cnt = int(m.sum())
                    areas[f"{thr}"] = round(cnt * cell_area / 1e6, 6)
                    wet[f"{thr}"] = cnt
                stats_frames.append(
                    {
                        "frame_id": fid,
                        "sim_seconds": sim_seconds,
                        "timestamp": timeline_frames[-1]["timestamp"],
                        "areas_km2": areas,
                        "wetted_cells": wet,
                    }
                )
            # ---- max parts ----
            mx = data["max"]
            max_parts = []
            for pr in range(split_r):
                r0, r1 = pr * run_rows // split_r, (pr + 1) * run_rows // split_r
                for pc in range(split_c):
                    c0, c1 = pc * run_cols // split_c, (pc + 1) * run_cols // split_c
                    sub = mx[r0:r1, c0:c1]
                    pth = f"frames/{cid}/{run_id}/max_r{pr}c{pc}.png"
                    Image.fromarray(depth_rgba(sub, depth_classes, transparent_below), "RGBA").save(out / pth)
                    max_parts.append(
                        {
                            "id": f"max_r{pr}c{pc}",
                            "row": pr,
                            "col": pc,
                            "path": pth,
                            "width": c1 - c0,
                            "height": r1 - r0,
                            "coordinates": part_corners(crs, run_tr6, r0, r1, c0, c1),
                        }
                    )
            write_blocks(mx, num_dir / "max")
            (frame_dir / "images.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "case_id": cid,
                        "run_id": run_id,
                        "kind": "depth_frames",
                        "grid": {
                            "crs": crs,
                            "rows": run_rows,
                            "cols": run_cols,
                            "transform": run_tr6,
                            "cell_size_m": cell,
                            "cell_area_m2": cell_area,
                        },
                        "color_scale": {
                            "transparent_below_m": transparent_below,
                            "classes": depth_classes,
                        },
                        "split": {"rows": split_r, "cols": split_c},
                        "frames": images_frames,
                        "max": {
                            "max_depth_m": round(float(np.nanmax(mx)) if np.isfinite(mx).any() else 0.0, 3),
                            "wet_pixels": int(np.sum(mx > transparent_below)),
                            "parts": max_parts,
                        },
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            # ---- timeline ----
            (out / "cases" / cid / run_id).mkdir(parents=True, exist_ok=True)
            (out / "cases" / cid / run_id / "timeline.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "case_id": cid,
                        "event_id": case["event_id"],
                        "run_id": run_id,
                        "time_origin": cfg["time_origin"],
                        "time_zone": TIME_ZONE,
                        "origin_status": cfg["origin_status"],
                        "origin_note": cfg["origin_note"],
                        "save_interval_s": 21600.0,
                        "simulation_duration_s": (n - 1) * 21600.0,
                        "frame_count": n,
                        "temporal_interpolation": "none",
                        "statistics": f"cases/{cid}/{run_id}/statistics.json",
                        "numeric_manifest": f"numeric/{cid}/{run_id}/manifest.json",
                        "frames": timeline_frames,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            # ---- statistics ----
            max_areas, max_wet = {}, {}
            for thr in catalog["display"]["thresholds_m"]:
                m = np.isfinite(mx) & data["valid"] & (mx > thr)
                cnt = int(m.sum())
                max_areas[f"{thr}"] = round(cnt * cell_area / 1e6, 6)
                max_wet[f"{thr}"] = cnt
            peak_i = int(max(range(n), key=lambda k: stats_frames[k]["areas_km2"]["0.05"]))
            (out / "cases" / cid / run_id / "statistics.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "case_id": cid,
                        "run_id": run_id,
                        "unit_area": "km2",
                        "thresholds_m": catalog["display"]["thresholds_m"],
                        "grid_cell_area_m2": cell_area,
                        "valid_cell_count": int(data["valid"].sum()),
                        "sea_cell_count": int(data["sea"].sum()),
                        "frames": stats_frames,
                        "max_envelope": {
                            "areas_km2": max_areas,
                            "wetted_cells": max_wet,
                            "peak_frame_id": stats_frames[peak_i]["frame_id"],
                            "peak_time_s": stats_frames[peak_i]["sim_seconds"],
                        },
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            # ---- numeric manifest ----
            (num_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "case_id": cid,
                        "run_id": run_id,
                        "grid": {
                            "crs": crs,
                            "rows": run_rows,
                            "cols": run_cols,
                            "transform": run_tr6,
                            "cell_size_m": cell,
                            "cell_area_m2": cell_area,
                        },
                        "block_size": BLOCK,
                        "blocks": {
                            "rows": math.ceil(run_rows / BLOCK),
                            "cols": math.ceil(run_cols / BLOCK),
                        },
                        "dtype": "<f4",
                        "byte_order": "little",
                        "nodata": "nan",
                        "layout": "block rows [by*256, min(rows,(by+1)*256)) x cols [bx*256, min(cols,(bx+1)*256)); C order; index=(r-r0)*w+(c-c0)",
                        "mask_png": f"numeric/{cid}/{run_id}/mask.png",
                        "mask_encoding": "1-bit PNG, full grid 1:1, row 0 north, pixel value 255=valid",
                        "frames": [
                            {
                                "frame_id": f["frame_id"],
                                "path_template": f"numeric/{cid}/{run_id}/{f['frame_id']}/{{block_y}}/{{block_x}}.bin",
                            }
                            for f in timeline_frames
                        ],
                        "max": {"path_template": f"numeric/{cid}/{run_id}/max/{{block_y}}/{{block_x}}.bin"},
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            runs_catalog.append(
                {
                    "run_id": run_id,
                    "label": rdef["label"],
                    "role": rdef["role"],
                    "frame_count": n,
                    "save_interval_s": 21600.0,
                    "simulation_duration_s": (n - 1) * 21600.0,
                    "timeline": f"cases/{cid}/{run_id}/timeline.json",
                    "statistics": f"cases/{cid}/{run_id}/statistics.json",
                    "numeric_manifest": f"numeric/{cid}/{run_id}/manifest.json",
                    "optional": False,
                    "notes": "mock 包：数据来自真实模型输出，前端联调用",
                    "quality": case["runs"][0]["quality"],
                }
            )

        # ---- 更新 catalog 的该案例 ----
        case["grid"] = {
            "rows": rows,
            "cols": cols,
            "transform": tr6,
            "crs": crs,
            "cell_size_m": cell,
            "cell_area_m2": cell_area,
            "coordinates": full_coords,
            "bbox_wgs84": bbox,
        }
        case["model_bbox"] = bbox
        case["subtitle"] = f"{'200' if cell >= 200 else '100'} m 情景 · {len(run_main['frames'])} 帧 · 6 h/帧（mock：{split_r}×{split_c} 分块）"
        case["runs"] = runs_catalog
        case["default_run_id"] = cfg["run"]
        case["time"] = {
            "time_origin": cfg["time_origin"],
            "time_zone": TIME_ZONE,
            "origin_status": cfg["origin_status"],
            "origin_note": cfg["origin_note"],
            "save_interval_s": 21600.0,
            "temporal_interpolation": "none",
        }
        layers = case["layers"]
        layers["flood"]["images"] = f"frames/{cid}/{cfg['run']}/images.json"
        layers["max"]["image"] = f"frames/{cid}/{cfg['run']}/max.png"
        layers["max"]["geometry"] = case["grid"]
        layers["dem"]["image"] = f"frames/{cid}/dem.png"
        layers["dem"]["geometry"] = case["grid"]
        for name in ("sar", "optical"):
            cfg_layer = cfg[name]
            layer = layers[name]
            layer.update(
                {
                    "status": cfg_layer["status"],
                    "label": cfg_layer["label"],
                    "summary": cfg_layer["summary"],
                    "note": cfg_layer["note"],
                    "asset_id": cfg_layer["asset_id"],
                    "acquired_at": cfg_layer["acquired_at"],
                    "image": f"frames/{cid}/{name}.png",
                    "geometry": case["grid"],
                }
            )
            layer["footprint"] = f"cases/{cid}/{name}_footprint.geojson"
            if name == "sar":
                layer["coverage_fraction"] = round(sar_cover, 4)
        layers["boundary"]["geojson"] = f"cases/{cid}/boundary.geojson"
        layers["sites"]["geojson"] = f"cases/{cid}/sites.geojson"
        case["quality"]["warnings"] = list(case["quality"]["warnings"]) + ["mock 联调包：显示图/统计来自真实 .wd 帧；SAR 用 S1 水体掩膜替代；光学为合成 RGB"]

    catalog["bundle_id"] = "mock-web-dev"
    catalog["created_utc"] = now
    catalog["source_manifest_hash"] = "sha256:" + hashlib.sha256(b"mock-web-dev").hexdigest()
    catalog["license_notice"] = (
        "研究性情景成果，非业务预警；各图层授权见 licenses/LICENSES.md。"
        "本包为 web 前端联调 mock 包（web/.dev/make_mock_bundle.py 生成），不是正式发布包。"
    )
    (out / "catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    (out / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "bundle_id": "mock-web-dev",
                "created_utc": now,
                "source_manifest_hash": catalog["source_manifest_hash"],
                "note": "前端联调 mock 包：水深/DEM/统计来自父项目真实输出；SAR 为 S1 水体掩膜；光学为合成 RGB",
                "cases": [
                    {"case_id": c["case_id"], "runs": [r["run_id"] for r in c["runs"]]} for c in catalog["cases"]
                ],
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    (out / "licenses").mkdir(exist_ok=True)
    (out / "licenses" / "LICENSES.md").write_text(
        "# 许可（mock 联调包）\n\n"
        "- 模型输出与 DEM：父项目研究性情景成果，仅内部联调使用。\n"
        "- SAR：Sentinel-1 水体掩膜（Copernicus）。\n"
        "- 光学：合成 RGB（非真实影像），仅供 UI 联调。\n",
        encoding="utf-8",
    )

    total = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"mock bundle -> {out}")
    print(f"files={sum(1 for p in out.rglob('*') if p.is_file())} bytes={total / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
