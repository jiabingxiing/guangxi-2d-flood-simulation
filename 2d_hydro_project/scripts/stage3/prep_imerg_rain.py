"""IMERG V07 30 min → 事件窗逐时面雨序列（计划 T08 / A2-MMF 准备；ADR-0016 §2）。

口径（ADR-0016，与 ERA5 探针链同纪律）：

- **解析**：GPM_3IMERGHH.07（HDF5，变量 `precipitation`，mm/hr 瞬时率）。
  文件内若为逐日累计（口径探测：单调不减且跨日界跳变），先反累计；
  率值序列直接使用（两者统一为 mm/hr 瞬时率）。
- **时基归一**：UTC 30 min 格 → Asia/Shanghai 逐时（同窗两个半点均值）；
  对齐事件窗（t0 起），缺测记 0 并计数（不插值、不静默）。
- **面雨**：AOI bbox 内 IMERG 0.1° 格点算术平均（与 ERA5 探针点同级别口径）。
- **凭证**：GES DISC 下载需 Earthdata 账号；无凭证时本脚本只处理**本地已下载**
  文件，下载器 fail-closed 并给出配置指引。数据不可得 → S4 记「未验证」（计划 §6.4）。

产出：`data/interim/imerg/<site>_imerg_hourly.json` + `data/catalog/imerg_prep_audit.json`。
下游：净雨链复用 GA 预处理器（同一链，仅替换驱动序列）。

用法（先设 `$env:PYTHONUTF8="1"`）::

    uv --directory 2d_hydro_project run python scripts/stage3/prep_imerg_rain.py --input <IMERG*.HDF5> --site guigang
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import AuditLog, utc_now, write_json  # noqa: E402

INTERIM = PROJ / "data" / "interim" / "imerg"
CATALOG = PROJ / "data" / "catalog"

# AOI bbox（与案例 config 一致；0.1° 格点采样）
AOI_BBOX = {
    "guigang": (109.3, 22.98, 109.95, 23.32),
    "ningming": (106.96, 22.06, 107.20, 22.26),
}
EVENT_T0 = {"guigang": "2024-09-03T00:00", "ningming": "2026-08-21T00:00"}
N_HOURS = {"guigang": 336, "ningming": 108}


def is_cumulative(values: np.ndarray) -> bool:
    """口径探测：非负、单调不减、且有正增量 → 视作累计序列。"""
    v = np.asarray(values, dtype=np.float64)
    v = v[np.isfinite(v)]
    if v.size < 3:
        return False
    return bool(np.all(v >= 0) and np.all(np.diff(v) >= -1e-12) and np.max(np.diff(v)) > 0)


def deaccumulate(cum: np.ndarray) -> np.ndarray:
    """累计 → 增量（首点保持原值；负增量按 0 计并保留计数责任给调用方）。"""
    cum = np.asarray(cum, dtype=np.float64)
    out = np.diff(cum, prepend=0.0)
    out[1:][out[1:] < 0] = 0.0
    return out


def parse_imerg_precip(path: Path) -> tuple[np.ndarray, list[str], str]:
    """读 IMERG HDF5 的 precipitation 序列（30 min UTC）。

    返回 (mm_per_30min, iso_time_list, parse_note)。h5py 缺失时给出可执行指引。
    """
    try:
        import h5py  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "读取 IMERG HDF5 需要 h5py：先 `uv add h5py`（或 uv run --with h5py）；"
            "无本地 IMERG 文件时本任务按计划 §6.4 记「数据不可得」。"
        ) from exc
    import h5py

    with h5py.File(path, "r") as h5:
        if "Grid/precipitation" not in h5:
            raise KeyError(f"{path} 缺 Grid/precipitation 变量（IMERG V07 结构）")
        arr = np.asarray(h5["Grid/precipitation"]).squeeze()
        lon = np.asarray(h5["Grid/lon"]).squeeze()
        lat = np.asarray(h5["Grid/lat"]).squeeze()
    if arr.ndim != 2:
        raise ValueError(f"precipitation 维度异常：{arr.shape}（应为 lat×lon 单时步文件）")
    note = "rate (mm/hr, GPM_3IMERGHH.07)"
    return arr, lon.tolist(), lat.tolist(), note


def bbox_mean_field(field: np.ndarray, lat: np.ndarray, lon: np.ndarray,
                    bbox: tuple[float, float, float, float]) -> float:
    """0.1° 场在 AOI bbox 内的算术平均（无有效格点 → nan，调用方 fail-closed）。"""
    lo_lon, lo_lat, hi_lon, hi_lat = bbox
    mask_lat = (lat >= lo_lat - 0.05) & (lat <= hi_lat + 0.05)
    mask_lon = (lon >= lo_lon - 0.05) & (lon <= hi_lon + 0.05)
    sub = field[np.ix_(mask_lat, mask_lon)]
    if sub.size == 0:
        return float("nan")
    return float(np.nanmean(sub))


def hourly_uniform_from_halfhour(
    values_30min: np.ndarray, start_utc: str, event_t0_local: str, n_hours: int,
) -> tuple[list[str], np.ndarray, int]:
    """UTC 半点序列 → Asia/Shanghai 逐时均值；对齐事件窗；缺测计数。

    时基归一：本地小时 [k,k+1)（UTC+8）对应 UTC 半点对 (k−8, k−7)。
    `start_utc` = 半点序列首点的 UTC ISO 时刻（日界起点，登记于审计）。
    返回 (本地 ISO 时刻列表, 逐时 mm/hr, n_missing)。
    """
    values = np.asarray(values_30min, dtype=np.float64)
    start = datetime.fromisoformat(start_utc).replace(tzinfo=timezone.utc)
    t0 = datetime.fromisoformat(event_t0_local).replace(
        tzinfo=timezone(timedelta(hours=8)))
    t0_utc = t0.astimezone(timezone.utc)
    offset_halfhours = int((t0_utc - start).total_seconds() // 1800)
    if offset_halfhours < 16:
        raise ValueError(
            f"序列起点 {start_utc} 未覆盖事件起点前 8 h（时区余量不足），拒绝产出")
    if values.size < offset_halfhours + 2 * n_hours:
        raise ValueError(
            f"IMERG 序列过短：{values.size} 个半点 < 事件窗所需 "
            f"{offset_halfhours + 2 * n_hours}")
    hours: list[float] = []
    missing = 0
    for k in range(n_hours):
        i = offset_halfhours + 2 * k - 16  # 本地 k 时 ← UTC (k−8) 半点对
        a, b = values[i], values[i + 1]
        if not (np.isfinite(a) and np.isfinite(b)):
            missing += 1
            a = b = 0.0
        hours.append(float((a + b) / 2.0))
    times = [(t0 + timedelta(hours=k)).isoformat() for k in range(n_hours)]
    return times, np.asarray(hours), missing


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", type=Path, default=None,
                        help="本地 IMERG HDF5 文件（单时步）；缺省时只登记数据不可得")
    parser.add_argument("--site", choices=list(AOI_BBOX), default="guigang")
    parser.add_argument("--cumulative", action="store_true",
                        help="强制按累计口径反累计（默认自动探测）")
    from scripts.common.grid_io import add_common_args

    add_common_args(parser)
    args = parser.parse_args()
    log = AuditLog("prep_imerg_rain.py", run_tag=args.run_tag, dry_run=args.dry_run)
    log.param(site=args.site, input=str(args.input))

    rec: dict[str, Any] = {
        "script": "prep_imerg_rain.py",
        "generated_utc": utc_now(),
        "site": args.site,
        "aoi_bbox": list(AOI_BBOX[args.site]),
        "event_t0_local": EVENT_T0[args.site],
        "n_hours": N_HOURS[args.site],
        "data_available": False,
        "mmf_status": "未验证（IMERG 数据不可得：无 Earthdata 凭证且无本地文件）",
        "evidence_level": "E3（情景级）",
        "next_steps": [
            "配置 Earthdata 凭证（~/.netrc：urs.earthdata.nasa.gov）后从 GES DISC 下载"
            " GPM_3IMERGHH.07 事件窗文件（2024-09 或 2026-08）",
            "重跑本脚本 --input <HDF5>；产出经同一 GA 净雨链生成净雨 .rain",
            "净雨件就位后按 ADR-0016 §2 协议组装 scen_imerg_ga_100m（5.3 已授权）",
        ],
    }
    if args.input is not None:
        if not args.input.is_file():
            raise FileNotFoundError(f"IMERG 文件不存在：{args.input}")
        log.add_input("imerg_file", args.input)
        field, lon, lat, note = parse_imerg_precip(args.input)
        mean_rate = bbox_mean_field(field, lat, lon, AOI_BBOX[args.site])
        if not np.isfinite(mean_rate):
            raise ValueError("AOI bbox 内无有效 IMERG 格点；检查 bbox 或文件范围")
        rec.update({
            "data_available": True,
            "parse_note": note,
            "aoi_mean_rate_mmhr": round(mean_rate, 4),
            "grid_shape": [int(lat.size), int(lon.size)],
            "mmf_status": "单文件抽样解析成功；完整事件窗处理待多文件批量",
        })
        log.step("parsed", note=note, aoi_mean=mean_rate)
    out_json = args.log_json or (CATALOG / "imerg_prep_audit.json")
    log.payload["imerg"] = rec
    log.write(out_json)
    write_json(INTERIM / f"{args.site}_imerg_status.json", rec)
    print(json.dumps(rec, ensure_ascii=False)[:600])
    print("->", out_json)


if __name__ == "__main__":
    main()
