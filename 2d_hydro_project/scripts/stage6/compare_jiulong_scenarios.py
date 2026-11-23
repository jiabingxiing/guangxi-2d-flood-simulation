"""久隆 R0–R4 双驱动对照因子差分与归因报表（只读诊断，不重跑）。

输入：R0 基线 `scen_free_200m_sv6h`（verify.json 旧 schema，缺键回退 res.mass/res.max
复算）、R1/R2a/R2/R3/R4（run_jiulong_dual_forcing 产出）、河道掩膜
`data/interim/jiulong/channel/sgc_width.asc`（width>0）。

口径：湿判据 depth>0.05 m 剔海面；斑块=8 邻域连通域计全部；河邻湿占比=湿格∧
河道 1 格膨胀（numpy 平移，无 scipy）；退水方向=frame_series 末 3 帧湿面积差符号，
S1 参照 09-10→09-17 常水 −5.84 km²（ADR-0010 诊断，root_cause_report.md §3.4）；
因子差分 r3−r1=来水、r3−r2=净雨、r2−r2a=河道(SGC)、r3−r0=总效应。

证据等级 E3；质量门 PASS ≠ 预测正确；非独立验证（ADR-0006/0010，S1 仅两景）。
产出 `diagnostics/jiulong/coupling/compare_report.json` + `.md`；审计
`logs/diagnostics/compare_jiulong_scenarios.json`。缺 run 跳过（status="missing"）；
已有报告拒绝覆盖，除非 --run-tag（tag 子目录）。

用法（先设 `$env:PYTHONUTF8="1"`）::

    uv --directory 2d_hydro_project run python scripts/stage6/compare_jiulong_scenarios.py --dry-run
"""
from __future__ import annotations

import json
import sys
from argparse import ArgumentParser
from pathlib import Path

import numpy as np

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import (  # noqa: E402
    AuditLog,
    GridError,
    add_common_args,
    load_sea_mask,
    read_ascii_grid,
    utc_now,
    write_json,
)
from scripts.common.patch_analysis import connected_components, patch_stats  # noqa: E402
from scripts.common.paths import rel_posix  # noqa: E402
from scripts.stage3.run_jiulong_dual_forcing import (  # noqa: E402
    EXPERIMENTS,
    parse_res_mass,
    wet_metrics,
)

EVENT_DIR = PROJ / "outputs/jiulong/2024-09-yagi"
RUN_NAMES = {
    "r0": "scen_free_200m_sv6h",
    "r1": "scen_r1_netrain_200m_sv6h_v3",  # v3 = 定版口径（v1/v2 作废）
    "r2a": "scen_r2a_inflow_nocut_200m_sv6h_v4",  # v4 = QVAR 单格段口径
    "r2": "scen_r2_inflow_sgc_200m_sv6h",
    "r3": "scen_r3_dual_sgc_200m_sv6h",
    "r4": "scen_r4_dual_sgc_tide_200m_sv6h",
}
RUN_DESC = {"r0": "既有基线（四边 FREE，毛雨，只读引用）", **{k: v["desc"] for k, v in EXPERIMENTS.items()}}
# r0 不在 EXPERIMENTS 里；其边界为 allfree、无 QVAR 来水（root_cause_report §3.3）
INFLOW_ON = {"r0": False, **{k: bool(v["inflow"]) for k, v in EXPERIMENTS.items()}}

CHANNEL_WIDTH_ASC = PROJ / "data/interim/jiulong/channel/sgc_width.asc"
ENTRY_CELL = (0, 156)  # 钦江入域格 [row, col]（data/catalog/jiulong_hydrography.json entry_cell）
WET_THRESHOLD_M = 0.05
DEFAULT_OUT_DIR = PROJ / "diagnostics/jiulong/coupling"
DEFAULT_LOG = PROJ / "logs/diagnostics/compare_jiulong_scenarios.json"

# S1 退水参照：常水面积 09-10→09-17 变化 −5.84 km²（ADR-0010 诊断，降级口径）
S1_RECESSION_REFERENCE_KM2 = -5.84

EVIDENCE_LEVEL = "E3（情景级模拟对照，非实测；质量门 PASS ≠ 预测正确）"
CAVEATS = [
    "本报表是情景间差分归因，不是独立验证；S1 仅灾前（09-05）与退水后（09-17）两景，无洪峰过境（ADR-0010）。",
    "r3−r1 同时引入来水与 SGC 河道（r1 无刻槽），纯河道效应用 r2−r2a 读数。",
    "R0 verify.json 为旧 schema，缺 mass/frame_series 时由 res.mass/res.max 按同口径复算并标注。",
    "退水方向只判符号（末 3 帧湿面积差），不判速率；与 S1 参照 −5.84 km² 同向仅作趋势一致性参照。",
]


def dilate1(mask: np.ndarray) -> np.ndarray:
    """8 邻域 1 格膨胀（numpy 平移实现，无 scipy）。"""
    m = np.asarray(mask, dtype=bool)
    out = m.copy()
    out[1:, :] |= m[:-1, :]
    out[:-1, :] |= m[1:, :]
    out[:, 1:] |= m[:, :-1]
    out[:, :-1] |= m[:, 1:]
    out[1:, 1:] |= m[:-1, :-1]
    out[1:, :-1] |= m[:-1, 1:]
    out[:-1, 1:] |= m[1:, :-1]
    out[:-1, :-1] |= m[1:, 1:]
    return out


def load_channel_mask(expected_shape: tuple[int, int] | None = None) -> np.ndarray:
    """SGC 河道掩膜（sgc_width.asc，width>0 = 河道格）。"""
    grid = read_ascii_grid(CHANNEL_WIDTH_ASC, expected_shape=expected_shape)
    return np.asarray(grid.data) > 0


def recession_tail_delta_km2(frame_series: list[dict] | None) -> float | None:
    """frame_series 末 3 帧湿面积差（末帧 − 窗口首帧）；不足 3 帧返回 None。"""
    if not frame_series or len(frame_series) < 3:
        return None
    tail = [fr["wet_area_km2"] for fr in frame_series[-3:]]
    return float(tail[-1] - tail[0])


def direction_of(delta_km2: float | None) -> str | None:
    """湿面积差符号 → 退水方向（receding=面积减小）。"""
    if delta_km2 is None:
        return None
    if delta_km2 < 0:
        return "receding"
    if delta_km2 > 0:
        return "expanding"
    return "flat"


def load_frame_masks(run_dir: Path, sea: np.ndarray | None) -> list[tuple[int, np.ndarray]]:
    """读全部 res-XXXX.wd → [(seq, wet_mask)]（wet=depth>0.05 且非海）。"""
    out = []
    for f in sorted((run_dir / "results").glob("res-*.wd")):
        if not f.stem[4:].isdigit():
            continue
        g = read_ascii_grid(f)
        w = np.asarray(g.data) > WET_THRESHOLD_M
        if sea is not None:
            w &= ~sea
        out.append((int(f.stem[4:]), w))
    out.sort(key=lambda item: item[0])
    return out


def run_metrics(run_key: str, cell_area_m2: float, channel_dil: np.ndarray | None) -> dict:
    """单 run 指标：verify.json 优先，缺键回退 res.mass/res.max 复算（同口径）。"""
    run_dir = EVENT_DIR / RUN_NAMES[run_key]
    rec: dict = {"run": run_key, "name": RUN_NAMES[run_key],
                 "run_dir": rel_posix(run_dir), "desc": RUN_DESC.get(run_key, ""),
                 "inflow_on": INFLOW_ON.get(run_key), "status": "ok"}
    if not run_dir.is_dir() or not (run_dir / "results" / "res.max").is_file():
        rec["status"] = "missing"
        return rec
    verify: dict = {}
    vpath = run_dir / "verify.json"
    if vpath.is_file():
        try:
            verify = json.loads(vpath.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            rec["status"] = "verify_json_unreadable"
    wet_block = verify.get("wet") or {}
    wet_area = wet_block.get("wet_area_km2_res_max", verify.get("wet_area_km2"))
    frames = verify.get("frame_series")
    mass = verify.get("mass")
    recomputed: list[str] = []

    grid = read_ascii_grid(run_dir / "results" / "res.max")
    depth = np.asarray(grid.data, dtype=np.float64)
    sea = load_sea_mask(run_dir, grid.shape)
    wet = depth > WET_THRESHOLD_M
    if sea is not None:
        wet &= ~sea
    if wet_area is None:
        wet_area = grid.area_km2(wet)
        recomputed.append("wet_area")
    if frames is None:
        try:
            frames = [{"seq": s, "wet_area_km2": round(float(m.sum()) * cell_area_m2 / 1e6, 4)}
                      for s, m in load_frame_masks(run_dir, sea)]
            recomputed.append("frame_series")
        except (GridError, OSError) as exc:
            frames = None
            rec["frame_series_error"] = str(exc)
    if mass is None and (run_dir / "results" / "res.mass").is_file():
        try:
            mass = parse_res_mass(run_dir / "results" / "res.mass")
            recomputed.append("mass")
        except (RuntimeError, OSError) as exc:
            rec["mass_error"] = str(exc)

    labels, n_patches = connected_components(wet, connectivity=8)
    patches = patch_stats(depth, labels, n_patches, cell_area_m2=cell_area_m2)
    tail = recession_tail_delta_km2(frames)
    rec.update({
        "verify_schema": "v2" if "mass" in verify else ("v1_legacy" if verify else "absent"),
        "recomputed_fields": recomputed,
        "wet_area_km2": round(float(wet_area), 4),
        "n_patches": int(n_patches),
        "largest_patch_km2": round(patches[0].area_km2, 4) if patches else 0.0,
        "n_frames": len(frames) if frames is not None else 0,
        "recession_tail_delta_km2": None if tail is None else round(tail, 4),
        "recession_direction": direction_of(tail),
        "river_adjacent_wet_frac": (
            round(float((wet & channel_dil).sum()) / max(int(wet.sum()), 1), 4)
            if channel_dil is not None else None
        ),
        "mass": None if mass is None else {
            k: mass[k] for k in ("v0_m3", "v_end_m3", "v_rain_m3",
                                 "v_in_integrated_m3", "v_out_integrated_m3", "e_mass")
        },
        "gate_1e-3": verify.get("gate_1e-3")
        or ("PASS" if mass and mass["e_mass"] <= 1e-3 else None),
    })
    return rec


def factor_diff(a: dict | None, b: dict | None) -> dict:
    """因子差分（a − b）：湿面积 / 斑块数 / ∫Qin；任一侧缺失记 None。

    指标取值：先读顶层键，回退 `mass` 子 dict（兼容扁平假 dict 与 run_metrics 输出）。
    """
    def metric(rec: dict | None, key: str) -> float | None:
        if rec is None:
            return None
        v = rec.get(key)
        if v is None:
            v = (rec.get("mass") or {}).get(key)
        return v

    def d(key: str) -> float | None:
        va, vb = metric(a, key), metric(b, key)
        if va is None or vb is None:
            return None
        return round(float(va) - float(vb), 4)

    return {
        "delta_wet_area_km2": d("wet_area_km2"),
        "delta_n_patches": d("n_patches"),
        "delta_v_in_integrated_m3": d("v_in_integrated_m3"),
    }


def build_factor_table(metrics: dict[str, dict]) -> list[dict]:
    """逐 run vs R0 + 四条归因对（缺 run 行保留、值记 None）。"""
    rows: list[dict] = []
    for key in ("r1", "r2a", "r2", "r3", "r4"):
        if key not in RUN_NAMES:
            continue
        row = {"pair": f"{key}_vs_r0", "factor": f"{key} 相对基线 R0",
               "available": metrics.get(key, {}).get("status") == "ok"
               and metrics.get("r0", {}).get("status") == "ok"}
        row.update(factor_diff(metrics.get(key), metrics.get("r0")))
        rows.append(row)
    for pair, factor in (
        ("r3_vs_r1", "inflow 效应（含 SGC；r1 无刻槽）"),
        ("r3_vs_r2", "rain 效应（净雨）"),
        ("r2_vs_r2a", "channel(SGC) 效应（纯河道刻槽）"),
        ("r3_vs_r0", "total（复合主情景 vs 基线）"),
    ):
        a_key, b_key = pair.split("_vs_")
        row = {"pair": pair, "factor": factor,
               "available": metrics.get(a_key, {}).get("status") == "ok"
               and metrics.get(b_key, {}).get("status") == "ok"}
        row.update(factor_diff(metrics.get(a_key), metrics.get(b_key)))
        rows.append(row)
    return rows


def write_markdown(payload: dict, path: Path) -> Path:
    """中文小结报表（表格 + 证据等级标注）。"""
    runs = payload["runs"]
    dash = "–"
    lines = [
        "# 久隆 R0–R4 双驱动对照因子差分与归因",
        "",
        f"生成：{payload['generated_utc']} ｜ 网格：{payload['grid']['ncols']}×{payload['grid']['nrows']} "
        f"@ {payload['grid']['cellsize_m']:.0f} m ｜ 湿判据：depth > {WET_THRESHOLD_M} m（剔除海面）",
        "",
        f"> **证据等级：{EVIDENCE_LEVEL}**。本表是情景差分归因，不是独立验证；",
        "> S1 仅灾前/退水两景、无洪峰过境（ADR-0006 / ADR-0010）。质量门 PASS ≠ 预测正确。",
        "",
        "## 各 run 指标",
        "",
        "| run | 说明 | 湿面积 km² | 斑块数 | ∫Qin m³ | e_mass | 门 | 河邻湿占比 | 退水方向（末3帧） |",
        "|---|---|---:|---:|---:|---:|---|---:|---|",
    ]
    for key in RUN_NAMES:
        r = runs.get(key, {"status": "missing"})
        if r.get("status") != "ok":
            lines.append(f"| {key} | {r.get('desc', '')} | {dash} | {dash} | {dash} | {dash} | "
                         f"{dash} | {dash} | status={r.get('status', 'missing')} |")
            continue
        m = r.get("mass") or {}
        e_mass = m.get("e_mass")
        lines.append(
            f"| {key} | {r['desc']} | {r['wet_area_km2']} | {r['n_patches']} | "
            f"{m.get('v_in_integrated_m3', dash)} | "
            f"{dash if e_mass is None else f'{e_mass:.2e}'} | {r.get('gate_1e-3') or dash} | "
            f"{r['river_adjacent_wet_frac'] if r['river_adjacent_wet_frac'] is not None else dash} | "
            f"{r['recession_direction'] or dash} |"
        )
    lines += ["", "## 因子差分（Δ = 前者 − 后者）", "",
              "| 对比 | 因子 | Δ湿面积 km² | Δ斑块数 | Δ∫Qin m³ |", "|---|---|---:|---:|---:|"]
    for row in payload["factor_table"]:
        fmt = lambda v: dash if v is None else v  # noqa: E731
        lines.append(f"| {row['pair']} | {row['factor']} | {fmt(row['delta_wet_area_km2'])} | "
                     f"{fmt(row['delta_n_patches'])} | {fmt(row['delta_v_in_integrated_m3'])} |")
    ref = payload["s1_recession_reference"]
    lines += [
        "",
        "## 退水方向对照",
        "",
        f"S1 参照（{ref['window']} 常水）：{ref['delta_km2']} km²（{ref['direction']}）；"
        f"来源：{ref['source']}。",
        "模型各 run 方向见上表「退水方向」列；同向仅作趋势一致性参照，不构成验证通过。",
        "",
        "## 注意事项",
        "",
        *[f"- {c}" for c in payload["caveats"]],
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def resolve_out_dir(out_dir: Path | None, run_tag: str | None, default: Path = DEFAULT_OUT_DIR) -> Path:
    """输出目录：--out-dir 优先；--run-tag 写 tag 子目录。"""
    base = Path(out_dir) if out_dir else default
    return base / run_tag if run_tag else base


def rel_or_name(path: Path) -> str:
    """项目相对 POSIX 路径；项目外路径只记文件名（审计清单禁宿主绝对路径）。"""
    try:
        return rel_posix(path)
    except Exception:
        return path.name


def guard_report_target(path: Path) -> None:
    """已有报告拒绝覆盖（终稿 §10 同源纪律，派生报表防误覆盖）。"""
    if path.exists():
        raise FileExistsError(f"{path} 已存在；如需另存请加 --run-tag（写入 tag 子目录）")


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__)
    add_common_args(parser)
    args = parser.parse_args(argv)
    log = AuditLog("compare_jiulong_scenarios", run_tag=args.run_tag, dry_run=args.dry_run)
    log.param(event_dir=rel_posix(EVENT_DIR), runs=RUN_NAMES)

    # 网格口径：R0 res.max 头优先，回退河道掩膜头（同 182×152 @200 m 网格）
    ref_dir = EVENT_DIR / RUN_NAMES["r0"]
    ref_max = ref_dir / "results" / "res.max"
    try:
        grid = read_ascii_grid(ref_max if ref_max.is_file() else CHANNEL_WIDTH_ASC)
    except (GridError, OSError) as exc:
        log.error(f"网格头不可读：{exc}")
        print(f"FAIL: 网格头不可读：{exc}")
        return 2
    cell_area = grid.cell_area_m2
    channel_dil: np.ndarray | None
    try:
        channel_dil = dilate1(load_channel_mask(expected_shape=grid.shape))
    except (GridError, OSError) as exc:
        channel_dil = None
        log.warn(f"河道掩膜不可用，河邻湿占比跳过：{exc}")

    metrics = {key: run_metrics(key, cell_area, channel_dil) for key in RUN_NAMES}
    for key, rec in metrics.items():
        if rec["status"] != "ok":
            log.warn(f"{key} 缺失或未完成（status={rec['status']}），跳过")
    factor_table = build_factor_table(metrics)
    log.step("metrics", available=[k for k, v in metrics.items() if v["status"] == "ok"])

    payload = {
        "script": "scripts/stage6/compare_jiulong_scenarios.py",
        "generated_utc": utc_now(),
        "purpose": "R0–R4 双驱动对照因子差分与归因（只读诊断）",
        "evidence_level": EVIDENCE_LEVEL,
        "not_independent_validation": True,
        "grid": {**grid.to_meta(), "cell_area_m2": cell_area,
                 "source": "r0 res.max 头" if ref_max.is_file() else "sgc_width.asc 头"},
        "method": {
            "wet_threshold_m": WET_THRESHOLD_M,
            "patches": "8 邻域连通域（patch_analysis.connected_components），计数全部",
            "river_adjacent": "湿格 ∧ dilate1(sgc_width>0) 占湿格比例（numpy 平移膨胀）",
            "recession": "frame_series 末 3 帧湿面积差符号（同 wet_metrics）",
            "mass_parse": "scripts/stage3/run_jiulong_dual_forcing.parse_res_mass（trapezoid ∫Qin/∫Qout）",
        },
        "s1_recession_reference": {
            "delta_km2": S1_RECESSION_REFERENCE_KM2,
            "window": "09-10→09-17",
            "direction": direction_of(S1_RECESSION_REFERENCE_KM2),
            "source": "ADR-0010 诊断（diagnostics/jiulong/root_cause_report.md §3.4 降级口径）",
        },
        "runs": metrics,
        "factor_table": factor_table,
        "caveats": CAVEATS,
    }
    log.payload["report"] = {"runs_available": [k for k, v in metrics.items() if v["status"] == "ok"]}

    out_dir = resolve_out_dir(args.out_dir, args.run_tag)
    out_json = out_dir / "compare_report.json"
    out_md = out_dir / "compare_report.md"
    if not args.dry_run:
        try:
            guard_report_target(out_json)
        except FileExistsError as exc:
            log.error(str(exc))
            print(f"FAIL: {exc}")
            return 2
    if not args.dry_run:
        write_json(out_json, payload)
        write_markdown(payload, out_md)
        log.step("written", outputs=[rel_or_name(out_json), rel_or_name(out_md)])
        print(f"out: {out_json}")
        print(f"out: {out_md}")
    else:
        print("dry-run: 未写报告文件")
    for row in factor_table:
        print(f"{row['pair']:<12} dWet={row['delta_wet_area_km2']} dPatch={row['delta_n_patches']} "
              f"dQin={row['delta_v_in_integrated_m3']}")

    tag = f".{args.run_tag}" if args.run_tag else ""
    log_path = args.log_json or (DEFAULT_LOG.parent / f"{DEFAULT_LOG.stem}{tag}.json")
    log.write(log_path)
    print(f"log: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
