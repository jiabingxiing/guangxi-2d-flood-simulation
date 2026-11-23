"""久隆 R0–R4 双驱动对照实验编排器（计划 任务5 / §8.1 / ADR-0019~0022）。

实验矩阵（冻结同一 2024-09 历史 5 d 窗、200 m 生产基线 DEM、fpfric 0.035、dt 5 s）：
- R0 = 既有基线 scen_free_200m_sv6h（只读引用，不新建 run）。
- R1 净雨无来水：ERA5 锚点 GA 离线净雨（A2），N 缘闭合（无 QVAR）、无 SGC。
- R2a 原地形无刻槽 + 纯河道来水（护栏 1 必跑对照）：rain off，QVAR，无 SGC。
- R2 河道来水 + SGC 亚网格河道：rain off，QVAR，SGC（先过 3 项小算例核验）。
- R3 净雨 + 河道来水复合（主情景）：净雨 + QVAR + SGC。
- R4 = R3 + 感潮 HVAR（合成天文潮包络，沿用 ztide 序列，零相位口径）。

边界口径（计划 §七）：N 缘 QVAR 段（钦江入域断面 cell，内缩 3 格宽）；
S 缘 FREE 0.0005；E/W 缘闭合（分水岭），消除基线四边 FREE 假排水。
防重复计量硬锁：rain=net 时 par 严禁含 infiltration/fc 键（组装断言）。

不覆盖：prepare_run_dir 先查 results/res.mass；同名 run 存在即报错换 RUN_TAG。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from argparse import ArgumentParser
from pathlib import Path

import numpy as np

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import (  # noqa: E402
    AuditLog,
    add_common_args,
    read_ascii_grid,
    sha256_file,
    utc_now,
    write_json,
)
from scripts.common.run_manifest import prepare_run_dir, write_run_manifest_best_effort  # noqa: E402

EXE = PROJ / "models/lisflood-fp-8.1/LISFLOOD-FP-trunk/lisflood.exe"
BASELINE = PROJ / "outputs/jiulong/2024-09-yagi/scen_free_200m_sv6h"
TIDE_BDY = PROJ / "outputs/jiulong/2024-09-yagi/scen_ztide_200m/tide.bdy"
NET_RAIN = PROJ / "data/interim/jiulong/rainfall/rain_net_ga_5d.rain"
INFLOW_BDY = PROJ / "data/interim/jiulong/hydrology/qinjiang_event_5d.bdy"
SGC_WIDTH = PROJ / "data/interim/jiulong/channel/sgc_width.asc"
SGC_BANK = PROJ / "data/interim/jiulong/channel/sgc_bank.asc"
HYDROGRAPHY = PROJ / "data/catalog/jiulong_hydrography.json"
SUMMARY = PROJ / "data/catalog/jiulong_dual_forcing_summary.json"
EVENT_DIR = PROJ / "outputs/jiulong/2024-09-yagi"

SIM_SECONDS = 432000
DT = 5.0
SAVEINT = 21600
FPFRIC = 0.035
WET_THRESHOLD_M = 0.05
EXE_TIMEOUT_S = 4 * 3600

EXPERIMENTS: dict[str, dict] = {
    "r1": {"rain": "net", "inflow": False, "sgc": False, "tide": False,
           "name": "scen_r1_netrain_200m_sv6h_v3",
           "desc": "A2 净雨（ERA5+GA 离线降级，35d 暖启动 + 饱和深渗 Ks）无来水；v1 因 rain 文件首行缺换行作废"},
    "r2a": {"rain": None, "inflow": True, "sgc": False, "tide": False,
            "name": "scen_r2a_inflow_nocut_200m_sv6h_v4",
            "desc": "原地形无刻槽 + 纯河道来水对照（护栏 1）；v4 = QVAR 单格段口径（v1-v3 作废）"},
    "r2": {"rain": None, "inflow": True, "sgc": True, "tide": False,
           "name": "scen_r2_inflow_sgc_200m_sv6h",
           "desc": "河道来水 + SGC 亚网格河道"},
    "r3": {"rain": "net", "inflow": True, "sgc": True, "tide": False,
           "name": "scen_r3_dual_sgc_200m_sv6h",
           "desc": "净雨 + 河道来水复合主情景"},
    "r4": {"rain": "net", "inflow": True, "sgc": True, "tide": True,
           "name": "scen_r4_dual_sgc_tide_200m_sv6h",
           "desc": "R3 + 感潮 HVAR（合成潮包络零相位口径）"},
    "r3rec": {"rain": "net13d", "inflow": True, "sgc": True, "tide": False,
              "sim_seconds": 1123200,
              "rain_file": "rain_net_ga_13d.rain",
              "inflow_file": "qinjiang_recession_13d.bdy",
              "name": "scen_r3_dual_sgc_rec13d_200m_sv6h",
              "desc": "R3 双驱动 13 d 退水窗（M6 退水方向验证，09-05..09-17）"},
}


def qvar_x_range(entry_col: int, xll: float, ncols: int, cellsize: float) -> tuple[float, float]:
    """入域断面 QVAR 段 = **单格**（col..col+1）。

    LISFLOOD-FP 口径（源码 boundary.cpp/sgc.cpp）：每条边界边按 值×dx 计流量，
    n 格段会把注入量放大 n 倍；bdy 文件值已按 Q/cellsize 生成 → 段宽必须 1 格
    （2026-11-22 stage6 审计修正，v3 及以前 3 格段作废）。
    """
    col = min(max(entry_col, 0), ncols - 1)
    return xll + col * cellsize, xll + (col + 1) * cellsize


def assemble_par(exp: dict, has_bdy: bool) -> str:
    sim_seconds = float(exp.get("sim_seconds", SIM_SECONDS))
    lines = ["DEMfile\tdem.asc", "bcifile\tbci.bci"]
    if has_bdy:
        lines.append("bdyfile\tinflow.bdy")
    if exp["rain"]:
        lines.append("rainfall\train.rain")
    lines += [
        "resroot\tres",
        "dirroot\tresults",
        f"sim_time\t{sim_seconds:.0f}",
        f"initial_tstep\t{DT}",
        "massint\t3600",
        f"saveint\t{SAVEINT:.0f}",
        f"fpfric\t{FPFRIC}",
        "acceleration",
    ]
    if exp["sgc"]:
        lines += [
            "SGC_enable",
            "SGCwidth\tsgc_width.asc",
            "SGCbank\tsgc_bank.asc",
            "SGCchan\t1",
            "SGCn\t0.03",
        ]
    return "\n".join(lines) + "\n"


def parse_res_mass(path: Path) -> dict:
    """res.mass → E_mass 口径与 g15 一致（trapezoid ∫Qin/∫Qout）。

    实测格式（2026-11-22 核对）：空白分隔，首行列名
    Time Tstep MinTstep NumTsteps Area Vol Qin Hds Qout Qerror Verror Rain-(Inf+Evap)；
    Vol 为累计体积，Rain-(Inf+Evap) 为累计净雨体积，V0/Ve 取首/末行 Vol。
    """
    lines = [ln for ln in path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    cols = lines[0].split()
    rows: list[list[float]] = []
    for ln in lines[1:]:
        try:
            rows.append([float(v) for v in ln.split()])
        except ValueError:
            continue
    if not rows:
        raise RuntimeError(f"res.mass 无数据行：{path}")
    ncol = min(len(cols), min(len(r) for r in rows))
    arr = np.asarray([r[:ncol] for r in rows], dtype=float)
    idx = {name: i for i, name in enumerate(cols[:ncol])}
    t = arr[:, idx["Time"]]
    vol = arr[:, idx["Vol"]]
    qin = arr[:, idx["Qin"]] if "Qin" in idx else np.zeros(len(t))
    qout = arr[:, idx["Qout"]] if "Qout" in idx else np.zeros(len(t))
    vrain = arr[:, idx["Rain-(Inf+Evap)"]][-1] if "Rain-(Inf+Evap)" in idx else 0.0
    dt = np.diff(t)
    int_qin = float(np.sum((qin[1:] + qin[:-1]) / 2 * dt))
    int_qout = float(np.sum((qout[1:] + qout[:-1]) / 2 * dt))
    v0 = float(vol[0])
    ve = float(vol[-1])
    denom = max(v0 + vrain + int_qin, 1.0)
    e_mass = abs(ve - v0 - vrain - (int_qin - int_qout)) / denom
    return {
        "v0_m3": v0,
        "v_end_m3": ve,
        "v_rain_m3": float(vrain),
        "v_in_integrated_m3": int_qin,
        "v_out_integrated_m3": int_qout,
        "e_mass": e_mass,
        "n_rows": int(arr.shape[0]),
        "columns": cols,
    }


def wet_metrics(run_dir: Path, cell_area_m2: float) -> dict:
    """res.max 湿面积 + 逐帧湿面积趋势（退水方向）。"""
    sea = np.load(run_dir / "sea_mask.npy") if (run_dir / "sea_mask.npy").exists() else None
    grid = read_ascii_grid(run_dir / "results" / "res.max")
    depth = grid.data.astype(np.float64)
    wet = depth > WET_THRESHOLD_M
    if sea is not None and sea.shape == wet.shape:
        wet &= ~sea.astype(bool)
    wet_area_km2 = float(wet.sum() * cell_area_m2 / 1e6)
    frames = []
    results = run_dir / "results"
    for f in sorted(results.glob("res-*.wd")):
        m = re.search(r"res-(\d+)\.wd", f.name)
        if not m:
            continue
        g = read_ascii_grid(f)
        w = g.data > WET_THRESHOLD_M
        if sea is not None and sea.shape == w.shape:
            w &= ~sea.astype(bool)
        frames.append({"seq": int(m.group(1)), "wet_area_km2": float(w.sum() * cell_area_m2 / 1e6)})
    tail_slope = None
    if len(frames) >= 3:
        tail = [fr["wet_area_km2"] for fr in frames[-3:]]
        tail_slope = tail[-1] - tail[0]
    return {
        "wet_area_km2_res_max": wet_area_km2,
        "n_frames": len(frames),
        "frame_series": frames,
        "recession_tail_delta_km2": tail_slope,
    }


CHUNK_BYTES = 32768  # 分块回拷（目标盘大写限流口径，2026-11-22 实测）


def chunked_copy_dir(src: Path, dst: Path) -> dict:
    """整树按 ≤32 KB 分块写入 dst（规避目标盘大写限流）；dst 若有残留文件先清理。"""
    dst.mkdir(parents=True, exist_ok=True)
    for f in list(dst.rglob("*")):
        if f.is_file() and f.name != "res.mass":
            f.unlink()
    total_files = 0
    total_bytes = 0
    for f in sorted(src.rglob("*")):
        rel = f.relative_to(src)
        target = dst / rel
        if f.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        data = f.read_bytes()
        with open(target, "wb") as fh:
            for i in range(0, len(data), CHUNK_BYTES):
                fh.write(data[i : i + CHUNK_BYTES])
        total_files += 1
        total_bytes += len(data)
    return {"files": total_files, "bytes": total_bytes}


def run_experiment(key: str, exp: dict, log: AuditLog, dry_run: bool,
                   work_root: Path | None = None, copy_back: bool = False) -> dict:
    grid = read_ascii_grid(BASELINE / "dem.asc")
    xll = grid.header["xllcorner"]
    hydro = json.loads(HYDROGRAPHY.read_text(encoding="utf-8"))
    entry_col = int(hydro["entry_cell"][1])
    base_dir = work_root if work_root is not None else EVENT_DIR
    run_dir = base_dir / exp["name"]
    final_dir = EVENT_DIR / exp["name"]
    if not dry_run:
        prepare_run_dir(run_dir)  # 已有 res.mass 即拒绝
        if copy_back:
            prepare_run_dir(final_dir)  # 回拷目标同样不得已有 res.mass
    run_dir.mkdir(parents=True, exist_ok=True)

    # 输入拷贝（字节级，SHA-256 入审计）
    (run_dir / "dem.asc").write_bytes((BASELINE / "dem.asc").read_bytes())
    src_sea = BASELINE / "sea_mask.npy"
    if src_sea.exists():
        (run_dir / "sea_mask.npy").write_bytes(src_sea.read_bytes())
    files_copied = {"dem.asc": sha256_file(BASELINE / "dem.asc")}
    rain_path = Path(NET_RAIN).parent / exp.get("rain_file", Path(NET_RAIN).name) if exp["rain"] else None
    inflow_path = Path(INFLOW_BDY).parent / exp.get("inflow_file", Path(INFLOW_BDY).name) if exp["inflow"] else None
    if exp["rain"]:
        (run_dir / "rain.rain").write_bytes(rain_path.read_bytes())
        files_copied["rain.rain"] = sha256_file(rain_path)
    if exp["inflow"]:
        (run_dir / "inflow.bdy").write_bytes(inflow_path.read_bytes())
        files_copied["inflow.bdy"] = sha256_file(inflow_path)
    if exp["sgc"]:
        (run_dir / "sgc_width.asc").write_bytes(Path(SGC_WIDTH).read_bytes())
        (run_dir / "sgc_bank.asc").write_bytes(Path(SGC_BANK).read_bytes())
        files_copied["sgc_width.asc"] = sha256_file(SGC_WIDTH)
    if exp["tide"]:
        (run_dir / "tide.bdy").write_bytes(TIDE_BDY.read_bytes())
        files_copied["tide.bdy"] = sha256_file(TIDE_BDY)

    # bci：N 缘 QVAR 段或闭合；S 缘 HVAR（潮）或 FREE；E/W 闭合
    lines = []
    if exp["inflow"]:
        x0, x1 = qvar_x_range(entry_col, xll, grid.shape[1], grid.cell_size_m)
        lines.append(f"N\t{x0:.1f}\t{x1:.1f}\tQVAR\tqinjiang")
    if exp["tide"]:
        lines.append("S\t-99999999\t99999999\tHVAR\tqztide")
    else:
        lines.append("S\t-99999999\t99999999\tFREE 0.0005")
    (run_dir / "bci.bci").write_text("\n".join(lines) + "\n", encoding="ascii")

    par = assemble_par(exp, has_bdy=exp["inflow"])
    if exp["rain"]:
        assert "infiltration" not in par.lower(), "防重复计量硬锁：净雨 run.par 不得含 infiltration"
    (run_dir / "run.par").write_text(par, encoding="ascii")
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "experiment": key,
                "desc": exp["desc"],
                "boundary_scheme": "N=QVAR/CLOSED, S=FREE(0.0005)|HVAR, E/W=CLOSED",
                "rain": exp["rain"],
                "inflow": exp["inflow"],
                "sgc": exp["sgc"],
                "tide": exp["tide"],
                "generated_utc": utc_now(),
                "plan": "plans/code_artifact.md §8.1 R0-R4",
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    log.add_input("dem.asc", BASELINE / "dem.asc")
    if exp["rain"]:
        log.add_input("rain.rain", rain_path)
    if exp["inflow"]:
        log.add_input("inflow.bdy", inflow_path)
    if exp["sgc"]:
        log.add_input("sgc_width.asc", SGC_WIDTH)
    if exp["tide"]:
        log.add_input("tide.bdy", TIDE_BDY)

    if dry_run:
        return {"experiment": key, "run_dir": str(run_dir), "status": "dry_run"}

    proc = subprocess.Popen(
        [str(EXE), "run.par"], cwd=run_dir,
        stdout=open(run_dir / "run.log", "w", encoding="utf-8", errors="replace"),
        stderr=subprocess.STDOUT,
    )
    proc.wait(timeout=EXE_TIMEOUT_S)
    log.step("exe", experiment=key, returncode=proc.returncode)
    res_mass = run_dir / "results" / "res.mass"
    if not res_mass.exists():
        return {"experiment": key, "run_dir": str(run_dir), "status": "no_res_mass",
                "returncode": proc.returncode,
                "log_tail": (run_dir / "run.log").read_text(encoding="utf-8", errors="replace")[-2000:]}

    mass = parse_res_mass(res_mass)
    wet = wet_metrics(run_dir, grid.cell_area_m2)
    gate = "PASS" if mass["e_mass"] <= 1e-3 else "FAIL"
    if copy_back:
        copied = chunked_copy_dir(run_dir, final_dir)
        log.step("copy_back", experiment=key, **copied)
    verify = {
        "experiment": key,
        "desc": exp["desc"],
        "generated_utc": utc_now(),
        "mass": mass,
        "gate_1e-3": gate,
        "wet": {k: v for k, v in wet.items() if k != "frame_series"},
        "frame_series": wet["frame_series"],
        "inputs_sha256": files_copied,
    }
    write_json(run_dir / "verify.json", verify)
    write_run_manifest_best_effort(run_dir, config_snapshot={"experiment": key, "desc": exp["desc"]}, model_version="LISFLOOD-FP 8.1.0")
    return {"experiment": key, "run_dir": str(run_dir), "status": "done",
            "e_mass": mass["e_mass"], "gate": gate,
            "wet_area_km2": wet["wet_area_km2_res_max"],
            "recession_tail_delta_km2": wet["recession_tail_delta_km2"]}


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__)
    add_common_args(parser)
    parser.add_argument("--experiments", default="r1,r2a,r2,r3,r4,r3rec",
                        help="逗号分隔：r1,r2a,r2,r3,r4,r3rec 或 all")
    parser.add_argument("--work-root", default=None,
                        help="run 目录根（默认 outputs/jiulong/2024-09-yagi）；目标盘大写限流时指向 D 盘临时目录")
    parser.add_argument("--copy-back", action="store_true",
                        help="成功后按 32 KB 分块把 run 目录回拷 EVENT_DIR")
    args = parser.parse_args(argv)
    work_root = Path(args.work_root) if args.work_root else None
    log = AuditLog("run_jiulong_dual_forcing", run_tag=args.run_tag, dry_run=args.dry_run)
    (PROJ / "logs" / "stage3").mkdir(parents=True, exist_ok=True)
    keys = list(EXPERIMENTS) if args.experiments == "all" else args.experiments.split(",")
    log.param(experiments=keys)
    summary = {}
    if SUMMARY.exists():
        summary = json.loads(SUMMARY.read_text(encoding="utf-8")).get("runs", {})
    for key in keys:
        if key not in EXPERIMENTS:
            log.error(f"未知实验 {key}（可选 {sorted(EXPERIMENTS)}）")
            return 2
        result = run_experiment(key, EXPERIMENTS[key], log, args.dry_run,
                                work_root=work_root, copy_back=args.copy_back)
        summary[key] = result
        print(f"{key}: {result.get('status')} e_mass={result.get('e_mass', 'NA')} wet={result.get('wet_area_km2', 'NA')} km2")
    if not args.dry_run:
        write_json(SUMMARY, {"generated_utc": utc_now(), "runs": summary,
                             "r0_reference": "outputs/jiulong/2024-09-yagi/scen_free_200m_sv6h"})
    tag = f".{args.run_tag}" if args.run_tag else ""
    log.write(PROJ / "logs" / "stage3" / f"run_jiulong_dual_forcing{tag}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
