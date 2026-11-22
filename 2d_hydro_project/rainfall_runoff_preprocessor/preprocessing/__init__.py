"""Preprocessing module for rainfall-runoff and soil parameter preparation."""

from .rainfall_deaccumulation import (
    RainfallSeries,
    load_rainfall_series,
    deaccumulate_rainfall,
    broadcast_rainfall_to_grid,
)
from .soil_parameter_loader import (
    SoilGridParameters,
    load_soil_parameters_for_grid,
)
from .landcover_fraction import (
    LandcoverGridFractions,
    extract_landcover_fractions_for_grid,
)
from .soil_grid_alignment import (
    resample_grid_to_dem,
)
from .antecedent_moisture import (
    AntecedentMoistureState,
    calculate_initial_moisture,
    classify_amc,
    adjust_cn_for_amc,
)

__all__ = [
    "RainfallSeries",
    "load_rainfall_series",
    "deaccumulate_rainfall",
    "broadcast_rainfall_to_grid",
    "SoilGridParameters",
    "load_soil_parameters_for_grid",
    "LandcoverGridFractions",
    "extract_landcover_fractions_for_grid",
    "resample_grid_to_dem",
    "AntecedentMoistureState",
    "calculate_initial_moisture",
    "classify_amc",
    "adjust_cn_for_amc",
]
