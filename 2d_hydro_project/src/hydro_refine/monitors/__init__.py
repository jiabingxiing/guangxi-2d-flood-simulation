"""官方水位监测数据接入、基准对齐与连续核验模块。"""
from __future__ import annotations

from hydro_refine.monitors.boundary_assimilation import (
    export_hvar_bdy,
    export_qvar_bdy,
    stage_to_discharge_rating,
)
from hydro_refine.monitors.datum_converter import (
    DatumAlignmentResult,
    align_water_levels,
    compute_stage_rise,
    load_atl13_probe_bounds,
)
from hydro_refine.monitors.gauge_extractor import extract_run_virtual_gauges
from hydro_refine.monitors.gauge_ingestor import (
    GaugeObservation,
    IngestionResult,
    StationSeries,
    ingest_case_gauges,
    interpolate_hourly_series,
)
from hydro_refine.monitors.hydro_evaluator import (
    evaluate_station_fit,
    kge_2009,
    mae,
    nse,
    pbias,
    pearson_r,
    rmse,
)

__all__ = [
    "DatumAlignmentResult",
    "GaugeObservation",
    "IngestionResult",
    "StationSeries",
    "align_water_levels",
    "compute_stage_rise",
    "evaluate_station_fit",
    "export_hvar_bdy",
    "export_qvar_bdy",
    "extract_run_virtual_gauges",
    "ingest_case_gauges",
    "interpolate_hourly_series",
    "kge_2009",
    "load_atl13_probe_bounds",
    "mae",
    "nse",
    "pbias",
    "pearson_r",
    "rmse",
    "stage_to_discharge_rating",
]
