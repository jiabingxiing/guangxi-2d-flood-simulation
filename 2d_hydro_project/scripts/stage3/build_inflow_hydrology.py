"""GR4J 型集总产汇流与 inflow bdy 生成（计划 A1/T06；ADR-0016 预注册）。

成功标准 S3 的上游侧。口径（ADR-0016，先预注册后计算）：

- **GR4J 日模型**（Perrin et al. 2003, J. Hydrol. 279；公式逐条对照 hydromad
  `gr4j.R` 参考实现）：生产库 tanh 族、渗漏 (4/9·S/X1)⁴、SH1/SH2 单位线
  （2.5 指数）、90/10 分流、汇流库 + 交换项 F=X2(R/X3)^3.5；
- **参数取文献通用区间中值**（X1=350、X2=0、X3=90、X4=1.7），不做自动率定；
  X1/X3/X4 单变量三档敏感性（OAT）；
- **IHC**：主口径 = 35 天真实雨/ET 暖启动结束状态；对照口径 = GA 预处理器
  θ_mean/θ_s 映射（S_init/X1），差异登记（吴欢 S2S 教训降级）；
- **输入**：Open-Meteo archive（ERA5）日降水 + ET0，流域采样点算术平均
  （采样点集预注册于 SITE_CFG，随审计登记）；
- **bdy 格式**（源码实证 `input.cpp::LoadTimeSeries`）：名称行 + `<点数> <单位>`
  + `<值> <时间>`（时间严格递增）；QVAR 值 = Q_m3s/cellsize（`sgc.cpp`
  Q_multiplier=dx）；HVAR 值 = 水面高程 m（`boundary.cpp`）；
- **run 组装**：只写新建 run 目录（guard_no_overwrite 前置），dem/bci/manning/
  rain 字节级复制锚点 run，仅替换边界件；run_manifest 记 provenance。

产出：新 run 目录 + `data/catalog/inflow_audit.json`（E3 证据等级 + 未验证字段）。
不调用 lisflood.exe（执行属 T10，另行启动）。

用法（先设 `$env:PYTHONUTF8="1"`）::

    uv --directory 2d_hydro_project run python scripts/stage3/build_inflow_hydrology.py --site guigang --dry-run
    uv --directory 2d_hydro_project run python scripts/stage3/build_inflow_hydrology.py --site guigang
    uv --directory 2d_hydro_project run python scripts/stage3/build_inflow_hydrology.py --site guigang --kind zdown_a
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import (  # noqa: E402
    AuditLog,
    GridError,
    guard_no_overwrite,
    read_ascii_header,
    sha256_file,
    utc_now,
    write_json,
)
from scripts.common.paths import rel_posix  # noqa: E402
from scripts.common.run_manifest import write_run_manifest_best_effort  # noqa: E402

PROJ = Path(__file__).resolve().parents[2]
CATALOG = PROJ / "data" / "catalog"
INTERIM = PROJ / "data" / "interim" / "inflow"
EVIDENCE_LEVEL = "E3（文献参数 + 公开面积推算，情景级；未率定）"


def _rel(path: Path) -> str:
    """审计记录用项目相对路径；项目外（测试 tmp）退回原样字符串。"""
    try:
        return rel_posix(path)
    except Exception:
        return str(path)

WARMUP_DAYS = 35
HOURLY_SPLIT = 24

# 郁江流域采样点（预注册；三分支 bbox，0.5° 网格）与明江流域（0.25°）
SITE_CFG = {
    "guigang": {
        "anchor_run": "outputs/guigang/2024-09-yagi/scen_yagi_q11500_100m_dt2p5_chhyd_d14_r4_infil3",
        "bdy_name": "yujiang",
        "out_run": "scen_gr4j_q_100m",
        "t0_local": "2024-09-03",
        "sim_seconds": 1209600,
        "basins": [
            {"name": "youjiang", "lon": (104.75, 107.25), "lat": (23.25, 24.25), "step": 0.5},
            {"name": "zuojiang", "lon": (106.25, 108.25), "lat": (21.75, 22.75), "step": 0.5},
            {"name": "ganliu_corridor", "lon": (107.75, 109.75), "lat": (22.75, 23.25), "step": 0.5},
        ],
        "preprocessor_dir": "outputs/preprocessor/guigang",
    },
    "ningming": {
        "anchor_run": "outputs/ningming/2026-08-zitan/scen_q6000_n100_100m",
        "bdy_name": "mingjiang",
        "out_run": "scen_gr4j_q6000_100m",
        "t0_local": "2026-08-21",
        "sim_seconds": 388800,
        "basins": [
            {"name": "mingjiang", "lon": (107.0, 108.5), "lat": (21.75, 22.25), "step": 0.25},
        ],
        "preprocessor_dir": None,
    },
}

PARAMS_BASE = {"x1": 350.0, "x2": 0.0, "x3": 90.0, "x4": 1.7}
SENSITIVITY = {"x1": [175.0, 350.0, 700.0], "x3": [45.0, 90.0, 180.0], "x4": [1.0, 1.7, 3.0]}

ZDOWN_RULE = {
    "a": {"offset_m": 4.0, "label": "强顶托档"},
    "b": {"offset_m": 1.0, "label": "弱顶托档"},
}


# ---------------------------------------------------------------- GR4J 核心
def unit_hydrographs(x4: float) -> tuple[np.ndarray, np.ndarray]:
    """GR4J UH1/UH2 离散序组（SH 曲线差分；和恒为 1）。x4 必须 ≥0.5。"""
    if x4 < 0.5:
        raise ValueError(f"X4={x4} < 0.5，超出 GR4J 有效域")
    n1 = int(math.ceil(x4))
    n2 = int(math.ceil(2 * x4))

    def sh1(t: float) -> float:
        if t <= 0:
            return 0.0
        return min((t / x4) ** 2.5, 1.0)

    def sh2(t: float) -> float:
        if t <= 0:
            return 0.0
        if t <= x4:
            return 0.5 * (t / x4) ** 2.5
        if t <= 2 * x4:
            return 1.0 - 0.5 * (2.0 - t / x4) ** 2.5
        return 1.0

    uh1 = np.array([sh1(j) - sh1(j - 1) for j in range(1, n1 + 1)], dtype=np.float64)
    uh2 = np.array([sh2(j) - sh2(j - 1) for j in range(1, n2 + 1)], dtype=np.float64)
    return uh1, uh2


def run_gr4j(
    p_mm: np.ndarray,
    e_mm: np.ndarray,
    *,
    x1: float, x2: float, x3: float, x4: float,
    s0_frac: float = 0.5, r0_frac: float = 0.0,
) -> dict[str, np.ndarray]:
    """逐日 GR4J；返回 {q_mm, s_mm, r_mm, pr_mm}（日序列，水量 mm/日）。

    UH 用前向累积实现：Pr[t] 以 uh[j] 权重计入第 t+j 日的 Q9/Q1。
    """
    p_mm = np.asarray(p_mm, dtype=np.float64)
    e_mm = np.asarray(e_mm, dtype=np.float64)
    if p_mm.shape != e_mm.shape or p_mm.ndim != 1:
        raise ValueError("P/E 必须同长一维")
    n = p_mm.size
    uh1, uh2 = unit_hydrographs(x4)
    q9 = np.zeros(n)
    q1 = np.zeros(n)
    q_mm = np.zeros(n)
    s_hist = np.zeros(n)
    r_hist = np.zeros(n)
    pr_hist = np.zeros(n)
    s = s0_frac * x1
    r = r0_frac * x3
    et_hist = np.zeros(n)  # 实际蒸发（库供水受限时可小于需水 En）
    pn_hist = np.zeros(n)
    for t in range(n):
        pn = max(p_mm[t] - e_mm[t], 0.0)
        en = max(e_mm[t] - p_mm[t], 0.0)
        pn_hist[t] = pn
        sr = s / x1
        ps = 0.0
        et = 0.0
        if pn > 0:
            th = math.tanh(pn / x1)
            ps = x1 * (1.0 - sr * sr) * th / (1.0 + sr * th)
        else:
            th = math.tanh(en / x1)
            et = s * (2.0 - sr) * th / (1.0 + (1.0 - sr) * th)
        et_hist[t] = et
        s = s - et + ps
        perc = s * (1.0 - (1.0 + (4.0 / 9.0 * s / x1) ** 4) ** (-0.25))
        s -= perc
        pr = perc + (pn - ps)
        pr_hist[t] = pr
        for j in range(uh1.size):
            idx = t + j
            if idx < n:
                q9[idx] += 0.9 * pr * uh1[j]
        for j in range(uh2.size):
            idx = t + j
            if idx < n:
                q1[idx] += 0.1 * pr * uh2[j]
        f = x2 * (r / x3) ** 3.5
        r_new = max(0.0, r + q9[t] + f)
        qr = r_new * (1.0 - (1.0 + (r_new / x3) ** 4) ** (-0.25))
        r_new -= qr
        qd = max(0.0, q1[t] + f)
        q_mm[t] = qr + qd
        r = r_new
        s_hist[t] = s
        r_hist[t] = r
    return {"q_mm": q_mm, "s_mm": s_hist, "r_mm": r_hist, "pr_mm": pr_hist,
            "et_mm": et_hist, "pn_mm": pn_hist}


def gr4j_balance_residual(
    p_mm: np.ndarray, e_mm: np.ndarray, states: dict[str, np.ndarray],
    s0_mm: float, r0_mm: float, *, x4: float = PARAMS_BASE["x4"],
) -> float:
    """X2=0 时水量守恒残差：ΣQ + ΔS + ΔR + UH在途 − Σ(Pn−Es)，应≈0。

    净输入用「有效雨 − 实际蒸发」（ET 受库供水限制时 < 需水 E）；
    序列尾部补零雨日使 UH 排空后为严格守恒。
    """
    uh1, uh2 = unit_hydrographs(float(x4))
    n = p_mm.size
    # UH 在途量：Pr[t] 中尚未流出窗口的部分
    in_flight = 0.0
    for t in range(n):
        if states["pr_mm"][t] <= 0:
            continue
        w9 = 0.9 * sum(uh1[j] for j in range(uh1.size) if t + j >= n)
        w1 = 0.1 * sum(uh2[j] for j in range(uh2.size) if t + j >= n)
        in_flight += states["pr_mm"][t] * (w9 + w1)
    q_sum = float(states["q_mm"].sum())
    ds = float(states["s_mm"][-1] - s0_mm)
    dr = float(states["r_mm"][-1] - r0_mm)
    net_in = float(np.sum(states["pn_mm"] - states["et_mm"]))
    return q_sum + ds + dr + in_flight - net_in


def daily_to_hourly(q_daily: np.ndarray, *, sim_seconds: float | None = None) -> np.ndarray:
    """逐时等分 + 日水量守恒（余量入当日最后一小时）；可截断到 sim_seconds。"""
    q_hourly = np.repeat(np.asarray(q_daily, dtype=np.float64) / HOURLY_SPLIT, HOURLY_SPLIT)
    for d in range(len(q_daily)):
        seg = q_hourly[d * HOURLY_SPLIT:(d + 1) * HOURLY_SPLIT]
        target = float(q_daily[d])
        seg[-1] += target - float(seg.sum())  # 浮点余量
        q_hourly[d * HOURLY_SPLIT:(d + 1) * HOURLY_SPLIT] = seg
    if sim_seconds is not None:
        keep = int(math.ceil(sim_seconds / 3600.0))
        q_hourly = q_hourly[:keep]
    return q_hourly


def mm_per_day_to_m3s(q_mm_day: np.ndarray, area_km2: float) -> np.ndarray:
    """mm/日 → m³/s（×面积×1000/86400）。"""
    return np.asarray(q_mm_day) * area_km2 * 1000.0 / 86400.0


# ---------------------------------------------------------------- 数据获取
def basin_points(basins: list[dict]) -> list[dict]:
    """bbox 网格采样点（含去重）。"""
    seen: set[tuple[float, float]] = set()
    points: list[dict] = []
    for basin in basins:
        lo, hi = basin["lon"]
        llo, lhi = basin["lat"]
        n_lon = int(round((hi - lo) / basin["step"]))
        n_lat = int(round((lhi - llo) / basin["step"]))
        for i in range(n_lon + 1):
            for j in range(n_lat + 1):
                lon = round(lo + i * basin["step"], 4)
                lat = round(llo + j * basin["step"], 4)
                if (lon, lat) in seen:
                    continue
                seen.add((lon, lat))
                points.append({"lon": lon, "lat": lat, "basin": basin["name"]})
    return points


def fetch_open_meteo_daily(
    points: list[dict], start: str, end: str, cache_path: Path, *,
    use_cache: bool = True, pause_s: float = 0.3,
) -> dict[str, Any]:
    """Open-Meteo archive 日降水 + ET0（Asia/Shanghai 日界）；带磁盘缓存。

    缓存键 = 窗口 + 采样点集；键不符即重取。网络失败 fail-closed。
    """
    if cache_path.is_file() and use_cache:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("window") == [start, end] and cached.get("points") == points:
            return cached
    import requests

    out: dict[str, Any] = {"window": [start, end], "points": points, "samples": {}}
    for k, pt in enumerate(points):
        url = (
            "https://archive-api.open-meteo.com/v1/archive"
            f"?latitude={pt['lat']}&longitude={pt['lon']}"
            f"&start_date={start}&end_date={end}"
            "&daily=precipitation_sum,et0_fao_evapotranspiration&timezone=Asia%2FShanghai"
        )
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 —— fail-closed，不静默半成品
            raise RuntimeError(
                f"Open-Meteo 日值获取失败（点 {pt['lon']},{pt['lat']}）：{exc}；"
                "检查网络后重跑，或删除缓存文件强制重取"
            ) from exc
        daily = data.get("daily", {})
        out["samples"][f"{pt['lon']}_{pt['lat']}"] = {
            "basin": pt["basin"],
            "time": daily.get("time", []),
            "precipitation_sum": daily.get("precipitation_sum", []),
            "et0_fao_evapotranspiration": daily.get("et0_fao_evapotranspiration", []),
        }
        print(f"  fetch {k + 1}/{len(points)} ({pt['lon']},{pt['lat']})", flush=True)
        time.sleep(pause_s)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(cache_path, out)
    return out


def basin_mean_daily(samples: dict[str, Any]) -> tuple[list[str], np.ndarray, np.ndarray]:
    """采样点算术平均 → (dates, P_mm, ET0_mm)；缺测按 0 计并计数。"""
    dates: list[str] | None = None
    p_acc: np.ndarray | None = None
    e_acc: np.ndarray | None = None
    n_used = 0
    n_missing = 0
    for rec in samples["samples"].values():
        t = rec["time"]
        if dates is None:
            dates = list(t)
            p_acc = np.zeros(len(t))
            e_acc = np.zeros(len(t))
        if t != dates:
            raise ValueError("采样点日期轴不一致；检查缓存与时区")
        p = np.asarray(rec["precipitation_sum"], dtype=np.float64)
        e = np.asarray(rec["et0_fao_evapotranspiration"], dtype=np.float64)
        if p.size != len(dates) or e.size != len(dates):
            n_missing += 1
            continue
        p_acc += p
        e_acc += e
        n_used += 1
    if p_acc is None or n_used == 0:
        raise ValueError("无有效采样点序列")
    if n_missing:
        print(f"[warn] {n_missing} 个采样点序列长度不符，已剔除", flush=True)
    return dates, p_acc / n_used, e_acc / n_used


# ---------------------------------------------------------------- IHC 对照
def ga_ihc_ratio(preprocessor_dir: Path) -> dict[str, Any]:
    """GA 预处理器 θ_i/θ_s 网格均值 → S_init/X1 映射对照（吴欢 IHC 降级）。"""
    import rasterio

    ti = preprocessor_dir / "soil_theta_i.tif"
    ts = preprocessor_dir / "soil_theta_s.tif"
    if not (ti.is_file() and ts.is_file()):
        return {"available": False,
                "note": "预处理器输出不存在；仅暖启动口径（如实登记）"}
    with rasterio.open(ti) as ds:
        theta_i = ds.read(1).astype("float64")
        nodata_i = ds.nodata
    with rasterio.open(ts) as ds:
        theta_s = ds.read(1).astype("float64")
        nodata_s = ds.nodata
    valid = np.ones(theta_i.shape, dtype=bool)
    if nodata_i is not None:
        valid &= theta_i != nodata_i
    if nodata_s is not None:
        valid &= theta_s != nodata_s
    valid &= (theta_s > 0) & np.isfinite(theta_i) & np.isfinite(theta_s)
    if not valid.any():
        return {"available": False, "note": "θ 栅格无有效像元"}
    ratio = float(np.mean(theta_i[valid] / theta_s[valid]))
    return {
        "available": True,
        "theta_i_mean": round(float(np.mean(theta_i[valid])), 4),
        "theta_s_mean": round(float(np.mean(theta_s[valid])), 4),
        "s_init_over_x1": round(ratio, 4),
        "note": "E3 映射：AOI 网格均值代理流域前期湿度；不作率定",
    }


# ---------------------------------------------------------------- 输出件
def write_bdy(
    path: Path, name: str, values: np.ndarray, times_s: np.ndarray, *,
    value_divisor: float = 1.0, header_comment: str = "",
) -> Path:
    """写 LISFLOOD-FP .bdy（名称行 + `<点数> <单位>` + `<值> <时间>`）。

    QVAR 系列传 value_divisor=cellsize（文件值=Q_m3s/cellsize）；
    HVAR 系列传 1.0（值=水面高程 m）。时间必须严格递增。
    """
    values = np.asarray(values, dtype=np.float64)
    times_s = np.asarray(times_s, dtype=np.float64)
    if values.shape != times_s.shape or values.size < 2:
        raise ValueError("bdy 序列至少 2 点且值/时间同长")
    if not np.all(np.diff(times_s) > 0):
        raise GridError("bdy 时间必须严格递增（LoadTimeSeries 硬约束）")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if header_comment:
        lines.append(f"# {header_comment}")
    lines.append(name)
    lines.append(f"{values.size} seconds")
    for v, t in zip(values, times_s):
        lines.append(f"{v / value_divisor:.4f}\t{t:.0f}")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return path


def hvar_series(bed_ref_m: float, offset_m: float, sim_seconds: float) -> tuple[np.ndarray, np.ndarray]:
    """HVAR 常数水位系列（首尾两点，值=水面高程 m）。"""
    return (np.array([bed_ref_m + offset_m, bed_ref_m + offset_m]),
            np.array([0.0, sim_seconds]))


def edge_bed_reference(dem_path: Path, edge: str = "E", pct: float = 10.0) -> float:
    """边界列 DEM 第 pct 百分位（非 nodata），作为 HVAR 水位锚。"""
    header = read_ascii_header(dem_path)
    nrows, ncols = int(header["nrows"]), int(header["ncols"])
    nodata = float(header["nodata_value"])
    data = np.loadtxt(dem_path, skiprows=6, dtype=np.float64, ndmin=2)
    if edge == "E":
        col_vals = data[:, ncols - 1]
    elif edge == "W":
        col_vals = data[:, 0]
    else:
        raise ValueError(f"暂只支持 E/W 边界列；实得 {edge}")
    vals = col_vals[np.isfinite(col_vals) & (col_vals != nodata)]
    if vals.size == 0:
        raise GridError(f"{dem_path} {edge} 边界列无有效高程")
    return float(np.percentile(vals, pct))


def prepare_run(
    anchor_dir: Path, run_dir: Path, *,
    bdy_files: dict[str, Path], run_par_edits: dict[str, str],
    bci_edits: dict[str, str] | None, meta_extra: dict[str, Any], log: AuditLog,
) -> Path:
    """新 run 目录组装：复制锚点静态件 + 替换边界件 + run.par/bci 编辑 + manifest。"""
    guard_no_overwrite(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    copied = {}
    for fname in ("dem.asc", "bci.bci", "rain.rain"):  # 必需
        src = anchor_dir / fname
        if not src.is_file():
            raise FileNotFoundError(f"锚点 run 缺 {fname}：{anchor_dir}")
        shutil.copyfile(src, run_dir / fname)
        copied[fname] = sha256_file(src)
        log.add_input(f"anchor_{fname}", src)
    for fname in ("manning.map",):  # 可选（宁明用 fpfric 标量糙率）
        src = anchor_dir / fname
        if src.is_file():
            shutil.copyfile(src, run_dir / fname)
            copied[fname] = sha256_file(src)
            log.add_input(f"anchor_{fname}", src)
    for bname, bpath in bdy_files.items():
        dst = run_dir / bpath.name
        if bpath.resolve() != dst.resolve():
            shutil.copyfile(bpath, dst)
    # run.par：逐行替换指定键
    par_lines = (anchor_dir / "run.par").read_text(encoding="utf-8").splitlines()
    new_par = []
    for line in par_lines:
        key = line.split()[0].strip().lower() if line.strip() else ""
        if key in run_par_edits:
            new_par.append(f"{key}\t{run_par_edits[key]}")
        else:
            new_par.append(line)
    (run_dir / "run.par").write_text("\n".join(new_par) + "\n", encoding="utf-8")
    # bci：逐行替换边界类型
    if bci_edits:
        bci_lines = (run_dir / "bci.bci").read_text(encoding="utf-8").splitlines()
        new_bci = []
        for line in bci_lines:
            parts = line.split()
            if len(parts) >= 4 and parts[0] in bci_edits and parts[3] in ("FREE", "HFIX", "HVAR", "QVAR", "QFIX"):
                parts[3] = bci_edits[parts[0]]
                new_bci.append("\t".join(parts))
            else:
                new_bci.append(line)
        (run_dir / "bci.bci").write_text("\n".join(new_bci) + "\n", encoding="utf-8")
    meta = {
        "run_id": run_dir.name,
        "generated_utc": utc_now(),
        "assembly": "build_inflow_hydrology.py（ADR-0016；复制锚点静态件，仅替换边界）",
        "anchor_run": _rel(anchor_dir),
        "anchor_files_sha256": copied,
        "bdy_files": {k: _rel(v) for k, v in bdy_files.items()},
        "evidence_level": EVIDENCE_LEVEL,
        **meta_extra,
    }
    (run_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    write_run_manifest_best_effort(run_dir, config_snapshot=meta,
                                   model_version="LISFLOOD-FP 8.1.0")
    return run_dir


# ---------------------------------------------------------------- 主流程
def build_site_inflow(
    site: str, *, use_cache: bool = True, dry_run: bool = False,
    sensitivity: bool = True, log: AuditLog,
) -> dict[str, Any]:
    """单站 GR4J 边界生成 + run 组装（gr4j_q 角点）。"""
    cfg = SITE_CFG[site]
    priors_path = CATALOG / "guigang_inflow_priors.json"
    priors = json.loads(priors_path.read_text(encoding="utf-8"))
    log.add_input("priors", priors_path)
    area_km2 = float(priors[site]["area_km2_prior"])
    anchor = PROJ / cfg["anchor_run"]
    log.add_input("anchor_run_dir", anchor, hash_file=False)
    if not (anchor / "results" / "res.mass").is_file():
        raise FileNotFoundError(f"锚点 run 缺 res.mass：{anchor}")

    from datetime import date, timedelta

    t0 = date.fromisoformat(cfg["t0_local"])
    sim_days = int(math.ceil(cfg["sim_seconds"] / 86400.0))
    event_end = t0 + timedelta(days=sim_days)
    warm_start = t0 - timedelta(days=WARMUP_DAYS)
    fetch_start = warm_start.isoformat()
    fetch_end = (event_end - timedelta(days=1)).isoformat()

    points = basin_points(cfg["basins"])
    cache_path = INTERIM / f"era5_daily_{site}_{fetch_start}_{fetch_end}.json"
    samples = fetch_open_meteo_daily(points, fetch_start, fetch_end, cache_path,
                                     use_cache=use_cache)
    log.add_input("era5_daily_cache", cache_path)
    dates, p_mm, e_mm = basin_mean_daily(samples)

    # GR4J：暖启动 → 事件窗
    warm_mask = [d < cfg["t0_local"] for d in dates]
    ev_mask = [not m for m in warm_mask]
    p_warm, e_warm = p_mm[np.array(warm_mask)], e_mm[np.array(warm_mask)]
    p_ev, e_ev = p_mm[np.array(ev_mask)], e_mm[np.array(ev_mask)]
    if p_ev.size < sim_days:
        raise ValueError(f"事件窗日数不足：{p_ev.size} < {sim_days}")
    warm = run_gr4j(p_warm, e_warm, **PARAMS_BASE)
    s0_warm = float(warm["s_mm"][-1])
    r0_warm = float(warm["r_mm"][-1])
    ev = run_gr4j(p_ev, e_ev, s0_frac=s0_warm / PARAMS_BASE["x1"],
                  r0_frac=r0_warm / PARAMS_BASE["x3"], **PARAMS_BASE)
    bal = gr4j_balance_residual(p_ev, e_ev, ev, s0_warm, r0_warm)

    ga = (ga_ihc_ratio(PROJ / cfg["preprocessor_dir"])
          if cfg["preprocessor_dir"] else
          {"available": False, "note": "该站无预处理器输出；仅暖启动口径"})

    q_m3s = mm_per_day_to_m3s(ev["q_mm"], area_km2)
    peak_idx = int(np.argmax(q_m3s))
    # 日→时等分（单位无关，线性等分守恒），截断到模拟窗末端并补末端点
    q_hourly_m3s = daily_to_hourly(q_m3s, sim_seconds=cfg["sim_seconds"])
    times = np.arange(q_hourly_m3s.size + 1, dtype=np.float64) * 3600.0
    q_series = np.append(q_hourly_m3s, q_hourly_m3s[-1])

    # 敏感性（OAT 三档；只入审计，不落 bdy）
    sens = []
    if sensitivity:
        for key, vals in SENSITIVITY.items():
            for v in vals:
                prm = dict(PARAMS_BASE)
                prm[key] = v
                if key in ("x1", "x3"):
                    s0f = s0_warm / prm["x1"]
                    r0f = r0_warm / prm["x3"]
                else:
                    s0f = s0_warm / PARAMS_BASE["x1"]
                    r0f = r0_warm / PARAMS_BASE["x3"]
                alt = run_gr4j(p_ev, e_ev, s0_frac=s0f, r0_frac=r0f, **prm)
                q_alt = mm_per_day_to_m3s(alt["q_mm"], area_km2)
                sens.append({"param": key, "value": v,
                             "peak_q_m3s": round(float(q_alt.max()), 1),
                             "volume_m3": round(float(q_alt.sum()) * 86400.0, 0)})

    rec: dict[str, Any] = {
        "site": site,
        "window": {"warmup_days": WARMUP_DAYS, "warm_start": fetch_start,
                   "t0_local": cfg["t0_local"], "sim_seconds": cfg["sim_seconds"],
                   "fetch_end": fetch_end},
        "sample_points": points,
        "basin_rain": {"total_mm": round(float(p_ev.sum()), 1),
                       "max_daily_mm": round(float(p_ev.max()), 1),
                       "warmup_total_mm": round(float(p_warm.sum()), 1)},
        "gr4j_params": PARAMS_BASE,
        "ihc": {"warmup_end_s_mm": round(s0_warm, 2),
                "warmup_end_r_mm": round(r0_warm, 2),
                "ga_mapping": ga},
        "balance_residual_mm": round(bal, 6),
        "q_summary": {
            "peak_m3s": round(float(q_m3s.max()), 1),
            "peak_date": dates[len(dates) - p_ev.size + peak_idx],
            "volume_m3": round(float(q_m3s.sum()) * 86400.0, 0),
        },
        "sensitivity_oat": sens,
        "evidence_level": EVIDENCE_LEVEL,
        "area_km2_prior": area_km2,
        "area_pending_user_review": priors.get("pending_user_review", []),
    }
    if dry_run:
        return rec

    # 日序列落盘（T07 GloFAS 先验对照输入；含暖启动+事件两段日期轴）
    ev_dates = list(dates[-p_ev.size:])
    write_json(INTERIM / f"{site}_gr4j_daily_q.json", {
        "site": site,
        "dates": list(dates),
        "q_m3s": [round(float(v), 3) for v in mm_per_day_to_m3s(
            run_gr4j(p_mm, e_mm, s0_frac=0.5, **PARAMS_BASE)["q_mm"], area_km2)],
        "event_q_m3s": [round(float(v), 3) for v in q_m3s],
        "event_dates": ev_dates,
        "event_window": [cfg["t0_local"], fetch_end],
        "area_km2": area_km2,
        "generated_by": "build_inflow_hydrology.py (ADR-0016)",
    })

    dem_header = read_ascii_header(anchor / "dem.asc")
    cellsize = float(dem_header["cellsize"])
    event_dir = Path(cfg["anchor_run"]).parent
    out_dir = PROJ / "outputs" / event_dir.parent.name / event_dir.name / cfg["out_run"]
    bdy = write_bdy(
        out_dir / f"inflow_{site}.bdy", cfg["bdy_name"], q_series, times,
        value_divisor=cellsize,
        header_comment=f"GR4J hourly upstream inflow (ADR-0016, E3 scenario, "
                       f"area={area_km2:.0f} km2 prior); file_value = Q_m3s / cellsize",
    )
    log.add_input("inflow_bdy", bdy)
    run = prepare_run(
        anchor, out_dir,
        bdy_files={"inflow": bdy},
        run_par_edits={"bdyfile": bdy.name},
        bci_edits=None,
        meta_extra={"gr4j": {k: rec[k] for k in
                             ("gr4j_params", "ihc", "q_summary", "balance_residual_mm",
                              "window", "area_km2_prior")}},
        log=log,
    )
    rec["run_dir"] = _rel(run)
    rec["inflow_bdy"] = _rel(bdy)
    return rec


def build_zdown_run(
    kind: str, *, dry_run: bool = False, log: AuditLog,
) -> dict[str, Any]:
    """zdown_a/b run 组装：锚点驱动 + E 边 HVAR 常数水位（顶托情景）。"""
    if kind not in ZDOWN_RULE:
        raise ValueError(f"kind 必须是 {'/'.join(ZDOWN_RULE)}；实得 {kind}")
    cfg = SITE_CFG["guigang"]
    anchor = PROJ / cfg["anchor_run"]
    rule = ZDOWN_RULE[kind]
    bed_ref = edge_bed_reference(anchor / "dem.asc", edge="E", pct=10.0)
    za, ta = hvar_series(bed_ref, rule["offset_m"], cfg["sim_seconds"])
    rec: dict[str, Any] = {
        "site": "guigang", "kind": f"zdown_{kind}",
        "z_rule": {"bed_ref_m": round(bed_ref, 3),
                   "bed_ref_method": "E 边界列 dem.asc 第 10 百分位（ADR-0016）",
                   "offset_m": rule["offset_m"], "label": rule["label"],
                   "z_const_m": round(float(za[0]), 3)},
        "evidence_level": EVIDENCE_LEVEL,
        "quality_gate_expectation": "FAIL-EXPLAINED 预注册（HVAR 边界通量口径，G13 先例）",
    }
    if dry_run:
        return rec
    out_dir = PROJ / "outputs" / "guigang" / "2024-09-yagi" / f"scen_zdown_{kind}_100m"
    # 合并 bdy：yujiang（QVAR）+ zdown（HVAR）两个命名系列共一文件
    src_bdy = anchor / "yujiang.bdy"
    yu_text = src_bdy.read_text(encoding="utf-8")
    zdown_text = (
        f"# HVAR downstream stage (ADR-0016 {rule['label']}; value = water surface elevation m)\n"
        f"zdown_{kind}\n{za.size} seconds\n"
        + "\n".join(f"{v:.4f}\t{t:.0f}" for v, t in zip(za, ta)) + "\n"
    )
    combined = out_dir / f"yujiang_zdown_{kind}.bdy"
    out_dir.mkdir(parents=True, exist_ok=True)
    combined.write_text(yu_text + "\n" + zdown_text, encoding="utf-8")
    log.add_input("anchor_yujiang_bdy", src_bdy)
    log.add_input("zdown_bdy", combined)
    run = prepare_run(
        anchor, out_dir,
        bdy_files={"combined": combined},
        run_par_edits={"bdyfile": combined.name},
        bci_edits={"E": f"HVAR zdown_{kind}"},
        meta_extra={"zdown": rec["z_rule"],
                    "quality_gate_expectation": rec["quality_gate_expectation"]},
        log=log,
    )
    rec["run_dir"] = _rel(run)
    rec["bdy_file"] = _rel(combined)
    return rec


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", choices=list(SITE_CFG), default=None,
                        help="GR4J 边界站点（guigang/ningming）")
    parser.add_argument("--kind", default="gr4j_q",
                        choices=["gr4j_q", "zdown_a", "zdown_b"],
                        help="gr4j_q=上游来水 run；zdown_a/b=顶托情景 run")
    parser.add_argument("--no-fetch", action="store_true", help="只用缓存，不联网")
    parser.add_argument("--no-sensitivity", action="store_true")
    from scripts.common.grid_io import add_common_args

    add_common_args(parser)
    args = parser.parse_args()
    log = AuditLog("build_inflow_hydrology.py", run_tag=args.run_tag, dry_run=args.dry_run)
    log.param(site=args.site, kind=args.kind, warmup_days=WARMUP_DAYS,
              params_base=PARAMS_BASE)

    if args.kind in ("zdown_a", "zdown_b"):
        rec = build_zdown_run(args.kind.split("_")[1], dry_run=args.dry_run, log=log)
    else:
        if not args.site:
            raise SystemExit("--kind gr4j_q 需要 --site guigang|ningming")
        rec = build_site_inflow(args.site, use_cache=not args.no_fetch,
                                dry_run=args.dry_run,
                                sensitivity=not args.no_sensitivity, log=log)
    out_json = args.log_json or (CATALOG / "inflow_audit.json")
    # 累积合并：审计 JSON 追加不重写历史条目（ADR-0015 §5.4 / 计划 5.4）
    history = {}
    if out_json.is_file():
        try:
            prev = json.loads(out_json.read_text(encoding="utf-8"))
            history = prev.get("runs", {})
        except (json.JSONDecodeError, OSError):
            history = {}
    key = f"{args.kind}:{args.site or 'guigang'}"
    history[key] = rec
    log.payload["runs"] = history
    log.write(out_json)
    print(json.dumps(rec, ensure_ascii=False)[:1200])
    print("->", out_json)


if __name__ == "__main__":
    main()
