"""阶段6：PipeNetGen 虚拟管网 1D PySWMM 模拟、2D 排水核构建与城区伪影治理评估。

功能：
1. 对指定案例的合成管网运行真实 EPA-SWMM5（PySWMM）动态水动力模拟；
2. 构建 2D 等效排水容量场 `drainage_capacity_mmh.asc` 与 LISFLOOD-FP 耦合边界件（`drainage_points.bci/.bdy`）；
3. 对比引入管网排涝前后城区内涝积水斑块数量、淹没面积与体积的消退成效；
4. 输出 `data/catalog/pipenet_coupling_evaluation.json`（含合成免责与不覆盖既有 run）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from hydro_refine.core.paths import rel_posix  # noqa: E402
from hydro_refine.coupling.drainage_kernel import (  # noqa: E402
    build_drainage_sink_grid,
    build_lisflood_drainage_bdy_and_bci,
    evaluate_urban_patch_drainage,
)
from hydro_refine.coupling.dynamic_pyswmm_link import run_swmm_drainage_simulation  # noqa: E402
from scripts.common.grid_io import AuditLog, utc_now, write_json  # noqa: E402

ERA5_GUIGANG = PROJ / "data" / "catalog" / "probes" / "guigang_sep2024_era5_hourly.json"
GUIGANG_REF_RUN = PROJ / "outputs" / "guigang" / "2024-09-yagi" / "scen_yagi_q11500_100m_dt2p5_chhyd_d14"


def run_guigang_coupling_eval() -> dict:
    pipe_dir = PROJ / "outputs" / "pipenet" / "guigang"
    inp_p = pipe_dir / "guigang_synthetic.inp"
    gj_p = pipe_dir / "guigang_pipes.geojson"
    if not inp_p.is_file() or not gj_p.is_file():
        raise FileNotFoundError(
            f"缺少贵港虚拟管网产物 {inp_p}，请先运行 build_synthetic_pipenet.py --case guigang"
        )

    swmm_res = run_swmm_drainage_simulation(inp_p)

    sink_info = build_drainage_sink_grid(
        gj_p,
        GUIGANG_REF_RUN / "dem.asc",
        epsg=32649,
        out_sink_asc=pipe_dir / "guigang_drainage_sink.asc",
    )

    # 读取 ERA5 实际小时降雨构建质量守恒的 LISFLOOD-FP 耦合边界点包
    era5 = json.loads(ERA5_GUIGANG.read_text(encoding="utf-8"))["hourly_mm"]
    times_s = [0.0]
    rains_mmh = [0.0]
    for k in sorted(era5):
        day, hh = k.split("T")
        t = (int(day[-2:]) - 3) * 86400.0 + int(hh[:2]) * 3600.0
        if 0 < t <= 14 * 86400.0:
            times_s.append(t)
            rains_mmh.append(float(era5[k]))

    bdy_pack = build_lisflood_drainage_bdy_and_bci(
        sink_info,
        times_s=times_s,
        urban_rain_mmh=rains_mmh,
        out_dir=pipe_dir / "lisflood_coupled_inputs",
    )

    # 等效排水水深 = PySWMM 1D 实测排河总水量 / 城区排涝有效面积（m）
    drained_vol_m3 = float(swmm_res["integrated_outfall_volume_m3"])
    urban_area_m2 = float(sink_info["urban_drained_area_km2"]) * 1e6
    eq_depth_m = drained_vol_m3 / max(urban_area_m2, 1.0)

    patch_eval = evaluate_urban_patch_drainage(
        GUIGANG_REF_RUN / "results" / "res.max",
        sink_info,
        swmm_drained_depth_m=eq_depth_m,
    )

    sink_summary = {k: v for k, v in sink_info.items() if k not in ("cap_mmh_grid", "outfalls_rc", "header")}
    return {
        "case": "guigang",
        "SYNTHETIC_DATA_NOT_REAL_ASSET": True,
        "swmm_1d_simulation": swmm_res,
        "drainage_sink_grid": {
            **sink_summary,
            "asc_file": rel_posix(pipe_dir / "guigang_drainage_sink.asc"),
        },
        "lisflood_coupled_boundary": bdy_pack,
        "urban_inundation_mitigation": patch_eval,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-json", type=Path, default=PROJ / "data" / "catalog" / "pipenet_coupling_evaluation.json")
    args = ap.parse_args()

    log = AuditLog("compare_pipenet_inundation")
    res = run_guigang_coupling_eval()
    payload = {
        "script": "scripts/stage6/compare_pipenet_inundation.py",
        "generated_utc": utc_now(),
        **res,
    }
    write_json(args.out_json, payload)
    log.step("evaluated", **{
        "patches_before": res["urban_inundation_mitigation"]["before"]["n_patches"],
        "patches_after": res["urban_inundation_mitigation"]["after"]["n_patches"],
        "patch_reduction_pct": res["urban_inundation_mitigation"]["patch_reduction_pct"],
    })
    log.write(PROJ / "logs" / "stage6" / "compare_pipenet_inundation.json")
    print(
        f"[guigang pipenet] patches {res['urban_inundation_mitigation']['before']['n_patches']} -> "
        f"{res['urban_inundation_mitigation']['after']['n_patches']} "
        f"(-{res['urban_inundation_mitigation']['patch_reduction_pct']}%), "
        f"vol_red={res['urban_inundation_mitigation']['volume_reduction_pct']}%"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
