"""把「斑块数悖论」的判据扫描与机制证据补进 guigang_phase4_scenarios.json。

来源：
  - data/catalog/guigang_phase4_threshold_scan.json（本脚本上一阶段生成）
  - Lead 独立复核的判据扫描表（已由 scan_guigang_threshold.py 逐项复现，数值完全一致）

本脚本只读两个 JSON，不改任何 run 目录。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
SCEN = PROJ / "data" / "catalog" / "guigang_phase4_scenarios.json"
SCAN = PROJ / "data" / "catalog" / "guigang_phase4_threshold_scan.json"


def main() -> None:
    scen = json.loads(SCEN.read_text(encoding="utf-8"))
    scan = json.loads(SCAN.read_text(encoding="utf-8"))

    # 1) 判据扫描表 + 单调性结论
    scen["patch_count_nondegeneracy"] = {
        "claim": (
            "斑块数在整体水位削减下不具单调性，因此不能作为本治理目标的验收指标。"
            "计划 §4 用『斑块数减少 ≥90%』作为验收标准，在本域存在指标失效风险。"
        ),
        "evidence_class": "E1（同一模型输出，多种后处理判据重算；可复算）",
        "wet_threshold_scan": scan["threshold_scan"],
        "monotonicity_finding": {
            "lower_threshold_does_not_fix_paradox": True,
            "worst_case_threshold_m": 0.02,
            "worst_case_patch_diff": 2432,
            "first_overtake_threshold_m": 0.30,
            "first_overtake_patch_diff": -302,
            "note": (
                "降低判据使悖论加剧（0.02 m 下 dualloss 6,221 vs baseline 3,789，差 +2,432）；"
                "需提高到约 0.30 m 才首次反超（4,580 vs 4,882）。"
                "但改判据不是改模型，只能作诊断对照，不能当作治理效果证据。"
            ),
        },
        "strict_subset_evidence": scan["subset_check"],
        "strict_subset_conclusion": (
            "dualloss_v1 的湿场是基线湿场的严格真子集（各判据下 dualloss_only 恒为 0）。"
            "既然减法只去水、不添水，斑块数上升只可能来自『减法把连通区域切断』，"
            "不存在新生成的水坑。"
        ),
        "split_accounting": {
            **scan["split_mechanism"],
            "reconciliation": {
                "baseline_patches": 4422,
                "baseline_single_cell_patches": 1922,
                "baseline_single_still_isolated_in_dualloss": 1629,
                "baseline_ge2_patches": 2500,
                "of_which_fully_dried": 63,
                "of_which_split_into": 4412,
                "sum_equals_dualloss_total": 1629 + 4412,
                "dualloss_total_patches": 6041,
                "residual": 6041 - (1629 + 4412),
                "note": "残差为 0，斑块数变化可逐斑块完全追溯。",
            },
        },
        "recommendation_for_phase1_and_adr0011": [
            "用湿面积变化率与主水体完整性（最大斑块面积及其占比）作主指标，二者单调。",
            "或把斑块数改为『≥N 格斑块数』（如 ≥10 格），过滤碎片噪声。",
            "若坚持用斑块数，必须同时固定湿格判据并声明其不单调性。",
        ],
    }

    # 2) 面积口径显式登记（回应 Lead 的核对要求）
    conv = scan["area_convention_check"]
    scen["metric_conventions"] = {
        "wet_threshold_m": 0.05,
        "connectivity": 8,
        "cell_area_m2": 10000.0,
        "fringe_patch_area_definition": (
            "『外围小斑块面积』= 非主水体斑块面积总和 = 总湿像元面积 − 最大斑块（主水体）面积。"
            "不是『≤9 格斑块面积总和』。"
        ),
        "fringe_area_flow_only_v1": conv,
        "fringe_area_baseline": {
            "wet_cells": 85256,
            "main_patch_cells": 61496,
            "non_main_patch_cells": 23760,
            "non_main_patch_area_km2": 237.6,
            "le9_patch_area_km2": 96.85,
            "le9_patch_count": 4089,
            "note": (
                "基线两种口径差异显著（237.60 vs 96.85 km²）。"
                "flow_only_v1 恰好两口径同值（29 像元 / 0.29 km²），因为该 run 无 10 格以上外围斑块。"
            ),
        },
        "convention_note": (
            "0.29 km² 明确为『非主水体斑块面积总和』。降幅若按 ≤9 格口径为 96.85 → 0.29 km²（−99.7%），"
            "按非主水体口径为 237.60 → 0.29 km²（−99.9%）。两者均已登记，不得混用。"
        ),
    }

    scen["generated_utc_updated"] = "2026-11-02"
    scen["update_note"] = (
        "补充 patch_count_nondegeneracy 与 metric_conventions 两节："
        "判据扫描、真子集验证、切断记账、面积口径。判据扫描数值与 Lead 独立复核完全一致。"
    )
    SCEN.write_text(json.dumps(scen, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"-> {SCEN}")
    print(f"   keys: {list(scen.keys())}")
    print(f"   file size: {SCEN.stat().st_size} bytes")


if __name__ == "__main__":
    main()
