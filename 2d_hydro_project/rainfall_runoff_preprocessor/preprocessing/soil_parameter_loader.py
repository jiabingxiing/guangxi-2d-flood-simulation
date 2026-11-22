"""Soil parameter loader, regional prior mapping, and grid parameter derivation.

Plan reference: §2 / §4.2 / §6.3 / §10.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


@dataclass(frozen=True)
class SoilGridParameters:
    """2D spatial distribution of soil hydraulic parameters on the model grid.

    Attributes
    ----------
    ks_mm_h: Saturated hydraulic conductivity (mm/hour), shape (rows, cols)
    psi_mm: Wetting front suction head (mm), shape (rows, cols)
    theta_s: Saturated volumetric water content (m3/m3), shape (rows, cols)
    theta_fc: Field capacity volumetric water content, shape (rows, cols)
    theta_wp: Wilting point volumetric water content, shape (rows, cols)
    hsg: Hydrologic soil group as numeric code (1=A, 2=B, 3=C, 4=D) or string
    effective_depth_m: Effective soil profile depth (m), shape (rows, cols)
    nodata_mask: Boolean mask where True indicates NoData/water/invalid cells
    """

    ks_mm_h: np.ndarray
    psi_mm: np.ndarray
    theta_s: np.ndarray
    theta_fc: np.ndarray
    theta_wp: np.ndarray
    hsg: np.ndarray
    effective_depth_m: np.ndarray
    nodata_mask: np.ndarray

    @property
    def shape(self) -> tuple[int, int]:
        return self.ks_mm_h.shape


def build_synthetic_regional_soil(
    shape: tuple[int, int],
    region_type: str = "south_china_red_soil",
    *,
    dem: np.ndarray | None = None,
    effective_depth_m: float = 0.8,
) -> SoilGridParameters:
    """Construct regional soil hydraulic parameters based on South China red soil priors.

    Parameters
    ----------
    shape: (rows, cols) of target grid
    region_type: 'south_china_red_soil' (Jiulong), 'south_china_alluvial_red_soil' (Guigang),
                 or 'karst_limestone_soil' (Ningming)
    dem: Optional elevation array to introduce natural topographic toposequence differentiation
    effective_depth_m: Base effective soil layer thickness

    Returns
    -------
    SoilGridParameters with spatially consistent physical parameters.
    """
    rows, cols = shape
    nodata_mask = np.zeros(shape, dtype=bool)

    if dem is not None and dem.shape == shape:
        nodata_mask = (dem <= -9000.0) | np.isnan(dem)

    # Base parameters according to region type
    if region_type == "south_china_alluvial_red_soil":
        # Alluvial plains (Guigang): silt loam to clay loam, HSG C/B in terraces, C/D in lowlands
        base_ks = 2.5
        base_psi = 180.0
        base_ts = 0.47
        base_tfc = 0.35
        base_twp = 0.19
        base_hsg = 3  # C
        base_depth = 1.0
    elif region_type == "karst_limestone_soil":
        # Karst limestone hills & shallow soils (Ningming): thinner soil, higher stoniness, HSG C/D
        base_ks = 3.5
        base_psi = 160.0
        base_ts = 0.44
        base_tfc = 0.30
        base_twp = 0.17
        base_hsg = 3  # C
        base_depth = 0.6
    else:
        # Classical South China hilly red soil (Jiulong / Qinzhou):
        # Sandy clay loam to clay loam, moderate Ks with high root/aggregate permeability in hills
        base_ks = 3.0
        base_psi = 200.0
        base_ts = 0.46
        base_tfc = 0.32
        base_twp = 0.19
        base_hsg = 3  # C
        base_depth = effective_depth_m

    # Generate slight spatial toposequence variation if DEM is present
    if dem is not None and not np.all(nodata_mask):
        valid_elev = np.where(~nodata_mask, dem, np.nan)
        elev_min = float(np.nanmin(valid_elev))
        elev_max = float(np.nanmax(valid_elev))
        span = max(elev_max - elev_min, 1.0)
        norm_elev = np.clip((dem - elev_min) / span, 0.0, 1.0)

        # Upper slopes have higher infiltration & suction, lower lowlands have slower Ks & deeper soil
        ks_arr = np.where(~nodata_mask, base_ks * (0.8 + 0.6 * norm_elev), base_ks)
        psi_arr = np.where(~nodata_mask, base_psi * (1.1 - 0.2 * norm_elev), base_psi)
        depth_arr = np.where(~nodata_mask, base_depth * (1.3 - 0.5 * norm_elev), base_depth)
    else:
        ks_arr = np.full(shape, base_ks, dtype=np.float64)
        psi_arr = np.full(shape, base_psi, dtype=np.float64)
        depth_arr = np.full(shape, base_depth, dtype=np.float64)

    ts_arr = np.full(shape, base_ts, dtype=np.float64)
    tfc_arr = np.full(shape, base_tfc, dtype=np.float64)
    twp_arr = np.full(shape, base_twp, dtype=np.float64)
    hsg_arr = np.full(shape, base_hsg, dtype=np.int32)

    return SoilGridParameters(
        ks_mm_h=ks_arr,
        psi_mm=psi_arr,
        theta_s=ts_arr,
        theta_fc=tfc_arr,
        theta_wp=twp_arr,
        hsg=hsg_arr,
        effective_depth_m=depth_arr,
        nodata_mask=nodata_mask,
    )


def load_soil_parameters_for_grid(
    shape: tuple[int, int],
    *,
    soil_raster_path: str | Path | None = None,
    region_type: str = "south_china_red_soil",
    dem: np.ndarray | None = None,
    effective_depth_m: float = 0.8,
) -> SoilGridParameters:
    """Load or generate soil parameters aligned to target 2D model grid."""
    if soil_raster_path and Path(soil_raster_path).is_file():
        # Load from file if available (e.g. GeoTIFF / HiHydroSoil)
        import rasterio
        with rasterio.open(soil_raster_path) as src:
            data = src.read(1)
            # Apply scaling if needed (e.g. integer * 0.0001)
            if data.dtype in (np.int16, np.int32, np.uint16):
                data = data.astype(np.float64) * 0.0001
            # Resize if necessary
            if data.shape != shape:
                from .soil_grid_alignment import resample_array_to_shape
                data = resample_array_to_shape(data, shape)
            ks_arr = np.clip(data * (10.0 / 24.0), 0.1, 100.0)
            psi_arr = np.full(shape, 200.0, dtype=np.float64)
            ts_arr = np.full(shape, 0.46, dtype=np.float64)
            tfc_arr = np.full(shape, 0.32, dtype=np.float64)
            twp_arr = np.full(shape, 0.19, dtype=np.float64)
            hsg_arr = np.full(shape, 3, dtype=np.int32)
            depth_arr = np.full(shape, effective_depth_m, dtype=np.float64)
            nodata_mask = np.isnan(ks_arr) | (ks_arr <= 0)
            return SoilGridParameters(
                ks_mm_h=ks_arr,
                psi_mm=psi_arr,
                theta_s=ts_arr,
                theta_fc=tfc_arr,
                theta_wp=twp_arr,
                hsg=hsg_arr,
                effective_depth_m=depth_arr,
                nodata_mask=nodata_mask,
            )

    return build_synthetic_regional_soil(
        shape=shape,
        region_type=region_type,
        dem=dem,
        effective_depth_m=effective_depth_m,
    )
