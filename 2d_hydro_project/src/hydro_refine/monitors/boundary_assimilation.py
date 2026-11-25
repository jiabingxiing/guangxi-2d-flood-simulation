"""水位监测反向同化、边界过程线生成与动态校准诊断模块。

用途：
1. 将实测水位过程线转换为 LISFLOOD-FP 8.1.0 原生支持的下游水位边界（HVAR .bdy）；
2. 通过水位-流量关系曲线（Rating Curve）将上游监测水位反演为入流过程（QVAR .bdy，含 1/dx 标度律）；
3. 基于模拟与实测涨水偏差给出 Manning 糙率/河槽输水修正建议。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np


def stage_to_discharge_rating(
    stage_m: np.ndarray | list[float],
    *,
    h0_m: float,
    coeff_a: float,
    exponent_b: float = 1.667,
) -> np.ndarray:
    """水位-流量经验幂函数率定曲线 Q(H) = a * max(H - H0, 0)^b。"""
    h = np.asarray(stage_m, dtype=np.float64)
    head = np.maximum(h - float(h0_m), 0.0)
    return float(coeff_a) * (head ** float(exponent_b))


def _validate_monotonic_bdy(times_s: np.ndarray, values: np.ndarray) -> None:
    if times_s.ndim != 1 or values.ndim != 1 or times_s.size != values.size:
        raise ValueError("times_s 与 values 必须为等长一维序列")
    if times_s.size < 2:
        raise ValueError("LISFLOOD-FP .bdy 序列至少需要 2 个时刻点")
    if not np.all(np.isfinite(times_s)) or not np.all(np.isfinite(values)):
        raise ValueError(".bdy 序列包含 NaN 或 Inf")
    if not np.all(np.diff(times_s) > 0):
        raise ValueError("LISFLOOD-FP 要求 .bdy 时间列严格递增（input.cpp::LoadTimeSeries）")


def export_hvar_bdy(
    out_path: Path,
    series_name: str,
    times_s: np.ndarray | list[float],
    levels_m: np.ndarray | list[float],
    *,
    comment: str = "official gauge HVAR boundary",
) -> Path:
    """写出 LISFLOOD-FP HVAR .bdy 文件（水面高程 m）。"""
    t = np.asarray(times_s, dtype=np.float64)
    h = np.asarray(levels_m, dtype=np.float64)
    _validate_monotonic_bdy(t, h)
    lines = [f"# {comment}", series_name, f"{t.size} seconds"]
    for ti, hi in zip(t, h):
        lines.append(f"{hi:.4f}\t{ti:.0f}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    return out_path


def export_qvar_bdy(
    out_path: Path,
    series_name: str,
    times_s: np.ndarray | list[float],
    q_m3s: np.ndarray | list[float],
    *,
    cellsize_m: float,
    allow_negative: bool = False,
    comment: str = "assimilated QVAR boundary",
) -> Path:
    """写出 LISFLOOD-FP QVAR .bdy 文件（file_value = Q_m3s / cellsize_m）。"""
    if cellsize_m <= 0:
        raise ValueError(f"cellsize_m 必须为正，实得 {cellsize_m}")
    t = np.asarray(times_s, dtype=np.float64)
    q = np.asarray(q_m3s, dtype=np.float64)
    _validate_monotonic_bdy(t, q)
    if not allow_negative and np.any(q < 0):
        raise ValueError("标准入流 QVAR 禁止负流量（若做排水汇请显式传 allow_negative=True）")

    lines = [
        f"# {comment}; file_value = Q_m3s / cellsize({cellsize_m:.1f}m)",
        series_name,
        f"{t.size} seconds",
    ]
    for ti, qi in zip(t, q):
        lines.append(f"{qi / cellsize_m:.4f}\t{ti:.0f}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    return out_path


def diagnose_assimilation_adjustment(fit_metrics: dict[str, Any]) -> dict[str, Any]:
    """根据水文站拟合误差生成模型参数/边界调优物理建议（仅供情景调优参考）。"""
    rise_err = fit_metrics.get("rise_error_m")
    timing_err = fit_metrics.get("peak_timing_error_h")
    suggestions: list[str] = []
    if rise_err is not None:
        if rise_err > 0.5:
            suggestions.append(
                f"模拟涨水偏高 +{rise_err:.2f} m：建议降低主槽糙率 n 或增大河槽过水断面，或核查下游 FREE 顶托/坡降。"
            )
        elif rise_err < -0.5:
            suggestions.append(
                f"模拟涨水偏低 {rise_err:.2f} m：建议核查上游入流峰值是否偏低、或存在漫滩分流过快（提高滩地糙率）。"
            )
        else:
            suggestions.append(f"涨水幅度匹配良好（|Δ(ΔH)|={abs(rise_err):.2f} m <= 0.5 m）。")
    if timing_err is not None and abs(timing_err) > 3.0:
        direction = "滞后" if timing_err > 0 else "偏早"
        suggestions.append(
            f"洪峰时刻{direction} {abs(timing_err):.1f} h：建议检查上游入流洪峰时基或调整河床坡降/SGC 糙率。"
        )
    return {
        "rise_error_m": rise_err,
        "peak_timing_error_h": timing_err,
        "suggestions": suggestions,
    }
