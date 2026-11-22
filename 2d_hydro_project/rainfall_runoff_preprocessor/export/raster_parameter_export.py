"""Spatial raster and LISFLOOD-FP .rain file export tools.

Plan reference: §6.1 / §7.4 / §10.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any, Sequence

import numpy as np

logger = logging.getLogger(__name__)


def export_ascii_grid(
    output_path: str | Path,
    data: np.ndarray,
    *,
    xllcorner: float = 0.0,
    yllcorner: float = 0.0,
    cellsize: float = 200.0,
    nodata_value: float = -9999.0,
) -> Path:
    """Export 2D numpy array as ESRI ASCII Grid (.asc).

    Header format:
      ncols         <N>
      nrows         <N>
      xllcorner     <X>
      yllcorner     <Y>
      cellsize      <C>
      NODATA_value  <NODATA>
    """
    p = Path(output_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    arr = np.asarray(data, dtype=np.float64)
    rows, cols = arr.shape

    with open(p, "w", encoding="utf-8") as f:
        f.write(f"ncols         {cols}\n")
        f.write(f"nrows         {rows}\n")
        f.write(f"xllcorner     {xllcorner:.4f}\n")
        f.write(f"yllcorner     {yllcorner:.4f}\n")
        f.write(f"cellsize      {cellsize:.4f}\n")
        f.write(f"NODATA_value  {nodata_value:.1f}\n")
        for r in range(rows):
            row_str = " ".join(f"{v:.4f}" if not np.isnan(v) else f"{nodata_value:.1f}" for v in arr[r])
            f.write(row_str + "\n")

    return p


def export_geotiff_raster(
    output_path: str | Path,
    data: np.ndarray,
    *,
    crs_epsg: int = 32649,
    bounds: tuple[float, float, float, float] | None = None,
    nodata: float = -9999.0,
) -> Path:
    """Export 2D array as GeoTIFF using rasterio.

    Parameters
    ----------
    output_path: Destination .tif file.
    data: 2D array.
    crs_epsg: EPSG code (e.g. 32649 for UTM 49N).
    bounds: (left, bottom, right, top) in CRS units.
    nodata: NoData value.
    """
    p = Path(output_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    arr = np.asarray(data, dtype=np.float32)
    rows, cols = arr.shape

    try:
        import rasterio
        from rasterio.transform import from_bounds

        if bounds is not None:
            left, bottom, right, top = bounds
            transform = from_bounds(left, bottom, right, top, cols, rows)
        else:
            transform = rasterio.transform.from_origin(0.0, float(rows * 200.0), 200.0, 200.0)

        with rasterio.open(
            p,
            "w",
            driver="GTiff",
            height=rows,
            width=cols,
            count=1,
            dtype=arr.dtype,
            crs=f"EPSG:{crs_epsg}",
            transform=transform,
            nodata=nodata,
        ) as dst:
            dst.write(arr, 1)

        return p
    except ImportError:
        # Fallback to ASCII grid if rasterio missing
        asc_path = p.with_suffix(".asc")
        export_ascii_grid(asc_path, arr, nodata_value=nodata)
        return asc_path


def export_lisflood_rain_file(
    output_path: str | Path,
    hourly_rates_mm_h: Sequence[float],
    *,
    header_comment: str = "LISFLOOD-FP Net Rainfall (mm/hr)",
) -> Path:
    """Export 1D hourly rainfall to native LISFLOOD-FP .rain format.

    Format:
      # comment
      N hours
      rate_0 0.0
      rate_1 1.0
      ...
      0.0   N.0
    """
    p = Path(output_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    n_hours = len(hourly_rates_mm_h)
    with open(p, "w", encoding="utf-8") as f:
        f.write(f"# {header_comment}\n")
        f.write(f"{n_hours + 1} hours\n")
        for h, rate in enumerate(hourly_rates_mm_h):
            f.write(f"{float(rate):.4f}\t{float(h):.1f}\n")
        # Final closing line at end of simulation
        f.write(f"0.0000\t{float(n_hours):.1f}\n")

    return p


def export_parameter_package(
    output_dir: str | Path,
    *,
    soil: Any,
    fractions: Any,
    theta_i: np.ndarray,
    gross_rain_ts: Sequence[float],
    ga_net_rain_ts: Sequence[float],
    cn_net_rain_ts: Sequence[float],
    horton_net_rain_ts: Sequence[float],
    mass_balance_ledger: list[dict],
    crs_epsg: int = 32649,
    bounds: tuple[float, float, float, float] | None = None,
) -> dict[str, Path]:
    """Export the complete deliverables bundle specified in Plan Section 7.4.

    Outputs:
      - soil_ksat.tif
      - soil_theta_s.tif
      - soil_theta_fc.tif
      - soil_theta_i.tif
      - soil_hsg.tif
      - landcover_impervious.tif
      - rainfall_gross.rain
      - rainfall_net_green_ampt.rain
      - rainfall_net_scs_cn.rain
      - rainfall_net_horton.rain
      - rainfall_runoff_mass_balance.csv
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    exported: dict[str, Path] = {}

    # Soil rasters
    exported["soil_ksat"] = export_geotiff_raster(out / "soil_ksat.tif", soil.ks_mm_h, crs_epsg=crs_epsg, bounds=bounds)
    exported["soil_theta_s"] = export_geotiff_raster(out / "soil_theta_s.tif", soil.theta_s, crs_epsg=crs_epsg, bounds=bounds)
    exported["soil_theta_fc"] = export_geotiff_raster(out / "soil_theta_fc.tif", soil.theta_fc, crs_epsg=crs_epsg, bounds=bounds)
    exported["soil_theta_i"] = export_geotiff_raster(out / "soil_theta_i.tif", theta_i, crs_epsg=crs_epsg, bounds=bounds)
    exported["soil_hsg"] = export_geotiff_raster(out / "soil_hsg.tif", soil.hsg, crs_epsg=crs_epsg, bounds=bounds)
    exported["landcover_impervious"] = export_geotiff_raster(
        out / "landcover_impervious.tif", fractions.impervious_fraction, crs_epsg=crs_epsg, bounds=bounds
    )

    # Rainfall .rain files for LISFLOOD-FP
    exported["rain_gross"] = export_lisflood_rain_file(
        out / "rainfall_gross.rain", gross_rain_ts, header_comment="Gross rainfall (ERA5 hourly, zero loss)"
    )
    exported["rain_ga"] = export_lisflood_rain_file(
        out / "rainfall_net_green_ampt.rain", ga_net_rain_ts, header_comment="Net rainfall (Green-Ampt preprocessed)"
    )
    exported["rain_cn"] = export_lisflood_rain_file(
        out / "rainfall_net_scs_cn.rain", cn_net_rain_ts, header_comment="Net rainfall (SCS-CN preprocessed)"
    )
    exported["rain_horton"] = export_lisflood_rain_file(
        out / "rainfall_net_horton.rain", horton_net_rain_ts, header_comment="Net rainfall (Horton preprocessed)"
    )

    # Mass balance CSV
    csv_path = out / "rainfall_runoff_mass_balance.csv"
    if mass_balance_ledger:
        fieldnames = list(mass_balance_ledger[0].keys())
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(mass_balance_ledger)
        exported["mass_balance_csv"] = csv_path

    logger.info(f"Exported complete parameter package to {out}")
    return exported
