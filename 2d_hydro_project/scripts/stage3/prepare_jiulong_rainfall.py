"""久隆 A2 域内多源降雨 → Green-Ampt 离线净雨（计划 任务5 前置 / §6.2 / ADR-0019）。

口径与红线：
- A2 = 200 m 生产基线网格（域内 1106.56 km²）；A1 产流只进 QVAR，A2 净雨只进
  .rain，两账分立（build_jiulong_inflow.py 为 A1 侧）。
- IMERG V07 / CDS 凭证类数据本机不可达（2026-11-22 实测）→ 降级：ERA5 单锚点
  逐时（data/catalog/probes/jiulong_sep2024_era5_hourly_13d.json，UTC+8）。
- GA 参数与 A1 事件模型一致（event_hydro.GA_PRIOR，华南红壤先验，E3）。
- **防重复计量硬锁**：本脚本产出净雨后，对应 run.par 严禁再含求解器
  infiltration/fc 扣减（run_jiulong_dual_forcing.py 组装时断言）。

输出：data/interim/jiulong/rainfall/rain_net_ga_5d.rain（事件 5 d 窗）+
data/catalog/jiulong_rainfall_audit.json。
"""

from __future__ import annotations

import json
import sys
from argparse import ArgumentParser
from pathlib import Path

import numpy as np

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.event_hydro import GA_PRIOR  # noqa: E402
from scripts.common.grid_io import AuditLog, add_common_args, utc_now, write_json  # noqa: E402
from scripts.common.net_rain import write_lisflood_rain  # noqa: E402
from rainfall_runoff_preprocessor.infiltration.green_ampt import green_ampt_net_rain  # noqa: E402

ANCHOR_HOURLY = PROJ / "data/catalog/probes/jiulong_sep2024_era5_hourly_13d.json"
ANCHOR_LAT, ANCHOR_LON = 22.0638, 108.7161  # ERA5 锚点（jiulong.yaml）
FETCH_START, FETCH_END = "2024-08-01", "2024-09-18"  # 35 d 暖启动 + 事件
CACHE = PROJ / "data/interim/inflow/era5_hourly_jiulong_anchor_full.json"
T0_LOCAL = "2024-09-05"
EVENT_DAYS = 5
RECESSION_DAYS = 13  # R3 退水窗（M6，09-05..09-17）
OUT_DIR = PROJ / "data/interim/jiulong/rainfall"
CATALOG = PROJ / "data/catalog/jiulong_rainfall_audit.json"
EVIDENCE_LEVEL = "E3（ERA5 单锚点逐时 + GA 华南红壤先验；IMERG/CDS 凭证缺记降级）"


def fetch_anchor_hourly(log: AuditLog) -> tuple[list[str], np.ndarray]:
    """锚点逐时降雨（UTC+8），35 d 暖启动 + 事件窗，带缓存（经 curl）。"""
    import subprocess

    if CACHE.exists():
        payload = json.loads(CACHE.read_text(encoding="utf-8"))
        log.step("cache_hit", path=str(CACHE))
        return payload["time"], np.asarray(payload["precipitation"], dtype=float)
    url = (
        "https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={ANCHOR_LAT}&longitude={ANCHOR_LON}"
        f"&start_date={FETCH_START}&end_date={FETCH_END}"
        "&hourly=precipitation&timezone=Asia%2FShanghai"
    )
    proc = subprocess.run(
        ["curl", "-s", "-m", "90", url], capture_output=True, text=True, check=True
    )
    data = json.loads(proc.stdout)
    hourly = data["hourly"]
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(
        json.dumps(
            {
                "time": hourly["time"],
                "precipitation": [float(v or 0.0) for v in hourly["precipitation"]],
                "source": "open-meteo archive-api ERA5 hourly, Asia/Shanghai",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    log.step("fetch_anchor", url=url.split("?")[0], cache=str(CACHE))
    return hourly["time"], np.asarray(
        [float(v or 0.0) for v in hourly["precipitation"]], dtype=float
    )


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__)
    add_common_args(parser)
    args = parser.parse_args(argv)
    log = AuditLog("prepare_jiulong_rainfall", run_tag=args.run_tag, dry_run=args.dry_run)
    log.add_input("anchor_hourly", ANCHOR_HOURLY)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (PROJ / "logs" / "stage3").mkdir(parents=True, exist_ok=True)

    log.add_input("anchor_13d_reference", ANCHOR_HOURLY)
    times, p_all = fetch_anchor_hourly(log)

    # 事件窗：t0 起 EVENT_DAYS 天（UTC+8 本地时）
    idx0 = next(i for i, t in enumerate(times) if t >= T0_LOCAL)
    ev = slice(idx0, min(idx0 + EVENT_DAYS * 24, len(times)))
    p_event = p_all[ev]
    log.param(t0_local=T0_LOCAL, event_hours=len(p_event), event_start=times[ev.start])

    # GA 沿全序列（35 d 暖启动）演化土壤含水状态，与 A1 事件模型口径一致；
    # 事件窗净雨单独切片输出。
    ga_full = green_ampt_net_rain(
        p_all,
        GA_PRIOR["ks_mm_h"],
        GA_PRIOR["psi_mm"],
        GA_PRIOR["theta_s"],
        GA_PRIOR["theta_i"],
        dt_h=1.0,
        F_max_mm=GA_PRIOR["f_max_mm"],
        # 口径定版（ADR-0019，2026-11-22）：8 月面雨量 313 mm → 35 d 暖启动后
        # 土壤蓄满；饱和后入渗容量按 docstring 语义回落为 drainage=Ks（深渗），
        # 事件净雨 = 超渗部分。系数 ~0.32（保守侧），如实入 audit。
        drainage_mm_h=GA_PRIOR["ks_mm_h"],
    )
    net_all = np.maximum(np.asarray(ga_full.net_rain_mm).reshape(-1), 0.0)
    net = net_all[ev]
    gross_total = float(p_all[ev].sum())
    net = np.maximum(net, 0.0)
    gross_total = float(p_event.sum())
    net_total = float(net.sum())
    log.step(
        "ga_offline",
        gross_total_mm=gross_total,
        net_total_mm=net_total,
        loss_total_mm=gross_total - net_total,
        mass_balance_max_rel_err=float(ga_full.max_relative_error),
        warmup_hours=idx0,
    )
    if np.any(net < -1e-9):
        log.error("净雨出现负值（违反计划 §10.2 非负门禁）")
        return 1

    # .rain：逐时净雨率（mm/h），首零端点 + 末尾归零端点；同轮产出 5 d（R1/R3）与
    # 13 d（R3 退水窗 M6，09-05..09-17）两份
    outputs: dict[str, str] = {}
    if not args.dry_run:
        for tag, days, net_w in (("5d", EVENT_DAYS, net), ("13d", RECESSION_DAYS, net_all[ev.start : ev.start + RECESSION_DAYS * 24])):
            event_h = days * 24
            pts: list[tuple[float, float]] = [(0.0, 0)]
            for i in range(event_h - 1):
                pts.append((float(net_w[i]), (i + 1) * 3600))
            pts.append((0.0, event_h * 3600))
            out = OUT_DIR / ("rain_net_ga_5d.rain" if tag == "5d" else "rain_net_ga_13d.rain")
            write_lisflood_rain(
                pts,
                out,
                "# Jiulong A2 net rain (ERA5 anchor hourly - Green-Ampt offline, ADR-0019; "
                "PAR MUST NOT add solver infiltration; E3 degraded, IMERG unavailable)" + chr(10),
            )
            outputs["rain_net_" + tag] = str(out.relative_to(PROJ)).replace(chr(92), "/")

    audit = {
        "generated_utc": utc_now(),
        "evidence_level": EVIDENCE_LEVEL,
        "t0_local": T0_LOCAL,
        "event_days": EVENT_DAYS,
        "gross_total_mm": gross_total,
        "net_total_mm": net_total,
        "max_1h_net_mm": float(net.max()),
        "ga_params": GA_PRIOR,
        "double_counting_lock": {
            "net_rain_in_dot_rain": True,
            "solver_infiltration_forbidden": True,
            "a1_qvar_separate": True,
        },
        "outputs": outputs,
    }
    if not args.dry_run:
        write_json(CATALOG, audit)
    tag = f".{args.run_tag}" if args.run_tag else ""
    log.write(PROJ / "logs" / "stage3" / f"prepare_jiulong_rainfall{tag}.json")
    print(f"net_rain total={net_total:.1f}mm (gross {gross_total:.1f}mm) max_1h={net.max():.2f}mm")
    return 0


if __name__ == "__main__":
    sys.exit(main())
