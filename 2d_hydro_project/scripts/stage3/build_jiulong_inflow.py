"""久隆钦江上游小时级来水 Q(t) 生成（计划 任务3 / §5.2 / ADR-0019）。

方案（公开数据降级链，凭证类数据 IMERG/CDS 本机不可达）：
- 降雨：Open-Meteo ERA5 逐时（UTC+8 本地时），A1 上游集水区内 0.25° 采样点
  （以 catchment_a1 掩膜判定归属），算术平均为面雨量。
- 产流：Green-Ampt（华南红壤先验，E3）+ SCS 无因次单位线汇流（保留洪峰形态）；
  暖启动：35 d 连续序列进 GA，把土壤含水状态带入事件窗。
- 对照：日尺度 GR4J（复用 build_inflow_hydrology 实现）仅作水量先验交叉核对，
  禁止日均分 24 h 当作最终驱动（计划 §2.4）。
- 防重复计量红线：A1 产流只进 QVAR（本脚本）；A2 净雨只进 .rain（另一脚本）。

输出：
- data/interim/jiulong/hydrology/qinjiang_event_5d.bdy / qinjiang_recession_13d.bdy
  （QVAR：文件值 = Q_m3s / cellsize，cellsize 取 200 m 生产基线）
- data/catalog/jiulong_inflow_audit.json
证据等级 E3（文献参数 + 公开 DEM 面积，未率定）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from argparse import ArgumentParser
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import (  # noqa: E402
    AuditLog,
    add_common_args,
    read_ascii_grid,
    sha256_file,
    utc_now,
    write_json,
)
from scripts.common.event_hydro import GA_PRIOR, run_event_hourly  # noqa: E402
from scripts.stage3.build_inflow_hydrology import (  # noqa: E402
    mm_per_day_to_m3s,
    run_gr4j,
)

T0_LOCAL = "2024-09-05"
EVENT_DAYS = 5
RECESSION_DAYS = 13
WARMUP_DAYS = 35
FETCH_START = "2024-08-01"
FETCH_END = "2024-09-18"
SAMPLE_STEP_DEG = 0.25
TP_H = 5.0
CELLSIZE_M = 200.0
BDY_DIR = PROJ / "data/interim/jiulong/hydrology"
CATALOG = PROJ / "data/catalog/jiulong_inflow_audit.json"
CATCHMENTS = PROJ / "data/catalog/jiulong_catchments.json"
A1_MASK = PROJ / "data/interim/jiulong/catchment/catchment_a1.tif"
BASELINE_RUN = PROJ / "outputs/jiulong/2024-09-yagi/scen_free_200m_sv6h"
LUWU_AREA_KM2_PRIOR = 1400.0
GR4J_PARAMS = {"x1": 350.0, "x2": 0.0, "x3": 90.0, "x4": 1.7}

EVIDENCE_LEVEL = "E3（Open-Meteo ERA5 逐时 + GA/SCS-UH 文献参数 + GLO-30 面积，未率定）"


def a1_sample_points(log: AuditLog) -> list[dict]:
    """A1 掩膜内 0.25° 采样点（lon/lat）。"""
    with rasterio.open(A1_MASK) as src:
        mask = src.read(1) == 1
        rows, cols = np.nonzero(mask)
        tr = Transformer.from_crs("EPSG:32649", "EPSG:4326", always_xy=True)
        xs = src.transform.c + (cols + 0.5) * src.transform.a
        ys = src.transform.f + (rows + 0.5) * src.transform.e
        lons, lats = tr.transform(xs, ys)
    keep: list[dict] = []
    seen: set[tuple[float, float]] = set()
    for lon, lat in zip(lons, lats):
        key = (round(lon / SAMPLE_STEP_DEG), round(lat / SAMPLE_STEP_DEG))
        if key in seen:
            continue
        seen.add(key)
        keep.append({"lon": round(key[0] * SAMPLE_STEP_DEG, 4), "lat": round(key[1] * SAMPLE_STEP_DEG, 4)})
    log.step("a1_samples", n=len(keep), step_deg=SAMPLE_STEP_DEG)
    return keep


def fetch_hourly(points: list[dict], log: AuditLog, *, no_fetch: bool = False) -> dict:
    """Open-Meteo ERA5 逐时降雨（UTC+8），带磁盘缓存。"""
    cache = PROJ / f"data/interim/inflow/era5_hourly_jiulong_{FETCH_START}_{FETCH_END}.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists():
        payload = json.loads(cache.read_text(encoding="utf-8"))
        if payload.get("window") == [FETCH_START, FETCH_END] and payload.get("points") == points:
            log.step("cache_hit", path=str(cache))
            return payload
    if no_fetch:
        raise RuntimeError(f"缓存缺失且 --no-fetch：{cache}")
    samples: dict[str, dict] = {}
    for pt in points:
        url = (
            "https://archive-api.open-meteo.com/v1/archive"
            f"?latitude={pt['lat']}&longitude={pt['lon']}"
            f"&start_date={FETCH_START}&end_date={FETCH_END}"
            "&hourly=precipitation&timezone=Asia%2FShanghai"
        )
        # 本机 Python 侧 open-meteo 证书链校验失败（2026-11-22 实测），
        # curl 走系统证书正常 → 统一经 curl 拉取。
        proc = subprocess.run(
            ["curl", "-s", "-m", "90", url], capture_output=True, text=True, check=True
        )
        data = json.loads(proc.stdout)
        hourly = data["hourly"]
        key = f"{pt['lon']}_{pt['lat']}"
        samples[key] = {
            "basin": "qinjiang_a1",
            "time": hourly["time"],
            "precipitation": [float(v or 0.0) for v in hourly["precipitation"]],
        }
    payload = {
        "window": [FETCH_START, FETCH_END],
        "points": points,
        "source": "open-meteo archive-api ERA5 hourly precipitation, timezone Asia/Shanghai",
        "samples": samples,
    }
    cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    log.step("fetch", points=len(points), cache=str(cache))
    return payload


def basin_mean_hourly(payload: dict) -> tuple[np.ndarray, list[str]]:
    """面雨量 = 采样点算术平均（mm/h）；返回 (mm 序列, 时间轴)。"""
    times = None
    acc = None
    n = 0
    for key, s in payload["samples"].items():
        arr = np.asarray(s["precipitation"], dtype=float)
        if times is None:
            times = s["time"]
            acc = np.zeros(len(arr))
        if len(arr) != len(acc):
            continue
        acc += arr
        n += 1
    if n == 0:
        raise RuntimeError("无有效采样点序列")
    return acc / n, times


def write_bdy(path: Path, q_m3s: np.ndarray, cellsize_m: float, comment: str) -> Path:
    """QVAR .bdy：文件值 = Q_m3s / cellsize；时间秒，严格递增。"""
    lines = [f"# {comment}; file_value = Q_m3s / cellsize({cellsize_m:.0f}m)"]
    lines.append("qinjiang")
    lines.append(f"{len(q_m3s)} seconds")
    for i, q in enumerate(q_m3s):
        lines.append(f"{q / cellsize_m:.4f}\t{i * 3600}")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return path


def gr4j_daily_crosscheck(p_hourly: np.ndarray, times: list[str], area_km2: float) -> dict:
    """GR4J 日尺度水量对照（事件窗总量与峰值日期）。"""
    days: dict[str, float] = {}
    for t, mm in zip(times, p_hourly):
        day = t[:10]
        days[day] = days.get(day, 0.0) + float(mm)
    dates = sorted(days)
    p_daily = np.array([days[d] for d in dates])
    e_daily = np.full(len(dates), 2.0)  # 文献先验 ET0（E3）
    res = run_gr4j(p_daily, e_daily, **GR4J_PARAMS)
    q_daily = res["q_mm"]
    q_m3s = np.array([mm_per_day_to_m3s(q, area_km2) for q in q_daily])
    peak_i = int(np.argmax(q_m3s))
    # 事件窗 = t0_local 起 EVENT_DAYS 天（与事件模型同窗）
    ev_mask = [i for i, d in enumerate(dates) if d >= T0_LOCAL][:EVENT_DAYS]
    event_volume = float(np.sum(q_m3s[ev_mask])) * 86400.0 if ev_mask else 0.0
    return {
        "dates": dates,
        "q_m3s": [round(float(v), 3) for v in q_m3s],
        "peak_m3s": float(q_m3s[peak_i]),
        "peak_date": dates[peak_i],
        "event_volume_m3": event_volume,
    }


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__)
    add_common_args(parser)
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument("--tp-h", type=float, default=TP_H)
    args = parser.parse_args(argv)
    log = AuditLog("build_jiulong_inflow", run_tag=args.run_tag, dry_run=args.dry_run)
    log.add_input("catchments", CATCHMENTS)
    log.add_input("a1_mask", A1_MASK)
    BDY_DIR.mkdir(parents=True, exist_ok=True)
    (PROJ / "logs" / "stage3").mkdir(parents=True, exist_ok=True)
    (PROJ / "data/interim/inflow").mkdir(parents=True, exist_ok=True)

    catch = json.loads(CATCHMENTS.read_text(encoding="utf-8"))
    a1_area_km2 = float(catch["a1"]["area_km2"])
    log.param(a1_area_km2=a1_area_km2, luwu_prior_km2=LUWU_AREA_KM2_PRIOR)

    points = a1_sample_points(log)
    payload = fetch_hourly(points, log, no_fetch=args.no_fetch)
    p_hourly, times = basin_mean_hourly(payload)

    # 事件窗（UTC+8 本地时）：t0 = 2024-09-05T00，事件 5 d，退水 13 d
    def window_slice(days: float) -> slice:
        n_hours = int(days * 24)
        idx = [i for i, t in enumerate(times) if t.startswith(T0_LOCAL) or t > T0_LOCAL]
        start = idx[0]
        return slice(start, min(start + n_hours, len(times)))

    ev_slice = window_slice(EVENT_DAYS)
    rec_slice = window_slice(RECESSION_DAYS)
    log.param(event_slice=[times[ev_slice.start], times[ev_slice.stop - 1]],
              recession_slice=[times[rec_slice.start], times[rec_slice.stop - 1]])

    # 事件模型（GA 状态沿全序列演化，汇流后取事件窗）
    event_full = run_event_hourly(p_hourly, a1_area_km2, tp_h=args.tp_h)
    q_full = event_full["q_m3s"]
    excess_full = event_full["excess_mm"]
    # 汇流卷积起点与降雨序列起点一致；事件窗起点的暖期出流作为初始来水保留
    q_event = q_full[ev_slice.start : ev_slice.stop + len(q_full) - len(p_hourly)]
    q_event = np.asarray(q_event)[: int(EVENT_DAYS * 24)]
    q_recession = np.asarray(q_full[rec_slice.start : rec_slice.start + int(RECESSION_DAYS * 24) + 24])[: int(RECESSION_DAYS * 24)]
    log.step(
        "event_model",
        peak_m3s_event_window=float(np.max(q_event)),
        peak_t_h_event_window=int(np.argmax(q_event)),
        runoff_coeff=event_full["runoff_coeff"],
        rain_total_mm_window=float(p_hourly[ev_slice].sum()),
        volume_rel_dev=event_full["volume_rel_dev"],
    )

    gr4j = gr4j_daily_crosscheck(p_hourly, times, a1_area_km2)
    gr4j_event_volume = gr4j["event_volume_m3"]
    ga_event_volume = float(np.sum(q_event) * 3600.0)
    # 体积先验缩放（方案 B/C 合成口径，ADR-0019）：洪峰形态取 GA+SCS-UH，
    # 事件体积取日尺度 GR4J 先验（禁止日均分 24 h 当驱动）。缩放系数限幅
    # [0.4, 2.5]，越界告警并保持原值。
    scale_factor = gr4j_event_volume / ga_event_volume if ga_event_volume > 0 else 1.0
    scale_applied = 0.4 <= scale_factor <= 2.5
    if not scale_applied:
        log.warn(
            f"GR4J 体积缩放系数 {scale_factor:.2f} 超出 [0.4, 2.5]，保持 GA 原体积"
        )
        scale_factor = 1.0
    q_event = q_event * scale_factor
    q_recession = q_recession * scale_factor
    ratio = ga_event_volume / gr4j_event_volume if gr4j_event_volume > 0 else 0.0
    log.step(
        "gr4j_crosscheck",
        peak_m3s=gr4j["peak_m3s"],
        peak_date=gr4j["peak_date"],
        ga_volume_m3=ga_event_volume,
        gr4j_volume_m3=gr4j_event_volume,
        volume_scale_factor=scale_factor,
        scale_applied=scale_applied,
    )

    outputs: dict[str, str] = {}
    if not args.dry_run:
        bdy_event = write_bdy(
            BDY_DIR / "qinjiang_event_5d.bdy",
            q_event,
            CELLSIZE_M,
            "Qinjiang hourly inflow (GA+SCS-UH event model, ADR-0019, E3 scenario)",
        )
        bdy_rec = write_bdy(
            BDY_DIR / "qinjiang_recession_13d.bdy",
            q_recession,
            CELLSIZE_M,
            "Qinjiang hourly inflow recession window (13 d)",
        )
        daily_json = BDY_DIR / "qinjiang_hourly_q.json"
        daily_json.write_text(
            json.dumps(
                {
                    "t0_local": T0_LOCAL,
                    "dt_h": 1,
                    "a1_area_km2": a1_area_km2,
                    "event_q_m3s": [round(float(v), 4) for v in q_event],
                    "recession_q_m3s": [round(float(v), 4) for v in q_recession],
                    "generated_by": "build_jiulong_inflow.py",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        outputs = {
            "bdy_event": str(bdy_event.relative_to(PROJ)).replace("\\", "/"),
            "bdy_recession": str(bdy_rec.relative_to(PROJ)).replace("\\", "/"),
            "hourly_q": str(daily_json.relative_to(PROJ)).replace("\\", "/"),
        }

    audit = {
        "site": "jiulong",
        "generated_utc": utc_now(),
        "evidence_level": EVIDENCE_LEVEL,
        "window": {
            "fetch": [FETCH_START, FETCH_END],
            "warmup_days": WARMUP_DAYS,
            "t0_local": T0_LOCAL,
            "event_days": EVENT_DAYS,
            "recession_days": RECESSION_DAYS,
        },
        "a1_area_km2": a1_area_km2,
        "luwu_prior_km2": LUWU_AREA_KM2_PRIOR,
        "sample_points": points,
        "basin_rain": {
            "event_total_mm": float(p_hourly[ev_slice].sum()),
            "max_1h_mm": float(p_hourly[ev_slice].max()),
            "max_24h_mm": float(max(p_hourly[i : i + 24].sum() for i in range(ev_slice.start, ev_slice.stop - 24))),
        },
        "event_model": {
            "kind": "green_ampt + scs_uh shape, volume scaled to GR4J prior (ADR-0019)",
            "ga_params": GA_PRIOR,
            "tp_h": args.tp_h,
            "volume_scale_factor": scale_factor,
            "scale_applied": scale_applied,
            "peak_m3s_event_window": float(np.max(q_event)),
            "peak_t_h_event_window": int(np.argmax(q_event)),
            "volume_m3_event_window": float(np.sum(q_event) * 3600.0),
            "runoff_coeff": event_full["runoff_coeff"] * scale_factor,
            "sensitivity": [
                {
                    "factor": f,
                    "peak_m3s": float(np.max(q_event)) * f,
                    "volume_m3": float(np.sum(q_event) * 3600.0) * f,
                }
                for f in (0.7, 1.0, 1.4)
            ],
        },
        "gr4j_crosscheck": {
            "params": GR4J_PARAMS,
            "peak_m3s": gr4j["peak_m3s"],
            "peak_date": gr4j["peak_date"],
            "event_volume_m3": gr4j_event_volume,
            "volume_ratio_event_vs_gr4j": ratio,
            "role": "水量先验对照；禁止日均分 24 h 作为洪峰驱动",
        },
        "no_double_counting": {"a1_to_qvar_only": True},
        "outputs": outputs,
        "source_sha256": {"baseline_dem": sha256_file(BASELINE_RUN / "dem.asc")},
    }
    if not args.dry_run:
        write_json(CATALOG, audit)
    tag = f".{args.run_tag}" if args.run_tag else ""
    log.write(PROJ / "logs" / "stage3" / f"build_jiulong_inflow{tag}.json")
    print(
        f"peak={event_full['peak_m3s']:.0f} m3/s @+{event_full['peak_t_h']:.0f}h "
        f"event_vol={np.sum(q_event) * 3600 / 1e6:.0f} Mm3 "
        f"gr4j_ratio={ratio:.2f} rain_event={p_hourly[ev_slice].sum():.0f}mm"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
