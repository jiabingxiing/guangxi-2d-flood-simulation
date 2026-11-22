"""Sub-grid land cover fractions and composite imperviousness extraction.

Plan reference: §3 / §6.3 / §6.4 / §7.4.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np


# Standard land cover classes for runoff modeling
LANDCOVER_CATEGORIES = (
    "forest",            # Trees, shrubs (WC 10, 20 / CLCD 2, 3)
    "cropland",          # Cropland, cultivated (WC 40 / CLCD 1)
    "grassland",         # Grassland (WC 30 / CLCD 4)
    "built_up",          # Settlements, urban, infrastructure (WC 50 / CLCD 8)
    "bare",              # Bare soil, sparse (WC 60 / CLCD 7)
    "water",             # Open water, rivers, ponds (WC 80 / CLCD 5)
    "wetland_mangrove",  # Wetlands, marshes, mangroves (WC 90, 95 / CLCD 9)
)


@dataclass(frozen=True)
class LandcoverGridFractions:
    """Sub-grid land cover fractional distribution on the 2D hydrodynamic grid.

    Attributes
    ----------
    fractions: dict mapping category name -> 2D numpy array of area fractions [0, 1].
               For any cell (r, c), sum of all category fractions is 1.0.
    impervious_fraction: 2D numpy array of estimated effective imperviousness [0, 1].
    water_mask: 2D boolean array where water fraction dominates (> 0.5).
    """

    fractions: dict[str, np.ndarray]
    impervious_fraction: np.ndarray
    water_mask: np.ndarray

    @property
    def shape(self) -> tuple[int, int]:
        return self.impervious_fraction.shape


def build_default_regional_fractions(
    shape: tuple[int, int],
    case: str = "jiulong",
) -> LandcoverGridFractions:
    """Provide realistic regional land cover fractions for Guangxi cases.

    Parameters
    ----------
    shape: (rows, cols)
    case: 'jiulong', 'guigang', or 'ningming'

    Returns
    -------
    LandcoverGridFractions summing to 1.0 everywhere.
    """
    rows, cols = shape
    fractions: dict[str, np.ndarray] = {}

    if case == "guigang":
        # Guigang Yujiang basin: extensive plains & agriculture, higher urban fraction
        f_forest = 0.35
        f_cropland = 0.45
        f_grass = 0.05
        f_built = 0.08
        f_bare = 0.02
        f_water = 0.03
        f_wetland = 0.02
    elif case == "ningming":
        # Ningming Mingjiang basin: karst hills, forest, sugar cane cropland, river
        f_forest = 0.50
        f_cropland = 0.35
        f_grass = 0.05
        f_built = 0.04
        f_bare = 0.02
        f_water = 0.02
        f_wetland = 0.02
    else:
        # Jiulong / Qinzhou coastal hills: mixed forest, orchards, paddy, rural settlements
        f_forest = 0.50
        f_cropland = 0.30
        f_grass = 0.05
        f_built = 0.06
        f_bare = 0.03
        f_water = 0.03
        f_wetland = 0.03

    fractions["forest"] = np.full(shape, f_forest, dtype=np.float64)
    fractions["cropland"] = np.full(shape, f_cropland, dtype=np.float64)
    fractions["grassland"] = np.full(shape, f_grass, dtype=np.float64)
    fractions["built_up"] = np.full(shape, f_built, dtype=np.float64)
    fractions["bare"] = np.full(shape, f_bare, dtype=np.float64)
    fractions["water"] = np.full(shape, f_water, dtype=np.float64)
    fractions["wetland_mangrove"] = np.full(shape, f_wetland, dtype=np.float64)

    # Impervious calculation
    # Built-up has 85% imperviousness, bare 10%, water 100%, others 2-5%
    impervious = (
        fractions["built_up"] * 0.85
        + fractions["bare"] * 0.10
        + fractions["cropland"] * 0.05
        + fractions["water"] * 1.00
    )
    impervious = np.clip(impervious, 0.0, 1.0)
    water_mask = fractions["water"] > 0.5

    return LandcoverGridFractions(
        fractions=fractions,
        impervious_fraction=impervious,
        water_mask=water_mask,
    )


def extract_landcover_fractions_for_grid(
    shape: tuple[int, int],
    *,
    worldcover_path: str | Path | None = None,
    case: str = "jiulong",
    dem_path: str | Path | None = None,
) -> LandcoverGridFractions:
    """Extract sub-grid land cover fractions from WorldCover or regional baseline.

    Parameters
    ----------
    shape: Target grid shape (rows, cols)
    worldcover_path: Optional path to WorldCover GeoTIFF
    case: Case identifier
    dem_path: Optional DEM path for georeferencing
    """
    # If worldcover is available and rasterio can be used to sample it
    if worldcover_path and Path(worldcover_path).is_file() and dem_path and Path(dem_path).is_file():
        try:
            import rasterio
            from rasterio.warp import calculate_default_transform, reproject, Resampling

            with rasterio.open(dem_path) as dem_src:
                dst_crs = dem_src.crs
                dst_transform = dem_src.transform
                dst_shape = shape

            with rasterio.open(worldcover_path) as wc_src:
                wc_reprojected = np.zeros(dst_shape, dtype=np.uint8)
                reproject(
                    source=rasterio.band(wc_src, 1),
                    destination=wc_reprojected,
                    src_transform=wc_src.transform,
                    src_crs=wc_src.crs,
                    dst_transform=dst_transform,
                    dst_crs=dst_crs,
                    resampling=Resampling.nearest,
                )

            # Map classes: 10/20->forest, 40->cropland, 30->grassland, 50->built_up, 60->bare, 80->water, 90/95->wetland
            fractions: dict[str, np.ndarray] = {cat: np.zeros(shape, dtype=np.float64) for cat in LANDCOVER_CATEGORIES}

            fractions["forest"] = np.where((wc_reprojected == 10) | (wc_reprojected == 20), 1.0, 0.0)
            fractions["cropland"] = np.where(wc_reprojected == 40, 1.0, 0.0)
            fractions["grassland"] = np.where(wc_reprojected == 30, 1.0, 0.0)
            fractions["built_up"] = np.where(wc_reprojected == 50, 1.0, 0.0)
            fractions["bare"] = np.where(wc_reprojected == 60, 1.0, 0.0)
            fractions["water"] = np.where(wc_reprojected == 80, 1.0, 0.0)
            fractions["wetland_mangrove"] = np.where((wc_reprojected == 90) | (wc_reprojected == 95), 1.0, 0.0)

            # Check unclassified or nodata (e.g. 0) and fill with case default
            total_f = sum(fractions.values())
            unmapped = total_f < 0.5
            if np.any(unmapped):
                default = build_default_regional_fractions(shape, case=case)
                for cat in LANDCOVER_CATEGORIES:
                    fractions[cat] = np.where(unmapped, default.fractions[cat], fractions[cat])

            # Normalize fractions to sum to 1.0
            total_f = sum(fractions.values())
            total_f = np.where(total_f <= 0.0, 1.0, total_f)
            for cat in LANDCOVER_CATEGORIES:
                fractions[cat] /= total_f

            impervious = (
                fractions["built_up"] * 0.85
                + fractions["bare"] * 0.10
                + fractions["cropland"] * 0.05
                + fractions["water"] * 1.00
            )
            impervious = np.clip(impervious, 0.0, 1.0)
            water_mask = fractions["water"] > 0.5

            return LandcoverGridFractions(
                fractions=fractions,
                impervious_fraction=impervious,
                water_mask=water_mask,
            )
        except Exception:
            # Fall back to default regional fractions on any warping issue
            pass

    return build_default_regional_fractions(shape, case=case)
