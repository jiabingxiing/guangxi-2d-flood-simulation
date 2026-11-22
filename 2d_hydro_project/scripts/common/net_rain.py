"""预处理器净雨 .rain 读取与时基归一（ADR-0013 组合实验共用）。

逻辑镜像 scripts/stage3/run_jiulong_ga_rerun.py:88-108（久隆一次性已执行脚本，
不回改）；差异点：贵港 ERA5 probe 首键 2024-09-05T00 而模型 T0=2024-09-03T00，
时轴必须 **+48 h 显式移位**（久隆 T0=09-05 无需移位），且 probe 序列 312 h 短于
模拟窗 336 h，尾段按窗口裁剪 + 末点补零。

.rain 格式（预处理器 export）：`N hours|seconds` 头 + 逐行 `rate<tab>t`。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from scripts.common.grid_io import sha256_file


class NetRainError(ValueError):
    """净雨文件格式或时轴不合法。"""


def read_preprocessor_net_rain(
    path: str | Path,
    *,
    t_offset_s: float,
    sim_seconds: float,
) -> dict:
    """读预处理器净雨序列并归一到模型时轴。

    参数
    ----
    path : 预处理器产出的 .rain 文件
    t_offset_s : 时轴移位（贵港 = 48*3600；序列 t' + offset = 模型 t）
    sim_seconds : 模拟窗长；t > sim_seconds 的行被裁剪（裁掉量登记）

    返回
    ----
    dict(points=[(rate, t_s), ...] 含 (0,0) 与 (0,sim_seconds) 端点、
         total_mm、total_mm_in_window、tail_dropped_mm、source_sha256、
         n_source_rows、source_unit、source_end_s)
    """
    path = Path(path)
    if not path.is_file():
        raise NetRainError(f"净雨文件缺失：{path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) < 2:
        raise NetRainError(f"{path} 缺头部")
    head = lines[1].split()
    if len(head) != 2 or head[1].lower() not in ("hours", "seconds"):
        raise NetRainError(f"{path} 头第二行应为 'N hours|seconds'，实得 {lines[1]!r}")
    unit = head[1].lower()
    n_declared = int(head[0])
    rows = []
    for ln in lines[2:]:
        parts = ln.split()
        if len(parts) != 2:
            raise NetRainError(f"{path} 数据行不合法：{ln!r}")
        rows.append((float(parts[0]), float(parts[1])))
    if len(rows) != n_declared:
        raise NetRainError(f"{path} 声明 {n_declared} 行，实得 {len(rows)}")
    scale = 3600.0 if unit == "hours" else 1.0
    # 源时轴必须从 0 开始单调不降
    ts_src = [t * scale for _r, t in rows]
    if rows[0][1] != 0.0:
        raise NetRainError(f"{path} 源时轴未从 0 开始（首行 t={rows[0][1]}）")
    if any(b < a for a, b in zip(ts_src, ts_src[1:])):
        raise NetRainError(f"{path} 源时轴非单调不降")
    rates = np.array([r for r, _t in rows], dtype="float64")
    if (rates < 0).any():
        raise NetRainError(f"{path} 出现负净雨率")

    total_mm = float(rates.sum())  # 源轴逐小时率：和即 mm
    shifted = [(float(r), t * scale + float(t_offset_s)) for r, t in rows]
    in_window = [(r, t) for r, t in shifted if 0 < t <= sim_seconds + 1e-9]
    tail_dropped_mm = float(sum(r for r, t in shifted if t > sim_seconds + 1e-9))
    points = [(0.0, 0.0)] + in_window
    if not points or points[-1][1] < sim_seconds - 1e-9:
        points.append((0.0, float(sim_seconds)))
    # 时刻必须严格递增（与 guigang_scenario 降雨写出口径一致）
    ts = [t for _r, t in points]
    if any(b <= a for a, b in zip(ts, ts[1:])):
        raise NetRainError(f"{path} 归一后时刻非严格递增")
    return {
        "points": points,
        "total_mm": round(total_mm, 4),
        "total_mm_in_window": round(float(sum(r for r, _t in in_window)), 4),
        "tail_dropped_mm": round(tail_dropped_mm, 4),
        "source_sha256": sha256_file(path),
        "n_source_rows": len(rows),
        "source_unit": unit,
        "source_end_s": float(ts_src[-1]),
    }


def derive_probe_offset_s(probe_json_path: str | Path, t0_day: int) -> float:
    """由 ERA5 probe 首键与 T0 推导时轴移位（不写死 48 h）。

    probe 键格式 `2024-09-05T00:00`；模型 t = (day − t0_day)*86400 + hh*3600。
    """
    import json

    doc = json.loads(Path(probe_json_path).read_text(encoding="utf-8"))
    keys = sorted(doc["hourly_mm"].keys())
    if not keys:
        raise NetRainError(f"{probe_json_path} 无 hourly_mm 键")
    first = keys[0]
    day = int(first[8:10])
    hh = int(first[11:13])
    return float((day - t0_day) * 86400 + hh * 3600)


def write_lisflood_rain(points: list[tuple[float, float]], out_path: str | Path,
                        header_comment: str) -> Path:
    """写 LISFLOOD `N seconds` 格式 rain.rain（rate<tab>seconds，ASCII）。"""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    ts = [t for _r, t in points]
    if any(b <= a for a, b in zip(ts, ts[1:])):
        raise NetRainError("rain 时刻必须严格递增")
    with out.open("w", encoding="ascii", newline="\n") as fh:
        fh.write(header_comment)
        fh.write(f"{len(points)} seconds\n")
        for r, t in points:
            fh.write(f"{r:.4f}\t{t:.0f}\n")
    return out


def assert_mass_consistent(total_mm_in_window: float, cell_count: int, cell_area_m2: float,
                           reported_v_rain_m3: float, *, rel_tol: float = 1e-4) -> dict:
    """E1 式交叉核对：net_mm × 面积 与 res.mass 的 Rain-(Inf+Evap) 对照。"""
    expected = total_mm_in_window / 1000.0 * cell_count * cell_area_m2
    rel = abs(expected - reported_v_rain_m3) / max(abs(expected), 1.0)
    ok = rel <= rel_tol
    return {"expected_m3": round(expected, 1), "reported_m3": round(reported_v_rain_m3, 1),
            "rel_dev": float(f"{rel:.3e}"), "pass": bool(ok), "rel_tol": rel_tol}
