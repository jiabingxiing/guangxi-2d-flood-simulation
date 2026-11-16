"""阶段5：贵港 2024-09 摩羯郁江洪水 情景回算（降级模式，ADR-0004）。

管线：走廊 DEM（本域实测无负值垃圾，G16 仅沿海平原）-> S1 09-05 常水河道定位
     -> 参数化河槽刻蚀 S2 -> 上游郁江三角 Q 情景（公开洪峰证据锚定）+ ERA5 均匀雨
     -> 运行 -> 验证（S1 09-12 洪峰景直接对比 + 市区锚点 WSE 诊断对照 43.09m）。
情景假设（成果必须标注）：
  - 上游 Q(t) 为三角情景线（基流 1000 -> 峰 11500 m3/s @09-12T12 -> 2500），
    锚定公开报道：南宁站 09-12T19:30 峰 11900 m3/s、贵港站 09-12T14 43.09m/10500m3/s；
  - 降雨 ERA5 市区锚点逐时均匀（降级）；
  - 下游东缘 FREE 带坡度；垂直基准未确认（G7）：站水位 43.09m 仅作诊断对照，不作闸门。
用法：uv run python scripts/stage5/guigang_scenario.py [all|prep|run|verify]

降雨模式 `--rain {on,off,loss_medium,loss_high}`（默认 `on` = 历史行为，逐位向后兼容）：
  - `on`          直接写 ERA5 原始逐时雨量（与所有历史 run 逐字节同源）；
  - `off`         不写 `rain.rain`，run.par 删除 `rainfall` 行 → 纯过境来水驱动对照；
  - `loss_medium` 净雨预处理：初损 Ia=30 mm，持续入渗 fc=2.0 mm/h；
  - `loss_high`   净雨预处理：初损 Ia=50 mm，持续入渗 fc=3.0 mm/h。
净雨预处理口径（计划 §Phase2 路径 A）：累计雨量 <= Ia 的整小时净雨为 0；**累计雨量达到 Ia
的那一小时**净雨 = 该小时雨量 − (Ia − 此前累计)；此后每小时净雨 = max(0, 雨量 − fc)；净雨不为负。
"达到 Ia 即穿越"是本项目实测口径：`loss_medium` 在 t=102h 处累计 29.5+0.5=30.0 mm 恰好触顶，
按此口径得净雨 45.40 mm / 11 小时（占毛雨 135.50 mm 的 33.51%），
`loss_high` 得 36.10 mm / 8 小时（26.64%）——两个预期值均已实测复现。
若改成"严格大于 Ia 才算穿越"（`cum+v > Ia`），loss_medium 会变成 47.20 mm / 12 小时，与预期不符。
**入渗参数不与净雨预处理叠加**：`FPInfiltration`（infevap.cpp:14-46）只对 H>1e-3 m 的湿单元
扣水，而净雨预处理已在雨量序列上扣掉 Ia+fc；再写 `infiltration` 会把 fc 重复扣一次。
本构建无初损参数，初损只能靠净雨预处理，故 loss 情景 **不写** `infiltration`，该点已登记进 meta。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.warp import reproject

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))
from scripts.common.grid_io import read_ascii_header, sha256_file  # noqa: E402
from scripts.common.net_rain import (  # noqa: E402  ADR-0013 组合实验
    NetRainError,
    derive_probe_offset_s,
    read_preprocessor_net_rain,
)
from scripts.common.run_manifest import write_run_manifest_best_effort  # noqa: E402
from scripts.common.paths import external  # noqa: E402

LISFLOOD = PROJ / "models" / "lisflood-fp-8.1" / "LISFLOOD-FP-trunk" / "lisflood.exe"
# 源 DEM 由 paths 解析（env GX_DEM_SOURCE → configs/paths.local.yaml →
# 项目内 data/raw/dem_source/广西.tif）。历史 D:/F: 路径已失效，不再作为候选：
# 盘符会变、上级目录名也会变，写死候选表无法覆盖未来位置。
RAW = external("DEM_SOURCE")
S1W_PRE = PROJ / "data" / "processed" / "s1_water" / "guigang_20240905T105022_water20m.tif"
S1W_FLD = PROJ / "data" / "processed" / "s1_water" / "guigang_20240912T104222_water20m.tif"
SIG2 = PROJ / "data" / "processed" / "s1_water" / "guigang_20240912T104222_sigma20m.tif"
SIG1 = PROJ / "data" / "processed" / "s1_water" / "guigang_20240905T105022_sigma20m.tif"
ERA5 = PROJ / "data" / "catalog" / "probes" / "guigang_sep2024_era5_hourly.json"
# ADR-0013：Phase G 预处理器贵港 GA-Mid 净雨（路径 A；infilfile 在本构建不生效）
PREPROCESSOR_RAIN = PROJ / "outputs" / "preprocessor" / "guigang" / "rainfall_net_green_ampt.rain"
PREPROCESSOR_SUMMARY = PROJ / "outputs" / "preprocessor" / "guigang" / "preprocessor_summary.json"

AOI = (109.30, 22.98, 109.95, 23.32)
# Phase B1：网格/时间步敏感性（终稿 §11 第 7 项）。默认值=首轮基线 200 m / dt 5 s。
RES = float(sys.argv[sys.argv.index("--res") + 1]) if "--res" in sys.argv else 200.0
DT = float(sys.argv[sys.argv.index("--dt") + 1]) if "--dt" in sys.argv else 5.0
SIM_SECONDS = 14 * 86400  # 09-03T00 ~ 09-17T00（前 2 天基流充河槽）
T0_DAY = 3  # 起算日（9 月 3 日）
Q_BASE = 1000.0
Q_PEAK = float(sys.argv[sys.argv.index("--qpeak") + 1]) if "--qpeak" in sys.argv else 11500.0
CHANNEL = sys.argv[sys.argv.index("--channel") + 1] if "--channel" in sys.argv else "off"
if CHANNEL not in ("off", "linear", "hydraulic"):
    raise SystemExit(f"--channel 只支持 off|linear|hydraulic，实得 {CHANNEL!r}")
# ADR-0012 hydraulic 模式：DEM 直接取水力重构层（scripts/stage3/build_guigang_channel_dem.py
# 产出），不重算走廊刻蚀；bci 点源 = 中心线上游注入点；主槽糙率经 manningfile 覆写。
CHANNEL_DEM = (Path(sys.argv[sys.argv.index("--channel-dem") + 1])
               if "--channel-dem" in sys.argv else None)
N_CHANNEL = (float(sys.argv[sys.argv.index("--n-channel") + 1])
             if "--n-channel" in sys.argv else 0.030)
CENTERLINE_GJ = PROJ / "data" / "interim" / "guigang" / "yujiang_centerline.geojson"
PRIORS_JSON = PROJ / "data" / "catalog" / "guigang_channel_priors.json"
CH_DEPTH_M: float | None = None
if CHANNEL == "hydraulic":
    import re as _re
    if CHANNEL_DEM is None or not CHANNEL_DEM.is_dir():
        raise SystemExit("--channel hydraulic 需要 --channel-dem <channel_recon/dXXm> 目录")
    _m = _re.match(r"d(\d+)m", CHANNEL_DEM.name)  # 允许后缀（如 d14m_drA，ADR-0014）
    if not _m:
        raise SystemExit(f"--channel-dem 目录名须为 dXXm，实得 {CHANNEL_DEM.name}")
    CH_DEPTH_M = float(_m.group(1))
    if not (0.020 <= N_CHANNEL <= 0.050):
        raise SystemExit(f"--n-channel 超出合理范围 [0.020, 0.050]：{N_CHANNEL}")


def _infiltration_check(rate_mm_h: float | None, rain: str) -> float | None:
    """ADR-0014 路径 B 单用纪律：求解器入渗只允许配毛雨（--rain on），禁与净雨预处理混用。"""
    if rate_mm_h is None:
        return None
    if rain != "on":
        raise SystemExit(
            f"--infiltration 只允许与 --rain on（毛雨）组合；实得 --rain {rain}。"
            "入渗 + 净雨预处理会重复扣损（ADR-0011 决策 2）")
    if not (0.05 <= rate_mm_h <= 5.0):
        raise SystemExit(f"--infiltration 超出合理范围 [0.05, 5.0] mm/h：{rate_mm_h}")
    return float(rate_mm_h)


# 河槽宽按米参数化（终稿 六.5 情景意图「宽 600m」）：半宽格数随分辨率折算，
# 200m 下 = 3 格（r-1..r+2），与首轮基线逐位一致；网格变细时保持物理宽度不变（B5 网格收敛前提）。
CHANNEL_WIDTH_M = float(sys.argv[sys.argv.index("--channel-width") + 1]) if "--channel-width" in sys.argv else 600.0
CH_HALF = max(1, int(round(CHANNEL_WIDTH_M / RES)) // 2)
# Phase 2 物理驱动重构：降雨模式。默认 on = 历史行为，逐位向后兼容。
# ADR-0013 新增 ga：Phase G 预处理器 GA-Mid 物理净雨（+48h 时基移位，路径 A）。
RAIN_MODES = ("on", "off", "loss_medium", "loss_high", "ga")
RAIN = sys.argv[sys.argv.index("--rain") + 1] if "--rain" in sys.argv else "on"
if RAIN not in RAIN_MODES:
    raise SystemExit(f"--rain 只支持 {RAIN_MODES}，收到 {RAIN!r}")
INFILTRATION_MM_H = _infiltration_check(
    float(sys.argv[sys.argv.index("--infiltration") + 1]) if "--infiltration" in sys.argv else None,
    RAIN if "--rain" in sys.argv else "on")
# 初损/持续损失预注册参数（计划 §Phase2 方案 2；跑前冻结，禁止按结果事后调档）
RAIN_LOSS = {
    "loss_medium": {"Ia_mm": 30.0, "fc_mm_per_h": 2.0},
    "loss_high": {"Ia_mm": 50.0, "fc_mm_per_h": 3.0},
}
ANCHOR = ("贵港市区郁江段", 109.60, 23.11)
ANCHOR_WSE_DIAG = 43.09  # 贵港站 09-12T14 水位（站基面，垂直基准未确认 G7，仅诊断）
GAUGE_0912_M = 43.09
GAUGE_0914_M = 44.00
T_GAUGE_0912_H = 228.0
T_GAUGE_0914_H = 270.0
RUN_TAG = os.environ.get("RUN_TAG", "").strip()
ch_tag = {"off": "_choff", "linear": ""}.get(CHANNEL)
if ch_tag is None:  # hydraulic
    ch_tag = f"_chhyd_d{int(CH_DEPTH_M)}"
RUN_DIR = PROJ / "outputs" / "guigang" / "2024-09-yagi" / (
    f"scen_yagi_q{int(Q_PEAK)}_{int(RES)}m"
    + (f"_dt{str(DT).replace('.', 'p')}" if abs(DT - 5.0) > 1e-9 else "")
    + ch_tag
    + (f"_{RUN_TAG}" if RUN_TAG else ""))


def _t(day: int, hh: int = 0) -> float:
    return (day - T0_DAY) * 86400.0 + hh * 3600.0


def _load_hydraulic_dem(recon_dir: Path, b, res: float, shape: tuple[int, int]) -> tuple[np.ndarray, dict]:
    """ADR-0012 hydraulic 模式：加载水力重构层并做门禁（validation 全过 + 网格一致）。

    返回 (dem, {injection_32649, meta})；dem 直接来自
    scripts/stage3/build_guigang_channel_dem.py 的产出，本函数不做任何再刻蚀。
    重构层床面可低于高程基准（datum 伪影，登记于 carve_statistics.dem_min_m），
    G16 负值断言只适用于上方 raw 走廊。
    """
    asc = recon_dir / "dem_reconstructed_100m.asc"
    stats_p = recon_dir / "carve_statistics.json"
    if not (asc.is_file() and stats_p.is_file()):
        raise SystemExit(f"{recon_dir} 缺 dem_reconstructed_100m.asc / carve_statistics.json")
    stats = json.loads(stats_p.read_text(encoding="utf-8"))
    val = stats.get("validation", {})
    if not val.get("all_pass", False):
        raise SystemExit(
            f"{recon_dir} 的 carve validation 未全过（all_pass={val.get('all_pass')}）；拒绝用于 run")
    hdr = read_ascii_header(asc)
    if not (int(hdr["ncols"]) == shape[1] and int(hdr["nrows"]) == shape[0]
            and abs(hdr["cellsize"] - res) < 1e-6
            and abs(hdr["xllcorner"] - b[0]) < 0.05 and abs(hdr["yllcorner"] - b[1]) < 0.05):
        raise SystemExit(
            f"{asc} 网格头与走廊不一致：{hdr} vs ncols={shape[1]} nrows={shape[0]} "
            f"cellsize={res} xll={b[0]:.1f} yll={b[1]:.1f}")
    dem = np.loadtxt(asc, skiprows=6, dtype="float32", ndmin=2)
    if dem.shape != shape:
        raise SystemExit(f"{asc} 数据形状 {dem.shape} 与头声明 {shape} 不符")
    gj = json.loads(CENTERLINE_GJ.read_text(encoding="utf-8"))
    props = gj["features"][0]["properties"]
    ix, iy = props["upstream_injection_32649"]
    meta = {
        "mode": "hydraulic",
        "depth_m": CH_DEPTH_M,
        "carved_px": int(stats["modified_pixel_count"]),
        "recon_dir": str(CHANNEL_DEM),
        "recon_dem_sha256": sha256_file(asc),
        "priors_sha256": stats.get("priors_sha256"),
        "priors_matrix_id": "guigang-channel-priors-v1",
        "centerline_source": stats.get("centerline_source"),
        "centerline_sha256": sha256_file(CENTERLINE_GJ),
        "profile": "smooth_trapezoid（4×4 亚采样，Z_new=min(Z_orig, Z_ideal)）",
        "dem_min_m": stats.get("dem_min_m"),
        "carve_validation": "all_pass=True（几何6项+输水核验，见 recon 目录 carve_statistics.json）",
        "method": "ADR-0012 水力约束河槽重构（OSM 中心线 + Manning 反演深度）——水力一致性先验地形，非实测 bathymetry",
    }
    return dem, {"injection_32649": (float(ix), float(iy)), "meta": meta}


def _write_manning_map(recon_dir: Path, n_channel: float, run_dir: Path,
                       res: float, b, shape: tuple[int, int]) -> int:
    """主槽糙率栅格（manningfile）：掩膜内 n_channel，其余 nodata=-9999 回退 fpfric。

    LoadManningsn（input.cpp:909-940）：读 6 行 ESRI 头，nodata 格替换为 FPn。
    """
    with rasterio.open(recon_dir / "channel_mask_100m.tif") as src:
        mask = src.read(1) == 1
        if (src.height, src.width) != shape:
            raise SystemExit(f"channel_mask 形状 {(src.height, src.width)} 与走廊 {shape} 不一致")
    grid = np.full(shape, -9999.0, dtype="float64")
    grid[mask] = float(n_channel)
    np.savetxt(run_dir / "manning.map", grid, fmt="%.4f", delimiter="\t",
               header=f"ncols {shape[1]}\nnrows {shape[0]}\nxllcorner {b[0]:.1f}\n"
                      f"yllcorner {b[1]:.1f}\ncellsize {res:.1f}\nNODATA_value -9999.0",
               comments="")
    return int(mask.sum())


def apply_initial_continuing_loss(
    gross_mm: list[float], *, ia_mm: float, fc_mm_per_h: float
) -> dict:
    """Initial Loss / Continuing Loss 净雨变换（计划 §Phase2 路径 A）。

    参数
    ----
    gross_mm     逐时毛雨量（mm/h），按时间升序；缺失时段应显式补 0（见调用方）。
    ia_mm        初损 Ia（mm）。
    fc_mm_per_h  持续入渗率 fc（mm/h）。

    规则（与 team task 及本模块 docstring 完全一致）
    -------------------------------------------------
    1. 累计毛雨 <= Ia 的整小时：净雨 0；
    2. **累计毛雨达到 Ia 的那一小时**：净雨 = 该小时雨量 − (Ia − 此前累计)；
    3. 穿越 Ia 之后每小时：净雨 = max(0, 雨量 − fc)；
    4. 净雨恒不为负（`max(0.0, ...)`）。

    第 2 条的"达到即穿越"是本项目实测口径，用于复现两个预注册预期值
    （loss_medium 45.40 mm / 11 h、loss_high 36.10 mm / 8 h）。改为"严格大于"会得到
    loss_medium 47.20 mm / 12 h，与预期不符 → 口径已冻结，不得按结果改动。

    返回 dict：net_mm（逐时净雨）、毛雨/净雨累计、有净雨小时数、穿越时刻与实现初损等。
    """
    net: list[float] = []
    cum = 0.0
    saturated = False
    cross_index: int | None = None
    ia_realised = 0.0
    for idx, rain in enumerate(gross_mm):
        rain = max(0.0, float(rain))
        if not saturated:
            if cum + rain < ia_mm:
                # 累计仍未达到 Ia：整小时净雨 0
                net.append(0.0)
                cum += rain
                continue
            # 累计达到/跨过 Ia：本小时净雨 = 雨量 − 尚缺的初损
            deficit = ia_mm - cum
            net.append(max(0.0, rain - deficit))
            ia_realised += min(rain, deficit)
            cum += rain
            saturated = True
            cross_index = idx
        else:
            net.append(max(0.0, rain - fc_mm_per_h))
    return {
        "net_mm": net,
        "gross_total_mm": round(float(sum(gross_mm)), 4),
        "net_total_mm": round(float(sum(net)), 4),
        "gross_hours_with_rain": sum(1 for v in gross_mm if v > 0),
        "net_hours_with_rain": sum(1 for v in net if v > 0),
        "crossing_hour_index": cross_index,
        "initial_abstraction_realised_mm": round(ia_realised, 4),
        "initial_abstraction_target_mm": ia_mm,
        "fc_mm_per_h": fc_mm_per_h,
        "net_rain_non_negative": all(v >= 0.0 for v in net),
        "net_fraction_of_gross": round(float(sum(net)) / max(float(sum(gross_mm)), 1e-9), 4),
    }


def prep() -> None:
    # 源 DEM 缺失时必须在建目录之前失败：否则会留下空 run 目录，
    # 被后续 g15 质量门当作独立 run 统计。
    if RAW is None:
        raise SystemExit(
            "贵港情景需要源 DEM（广西.tif）重建走廊：当前未找到。\n"
            "  设置环境变量 GX_DEM_SOURCE=<路径>，或把文件放到 "
            "data/raw/dem_source/广西.tif，\n"
            "  或在 configs/paths.local.yaml 写 DEM_SOURCE: <路径>"
        )
    # 终稿 §10 双保险：run_manifest.prepare_run_dir 与 grid_io.guard_no_overwrite
    # 都按 results/res.mass 是否存在拒绝覆盖。
    from scripts.common.grid_io import OverwriteRefused, guard_no_overwrite
    try:
        guard_no_overwrite(RUN_DIR)
    except OverwriteRefused as exc:
        raise SystemExit(str(exc)) from None
    try:
        from scripts.common.run_manifest import prepare_run_dir
        prepare_run_dir(RUN_DIR)
    except FileExistsError:
        raise SystemExit(f"{RUN_DIR} 已有 res.mass；终稿 §10 禁止覆盖。请设置 RUN_TAG 使用新目录。") from None
    tf = Transformer.from_crs(4326, 32649, always_xy=True)
    corners = [tf.transform(x, y) for x, y in [(AOI[0], AOI[1]), (AOI[2], AOI[3])]]
    b = (min(c[0] for c in corners), min(c[1] for c in corners),
         max(c[0] for c in corners), max(c[1] for c in corners))
    tgt = rasterio.Affine(RES, 0, b[0], 0, -RES, b[3])
    shape = (int((b[3] - b[1]) / RES), int((b[2] - b[0]) / RES))
    with rasterio.open(RAW) as src:
        with WarpedVRT(src, crs="EPSG:32649", resampling=Resampling.bilinear,
                       width=shape[1], height=shape[0], transform=tgt) as v:
            dem = v.read(1).astype("float32")
    h, w = dem.shape
    dem[dem == -9999.0] = np.nanmax(dem[dem != -9999.0])
    assert not (dem < 0).any(), "贵港走廊出现负值（G16 预排查为 0，若触发须复核）"
    dem_pre_edit = dem.copy()  # 河槽刻蚀前的原始地形（用于地形修改留痕）

    # S1 09-05 常水水体 -> 河道定位
    # 河道定位：D8 山谷追踪（锚点吸附谷底，上下游各走）。S1 掩膜路线三版皆败
    # （足印覆盖 58%/74% 截断致分量碎裂、端点退化，2026-10-25），改用 DEM 山谷本身；
    # S1 掩膜仅用于验证（verify），不再参与河槽几何
    # 走廊 DEM 实测为宽平底平原（平滑面低带 16~27m 遍布全域，200m 下无河谷可循；
    # 城区江段未被 S1 检出，D8 无梯度可走，2026-10-25 三版追踪皆败）——按终稿 六.5
    # 参数化河槽：过锚点沿郁江过境走向直线河槽，宽 3 格（600m），
    # 床 = 沿线地表 polyfit 线性 - 12m。几何为情景假设，成果必须标注。
    # 走向取 100°（东偏南 ~10°）：低带实测在南半部（行 130~154），70° 走向北漂进山（首版 bed 98m 已废弃）
    # 另：城区平原 DEM ~21m vs 贵港站洪峰 43.09m -> 垂直基准差约 -20m（G7 实证，对照仅诊断）
    arow, acol = rasterio.transform.rowcol(tgt, *tf.transform(ANCHOR[1], ANCHOR[2]))
    arow, acol = int(arow), int(acol)
    dc, dr = np.sin(np.radians(100.0)), -np.cos(np.radians(100.0))  # 东分量->列，北分量->行（行向下为北减）
    ts = np.arange(-2 * max(h, w), 2 * max(h, w) + 1, 0.5)
    rr = np.round(arow + ts * dr).astype(int)
    cc = np.round(acol + ts * dc).astype(int)
    inb = (rr >= 0) & (rr < h) & (cc >= 0) & (cc < w)
    path = list(dict.fromkeys((int(r), int(c)) for r, c in zip(rr[inb], cc[inb])))  # 保序去重
    if CHANNEL != "hydraulic":
        print(f"河槽(参数化直线): {len(path)} 节点，过锚点(行{arow},列{acol})，走向 100°/280°，"
              f"宽 {2 * CH_HALF + 1} 格（≈{(2 * CH_HALF + 1) * RES:.0f}m，目标 {CHANNEL_WIDTH_M:.0f}m）")
    seen = set()
    path = [p for p in path if not (p in seen or seen.add(p))]  # 去重保序

    carved = 0
    if CHANNEL == "linear":
        # 河槽纵坡 v2（2026-10-25 二修）：v1 polyfit 拷贝了沿线地形 30m 落差（真郁江该河段 ~8m），
        # 中段岸深被拉薄 -> 11500m3/s 沿线漫溢（首轮湿 487km²）。
        # v2：床 = 沿线局部地表（±1 带最小值，15 节点滑动均值）- 12m，再向下游累计取小保证单调
        # —— 岸深处处 12m，纵坡跟随地形但不会平均化拉薄中段岸深。
        surf = []
        for r, c in path:
            r0, r1 = max(0, r - CH_HALF), min(h, r + CH_HALF + 1)
            c0, c1 = max(0, c - CH_HALF), min(w, c + CH_HALF + 1)
            surf.append(float(dem[r0:r1, c0:c1].min()))
        surf = np.array(surf)
        ker = np.ones(15) / 15.0
        surf_s = np.convolve(np.pad(surf, 7, mode="edge"), ker, mode="valid")
        bed_line = surf_s - 12.0
        bed_line = np.minimum.accumulate(bed_line)  # 向下游累计取小（单调不升）
        for i, (r, c) in enumerate(path):
            r0, r1 = max(0, r - CH_HALF), min(h, r + CH_HALF + 1)
            c0, c1 = max(0, c - CH_HALF), min(w, c + CH_HALF + 1)
            bed_c = float(bed_line[i])
            sub = dem[r0:r1, c0:c1]
            loc = sub > bed_c + 1.5
            if loc.any():
                sub[loc] = bed_c
                dem[r0:r1, c0:c1] = sub
                carved += int(loc.sum())
        bed_w, bed_e = float(bed_line[0]), float(bed_line[-1])
        if not (0 < bed_w - bed_e <= 60 and 0 < bed_e < 80):
            raise SystemExit(f"河槽底坡不合理：bed_up={bed_w:.1f} bed_dn={bed_e:.1f}（主分量选错或 DEM 异常）")
        print(f"河槽: 路径 {len(path)} 节点（起点(行{path[0][0]},列{path[0][1]})->终点(行{path[-1][0]},列{path[-1][1]})），"
              f"刻蚀 {carved} 格，底 上游{bed_w:.1f}->下游{bed_e:.1f} m")
    elif CHANNEL == "off":
        bed_w = float(dem[path[0][0], path[0][1]])
        bed_e = float(dem[path[-1][0], path[-1][1]])
        print(f"河槽[off模式]: 未施加任何人工河槽刻蚀与虚假下切。路径起点底高={bed_w:.1f}m，终点={bed_e:.1f}m")

    if CHANNEL == "hydraulic":
        # ADR-0012：DEM 整体替换为水力重构层（validation 全过才放行），点源 = 中心线注入点
        dem, hyd = _load_hydraulic_dem(CHANNEL_DEM, b, RES, (h, w))
        sx, sy = hyd["injection_32649"]
        manning_px = _write_manning_map(CHANNEL_DEM, N_CHANNEL, RUN_DIR, RES, b, (h, w))
        print(f"河槽[hydraulic]: D={CH_DEPTH_M:.0f} m 重构层已加载（{CHANNEL_DEM.name}），"
              f"刻蚀 {hyd['meta']['carved_px']} 格；主槽 n={N_CHANNEL}（manning.map，{manning_px} 格），"
              f"滩地回退 fpfric=0.035；注入点({sx:.1f},{sy:.1f})")

    # 地形修改留痕（可视化「地形修改证据」层与审计读取）
    edit_mask = np.abs(dem - dem_pre_edit) > 1e-6
    if edit_mask.any():
        np.savetxt(RUN_DIR / "dem_pre_carve.diff.txt",
                   np.where(edit_mask, dem - dem_pre_edit, 0.0),
                   fmt="%.2f", delimiter="\t",
                   header=f"ncols {w}\nnrows {h}\nxllcorner {b[0]:.1f}\nyllcorner {b[1]:.1f}\n"
                          f"cellsize {RES:.1f}\nNODATA_value -9999.0", comments="")
        base = {"driver": "GTiff", "height": h, "width": w, "count": 1,
                "crs": "EPSG:32649", "transform": tgt, "compress": "lzw"}
        with rasterio.open(RUN_DIR / "dem_edit_mask.tif", "w", dtype="uint8", **base) as dst:
            dst.write(edit_mask.astype("uint8"), 1)
        with rasterio.open(RUN_DIR / "dem_edit_diff.tif", "w", dtype="float32",
                           nodata=0.0, **base) as dst:
            dst.write(np.where(edit_mask, dem - dem_pre_edit, 0.0).astype("float32"), 1)
    if CHANNEL != "hydraulic":
        sx, sy = tgt * (path[0][1] + 0.5, path[0][0] + 0.5)  # 路径起点（上游）= 点源
    # （hydraulic 模式的点源已由中心线注入点覆写，见上方 hydraulic 分支）
    np.savetxt(RUN_DIR / "dem.asc", dem, fmt="%.3f", delimiter="\t",
               header=f"ncols         {w}\nnrows         {h}\nxllcorner     {b[0]:.1f}\n"
                      f"yllcorner     {b[1]:.1f}\ncellsize      {RES:.1f}\nNODATA_value  -9999.0",
               comments="")

    BIG = 99999999
    (RUN_DIR / "bci.bci").write_text(
        f"P\t{sx:.1f}\t{sy:.1f}\tQVAR\tyujiang\n"
        f"E\t-{BIG}\t{BIG}\tFREE\t0.00015\n"
        f"W\t-{BIG}\t{BIG}\tFREE\t0.0001\n"
        f"N\t-{BIG}\t{BIG}\tFREE\nS\t-{BIG}\t{BIG}\tFREE\n", encoding="ascii", newline="\n")

    # 上游三角 Q（标度律 B6：文件值 = Q/网格边长）
    scale = RES
    ts_q = [(0.0, Q_BASE), (_t(8), Q_BASE), (_t(12, 12), Q_PEAK), (_t(16, 12), 2500.0),
            (SIM_SECONDS, 2000.0)]
    (RUN_DIR / "yujiang.bdy").write_text(
        "# scenario upstream hydrograph (triangular, public-peak anchored); file_value = Q_m3s / cellsize\n"
        f"yujiang\n{len(ts_q)} seconds\n" +
        "".join(f"{q / scale:.4f}\t{t:.0f}\n" for t, q in ts_q), encoding="ascii", newline="\n")

    hourly = json.loads(ERA5.read_text(encoding="utf-8"))["hourly_mm"]
    pts = [(0.0, 0.0)]
    for tkey in sorted(hourly):
        day, hh = tkey.split("T")
        t = (int(day[-2:]) - T0_DAY) * 86400 + int(hh[:2]) * 3600
        if 0 < t <= SIM_SECONDS:
            pts.append((hourly[tkey], float(t)))
    pts.append((0.0, SIM_SECONDS))
    pts = sorted({t: v for v, t in pts}.items())  # 去重：末日T00与末点重号会致时刻非递增（实测 exit -1）
    assert all(b[0] > a[0] for a, b in zip(pts, pts[1:])), "rain 时刻必须严格递增"

    # ---- 降雨模式分派（RAIN：on / off / loss_medium / loss_high / ga）----
    # 时间轴与 on 模式逐点同源；off/loss 只改数值，不改时刻（保证与基线可比）。
    # ga 例外：时轴来自预处理器净雨序列（+48h 移位后与 on 模式同窗，ADR-0013）。
    times_s = [t for t, _v in pts]
    gross_vals = [float(v) for _t_s, v in pts]
    rain_meta: dict = {"mode": RAIN}
    if RAIN == "on":
        rain_vals = gross_vals
        rain_header = "# ERA5 guigang anchor hourly uniform rain (degraded)\n"
        rain_meta.update({
            "description": "ERA5 市区锚点逐时均匀雨，未施加任何损失（历史基线口径）",
            "gross_total_mm": round(float(sum(gross_vals)), 4),
            "net_total_mm": round(float(sum(rain_vals)), 4),
            "loss_applied": False,
        })
    elif RAIN == "off":
        # 纯过境来水驱动：不写 rain.rain，run.par 删除 rainfall 行。
        # 不采用「全零 rain.rain」——两法数值等价（全域零雨），但删行更明确，
        # 且不产生一个可能被误读为「有降雨输入」的文件。
        rain_vals = None
        rain_header = None
        rain_meta.update({
            "description": "纯上游来水驱动（rain=off）：不生成 rain.rain，run.par 不含 rainfall 行",
            "gross_total_mm": 0.0,
            "net_total_mm": 0.0,
            "loss_applied": False,
            "era5_gross_reference_mm": round(float(sum(gross_vals)), 4),
            "era5_gross_reference_note": (
                "ERA5 原始序列累计值，仅作对照登记；本 run 未使用该序列"
            ),
        })
    elif RAIN == "ga":
        # ADR-0013：GA-Mid 物理净雨（Phase G 预处理器，路径 A；run.par 仍不写 infiltration）。
        if not PREPROCESSOR_RAIN.is_file():
            raise SystemExit(
                f"GA 净雨文件缺失：{PREPROCESSOR_RAIN}"
                "（先运行 rainfall_runoff_preprocessor --case guigang 生成）")
        offset_s = derive_probe_offset_s(ERA5, T0_DAY)
        nr = read_preprocessor_net_rain(PREPROCESSOR_RAIN, t_offset_s=offset_s,
                                        sim_seconds=SIM_SECONDS)
        times_s = [t for _r, t in nr["points"]]
        rain_vals = [r for r, _t in nr["points"]]
        ga_params = {}
        if PREPROCESSOR_SUMMARY.is_file():
            _s = json.loads(PREPROCESSOR_SUMMARY.read_text(encoding="utf-8"))
            ga_params = _s.get("scenarios", {}).get("GA-Mid", {})
        rain_header = (
            f"# GA-Mid physical net rain (Phase G preprocessor; source sha256 "
            f"{nr['source_sha256'][:16]}...; time offset +{offset_s / 3600:.0f} h; "
            f"window-clipped to {SIM_SECONDS:.0f} s)\n"
        )
        rain_meta.update({
            "description": "GA-Mid 物理净雨（Phase G 预处理器 Green-Ampt，路径 A 净雨预处理）",
            "loss_applied": True,
            "method": "Green-Ampt GA-Mid（区域先验合成土壤参数，1 min 子步积分，未改求解器）",
            "source_file": str(PREPROCESSOR_RAIN),
            "source_sha256": nr["source_sha256"],
            "time_offset_h": round(offset_s / 3600.0, 1),
            "source_total_mm": nr["total_mm"],
            "net_total_mm": nr["total_mm_in_window"],
            "tail_dropped_mm": nr["tail_dropped_mm"],
            "ga_params": ga_params,
            "gross_total_mm": float(ga_params.get("gross_rain_mm", 137.1)),
            "gross_reference_mm": round(float(sum(gross_vals)), 4),
            "gross_reference_note": "ERA5 probe 模型窗内毛雨（对照口径）；GA 源序列毛雨为全序列 137.1 mm（G1 对账：差=窗口裁剪）",
        })
    else:
        spec = RAIN_LOSS[RAIN]
        loss = apply_initial_continuing_loss(
            gross_vals, ia_mm=spec["Ia_mm"], fc_mm_per_h=spec["fc_mm_per_h"])
        rain_vals = loss["net_mm"]
        rain_header = (
            "# ERA5 guigang anchor hourly uniform NET rain: "
            f"initial loss Ia={spec['Ia_mm']} mm + continuing loss fc={spec['fc_mm_per_h']} mm/h\n"
        )
        cross_i = loss["crossing_hour_index"]
        rain_meta.update({
            "description": (
                f"净雨预处理（计划 §Phase2 路径 A）：初损 Ia={spec['Ia_mm']} mm，"
                f"持续入渗 fc={spec['fc_mm_per_h']} mm/h"
            ),
            "loss_applied": True,
            "method": "Initial Loss / Continuing Loss（净雨预处理，未改求解器）",
            "ia_mm": spec["Ia_mm"],
            "fc_mm_per_h": spec["fc_mm_per_h"],
            "gross_total_mm": loss["gross_total_mm"],
            "net_total_mm": loss["net_total_mm"],
            "gross_hours_with_rain": loss["gross_hours_with_rain"],
            "net_hours_with_rain": loss["net_hours_with_rain"],
            "net_fraction_of_gross": loss["net_fraction_of_gross"],
            "initial_abstraction_realised_mm": loss["initial_abstraction_realised_mm"],
            "crossing_rule": "累计毛雨达到 Ia 的那一小时即穿越（cum+rain >= Ia）",
            "crossing_time_h": round(times_s[cross_i] / 3600.0, 2) if cross_i is not None else None,
            "crossing_time_utc_local_note": "t_h 为相对 2024-09-03T00 Asia/Shanghai 的小时数",
            "net_rain_non_negative": loss["net_rain_non_negative"],
        })
    if rain_vals is not None:
        (RUN_DIR / "rain.rain").write_text(
            rain_header + f"{len(times_s)} seconds\n" +
            "".join(f"{v:.4f}\t{t:.0f}\n" for t, v in zip(times_s, rain_vals)),
            encoding="ascii", newline="\n")
    # 口径登记：本构建的标量 infiltration 只对 H>1e-3 m 的湿单元扣水，
    # 无法表达初损；且 loss 情景的 fc 已在雨量序列上扣过，再写入渗会重复计数。
    rain_meta["infiltration_in_par"] = INFILTRATION_MM_H
    if INFILTRATION_MM_H:
        rain_meta["infiltration_note"] = (
            f"路径 B 单用（ADR-0014 诊断探针）：run.par 写 infiltration={INFILTRATION_MM_H} mm/h"
            f"（{INFILTRATION_MM_H / 3600 / 1000:.3e} m/s）。FPInfiltration"
            "（infevap.cpp:14-46）对 H>1e-3 m 湿格持续扣水（含主槽水面，量级约入流 0.4%，已登记）；"
            "毛雨未做净雨预处理，无重复扣损。恒定率无土壤库，事件后期可能过排（限制登记）；"
            "速率取华南红壤稳渗先验 2~3 mm/h 界内。")
    else:
        rain_meta["infiltration_note"] = (
            "本 run 的 run.par **不含** infiltration 行。原因：(1) FPInfiltration"
            "（infevap.cpp:14-46）只对 H>1e-3 m 的湿单元生效，无法表达初损；"
            "(2) loss 情景的 fc 已在净雨序列上扣过，再写 infiltration 会把 fc 重复扣一次。"
            "计划 §Phase2 路径 B 在本构建不可与路径 A 叠加，已如实登记。"
        )

    (RUN_DIR / "run.par").write_text(
        "DEMfile\tdem.asc\nbcifile\tbci.bci\nbdyfile\tyujiang.bdy\n"
        + ("rainfall\train.rain\n" if rain_vals is not None else "")
        + ("manningfile\tmanning.map\n" if CHANNEL == "hydraulic" else "")
        + (f"infiltration\t{INFILTRATION_MM_H / 3600.0 / 1000.0:.9e}\n" if INFILTRATION_MM_H else "")
        + f"resroot\tres\ndirroot\tresults\nsim_time\t{SIM_SECONDS:.0f}\n"
        f"initial_tstep\t{DT}\nmassint\t3600\nsaveint\t21600\n"
        f"fpfric\t0.035\nacceleration\n", encoding="ascii", newline="\n")
    if CHANNEL == "off":
        ch_meta = {
            "mode": "off",
            "carved_px": 0,
            "path_nodes": len(path),
            "note": "off: 真实DEM未施加任何人工河槽刻蚀与虚假下切，严格采用源地形",
        }
    elif CHANNEL == "hydraulic":
        ch_meta = {**hyd["meta"], "n_channel": N_CHANNEL,
                   "floodplain_note": "滩地维持基线 fpfric=0.035（manningfile 掩膜外 nodata 回退）；"
                                      "首轮单变量控制，滩地糙率敏感性留第 2 轮（用户 2026-11-07 决策）"}
    else:
        ch_meta = {
            "mode": "linear",
            "path_nodes": len(path), "carved_px": carved,
            "bed_up_m": round(bed_w, 1), "bed_dn_m": round(bed_e, 1),
            "depth_m": 12.0, "width_cells": 2 * CH_HALF + 1,
            "width_actual_m": round((2 * CH_HALF + 1) * RES), "width_target_m": CHANNEL_WIDTH_M,
            "method_version": "v3",
            "method": "参数化直线河槽（过市区锚点、走向100°/280°、宽按米参数化≈600m、床=局部地表−12m + 下游累计取小）——走廊为宽平底平原无河谷可循，纯情景假设；v3 起宽度按米跨分辨率一致（200m 下与 v2 逐位同）",
        }
    meta = {"run_id": RUN_DIR.name, "aoi": AOI, "res": RES, "dt": DT,
            "q_base": Q_BASE, "q_peak": Q_PEAK, "sim_seconds": SIM_SECONDS,
            "t0": "2024-09-03T00 Asia/Shanghai（前2日基流充槽）",
            "point_source_32649": [round(sx, 1), round(sy, 1)],
            "channel": ch_meta,
            "rain": rain_meta,
            "evidence": "南宁站 09-12T19:30 峰 11900m3/s；贵港站 09-12T14 43.09m/10500m3/s（公开转引）",
            "note": "上游 Q 为情景三角线（无逐时实测）；WSE 与站水位对照受 G7 垂直基准未确认限制，仅诊断；情景级结论",
            "generated_utc": datetime.now(timezone.utc).isoformat()}
    if RAIN != "on":
        meta["run_tag"] = RUN_TAG
        meta["scenario"] = {
            "off": "flow_only_v1 纯过境来水驱动对照",
            "loss_medium": "dualloss_v1 双驱动（来水 + 降雨初损 Ia=30mm / fc=2.0mm·h⁻¹）",
            "loss_high": "rainloss_high_v1 双驱动高入渗（来水 + 降雨初损 Ia=50mm / fc=3.0mm·h⁻¹）",
            "ga": "combo_ga GA-Mid 物理净雨（Phase G 预处理器，路径 A，ADR-0013 组合实验）",
        }[RAIN]
        held_fixed = ["res=100", "q_peak=11500", "q_base=1000", "dt=2.5",
                      "sim_time=1209600", "fpfric=0.035",
                      "saveint=21600", "massint=3600", "acceleration"]
        if CHANNEL == "hydraulic":
            meta["baseline_comparison"] = {
                "baseline_run": "scen_yagi_q11500_100m_dt2p5_choff_realdem",
                "held_fixed": held_fixed,
                "variables": [f"channel=hydraulic D={CH_DEPTH_M:.0f} m（ADR-0012）",
                              f"rain={RAIN}（ADR-0013 净雨口径）"],
                "only_variable": "channel + rain（双变量；归因靠 2×2 因子差分，"
                                 "见 data/catalog/guigang_combo_attribution.json）",
            }
        else:
            meta["baseline_comparison"] = {
                "baseline_run": "scen_yagi_q11500_100m_dt2p5_choff_realdem",
                "held_fixed": held_fixed + ["channel=off"],
                "only_variable": "rain（降雨输入模式与雨量序列）",
            }
        meta["phase2_plan_ref"] = (
            "plans/贵港水动力推演效果改进实施计划_从零散水点治理到上游来水与降雨双驱动重构.md"
            " §Phase2 / §Phase4"
        )
    if CHANNEL == "hydraulic":
        refs = ["docs/adr/ADR-0012-贵港水力约束河槽重构.md"]
        if RAIN == "ga":
            refs.append("docs/adr/ADR-0013-贵港组合驱动归因实验.md")
        if INFILTRATION_MM_H or "dr" in CHANNEL_DEM.name:
            refs.append("docs/adr/ADR-0014-贵港排水连通治理.md")
        meta["adr_ref"] = refs
        if INFILTRATION_MM_H or "dr" in CHANNEL_DEM.name:
            meta["probe_disclaimer"] = (
                "诊断探针（ADR-0014）：DEM 推导排水沟/求解器入渗均为情景假设，"
                "用于量化「多强排水能力能解释残余 FAR」，不是真实排水网，不得作资产判据")
        if RAIN == "on":
            # 单变量口径（rain=on 与基线同源）。RAIN != on 的双变量登记在上一个
            # RAIN != "on" 分支（ADR-0013），不得在此覆盖。
            meta["baseline_comparison"] = {
                "baseline_run": "scen_yagi_q11500_100m_dt2p5_choff_realdem",
                "held_fixed": ["res=100", "q_peak=11500", "q_base=1000", "dt=2.5",
                               "sim_time=1209600", "fpfric=0.035",
                               "saveint=21600", "massint=3600", "acceleration",
                               "rain=on（ERA5 原始序列逐点同源）"],
                "only_variable": (f"channel（ADR-0012 水力约束重构 DEM，D={CH_DEPTH_M:.0f} m，"
                                  f"manningfile 主槽 n={N_CHANNEL}；滩地 fpfric 不变）"),
                "note": "点源位置随中心线上游端移至走廊南缘入域处（真实河道入口），与基线西缘注入不同——属几何一致性变更，已登记",
            }
    (RUN_DIR / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print("prep 完成 ->", RUN_DIR)
    if RAIN == "ga":
        print(f"净雨: GA-Mid 毛雨 {rain_meta['gross_total_mm']} mm -> 窗内净雨 "
              f"{rain_meta['net_total_mm']} mm"
              f"（{rain_meta['net_total_mm'] / rain_meta['gross_total_mm'] * 100:.2f}%），"
              f"时基 +{rain_meta['time_offset_h']:.0f} h，尾部裁 {rain_meta['tail_dropped_mm']} mm")
    elif rain_meta.get("loss_applied"):
        print(f"净雨: 毛雨 {rain_meta['gross_total_mm']} mm -> 净雨 {rain_meta['net_total_mm']} mm"
              f"（{rain_meta['net_fraction_of_gross'] * 100:.2f}%），"
              f"有净雨 {rain_meta['net_hours_with_rain']} 小时，"
              f"穿越 Ia 于 t={rain_meta['crossing_time_h']} h")
    elif RAIN == "off":
        print("降雨: off（未生成 rain.rain，run.par 无 rainfall 行）")
    else:
        print(f"降雨: on（ERA5 原始序列累计 {rain_meta['gross_total_mm']} mm）"
              + (f"；入渗 {INFILTRATION_MM_H} mm/h（路径 B 诊断探针，ADR-0014）"
                 if INFILTRATION_MM_H else ""))


def run() -> dict:
    import psutil
    proc = subprocess.Popen([str(LISFLOOD), "run.par"], cwd=RUN_DIR,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    peak = {"mb": 0.0}
    p = psutil.Process(proc.pid)

    def poll():
        while proc.poll() is None:
            try:
                peak["mb"] = max(peak["mb"], p.memory_info().rss / 1e6)
            except Exception:
                break
            time.sleep(0.2)

    threading.Thread(target=poll, daemon=True).start()
    started_utc = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()
    out, _ = proc.communicate(timeout=24 * 3600)
    wall = time.monotonic() - t0
    (RUN_DIR / "run.log").write_text(out, encoding="utf-8", errors="replace")
    rec = {"exit": proc.returncode, "wall_s": round(wall, 1), "peak_rss_mb": round(peak["mb"], 1)}
    write_run_manifest_best_effort(
        RUN_DIR, peak_rss_mb=rec["peak_rss_mb"], started_utc=started_utc,
        warnings=["运行后清单；verify 完成后更新质量门。"],
    )
    print(json.dumps(rec), "（日志 run.log）")
    return rec


def _read_asc(p: Path):
    with open(p) as f:
        hdr = [next(f) for _ in range(6)]
    return np.loadtxt(p, skiprows=6), {l.split()[0]: float(l.split()[1]) for l in hdr}


def verify() -> None:
    res = RUN_DIR / "results"
    rmax, hdr = _read_asc(res / "res.max")
    cell = hdr["cellsize"]
    xll, yll = hdr["xllcorner"], hdr["yllcorner"]
    tgt = rasterio.Affine(cell, 0, xll, 0, -cell, yll + rmax.shape[0] * cell)

    # 锚点 WSE 序列（诊断，G7）
    tf = Transformer.from_crs(4326, 32649, always_xy=True)
    ax, ay = tf.transform(ANCHOR[1], ANCHOR[2])
    col = int((ax - xll) / cell)
    row = int((yll + rmax.shape[0] * cell - ay) / cell)
    dem_arr, _ = _read_asc(RUN_DIR / "dem.asc")
    wet_thr = 0.05
    wse_series = []
    for wd_p in sorted(res.glob("res-*.wd")):
        idx = int(wd_p.stem.split("-")[1])
        wd, _ = _read_asc(wd_p)
        r0, r1 = max(0, row - 3), min(rmax.shape[0], row + 4)
        c0, c1 = max(0, col - 3), min(rmax.shape[1], col + 4)
        wd_sub = wd[r0:r1, c0:c1]
        dem_sub = dem_arr[r0:r1, c0:c1]
        wet = wd_sub > wet_thr
        wse_series.append({
            "t_h": round(idx * 6.0, 1),
            "wse_center_m": round(float(dem_arr[row, col] + wd[row, col]), 2)
            if bool(wet[row - r0, col - c0]) else None,
            "wse_wet_max_m": round(float((dem_sub[wet] + wd_sub[wet]).max()), 2)
            if bool(wet.any()) else None,
            "wet_cells": int(wet.sum()),
        })

    # S1 09-12 洪峰景对比（t=228h=09-12T12 最近景 10:42）
    wd_f, _ = _read_asc(res / "res-0038.wd") if (res / "res-0038.wd").exists() else (None, None)
    s1_res = None
    if wd_f is not None:
        sim_wet = wd_f > 0.05
        obs = np.zeros(rmax.shape, dtype="uint8")
        cover = np.zeros(rmax.shape, dtype="uint8")
        with rasterio.open(S1W_FLD) as ds:
            reproject(ds.read(1), obs, src_transform=ds.transform, src_crs=ds.crs,
                      dst_transform=tgt, dst_crs="EPSG:32649", resampling=Resampling.nearest)
            reproject((ds.read(1) != 255).astype("uint8"), cover,
                      src_transform=ds.transform, src_crs=ds.crs,
                      dst_transform=tgt, dst_crs="EPSG:32649", resampling=Resampling.nearest)
        obsb = (obs == 1) & (cover == 1)
        valid = (cover == 1)  # 足迹内才算有效（足迹外无观测，不计假警报）
        H = int((sim_wet & obsb).sum())
        FA = int((sim_wet & ~obsb & valid).sum())
        M = int((~sim_wet & obsb).sum())
        s1_res = {
            "t_model_h": 228.0, "scene": "2024-09-12T10:42",
            "csi": round(H / max(H + FA + M, 1), 3), "pod": round(H / max(H + M, 1), 3),
            "far": round(FA / max(H + FA, 1), 3),
            "area_sim_km2": round(sim_wet.sum() * cell * cell / 1e6, 2),
            "area_sim_in_footprint_km2": round((sim_wet & valid).sum() * cell * cell / 1e6, 2),
            "area_obs_km2_in_footprint": round(obsb.sum() * cell * cell / 1e6, 2),
            "valid_footprint_km2": round(valid.sum() * cell * cell / 1e6, 2),
            "footprint_frac_of_domain": round(float(valid.mean()), 3),
            "note": "S1 固定阈值-17dB，风浪/淹没植被偏保守（G17）；仅足迹内比对；面积比较须用 area_sim_in_footprint",
        }
    mass_lines = (res / "res.mass").read_text().splitlines()
    keys = mass_lines[0].split()
    rows = [{k: float(v) for k, v in zip(keys, ln.split())} for ln in mass_lines[1:] if ln.split()]
    v_in = sum(0.5 * (a["Qin"] + bb["Qin"]) * (bb["Time"] - a["Time"]) for a, bb in zip(rows, rows[1:]))
    v_out = sum(0.5 * (a["Qout"] + bb["Qout"]) * (bb["Time"] - a["Time"]) for a, bb in zip(rows, rows[1:]))
    v_rain = rows[-1]["Rain-(Inf+Evap)"]
    resid = rows[-1]["Vol"] - rows[0]["Vol"] - v_rain - (v_in - v_out)
    e_mass = abs(resid) / max(rows[0]["Vol"] + v_rain + v_in, 1.0)
    wet_peak_vals = [x["wse_wet_max_m"] for x in wse_series if x["wse_wet_max_m"] is not None]
    wse_peak = max(wet_peak_vals, default=None)
    peak_row = max((x for x in wse_series if x["wse_wet_max_m"] is not None),
                   key=lambda x: x["wse_wet_max_m"], default=None)
    def at_time(t_h: float):
        return next((x for x in wse_series if abs(x["t_h"] - t_h) < 1e-6), None)

    def gauge_rec(t_h: float, gauge_m: float):
        row = at_time(t_h)
        model_wse = row["wse_wet_max_m"] if row else None
        return {
            "t_h": t_h, "gauge_m": gauge_m, "model_wet_wse_m": model_wse,
            "diff_gauge_minus_model_m": round(gauge_m - model_wse, 2)
            if model_wse is not None else None,
        }

    gauge_0912 = gauge_rec(T_GAUGE_0912_H, GAUGE_0912_M)
    gauge_0914 = gauge_rec(T_GAUGE_0914_H, GAUGE_0914_M)
    report = {
        "run_id": RUN_DIR.name,
        "anchor": ANCHOR[0], "wse_peak_m": wse_peak,
        "wse_peak_time_h": peak_row["t_h"] if peak_row else None,
        "wse_gauge_context": {
            "0912T14": gauge_0912,
            "0914T05": gauge_0914,
            "caveat": "G7 垂直基准未确认；两个时点/三个 Q 情景的 apparent offset 不一致，"
                      "不得当作 datum_offset_m、率定值或同峰对齐依据",
            "diagnostic_only": True,
        },
        "s1_0912_check": s1_res,
        "e_mass_132": float(f"{e_mass:.3e}"),
        "v_in_integrated_m3": round(v_in, 1), "v_out_integrated_m3": round(v_out, 1),
        "v_rain_m3": round(v_rain, 1), "final_vol_m3": round(rows[-1]["Vol"], 1),
        "qout_final": rows[-1]["Qout"],
        "wse_series": wse_series,
        "note": "上游三角Q情景+ERA5均匀雨；情景级结论（ADR-0004）",
    }
    out = RUN_DIR / "verify.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_run_manifest_best_effort(
        RUN_DIR,
        mass_gate={"status": "PASS" if e_mass <= 1e-3 else "FAIL",
                   "e_mass": float(f"{e_mass:.3e}")},
        warnings=["WSE 为诊断口径；垂直基准未闭合。"],
    )
    print(json.dumps({k: v for k, v in report.items() if k != "wse_series"},
                     ensure_ascii=False, indent=2))


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("prep", "all"):
        prep()
    if cmd in ("run", "all"):
        rec = run()
        if rec["exit"] != 0 and cmd == "all":
            print("run 失败，跳过 verify（run.log 留痕）", flush=True)
            return
    if cmd in ("verify", "all"):
        verify()


if __name__ == "__main__":
    main()
