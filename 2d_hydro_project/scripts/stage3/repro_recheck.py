"""终稿 13.4 可复现门：三案例冻结输入重跑比对（jiulong/ningming/guigang）。

对每案例的 <baseline> 与 <baseline>_repro 目录比较：
  - results/res.max 是否逐位一致（不一致时报最大绝对差，m）；
  - results/res.mass 末行是否一致；
  - E_mass（13.2 口径）双值。
判定：res.max 逐位一致 => PASS（bit-identical）；否则按最大差与湿面积相对差给
TOLERANCE_PASS / FAIL。结果并入手册引用的 data/catalog/reproducibility_test.json。

用法：uv run python scripts/stage3/repro_recheck.py
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

PROJ = Path(__file__).resolve().parents[2]
OUT = PROJ / "data" / "catalog" / "reproducibility_test.json"

PAIRS = {
    "jiulong": ("outputs/jiulong/2024-09-yagi/scen_free_200m",
                "outputs/jiulong/2024-09-yagi/scen_free_200m_repro"),
    "ningming": ("outputs/ningming/2026-08-zitan/scen_q6000_n100_100m",
                 "outputs/ningming/2026-08-zitan/scen_q6000_n100_100m_repro"),
    "guigang": ("outputs/guigang/2024-09-yagi/scen_yagi_q11500_200m",
                "outputs/guigang/2024-09-yagi/scen_yagi_q11500_200m_repro"),
}


def read_asc(path: Path) -> tuple[np.ndarray, dict]:
    with open(path) as f:
        hdr = {}
        for _ in range(6):
            k, *v = next(f).split()
            hdr[k.lower()] = float(v[0]) if v else None
        arr = np.loadtxt(f)
    return arr, hdr


def mass_rows(d: Path) -> list[str]:
    return (d / "results" / "res.mass").read_text().splitlines()


def e_mass(d: Path) -> float:
    lines = mass_rows(d)
    keys = lines[0].split()
    rows = [dict(zip(keys, map(float, ln.split()))) for ln in lines[1:] if ln.split()]
    v_in = sum(0.5 * (a["Qin"] + b["Qin"]) * (b["Time"] - a["Time"]) for a, b in zip(rows, rows[1:]))
    v_out = sum(0.5 * (a["Qout"] + b["Qout"]) * (b["Time"] - a["Time"]) for a, b in zip(rows, rows[1:]))
    resid = rows[-1]["Vol"] - rows[0]["Vol"] - rows[-1]["Rain-(Inf+Evap)"] - (v_in - v_out)
    return float(f"{abs(resid) / max(rows[0]['Vol'] + rows[-1]['Rain-(Inf+Evap)'] + v_in, 1.0):.3e}")


def wet_area_km2(arr: np.ndarray, sea_mask: np.ndarray | None, cell: float) -> float:
    wet = (arr > 0.05) & ~sea_mask if sea_mask is not None else arr > 0.05
    return round(float(wet.sum()) * cell * cell / 1e6, 2)


def compare(case: str, base: Path, repro: Path) -> dict:
    a, ha = read_asc(base / "results" / "res.max")
    b, _ = read_asc(repro / "results" / "res.max")
    sea_p = base / "sea_mask.npy"
    sea = np.load(sea_p) if sea_p.exists() else None
    bit = bool(np.array_equal(a, b))
    max_diff = float(np.abs(a - b).max())
    mass_identical = mass_rows(base)[-1] == mass_rows(repro)[-1]
    area_a, area_b = wet_area_km2(a, sea, ha["cellsize"]), wet_area_km2(b, sea, ha["cellsize"])
    area_rel = abs(area_b - area_a) / max(area_a, 1e-9)
    if bit:
        verdict = "PASS（bit-identical）"
    elif max_diff <= 1e-6 and area_rel <= 1e-3:
        verdict = "PASS（容差内）"
    else:
        verdict = "FAIL"
    return {"baseline": base.name, "repro": repro.name,
            "bit_identical_resmax": bit, "max_abs_diff_m": max_diff,
            "mass_last_row_identical": mass_identical,
            "wet_area_base_km2": area_a, "wet_area_repro_km2": area_b,
            "e_mass_base": e_mass(base), "e_mass_repro": e_mass(repro),
            "verdict": verdict}


def main() -> None:
    cases = {}
    for case, (bp, rp) in PAIRS.items():
        base, repro = PROJ / bp, PROJ / rp
        if not (base / "results" / "res.max").exists():
            cases[case] = {"error": f"缺基线 {base}"}
            continue
        if not (repro / "results" / "res.max").exists():
            cases[case] = {"error": f"缺重跑 {repro}"}
            continue
        cases[case] = compare(case, base, repro)
    payload = {"generated_utc": datetime.now(timezone.utc).isoformat(),
               "note": "终稿 13.4 可复现门：同冻结输入重跑比对；jiulong 条目口径沿用首版（逐位一致）",
               "cases": cases}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("->", OUT)


if __name__ == "__main__":
    main()
