"""阶段6：贵港组合驱动 2×2 因子归因（只读，ADR-0013）。

从 `data/catalog/guigang_s1_validation.json`（validate_guigang_s1.py 产出）取
各情景 t=228 h 足迹内指标，按 2×2 因子做差分归因。

记号（因子 A=河槽 off→hydraulic，因子 B=降雨 参照档→变体档）：
  A0B0 = 基线(off, on)          A1B0 = hydraulic 同参照档（chhyd_d14 / d10）
  A0B1 = 无河槽变体档           A1B1 = 组合 run（本轮新跑）
效应：
  A@B0 = A1B0 − A0B0（河槽主效应·参照档）
  A@B1 = A1B1 − A0B1（河槽主效应·变体档）
  B@A0 = A0B1 − A0B0（降雨主效应·无河槽）
  B@A1 = A1B1 − A1B0（降雨主效应·有河槽）
  交互 AB = B@A1 − B@A0（= A@B1 − A@B0）

两块 2×2：
  channel_x_loss_medium：B = on→loss_medium（A0B1=dualloss_v1）
  channel_x_norain：    B = on→off（A0B1=flow_only_v1）

纪律：只读 catalog JSON；不调 lisflood.exe；缺角点登记 unavailable 并跳过差分，
不得用不完整因子冒充归因。

用法：uv run python scripts/stage6/attribute_guigang_combo.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))
from scripts.common.grid_io import utc_now, write_json  # noqa: E402

S1_JSON = PROJ / "data" / "catalog" / "guigang_s1_validation.json"
OUT_JSON = PROJ / "data" / "catalog" / "guigang_combo_attribution.json"

METRICS = ("csi", "pod", "far", "area_sim_in_footprint_km2", "wet_cells_frame")

# 两块 2×2：A1B0 = 有河槽的 on 角点（须与组合同重构层深度）
BLOCKS = (
    {
        "block_id": "channel_x_loss_medium",
        "factor_b": "rain: on → loss_medium",
        "a0b0": "baseline_realdem",
        "a1b0": "chhyd_d14",
        "a0b1": "dualloss_v1",
        "a1b1": "chhyd_d14_r2_lossmed",
    },
    {
        "block_id": "channel_x_norain",
        "factor_b": "rain: on → off",
        "a0b0": "baseline_realdem",
        "a1b0": "chhyd_d10",
        "a0b1": "flow_only_v1",
        "a1b1": "chhyd_d10_r2_norain",
    },
)

GA_TAG = "chhyd_d14_r2_ga"  # 主方案单独对照（同 d14 河槽：on → ga）


def _cell(rec: dict | None, metrics: list[str]) -> dict:
    if rec is None:
        return {"available": False}
    return {"available": True, **{m: rec[m] for m in metrics if m in rec}}


def main() -> None:
    if not S1_JSON.is_file():
        raise SystemExit(f"缺少 {S1_JSON}（先跑 validate_guigang_s1.py）")
    doc = json.loads(S1_JSON.read_text(encoding="utf-8"))
    scen = doc.get("scenarios", {})

    payload: dict = {
        "script": "scripts/stage6/attribute_guigang_combo.py",
        "generated_utc": utc_now(),
        "read_only": True,
        "source": str(S1_JSON.relative_to(PROJ)),
        "conventions": {
            "notation": "A0B0=基线(off,on)；A1B0=hydraulic(on)；A0B1=无河槽变体档；A1B1=组合",
            "effects": "A@B0=A1B0−A0B0；A@B1=A1B1−A0B1；B@A0=A0B1−A0B0；B@A1=A1B1−A1B0；"
                       "交互AB=B@A1−B@A0",
            "metric_direction": "FAR/足迹内面积 越低越好；POD/CSI 越高越好",
            "caveat": "S1 足迹内重叠度口径（覆盖 20.6%，-17 dB 固定阈值 G17）；不是模型精度",
        },
        "blocks": [],
        "ga_standalone": None,
        "warnings": [],
    }

    for block in BLOCKS:
        tags = {k: block[k] for k in ("a0b0", "a1b0", "a0b1", "a1b1")}
        cells = {}
        for role, tag in tags.items():
            rec = scen.get(tag, {})
            cells[role] = _cell(rec if rec.get("available") else None, list(METRICS))
            cells[role]["tag"] = tag
        entry = {"block_id": block["block_id"], "factor_b": block["factor_b"],
                 "cells": cells, "complete": False, "metrics": {}}
        if all(c["available"] for c in cells.values()):
            entry["complete"] = True
            for m in METRICS:
                if not all(m in c for c in cells.values()):
                    continue
                v00, v10 = cells["a0b0"][m], cells["a1b0"][m]
                v01, v11 = cells["a0b1"][m], cells["a1b1"][m]
                entry["metrics"][m] = {
                    "cells": {"A0B0": v00, "A1B0": v10, "A0B1": v01, "A1B1": v11},
                    "A_at_B0": round(v10 - v00, 4),
                    "A_at_B1": round(v11 - v01, 4),
                    "B_at_A0": round(v01 - v00, 4),
                    "B_at_A1": round(v11 - v10, 4),
                    "interaction_AB": round((v11 - v10) - (v01 - v00), 4),
                }
        else:
            missing = [role for role, c in cells.items() if not c["available"]]
            payload["warnings"].append(
                f"{block['block_id']} 缺角点 {missing}；差分未计算（不得用不完整因子冒充归因）")
        payload["blocks"].append(entry)

    ga = scen.get(GA_TAG, {})
    if ga.get("available"):
        base = scen.get("baseline_realdem", {})
        d14 = scen.get("chhyd_d14", {})
        payload["ga_standalone"] = {
            "tag": GA_TAG,
            "metrics": {m: ga[m] for m in METRICS if m in ga},
            "delta_vs_baseline": ({m: round(ga[m] - base[m], 4) for m in METRICS
                                   if m in ga and base.get("available") and m in base}
                                  if base.get("available") else None),
            "delta_vs_chhyd_d14": ({m: round(ga[m] - d14[m], 4) for m in METRICS
                                    if m in ga and d14.get("available") and m in d14}
                                   if d14.get("available") else None),
            "note": "GA-Mid 主方案对照：delta_vs_chhyd_d14 = 同河槽档物理净雨的净效应（B@A1）",
        }
    else:
        payload["warnings"].append(f"{GA_TAG} 不可用")

    write_json(OUT_JSON, payload)
    print(f"归因结果 -> {OUT_JSON}")
    for b in payload["blocks"]:
        print(f"  {b['block_id']}: complete={b['complete']}")
        for m, eff in b["metrics"].items():
            print(f"    {m}: A@B0 {eff['A_at_B0']:+.4f}  A@B1 {eff['A_at_B1']:+.4f}  "
                  f"B@A0 {eff['B_at_A0']:+.4f}  B@A1 {eff['B_at_A1']:+.4f}  "
                  f"交互 {eff['interaction_AB']:+.4f}")
    if payload["ga_standalone"]:
        g = payload["ga_standalone"]
        print(f"  GA 主方案 {g['tag']}: delta_vs_baseline={g['delta_vs_baseline']}  "
              f"delta_vs_chhyd_d14={g['delta_vs_chhyd_d14']}")
    for w in payload["warnings"]:
        print(f"  警告: {w}")


if __name__ == "__main__":
    main()
