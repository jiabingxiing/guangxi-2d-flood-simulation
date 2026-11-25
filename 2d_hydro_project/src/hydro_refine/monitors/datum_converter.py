"""垂直基准面转换与有界对齐引擎（遵循 ADR-0004 / ADR-0017）。

核心原则：
1. 不同水文基面（1985国家高程、1956黄海、珠江基面、吴淞零点）向 EGM2008 / 本地 DEM
   转换时，必须记录基准状态（datum_closed=True/False）；
2. 未获取联测水准数据时（G7 未闭合），不得硬编码确定性常数，而是返回基准差区间
   或改用基准无关的「相对涨水幅度 ΔH(t)」进行评估。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

# 区域经验基面参考换算（仅用于已知国家基面间的名义换算，单位 m；本地站基面不做猜测）
STANDARD_DATUM_TO_85_M: dict[str, float] = {
    "1985_national": 0.0,
    "egm2008": 0.0,
    "1956_yellow_sea": -0.029,     # 广西沿海近似名义值（1985 ≈ 1956 - 0.029）
    "pearl_river_frozen": -0.586,  # 珠江基面名义换算示例，须以站档案为准
    "wusong": -1.80,
}


@dataclass(frozen=True)
class DatumAlignmentResult:
    """垂直基准对齐结果与置信度登记。"""

    source_datum: str
    target_datum: str
    offset_applied_m: float
    uncertainty_iqr_m: float | None
    datum_closed: bool
    method: str
    aligned_levels_m: np.ndarray

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_datum": self.source_datum,
            "target_datum": self.target_datum,
            "offset_applied_m": round(float(self.offset_applied_m), 4),
            "uncertainty_iqr_m": (
                None if self.uncertainty_iqr_m is None else round(float(self.uncertainty_iqr_m), 4)
            ),
            "datum_closed": self.datum_closed,
            "method": self.method,
        }


def load_atl13_probe_bounds(probe_json: Path) -> dict[str, Any] | None:
    """读取 ADR-0017 ICESat-2 ATL13 探针结果，提取有界偏差区间（无则返回 None）。"""
    if not probe_json.is_file():
        return None
    try:
        doc = json.loads(probe_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    probe = doc.get("probe", doc)
    if not probe.get("data_available"):
        return None
    verdict = probe.get("verdict") or {}
    interval = verdict.get("offset_interval")
    if not isinstance(interval, dict) or interval.get("median_m") is None:
        return None
    return {
        "median_m": float(interval["median_m"]),
        "iqr_m": float(interval.get("iqr_m") or 0.0),
        "coverage": verdict.get("coverage", "unknown"),
    }


def align_water_levels(
    levels_m: np.ndarray | list[float],
    *,
    source_datum: str = "station_local",
    target_datum: str = "dem_local",
    explicit_offset_m: float | None = None,
    atl13_bounds: dict[str, Any] | None = None,
) -> DatumAlignmentResult:
    """把观测水位从站点基面尝试对齐至模型 DEM 基准。

    遵循 ADR-0017：
    - 若 explicit_offset_m 已由测绘/水准联测给出 → datum_closed=True；
    - 若仅知标准国家基面关系 → 应用名义换算，若目标为未查证本地 DEM 仍记 datum_closed=False；
    - 若为 station_local 且无联测值 → offset=0.0，datum_closed=False，附带 ATL13 区间信息（若有）。
    """
    arr = np.asarray(levels_m, dtype=np.float64)
    if explicit_offset_m is not None:
        return DatumAlignmentResult(
            source_datum=source_datum,
            target_datum=target_datum,
            offset_applied_m=float(explicit_offset_m),
            uncertainty_iqr_m=0.0,
            datum_closed=True,
            method="explicit_surveyed_offset",
            aligned_levels_m=arr + float(explicit_offset_m),
        )

    if source_datum in STANDARD_DATUM_TO_85_M and target_datum in STANDARD_DATUM_TO_85_M:
        shift = STANDARD_DATUM_TO_85_M[source_datum] - STANDARD_DATUM_TO_85_M[target_datum]
        return DatumAlignmentResult(
            source_datum=source_datum,
            target_datum=target_datum,
            offset_applied_m=shift,
            uncertainty_iqr_m=0.10,
            datum_closed=True,
            method="standard_national_datum_conversion",
            aligned_levels_m=arr + shift,
        )

    iqr = float(atl13_bounds["iqr_m"]) if atl13_bounds else None
    note = (
        f"unclosed_g7_with_atl13_iqr_{iqr:.2f}m"
        if iqr is not None
        else "unclosed_g7_use_stage_rise_delta_h"
    )
    return DatumAlignmentResult(
        source_datum=source_datum,
        target_datum=target_datum,
        offset_applied_m=0.0,
        uncertainty_iqr_m=iqr,
        datum_closed=False,
        method=note,
        aligned_levels_m=arr.copy(),
    )


def compute_stage_rise(
    levels_m: np.ndarray | list[float],
    baseline_m: float | None = None,
) -> np.ndarray:
    """计算基准无关的相对涨水幅度 ΔH(t) = H(t) - H_base（用于 G7 未闭合稳健核验）。"""
    arr = np.asarray(levels_m, dtype=np.float64)
    if arr.size == 0:
        return arr.copy()
    valid = arr[np.isfinite(arr)]
    if valid.size == 0:
        return arr.copy()
    base = float(baseline_m) if baseline_m is not None and np.isfinite(baseline_m) else float(valid[0])
    return arr - base
