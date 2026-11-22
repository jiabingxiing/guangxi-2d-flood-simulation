"""Command-line runner and pipeline orchestrator for rainfall-runoff preprocessing.

广西华南湿润红壤区二维地表水动力模拟：土壤下渗数据源与净雨转换算法（针对产流水量虚高）
Plan reference: §6 / §7 / §9 / §10.

Usage:
  uv --directory 2d_hydro_project run python -m rainfall_runoff_preprocessor.run_preprocessor --case jiulong
  uv --directory 2d_hydro_project run python -m rainfall_runoff_preprocessor.run_preprocessor --case guigang
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import yaml

# Ensure project root is in sys.path
PROJ_ROOT = Path(__file__).resolve().parents[1]
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))

from rainfall_runoff_preprocessor.preprocessing.rainfall_deaccumulation import (
    RainfallSeries,
    broadcast_rainfall_to_grid,
    load_rainfall_series,
)
from rainfall_runoff_preprocessor.preprocessing.soil_parameter_loader import (
    SoilGridParameters,
    load_soil_parameters_for_grid,
)
from rainfall_runoff_preprocessor.preprocessing.landcover_fraction import (
    LandcoverGridFractions,
    extract_landcover_fractions_for_grid,
)
from rainfall_runoff_preprocessor.preprocessing.antecedent_moisture import (
    calculate_initial_moisture,
    adjust_cn_for_amc,
)
from rainfall_runoff_preprocessor.infiltration.green_ampt import (
    green_ampt_net_rain,
    green_ampt_grid_with_landcover,
)
from rainfall_runoff_preprocessor.infiltration.scs_cn import (
    scs_cn_net_rain,
    scs_cn_grid_with_landcover,
)
from rainfall_runoff_preprocessor.infiltration.horton import (
    horton_net_rain,
    horton_grid_with_landcover,
)
from rainfall_runoff_preprocessor.validation.mass_balance import (
    generate_mass_balance_table,
    verify_mass_balance,
)
from rainfall_runoff_preprocessor.validation.event_runoff_validation import (
    EventRunoffMetrics,
    compare_runoff_inflation,
    compute_event_metrics,
)
from rainfall_runoff_preprocessor.validation.parameter_sensitivity import (
    run_parameter_sensitivity_suite,
)
from rainfall_runoff_preprocessor.export.raster_parameter_export import (
    export_parameter_package,
)
from rainfall_runoff_preprocessor.export.lisflood_rain_nc import (
    export_netcdf_rainfall,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("run_preprocessor")


def load_yaml_config(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def run_pipeline(
    case: str = "jiulong",
    output_dir: Path | None = None,
    scenario_mode: str = "all",
    substep_min: float = 1.0,
    config_dir: Path | None = None,
) -> dict[str, Any]:
    """Execute end-to-end preprocessing pipeline for a given case."""
    if config_dir is None:
        config_dir = Path(__file__).resolve().parent / "config"

    grid_cfg = load_yaml_config(config_dir / "model_grid.yaml")
    rain_cfg = load_yaml_config(config_dir / "rainfall_source.yaml")
    soil_cfg = load_yaml_config(config_dir / "soil_parameter_source.yaml")
    land_cfg = load_yaml_config(config_dir / "landcover_cn_lookup.yaml")

    if case not in grid_cfg["cases"]:
        raise ValueError(f"Unknown case '{case}'. Available: {list(grid_cfg['cases'].keys())}")

    case_info = grid_cfg["cases"][case]
    case_name = case_info["name"]
    res_m = float(case_info["resolution_m"])
    effective_depth_m = float(case_info.get("effective_soil_depth_m", 0.8))
    soil_region = case_info.get("soil_region", "south_china_red_soil")

    if output_dir is None:
        output_dir = PROJ_ROOT / "outputs" / "preprocessor" / case
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"=== Starting Rainfall-Runoff Preprocessor for [{case_name} ({case})] ===")
    logger.info(f"Resolution: {res_m} m | Soil Region: {soil_region} | Soil Depth: {effective_depth_m} m")

    # 1. Determine Grid Dimensions and DEM
    dem_rel = case_info.get("dem_path")
    dem_full = (PROJ_ROOT / dem_rel) if dem_rel else None
    dem_arr = None
    bounds = None
    crs_epsg = int(case_info.get("crs", "EPSG:32649").split(":")[-1])

    if dem_full and dem_full.is_file():
        import rasterio
        with rasterio.open(dem_full) as src:
            src_dem = src.read(1)
            src_bounds = src.bounds
            bounds = (src_bounds.left, src_bounds.bottom, src_bounds.right, src_bounds.top)
            # Resample DEM if resolution differs
            src_res = src.res[0]
            if abs(src_res - res_m) > 1.0:
                scale_factor = src_res / res_m
                target_rows = max(10, int(round(src.height * scale_factor)))
                target_cols = max(10, int(round(src.width * scale_factor)))
                from rainfall_runoff_preprocessor.preprocessing.soil_grid_alignment import resample_array_to_shape
                dem_arr = resample_array_to_shape(src_dem, (target_rows, target_cols), method="nearest")
            else:
                dem_arr = src_dem
        grid_shape = dem_arr.shape
        logger.info(f"Loaded DEM from {dem_rel}, grid shape: {grid_shape}")
    else:
        # Default manageable grid (e.g. 150 x 180 cells ~ 30km x 36km @ 200m)
        grid_shape = (150, 180)
        dem_arr = np.full(grid_shape, 25.0, dtype=np.float64)
        logger.info(f"Using synthetic grid shape: {grid_shape}")

    domain_area_m2 = grid_shape[0] * grid_shape[1] * (res_m ** 2)
    logger.info(f"Total domain area: {domain_area_m2 / 1e6:.2f} km² ({grid_shape[0]}x{grid_shape[1]} cells)")

    # 2. Load Hourly Rainfall Series
    rain_rel = case_info.get("rainfall_probe")
    rain_full = PROJ_ROOT / rain_rel
    rain_series = load_rainfall_series(rain_full)
    logger.info(
        f"Loaded rainfall series: {rain_series.duration_hours} hours, "
        f"Total Gross: {rain_series.total_mm:.2f} mm, "
        f"Peak: {rain_series.peak_rate_mm_h:.2f} mm/h (hour {rain_series.peak_index})"
    )

    # 3. Load Soil Parameters
    soil_params = load_soil_parameters_for_grid(
        shape=grid_shape,
        region_type=soil_region,
        dem=dem_arr,
        effective_depth_m=effective_depth_m,
    )
    logger.info(
        f"Soil parameters generated: Ks mean={float(np.mean(soil_params.ks_mm_h)):.2f} mm/h, "
        f"theta_s mean={float(np.mean(soil_params.theta_s)):.3f}, "
        f"psi mean={float(np.mean(soil_params.psi_mm)):.1f} mm"
    )

    # 4. Extract Sub-grid Land Cover Fractions
    wc_rel = case_info.get("worldcover_tif")
    wc_full = (PROJ_ROOT / wc_rel) if wc_rel else None
    fractions = extract_landcover_fractions_for_grid(
        shape=grid_shape,
        worldcover_path=wc_full,
        case=case,
        dem_path=dem_full,
    )
    mean_imp = float(np.mean(fractions.impervious_fraction))
    logger.info(f"Land cover processed: mean impervious fraction = {mean_imp * 100.0:.1f}%")

    # 5. Broadcast Rainfall to 3D Grid
    P_grid_3d = broadcast_rainfall_to_grid(rain_series.rates_mm_h, grid_shape)

    # 6. Build lookup tables for SCS-CN and Horton
    cn_lookup = {}
    for cat_code, cat_info in land_cfg["worldcover_classes"].items():
        cn_lookup[cat_info["name"]] = cat_info["cn_hsg"]

    horton_defaults = land_cfg["horton_defaults"]

    # 7. Run Parameter Sensitivity Suite (8 Scenarios)
    logger.info("Executing 8-scenario parameter sensitivity matrix...")
    sensitivity_results = run_parameter_sensitivity_suite(
        P_mm=P_grid_3d,
        soil=soil_params,
        fractions=fractions,
        cn_lookup=cn_lookup,
        horton_defaults=horton_defaults,
        domain_area_m2=domain_area_m2,
        dt_h=1.0,
    )

    scenarios = sensitivity_results["scenarios"]
    time_series = sensitivity_results["time_series"]
    inflation = sensitivity_results["inflation_comparisons"]

    # 8. Strict Mass Balance Verification on all scenarios
    logger.info("Performing strict mass conservation audit (quality gate <= 1.0e-5)...")
    for name, ts_dict in time_series.items():
        net_arr = ts_dict["net_rain"]
        loss_arr = ts_dict["loss"]
        passed, max_abs, rel_err = verify_mass_balance(P_grid_3d, net_arr, loss_arr)
        if not passed:
            raise RuntimeError(f"Mass balance FAILED for scenario '{name}'! Rel err={rel_err:.2e} > 1e-5")
        logger.info(f"  [PASS] Scenario '{name}': Rel Err = {rel_err:.2e} <= 1.0e-5 (Max Abs Err = {max_abs:.2e} mm)")

    # 9. Generate Mass Balance Ledger for Preferred Green-Ampt Baseline (GA-Mid)
    ga_mid_net_1d = np.mean(time_series["GA-Mid"]["net_rain"], axis=(1, 2))
    ga_mid_loss_1d = np.mean(time_series["GA-Mid"]["loss"], axis=(1, 2))
    ledger = generate_mass_balance_table(
        timestamps=rain_series.timestamps,
        P_series_mm=rain_series.rates_mm_h,
        R_net_series_mm=ga_mid_net_1d,
        Loss_series_mm=ga_mid_loss_1d,
        cell_area_m2=domain_area_m2,
    )

    # 10. Export Parameter Package (.tif, .asc, .rain, .csv)
    logger.info(f"Exporting parameter rasters and LISFLOOD-FP .rain files to {output_dir}...")
    cn200_net_1d = np.mean(time_series["CN-200"]["net_rain"], axis=(1, 2))
    horton_net_1d = np.mean(time_series["Horton"]["net_rain"], axis=(1, 2))

    moist_mid = calculate_initial_moisture(soil_params.theta_s, soil_params.theta_wp, scenario="mid")

    exported_files = export_parameter_package(
        output_dir=output_dir,
        soil=soil_params,
        fractions=fractions,
        theta_i=moist_mid.theta_i,
        gross_rain_ts=rain_series.rates_mm_h,
        ga_net_rain_ts=ga_mid_net_1d,
        cn_net_rain_ts=cn200_net_1d,
        horton_net_rain_ts=horton_net_1d,
        mass_balance_ledger=ledger,
        crs_epsg=crs_epsg,
        bounds=bounds,
    )

    # Also export NetCDF for GA-Mid
    nc_path = output_dir / "rainfall_net_green_ampt.nc"
    export_netcdf_rainfall(
        nc_path,
        time_series["GA-Mid"]["net_rain"],
        rain_series.timestamps,
        crs_epsg=crs_epsg,
    )
    exported_files["rainfall_net_nc"] = nc_path

    # 11. Write Run Summary JSON
    summary_catalog = {
        "case": case,
        "case_name": case_name,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "gross_rainfall_mm": rain_series.total_mm,
        "duration_hours": rain_series.duration_hours,
        "domain_area_km2": round(domain_area_m2 / 1e6, 2),
        "resolution_m": res_m,
        "scenarios": {k: v.__dict__ for k, v in scenarios.items()},
        "runoff_inflation_analysis": inflation,
        "exported_files": {k: str(v.name) for k, v in exported_files.items()},
    }

    catalog_path = output_dir / "preprocessor_summary.json"
    with open(catalog_path, "w", encoding="utf-8") as f:
        json.dump(summary_catalog, f, indent=2, ensure_ascii=False)

    # 12. Print Formatted Comparison Table
    print("\n" + "=" * 90)
    print(f"=== 广西水动力产流损失与净雨预处理成果表 [{case_name} ({case})] ===")
    print("=" * 90)
    print(f"{'情景方案':<12} | {'总降雨(mm)':<10} | {'净雨量(mm)':<10} | {'下渗损失(mm)':<12} | {'径流系数 C':<10} | {'洪峰雨强(mm/h)':<14} | {'削峰率':<8}")
    print("-" * 90)
    for name, m in scenarios.items():
        print(
            f"{name:<12} | {m.gross_rain_mm:<10.1f} | {m.net_rain_mm:<10.1f} | "
            f"{m.loss_mm:<12.1f} | {m.runoff_coefficient:<10.3f} | {m.peak_net_rate_mm_h:<14.2f} | "
            f"{m.peak_attenuation_ratio * 100.0:<7.1f}%"
        )
    print("=" * 90)

    print("\n" + "=" * 90)
    print(f"=== 零损失基准（产流水量虚高）对比量化表 ===")
    print("=" * 90)
    print(f"{'对比模型':<12} | {'净雨量(mm)':<10} | {'虚高径流深(mm)':<14} | {'虚高水量(万m³)':<14} | {'产流虚高幅度':<14} | {'基准削减率':<10}")
    print("-" * 90)
    base_m = scenarios["Baseline"]
    for name, inf in inflation.items():
        m = scenarios[name]
        diff_depth = base_m.net_rain_mm - m.net_rain_mm
        diff_vol_wan = inf["overestimated_volume_m3"] / 10000.0
        print(
            f"{name:<12} | {m.net_rain_mm:<10.1f} | {diff_depth:<14.1f} | "
            f"{diff_vol_wan:<14.1f} | +{inf['runoff_inflation_pct']:<13.1f}% | -{inf['runoff_reduction_pct']:<9.1f}%"
        )
    print("=" * 90 + "\n")

    logger.info(f"=== Finished Preprocessing Pipeline for [{case}]. Output in {output_dir} ===")
    return summary_catalog


def main() -> None:
    parser = argparse.ArgumentParser(description="2D Hydrodynamic Rainfall-Runoff Preprocessor")
    parser.add_argument("--case", choices=["jiulong", "guigang", "ningming"], default="jiulong", help="Case study")
    parser.add_argument("--scenario", default="all", help="Scenario to run (all, baseline, ga, cn, horton)")
    parser.add_argument("--output-dir", type=str, default=None, help="Custom output directory")
    parser.add_argument("--substep-min", type=float, default=1.0, help="Internal integration sub-step in minutes")
    args = parser.parse_args()

    out_p = Path(args.output_dir) if args.output_dir else None
    run_pipeline(
        case=args.case,
        output_dir=out_p,
        scenario_mode=args.scenario,
        substep_min=args.substep_min,
    )


if __name__ == "__main__":
    main()
