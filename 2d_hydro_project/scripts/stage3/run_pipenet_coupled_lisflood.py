"""阶段3：三案例全域 2D 模型的 1D-2D PipeNetGen 管网排涝耦合**真实重跑**编排器。

用途
----
在隔离目录 `outputs/pipenet/<case>/run_coupled_*/` 下真正调用 `lisflood.exe`
（LISFLOOD-FP 8.1.0）重跑管网耦合情景，并与**同目录自产的基线算例**做受控差分。
不覆盖任何既有 `res.mass` 历史 run，不修改 `models/` 源码，不污染质量门 SSOT
（`outputs/pipenet` 不在 `g15_mass_recheck.py::CASE_ROOTS` 白名单内）。

两种算例
--------
- `role=baseline`：复制基准 run 的全部输入，**不加**管网源汇。用于与耦合算例
  共享同一 DEM / 降雨 / 时间轴 / 时步，唯一变量是管网耦合项。
- `role=coupled`：在同构输入上合并 `drainage_points.bci` 片段与 `coupled.bdy` 曲线。

宁明按用户决策（2026-11-25）改跑「基线 + 耦合」双算例，原因是原参照
`scen_q6000_n100_100m_choff_eclosed` 的 `meta.json` 自带 `diagnostic_only: true`
与 `must_not_be_used_as: 结果基线`，且其替代候选 `scen_q6000_n100_100m` 保留了
已被诊断为短路的东缘 FREE 出流。自产基线同时满足：对照关系自洽、不改历史 run 标注、
g15 计数不动。

用法
----
    $env:PYTHONUTF8 = "1"
    uv --directory 2d_hydro_project run python scripts/stage3/run_pipenet_coupled_lisflood.py --case jiulong --dry-run
    uv --directory 2d_hydro_project run python scripts/stage3/run_pipenet_coupled_lisflood.py --case all
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from hydro_refine.core.paths import rel_posix  # noqa: E402
from hydro_refine.coupling.drainage_kernel import (  # noqa: E402
    build_fullgrid_coupled_forcing,
)
from scripts.common.grid_io import (  # noqa: E402
    AuditLog,
    guard_no_overwrite,
    read_ascii_header,
    sha256_file,
    utc_now,
    write_json,
)
from scripts.common.run_manifest import (  # noqa: E402
    prepare_run_dir,
    write_run_manifest_best_effort,
)

LISFLOOD_EXE = PROJ / "models" / "lisflood-fp-8.1" / "LISFLOOD-FP-trunk" / "lisflood.exe"

# 三案例配置。`baseline_run` 是被复制的基准 run；`own_baseline` 决定是否在隔离目录
# 内另跑一个同构基线算例（宁明 = True，见模块 docstring）。
CASES: dict[str, dict[str, Any]] = {
    "jiulong": {
        "baseline_run": PROJ / "outputs/jiulong/2024-09-yagi/scen_r3_dual_sgc_200m_sv6h",
        "epsg": 32649,
        "exclude_mask": "sgc_width.asc",
        "unique_series_per_cell": True,
        "own_baseline": True,
        "note": "SGC 亚网格河道 + 钦江来水；点汇须剔除 sgc_width>0 河道像元",
    },
    "ningming": {
        "baseline_run": PROJ / "outputs/ningming/2026-08-zitan/scen_q6000_n100_100m_choff_eclosed",
        "epsg": 32648,
        "exclude_mask": None,
        "unique_series_per_cell": False,
        "own_baseline": True,
        "note": "原参照带 diagnostic_only 标注，按用户决策改跑自产基线 + 耦合双算例",
    },
    "guigang": {
        "baseline_run": PROJ / "outputs/guigang/2024-09-yagi/scen_yagi_q11500_100m_dt2p5_chhyd_d14",
        "epsg": 32649,
        "exclude_mask": None,
        "unique_series_per_cell": False,
        "own_baseline": True,
        "note": "水力约束河槽 D=14 m 双驱动；无 SGC；100 m 4032 城区格",
    },
}

# 需要复制的输入文件（存在才复制）。run.par / bci.bci / bdy 由本脚本重写。
COPY_FILES = (
    "dem.asc",
    "manning.map",
    "rain.rain",
    "sgc_width.asc",
    "sgc_bank.asc",
    "sea_mask.npy",
    "dem_edit_diff.tif",
    "dem_edit_mask.tif",
)


def _parse_bci_lines(path: Path) -> list[str]:
    """读基准 bci，剔除空行与注释。"""
    if not path.is_file():
        return []
    out = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = raw.rstrip()
        if not s.strip() or s.lstrip().startswith("#"):
            continue
        out.append(s)
    return out


def _parse_par(path: Path) -> list[tuple[str, str | None]]:
    """读 run.par，返回 [(key, value_or_None)]，保留开关行。"""
    rows: list[tuple[str, str | None]] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if len(parts) == 1:
            rows.append((parts[0], None))
        else:
            rows.append((parts[0], parts[1]))
    return rows


def _base_bdy_name(par_rows: list[tuple[str, str | None]]) -> str | None:
    for key, val in par_rows:
        if key.lower() == "bdyfile" and val:
            return val
    return None


def assemble_case(
    case: str,
    *,
    run_tag: str,
    dry_run: bool,
    safety_factor: float,
    n_capacity_bins: int,
    keep_history: bool = False,
) -> dict[str, Any]:
    """装配并（可选）运行一个案例的基线/耦合算例，返回审计字典。"""
    cfg = CASES[case]
    base_run: Path = cfg["baseline_run"]
    if not base_run.is_dir():
        raise FileNotFoundError(f"基准 run 不存在：{base_run}")
    par_rows = _parse_par(base_run / "run.par")
    if not par_rows:
        raise FileNotFoundError(f"{base_run}/run.par 缺失或为空")
    base_bdy_name = _base_bdy_name(par_rows)
    par_map = {k.lower(): v for k, v in par_rows}
    sim_time = float(par_map.get("sim_time") or 0.0)
    cellsize = float(read_ascii_header(base_run / "dem.asc")["cellsize"])

    case_root = PROJ / "outputs" / "pipenet" / case
    case_root.mkdir(parents=True, exist_ok=True)

    out: dict[str, Any] = {
        "case": case,
        "SYNTHETIC_DATA_NOT_REAL_ASSET": True,
        "physical_recalculation": True,
        "baseline_run": rel_posix(base_run),
        "run_tag": run_tag,
        "note": cfg["note"],
        "cellsize_m": cellsize,
        "sim_time_s": sim_time,
        "dry_run": bool(dry_run),
    }

    # ---- 1. 构建耦合边界（dry-run 也做，供集成测试校验闭合）----
    forcing = build_fullgrid_coupled_forcing(
        PROJ / "outputs" / "pipenet" / case / f"{case}_pipes.geojson",
        base_run,
        case_root / "lisflood_coupled_inputs",
        epsg=int(cfg["epsg"]),
        time_window_s=sim_time,
        safety_factor=safety_factor,
        n_capacity_bins=n_capacity_bins,
        unique_series_per_cell=bool(cfg["unique_series_per_cell"]),
        exclude_mask_asc=(base_run / cfg["exclude_mask"]) if cfg["exclude_mask"] else None,
    )
    if not forcing["mass_closure_ok"]:
        raise RuntimeError(
            f"{case} 耦合边界水量不闭合：rel_err={forcing['mass_closure_rel_error']}"
        )
    out["forcing"] = {k: v for k, v in forcing.items() if not k.startswith("series_")}

    # ---- 2. 装配两个算例目录 ----
    roles = [("baseline", False)]
    if not cfg["own_baseline"]:
        roles = [("coupled", True)]
    else:
        roles = [("baseline", False), ("coupled", True)]

    for role, coupled in roles:
        run_dir = case_root / f"run_{role}_{run_tag}"
        guard_no_overwrite(run_dir)
        prepare_run_dir(run_dir)
        out.setdefault("runs", {})[role] = {
            "run_dir": rel_posix(run_dir),
            "coupled": coupled,
            "exists_before": False,
        }

        # 复制输入
        copied = []
        for name in COPY_FILES:
            src = base_run / name
            if src.is_file():
                shutil.copy2(src, run_dir / name)
                copied.append(name)

        # 基准 .bdy 原样复制（保留上游来水曲线）
        if base_bdy_name:
            src_bdy = base_run / base_bdy_name
            if src_bdy.is_file():
                shutil.copy2(src_bdy, run_dir / base_bdy_name)
                copied.append(base_bdy_name)

        # 写 bci：基准行 + （耦合时）管网点源行
        bci_lines = _parse_bci_lines(base_run / "bci.bci")
        if coupled:
            coup_bci = Path(PROJ / forcing["bci_path"])
            bci_lines = bci_lines + _parse_bci_lines(coup_bci)
        (run_dir / "bci.bci").write_text(
            "\n".join(bci_lines) + "\n", encoding="ascii", newline="\n"
        )

        # 写 bdy：把管网曲线**追加**到基准 bdy（.par 只读单一 bdyfile）
        if coupled:
            coup_bdy = Path(PROJ / forcing["bdy_path"])
            if not base_bdy_name:
                raise RuntimeError(f"{case} 基准 run 无 bdyfile，无法合并管网曲线")
            merged = (base_run / base_bdy_name).read_text(encoding="utf-8", errors="replace")
            extra = coup_bdy.read_text(encoding="ascii", errors="replace")
            (run_dir / "coupled.bdy").write_text(
                merged.rstrip("\n") + "\n" + extra, encoding="ascii", newline="\n"
            )

        # 写 run.par：耦合算例切到 coupled.bdy，其余键与基准一致
        lines: list[str] = []
        for key, val in par_rows:
            if key.lower() == "bdyfile" and coupled:
                lines.append("bdyfile\tcoupled.bdy")
                continue
            lines.append(f"{key}\t{val}" if val is not None else key)
        (run_dir / "run.par").write_text(
            "\n".join(lines) + "\n", encoding="ascii", newline="\n"
        )

        # meta.json：隔离标记 + 非真实资产声明
        write_json(
            run_dir / "meta.json",
            {
                "run_id": f"run_{role}_{run_tag}",
                "case": case,
                "role": f"pipenet_{role}",
                "coupled": bool(coupled),
                "source_baseline_run": rel_posix(base_run),
                "SYNTHETIC_DATA_NOT_REAL_ASSET": True,
                "physical_recalculation": True,
                "isolated_from_g15_ssot": True,
                "g15_scan_root_excluded": "outputs/pipenet 不在 g15_mass_recheck.py::CASE_ROOTS 白名单",
                "generated_utc": utc_now(),
                "note": cfg["note"],
            },
        )
        out["runs"][role]["copied_files"] = copied

    # ---- 3. 运行求解器 ----
    if dry_run:
        out["executed"] = False
        return out

    if not LISFLOOD_EXE.is_file():
        raise FileNotFoundError(f"缺少 LISFLOOD-FP 可执行文件：{LISFLOOD_EXE}")

    for role, _coupled in roles:
        run_dir = case_root / f"run_{role}_{run_tag}"
        t0 = time.monotonic()
        started = utc_now()
        proc = subprocess.run(
            [str(LISFLOOD_EXE), "run.par"],
            cwd=run_dir,
            capture_output=True,
            text=True,
            timeout=7200,
        )
        (run_dir / "run.log").write_text(
            proc.stdout + proc.stderr, encoding="utf-8", errors="replace"
        )
        wall = time.monotonic() - t0
        rec = out["runs"][role]
        rec["exit_code"] = int(proc.returncode)
        rec["wall_s"] = round(wall, 1)
        if proc.returncode != 0:
            rec["error"] = f"lisflood 退出码 {proc.returncode}"
            out["executed"] = True
            out["failed"] = True
            return out

        mass_rec = compute_e_mass(run_dir)
        rec["mass"] = mass_rec
        rec["mass_gate"] = "PASS" if mass_rec["e_mass_132"] <= 1e-3 else "FAIL"
        write_run_manifest_best_effort(
            run_dir,
            config_snapshot=json.loads((run_dir / "meta.json").read_text(encoding="utf-8")),
            model_version="LISFLOOD-FP 8.1.0",
            mass_gate={"e_mass_132": mass_rec["e_mass_132"], "gate": rec["mass_gate"]},
            warnings=[] if rec["mass_gate"] == "PASS" else ["E_mass 超 1e-3"],
            started_utc=started,
        )
        if rec["mass_gate"] != "PASS":
            out["executed"] = True
            out["failed"] = True
            out["fail_reason"] = f"{role} E_mass={mass_rec['e_mass_132']} 超 1e-3"
            return out

    out["executed"] = True
    out["failed"] = False
    return out


def compute_e_mass(run_dir: Path) -> dict[str, Any]:
    """与 `g15_mass_recheck.py::e_mass` 完全同口径，保证跨脚本可比。"""
    mass_path = Path(run_dir) / "results" / "res.mass"
    if not mass_path.is_file():
        raise FileNotFoundError(f"{mass_path} 缺失，求解器未产出质量文件")
    lines = mass_path.read_text(encoding="utf-8", errors="replace").splitlines()
    keys = lines[0].split()
    rows = [dict(zip(keys, map(float, ln.split()))) for ln in lines[1:] if ln.split()]
    v_in = sum(
        0.5 * (a["Qin"] + b["Qin"]) * (b["Time"] - a["Time"])
        for a, b in zip(rows, rows[1:])
    )
    v_out = sum(
        0.5 * (a["Qout"] + b["Qout"]) * (b["Time"] - a["Time"])
        for a, b in zip(rows, rows[1:])
    )
    v0, ve = rows[0]["Vol"], rows[-1]["Vol"]
    v_rain = rows[-1]["Rain-(Inf+Evap)"]
    resid = ve - v0 - v_rain - (v_in - v_out)
    denom = max(v0 + v_rain + v_in, 1.0)
    return {
        "v0_m3": round(v0, 1),
        "v_end_m3": round(ve, 1),
        "v_rain_m3": round(v_rain, 1),
        "v_in_integrated_m3": round(v_in, 1),
        "v_out_integrated_m3": round(v_out, 1),
        "residual_m3": round(resid, 1),
        "e_mass_132": float(f"{abs(resid) / denom:.3e}"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--case", choices=[*CASES, "all"], default="jiulong")
    ap.add_argument("--run-tag", default=None, help="输出目录后缀；缺省按案例分辨率自动取")
    ap.add_argument("--dry-run", action="store_true", help="只装配输入，不启求解器")
    ap.add_argument("--safety-factor", type=float, default=0.85)
    ap.add_argument("--n-capacity-bins", type=int, default=10)
    ap.add_argument("--out-json", type=Path, default=None)
    args = ap.parse_args(argv)

    if args.run_tag is None:
        # 目录名带分辨率，便于区分 100 m / 200 m 算例
        args.run_tag = "c200m" if args.case == "jiulong" else "c100m"

    log = AuditLog("run_pipenet_coupled_lisflood", run_tag=args.run_tag, dry_run=args.dry_run)
    cases = list(CASES) if args.case == "all" else [args.case]
    results: dict[str, Any] = {}
    failed = False
    for case in cases:
        print(f"[{case}] 装配中 …", flush=True)
        try:
            rec = assemble_case(
                case,
                run_tag=args.run_tag,
                dry_run=args.dry_run,
                safety_factor=args.safety_factor,
                n_capacity_bins=args.n_capacity_bins,
            )
        except Exception as exc:  # 单案例失败不阻断其余案例的装配审计
            rec = {"case": case, "failed": True, "error": f"{type(exc).__name__}: {exc}"}
            failed = True
            log.error(f"{case}: {rec['error']}")
            print(f"[{case}] 失败：{rec['error']}")
        results[case] = rec
        if rec.get("failed"):
            failed = True
        if not args.dry_run and rec.get("runs"):
            for role, r in rec["runs"].items():
                mg = r.get("mass_gate", "未运行")
                em = (r.get("mass") or {}).get("e_mass_132", "-")
                print(f"[{case}] {role}: gate={mg} E_mass={em} wall={r.get('wall_s')}s")
        log.step("case_done", case=case, failed=bool(rec.get("failed")))

    payload = {
        "script": "scripts/stage3/run_pipenet_coupled_lisflood.py",
        "generated_utc": utc_now(),
        "run_tag": args.run_tag,
        "dry_run": bool(args.dry_run),
        "safety_factor": args.safety_factor,
        "n_capacity_bins": args.n_capacity_bins,
        "SYNTHETIC_DATA_NOT_REAL_ASSET": True,
        "isolated_from_g15_ssot": True,
        "cases": results,
    }
    out_json = args.out_json or (PROJ / "data" / "catalog" / "pipenet_coupled_rerun.json")
    if not args.dry_run:
        # 合并写入：单案例运行不得抹掉其它案例的记录（否则 --case guigang 会把
        # 已有的 jiulong/ningming 结果清空，catalog 变成"最后一次运行"的样子）。
        if out_json.is_file():
            try:
                prior = json.loads(out_json.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                prior = {}
            merged = dict(prior.get("cases") or {})
            merged.update(results)
            payload["cases"] = {c: merged[c] for c in sorted(merged)}
            payload["merged_from_prior"] = sorted(set(merged) - set(results))
        write_json(out_json, payload)
        print(f"wrote -> {rel_posix(out_json)}")
    else:
        print("dry-run: 未写 catalog JSON")

    log.write(PROJ / "logs" / "stage3" / f"run_pipenet_coupled_lisflood.{args.run_tag}.json")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())