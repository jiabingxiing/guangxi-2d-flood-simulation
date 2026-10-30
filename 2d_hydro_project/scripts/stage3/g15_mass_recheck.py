"""G15 复核：统一 E_mass 口径（终稿 13.2）——同时积分 Qin 与 Qout。

自动扫描三案例全部有效 run（排除 _v1invalid/_v1_polyfitbed），按边界类型给出：
  PASS            E_mass <= 1e-3
  FAIL-EXPLAINED  HFIX/HVAR 海/潮边界且 E_mass > 1e-3（G13 湿激波质量增益放大；
                  非门禁合格，只是解释）
  FAIL            其它超限
不再使用 N/A-HFIX；EXCLUDED 不计入通过数。

口径：E_mass = |V_end − V_0 − V_rain − (∫Qin dt − ∫Qout dt)| / max(V_0+V_rain+∫Qin dt, 1)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
OUT = PROJ / "data" / "catalog" / "mass_recheck_g15.json"
CASE_ROOTS = [PROJ / "outputs" / "ningming", PROJ / "outputs" / "jiulong", PROJ / "outputs" / "guigang"]
EXCLUDE_MARKERS = ("_v1invalid", "_v1_polyfitbed")


def e_mass(res_dir: Path) -> dict:
    lines = (res_dir / "results" / "res.mass").read_text().splitlines()
    keys = lines[0].split()
    rows = [dict(zip(keys, map(float, ln.split()))) for ln in lines[1:] if ln.split()]
    v_in = sum(0.5 * (a["Qin"] + b["Qin"]) * (b["Time"] - a["Time"])
               for a, b in zip(rows, rows[1:]))
    v_out = sum(0.5 * (a["Qout"] + b["Qout"]) * (b["Time"] - a["Time"])
                for a, b in zip(rows, rows[1:]))
    v0, ve = rows[0]["Vol"], rows[-1]["Vol"]
    v_rain = rows[-1]["Rain-(Inf+Evap)"]
    resid = ve - v0 - v_rain - (v_in - v_out)
    denom = max(v0 + v_rain + v_in, 1.0)
    return {
        "v0_m3": round(v0, 1), "v_end_m3": round(ve, 1),
        "v_rain_m3": round(v_rain, 1),
        "v_in_integrated_m3": round(v_in, 1), "v_out_integrated_m3": round(v_out, 1),
        "residual_m3": round(resid, 1),
        "residual_pct_of_in": round(resid / max(v_in, 1) * 100, 2),
        "e_mass_132": float(f"{abs(resid) / denom:.3e}"),
        "qerror_last": rows[-1]["Qerror"], "verror_last": rows[-1]["Verror"],
    }


def bc_kind(run_dir: Path) -> str:
    bci = run_dir / "bci.bci"
    if not bci.exists():
        return "UNKNOWN"
    txt = bci.read_text(encoding="utf-8", errors="replace").upper()
    if "HVAR" in txt:
        return "HVAR"
    if "HFIX" in txt:
        return "HFIX"
    if "FREE" in txt:
        return "FREE"
    return "UNKNOWN"


def classify(em: float, kind: str) -> tuple[str, str | None]:
    if em <= 1e-3:
        return "PASS", None
    if kind in ("HFIX", "HVAR"):
        return "FAIL-EXPLAINED", (
            "G13 求解器在常湿激波/HFIX-HVAR 边界下的有界质量增益；"
            "G15 已确认 Qin/Qout 列含边界通量。该 run 不计入质量门通过。"
        )
    return "FAIL", "非海/潮边界 run 超 1e-3，需先定位边界/源汇记账或数值问题。"


def main() -> None:
    results: dict[str, dict] = {}
    excluded: dict[str, str] = {}
    for root in CASE_ROOTS:
        for mass in sorted(root.rglob("results/res.mass")):
            run_dir = mass.parents[1]
            if not (run_dir / "run.log").exists():
                continue
            rel = run_dir.relative_to(PROJ).as_posix()
            if any(m in rel for m in EXCLUDE_MARKERS):
                excluded[rel] = "已废弃的 v1 DEM/河槽变体，不参与质量门"
                continue
            kind = bc_kind(run_dir)
            rec = e_mass(run_dir)
            gate, cause = classify(rec["e_mass_132"], kind)
            rec.update({"bc": kind, "gate_1e-3": gate, "cause": cause,
                        "excluded_from_pass_count": gate != "PASS"})
            results[rel] = rec
            print(rel, kind, rec["e_mass_132"], gate)

    counts = {k: sum(1 for r in results.values() if r["gate_1e-3"] == k)
              for k in ("PASS", "FAIL-EXPLAINED", "FAIL")}
    OUT.write_text(json.dumps({
        "computed_utc": datetime.now(timezone.utc).isoformat(),
        "formula": "E_mass=|Ve-V0-Vrain-(∫Qin-∫Qout)|/max(V0+Vrain+∫Qin,1)",
        "policy": "PASS<=1e-3；HFIX/HVAR 超限记 FAIL-EXPLAINED 且不计通过；其它超限 FAIL；不再用 N/A",
        "counts": counts, "excluded_variants": excluded, "runs": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("counts", counts, "->", OUT)


if __name__ == "__main__":
    main()
