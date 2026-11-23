"""久隆淹没 S1 遥感验证（R0–R4，只读诊断，降级口径）。

S1 现状（ADR-0010）：久隆 2024-09 洪峰期**无 S1 过境**，仅
- 灾前常水景 `jiulong_20240905T105022_water20m.tif`（20 m，EPSG:32649，{0,1,255}，255=nodata）；
- 退水后景 `jiulong_20240917T105023_water20m.tif`。
两景均为**趋势参照**，无洪中事件观测——本文件所有 CSI/POD/FAR 都不是淹没精度结论。

口径
- 重投影：rasterio reproject + Resampling.average + src_nodata=255 → 200 m 模型网格
  水体占比；常水观测掩膜 = 占比 > 0.12（river-constant water）；
- 有效足迹 = 覆盖占比 > 0.5 且非海（同 jiulong_recession_s1 口径）；足迹外不计虚警；
- 模型湿格：res.max depth > 0.05 m，剔除海面；对灾前常水掩膜算 CSI/POD/FAR；
  基线参照 CSI 0.018 / POD 0.387 / FAR 0.982（diagnostics/jiulong/root_cause_report.md
  §3.4，复用未重算，仅作量级对照）；
- 退水方向：模型 frame_series 末 3 帧湿面积差符号 vs S1 常水面积变化方向
  （S1_2 − S1_1）；同向记 PASS（趋势一致性，不是精度通过门）。

产出：`diagnostics/jiulong/validation/s1_validation.json`；
审计 `logs/diagnostics/validate_jiulong_inundation.json`。
缺 run 跳过（status="missing"）；已有报告拒绝覆盖，除非 --run-tag（tag 子目录）。

用法（先设 `$env:PYTHONUTF8="1"`）::

    uv --directory 2d_hydro_project run python scripts/stage6/validate_jiulong_inundation.py --dry-run
"""
from __future__ import annotations

import json
import sys
from argparse import ArgumentParser
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import (  # noqa: E402
    AuditLog,
    GridError,
    add_common_args,
    load_sea_mask,
    read_ascii_grid,
    read_ascii_header,
    sha256_file,
    utc_now,
    write_json,
)
from scripts.common.paths import rel_posix  # noqa: E402
from scripts.stage3.run_jiulong_dual_forcing import wet_metrics  # noqa: E402
from scripts.stage6.compare_jiulong_scenarios import (  # noqa: E402
    EVENT_DIR,
    RUN_NAMES,
    WET_THRESHOLD_M,
    direction_of,
    guard_report_target,
    recession_tail_delta_km2,
    rel_or_name,
    resolve_out_dir,
)

S1_PRE = PROJ / "data/processed/s1_water/jiulong_20240905T105022_water20m.tif"
S1_POST = PROJ / "data/processed/s1_water/jiulong_20240917T105023_water20m.tif"
S1_FRAC_THR = 0.12       # 常水观测掩膜阈值（200 m 格水体占比）
S1_COVER_THR = 0.5       # 有效足迹阈值（同 jiulong_recession_s1）
DEFAULT_OUT_DIR = PROJ / "diagnostics/jiulong/validation"
DEFAULT_LOG = PROJ / "logs/diagnostics/validate_jiulong_inundation.json"

# 基线参照（root_cause_report.md §3.4，复用未重算）：仅量级对照，非本表重算目标
BASELINE_S1_REFERENCE = {"csi": 0.018, "pod": 0.387, "far": 0.982,
                         "source": "diagnostics/jiulong/root_cause_report.md §3.4（S1 对照复用，未重算）"}

FLOOD_OBS_NOTE = ("洪峰期无 S1 过境：仅 09-05（灾前）与 09-17（退水后）两景，"
                  "均为趋势参照，无洪中事件观测（ADR-0010）")
CAVEATS = [
    FLOOD_OBS_NOTE,
    "CSI/POD/FAR 对灾前常水掩膜计算：模型未编码常水（河/塘/海已剔海面但河塘仍在），FAR 系统性偏高。",
    "S1 固定阈值检出为下限（风浪/入射角/植被漏提，G17）；09-17 检出低于 09-05 常水。",
    "退水方向同向仅是趋势一致性参照，不是验证通过门；质量门 PASS ≠ 预测正确（ADR-0006）。",
]


def csi_pod_far(model_wet: np.ndarray, obs_wet: np.ndarray,
                valid: np.ndarray) -> dict:
    """像元级 CSI/POD/FAR（只在 valid 足迹内比对；足迹外不计虚警）。"""
    m = np.asarray(model_wet, bool) & np.asarray(valid, bool)
    o = np.asarray(obs_wet, bool) & np.asarray(valid, bool)
    h = int((m & o).sum())
    fa = int((m & ~o).sum())
    ms = int((~m & o).sum())
    csi = h / (h + fa + ms) if (h + fa + ms) else None
    pod = h / (h + ms) if (h + ms) else None
    far = fa / (h + fa) if (h + fa) else None
    return {"hits": h, "false_alarm_cells": fa, "misses": ms,
            "csi": None if csi is None else round(csi, 4),
            "pod": None if pod is None else round(pod, 4),
            "far": None if far is None else round(far, 4)}


def s1_fraction(tif: Path, shape: tuple[int, int],
                transform: rasterio.Affine) -> tuple[np.ndarray, np.ndarray]:
    """S1 20 m 水体聚合到模型网格 → (水体占比, 覆盖占比)（average，nodata=255 剔除）。"""
    frac = np.zeros(shape, dtype="float32")
    cover = np.zeros(shape, dtype="float32")
    with rasterio.open(tif) as ds:
        reproject((ds.read(1) == 1).astype("float32"), frac,
                  src_transform=ds.transform, src_crs=ds.crs,
                  dst_transform=transform, dst_crs="EPSG:32649",
                  resampling=Resampling.average, src_nodata=255.0)
        reproject((ds.read(1) != 255).astype("float32"), cover,
                  src_transform=ds.transform, src_crs=ds.crs,
                  dst_transform=transform, dst_crs="EPSG:32649",
                  resampling=Resampling.average, src_nodata=0.0)
    return frac, cover


def same_direction(model_delta: float | None, s1_delta: float | None) -> bool | None:
    """退水方向一致性：两侧方向均可判定且相同 → True。"""
    dm, ds1 = direction_of(model_delta), direction_of(s1_delta)
    if dm is None or ds1 is None:
        return None
    return dm == ds1


def grid_transform_and_shape() -> tuple[tuple[int, int], rasterio.Affine, float]:
    """模型网格（R0 res.max 头优先，回退 SGC 掩膜头）→ (shape, transform, cellsize)。"""
    ref = EVENT_DIR / RUN_NAMES["r0"] / "results" / "res.max"
    path = ref if ref.is_file() else PROJ / "data/interim/jiulong/channel/sgc_width.asc"
    hdr = read_ascii_header(path)
    nrows, ncols = int(hdr["nrows"]), int(hdr["ncols"])
    cell = float(hdr["cellsize"])
    transform = rasterio.Affine(cell, 0, hdr["xllcorner"], 0, -cell,
                                hdr["yllcorner"] + nrows * cell)
    return (nrows, ncols), transform, cell


def run_validation(key: str, obs_pre: np.ndarray, valid: np.ndarray,
                   cell_area_m2: float) -> dict:
    """单 run：res.max 湿格 vs 灾前常水 CSI/POD/FAR + 退水方向对照。"""
    run_dir = EVENT_DIR / RUN_NAMES[key]
    rec: dict = {"run": key, "name": RUN_NAMES[key], "run_dir": rel_posix(run_dir), "status": "ok"}
    if not run_dir.is_dir() or not (run_dir / "results" / "res.max").is_file():
        rec["status"] = "missing"
        return rec
    grid = read_ascii_grid(run_dir / "results" / "res.max")
    depth = np.asarray(grid.data, dtype=np.float64)
    try:
        sea = load_sea_mask(run_dir, grid.shape)
    except GridError as exc:
        rec["sea_mask_error"] = str(exc)
        sea = None
    wet = depth > WET_THRESHOLD_M
    if sea is not None:
        wet &= ~sea
    rec["model"] = {"wet_area_km2_res_max": round(grid.area_km2(wet), 4),
                    "wet_cells": int(wet.sum())}
    rec["vs_s1_prevent_constwater"] = csi_pod_far(wet, obs_pre, valid)

    frames = None
    verify_path = run_dir / "verify.json"
    if verify_path.is_file():
        try:
            data = json.loads(verify_path.read_text(encoding="utf-8"))
            frames = data.get("frame_series")
        except json.JSONDecodeError:
            pass
    if frames is None:
        try:
            frames = wet_metrics(run_dir, cell_area_m2)["frame_series"]
        except (GridError, OSError):
            frames = None
    tail = recession_tail_delta_km2(frames)
    rec["recession"] = {"n_frames": len(frames) if frames else 0,
                        "tail_delta_km2": None if tail is None else round(tail, 4),
                        "direction": direction_of(tail)}
    return rec


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__)
    add_common_args(parser)
    args = parser.parse_args(argv)
    log = AuditLog("validate_jiulong_inundation", run_tag=args.run_tag, dry_run=args.dry_run)
    log.param(runs=RUN_NAMES, s1_frac_thr=S1_FRAC_THR)
    log.add_input("s1_pre", S1_PRE)
    log.add_input("s1_post", S1_POST)
    for tif in (S1_PRE, S1_POST):
        if not tif.is_file():
            log.error(f"S1 水体栅格缺失：{tif}")
            print(f"FAIL: S1 水体栅格缺失：{tif}")
            return 2

    shape, transform, cell = grid_transform_and_shape()
    cell_area_m2 = cell * cell

    frac_pre, cover_pre = s1_fraction(S1_PRE, shape, transform)
    frac_post, cover_post = s1_fraction(S1_POST, shape, transform)
    # 海面：以 r0 的 sea_mask 为准（缺失则不剔，随 run 各自剔除模型侧）
    try:
        sea_ref = load_sea_mask(EVENT_DIR / RUN_NAMES["r0"], shape)
    except GridError:
        sea_ref = None
    no_sea = np.zeros(shape, dtype=bool) if sea_ref is None else ~sea_ref
    valid_pre = (cover_pre > S1_COVER_THR) & no_sea
    valid_post = (cover_post > S1_COVER_THR) & no_sea
    obs_pre = (frac_pre > S1_FRAC_THR) & valid_pre
    obs_post = (frac_post > S1_FRAC_THR) & valid_post
    area = lambda m: round(float(m.sum()) * cell_area_m2 / 1e6, 4)  # noqa: E731
    s1_pre_km2, s1_post_km2 = area(obs_pre), area(obs_post)
    s1_delta = s1_post_km2 - s1_pre_km2
    log.step("s1_masks", pre_km2=s1_pre_km2, post_km2=s1_post_km2,
             valid_frac_pre=round(float(valid_pre.mean()), 3))

    runs = {key: run_validation(key, obs_pre, valid_pre, cell_area_m2) for key in RUN_NAMES}
    for key, rec in runs.items():
        if rec["status"] == "ok":
            rec["recession"]["s1_constwater_delta_km2"] = round(s1_delta, 4)
            rec["recession"]["s1_direction"] = direction_of(s1_delta)
            rec["recession"]["direction_match"] = same_direction(
                rec["recession"]["tail_delta_km2"], s1_delta)
            rec["recession"]["pass_same_direction"] = bool(rec["recession"]["direction_match"])
        else:
            log.warn(f"{key} 缺失（status={rec['status']}），跳过")
    log.step("validated", available=[k for k, v in runs.items() if v["status"] == "ok"])

    payload = {
        "script": "scripts/stage6/validate_jiulong_inundation.py",
        "generated_utc": utc_now(),
        "purpose": "久隆淹没 S1 遥感验证（灾前常水 CSI/POD/FAR + 退水方向趋势对照）",
        "evidence_level": "E2/WEAK（S1 两景趋势参照；G17 下限；非独立验证通过门）",
        "not_a_gate": True,
        "flood_obs_available": False,
        "flood_obs_note": FLOOD_OBS_NOTE,
        "grid": {"shape": list(shape), "cellsize_m": cell,
                 "transform_source": rel_posix(EVENT_DIR / RUN_NAMES["r0"] / "results" / "res.max")},
        "method": {
            "reprojection": "rasterio reproject + Resampling.average，src_nodata=255 → 水体占比",
            "obs_constwater": f"占比 > {S1_FRAC_THR} 且 cover > {S1_COVER_THR} 且非海",
            "model_wet": f"res.max depth > {WET_THRESHOLD_M} m，剔除海面",
            "recession": "模型 frame_series 末 3 帧湿面积差符号 vs S1 常水面积变化方向；同向 = PASS",
        },
        "s1_scenes": {
            "pre": {"path": rel_posix(S1_PRE), "sha256": sha256_file(S1_PRE),
                    "scene_utc": "2024-09-05T10:50:22", "role": "灾前常水（CSI/POD/FAR 观测侧）",
                    "constwater_km2": s1_pre_km2},
            "post": {"path": rel_posix(S1_POST), "sha256": sha256_file(S1_POST),
                     "scene_utc": "2024-09-17T10:50:23", "role": "退水后（趋势参照）",
                     "constwater_km2": s1_post_km2},
            "constwater_delta_km2": round(s1_delta, 4),
            "direction": direction_of(s1_delta),
        },
        "baseline_reference": BASELINE_S1_REFERENCE,
        "runs": runs,
        "caveats": CAVEATS,
    }

    out_dir = resolve_out_dir(args.out_dir, args.run_tag, default=DEFAULT_OUT_DIR)
    out_json = out_dir / "s1_validation.json"
    if not args.dry_run:
        try:
            guard_report_target(out_json)
        except FileExistsError as exc:
            log.error(str(exc))
            print(f"FAIL: {exc}")
            return 2
        write_json(out_json, payload)
        log.step("written", outputs=[rel_or_name(out_json)])
        print(f"out: {out_json}")
    else:
        print("dry-run: 未写验证 JSON")
    print(f"S1 常水 09-05 {s1_pre_km2} km² → 09-17 {s1_post_km2} km²（Δ {round(s1_delta, 4)}，"
          f"{direction_of(s1_delta)}）")
    for key, rec in runs.items():
        m = rec.get("vs_s1_prevent_constwater", {})
        print(f"{key:<4} status={rec['status']} CSI={m.get('csi')} POD={m.get('pod')} "
              f"FAR={m.get('far')} 退水方向={rec.get('recession', {}).get('direction')} "
              f"同向={rec.get('recession', {}).get('direction_match')}")

    tag = f".{args.run_tag}" if args.run_tag else ""
    log_path = args.log_json or (DEFAULT_LOG.parent / f"{DEFAULT_LOG.stem}{tag}.json")
    log.write(log_path)
    print(f"log: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
