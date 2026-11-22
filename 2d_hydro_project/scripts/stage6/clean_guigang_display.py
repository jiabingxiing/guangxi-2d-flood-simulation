"""Phase 1：贵港 100 m 生产情景的**只读展示质控**派生层（display_qc）与逐帧审计。

定位（贵港计划 §3 Phase 1 / §4）
--------------------------------
本脚本生成的是**展示派生层**：它把源 run 的水深场按预注册规则做展示口径处理，
不改写源 run 的任何文件，也不产生任何新的物理结果。派生层始终带
``not_physical_recalculation=true``，不得表述为“校准”“真实淹没”或“物理修复”。

与久隆版的关系
--------------
本脚本迁移自 ``scripts/stage6/clean_jiulong_display.py``（久隆已验证实现），
**原样保留**久隆的全部纪律：

1. **单元面积从栅格头读取**：``cell_area_m2 = cellsize ** 2``。脚本不含任何
   写死的单元面积常量，也不含“4/9 像元”这类写死阈值。
2. **物理阈值优先，像元数是换算结果**：面积阈值用 km² 登记，像元数由
   ``n_cells = ceil(area_threshold_m2 / cell_area_m2)`` 现算；持续性阈值用小时
   登记，帧数由 ``ceil(hours * 3600 / saveint)`` 现算。
3. **规则顺序：先豁免，后过滤**（计划 §3 Phase 1 第 1–4 条）。任一豁免条件成立
   即保留；只有同时满足“小、短暂、孤立、浅水”的斑块才进入过滤候选。
4. **参数预注册**：A/B/C 三档写在模块常量里，处理前解析成含像元换算的完整
   参数 dict 并计算 ``param_hash``；不允许按结果事后挑选。
5. **预注册告警**：任一档导致湿面积或体积变化率超过预注册阈值，该档
   ``blocked_for_default_publish=true``，只能作诊断层，不得默认发布。

贵港与久隆的差异（本脚本的贵港专用部分）
----------------------------------------
- **三档参数取贵港计划 §3 表格原值**（面积 ≤0.04 / 0.09 / 0.25 km²，
  水深 ≤0.15 / 0.30 / 0.50 m），不得为凑结果修改。
- **主水体保护不用 S1 常水掩膜**：本域 S1 足印仅覆盖模型域 26.19%，北部
  半域覆盖率为 0，最大常水连通块 4.33 km²，而郁江主水体是 614.96 km²，
  无法充当主槽白名单。改用久隆版“候选主汇流区”定义
  （``MAIN_BODY_AREA_FRACTION``：按面积降序累计覆盖 50% 湿面积的斑块集合）
  加计划 §3 第 1 条的“主干水体 ≥5 km² 绝对豁免”。
- **第 4 条时空持久性规则真正生效**：同时用久隆的
  ``longest_consecutive_run < persistence_frames`` 与计划 §3 的
  ``persistence_ratio < min_persistence_ratio`` 两个判定（任一成立即判“短暂”），
  两阈值都由预注册的持续性小时数换算。

硬约束
------
- 禁止修改 ``outputs/guigang/**``：脚本前后各算一次源 run 哈希并比对，不一致即报错。
- 派生目录**不得包含** ``results/res.mass``：``scripts/stage3/g15_mass_recheck.py``
  用 ``rglob("results/res.mass")`` 扫描，派生目录若带 res.mass 会被误计为独立 run。
- 写盘前经 ``guard_no_overwrite``；目标目录已有 res.mass 直接退出。
- 不重跑 ``lisflood.exe``，不读写 ``models/``。

用法
----
    uv --directory 2d_hydro_project run python \\
        scripts/stage6/clean_guigang_display.py --dry-run

    uv --directory 2d_hydro_project run python \\
        scripts/stage6/clean_guigang_display.py --run-tag display-qc-v1
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from collections import Counter, deque
from pathlib import Path
from typing import Any

import numpy as np

PROJ = Path(__file__).resolve().parents[2]
if str(PROJ) not in sys.path:  # 允许 `python scripts/stage6/xxx.py` 直接运行
    sys.path.insert(0, str(PROJ))

from scripts.common.grid_io import (  # noqa: E402
    AsciiGrid,
    AuditLog,
    GridError,
    add_common_args,
    guard_no_overwrite,
    load_sea_mask,
    parse_run_par,
    read_ascii_grid,
    read_ascii_header,
    run_frames,
    sha256_file,
    utc_now,
    write_ascii_grid,
    write_json,
)
from scripts.common.patch_analysis import (  # noqa: E402
    PersistenceTracker,
    summarize_frame,
)

# --------------------------------------------------------------------------- #
# 常量：口径与预注册参数（处理前登记，禁止按结果修改）
# --------------------------------------------------------------------------- #

EVENT_ID = "2024-09-yagi"
CASE_DIR = PROJ / "outputs" / "guigang" / EVENT_ID
DISPLAY_QC_ROOT = PROJ / "outputs" / "display_qc" / "guigang" / EVENT_ID
AUDIT_PATH = PROJ / "data" / "catalog" / "guigang_display_qc_audit.json"
DEFAULT_RUN_ID = "scen_yagi_q11500_100m_dt2p5_choff_realdem"
#: 派生目录名沿用贵港计划 §3 Phase 1 第 3 条给定的路径，与源 run id 不同名。
DEFAULT_DISPLAY_RUN_ID = "scen_yagi_q11500_100m_realdem_display_qc"
TOOL = "scripts/stage6/clean_guigang_display.py"

#: 参数档冻结时刻（处理前登记的证据，写入审计 JSON）。
PRE_REGISTRATION_UTC = "2026-11-02T00:00:00+00:00"

#: 湿格判定口径。与项目既有统计（floodviz、products 报告）一致：
#: ``depth > 0.05 m`` 为湿，严格大于。E1（仓库内多处可复核）。
WET_THRESHOLD_M = 0.05

#: 主水体判定：每帧按面积降序累加，累计覆盖该帧湿面积 >= 该比例的斑块集合
#: 视为该帧的“候选主汇流区”。这是久隆版已验证的定义，贵港沿用。
#: E3 候选假设（项目口径，非外部规范）。
MAIN_BODY_AREA_FRACTION = 0.5

#: 计划 §3 Phase 1 第 1 条“主干水系保护（绝对豁免）”的面积门槛（km²）。
#: 直接取自计划原文 ``protected_main_stem_min_area_km2 = 5.0``。
MAIN_STEM_MIN_AREA_KM2 = 5.0

#: 计划 §3 Phase 1 第 2 条“显著深水洼地保护”的水深门槛（m）。
#: 直接取自计划原文 ``deep_depth_threshold_m = 0.50``。
DEEP_DEPTH_THRESHOLD_M = 0.50

#: 退水增长豁免：全局峰值帧之后，轨迹面积相对峰值帧增长超过该比例，
#: 视为“对退水异常有贡献”的斑块。退水异常不得用滤波掩盖，故必须豁免。E3 候选假设。
RECESSION_GROWTH_RATIO = 0.10

#: 预注册参数档 —— **严格取贵港计划 §3 表格原值，不得为凑结果修改**。
#:
#: 计划表格（面积上限 / 水深上限 / 预期去除斑块比例）：
#:   A 保守档  <= 0.04 km² & <= 0.15 m   预期 80.2%
#:   B 平衡档  <= 0.09 km² & <= 0.30 m   预期 92.5%
#:   C 激进档  <= 0.25 km² & <= 0.50 m   预期 96.8%
#:
#: ``persistence_hours`` 计划表格未给出，沿用久隆已验证的持续性梯度
#: 36 / 72 / 144 h（计划 §3 Phase 1 原文要求“采用久隆验证的三级复合规则”）。
#: 全部为项目候选参数，未经本地率定，置信度 E3 候选假设。
PRE_REGISTERED_PARAM_SETS: dict[str, dict[str, Any]] = {
    "A": {
        "label": "A 保守档",
        "intent": "只清除计划 A 档上限内的微小离散积水，尽量不动可解释积水",
        "shallow_max_area_km2": 0.04,
        "shallow_depth_m": 0.15,
        "persistence_hours": 36.0,
        "planned_patch_removal_pct": 80.2,
    },
    "B": {
        "label": "B 平衡档",
        "intent": "计划推荐平衡档：清除 ≤0.09 km² 且 ≤0.30 m 的离散积水",
        "shallow_max_area_km2": 0.09,
        "shallow_depth_m": 0.30,
        "persistence_hours": 72.0,
        "planned_patch_removal_pct": 92.5,
    },
    "C": {
        "label": "C 激进档",
        "intent": "计划激进展示档：清除 ≤0.25 km² 且 ≤0.50 m 的离散积水",
        "shallow_max_area_km2": 0.25,
        "shallow_depth_m": 0.50,
        "persistence_hours": 144.0,
        "planned_patch_removal_pct": 96.8,
    },
}

#: 预注册告警阈值（贵港计划 §3 Phase 1 表格“全域水量变化率告警门禁”列）。
#: 计划给的门禁是 < 5%，分档预期 < 0.5% / < 1.2% / < 2.5%；本脚本取计划门禁
#: 5% 作为**预注册告警上限**（超限即 blocked），分档预期值单列对照，不参与判定。
PRE_REGISTERED_ALARM: dict[str, Any] = {
    "area_change_pct": 5.0,
    "volume_change_pct": 5.0,
    "material_frame_fraction": 0.05,
    "sensitivity_band_pct": [0.5, 1.2, 2.5, 5.0],
    "planned_per_tier_pct": {"A": 0.5, "B": 1.2, "C": 2.5},
    "basis": (
        "计划 §3 Phase 1 表格原文“全域水量变化率（告警门禁 < 5%）”；"
        "分档预期 <0.5% / <1.2% / <2.5% 仅作对照，不作判定阈值。非外部规范数值。"
    ),
    "confidence": "E3 候选假设",
}

#: 计划 §4 验收目标（用于差距判定，不得修改）。
PLAN_ACCEPTANCE: dict[str, Any] = {
    "baseline_patch_count": 4422,
    "target_patch_count": 450,
    "target_patch_removal_pct": 89.82,
    "baseline_micro_patch_pct": 92.47,
    "target_micro_patch_pct": 25.0,
    "main_body_area_km2": 614.96,
    "max_volume_change_pct": 1.5,
    "min_test_passed": 160,
}

#: 规则顺序（写入审计，供复核）。顺序即计划 §3 Phase 1 的 1–4 条。
RULE_ORDER = [
    "R1 像元分类：nodata / 海域 / 干格 / 有效浅水 / 有效深水",
    "R2 斑块面积按栅格头 cell_area_m2 现算（不写死像元面积）",
    "R3 斑块统计：面积、最大/均值/分位水深、出现帧数、连续帧数、到候选主汇流区距离",
    "R4 豁免优先（计划 §3 第 1–2 条）：候选主汇流区 / 主干水体 >=5 km² / 深水 >=0.50 m / 触边界 / 对退水增长有贡献",
    "R5 过滤（计划 §3 第 3 条）：面积 <= A_max 且 水深 <= H_max",
    "R6 过滤（计划 §3 第 4 条时空持久性）：连续帧数 < persistence_frames 或 persistence_ratio < min_persistence_ratio",
    "R7 A/B/C 三档参数处理前登记，像元数由物理面积换算",
    "R8 逐帧写出被过滤像元、面积、体积、斑块数与原因码",
]

EXEMPT_REASONS = (
    "EXEMPT_MAIN_BODY",
    "EXEMPT_MAIN_STEM_LARGE",
    "EXEMPT_DEEP_WATER",
    "EXEMPT_BOUNDARY_OUTLET",
    "EXEMPT_RECESSION_GROWTH",
)
FILTER_REASONS = ("FILTER_DISCRETE_PUDDLE", "FILTER_TRANSIENT_NOISE")
KEEP_REASONS = ("KEEP_NATURAL_WETLAND",)

CAVEATS = [
    "展示派生层，不是物理重算，不得用于业务告警。",
    "被过滤像元水深置 0 仅为展示与统计口径处理；源 run 文件逐字节未改动。",
    "阈值属项目候选参数，未经本地率定，置信度 E3 候选假设；不归因于任何国际规范。",
    "100 m 为贵港本层处理的唯一网格；像元数由栅格头 cellsize 现算，未写死单元面积。",
    "主水体保护改用“候选主汇流区”定义，未使用 S1 常水掩膜（足印仅覆盖模型域 26.19%，北部半域为 0）。",
    "退水期湿面积增长等已知异常未被本层修复，仅在审计中登记，不得以滤波掩盖。",
    "本层不得表述为“校准”“真实淹没”或“物理修复”；raw 层保留且可一键回退。",
    "展示质控只能改善观感与展示统计口径，不能提高模型精度，也不能作为精度证明。",
    "计划 §4 的 >=90% 斑块去除目标在计划 §3 给定参数下不可达；详见审计 acceptance_gap。",
]

# 派生目录禁止出现的文件名（g15 质量门按 results/res.mass 识别独立 run）
FORBIDDEN_DERIVED_NAMES = ("res.mass",)

#: S1 常水掩膜不可用作主槽白名单的实测证据（本脚本运行时复核，写入审计）。
S1_MASK_EVIDENCE: dict[str, Any] = {
    "why_not_used": (
        "计划 §3 Phase 1 原设计用 S1 常水掩膜做郁江主槽保护白名单。实测本域 S1 "
        "掩膜足印不足：无法覆盖 614.96 km² 的郁江主水体，若用作白名单会漏保护"
        "大半主槽，故改用久隆版“候选主汇流区”定义加计划 §3 第 1 条面积豁免。"
    ),
    "scene_pre_event": "data/processed/s1_water/guigang_20240905T105022_water20m.tif",
    "scene_peak": "data/processed/s1_water/guigang_20240912T104222_water20m.tif",
    "measured": {
        "footprint_fraction_of_model_domain": 0.2619,
        "north_half_row_valid_coverage": 0.0,
        "largest_constant_water_patch_km2_at_20m": 4.33,
        "main_body_area_km2": 614.96,
    },
    "measurement_basis": (
        "20 m 掩膜按像元中心投影到模型域后统计：有效像元占模型域面积 26.19%；"
        "北半域（模型行 0–184）有效覆盖率 0%；域内最大常水连通块 4.33 km²。"
    ),
}

#: 计划 §4 验收差距诊断（**只诊断，不参与过滤，不修改预注册参数**）。
#: 目的：量化“要在计划给定规则结构下达到 >=90% 去除率需要多大的阈值变更”。
ACCEPTANCE_GAP_DIAGNOSTIC: dict[str, Any] = {
    "diagnostic_only": True,
    "why": (
        "计划 §3 第 2 条把 max_depth >= 0.50 m 的斑块全部豁免，因此任何档位"
        "最多只能去除 max_depth < 0.50 m 的斑块；这一上限与计划 §4 的 >=90% "
        "目标直接冲突。本诊断只量化差距，不改变预注册三档参数。"
    ),
    "depth_caps_m": [0.50, 1.00, 2.00, 3.00, 5.00],
    "area_caps_km2": [0.09, 0.25, 1.00, 2.00],
}

# 8 邻域偏移（与 patch_analysis 的连通域口径一致）
_NEIGHBORS8 = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))


# --------------------------------------------------------------------------- #
# 阈值换算（唯一入口，全部来自栅格头）
# --------------------------------------------------------------------------- #
def cells_for_area(area_km2: float, cell_area_m2: float) -> int:
    """物理面积阈值 → 像元数：``ceil(area_threshold_m2 / cell_area_m2)``，至少 1。

    ``cell_area_m2`` 必须来自栅格头 ``cellsize ** 2``（计划 §3.3 参数冻结规则）。
    """
    if cell_area_m2 <= 0:
        raise GridError(f"cell_area_m2 必须为正，收到 {cell_area_m2}")
    if area_km2 <= 0:
        return 1
    return max(1, int(math.ceil(area_km2 * 1e6 / cell_area_m2)))


def frames_for_persistence(hours: float, saveint_s: float) -> int:
    """持续性阈值 → 帧数：``ceil(hours * 3600 / saveint)``，至少 1 帧。"""
    if saveint_s <= 0:
        raise GridError(f"saveint 必须为正，收到 {saveint_s}")
    if hours <= 0:
        return 1
    return max(1, int(math.ceil(hours * 3600.0 / saveint_s)))


def param_hash(params: dict[str, Any]) -> str:
    """参数 dict 的 sha256（键排序、UTF-8、紧凑分隔符，保证可复算）。

    计算时**排除 ``param_hash`` 自身**，因此 ``param_hash(解析后的参数 dict)``
    可以直接复算并校验落盘的哈希，不会自指。
    """
    payload = {
        key: value for key, value in params.items() if key != "param_hash"
    }
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def resolve_param_set(
    spec: dict[str, Any],
    *,
    cell_area_m2: float,
    cellsize_m: float,
    saveint_s: float,
    n_frames: int,
) -> dict[str, Any]:
    """把预注册参数档解析成含像元/帧数换算结果的完整参数 dict（含 param_hash）。"""
    persistence_frames = frames_for_persistence(spec["persistence_hours"], saveint_s)
    resolved: dict[str, Any] = {
        "label": spec["label"],
        "intent": spec["intent"],
        "wet_threshold_m": WET_THRESHOLD_M,
        "cellsize_m": float(cellsize_m),
        "cell_area_m2": float(cell_area_m2),
        "shallow_max_area_km2": float(spec["shallow_max_area_km2"]),
        "n_cells_shallow_max_area": cells_for_area(
            spec["shallow_max_area_km2"], cell_area_m2),
        "shallow_depth_m": float(spec["shallow_depth_m"]),
        "persistence_hours": float(spec["persistence_hours"]),
        "persistence_frames": persistence_frames,
        "saveint_s": float(saveint_s),
        "n_frames": int(n_frames),
        "min_persistence_ratio": (
            float(persistence_frames) / float(n_frames) if n_frames > 0 else 1.0
        ),
        "main_stem_min_area_km2": float(MAIN_STEM_MIN_AREA_KM2),
        "n_cells_main_stem_min_area": cells_for_area(
            MAIN_STEM_MIN_AREA_KM2, cell_area_m2),
        "deep_depth_threshold_m": float(DEEP_DEPTH_THRESHOLD_M),
        "main_body_area_fraction": MAIN_BODY_AREA_FRACTION,
        "recession_growth_ratio": RECESSION_GROWTH_RATIO,
        "planned_patch_removal_pct": float(spec["planned_patch_removal_pct"]),
        "source": "项目候选参数，未经本地率定",
        "confidence": "E3 候选假设",
    }
    resolved["param_hash"] = param_hash(resolved)
    return resolved


# --------------------------------------------------------------------------- #
# 规则 R1：像元分类
# --------------------------------------------------------------------------- #
def classify_cells(
    depth: np.ndarray,
    *,
    sea_mask: np.ndarray | None,
    wet_threshold_m: float,
    shallow_depth_m: float,
    nodata: float,
) -> dict[str, int]:
    """按计划 §3 第 1 条区分 nodata / 海域 / 干格 / 有效浅水 / 有效深水。

    **不得升位到 float64**：栅格以 float32 存储，``float32(0.05)`` 实际为
    ``0.05000000074505806``。升到 float64 后 ``> 0.05`` 会把恰好写成 0.050 的像元
    判成湿，与 ``patch_analysis.summarize_frame`` 的 float32 口径不一致。
    """
    arr = np.asarray(depth)
    nodata_mask = ~np.isfinite(arr) | (arr == arr.dtype.type(nodata))
    sea = np.zeros(arr.shape, dtype=bool) if sea_mask is None else np.asarray(sea_mask, bool)
    valid = ~nodata_mask & ~sea
    wet = valid & (arr > arr.dtype.type(wet_threshold_m))
    shallow = wet & (arr < arr.dtype.type(shallow_depth_m))
    return {
        "nodata_cells": int(np.count_nonzero(nodata_mask)),
        "sea_cells": int(np.count_nonzero(sea)),
        "dry_cells": int(np.count_nonzero(valid & ~wet)),
        "valid_shallow_cells": int(np.count_nonzero(shallow)),
        "valid_deep_cells": int(np.count_nonzero(wet & ~shallow)),
        "wet_cells": int(np.count_nonzero(wet)),
    }


# --------------------------------------------------------------------------- #
# 规则 R3/R4：到候选主汇流区的距离、主汇流区标签、触边界
# --------------------------------------------------------------------------- #
def distance_to_mask(mask: np.ndarray) -> np.ndarray:
    """8 邻域 BFS 距离场（单位：像元数）；不可达为 -1。纯 numpy/标准库实现。"""
    mask = np.asarray(mask, dtype=bool)
    height, width = mask.shape
    out = np.full((height, width), -1, dtype=np.int32)
    queue: deque[tuple[int, int]] = deque()
    for row, col in zip(*np.nonzero(mask)):
        out[row, col] = 0
        queue.append((int(row), int(col)))
    while queue:
        cur_row, cur_col = queue.popleft()
        nxt = out[cur_row, cur_col] + 1
        for d_row, d_col in _NEIGHBORS8:
            row, col = cur_row + d_row, cur_col + d_col
            if 0 <= row < height and 0 <= col < width and out[row, col] == -1:
                out[row, col] = nxt
                queue.append((row, col))
    return out


def main_body_patch_labels(summary: Any, fraction: float) -> set[int]:
    """该帧的候选主汇流区斑块标签集合（按面积降序累计覆盖 fraction 湿面积）。"""
    total = sum(p.area_km2 for p in summary.patches)
    if total <= 0:
        return set()
    labels: set[int] = set()
    acc = 0.0
    for patch in summary.patches:  # summarize_frame 已按面积降序
        if acc >= fraction * total:
            break
        labels.add(patch.label)
        acc += patch.area_km2
    return labels


def touches_boundary(patch: Any, shape: tuple[int, int]) -> bool:
    """斑块是否触及模型域边界（候选出流口，计划 §3 第 1 条“可信排水路径”的代理）。"""
    return (
        patch.row_min == 0
        or patch.col_min == 0
        or patch.row_max == shape[0] - 1
        or patch.col_max == shape[1] - 1
    )


# --------------------------------------------------------------------------- #
# 规则 R4/R5/R6：先豁免，后过滤（计划 §3 Phase 1 第 1–4 条）
# --------------------------------------------------------------------------- #
def classify_patch(
    patch: Any,
    params: dict[str, Any],
    *,
    is_main_body: bool,
    longest_consecutive_run: int,
    persistence_ratio: float,
    is_recession_growth: bool,
    hits_boundary: bool,
    min_distance_cells: int,
) -> tuple[str, str]:
    """单个斑块的展示质控分类，返回 ``(verdict, reason_code)``。

    verdict ∈ {``exempt``, ``filter``, ``keep``}。

    顺序严格按计划 §3 Phase 1：

    - **第 1 条 主干水系保护（绝对豁免）**：候选主汇流区成员，或面积
      ``>= MAIN_STEM_MIN_AREA_KM2``（计划原文 5.0 km²）。
    - **第 2 条 显著深水洼地保护**：``max_depth_m >= 0.50 m``（计划原文）。
    - 另加两条久隆已验证的保守豁免：触模型域边界（候选出流口）、
      对退水增长有贡献（退水异常不得用滤波掩盖）。
    - **第 3 条 微型离散浅水斑块**：``area <= A_max`` 且 ``depth <= H_max``。
    - **第 4 条 时空持久性**：``longest_consecutive_run < persistence_frames``
      或 ``persistence_ratio < min_persistence_ratio``（任一成立即判“短暂”）。

    任一豁免条件成立都不会被过滤（先豁免后过滤）。
    """
    area = float(patch.area_km2)
    depth = float(patch.max_depth_m)
    persist_frames = int(params["persistence_frames"])

    # ---- 第 1 条：主干水系保护（绝对豁免） ----
    if is_main_body:
        return "exempt", "EXEMPT_MAIN_BODY"
    if area >= float(params["main_stem_min_area_km2"]):
        return "exempt", "EXEMPT_MAIN_STEM_LARGE"
    # ---- 第 2 条：显著深水洼地保护 ----
    if depth >= float(params["deep_depth_threshold_m"]):
        return "exempt", "EXEMPT_DEEP_WATER"
    # ---- 久隆已验证的额外保守豁免 ----
    if is_recession_growth:
        return "exempt", "EXEMPT_RECESSION_GROWTH"
    if hits_boundary:
        return "exempt", "EXEMPT_BOUNDARY_OUTLET"

    # ---- 第 3 条：微型离散浅水斑块 ----
    if (
        area <= float(params["shallow_max_area_km2"])
        and depth <= float(params["shallow_depth_m"])
    ):
        return "filter", "FILTER_DISCRETE_PUDDLE"

    # ---- 第 4 条：时空持久性判定 ----
    brief_by_run = int(longest_consecutive_run) < persist_frames
    brief_by_ratio = float(persistence_ratio) < float(params["min_persistence_ratio"])
    if brief_by_run or brief_by_ratio:
        return "filter", "FILTER_TRANSIENT_NOISE"
    return "keep", "KEEP_NATURAL_WETLAND"


# --------------------------------------------------------------------------- #
# 源 run 哈希（前后比对，证明只读）
# --------------------------------------------------------------------------- #
def hash_source_run(run_dir: Path, frames: list[tuple[int, Path]]) -> dict[str, Any]:
    """源 run 完整性快照：res.max、全部 res-*.wd、res.mass 的 sha256。"""
    res_dir = run_dir / "results"
    frame_hashes = {path.name: sha256_file(path) for _seq, path in frames}
    res_max = res_dir / "res.max"
    res_mass = res_dir / "res.mass"
    combined = hashlib.sha256()
    for name in sorted(frame_hashes):
        combined.update(f"{name}:{frame_hashes[name]}\n".encode("utf-8"))
    return {
        "res_max_sha256": sha256_file(res_max) if res_max.is_file() else None,
        "res_mass_sha256": sha256_file(res_mass) if res_mass.is_file() else None,
        "frame_count": len(frame_hashes),
        "frame_list_sha256": combined.hexdigest(),
        "frames": frame_hashes,
    }


def compare_source_hashes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """比对前后哈希；任何差异都是硬失败（计划 §5 Phase 1 验收第 1 条）。"""
    diffs = [
        key
        for key in ("res_max_sha256", "res_mass_sha256", "frame_list_sha256", "frame_count")
        if before.get(key) != after.get(key)
    ]
    changed_frames = sorted(
        name
        for name, digest in before.get("frames", {}).items()
        if after.get("frames", {}).get(name) != digest
    )
    return {
        "unchanged": not diffs and not changed_frames,
        "differing_keys": diffs,
        "changed_frames": changed_frames,
    }


# --------------------------------------------------------------------------- #
# 派生目录写入守卫
# --------------------------------------------------------------------------- #
def _assert_outside_raw_runs(out_dir: Path) -> None:
    """派生目录不得落在受保护的 raw run 目录内（禁止修改 outputs/guigang/**）。"""
    protected = (PROJ / "outputs" / "guigang").resolve()
    target = Path(out_dir).resolve()
    if target == protected or protected in target.parents:
        raise RuntimeError(
            f"拒绝写入受保护目录 {target}：禁止修改 outputs/guigang/** 下任何文件"
        )


def guard_derived_target(target: Path) -> None:
    """派生目录写前检查：已有 res.mass 拒写；非派生层目录不覆盖。"""
    guard_no_overwrite(target)  # 终稿 §10：有 results/res.mass 即拒绝
    meta_path = target / "meta.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
        if meta.get("not_physical_recalculation") is not True:
            raise RuntimeError(
                f"{target} 已存在且未标记 not_physical_recalculation=true；"
                "为避免覆盖非派生结果，拒绝写入"
            )


def assert_no_forbidden_files(out_dir: Path) -> None:
    """派生目录禁止出现 res.mass 等会被质量门误计的文件。"""
    offenders = [
        p.relative_to(out_dir).as_posix()
        for p in out_dir.rglob("*")
        if p.is_file() and p.name in FORBIDDEN_DERIVED_NAMES
    ]
    if offenders:
        raise RuntimeError(
            f"派生目录出现禁止文件 {offenders}：g15_mass_recheck 会把带 "
            "results/res.mass 的目录误计为独立 run，污染质量门计数"
        )


def _as_grid(data: np.ndarray, header: dict[str, float]) -> AsciiGrid:
    return AsciiGrid(data=np.asarray(data, dtype=np.float32), header=header)


# --------------------------------------------------------------------------- #
# PersistenceTracker 内部状态契约（Lead 模块未暴露像元→轨迹映射的公开 API）
# --------------------------------------------------------------------------- #
def assert_tracker_contract(tracker: PersistenceTracker) -> dict[int, int]:
    """校验并返回 ``PersistenceTracker._cell_owner``（像元扁平号 → 轨迹 id）。

    ``patch_analysis.PersistenceTracker`` 只提供 ``update``/``summaries``，
    没有公开的“像元 → 轨迹”映射；而展示质控必须把轨迹级判定回写到像元。本函数把
    对内部属性的依赖收敛到一处，并在结构不符时**显式报错**而不是静默退化。
    """
    owner = getattr(tracker, "_cell_owner", None)
    if not isinstance(owner, dict):
        raise GridError(
            "PersistenceTracker 未提供 _cell_owner 映射；"
            "展示质控需要像元→轨迹映射，请先与 Lead 确认模块契约"
        )
    for cell, track_id in owner.items():
        if not isinstance(cell, int) or not isinstance(track_id, int):
            raise GridError(
                f"PersistenceTracker._cell_owner 类型不符：{type(cell)}->{type(track_id)}；"
                "期望 int->int（扁平像元号 → 轨迹 id）"
            )
    return dict(owner)


# --------------------------------------------------------------------------- #
# 轨迹上下文（三档共用，只算一次）
# --------------------------------------------------------------------------- #
def build_track_context(
    summaries: list[Any],
    owner_by_frame: dict[int, dict[int, int]],
    tracks: list[dict[str, Any]],
    *,
    shape: tuple[int, int],
    peak_seq: int,
    n_frames: int,
) -> dict[str, Any]:
    """汇总轨迹级证据，并压成**按像元**的查表数组，供逐帧逐斑块向量化取用。

    每个像元在任一帧只归属一条轨迹，但跨帧可能归属多条；因此对“是否豁免”
    这类判定，本函数按**最保守**方向聚合（取最持久、任一触边界、任一增长），
    避免因轨迹碎片化把真实积水误判为短暂。
    """
    main_body_tracks: set[int] = set()
    boundary_tracks: set[int] = set()
    main_body_cells = np.zeros(shape, dtype=bool)
    track_frame_area: dict[int, dict[int, float]] = {}
    frame_main_labels: dict[int, set[int]] = {}

    for summary in summaries:
        labels = main_body_patch_labels(summary, MAIN_BODY_AREA_FRACTION)
        frame_main_labels[summary.seq] = labels
        owner = owner_by_frame[summary.seq]
        for patch in summary.patches:
            cells = patch.cell_indices
            if cells.size == 0:
                continue
            track_id = owner.get(int(cells[0]))
            if track_id is None:
                continue
            areas = track_frame_area.setdefault(track_id, {})
            areas[summary.seq] = max(areas.get(summary.seq, 0.0), patch.area_km2)
            if patch.label in labels:
                main_body_cells.ravel()[cells] = True
                main_body_tracks.add(track_id)
            if touches_boundary(patch, shape):
                boundary_tracks.add(track_id)

    distance = distance_to_mask(main_body_cells)

    after_peak = [summary.seq for summary in summaries if summary.seq > peak_seq]
    growth: dict[int, bool] = {}
    for track_id, areas in track_frame_area.items():
        peak_area = areas.get(peak_seq, 0.0)
        post = [areas[seq] for seq in after_peak if seq in areas]
        if peak_area > 0 and post:
            growth[track_id] = (max(post) - peak_area) > RECESSION_GROWTH_RATIO * peak_area
        else:
            growth[track_id] = False

    run_by_track = {
        int(t["track_id"]): int(t["longest_consecutive_run"]) for t in tracks
    }
    ratio_by_track = {
        int(t["track_id"]): (
            float(t["frame_count"]) / float(n_frames) if n_frames > 0 else 0.0
        )
        for t in tracks
    }

    # ---- 压成按像元查表数组（跨帧取最保守方向） ----
    n_cells = shape[0] * shape[1]
    flat_run = np.zeros(n_cells, dtype=np.int32)
    flat_ratio = np.zeros(n_cells, dtype=np.float32)
    flat_growth = np.zeros(n_cells, dtype=bool)
    flat_boundary = np.zeros(n_cells, dtype=bool)
    for owner in owner_by_frame.values():
        for cell, track_id in owner.items():
            run = run_by_track.get(track_id, 0)
            ratio = ratio_by_track.get(track_id, 0.0)
            if run > flat_run[cell]:
                flat_run[cell] = run
            if ratio > flat_ratio[cell]:
                flat_ratio[cell] = ratio
            if growth.get(track_id):
                flat_growth[cell] = True
            if track_id in boundary_tracks:
                flat_boundary[cell] = True

    min_distance: dict[int, int] = {}
    flat_distance = distance.ravel()
    for summary in summaries:
        owner = owner_by_frame[summary.seq]
        for patch in summary.patches:
            cells = patch.cell_indices
            if cells.size == 0:
                continue
            track_id = owner.get(int(cells[0]))
            if track_id is None:
                continue
            values = flat_distance[cells]
            values = values[values >= 0]
            if values.size == 0:
                continue
            best = int(values.min())
            prev = min_distance.get(track_id)
            if prev is None or best < prev:
                min_distance[track_id] = best

    # 每个像元到候选主汇流区的最小距离（跨帧取最小；仅审计用，不参与判定）
    flat_min_distance = np.full(n_cells, -1, dtype=np.int32)
    for owner in owner_by_frame.values():
        for cell, track_id in owner.items():
            best = min_distance.get(track_id)
            if best is None:
                continue
            if flat_min_distance[cell] < 0 or best < flat_min_distance[cell]:
                flat_min_distance[cell] = best

    return {
        "main_body_tracks": main_body_tracks,
        "boundary_tracks": boundary_tracks,
        "main_body_cells": main_body_cells,
        "frame_main_labels": frame_main_labels,
        "distance": distance,
        "min_distance": min_distance,
        "growth": growth,
        "track_frame_area": track_frame_area,
        "flat_run": flat_run,
        "flat_ratio": flat_ratio,
        "flat_growth": flat_growth,
        "flat_boundary": flat_boundary,
        "flat_min_distance": flat_min_distance,
    }


def _patch_evidence(
    patch: Any, context: dict[str, Any], *, is_main_body: bool
) -> dict[str, Any]:
    """从按像元查表数组取出该斑块的轨迹级证据（最保守方向）。

    ``min_distance_cells`` 只作审计证据登记，**不参与判定**：贵港计划 §3
    Phase 1 的四条规则里没有“孤立性”条件，故本脚本不擅自增加该过滤条件。
    """
    cells = patch.cell_indices
    distances = context["flat_min_distance"][cells]
    distances = distances[distances >= 0]
    return {
        "is_main_body": bool(is_main_body),
        "longest_consecutive_run": int(context["flat_run"][cells].max()) if cells.size else 0,
        "persistence_ratio": (
            float(context["flat_ratio"][cells].max()) if cells.size else 0.0
        ),
        "is_recession_growth": bool(context["flat_growth"][cells].any()) if cells.size else False,
        "hits_boundary": bool(context["flat_boundary"][cells].any()) if cells.size else False,
        "min_distance_cells": int(distances.min()) if distances.size else -1,
    }


def classify_frame_patches(
    summary: Any,
    params: dict[str, Any],
    context: dict[str, Any],
    *,
    main_labels: set[int],
) -> dict[int, tuple[str, str]]:
    """对一帧的全部斑块做展示质控分类，返回 ``{patch_label: (verdict, reason)}``。"""
    verdicts: dict[int, tuple[str, str]] = {}
    for patch in summary.patches:
        evidence = _patch_evidence(
            patch, context, is_main_body=patch.label in main_labels)
        verdicts[patch.label] = classify_patch(patch, params, **evidence)
    return verdicts


# --------------------------------------------------------------------------- #
# 单档处理
# --------------------------------------------------------------------------- #
def process_param_set(
    *,
    set_id: str,
    params: dict[str, Any],
    out_dir: Path,
    shape: tuple[int, int],
    cell_area_m2: float,
    header: dict[str, float],
    sea_mask: np.ndarray,
    frames: list[tuple[int, Path]],
    frame_grids: dict[int, np.ndarray],
    summaries: list[Any],
    context: dict[str, Any],
    res_max: np.ndarray,
    res_max_summary: Any,
    res_max_main_labels: set[int],
    dry_run: bool,
    source_rel: str,
    source_dir: Path,
    source_hashes: dict[str, Any],
    run_tag: str | None,
    plan_acceptance: dict[str, Any],
) -> dict[str, Any]:
    """单档处理：逐帧逐斑块分类 → 过滤 → 写派生层与审计。"""
    peak_area = max(summary.wet_area_km2 for summary in summaries)
    material_fraction = float(PRE_REGISTERED_ALARM["material_frame_fraction"])
    frame_records: list[dict[str, Any]] = []
    all_verdicts: Counter[str] = Counter()
    all_reasons: Counter[str] = Counter()
    patch_verdict_records: list[dict[str, Any]] = []
    # 逐帧被过滤像元掩膜，供 res.max 单向性保护使用（见下方 monotonicity_guard）
    frame_removed_masks: list[np.ndarray] = []

    if not dry_run:
        guard_derived_target(out_dir)

    for summary in summaries:
        seq = summary.seq
        depth = frame_grids[seq]
        main_labels = context["frame_main_labels"].get(seq, set())
        verdicts = classify_frame_patches(
            summary, params, context, main_labels=main_labels)

        # ---- 由斑块判定构造被过滤像元掩膜 ----
        removed = np.zeros(shape[0] * shape[1], dtype=bool)
        removed_patches = 0
        reason_patches: Counter[str] = Counter()
        reason_cells: Counter[str] = Counter()
        for patch in summary.patches:
            cells = patch.cell_indices
            if cells.size == 0:
                continue
            kind, code = verdicts[patch.label]
            all_verdicts[kind] += 1
            all_reasons[code] += 1
            if kind == "filter":
                removed[cells] = True
                removed_patches += 1
                reason_patches[code] += 1
                reason_cells[code] += int(cells.size)
                evidence = _patch_evidence(
                    patch, context, is_main_body=patch.label in main_labels)
                patch_verdict_records.append({
                    "seq": seq,
                    "patch_label": int(patch.label),
                    "verdict": kind,
                    "reason_code": code,
                    "area_km2": round(float(patch.area_km2), 6),
                    "max_depth_m": round(float(patch.max_depth_m), 4),
                    "n_cells": int(patch.n_cells),
                    **evidence,
                })
        removed = removed.reshape(shape)
        frame_removed_masks.append(removed)

        cells_counts = classify_cells(
            depth,
            sea_mask=sea_mask,
            wet_threshold_m=params["wet_threshold_m"],
            shallow_depth_m=params["shallow_depth_m"],
            nodata=float(header["nodata_value"]),
        )
        raw_volume = summary.wet_volume_m3
        removed_cells = int(np.count_nonzero(removed))
        # 与 patch_analysis.summarize_frame 同口径：float32 求和后乘单元面积，
        # 保证 raw = kept + removed 的账目可复算（不引入额外的 float64 升位）
        removed_volume = float(np.sum(np.where(removed, depth, 0.0))) * cell_area_m2
        removed_area = removed_cells * cell_area_m2 / 1e6

        cleaned = depth.copy()
        cleaned[removed] = 0.0
        after = _quick_frame_counts(
            cleaned,
            wet_threshold_m=params["wet_threshold_m"],
            cell_area_m2=cell_area_m2,
            sea_mask=sea_mask,
        )
        area_change = (
            (summary.wet_area_km2 - after["wet_area_km2"]) / summary.wet_area_km2 * 100.0
            if summary.wet_area_km2 > 0 else 0.0
        )
        volume_change = (
            (raw_volume - after["wet_volume_m3"]) / raw_volume * 100.0
            if raw_volume > 0 else 0.0
        )

        frame_records.append({
            "seq": seq,
            "t_h": round(seq * params["saveint_s"] / 3600.0, 3),
            "material_frame": bool(
                peak_area > 0 and summary.wet_area_km2 >= peak_area * material_fraction
            ),
            **cells_counts,
            "n_patches_raw": summary.n_patches,
            "n_patches_after": after["n_patches"],
            "removed_cells": removed_cells,
            "removed_area_km2": round(removed_area, 6),
            "removed_volume_m3": round(removed_volume, 1),
            "removed_patches": removed_patches,
            "removed_reason_patches": dict(sorted(reason_patches.items())),
            "removed_reason_cells": dict(sorted(reason_cells.items())),
            "raw_wet_area_km2": round(summary.wet_area_km2, 6),
            "raw_wet_volume_m3": round(raw_volume, 1),
            "kept_wet_cells": after["wet_cells"],
            "kept_wet_area_km2": round(after["wet_area_km2"], 6),
            "kept_wet_volume_m3": round(after["wet_volume_m3"], 1),
            "area_change_pct": round(area_change, 4),
            "volume_change_pct": round(volume_change, 4),
        })

        if not dry_run:
            write_ascii_grid(
                out_dir / "results" / f"res-{seq:04d}.wd", _as_grid(cleaned, header)
            )

    # ---- res.max 峰值场 ----
    # **口径决策（重要，2026-11-02 Lead 定）**：
    # 派生 res.max 一律取「全部保留帧的逐像元最大水深」，即 cleaned 帧的包络。
    # 理由三条：
    #  1. **单调性由构造保证**：包络按定义 ≥ 任一保留帧，`res.max ≥ 逐帧` 恒成立，
    #     不需要任何事后"保护"补丁。此前按 res.max 斑块级删 + 事后保护的做法，
    #     实测两头都不对：不加保护则 1,732 像元违反单向性（floodviz validate FAIL）；
    #     加"任一保留帧保留即不清零"则退化为 res.max 与原场逐位相同（等于不过滤）。
    #  2. **口径自洽**：res.max 与逐帧走同一套轨迹判定，不会出现"某像元在 res.max
    #     被删、在逐帧被保留"的自相矛盾。
    #  3. **代价必须登记**：源 run 的 res.max 是**子步累积包络**，6 h 采样帧的包络
    #     会略低于它（源 res.max 捕捉了帧间峰）。因此派生 res.max 的过滤率**不等于**
    #     源 res.max 口径的过滤率，两者必须分别报告，不得混用。
    #     计划 §4 的"4,422 斑块 / 去除率"是按**源** res.max 计的，仍由
    #     res_max_summary（raw）与 res_max_verdicts 给出，用于对齐计划口径。
    res_max_verdicts = classify_frame_patches(
        res_max_summary, params, context, main_labels=res_max_main_labels)
    raw_max_removed = np.zeros(shape[0] * shape[1], dtype=bool)
    max_removed_patches = 0
    max_reason_patches: Counter[str] = Counter()
    for patch in res_max_summary.patches:
        kind, code = res_max_verdicts[patch.label]
        if kind == "filter":
            raw_max_removed[patch.cell_indices] = True
            max_removed_patches += 1
            max_reason_patches[code] += 1
    raw_max_removed = raw_max_removed.reshape(shape)

    # 派生 res.max = 保留帧包络（单调性由构造保证）
    cleaned_max = np.zeros(shape, dtype=res_max.dtype)
    for idx, summary in enumerate(summaries):
        seq = summary.seq
        depth = frame_grids[seq]
        frame_removed = frame_removed_masks[idx]
        kept = (~frame_removed) & np.isfinite(depth)
        np.maximum(cleaned_max, np.where(kept, depth, 0), out=cleaned_max)
    # ---- 主水体 100% 保护（计划 §4 硬验收）----
    # 包络法下主水体边缘的浅水像元可能因"被过滤帧不再贡献峰值"而跌破湿阈值。
    # 计划 §4 要求「郁江干流连通漫滩面积 614.96 km² 完整保护」，故对这些像元一律取
    # **源 res.max 原值**（不削减）。该操作只抬高水深、不新增像元，且仍满足
    # res.max >= 逐帧（源 res.max 本身是子步包络）。
    #
    # 保护范围取**两套口径的并集**（都属计划 §4 要保护的"干流连通漫滩"）：
    #   (a) 跨帧候选主汇流区 `context["main_body_cells"]`（按面积降序累计 50% 湿面积）；
    #   (b) res.max 上面积最大的单个连通斑块（计划 §4 原文的 614.96 km² 基准）。
    # 只用 (a) 不够：实测 res.max 最大斑块有 2,018 像元不在跨帧并集里（该并集是
    # 逐帧 50% 覆盖的并，非 res.max 斑块本身），会漏保护。
    main_protected = np.asarray(context["main_body_cells"], dtype=bool).copy()
    if res_max_summary.patches:
        main_protected.ravel()[res_max_summary.patches[0].cell_indices] = True
    main_protected_cells = int(np.count_nonzero(main_protected))
    cleaned_max[main_protected] = np.maximum(
        cleaned_max[main_protected], res_max[main_protected])
    # 实际被清零的像元（相对源 res.max）
    max_removed = (res_max > 0) & (cleaned_max == 0)
    max_removed_cells = int(np.count_nonzero(max_removed))
    # 派生包络相对源 res.max 的**湿格**减少量（计划 §4 的湿格口径，depth>0.05）
    # 注意：包络法下几乎不会被"清零"，湿格减少主要来自**水深被压低到阈值以下**
    # （被过滤的帧不再贡献该像元的最大值）。因此"减少"必须按湿格口径统计，
    # 不能按"清零"统计，否则会得到 0 而掩盖真实削减。
    raw_wet_mask = np.isfinite(res_max) & (res_max > res_max.dtype.type(params["wet_threshold_m"]))
    kept_wet_mask = np.isfinite(cleaned_max) & (
        cleaned_max > cleaned_max.dtype.type(params["wet_threshold_m"]))
    max_wet_lost = raw_wet_mask & ~kept_wet_mask
    max_removed_cells = int(np.count_nonzero(max_wet_lost))
    max_removed = max_wet_lost
    monotonicity_guard = {
        "applied": True,
        "method": "envelope_of_kept_frames",
        "rule": (
            "派生 res.max = 全部保留帧的逐像元最大水深（包络），"
            "因此 res.max >= 任一保留帧由构造保证"
        ),
        "cells_cleared_to_zero": int(
            np.count_nonzero((res_max > 0) & (cleaned_max == 0))),
        "wet_cells_lost_below_threshold": max_removed_cells,
        "reason": (
            "源 res.max 为子步累积包络、逐帧为 6 h 采样；按 res.max 斑块级删再事后"
            "补保护，实测非违反单向性(1,732 像元)即退化为不过滤。改用保留帧包络后"
            "两项问题同时消除。代价：派生 res.max 略低于源 res.max，过滤率与源口径"
            "不同，两者分别报告"
        ),
    }

    # 注意：cleaned_max 已在上方由「保留帧包络」算出，此处**不得**再用
    # res_max.copy() 覆盖（曾因此使 res.max 与源逐位相同、过滤完全失效）。
    after_max = _quick_frame_counts(
        cleaned_max,
        wet_threshold_m=params["wet_threshold_m"],
        cell_area_m2=cell_area_m2,
        sea_mask=sea_mask,
    )
    raw_max = res_max_summary
    max_area_change = (
        (raw_max.wet_area_km2 - after_max["wet_area_km2"]) / raw_max.wet_area_km2 * 100.0
        if raw_max.wet_area_km2 > 0 else 0.0
    )
    max_volume_change = (
        (raw_max.wet_volume_m3 - after_max["wet_volume_m3"]) / raw_max.wet_volume_m3 * 100.0
        if raw_max.wet_volume_m3 > 0 else 0.0
    )

    # ---- 主水体 100% 保护硬校验（计划 §4 验收） ----
    main_cells = context["main_body_cells"]
    main_body_cells_total = int(np.count_nonzero(main_cells))
    main_body_removed_cells = int(np.count_nonzero(max_removed & main_cells))
    main_body_area_raw = main_body_cells_total * cell_area_m2 / 1e6
    main_body_area_kept = (
        main_body_cells_total - main_body_removed_cells) * cell_area_m2 / 1e6

    # 计划 §4 原文口径：**郁江干流连通漫滩面积 614.96 km²**，即 res.max 上
    # 面积最大的单个连通斑块（占 72.13%）。此处按斑块标签精确核对，不用像元并集，
    # 避免把“跨帧并集 612.68 km²”误当成计划基准值。
    largest_patch = raw_max.patches[0] if raw_max.patches else None
    largest_patch_removed_cells = 0
    if largest_patch is not None:
        largest_patch_removed_cells = int(
            np.count_nonzero(max_removed.ravel()[largest_patch.cell_indices]))
    main_stem_record = {
        "definition": "res.max 上面积最大的单个连通斑块（计划 §4 的“郁江干流连通漫滩”）",
        "label": int(largest_patch.label) if largest_patch is not None else None,
        "raw_cells": int(largest_patch.n_cells) if largest_patch is not None else 0,
        "raw_area_km2": (
            round(float(largest_patch.area_km2), 4) if largest_patch is not None else 0.0),
        "plan_reference_area_km2": plan_acceptance["main_body_area_km2"],
        "removed_cells": largest_patch_removed_cells,
        "kept_area_km2": round(
            (int(largest_patch.n_cells) - largest_patch_removed_cells)
            * cell_area_m2 / 1e6, 4) if largest_patch is not None else 0.0,
        "fully_protected": largest_patch_removed_cells == 0,
        "note": "计划 §4 验收项；removed_cells 必须为 0，kept_area_km2 必须等于 614.96",
    }

    main_body_record = {
        "raw_cells": main_body_cells_total,
        "raw_area_km2": round(main_body_area_raw, 4),
        "removed_cells": main_body_removed_cells,
        "kept_cells": main_body_cells_total - main_body_removed_cells,
        "kept_area_km2": round(main_body_area_kept, 4),
        "fully_protected": main_body_removed_cells == 0,
        "plan_reference_area_km2": plan_acceptance["main_body_area_km2"],
        "largest_patch": main_stem_record,
        "note": (
            "候选主汇流区（按面积降序累计覆盖 50% 湿面积的斑块集合，跨帧并集）"
            "逐像元核对；removed_cells 必须为 0。计划 §4 的 614.96 km² 对应 "
            "largest_patch（res.max 最大单斑块），二者口径不同故数值不同。"
        ),
    }

    # ---- 微小斑块占比（计划 §4 验收：92.47% -> <=25%） ----
    def micro_patch_pct(summary_like: Any) -> dict[str, Any]:
        """微小斑块（<=9 像元）占比，直接由斑块统计得出，不重算连通域。"""
        total = int(summary_like.n_patches)
        if total == 0:
            return {"n_patches": 0, "n_le_9_cells": 0, "micro_pct": 0.0}
        le9 = sum(1 for p in summary_like.patches if p.n_cells <= 9)
        return {
            "n_patches": total,
            "n_le_9_cells": int(le9),
            "micro_pct": round(le9 / total * 100.0, 4),
        }

    raw_micro = micro_patch_pct(raw_max)
    kept_micro = {
        "n_patches": after_max["n_patches"],
        "n_le_9_cells": after_max["n_le_9_cells"],
        "micro_pct": after_max["micro_pct"],
    }

    res_max_record = {
        # ---- 口径 A：源 res.max 的**斑块级判定**（对齐计划 §4 的 4,422 斑块口径）----
        "raw_wet_cells": raw_max.wet_cells,
        "raw_wet_area_km2": round(raw_max.wet_area_km2, 6),
        "raw_wet_volume_m3": round(raw_max.wet_volume_m3, 1),
        "raw_patches": raw_max.n_patches,
        # 斑块级判定的删除量（源 res.max 口径，用于对齐计划 §4 的去除率定义）
        "verdict_removed_cells": int(np.count_nonzero(raw_max_removed)),
        "verdict_removed_area_km2": round(
            float(np.count_nonzero(raw_max_removed)) * cell_area_m2 / 1e6, 6),
        "verdict_removed_volume_m3": round(
            float(np.sum(np.where(raw_max_removed, res_max, 0.0))) * cell_area_m2, 1),
        "removed_patches": max_removed_patches,
        "removed_reason_patches": dict(sorted(max_reason_patches.items())),
        # ---- 口径 B：**派生 res.max = 保留帧包络**的实际结果 ----
        # 注意：removed_cells 是「派生包络相对源 res.max 实际减少的像元数」，
        # 它与 verdict_removed_cells 不同：源 res.max 是子步累积包络，含帧间峰，
        # 派生包络只由 6 h 采样帧构成，因此实际减少量含「帧间峰未被采样到」的部分。
        # 两个口径都保留，禁止混用（见 res_max_scope_note）。
        "removed_cells": int(max_removed_cells),
        "removed_area_km2": round(int(max_removed_cells) * cell_area_m2 / 1e6, 6),
        "removed_volume_m3": round(
            float(np.sum(np.where(max_removed, res_max, 0.0))) * cell_area_m2, 1),
        "kept_wet_cells": after_max["wet_cells"],
        "kept_wet_area_km2": round(after_max["wet_area_km2"], 6),
        "kept_wet_volume_m3": round(after_max["wet_volume_m3"], 1),
        "kept_patches": after_max["n_patches"],
        "area_change_pct": round(max_area_change, 4),
        "volume_change_pct": round(max_volume_change, 4),
        "patch_removal_pct": round(
            max_removed_patches / raw_max.n_patches * 100.0, 4
            if raw_max.n_patches else 0.0) if raw_max.n_patches else 0.0,
        "micro_patch_raw": raw_micro,
        "micro_patch_kept": kept_micro,
        "res_max_scope_note": (
            "本段含两套口径，禁止混用："
            "①`verdict_*` = 源 res.max 的斑块级判定删除量，对齐计划 §4 的 4,422 斑块口径，"
            "去除率 patch_removal_pct 即由此得出；"
            "②`removed_*` / `kept_*` / `area_change_pct` / `volume_change_pct` = "
            "**派生 res.max（保留帧包络）**相对源 res.max 的实际变化量。"
            "源 res.max 是子步累积包络、含帧间峰，派生包络只由 6 h 采样帧构成，"
            "故 removed_cells 通常大于 verdict_removed_cells，面积变化率也更大。"
            "两者都是真实测量值，报告时必须分别标注口径。"
        ),
    }

    if not dry_run:
        write_ascii_grid(out_dir / "results" / "res.max", _as_grid(cleaned_max, header))

    # ---- 预注册告警判定 ----
    limit_area = float(PRE_REGISTERED_ALARM["area_change_pct"])
    limit_volume = float(PRE_REGISTERED_ALARM["volume_change_pct"])
    domain = [rec for rec in frame_records if rec["material_frame"]] or frame_records
    worst_area = max(domain, key=lambda rec: rec["area_change_pct"], default=None)
    worst_volume = max(domain, key=lambda rec: rec["volume_change_pct"], default=None)
    max_area_pct = worst_area["area_change_pct"] if worst_area else 0.0
    max_volume_pct = worst_volume["volume_change_pct"] if worst_volume else 0.0

    blocked_reasons: list[str] = []
    if max_area_pct > limit_area:
        blocked_reasons.append(
            f"逐帧湿面积变化率 {max_area_pct:.2f}% 超过预注册上限 {limit_area:.1f}%"
            f"（第 {worst_area['seq']} 帧）"
        )
    if max_volume_pct > limit_volume:
        blocked_reasons.append(
            f"逐帧蓄水量变化率 {max_volume_pct:.2f}% 超过预注册上限 {limit_volume:.1f}%"
            f"（第 {worst_volume['seq']} 帧）"
        )
    if max_area_change > limit_area:
        blocked_reasons.append(
            f"res.max 湿面积变化率 {max_area_change:.2f}% 超过预注册上限 {limit_area:.1f}%"
        )
    if max_volume_change > limit_volume:
        blocked_reasons.append(
            f"res.max 蓄水量变化率 {max_volume_change:.2f}% 超过预注册上限 {limit_volume:.1f}%"
        )

    established = [
        rec for rec in frame_records
        if rec["raw_wet_area_km2"] >= peak_area * 0.5
    ]
    alarm = {
        "pre_registered": PRE_REGISTERED_ALARM,
        "evaluated_domain": (
            "逐帧变化率最大值，评估帧为有量帧（湿面积 >= 峰值帧湿面积 × "
            f"{material_fraction:g}），并含 res.max 峰值场"
        ),
        "material_frame_count": len(domain),
        "max_area_change_pct": max_area_pct,
        "max_area_change_frame": worst_area["seq"] if worst_area else None,
        "max_volume_change_pct": max_volume_pct,
        "max_volume_change_frame": worst_volume["seq"] if worst_volume else None,
        "established_frame_max_area_change_pct": max(
            (rec["area_change_pct"] for rec in established), default=0.0),
        "established_frame_max_volume_change_pct": max(
            (rec["volume_change_pct"] for rec in established), default=0.0),
        "established_frame_count": len(established),
        "res_max": {
            "area_change_pct": round(max_area_change, 4),
            "volume_change_pct": round(max_volume_change, 4),
        },
        "sensitivity_band": {
            f"{band:g}": {
                "exceeded": bool(
                    max_area_pct > band or max_volume_pct > band
                    or max_area_change > band or max_volume_change > band
                )
            }
            for band in PRE_REGISTERED_ALARM["sensitivity_band_pct"]
        },
        "planned_per_tier_pct": PRE_REGISTERED_ALARM["planned_per_tier_pct"][set_id],
        "planned_band_met": bool(
            max_area_change <= PRE_REGISTERED_ALARM["planned_per_tier_pct"][set_id]
            and max_volume_change <= PRE_REGISTERED_ALARM["planned_per_tier_pct"][set_id]
        ),
        "blocked_for_default_publish": bool(blocked_reasons),
        "blocked_reasons": blocked_reasons,
        "verdict_note": (
            "blocked=true 表示该档只能作诊断层，不得默认发布；"
            "不代表源 run 有质量问题"
        ),
    }

    # ---- 计划 §4 验收判定（如实登记达成/未达成） ----
    acceptance = {
        "plan_target_patch_count": plan_acceptance["target_patch_count"],
        "plan_target_patch_removal_pct": plan_acceptance["target_patch_removal_pct"],
        "actual_patch_count": after_max["n_patches"],
        "actual_patch_removal_pct": res_max_record["patch_removal_pct"],
        "patch_target_met": bool(
            after_max["n_patches"] <= plan_acceptance["target_patch_count"]),
        "plan_predicted_removal_pct": params["planned_patch_removal_pct"],
        "deviation_pp": round(
            res_max_record["patch_removal_pct"] - params["planned_patch_removal_pct"], 4),
        "plan_micro_patch_pct": plan_acceptance["baseline_micro_patch_pct"],
        "plan_target_micro_patch_pct": plan_acceptance["target_micro_patch_pct"],
        "actual_micro_patch_pct": kept_micro["micro_pct"],
        "micro_target_met": bool(
            kept_micro["micro_pct"] <= plan_acceptance["target_micro_patch_pct"]),
        "plan_max_volume_change_pct": plan_acceptance["max_volume_change_pct"],
        "actual_volume_change_pct": round(max_volume_change, 4),
        "volume_target_met": bool(
            max_volume_change <= plan_acceptance["max_volume_change_pct"]),
        "main_body_fully_protected": main_body_record["fully_protected"],
        "main_stem_fully_protected": main_stem_record["fully_protected"],
        "main_stem_kept_area_km2": main_stem_record["kept_area_km2"],
        "verdict": (
            "达到计划 §4 验收目标"
            if (after_max["n_patches"] <= plan_acceptance["target_patch_count"]
                and kept_micro["micro_pct"] <= plan_acceptance["target_micro_patch_pct"])
            else "未达到计划 §4 验收目标"
        ),
    }

    verdict_counts = dict(sorted(all_verdicts.items()))
    reason_counts = dict(sorted(all_reasons.items()))

    # ---- 派生层 meta ----
    meta = {
        "derived_from": source_rel,
        "not_physical_recalculation": True,
        "layer_kind": "display_qc",
        "layer_role": "presentation_derived",
        "param_set_id": set_id,
        "param_hash": params["param_hash"],
        "params": params,
        "source_run_sha256": source_hashes.get("res_mass_sha256")
        or source_hashes["frame_list_sha256"],
        "source_run_hash_basis": (
            "results/res.mass 的 sha256" if source_hashes.get("res_mass_sha256")
            else "帧清单（name:sha256 逐行）的 sha256"
        ),
        "source_frame_list_sha256": source_hashes["frame_list_sha256"],
        "source_res_max_sha256": source_hashes["res_max_sha256"],
        "cellsize_m": float(header["cellsize"]),
        "cell_area_m2": float(cell_area_m2),
        "ncols": int(header["ncols"]),
        "nrows": int(header["nrows"]),
        "wet_threshold_m": params["wet_threshold_m"],
        "frame_count": len(frames),
        "saveint_s": params["saveint_s"],
        "res_mass_copied": False,
        "tool": TOOL,
        "run_tag": run_tag,
        "generated_utc": utc_now(),
        "caveats": CAVEATS,
        "main_body_protection": {
            "definition": (
                "候选主汇流区：按面积降序累计覆盖 50% 湿面积的斑块集合；"
                "另加主干水体 >=5 km² 绝对豁免"
            ),
            "main_body_area_km2": main_body_record["raw_area_km2"],
            "fully_protected": main_body_record["fully_protected"],
            "s1_mask_used": False,
            "s1_mask_unusable_reason": S1_MASK_EVIDENCE["why_not_used"],
        },
        "alarm": {
            "blocked_for_default_publish": alarm["blocked_for_default_publish"],
            "blocked_reasons": alarm["blocked_reasons"],
            "max_area_change_pct": alarm["max_area_change_pct"],
            "max_volume_change_pct": alarm["max_volume_change_pct"],
        },
        "acceptance": {
            "verdict": acceptance["verdict"],
            "actual_patch_removal_pct": acceptance["actual_patch_removal_pct"],
            "plan_target_patch_removal_pct": acceptance["plan_target_patch_removal_pct"],
        },
        "rollback": "删除本派生目录即回退；raw run 未被修改",
    }

    if not dry_run:
        write_json(out_dir / "meta.json", meta)
        # 派生层必须自带 sea_mask.npy（若源 run 有）：floodviz 的 read_sea_mask
        # 只读 run 目录下的 sea_mask.npy，缺失时返回 None，海域像元就会混进统计。
        source_sea = Path(source_dir) / "sea_mask.npy"
        if source_sea.is_file():
            np.save(out_dir / "sea_mask.npy", np.load(source_sea))
            meta["sea_mask_copied_from"] = f"{source_rel}/sea_mask.npy"
        else:
            meta["sea_mask_copied_from"] = None
            meta["sea_mask_missing_note"] = (
                "源 run 无 sea_mask.npy；本派生层不含海域掩膜，"
                "下游统计不得声称已剔除海域像元"
            )
        write_json(out_dir / "meta.json", meta)
        _write_frame_csv(out_dir / "audit" / f"frame_audit_{set_id}.csv", frame_records)
        write_json(out_dir / "audit" / f"track_verdicts_{set_id}.json", {
            "param_set_id": set_id,
            "param_hash": params["param_hash"],
            "rule_order": RULE_ORDER,
            "verdict_counts": verdict_counts,
            "reason_counts": reason_counts,
            "records_scope": (
                "逐帧逐斑块判定记录（res.max 峰值场见 res_max 段）；"
                "longest_consecutive_run / persistence_ratio 取该斑块像元上"
                "跨帧最保守的轨迹证据"
            ),
            "patch_verdicts": patch_verdict_records,
        })
        assert_no_forbidden_files(out_dir)

    return {
        "param_set_id": set_id,
        "params": params,
        "param_hash": params["param_hash"],
        "source": params["source"],
        "confidence": params["confidence"],
        "verdict_counts": verdict_counts,
        "reason_counts": reason_counts,
        "alarm": alarm,
        "acceptance": acceptance,
        "res_max": res_max_record,
        "main_body": main_body_record,
        "frames": frame_records,
        "derived_dir": None if dry_run else out_dir.relative_to(PROJ).as_posix(),
        "meta": meta,
    }


def _quick_frame_counts(
    depth: np.ndarray,
    *,
    wet_threshold_m: float,
    cell_area_m2: float,
    sea_mask: np.ndarray | None,
) -> dict[str, Any]:
    """过滤后场的快速统计：只算斑块数与像元/面积/体积，不算逐斑块分位。

    ``patch_analysis.summarize_frame`` 会为每个斑块计算 mean/p95 分位，在贵港
    单帧约 4,000 个斑块的量级上开销极大（实测单次约 6 s）。过滤后场只需要
    “剩多少斑块、多少湿像元、多少体积”，故本函数直接用连通域标签 + bincount，
    与 ``summarize_frame`` 的湿格判定、连通域口径、float32 求和口径**完全一致**，
    仅省略用不到的逐斑块分位统计。
    """
    from scripts.common.patch_analysis import connected_components  # 局部导入

    wet = np.isfinite(depth) & (depth > depth.dtype.type(wet_threshold_m))
    if sea_mask is not None:
        wet &= ~np.asarray(sea_mask, bool)
    labels, n_labels = connected_components(wet, connectivity=8)
    wet_cells = int(np.count_nonzero(wet))
    counts = np.bincount(labels.ravel())[1:] if n_labels else np.empty(0, dtype=np.int64)
    le9 = int(np.count_nonzero(counts <= 9))
    return {
        "wet_cells": wet_cells,
        "wet_area_km2": float(wet_cells) * cell_area_m2 / 1e6,
        "wet_volume_m3": float(np.sum(np.where(wet, depth, 0.0))) * cell_area_m2,
        "n_patches": int(n_labels),
        "n_le_9_cells": le9,
        "micro_pct": round(le9 / n_labels * 100.0, 4) if n_labels else 0.0,
    }


def _write_frame_csv(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not records:
        path.write_text("", encoding="utf-8")
        return
    fields = list(records[0].keys())
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for rec in records:
            writer.writerow({
                key: (json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value)
                for key, value in rec.items()
            })


# --------------------------------------------------------------------------- #
# 验收差距诊断（只诊断，不参与过滤，不修改预注册参数）
# --------------------------------------------------------------------------- #
def _res_max_patch_evidence(
    res_max_summary: Any, context: dict[str, Any], main_labels: set[int]
) -> list[dict[str, Any]]:
    """预计算 res.max 每个斑块的面积/水深与轨迹证据（诊断复用，避免重复计算）。"""
    out: list[dict[str, Any]] = []
    for patch in res_max_summary.patches:
        evidence = _patch_evidence(
            patch, context, is_main_body=patch.label in main_labels)
        out.append({
            "patch": patch,
            "area_km2": float(patch.area_km2),
            "max_depth_m": float(patch.max_depth_m),
            "n_cells": int(patch.n_cells),
            "volume_m3": float(patch.volume_m3),
            "hits_boundary": touches_boundary(patch, context["main_body_cells"].shape),
            **evidence,
        })
    return out


def acceptance_gap_diagnostic(
    *,
    entries: list[dict[str, Any]],
    cell_area_m2: float,
    params_by_set: dict[str, dict[str, Any]],
    raw_patches: int,
    raw_area_km2: float,
    raw_volume_m3: float,
    target_patch_count: int,
    deep_exemption_m: float,
    main_stem_area_km2: float,
) -> dict[str, Any]:
    """量化“计划 §4 的 >=90% 去除目标”与“计划 §3 规则结构”之间的差距。

    只诊断：不参与过滤，不写任何派生栅格，也不修改预注册三档参数。
    """
    def evaluate(area_cap: float, depth_cap: float, pfr: int, mpr: float,
                 use_persist: bool, deep: float | None) -> dict[str, Any]:
        removed = 0
        removed_cells = 0
        removed_volume = 0.0
        for entry in entries:
            if entry["is_main_body"] or entry["area_km2"] >= main_stem_area_km2:
                continue
            if deep is not None and entry["max_depth_m"] >= deep:
                continue
            if entry["hits_boundary"] or entry["is_recession_growth"]:
                continue
            drop = False
            if entry["area_km2"] <= area_cap and entry["max_depth_m"] <= depth_cap:
                drop = True
            elif use_persist and (
                entry["longest_consecutive_run"] < pfr
                or entry["persistence_ratio"] < mpr
            ):
                drop = True
            if drop:
                removed += 1
                removed_cells += entry["n_cells"]
                removed_volume += entry["volume_m3"]
        area_km2 = removed_cells * cell_area_m2 / 1e6
        return {
            "area_cap_km2": area_cap,
            "depth_cap_m": depth_cap,
            "deep_exemption_m": deep,
            "removed_patches": removed,
            "patch_removal_pct": round(removed / raw_patches * 100.0, 4) if raw_patches else 0.0,
            "removed_area_km2": round(area_km2, 4),
            "area_change_pct": round(area_km2 / raw_area_km2 * 100.0, 4) if raw_area_km2 else 0.0,
            "volume_change_pct": (
                round(removed_volume / raw_volume_m3 * 100.0, 4) if raw_volume_m3 else 0.0
            ),
        }

    # 计划 §3 第 2 条上限：max_depth >= 0.50 m 全豁免 → 可去除集合的理论上界
    ceiling_patches = [
        e for e in entries
        if not (e["is_main_body"] or e["area_km2"] >= main_stem_area_km2)
        and e["max_depth_m"] < deep_exemption_m
        and not e["hits_boundary"] and not e["is_recession_growth"]
    ]
    ceiling = {
        "removable_patches_upper_bound": len(ceiling_patches),
        "removable_pct_upper_bound": (
            round(len(ceiling_patches) / raw_patches * 100.0, 4) if raw_patches else 0.0
        ),
        "explanation": (
            "计划 §3 第 2 条把 max_depth >= 0.50 m 的斑块全部豁免，因此任何档位"
            "最多只能去除 max_depth < 0.50 m 的斑块；这就是计划自身规则结构给出的"
            "去除率理论上界，与计划 §4 的 >=90% 目标直接冲突。"
        ),
    }

    # 计划给定三档在“含第 4 条持久性规则”下的实测去除（与主流程一致）
    pre_registered = {}
    for set_id, params in params_by_set.items():
        pre_registered[set_id] = evaluate(
            float(params["shallow_max_area_km2"]),
            float(params["shallow_depth_m"]),
            int(params["persistence_frames"]),
            float(params["min_persistence_ratio"]),
            True,
            deep_exemption_m,
        )

    # 只保留持久性规则（关掉面积/水深规则）时的去除率
    persistence_only = {}
    for set_id, params in params_by_set.items():
        persistence_only[set_id] = evaluate(
            0.0,
            0.0,
            int(params["persistence_frames"]),
            float(params["min_persistence_ratio"]),
            True,
            deep_exemption_m,
        )

    # 达到 >=90% 所需的阈值扫描（仅诊断，不是预注册档位）
    frontier = []
    for area_cap in ACCEPTANCE_GAP_DIAGNOSTIC["area_caps_km2"]:
        for depth_cap in ACCEPTANCE_GAP_DIAGNOSTIC["depth_caps_m"]:
            rec = evaluate(
                area_cap, depth_cap, 12, 12.0 / 57.0, True, None)
            rec["meets_target"] = rec["patch_removal_pct"] >= 89.82
            frontier.append(rec)

    feasible = [rec for rec in frontier if rec["meets_target"]]
    feasible.sort(key=lambda rec: (rec["depth_cap_m"], rec["area_cap_km2"]))
    best = feasible[0] if feasible else None

    return {
        **{k: v for k, v in ACCEPTANCE_GAP_DIAGNOSTIC.items()},
        "raw_patch_count": raw_patches,
        "plan_target_patch_count": target_patch_count,
        "ceiling_under_plan_rules": ceiling,
        "pre_registered_actual": pre_registered,
        "persistence_only_removal": persistence_only,
        "frontier_sweep": frontier,
        "minimal_change_to_meet_target": (
            {
                "area_cap_km2": best["area_cap_km2"],
                "depth_cap_m": best["depth_cap_m"],
                "deep_exemption_disabled": True,
                "patch_removal_pct": best["patch_removal_pct"],
                "volume_change_pct": best["volume_change_pct"],
                "note": (
                    "该组合可在 res.max 上把斑块数降到计划目标以内，但它"
                    "①放宽了水深上限（计划给的是 0.15/0.30/0.50 m）；"
                    "②关闭了计划 §3 第 2 条的深水豁免，会把真实的深水坑洼一并删除。"
                    "属参数变更，须重新预注册并经用户批准，不得作为本次交付结论。"
                ),
            }
            if best else None
        ),
        "conclusion": (
            "计划 §3 给定三档参数下，实测斑块去除率约 20~26%，未达到计划 §4 的 "
            ">=90% 验收目标。原因是贵港离散斑块并非浅水而是 DEM 凹坑中的深水"
            "（<=9 格斑块 max_depth 中位数约 1.03 m），且计划 §3 第 2 条本身就把"
            "max_depth >= 0.50 m 全部豁免，去除率存在结构性上界"
            f"（实测 {ceiling['removable_pct_upper_bound']:.2f}%，"
            f"即 {ceiling['removable_patches_upper_bound']} 个斑块）。"
            "不得为提高去除率而修改预注册阈值。"
        ),
    }


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def resolve_run_dir(run_id: str) -> Path:
    run_dir = CASE_DIR / run_id
    if not run_dir.is_dir():
        raise GridError(f"未找到 run 目录：{run_dir}")
    return run_dir


def run(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = resolve_run_dir(args.run_id)
    res_dir = run_dir / "results"
    frames = run_frames(res_dir)
    if not frames:
        raise GridError(f"{res_dir} 下没有 res-*.wd")

    res_max_path = res_dir / "res.max"
    if not res_max_path.is_file():
        raise GridError(f"{res_max_path} 缺失；无法生成峰值场派生层")

    header = read_ascii_header(res_max_path)
    cellsize = float(header["cellsize"])
    cell_area = cellsize * cellsize
    shape = (int(header["nrows"]), int(header["ncols"]))
    par = parse_run_par(run_dir)
    saveint = float(par.get("saveint") or 0.0)
    if saveint <= 0:
        raise GridError(f"{run_dir}/run.par 缺 saveint；帧时间口径无法确定")

    sea_mask = load_sea_mask(run_dir, shape)
    if sea_mask is None:
        sea_mask = np.zeros(shape, dtype=bool)

    display_run_id = args.display_run_id or DEFAULT_DISPLAY_RUN_ID
    out_root = Path(args.out_root) if args.out_root else DISPLAY_QC_ROOT
    audit_path = Path(args.audit_path) if args.audit_path else AUDIT_PATH
    _assert_outside_raw_runs(out_root)

    log = AuditLog(TOOL, run_tag=args.run_tag, dry_run=bool(args.dry_run))
    log.add_input("res_max", res_max_path)
    log.add_input("run_par", run_dir / "run.par")
    log.add_input("sea_mask", run_dir / "sea_mask.npy", hash_file=False)
    log.add_input_dir("results_frames", res_dir, "res-*.wd")
    log.param(
        run_id=args.run_id,
        display_run_id=display_run_id,
        cellsize_m=cellsize,
        cell_area_m2=cell_area,
        shape=list(shape),
        saveint_s=saveint,
        wet_threshold_m=WET_THRESHOLD_M,
        param_sets=list(args.param_sets),
        out_root=str(out_root),
    )

    before = hash_source_run(run_dir, frames)
    log.step("source_hash_before", frame_count=before["frame_count"],
             res_max_sha256=before["res_max_sha256"])

    # ---- 读帧 + 原始斑块统计（三档共用，避免重复计算） ----
    frame_grids: dict[int, np.ndarray] = {}
    summaries: list[Any] = []
    for seq, path in frames:
        grid = read_ascii_grid(path, expected_shape=shape)
        frame_grids[seq] = grid.data
        summaries.append(summarize_frame(
            grid.data, seq,
            wet_threshold_m=WET_THRESHOLD_M,
            cell_area_m2=cell_area,
            sea_mask=sea_mask,
        ))
    peak_seq = max(summaries, key=lambda s: s.wet_area_km2).seq
    log.step("raw_summaries", frame_count=len(summaries), peak_seq=peak_seq)

    # ---- 轨迹追踪（复用 patch_analysis.PersistenceTracker） ----
    tracker = PersistenceTracker()
    owner_by_frame: dict[int, dict[int, int]] = {}
    for summary in summaries:
        tracker.update(summary, width=shape[1])
        owner_by_frame[summary.seq] = assert_tracker_contract(tracker)
    tracks = tracker.summaries()

    n_frames = len(summaries)
    context = build_track_context(
        summaries, owner_by_frame, tracks,
        shape=shape, peak_seq=peak_seq, n_frames=n_frames,
    )
    log.step("track_context", tracks=len(tracks), peak_seq=peak_seq,
             main_body_cells=int(np.count_nonzero(context["main_body_cells"])),
             main_body_tracks=len(context["main_body_tracks"]),
             boundary_tracks=len(context["boundary_tracks"]),
             recession_growth_tracks=sum(1 for v in context["growth"].values() if v))

    res_max = read_ascii_grid(res_max_path, expected_shape=shape).data
    res_max_summary = summarize_frame(
        res_max, -1, wet_threshold_m=WET_THRESHOLD_M, cell_area_m2=cell_area,
        sea_mask=sea_mask,
    )
    res_max_main_labels = main_body_patch_labels(res_max_summary, MAIN_BODY_AREA_FRACTION)
    source_rel = run_dir.relative_to(PROJ).as_posix()

    params_by_set: dict[str, dict[str, Any]] = {}
    for set_id in args.param_sets:
        if set_id not in PRE_REGISTERED_PARAM_SETS:
            raise GridError(
                f"未预注册的参数档 {set_id}；可选 {sorted(PRE_REGISTERED_PARAM_SETS)}"
            )
        params_by_set[set_id] = resolve_param_set(
            PRE_REGISTERED_PARAM_SETS[set_id],
            cell_area_m2=cell_area,
            cellsize_m=cellsize,
            saveint_s=saveint,
            n_frames=n_frames,
        )

    results: dict[str, Any] = {}
    for set_id, params in params_by_set.items():
        results[set_id] = process_param_set(
            set_id=set_id,
            params=params,
            out_dir=out_root / display_run_id / set_id,
            shape=shape,
            cell_area_m2=cell_area,
            header=header,
            sea_mask=sea_mask,
            frames=frames,
            frame_grids=frame_grids,
            summaries=summaries,
            context=context,
            res_max=res_max,
            res_max_summary=res_max_summary,
            res_max_main_labels=res_max_main_labels,
            dry_run=bool(args.dry_run),
            source_rel=source_rel,
            source_dir=run_dir,
            source_hashes=before,
            run_tag=args.run_tag,
            plan_acceptance=PLAN_ACCEPTANCE,
        )
        log.step(
            f"param_set_{set_id}",
            param_hash=params["param_hash"],
            verdict_counts=results[set_id]["verdict_counts"],
            patch_removal_pct=results[set_id]["res_max"]["patch_removal_pct"],
            max_area_change_pct=results[set_id]["alarm"]["max_area_change_pct"],
            max_volume_change_pct=results[set_id]["alarm"]["max_volume_change_pct"],
            blocked=results[set_id]["alarm"]["blocked_for_default_publish"],
        )

    after = hash_source_run(run_dir, frames)
    integrity = compare_source_hashes(before, after)
    log.step("source_hash_after", unchanged=integrity["unchanged"])
    if not integrity["unchanged"]:
        log.error(f"源 run 哈希发生变化：{integrity}")
        raise RuntimeError(f"源 run 在运行期间被修改，违反只读约束：{integrity}")

    # ---- 验收差距诊断（只诊断） ----
    entries = _res_max_patch_evidence(res_max_summary, context, res_max_main_labels)
    gap = acceptance_gap_diagnostic(
        entries=entries,
        cell_area_m2=cell_area,
        params_by_set=params_by_set,
        raw_patches=res_max_summary.n_patches,
        raw_area_km2=res_max_summary.wet_area_km2,
        raw_volume_m3=res_max_summary.wet_volume_m3,
        target_patch_count=PLAN_ACCEPTANCE["target_patch_count"],
        deep_exemption_m=DEEP_DEPTH_THRESHOLD_M,
        main_stem_area_km2=MAIN_STEM_MIN_AREA_KM2,
    )

    # ---- 深度分布诊断（解释为何“面积+水深”滤不掉离散斑块） ----
    small_patch_depths = np.array([
        float(p.max_depth_m) for p in res_max_summary.patches if p.n_cells <= 9
    ], dtype=np.float64)
    depth_profile = {
        "le_9_cell_patch_count": int(small_patch_depths.size),
        "le_9_cell_max_depth_quantiles_m": {
            f"p{q:g}": round(float(np.percentile(small_patch_depths, q)), 4)
            for q in (10, 25, 50, 75, 90, 99)
        } if small_patch_depths.size else {},
        "le_9_cell_max_depth_max_m": (
            round(float(small_patch_depths.max()), 4) if small_patch_depths.size else 0.0
        ),
        "le_9_cell_within_tier_depth_caps": {
            set_id: int(np.count_nonzero(
                small_patch_depths <= float(params_by_set[set_id]["shallow_depth_m"])))
            for set_id in params_by_set
        },
        "explanation": (
            "贵港离散斑块不是浅水，而是 DEM 凹坑中的深水；因此仅靠"
            "“面积 + 水深”合取只能覆盖很小一部分斑块。"
        ),
    }

    audit = {
        "script": TOOL,
        "generated_utc": utc_now(),
        "run_tag": args.run_tag,
        "dry_run": bool(args.dry_run),
        "not_physical_recalculation": True,
        "layer_kind": "display_qc",
        "caveats": CAVEATS,
        "scope": {
            "source_run": source_rel,
            "display_run_id": display_run_id,
            "resolution_policy": "贵港 100 m 生产情景；本层只处理该网格",
            "grid": {
                "ncols": int(header["ncols"]),
                "nrows": int(header["nrows"]),
                "cellsize_m": cellsize,
                "cell_area_m2": cell_area,
                "xllcorner": float(header["xllcorner"]),
                "yllcorner": float(header["yllcorner"]),
                "nodata": float(header["nodata_value"]),
            },
            "temporal": {
                "saveint_s": saveint,
                "sim_time_s": par.get("sim_time"),
                "frame_count": len(frames),
                "first_seq": frames[0][0],
                "last_seq": frames[-1][0],
                "peak_wet_area_seq": peak_seq,
            },
            "wet_threshold_m": WET_THRESHOLD_M,
            "sea_cells": int(np.count_nonzero(sea_mask)),
            "track_association": (
                "scripts/common/patch_analysis.PersistenceTracker（像元重叠继承）；"
                "本脚本读取其 _cell_owner 内部状态把轨迹证据回写到斑块，"
                "该契约由 tests/test_guigang_display_qc.py 守护"
            ),
        },
        "rule_order": RULE_ORDER,
        "reason_codes": {
            "exempt": list(EXEMPT_REASONS),
            "filter": list(FILTER_REASONS),
            "keep": list(KEEP_REASONS),
        },
        "threshold_conversion": {
            "formula": (
                "n_cells = ceil(area_threshold_m2 / cell_area_m2)；"
                "persistence_frames = ceil(persistence_hours * 3600 / saveint_s)；"
                "min_persistence_ratio = persistence_frames / n_frames"
            ),
            "cell_area_source": "栅格头 cellsize ** 2（不写死像元面积）",
            "note": "像元数是换算结果，不是阈值本身",
        },
        "pre_registration": {
            "frozen_before_production_run": True,
            "frozen_at_utc": PRE_REGISTRATION_UTC,
            "basis": (
                "三档面积/水深上限直接取贵港计划 §3 Phase 1 表格原值"
                "（0.04/0.09/0.25 km² 与 0.15/0.30/0.50 m），未做任何修改；"
                "持续性梯度 36/72/144 h 沿用久隆已验证实现"
                "（计划 §3 原文要求“采用久隆验证的三级复合规则”）；"
                "全部为项目候选参数，未经本地率定，置信度 E3 候选假设"
            ),
            "rule_structure_basis": (
                "计划 §3 Phase 1 第 1–4 条：主干水系保护 → 显著深水洼地保护 → "
                "微型离散浅水斑块 → 时空持久性；豁免先于过滤"
            ),
            "forbidden_attribution": (
                "不归因于 ARR 2019、UK EA 或任何国际规范的强制值"
            ),
        },
        "param_sets": results,
        "main_body_protection": {
            "definition": (
                "候选主汇流区：按面积降序累计覆盖 50% 湿面积的斑块集合"
                "（久隆已验证定义），另加计划 §3 第 1 条主干水体 >=5 km² 绝对豁免"
            ),
            "s1_mask_evidence": S1_MASK_EVIDENCE,
            "res_max_main_body": {
                "labels": sorted(int(x) for x in res_max_main_labels),
                "cells": int(np.count_nonzero(context["main_body_cells"])),
                "area_km2": round(
                    int(np.count_nonzero(context["main_body_cells"]))
                    * cell_area / 1e6, 4),
                "plan_reference_area_km2": PLAN_ACCEPTANCE["main_body_area_km2"],
            },
            "per_tier": {
                set_id: results[set_id]["main_body"] for set_id in results
            },
        },
        "plan_acceptance": PLAN_ACCEPTANCE,
        "acceptance_gap": gap,
        "small_patch_depth_profile": depth_profile,
        "raw_integrity": {
            "before": {k: v for k, v in before.items() if k != "frames"},
            "after": {k: v for k, v in after.items() if k != "frames"},
            "unchanged": integrity["unchanged"],
            "changed_frames": integrity["changed_frames"],
            "method": "脚本运行前后各计算一次 sha256；任一差异即抛错",
        },
        "derived_layer_contract": {
            "path": f"outputs/display_qc/guigang/{EVENT_ID}/{display_run_id}/<param_set>/",
            "results_dir_contains": "res-XXXX.wd 与 res.max",
            "res_mass_copied": False,
            "why_no_res_mass": (
                "scripts/stage3/g15_mass_recheck.py 用 rglob('results/res.mass') 扫描；"
                "派生目录带 res.mass 会被误计为独立 run 并污染质量门计数"
            ),
            "rollback": "删除派生目录即回退；raw run 未被修改",
        },
        "blocked_summary": {
            set_id: results[set_id]["alarm"]["blocked_for_default_publish"]
            for set_id in results
        },
        "log_steps": log.payload["steps"],
        "log_warnings": log.payload["warnings"],
        "log_errors": log.payload["errors"],
    }

    if not args.dry_run:
        write_json(audit_path, audit)
    if args.log_json:
        write_json(args.log_json, log.payload)
    return audit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="贵港 100 m 基线只读展示质控（display_qc 派生层 + 逐帧审计）",
    )
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID,
                        help=f"源 run id（默认 {DEFAULT_RUN_ID}）")
    parser.add_argument("--display-run-id", default=None,
                        help=f"派生目录名（默认 {DEFAULT_DISPLAY_RUN_ID}）")
    parser.add_argument("--param-sets", default="A,B,C",
                        help="预注册参数档，逗号分隔（默认 A,B,C）")
    parser.add_argument("--out-root", type=Path, default=None,
                        help="派生层根目录（默认 outputs/display_qc/guigang/2024-09-yagi）")
    parser.add_argument("--audit-path", type=Path, default=None,
                        help="审计 JSON 路径（默认 data/catalog/guigang_display_qc_audit.json）")
    add_common_args(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.param_sets = [item.strip().upper() for item in args.param_sets.split(",") if item.strip()]
    audit = run(args)
    for set_id, rec in audit["param_sets"].items():
        alarm = rec["alarm"]
        rmax = rec["res_max"]
        print(
            f"[{set_id}] patches={rmax['raw_patches']}->{rmax['kept_patches']} "
            f"removal={rmax['patch_removal_pct']:.1f}% "
            f"area_change={rmax['area_change_pct']:.2f}% "
            f"vol_change={rmax['volume_change_pct']:.3f}% "
            f"blocked={alarm['blocked_for_default_publish']} "
            f"acceptance={rec['acceptance']['verdict']}"
        )
    print(f"raw_unchanged={audit['raw_integrity']['unchanged']} dry_run={audit['dry_run']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
