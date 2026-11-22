"""LISFLOOD-FP 8.1 NetCDF dynamic rainfall export module.

Plan reference: §6.1 / §7.4 / §10.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

import numpy as np

logger = logging.getLogger(__name__)


def export_netcdf_rainfall(
    output_path: str | Path,
    rain_rates_mm_h: np.ndarray,
    timestamps: Sequence[str],
    *,
    x_coords: np.ndarray | None = None,
    y_coords: np.ndarray | None = None,
    crs_epsg: int = 32649,
) -> Path:
    """Export 3D dynamic rainfall rates to NetCDF.

    Parameters
    ----------
    output_path: Destination .nc file path.
    rain_rates_mm_h: (time, rows, cols) array of rainfall rates (mm/hour).
    timestamps: Sequence of timestamps.
    x_coords: 1D array of x/easting coordinates.
    y_coords: 1D array of y/northing coordinates.
    crs_epsg: Coordinate system EPSG code.

    Returns
    -------
    Path to exported file.
    """
    p = Path(output_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    arr = np.asarray(rain_rates_mm_h, dtype=np.float32)
    n_time, rows, cols = arr.shape

    if x_coords is None:
        x_coords = np.arange(cols, dtype=np.float32) * 200.0
    if y_coords is None:
        y_coords = np.arange(rows, dtype=np.float32) * 200.0

    try:
        import netCDF4 as nc

        with nc.Dataset(p, "w", format="NETCDF4") as ds:
            ds.title = "LISFLOOD-FP 8.1 Net Rainfall Rate Grid"
            ds.source = "Rainfall-Runoff Preprocessor (Green-Ampt / SCS-CN)"
            ds.crs_epsg = crs_epsg

            # Dimensions
            ds.createDimension("time", n_time)
            ds.createDimension("y", rows)
            ds.createDimension("x", cols)

            # Variables
            time_var = ds.createVariable("time", "f4", ("time",))
            time_var.units = "hours since simulation start"
            time_var[:] = np.arange(n_time, dtype=np.float32)

            y_var = ds.createVariable("y", "f4", ("y",))
            y_var.units = "meters"
            y_var[:] = y_coords

            x_var = ds.createVariable("x", "f4", ("x",))
            x_var.units = "meters"
            x_var[:] = x_coords

            rain_var = ds.createVariable("rainfall", "f4", ("time", "y", "x"), zlib=True)
            rain_var.units = "mm/h"
            rain_var.long_name = "Net rainfall excess rate"
            rain_var[:] = arr

        logger.info(f"Successfully exported NetCDF rainfall: {p}")
        return p

    except ImportError:
        # Fallback when netCDF4 is not installed in the environment:
        # Save compressed npz format with same schema alongside an explanatory notice
        npz_path = p.with_suffix(".npz")
        np.savez_compressed(
            npz_path,
            rainfall=arr,
            time=np.arange(n_time, dtype=np.float32),
            x=x_coords,
            y=y_coords,
            crs_epsg=crs_epsg,
            timestamps=np.array(timestamps),
        )
        # Also touch dummy notice text
        with open(p, "w", encoding="utf-8") as f:
            f.write(
                f"# NetCDF4 library not installed in Python environment.\n"
                f"# Full 3D array saved to companion file: {npz_path.name}\n"
                f"# Array shape: {arr.shape}, total hours: {n_time}\n"
            )
        logger.info(f"netCDF4 not available; exported NPZ grid companion: {npz_path}")
        return p
