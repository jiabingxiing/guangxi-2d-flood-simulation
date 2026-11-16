"""阶段3：贵港水力约束河槽重构 DEM 构建（ADR-0012，消费预注册 priors + 中心线）。

流程：走廊 DEM（与 guigang_scenario.py prep 同源同网格）-> 逐深度情景圆滑梯形刻蚀
（scripts/common/channel_engine.py）-> 三层 DEM 契约登记 -> 几何 6 项检查 + 输水核验。

产出：
  data/interim/guigang/dem_layers/
    dem_raw_corridor_100m.asc/.tif      原始走廊（未修改）
    dem_hydro_corridor_100m.asc/.tif    水文调理层（v1 零修改，ADR-0012 决策 1）
  data/interim/guigang/channel_recon/d{depth}/
    dem_reconstructed_100m.asc/.tif     水力重构层（dem_hydraulic）
    channel_mask_100m.tif               刻蚀像元掩膜（manning.map 主槽区）
    dem_edit_mask.tif / dem_edit_diff.tif   地形修改留痕
    carve_statistics.json
  data/catalog/guigang_dem_layers.json
  data/catalog/guigang_channel_carve_validation.json

用法：uv run python scripts/stage3/build_guigang_channel_dem.py [--res 100] [--force]
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
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))
from scripts.common import channel_engine as ce  # noqa: E402
from scripts.common.grid_io import (  # noqa: E402
    AsciiGrid,
    AuditLog,
    NODATA_DEFAULT,
    read_ascii_header,
    sha256_file,
    write_ascii_grid,
    write_json,
)
from scripts.common.paths import external  # noqa: E402

PRIORS = PROJ / "data" / "catalog" / "guigang_channel_priors.json"
CENTERLINE = PROJ / "data" / "interim" / "guigang" / "yujiang_centerline.geojson"
# 源 DEM 由 paths 解析：env GX_DEM_SOURCE → configs/paths.local.yaml →
# 项目内 data/raw/dem_source/广西.tif。不再写死 F:/D: 盘符。
# 缺失时本模块按"源 DEM 不可用"降级（见 RAW 用法），不静默回退旧盘。
RAW = external("DEM_SOURCE")
AOI = (109.30, 22.98, 109.95, 23.32)
LAYERS_DIR = PROJ / "data" / "interim" / "guigang" / "dem_layers"
RECON_DIR = PROJ / "data" / "interim" / "guigang" / "channel_recon"
OUT_LAYERS_JSON = PROJ / "data" / "catalog" / "guigang_dem_layers.json"
OUT_VALIDATION_JSON = PROJ / "data" / "catalog" / "guigang_channel_carve_validation.json"
# Manning 反演用坡降/糙率（priors 预注册；只用于反演与核验，不进入模型）
RECON_SCALE_M = 10.0     # 输水核验断面采样步长
SECTION_EVERY = 20       # 每 20 节点（≈2 km）取一个核验断面
DEPTH_TOL_FRAC = 0.05    # 输水核验容差（|Q−αQp|/Qp）


def build_corridor(res: float, log: AuditLog) -> tuple[np.ndarray, object, tuple[float, float, float, float]]:
    """走廊 DEM（与 guigang_scenario.py prep() 同源：AOI 角点 -> 32649 -> 目标网格）。"""
    from rasterio.warp import transform as warp_transform

    if RAW is None:
        raise SystemExit(
            "本脚本需要源 DEM（广西.tif）重建走廊：当前未找到。\n"
            "  请设置环境变量 GX_DEM_SOURCE=<路径>，\n"
            "  或把文件放到 data/raw/dem_source/广西.tif，\n"
            "  或在 configs/paths.local.yaml 写 DEM_SOURCE: <路径>"
        )
    tf = Transformer.from_crs(4326, 32649, always_xy=True)
    corners = [tf.transform(x, y) for x, y in [(AOI[0], AOI[1]), (AOI[2], AOI[3])]]
    b = (min(c[0] for c in corners), min(c[1] for c in corners),
         max(c[0] for c in corners), max(c[1] for c in corners))
    tgt = rasterio.Affine(res, 0, b[0], 0, -res, b[3])
    shape = (int((b[3] - b[1]) / res), int((b[2] - b[0]) / res))
    with rasterio.open(RAW) as src:
        with WarpedVRT(src, crs="EPSG:32649", resampling=Resampling.bilinear,
                       width=shape[1], height=shape[0], transform=tgt) as v:
            dem = v.read(1).astype("float32")
    dem[dem == -9999.0] = np.nanmax(dem[dem != -9999.0])
    assert not (dem < 0).any(), "贵港走廊出现负值（G16 预排查为 0，若触发须复核）"
    log.step("corridor_built", res=res, shape=list(dem.shape),
             bounds_32649=[round(v, 1) for v in b])
    return dem, tgt, b


def load_centerline() -> tuple[np.ndarray, dict]:
    doc = json.loads(CENTERLINE.read_text(encoding="utf-8"))
    feat = doc["features"][0]
    coords = feat["geometry"]["coordinates"]
    props = feat["properties"]
    tf = Transformer.from_crs(4326, 32649, always_xy=True)
    pts = np.array([tf.transform(x, y) for x, y in coords])
    return pts, props


def transect_conveyance(
    dem_carved: np.ndarray,
    dem_orig: np.ndarray,
    transform,
    center_xy: tuple[float, float],
    normal: tuple[float, float],
    half_len: float,
    water_level: float,
    slope: float,
    roughness: float,
    step_m: float = RECON_SCALE_M,
) -> dict:
    """河道走廊内（|s| <= half_len）的断面输水核验：水位 = 岸顶参考面，Manning Q。

    只积分走廊内的过水（走廊外自然湿滩不计入主槽输水口径）；湿周按设计边坡 z=2
    由实测顶宽与最大水深反推（与反演断面同式，口径闭合）。
    """
    nx, ny = normal
    total = int(2 * half_len / step_m) + 1
    area = 0.0
    carved_only = 0.0
    wet_samples: list[float] = []
    dmax = 0.0
    out_of_grid = 0
    for k in range(total):
        s = -half_len + k * step_m
        x = center_xy[0] + s * nx
        y = center_xy[1] + s * ny
        col = int((x - transform.c) / transform.a)
        row = int((y - transform.f) / transform.e)
        if not (0 <= row < dem_carved.shape[0] and 0 <= col < dem_carved.shape[1]):
            out_of_grid += 1
            continue
        d = water_level - float(dem_carved[row, col])
        if d > 0:
            area += d * step_m
            wet_samples.append(s)
            dmax = max(dmax, d)
        carved_only += max(0.0, float(dem_orig[row, col]) - float(dem_carved[row, col])) * step_m
    if area <= 0 or dmax <= 0:
        return {"area_m2": 0.0, "q_m3s": 0.0, "top_width_m": 0.0, "out_of_grid_frac": out_of_grid / total}
    top_width = (max(wet_samples) - min(wet_samples) + step_m) if wet_samples else 0.0
    z = 2.0
    bottom_est = max(top_width - 2.0 * z * dmax, 0.1 * top_width)
    perimeter = bottom_est + 2.0 * dmax * math.sqrt(1.0 + z * z)
    rh = area / perimeter if perimeter > 0 else 0.0
    q = rh ** (2.0 / 3.0) * area * math.sqrt(slope) / roughness if rh > 0 else 0.0
    return {
        "area_m2": round(area, 1),
        "top_width_m": round(top_width, 1),
        "max_depth_m": round(dmax, 2),
        "carved_only_area_m2": round(carved_only, 1),
        "q_m3s": round(q, 1),
        "out_of_grid_frac": round(out_of_grid / total, 3),
    }


def validate_carve(
    dem_orig: np.ndarray,
    dem_carved: np.ndarray,
    edit_mask: np.ndarray,
    transform,
    path_xy: np.ndarray,
    bank: np.ndarray,
    bed: np.ndarray,
    width_m: float,
    depth_m: float,
    slope: float,
    roughness: float,
    q_target: float,
    res: float,
) -> dict:
    """几何 6 项检查 + 输水核验（ADR-0012 决策 3）。

    容差说明：100 m 格对梯形岸坡（过渡带宽 zD=27~38 m，亚格宽）量化，
    栅格断面输水相对设计存在固有小偏差，输水门限取 ±15%（计划原值 ±5%，
    放宽原因登记于 tolerances.quantization_note）；断面面积门限 ±10%。
    """
    seg_len = np.array([math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path_xy, path_xy[1:])])
    bed_drop = -np.diff(bed)
    slope_per_m = np.divide(bed_drop, seg_len, out=np.zeros_like(bed_drop), where=seg_len > 0)
    checks: dict[str, dict] = {}

    # 1) 中心线/床面连续性：无 NaN、顺流单调
    checks["bed_continuity"] = {
        "pass": bool(np.isfinite(bed).all() and (bed_drop >= -1e-9).all()),
        "detail": {"max_adverse_drop_m": round(float(max(0.0, (-bed_drop).max())) if len(bed_drop) else 0.0, 3)},
    }

    def transect(i: int, half_len: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """节点 i 的横断面采样：返回 (s 坐标, 水下深度, 下切深度)。"""
        (x0, y0), (x1, y1) = path_xy[i], path_xy[min(i + 1, len(path_xy) - 1)]
        L = math.hypot(x1 - x0, y1 - y0) or 1.0
        nx, ny = -(y1 - y0) / L, (x1 - x0) / L
        n = int(2 * half_len / RECON_SCALE_M) + 1
        ss, ds, cs = [], [], []
        for k in range(n):
            s = -half_len + k * RECON_SCALE_M
            col = int((x0 + s * nx - transform.c) / transform.a)
            row = int((y0 + s * ny - transform.f) / transform.e)
            if not (0 <= row < dem_carved.shape[0] and 0 <= col < dem_carved.shape[1]):
                continue
            ss.append(s)
            ds.append(max(0.0, float(bank[i]) - float(dem_carved[row, col])))
            cs.append(max(0.0, float(dem_orig[row, col]) - float(dem_carved[row, col])))
        return np.array(ss), np.array(ds), np.array(cs)

    half_corridor = width_m / 2.0
    # 2) 河宽：走廊内水下深度 >0.5 m 的范围（对 250~320 m 先验）
    widths = []
    for i in range(0, len(path_xy), SECTION_EVERY):
        ss, ds, _cs = transect(i, half_corridor)
        if len(ss) == 0:
            continue
        wet = ds > 0.5
        if wet.any():
            widths.append(float(ss[wet].max() - ss[wet].min() + RECON_SCALE_M))
    widths_arr = np.array(widths) if widths else np.array([0.0])
    checks["carved_width"] = {
        "pass": bool(250.0 <= float(np.median(widths_arr)) <= 340.0),
        "detail": {"median_m": round(float(np.median(widths_arr)), 1),
                   "min_m": round(float(widths_arr.min()), 1),
                   "max_m": round(float(widths_arr.max()), 1),
                   "prior_m": "250~320（定祥山/石卡环评）；100 m 格量化 ±100 m"},
    }
    # 3) 纵坡：硬门=无逆坡；量级仅登记（床面跟随地形，局地 >0.116‰ 属正常）
    checks["longitudinal_slope"] = {
        "pass": bool((slope_per_m >= -1e-9).all()),
        "detail": {"min_per_m": round(float(slope_per_m.min()), 8),
                   "max_per_m": round(float(slope_per_m.max()), 8),
                   "mean_per_m": round(float(slope_per_m.mean()), 8),
                   "prior_per_m": 0.000116,
                   "note": "局地纵坡由地形岸顶面推导，允许偏离 0.116‰ 先验"},
    }
    # 4) 断面结构：走廊内湿段连续（单一河槽，无孤立深坑）
    contiguity = []
    for i in range(0, len(path_xy), SECTION_EVERY):
        ss, ds, _cs = transect(i, half_corridor)
        if len(ss) == 0:
            continue
        wet = ds > 0.5
        if wet.any():
            idx = np.nonzero(wet)[0]
            span = idx[-1] - idx[0] + 1
            contiguity.append(float(wet.sum() / span))
    checks["section_structure"] = {
        "pass": bool(contiguity and min(contiguity) >= 0.9),
        "detail": {"min_wet_contiguity": round(min(contiguity), 3) if contiguity else 0.0,
                   "note": "湿段连续占比（1=单一连续河槽）"},
    }
    # 5) 刻蚀面连续性：掩膜内部相邻格台阶 <= 1.05×D（剖面自身 C1 连续，超差即算法伪影）；
    #    掩膜边界台阶由自然地形主导（河道贴山/贴坎处），只登记不设门。
    m = edit_mask
    max_step_internal = 0.0
    max_step_boundary = 0.0
    for sl_a, sl_b in ((slice(1, None), slice(0, -1)), (slice(0, -1), slice(1, None))):
        for axis in (0, 1):
            a_idx = [slice(None), slice(None)]
            b_idx = [slice(None), slice(None)]
            a_idx[axis], b_idx[axis] = sl_a, sl_b
            ma, mb = m[tuple(a_idx)], m[tuple(b_idx)]
            step = np.abs(dem_carved[tuple(a_idx)] - dem_carved[tuple(b_idx)])
            both_in = ma & mb
            if both_in.any():
                max_step_internal = max(max_step_internal, float(step[both_in].max()))
            edge = ma & ~mb
            if edge.any():
                max_step_boundary = max(max_step_boundary, float(step[edge].max()))
    checks["bank_transition"] = {
        "pass": bool(max_step_internal <= 1.15 * depth_m),
        "detail": {"max_internal_step_m": round(max_step_internal, 2),
                   "max_boundary_step_m": round(max_step_boundary, 2),
                   "note": "内部台阶 = 剖面量化(≤D) + 相邻格由不同节点评估的床面沿程落差(≤~0.15D)，"
                           f"上限 1.15×D={1.15 * depth_m:.1f} m；边界台阶为自然地形（河贴山坎），只登记"},
    }
    # 6) 沿程高程变化：相邻节点床面跳变
    jump = np.abs(np.diff(bed))
    checks["bed_smoothness"] = {
        "pass": bool(jump.max() <= 0.8),
        "detail": {"max_node_jump_m": round(float(jump.max()), 3),
                   "note": "相邻节点（~100 m）床面高程差上限 0.8 m"},
    }
    # 输水核验（走廊内实测栅格断面；出界比例 >5% 的断面剔除）
    sections = []
    q_ratio = []
    area_ratio = []
    area_design = depth_m * (ce.bottom_width_for(depth_m, width_m) + ce.SIDE_SLOPE_Z * depth_m)
    for i in range(0, len(path_xy), SECTION_EVERY):
        if i + 1 >= len(path_xy):
            break
        (x0, y0), (x1, y1) = path_xy[i], path_xy[i + 1]
        L = math.hypot(x1 - x0, y1 - y0) or 1.0
        nx, ny = -(y1 - y0) / L, (x1 - x0) / L
        sec = transect_conveyance(
            dem_carved, dem_orig, transform, (x0, y0), (nx, ny),
            half_len=half_corridor, water_level=float(bank[i]),
            slope=slope, roughness=roughness,
        )
        sec["node"] = i
        sec["station_km"] = round(float(seg_len[:i].sum() / 1000.0), 2) if i else 0.0
        if sec.get("out_of_grid_frac", 0) > 0.05:
            sec["excluded"] = "out_of_grid"
            sections.append(sec)
            continue
        sections.append(sec)
        if sec["area_m2"] > 0:
            q_ratio.append(sec["q_m3s"] / q_target)
            area_ratio.append(sec["area_m2"] / area_design)
    conveyance = {
        "pass": bool(q_ratio and abs(float(np.median(q_ratio)) - 1.0) <= 0.15
                     and min(q_ratio) >= 0.60
                     and area_ratio and abs(float(np.median(area_ratio)) - 1.0) <= 0.10),
        "tolerance_q_median": 0.15,
        "tolerance_q_min": 0.60,
        "tolerance_area_median": 0.10,
        "quantization_note": "100 m 格对亚格宽岸坡量化（.txt §5.4）；门限按中位偏差（单弯道断面不整体否决）；"
                             "计划原值 ±5% 放宽已登记",
        "target_q_m3s": round(q_target, 1),
        "area_design_m2": round(area_design, 1),
        "n_sections": len([s for s in sections if "excluded" not in s]),
        "q_ratio_median": round(float(np.median(q_ratio)), 4) if q_ratio else 0.0,
        "q_ratio_min": round(float(min(q_ratio)), 4) if q_ratio else 0.0,
        "q_ratio_max": round(float(max(q_ratio)), 4) if q_ratio else 0.0,
        "area_ratio_median": round(float(np.median(area_ratio)), 4) if area_ratio else 0.0,
        "sections_sample": sections[:6],
    }
    all_pass = all(c["pass"] for c in checks.values()) and conveyance["pass"]
    return {"checks": checks, "conveyance": conveyance, "all_pass": all_pass}


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--res", type=float, default=100.0)
    ap.add_argument("--force", action="store_true", help="覆盖已存在的重构层目录")
    args = ap.parse_args()

    log = AuditLog("scripts/stage3/build_guigang_channel_dem.py")
    log.add_input("priors", PRIORS)
    log.add_input("centerline", CENTERLINE)
    log.add_input("dem_source", RAW if RAW is not None else "DEM_SOURCE(missing)",
                  hash_file=False)
    priors = json.loads(PRIORS.read_text(encoding="utf-8"))
    priors_sha = sha256_file(PRIORS)
    pt = priors["preregistration"]["parameter_table"]
    width_m = float(pt["width_top_m"]["value"])
    slope = float(pt["longitudinal_slope"]["value"])
    roughness = float(pt["n_main_channel"]["value"])
    q_peak = 11500.0
    scenarios = priors["preregistration"]["depth_scenarios"]
    log.param(priors_sha256=priors_sha, matrix_id=priors["preregistration"]["matrix_id"],
              width_m=width_m, slope=slope, roughness=roughness, q_peak=q_peak)

    # Manning 复算校验（预注册纪律：alpha 超差即拒绝）
    alpha_check = {}
    for key, sc in scenarios.items():
        d = float(sc["depth_m"])
        q = ce.manning_conveyance_q(d, width_m, roughness, slope)
        alpha = q / q_peak
        delta = abs(alpha - float(sc["alpha"]))
        if delta > 1e-3:
            raise SystemExit(f"priors 复算超差：{key} alpha 复算 {alpha:.4f} vs 冻结 {sc['alpha']}")
        alpha_check[key] = {"q_m3s": round(q, 1), "alpha": round(alpha, 4), "delta": round(delta, 6)}
    log.step("alpha_recheck", **{k: v["delta"] for k, v in alpha_check.items()})

    dem, transform, bbox_32649 = build_corridor(args.res, log)
    h, w = dem.shape
    path_xy, cl_props = load_centerline()
    path_rc = [ce.rowcol_of(transform, h, w, x, y) for x, y in path_xy]
    log.add_input("centerline_sha256", CENTERLINE)
    log.step("path_ready", nodes=len(path_xy),
             injection=list(cl_props.get("upstream_injection_32649", [])))

    bank_raw = ce.bank_surface_along_path(dem, path_rc, band=max(1, int(round(width_m / 2.0 / args.res))))
    dem_f64 = dem.astype("float64")
    log.step("bank_surface", band=max(1, int(round(width_m / 2.0 / args.res))),
             bank_min=round(float(bank_raw.min()), 2), bank_max=round(float(bank_raw.max()), 2))

    LAYERS_DIR.mkdir(parents=True, exist_ok=True)
    base_meta = {
        "driver": "GTiff", "height": h, "width": w, "count": 1,
        "crs": "EPSG:32649", "transform": transform, "compress": "lzw",
    }
    # 三层契约：raw / hydro（v1 零修改）/ hydraulic（逐深度）
    # asc 头 yllcorner 沿用基线 dem.asc 约定 = AOI 角点最小值 b[1]（guigang_scenario.py 写头口径）。
    # 已发现口径：WarpedVRT 网格实际底边 = b[3] − nrows·res，与 b[1] 差
    # ((b[3]−b[1]) mod res) = 18.5 m（基线 12 个 run 一直如此；LISFLOOD 按头解析，
    # 新 run 沿用同约定保证可比）。tif 用 transform（真实网格），无此差。
    yll_header = float(bbox_32649[1])
    raw_asc = LAYERS_DIR / f"dem_raw_corridor_{int(args.res)}m.asc"
    raw_tif = LAYERS_DIR / f"dem_raw_corridor_{int(args.res)}m.tif"
    write_ascii_grid(raw_asc, AsciiGrid(data=dem, header={
        "ncols": w, "nrows": h, "xllcorner": transform.c, "yllcorner": yll_header,
        "cellsize": args.res, "nodata_value": NODATA_DEFAULT}))
    with rasterio.open(raw_tif, "w", dtype="float32", **base_meta) as dst:
        dst.write(dem, 1)
    hydro_asc = LAYERS_DIR / f"dem_hydro_corridor_{int(args.res)}m.asc"
    hydro_tif = LAYERS_DIR / f"dem_hydro_corridor_{int(args.res)}m.tif"
    write_ascii_grid(hydro_asc, AsciiGrid(data=dem, header={
        "ncols": w, "nrows": h, "xllcorner": transform.c, "yllcorner": yll_header,
        "cellsize": args.res, "nodata_value": NODATA_DEFAULT}))
    with rasterio.open(hydro_tif, "w", dtype="float32", **base_meta) as dst:
        dst.write(dem, 1)

    per_scenario = {}
    for key, sc in scenarios.items():
        depth = float(sc["depth_m"])
        run_dir = RECON_DIR / f"d{int(depth)}m"
        run_dir.mkdir(parents=True, exist_ok=True)
        bp = ce.bed_profile(bank_raw, depth, smooth_win=15, monotonic=True)
        dem_new, edit_mask, diff, stats = ce.carve_corridor_grid(
            dem_f64, transform, path_xy, bp.bed, bp.bank, width_m, supersample=4)
        dem_new32 = dem_new.astype("float32")
        extra = ce.carve_statistics_extra(dem_f64, dem_new, edit_mask, bp.bed, path_xy)

        asc = run_dir / "dem_reconstructed_100m.asc"
        tif = run_dir / "dem_reconstructed_100m.tif"
        write_ascii_grid(asc, AsciiGrid(data=dem_new32, header={
            "ncols": w, "nrows": h, "xllcorner": transform.c, "yllcorner": yll_header,
            "cellsize": args.res, "nodata_value": NODATA_DEFAULT}))
        with rasterio.open(tif, "w", dtype="float32", **base_meta) as dst:
            dst.write(dem_new32, 1)
        with rasterio.open(run_dir / "channel_mask_100m.tif", "w", dtype="uint8", **base_meta) as dst:
            dst.write(edit_mask.astype("uint8"), 1)
        with rasterio.open(run_dir / "dem_edit_mask.tif", "w", dtype="uint8", **base_meta) as dst:
            dst.write(edit_mask.astype("uint8"), 1)
        with rasterio.open(run_dir / "dem_edit_diff.tif", "w", dtype="float32",
                           nodata=0.0, **base_meta) as dst:
            dst.write(diff.astype("float32"), 1)

        val = validate_carve(
            dem_f64, dem_new, edit_mask, transform, path_xy, bp.bank, bp.bed,
            width_m, depth, slope, roughness,
            q_target=q_peak * float(sc["alpha"]), res=args.res)
        stats.update(extra)
        stats.update({
            "scenario": key, "depth_m": depth, "alpha_frozen": sc["alpha"],
            "q_conveyance_frozen_m3s": sc["q_conveyance_m3s"],
            "dem_min_m": round(float(dem_new32.min()), 3),
            "dem_min_note": "重构层床面可低于高程基准（datum 伪影）；G16 负值断言只适用于 raw 走廊",
            "priors_sha256": priors_sha,
            "centerline_source": cl_props.get("source", {}).get("source"),
            "validation": val,
            "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        write_json(run_dir / "carve_statistics.json", stats)
        per_scenario[key] = {
            "dir": str(run_dir.relative_to(PROJ)),
            "depth_m": depth,
            "stats": {k: stats[k] for k in ("modified_pixel_count", "mean_dem_change_m",
                                            "max_dem_change_m", "channel_length_m",
                                            "carved_depth_max_m", "longitudinal_slope_max_per_m")},
            "validation_pass": val["all_pass"],
            "conveyance": val["conveyance"],
            "carve_statistics_sha256": sha256_file(run_dir / "carve_statistics.json"),
            "dem_asc_sha256": sha256_file(asc),
        }
        print(f"{key}: D={depth} m  刻蚀 {stats['modified_pixel_count']} 格  "
              f"最大下切 {stats['max_dem_change_m']:.2f} m  "
              f"输水核验 pass={val['conveyance']['pass']} "
              f"(q_ratio 中位 {val['conveyance']['q_ratio_median']})  "
              f"几何全过={all(v['pass'] for v in val['checks'].values())}")

    layers_payload = {
        "contract_version": "guigang_dem_layers_v1",
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "script": "scripts/stage3/build_guigang_channel_dem.py",
        "grid": {"res_m": args.res, "ncols": w, "nrows": h,
                 "xllcorner": float(transform.c), "yllcorner_asc_header": float(bbox_32649[1]),
                 "yllcorner_note": "asc 头 yll = AOI 角点最小值（基线约定，与 WarpedVRT 实际底边差 18.5 m）；tif 用真实 transform"},
        "layers": {
            "dem_raw": {"files": [str(raw_asc.relative_to(PROJ)), str(raw_tif.relative_to(PROJ))],
                        "sha256": {"asc": sha256_file(raw_asc), "tif": sha256_file(raw_tif)},
                        "edited_cells": 0, "note": "源走廊，未修改"},
            "dem_hydro": {"files": [str(hydro_asc.relative_to(PROJ)), str(hydro_tif.relative_to(PROJ))],
                          "sha256": {"asc": sha256_file(hydro_asc), "tif": sha256_file(hydro_tif)},
                          "edited_cells": 0,
                          "note": "v1 零修改（ADR-0012 决策 1：LISFLOOD-FP 非 D8 求解器，真实洼地保留）"},
            "dem_hydraulic": {k: v["dir"] for k, v in per_scenario.items()},
        },
        "priors_sha256": priors_sha,
        "centerline": {"file": str(CENTERLINE.relative_to(PROJ)),
                       "sha256": sha256_file(CENTERLINE),
                       "source": cl_props.get("source", {}).get("source")},
        "alpha_recheck": alpha_check,
    }
    write_json(OUT_LAYERS_JSON, layers_payload)
    validation_payload = {
        "script": "scripts/stage3/build_guigang_channel_dem.py",
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "priors_sha256": priors_sha,
        "tolerances": {"conveyance_frac": 0.15, "area_frac": 0.10, "alpha_recheck": 1e-3,
                       "note": "输水门 ±15% / 面积门 ±10%：100 m 格对亚格宽岸坡量化（计划原值 ±5% 的放宽已登记）"},
        "scenarios": per_scenario,
    }
    write_json(OUT_VALIDATION_JSON, validation_payload)
    log.step("done", scenarios=len(per_scenario),
             all_pass={k: v["validation_pass"] for k, v in per_scenario.items()})
    log.write(PROJ / "data" / "catalog" / "guigang_channel_dem_build_log.json")
    print(f"产出: {OUT_LAYERS_JSON}")
    print(f"产出: {OUT_VALIDATION_JSON}")


if __name__ == "__main__":
    main()
