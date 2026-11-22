"""Rainfall data loading, deaccumulation, unit conversion, and temporal alignment.

Plan reference: §6.2 / §7.4 / §10.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

import numpy as np


@dataclass(frozen=True)
class RainfallSeries:
    """Hourly rainfall time series container.

    Attributes
    ----------
    timestamps: list of ISO datetime strings (UTC or local, consistent)
    rates_mm_h: 1D numpy array of hourly rainfall rate (mm/hour)
    total_mm: sum of rainfall depths across all hours (mm)
    peak_rate_mm_h: maximum hourly rainfall rate (mm/hour)
    peak_index: index of maximum hourly rate
    """

    timestamps: list[str]
    rates_mm_h: np.ndarray
    total_mm: float
    peak_rate_mm_h: float
    peak_index: int

    @property
    def duration_hours(self) -> int:
        return len(self.timestamps)


def deaccumulate_rainfall(
    accumulated: Sequence[float],
    reset_negative: bool = True,
) -> np.ndarray:
    """Convert accumulated precipitation (e.g. cumulative sum) to incremental rates.

    Parameters
    ----------
    accumulated: Sequence of cumulative depths (e.g., in mm or m).
    reset_negative: If True, reset any negative difference (due to model cycle resets) to 0.

    Returns
    -------
    incremental: 1D numpy array of incremental depths.
    """
    arr = np.asarray(accumulated, dtype=np.float64)
    if arr.ndim != 1 or len(arr) == 0:
        raise ValueError("Accumulated rainfall must be a non-empty 1D array.")

    diff = np.diff(arr, prepend=0.0)
    if reset_negative:
        diff = np.where(diff < 0.0, 0.0, diff)
    return diff


def load_rainfall_series(
    file_path: str | Path,
    *,
    unit: str = "mm",
    is_cumulative: bool = False,
    start_time: str | None = None,
    end_time: str | None = None,
) -> RainfallSeries:
    """Load hourly rainfall time series from JSON or text file.

    Parameters
    ----------
    file_path: Path to JSON probe file with `hourly_mm` dict or list.
    unit: Input unit, 'mm' or 'm'. If 'm', multiplied by 1000.0.
    is_cumulative: Whether input is cumulative and requires deaccumulation.
    start_time: Optional ISO timestamp to slice start (inclusive).
    end_time: Optional ISO timestamp to slice end (inclusive).

    Returns
    -------
    RainfallSeries object with clean, non-negative mm/h rates.
    """
    p = Path(file_path)
    if not p.is_file():
        raise FileNotFoundError(f"Rainfall file not found: {p}")

    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)

    if "hourly_mm" in data:
        raw_dict = data["hourly_mm"]
        sorted_keys = sorted(raw_dict.keys())
        timestamps = sorted_keys
        vals = [float(raw_dict[k]) for k in sorted_keys]
    elif "series" in data:
        series_data = data["series"]
        timestamps = [item["time"] for item in series_data]
        vals = [float(item["val"]) for item in series_data]
    else:
        raise ValueError(f"Unrecognized rainfall JSON format in {p}")

    # Optional time window slicing
    if start_time or end_time:
        sliced_ts = []
        sliced_vals = []
        for ts, val in zip(timestamps, vals):
            if start_time and ts < start_time:
                continue
            if end_time and ts > end_time:
                continue
            sliced_ts.append(ts)
            sliced_vals.append(val)
        timestamps = sliced_ts
        vals = sliced_vals

    if not timestamps:
        raise ValueError(f"No rainfall records found within requested time window in {p}")

    arr = np.asarray(vals, dtype=np.float64)

    # Unit conversion
    if unit == "m":
        arr *= 1000.0

    # Deaccumulation if required
    if is_cumulative:
        arr = deaccumulate_rainfall(arr)

    # Ensure non-negative
    arr = np.maximum(0.0, arr)

    total_mm = float(np.sum(arr))
    peak_idx = int(np.argmax(arr))
    peak_rate = float(arr[peak_idx])

    return RainfallSeries(
        timestamps=timestamps,
        rates_mm_h=arr,
        total_mm=total_mm,
        peak_rate_mm_h=peak_rate,
        peak_index=peak_idx,
    )


def broadcast_rainfall_to_grid(
    rates_1d: np.ndarray,
    shape_2d: tuple[int, int],
) -> np.ndarray:
    """Broadcast a 1D hourly rainfall rate array to 3D grid (time, rows, cols).

    Parameters
    ----------
    rates_1d: 1D array of shape (time,)
    shape_2d: tuple (nrows, ncols)

    Returns
    -------
    3D array of shape (time, nrows, ncols)
    """
    rates = np.asarray(rates_1d, dtype=np.float64)
    if rates.ndim != 1:
        raise ValueError("rates_1d must be 1-dimensional.")
    nrows, ncols = shape_2d
    return np.broadcast_to(rates[:, None, None], (len(rates), nrows, ncols)).copy()
