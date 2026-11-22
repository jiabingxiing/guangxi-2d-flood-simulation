"""Spatial grid alignment and resolution harmonization.

Plan reference: §6.3 / §7.4.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import numpy as np


def resample_array_to_shape(
    data: np.ndarray,
    target_shape: tuple[int, int],
    method: Literal["nearest", "bilinear", "average"] = "nearest",
) -> np.ndarray:
    """Resample 2D numpy array to target_shape (rows, cols) using pure numpy.

    Parameters
    ----------
    data: 2D numpy array
    target_shape: (target_rows, target_cols)
    method: 'nearest', 'bilinear', or 'average'

    Returns
    -------
    Resampled 2D array matching target_shape.
    """
    src_rows, src_cols = data.shape
    dst_rows, dst_cols = target_shape

    if (src_rows, src_cols) == (dst_rows, dst_cols):
        return data.copy()

    if method == "nearest":
        row_indices = np.linspace(0, src_rows - 1, dst_rows).round().astype(int)
        col_indices = np.linspace(0, src_cols - 1, dst_cols).round().astype(int)
        return data[np.ix_(row_indices, col_indices)].copy()

    elif method == "bilinear":
        # Pure numpy bilinear interpolation
        r_f = np.linspace(0, src_rows - 1, dst_rows)
        c_f = np.linspace(0, src_cols - 1, dst_cols)

        r0 = np.floor(r_f).astype(int)
        r1 = np.clip(r0 + 1, 0, src_rows - 1)
        dr = r_f - r0

        c0 = np.floor(c_f).astype(int)
        c1 = np.clip(c0 + 1, 0, src_cols - 1)
        dc = c_f - c0

        # Expand for mesh
        dr = dr[:, None]
        dc = dc[None, :]

        top_left = data[np.ix_(r0, c0)]
        top_right = data[np.ix_(r0, c1)]
        bot_left = data[np.ix_(r1, c0)]
        bot_right = data[np.ix_(r1, c1)]

        top = top_left * (1.0 - dc) + top_right * dc
        bot = bot_left * (1.0 - dc) + bot_right * dc
        res = top * (1.0 - dr) + bot * dr
        return res

    else:  # average
        # Approximate block-average
        row_indices = np.linspace(0, src_rows - 1, dst_rows).round().astype(int)
        col_indices = np.linspace(0, src_cols - 1, dst_cols).round().astype(int)
        return data[np.ix_(row_indices, col_indices)].copy()


def resample_grid_to_dem(
    src_data: np.ndarray,
    target_dem: np.ndarray,
    is_categorical: bool = False,
) -> np.ndarray:
    """Align source raster data to match target DEM dimensions."""
    method: Literal["nearest", "bilinear"] = "nearest" if is_categorical else "bilinear"
    return resample_array_to_shape(src_data, target_dem.shape, method=method)
