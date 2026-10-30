"""为已有模型运行生成可追溯清单。

脚本只读取运行输入和输出，并在每个有效 run 目录写入 run_manifest.json。
它不修改模型输入、输出帧或验证 JSON。

默认跳过已有清单。使用 --force 更新清单。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))
MODEL_VERSION = "LISFLOOD-FP 8.1.0"
EXCLUDE_MARKERS = ("_v1invalid", "_v1_polyfitbed", "_interrupted")


def _mass_gate(run_dir: Path) -> dict:
    mass = run_dir / "results" / "res.mass"
    if not mass.is_file():
        return {"status": "missing", "reason": "res.mass 不存在"}
    lines = [line for line in mass.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    if len(lines) < 2:
        return {"status": "invalid", "reason": "res.mass 没有数据行"}
    keys = lines[0].split()
    rows = [dict(zip(keys, map(float, line.split()))) for line in lines[1:]]
    if len(rows) < 2:
        return {"status": "invalid", "reason": "res.mass 数据行不足"}
    qin = sum(0.5 * (a["Qin"] + b["Qin"]) * (b["Time"] - a["Time"])
              for a, b in zip(rows, rows[1:]))
    qout = sum(0.5 * (a["Qout"] + b["Qout"]) * (b["Time"] - a["Time"])
               for a, b in zip(rows, rows[1:]))
    rain = rows[-1]["Rain-(Inf+Evap)"]
    residual = rows[-1]["Vol"] - rows[0]["Vol"] - rain - (qin - qout)
    denominator = max(rows[0]["Vol"] + rain + qin, 1.0)
    e_mass = abs(residual) / denominator
    bci_path = run_dir / "bci.bci"
    bci = bci_path.read_text(encoding="utf-8", errors="replace").upper() if bci_path.is_file() else ""
    bc = "HVAR" if "HVAR" in bci else "HFIX" if "HFIX" in bci else "FREE" if "FREE" in bci else "UNKNOWN"
    if e_mass <= 1e-3:
        status = "PASS"
        reason = None
    elif bc in ("HFIX", "HVAR"):
        status = "FAIL-EXPLAINED"
        reason = "HFIX/HVAR 边界质量增益；不计入通过数"
    else:
        status = "FAIL"
        reason = "非 HFIX/HVAR 边界超过 1e-3"
    return {
        "status": status,
        "gate_1e-3": status,
        "e_mass": float(f"{e_mass:.3e}"),
        "boundary": bc,
        "reason": reason,
        "boundary_source": "bci.bci" if bci_path.is_file() else "missing",
    }


def _config_snapshot(run_dir: Path) -> dict:
    meta_path = run_dir / "meta.json"
    meta: dict = {}
    if meta_path.is_file():
        try:
            raw = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                meta = raw
        except (OSError, json.JSONDecodeError):
            meta = {"meta_read_error": str(meta_path)}
    par = run_dir / "run.par"
    return {
        "meta": meta,
        "run_par": par.read_text(encoding="utf-8", errors="replace") if par.is_file() else None,
    }


def build(force: bool = False) -> list[dict]:
    records = []
    for mass in sorted((PROJ / "outputs").glob("*/**/results/res.mass")):
        run_dir = mass.parents[1]
        relative = run_dir.relative_to(PROJ).as_posix()
        if any(marker in relative for marker in EXCLUDE_MARKERS):
            continue
        target = run_dir / "run_manifest.json"
        if target.exists() and not force:
            records.append({"run": relative, "status": "skipped_existing"})
            continue
        from scripts.common.run_manifest import write_run_manifest
        rec = write_run_manifest(
            run_dir,
            config_snapshot=_config_snapshot(run_dir),
            model_version=MODEL_VERSION,
            mass_gate=_mass_gate(run_dir),
            warnings=[
                "清单由 build_run_manifests.py 补建；模型运行时未自动写入。",
                "当前项目仍为公开数据降级情景结果。",
            ],
        )
        records.append({"run": relative, "status": "written", "run_id": rec["run_id"]})
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="更新已有 run_manifest.json")
    args = parser.parse_args()
    records = build(force=args.force)
    out = PROJ / "data/catalog/run_manifest_index.json"
    out.write_text(json.dumps({"records": records}, ensure_ascii=False, indent=2), encoding="utf-8")
    counts = {}
    for record in records:
        counts[record["status"]] = counts.get(record["status"], 0) + 1
    print(json.dumps({"counts": counts, "index": str(out)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
