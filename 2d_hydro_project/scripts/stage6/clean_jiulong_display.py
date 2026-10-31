"""Phase 1：久隆 200 m 生产基线的**只读展示质控**派生层（display_qc）与逐帧审计。

定位（计划 §3.3 / §5 Phase 1 / §7.1）
------------------------------------
本脚本生成的是**展示派生层**：它把源 run 的水深场按预注册规则做展示口径处理，
不改写源 run 的任何文件，也不产生任何新的物理结果。派生层始终带
``not_physical_recalculation=true``，不得表述为“校准”“真实淹没”或“物理修复”。

关键设计（对应计划 §1.4 / §3.3）
-------------------------------
1. **单元面积从栅格头读取**：``cell_area_m2 = cellsize ** 2``。脚本不含任何
   写死的单元面积常量，也不含“4/9 像元”这类写死阈值。
2. **物理阈值优先，像元数是换算结果**：面积阈值用 km² 登记，像元数由
   ``n_cells = ceil(area_threshold_m2 / cell_area_m2)`` 现算；持续性阈值用小时
   登记，帧数由 ``ceil(hours * 3600 / saveint)`` 现算。同一参数档在 200 m 与
   100 m 网格上得到不同像元数，这正是计划 §1.4 要求的口径。
3. **规则顺序：先豁免，后过滤**（计划 §3.3 第 4–5 条）。任一豁免条件成立即保留；
   只有同时满足“小、短暂、孤立、浅水”的斑块才进入过滤候选（第 4 条原文是
   “小且短暂、孤立、浅水”，故取合取）。
4. **参数预注册**：A/B/C 三档写在模块常量里，处理前解析成含像元换算的完整
   参数 dict 并计算 ``param_hash``；不允许按结果事后挑选。
5. **预注册告警**：任一档导致湿面积或体积变化率超过预注册阈值，该档
   ``blocked_for_default_publish=true``，只能作诊断层，不得默认发布（§9.2）。

硬约束
------
- 禁止修改 ``outputs/jiulong/**``：脚本前后各算一次源 run 哈希并比对，不一致即报错。
- 派生目录**不得包含** ``results/res.mass``：``scripts/stage3/g15_mass_recheck.py``
  用 ``rglob("results/res.mass")`` 扫描，派生目录若带 res.mass 会被误计为独立 run。
- 写盘前经 ``guard_no_overwrite``；目标目录已有 res.mass 直接退出。
- 不重跑 ``lisflood.exe``，不读写 ``models/``。

用法
----
    uv --directory 2d_hydro_project run python \\
        scripts/stage6/clean_jiulong_display.py \\
        --run-id scen_free_200m_sv6h_recession --run-tag display-qc-v1 --dry-run
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

CASE_DIR = PROJ / "outputs" / "jiulong" / "2024-09-yagi"
DISPLAY_QC_ROOT = PROJ / "outputs" / "display_qc"
AUDIT_PATH = PROJ / "data" / "catalog" / "jiulong_display_qc_audit.json"
DEFAULT_RUN_ID = "scen_free_200m_sv6h_recession"
CROSS_RES_RUN_ID = "scen_free_100m_dt2p5_sv6h_realdem"
TOOL = "scripts/stage6/clean_jiulong_display.py"

#: 参数档冻结时刻（处理前登记的证据，写入审计 JSON）。
PRE_REGISTRATION_UTC = "2026-10-31T13:00:00+00:00"

#: 湿格判定口径。与项目既有统计（floodviz、products 报告）一致：
#: ``depth > 0.05 m`` 为湿，严格大于。E1（仓库内多处可复核）。
WET_THRESHOLD_M = 0.05

#: 主水体判定：每帧按面积降序累加，累计覆盖该帧湿面积 >= 该比例的斑块集合
#: 视为该帧的“候选主汇流区”。计划 §3.3 第 3 条要求“与候选主汇流区的距离”，
#: 本常量即其定义。E3 候选假设（项目口径，非外部规范）。
MAIN_BODY_AREA_FRACTION = 0.5

#: 退水增长豁免：全局峰值帧之后，轨迹面积相对峰值帧增长超过该比例，
#: 视为“对退水异常有贡献”的斑块。计划 §3.5 要求退水异常不得用滤波掩盖，
#: 故这类斑块必须豁免而不是过滤。E3 候选假设。
RECESSION_GROWTH_RATIO = 0.10

#: 预注册参数档。面积单位 km²，深度单位 m，持续性单位**小时**。
#: 来源统一为“项目候选参数，未经本地率定”，置信度 E3 候选假设（计划 §2.4）。
#: 三档面积量级取 200 m 网格上的 2 / 4 / 9 个像元（0.08 / 0.16 / 0.36 km²），
#: 但在 100 m 网格上同一阈值分别换算为 8 / 16 / 36 个像元 —— 说明“像元数”
#: 不是跨分辨率稳定的量（计划 §1.4）。
PRE_REGISTERED_PARAM_SETS: dict[str, dict[str, Any]] = {
    "A": {
        "label": "A 保守档",
        "intent": "只清除明显亚网格碎斑，尽量不动可解释积水",
        "min_patch_area_km2": 0.08,
        "persistence_hours": 36.0,
        "shallow_depth_m": 0.10,
        "deep_depth_m": 0.30,
        "exemption_area_km2": 0.40,
        "evidence_area_km2": 0.16,
        "shallow_max_area_km2": 0.16,
        "isolation_min_distance_cells": 1,
    },
    "B": {
        "label": "B 平衡档",
        "intent": "清除亚网格碎斑与短暂浅水片流，保留全部成片积水",
        "min_patch_area_km2": 0.16,
        "persistence_hours": 72.0,
        "shallow_depth_m": 0.15,
        "deep_depth_m": 0.45,
        "exemption_area_km2": 0.80,
        "evidence_area_km2": 0.16,
        "shallow_max_area_km2": 0.24,
        "isolation_min_distance_cells": 1,
    },
    "C": {
        "label": "C 激进档",
        "intent": "压制观感噪点的上限档，用于检验阈值敏感性与告警边界",
        "min_patch_area_km2": 0.36,
        "persistence_hours": 144.0,
        "shallow_depth_m": 0.20,
        "deep_depth_m": 0.60,
        "exemption_area_km2": 1.60,
        "evidence_area_km2": 0.36,
        "shallow_max_area_km2": 0.36,
        "isolation_min_distance_cells": 1,
    },
}

#: 预注册告警阈值（计划 §3.3 参数冻结规则末条 / §5 Phase 1 验收）。
#: 判定域：逐帧变化率的最大值，评估帧为“有量帧”（湿面积 >= 峰值帧湿面积的
#: material_frame_fraction 倍），并同时评估 res.max 峰值场。超限即 blocked。
PRE_REGISTERED_ALARM: dict[str, Any] = {
    "area_change_pct": 10.0,
    "volume_change_pct": 10.0,
    "material_frame_fraction": 0.05,
    "sensitivity_band_pct": [5.0, 10.0, 15.0, 20.0],
    "basis": (
        "项目候选阈值：展示层若使报出的湿面积或蓄水量相对源 run 变化超过该比例，"
        "则该档只能作诊断层，不得默认发布。非外部规范数值。"
    ),
    "confidence": "E3 候选假设",
}

#: 规则顺序（写入审计，供复核）。计划 §3.3 第 1–7 条。
RULE_ORDER = [
    "R1 像元分类：nodata / 海域 / 干格 / 有效浅水 / 有效深水",
    "R2 斑块面积按栅格头 cell_area_m2 现算（不写死像元面积）",
    "R3 斑块统计：面积、最大/均值/分位水深、出现帧数、连续帧数、到候选主汇流区距离",
    "R5 豁免优先：与主水体连通 / 超大面积 / 持续存在 / 深度较大 / 对退水增长有贡献 / 触域边界",
    "R4 过滤候选：同时满足小、短暂、孤立、浅水",
    "R6 A/B/C 三档参数处理前登记，像元数由物理面积换算",
    "R7 逐帧写出被过滤像元、面积、体积、斑块数与原因码",
]

EXEMPT_REASONS = (
    "EXEMPT_MAIN_BODY",
    "EXEMPT_LARGE_AREA",
    "EXEMPT_PERSISTENT",
    "EXEMPT_DEEP",
    "EXEMPT_RECESSION_GROWTH",
    "EXEMPT_BOUNDARY_OUTLET",
)
FILTER_REASONS = ("FILTER_SUBRESOLUTION", "FILTER_SHALLOW")
KEEP_REASONS = ("KEEP_OTHER",)

CAVEATS = [
    "展示派生层，不是物理重算，不得用于业务告警。",
    "被过滤像元水深置 0 仅为展示与统计口径处理；源 run 文件逐字节未改动。",
    "阈值属项目候选参数，未经本地率定，置信度 E3 候选假设；不归因于任何国际规范。",
    "200 m 为生产基线（方案 A）；100 m 仅为敏感性参照，本层未处理 100 m 结果。",
    "退水期湿面积增长等已知异常未被本层修复，仅在审计中登记，不得以滤波掩盖。",
    "本层不得表述为“校准”“真实淹没”或“物理修复”；raw 层保留且可一键回退。",
    "展示质控只能改善观感与展示统计口径，不能提高模型精度，也不能作为精度证明。",
]

# 派生目录禁止出现的文件名（g15 质量门按 results/res.mass 识别独立 run）
FORBIDDEN_DERIVED_NAMES = ("res.mass",)

#: 旧实现 ``scripts/stage6/clean_rain_on_grid.py`` 的写死阈值，仅用于**诊断对照**：
#: 本脚本不复用该实现，也不把这些数字当作阈值。审计里用它量化“在 200 m 上直接套
#: 100 m 像元规则”的后果（计划 §1.4 / §8 风险表第 1 行）。
LEGACY_PIXEL_RULE_DIAGNOSTIC = {
    "source": "scripts/stage6/clean_rain_on_grid.py（旧实现，本脚本不复用）",
    "rule_1_min_patch_cells": 4,
    "rule_2_shallow_patch_cells": 9,
    "rule_2_shallow_threshold_m": 0.15,
    "why_not_used": (
        "写死像元数，只在 100 m 成立；在 200 m 上 4 格 = 0.16 km²、9 格 = 0.36 km²，"
        "会把四倍面积的斑块误判为同一类对象（计划 §1.4）"
    ),
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
) -> dict[str, Any]:
    """把预注册参数档解析成含像元/帧数换算结果的完整参数 dict（含 param_hash）。"""
    resolved: dict[str, Any] = {
        "label": spec["label"],
        "intent": spec["intent"],
        "wet_threshold_m": WET_THRESHOLD_M,
        "cellsize_m": float(cellsize_m),
        "cell_area_m2": float(cell_area_m2),
        "min_patch_area_km2": float(spec["min_patch_area_km2"]),
        "n_cells_min_patch_area": cells_for_area(spec["min_patch_area_km2"], cell_area_m2),
        "persistence_hours": float(spec["persistence_hours"]),
        "persistence_frames": frames_for_persistence(spec["persistence_hours"], saveint_s),
        "saveint_s": float(saveint_s),
        "shallow_depth_m": float(spec["shallow_depth_m"]),
        "deep_depth_m": float(spec["deep_depth_m"]),
        "exemption_area_km2": float(spec["exemption_area_km2"]),
        "n_cells_exemption_area": cells_for_area(spec["exemption_area_km2"], cell_area_m2),
        "evidence_area_km2": float(spec["evidence_area_km2"]),
        "n_cells_evidence_area": cells_for_area(spec["evidence_area_km2"], cell_area_m2),
        "shallow_max_area_km2": float(spec["shallow_max_area_km2"]),
        "n_cells_shallow_max_area": cells_for_area(spec["shallow_max_area_km2"], cell_area_m2),
        "isolation_min_distance_cells": int(spec["isolation_min_distance_cells"]),
        "main_body_area_fraction": MAIN_BODY_AREA_FRACTION,
        "recession_growth_ratio": RECESSION_GROWTH_RATIO,
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
    """按计划 §3.3 第 1 条区分 nodata / 海域 / 干格 / 有效浅水 / 有效深水。

    **不得升位到 float64**：栅格以 float32 存储，``float32(0.05)`` 实际为
    ``0.05000000074505806``。升到 float64 后 ``> 0.05`` 会把恰好写成 0.050 的像元
    判成湿，与 ``patch_analysis.summarize_frame`` 的 float32 口径不一致，导致
    逐帧审计自相矛盾。本函数保持输入 dtype，与 summarize_frame 完全同口径。
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
# 规则 R3：到候选主汇流区的距离场
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
    """斑块是否触及模型域边界（候选出流口，第 5 条“可信排水路径”的代理）。"""
    return (
        patch.row_min == 0
        or patch.col_min == 0
        or patch.row_max == shape[0] - 1
        or patch.col_max == shape[1] - 1
    )


# --------------------------------------------------------------------------- #
# 规则 R5 / R4：先豁免，后过滤
# --------------------------------------------------------------------------- #
def classify_track(
    track: dict[str, Any],
    params: dict[str, Any],
    *,
    is_main_body: bool,
    is_recession_growth: bool,
    hits_boundary: bool,
    min_distance_cells: int,
) -> tuple[str, str]:
    """轨迹分类，返回 ``(verdict, reason_code)``。

    verdict ∈ {``exempt``, ``filter``, ``keep``}。

    顺序严格按计划 §3.3：先判第 5 条豁免（与主水体连通、超大面积、持续存在、
    深度较大、对退水增长有贡献、位于候选排水路径），全部不成立后才判第 4 条
    过滤（小且短暂、孤立、浅水）。因此任何豁免条件成立都不会被过滤。
    """
    area = float(track["max_area_km2"])
    run = int(track["longest_consecutive_run"])
    depth = float(track["max_depth_m"])
    persist = int(params["persistence_frames"])
    evidence_area = float(params["evidence_area_km2"])

    # ---- R5 豁免（顺序即优先级） ----
    if is_main_body:
        return "exempt", "EXEMPT_MAIN_BODY"
    if area >= float(params["exemption_area_km2"]):
        return "exempt", "EXEMPT_LARGE_AREA"
    if run >= persist and area >= evidence_area:
        return "exempt", "EXEMPT_PERSISTENT"
    if depth >= float(params["deep_depth_m"]) and area >= evidence_area:
        return "exempt", "EXEMPT_DEEP"
    if is_recession_growth:
        return "exempt", "EXEMPT_RECESSION_GROWTH"
    if hits_boundary:
        return "exempt", "EXEMPT_BOUNDARY_OUTLET"

    # ---- R4 过滤候选：小 且 短暂 且 孤立 且 浅 ----
    brief = run < persist
    small = area < float(params["min_patch_area_km2"])
    isolated = min_distance_cells > int(params["isolation_min_distance_cells"])
    if small and brief and isolated and depth < float(params["deep_depth_m"]):
        return "filter", "FILTER_SUBRESOLUTION"
    if (
        depth < float(params["shallow_depth_m"])
        and brief
        and isolated
        and area < float(params["shallow_max_area_km2"])
    ):
        return "filter", "FILTER_SHALLOW"
    return "keep", "KEEP_OTHER"


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
    """派生目录不得落在受保护的 raw run 目录内（禁止修改 outputs/jiulong/**）。"""
    protected = (PROJ / "outputs" / "jiulong").resolve()
    target = out_dir.resolve()
    if target == protected or protected in target.parents:
        raise RuntimeError(
            f"拒绝写入受保护目录 {target}：禁止修改 outputs/jiulong/** 下任何文件"
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

    Lead 的 ``patch_analysis.PersistenceTracker`` 只提供 ``update``/``summaries``，
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
# 诊断：旧写死像元规则在 200 m 上的后果（计划 §1.4 / §8）
# --------------------------------------------------------------------------- #
def legacy_pixel_rule_diagnostic(
    summaries: list[Any],
    owner_by_frame: dict[int, dict[int, int]],
    *,
    cell_area_m2: float,
) -> dict[str, Any]:
    """量化“在 200 m 上直接套用旧 4/9 像元规则”的后果（只诊断，不参与过滤）。

    结论用于说明为何本脚本改用物理面积阈值：旧规则在 200 m 上删除的斑块面积占比
    远大于 100 m 上的名义值，且被删斑块中大量是**长期持续**的。
    """
    rule1 = int(LEGACY_PIXEL_RULE_DIAGNOSTIC["rule_1_min_patch_cells"])
    rule2_cells = int(LEGACY_PIXEL_RULE_DIAGNOSTIC["rule_2_shallow_patch_cells"])
    rule2_depth = float(LEGACY_PIXEL_RULE_DIAGNOSTIC["rule_2_shallow_threshold_m"])

    def would_drop(n_cells: int, max_depth: float) -> bool:
        if n_cells <= rule1:
            return True
        return n_cells <= rule2_cells and max_depth < rule2_depth

    # 每个轨迹的湿帧集合（衡量持久性；旧规则完全不区分持续性）
    frames_of_track: dict[int, set[int]] = {}
    for summary in summaries:
        owner = owner_by_frame[summary.seq]
        for patch in summary.patches:
            if patch.cell_indices.size == 0:
                continue
            track_id = owner.get(int(patch.cell_indices[0]))
            if track_id is not None:
                frames_of_track.setdefault(track_id, set()).add(summary.seq)

    peak = max(summaries, key=lambda s: s.wet_area_km2)
    peak_owner = owner_by_frame[peak.seq]
    drop_patches = [p for p in peak.patches if would_drop(p.n_cells, p.max_depth_m)]
    drop_cells = sum(p.n_cells for p in drop_patches)
    drop_tracks = {
        peak_owner.get(int(p.cell_indices[0]))
        for p in drop_patches
        if p.cell_indices.size
    }
    drop_tracks.discard(None)
    long_lived = [
        track_id for track_id in drop_tracks
        if len(frames_of_track.get(track_id, ())) >= 0.9 * len(summaries)
    ]

    return {
        **LEGACY_PIXEL_RULE_DIAGNOSTIC,
        "applied_on_grid": {
            "cellsize_m": float(math.sqrt(cell_area_m2)),
            "cell_area_m2": float(cell_area_m2),
            "rule_1_area_km2": round(rule1 * cell_area_m2 / 1e6, 4),
            "rule_2_area_km2": round(rule2_cells * cell_area_m2 / 1e6, 4),
        },
        "peak_frame_seq": peak.seq,
        "peak_frame_patches": peak.n_patches,
        "peak_frame_drop_patches": len(drop_patches),
        "peak_frame_drop_cells": int(drop_cells),
        "peak_frame_drop_area_km2": round(drop_cells * cell_area_m2 / 1e6, 4),
        "peak_frame_drop_area_pct": (
            round(drop_cells * cell_area_m2 / 1e6 / peak.wet_area_km2 * 100.0, 3)
            if peak.wet_area_km2 else 0.0
        ),
        "peak_frame_drop_tracks": len(drop_tracks),
        "peak_frame_drop_tracks_wet_in_90pct_of_frames": len(long_lived),
        "conclusion": (
            "旧规则在本 200 m 网格上删除的斑块中，大量在 90% 以上帧次持续存在，"
            "属于模型长期维持的浅水像元而非瞬时渲染噪点；仅按像元数过滤会删除"
            "真实模型特征（计划 §1.4 风险表第 1 行）。因此本脚本只按物理面积 + "
            "持续性 + 孤立性 + 浅水联合判定，并把持续性作为豁免优先条件。"
        ),
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
# 轨迹上下文（三档共用，只算一次）
# --------------------------------------------------------------------------- #
def build_track_context(
    summaries: list[Any],
    owner_by_frame: dict[int, dict[int, int]],
    *,
    shape: tuple[int, int],
    peak_seq: int,
) -> dict[str, Any]:
    """汇总轨迹级豁免证据：主水体成员、退水增长、触边界、到主汇流区最小距离。"""
    main_body_tracks: set[int] = set()
    boundary_tracks: set[int] = set()
    main_body_cells = np.zeros(shape, dtype=bool)
    track_frame_area: dict[int, dict[int, float]] = {}

    for summary in summaries:
        labels = main_body_patch_labels(summary, MAIN_BODY_AREA_FRACTION)
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
    flat_distance = distance.ravel()
    min_distance: dict[int, int] = {}
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

    after_peak = [summary.seq for summary in summaries if summary.seq > peak_seq]
    growth: dict[int, bool] = {}
    for track_id, areas in track_frame_area.items():
        peak_area = areas.get(peak_seq, 0.0)
        post = [areas[seq] for seq in after_peak if seq in areas]
        if peak_area > 0 and post:
            growth[track_id] = (max(post) - peak_area) > RECESSION_GROWTH_RATIO * peak_area
        else:
            growth[track_id] = False

    return {
        "main_body_tracks": main_body_tracks,
        "boundary_tracks": boundary_tracks,
        "main_body_cells": main_body_cells,
        "distance": distance,
        "min_distance": min_distance,
        "growth": growth,
        "track_frame_area": track_frame_area,
    }


def _removed_cell_mask(
    shape: tuple[int, int],
    owner: dict[int, dict[int, int]],
    verdict: dict[int, tuple[str, str]],
) -> np.ndarray:
    """当前帧被过滤的像元掩膜：像元归属轨迹判定为 filter 即移除。"""
    mask = np.zeros(shape[0] * shape[1], dtype=bool)
    for cell, track_id in owner.items():
        decision = verdict.get(track_id)
        if decision is not None and decision[0] == "filter":
            mask[cell] = True
    return mask.reshape(shape)


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
    owner_by_frame: dict[int, dict[int, int]],
    tracks: list[dict[str, Any]],
    context: dict[str, Any],
    res_max: np.ndarray,
    dry_run: bool,
    source_rel: str,
    source_hashes: dict[str, Any],
    run_tag: str | None,
) -> dict[str, Any]:
    """单档处理：轨迹分类 → 逐帧过滤 → 写派生层与审计。"""
    verdict: dict[int, tuple[str, str]] = {}
    for track in tracks:
        track_id = int(track["track_id"])
        verdict[track_id] = classify_track(
            track,
            params,
            is_main_body=track_id in context["main_body_tracks"],
            is_recession_growth=bool(context["growth"].get(track_id, False)),
            hits_boundary=track_id in context["boundary_tracks"],
            min_distance_cells=int(context["min_distance"].get(track_id, -1)),
        )

    track_verdicts = []
    for track in tracks:
        track_id = int(track["track_id"])
        kind, reason = verdict[track_id]
        track_verdicts.append({
            "track_id": track_id,
            "verdict": kind,
            "reason_code": reason,
            "max_area_km2": track["max_area_km2"],
            "max_depth_m": track["max_depth_m"],
            "frame_count": track["frame_count"],
            "longest_consecutive_run": track["longest_consecutive_run"],
            "first_frame": track["first_frame"],
            "last_frame": track["last_frame"],
            "min_distance_cells": int(context["min_distance"].get(track_id, -1)),
            "is_main_body": track_id in context["main_body_tracks"],
            "is_recession_growth": bool(context["growth"].get(track_id, False)),
            "hits_boundary": track_id in context["boundary_tracks"],
        })

    if not dry_run:
        guard_derived_target(out_dir)

    peak_area = max(summary.wet_area_km2 for summary in summaries)
    material_fraction = float(PRE_REGISTERED_ALARM["material_frame_fraction"])
    frame_records: list[dict[str, Any]] = []

    for summary in summaries:
        seq = summary.seq
        depth = frame_grids[seq]
        owner = owner_by_frame[seq]
        removed = _removed_cell_mask(shape, owner, verdict)

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

        # 被完全移除的斑块数与该帧的原因码分布（按像元与按斑块）
        removed_patches = 0
        reason_patches: Counter[str] = Counter()
        reason_cells: Counter[str] = Counter()
        for patch in summary.patches:
            cells = patch.cell_indices
            if cells.size == 0:
                continue
            kinds = {
                verdict.get(owner.get(int(cell)), ("keep", "KEEP_OTHER"))[0]
                for cell in cells.tolist()
            }
            if kinds == {"filter"}:
                removed_patches += 1
                code = verdict.get(owner.get(int(cells[0])), ("keep", "KEEP_OTHER"))[1]
                reason_patches[code] += 1
                reason_cells[code] += int(cells.size)

        cleaned = depth.copy()
        cleaned[removed] = 0.0
        after = summarize_frame(
            cleaned,
            seq,
            wet_threshold_m=params["wet_threshold_m"],
            cell_area_m2=cell_area_m2,
            sea_mask=sea_mask,
            keep_patches=False,
        )
        area_change = (
            (summary.wet_area_km2 - after.wet_area_km2) / summary.wet_area_km2 * 100.0
            if summary.wet_area_km2 > 0 else 0.0
        )
        volume_change = (
            (raw_volume - after.wet_volume_m3) / raw_volume * 100.0 if raw_volume > 0 else 0.0
        )

        frame_records.append({
            "seq": seq,
            "t_h": round(seq * params["saveint_s"] / 3600.0, 3),
            "material_frame": bool(
                peak_area > 0 and summary.wet_area_km2 >= peak_area * material_fraction
            ),
            **cells_counts,
            "n_patches_raw": summary.n_patches,
            "n_patches_after": after.n_patches,
            "removed_cells": removed_cells,
            "removed_area_km2": round(removed_area, 6),
            "removed_volume_m3": round(removed_volume, 1),
            "removed_patches": removed_patches,
            "removed_reason_patches": dict(sorted(reason_patches.items())),
            "removed_reason_cells": dict(sorted(reason_cells.items())),
            "raw_wet_area_km2": round(summary.wet_area_km2, 6),
            "raw_wet_volume_m3": round(raw_volume, 1),
            "kept_wet_cells": after.wet_cells,
            "kept_wet_area_km2": round(after.wet_area_km2, 6),
            "kept_wet_volume_m3": round(after.wet_volume_m3, 1),
            "area_change_pct": round(area_change, 4),
            "volume_change_pct": round(volume_change, 4),
        })

        if not dry_run:
            write_ascii_grid(
                out_dir / "results" / f"res-{seq:04d}.wd", _as_grid(cleaned, header)
            )

    # ---- res.max 峰值场：像元仅在“全部触及轨迹均为 filter”时移除（保守） ----
    cell_tracks: dict[int, set[int]] = {}
    for owner in owner_by_frame.values():
        for cell, track_id in owner.items():
            cell_tracks.setdefault(cell, set()).add(track_id)
    max_removed = np.zeros(shape[0] * shape[1], dtype=bool)
    for cell, track_ids in cell_tracks.items():
        if all(verdict.get(t, ("keep", "KEEP_OTHER"))[0] == "filter" for t in track_ids):
            max_removed[cell] = True
    max_removed = max_removed.reshape(shape)

    raw_max = summarize_frame(
        res_max, -1, wet_threshold_m=params["wet_threshold_m"],
        cell_area_m2=cell_area_m2, sea_mask=sea_mask, keep_patches=False,
    )
    cleaned_max = res_max.copy()
    cleaned_max[max_removed] = 0.0
    after_max = summarize_frame(
        cleaned_max, -1, wet_threshold_m=params["wet_threshold_m"],
        cell_area_m2=cell_area_m2, sea_mask=sea_mask, keep_patches=False,
    )
    max_area_change = (
        (raw_max.wet_area_km2 - after_max.wet_area_km2) / raw_max.wet_area_km2 * 100.0
        if raw_max.wet_area_km2 > 0 else 0.0
    )
    max_volume_change = (
        (raw_max.wet_volume_m3 - after_max.wet_volume_m3) / raw_max.wet_volume_m3 * 100.0
        if raw_max.wet_volume_m3 > 0 else 0.0
    )
    res_max_record = {
        "raw_wet_cells": raw_max.wet_cells,
        "raw_wet_area_km2": round(raw_max.wet_area_km2, 6),
        "raw_wet_volume_m3": round(raw_max.wet_volume_m3, 1),
        "raw_patches": raw_max.n_patches,
        "removed_cells": int(np.count_nonzero(max_removed)),
        "removed_area_km2": round(
            float(np.count_nonzero(max_removed)) * cell_area_m2 / 1e6, 6),
        "removed_volume_m3": round(
            float(np.sum(np.where(max_removed, res_max, 0.0))) * cell_area_m2, 1),
        "kept_wet_cells": after_max.wet_cells,
        "kept_wet_area_km2": round(after_max.wet_area_km2, 6),
        "kept_wet_volume_m3": round(after_max.wet_volume_m3, 1),
        "kept_patches": after_max.n_patches,
        "area_change_pct": round(max_area_change, 4),
        "volume_change_pct": round(max_volume_change, 4),
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
        "blocked_for_default_publish": bool(blocked_reasons),
        "blocked_reasons": blocked_reasons,
        "verdict_note": (
            "blocked=true 表示该档只能作诊断层，不得默认发布（计划 §3.3 / §9.2）；"
            "不代表源 run 有质量问题"
        ),
    }

    verdict_counts = Counter(item["verdict"] for item in track_verdicts)
    reason_counts = Counter(item["reason_code"] for item in track_verdicts)

    # ---- 派生层 meta（计划 §5 Phase 1 验收第 2 条） ----
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
        "alarm": {
            "blocked_for_default_publish": alarm["blocked_for_default_publish"],
            "blocked_reasons": alarm["blocked_reasons"],
            "max_area_change_pct": alarm["max_area_change_pct"],
            "max_volume_change_pct": alarm["max_volume_change_pct"],
        },
        "rollback": "删除本派生目录即回退；raw run 未被修改（计划 §9.2）",
    }

    if not dry_run:
        write_json(out_dir / "meta.json", meta)
        _write_frame_csv(out_dir / "audit" / f"frame_audit_{set_id}.csv", frame_records)
        write_json(out_dir / "audit" / f"track_verdicts_{set_id}.json", {
            "param_set_id": set_id,
            "param_hash": params["param_hash"],
            "rule_order": RULE_ORDER,
            "verdict_counts": dict(sorted(verdict_counts.items())),
            "reason_counts": dict(sorted(reason_counts.items())),
            "tracks": track_verdicts,
        })
        assert_no_forbidden_files(out_dir)

    return {
        "param_set_id": set_id,
        "params": params,
        "param_hash": params["param_hash"],
        "source": params["source"],
        "confidence": params["confidence"],
        "verdict_counts": dict(sorted(verdict_counts.items())),
        "reason_counts": dict(sorted(reason_counts.items())),
        "alarm": alarm,
        "res_max": res_max_record,
        "frames": frame_records,
        "derived_dir": None if dry_run else out_dir.relative_to(PROJ).as_posix(),
        "meta": meta,
    }


# --------------------------------------------------------------------------- #
# 跨分辨率换算对照（计划 §3.4）
# --------------------------------------------------------------------------- #
def cross_resolution_check(header: dict[str, float], cell_area_m2: float) -> dict[str, Any]:
    """同一物理面积阈值在 200 m 与 100 m 网格上的像元数换算对照。"""
    out: dict[str, Any] = {
        "primary_grid": {
            "cellsize_m": float(header["cellsize"]),
            "cell_area_m2": float(cell_area_m2),
        },
        "comparison_grid": None,
        "thresholds": {},
        "note": (
            "同一物理面积阈值在两种网格上换算出的像元数不同；像元数不是跨分辨率"
            "稳定量（计划 §1.4）。100 m 结果本层未处理，仅作换算对照。"
        ),
    }
    other = CASE_DIR / CROSS_RES_RUN_ID / "results" / "res.max"
    if not other.is_file():
        out["comparison_grid"] = {"status": "missing", "path": str(other)}
        return out
    other_header = read_ascii_header(other)
    other_cellsize = float(other_header["cellsize"])
    other_cell_area = other_cellsize * other_cellsize
    out["comparison_grid"] = {
        "status": "present",
        "run_id": CROSS_RES_RUN_ID,
        "cellsize_m": other_cellsize,
        "cell_area_m2": other_cell_area,
        "ncols": int(other_header["ncols"]),
        "nrows": int(other_header["nrows"]),
    }
    for set_id, spec in PRE_REGISTERED_PARAM_SETS.items():
        for key in ("min_patch_area_km2", "exemption_area_km2",
                    "evidence_area_km2", "shallow_max_area_km2"):
            area = float(spec[key])
            out["thresholds"].setdefault(key, {})[set_id] = {
                "area_km2": area,
                "n_cells_primary": cells_for_area(area, cell_area_m2),
                "n_cells_comparison": cells_for_area(area, other_cell_area),
            }
    return out


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

    # ---- 轨迹追踪（复用 Lead 的 PersistenceTracker） ----
    tracker = PersistenceTracker()
    owner_by_frame: dict[int, dict[int, int]] = {}
    for summary in summaries:
        tracker.update(summary, width=shape[1])
        owner_by_frame[summary.seq] = assert_tracker_contract(tracker)
    tracks = tracker.summaries()

    context = build_track_context(
        summaries, owner_by_frame, shape=shape, peak_seq=peak_seq,
    )
    log.step("track_context", tracks=len(tracks), peak_seq=peak_seq,
             main_body_cells=int(np.count_nonzero(context["main_body_cells"])),
             main_body_tracks=len(context["main_body_tracks"]),
             boundary_tracks=len(context["boundary_tracks"]),
             recession_growth_tracks=sum(1 for v in context["growth"].values() if v))

    legacy = legacy_pixel_rule_diagnostic(summaries, owner_by_frame, cell_area_m2=cell_area)
    log.step("legacy_pixel_rule_diagnostic",
             peak_frame_drop_area_pct=legacy["peak_frame_drop_area_pct"],
             peak_frame_drop_tracks_wet_in_90pct=legacy[
                 "peak_frame_drop_tracks_wet_in_90pct_of_frames"])

    res_max = read_ascii_grid(res_max_path, expected_shape=shape).data
    source_rel = run_dir.relative_to(PROJ).as_posix()

    results: dict[str, Any] = {}
    for set_id in args.param_sets:
        if set_id not in PRE_REGISTERED_PARAM_SETS:
            raise GridError(
                f"未预注册的参数档 {set_id}；可选 {sorted(PRE_REGISTERED_PARAM_SETS)}"
            )
        params = resolve_param_set(
            PRE_REGISTERED_PARAM_SETS[set_id],
            cell_area_m2=cell_area,
            cellsize_m=cellsize,
            saveint_s=saveint,
        )
        results[set_id] = process_param_set(
            set_id=set_id,
            params=params,
            out_dir=out_root / args.run_id / set_id,
            shape=shape,
            cell_area_m2=cell_area,
            header=header,
            sea_mask=sea_mask,
            frames=frames,
            frame_grids=frame_grids,
            summaries=summaries,
            owner_by_frame=owner_by_frame,
            tracks=tracks,
            context=context,
            res_max=res_max,
            dry_run=bool(args.dry_run),
            source_rel=source_rel,
            source_hashes=before,
            run_tag=args.run_tag,
        )
        log.step(
            f"param_set_{set_id}",
            param_hash=params["param_hash"],
            verdict_counts=results[set_id]["verdict_counts"],
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
            "resolution_policy": "方案 A：200 m 为生产基线；100 m 仅敏感性参照（计划 §1.4）",
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
                "本脚本读取其 _cell_owner 内部状态把轨迹判定回写像元，"
                "该契约由 tests/test_display_qc.py 守护"
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
                "persistence_frames = ceil(persistence_hours * 3600 / saveint_s)"
            ),
            "cell_area_source": "栅格头 cellsize ** 2（不写死像元面积）",
            "note": "像元数是换算结果，不是阈值本身；同一物理阈值在 100 m 与 200 m 上不同",
        },
        "pre_registration": {
            "frozen_before_production_run": True,
            "frozen_at_utc": PRE_REGISTRATION_UTC,
            "basis": (
                "面积阈值取 200 m 网格上 2 / 4 / 9 个像元的量级（0.08 / 0.16 / 0.36 km²），"
                "持续性取 1.5 / 3 / 6 天，水深取 0.10 / 0.15 / 0.20 m；"
                "全部为项目候选参数，未经本地率定，置信度 E3 候选假设"
            ),
            "rule_structure_basis": (
                "计划 §3.3 第 4 条原文为“小且短暂、孤立、浅水”，故过滤条件取合取；"
                "第 5 条豁免先于第 4 条判定"
            ),
            "forbidden_attribution": (
                "不归因于 ARR 2019、UK EA 或任何国际规范的强制值（计划 §2.3）"
            ),
        },
        "param_sets": results,
        "legacy_pixel_rule_diagnostic": legacy,
        "resolution_comparison": cross_resolution_check(header, cell_area),
        "raw_integrity": {
            "before": {k: v for k, v in before.items() if k != "frames"},
            "after": {k: v for k, v in after.items() if k != "frames"},
            "unchanged": integrity["unchanged"],
            "changed_frames": integrity["changed_frames"],
            "method": "脚本运行前后各计算一次 sha256；任一差异即抛错",
        },
        "derived_layer_contract": {
            "path": f"outputs/display_qc/{args.run_id}/<param_set>/",
            "results_dir_contains": "res-XXXX.wd 与 res.max",
            "res_mass_copied": False,
            "why_no_res_mass": (
                "scripts/stage3/g15_mass_recheck.py 用 rglob('results/res.mass') 扫描；"
                "派生目录带 res.mass 会被误计为独立 run 并污染质量门计数"
            ),
            "rollback": "删除派生目录即回退；raw run 未被修改（计划 §9.2）",
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
        description="久隆 200 m 基线只读展示质控（display_qc 派生层 + 逐帧审计）",
    )
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID,
                        help=f"源 run id（默认 {DEFAULT_RUN_ID}）")
    parser.add_argument("--param-sets", default="A,B,C",
                        help="预注册参数档，逗号分隔（默认 A,B,C）")
    parser.add_argument("--out-root", type=Path, default=None,
                        help="派生层根目录（默认 outputs/display_qc）")
    parser.add_argument("--audit-path", type=Path, default=None,
                        help="审计 JSON 路径（默认 data/catalog/jiulong_display_qc_audit.json）")
    add_common_args(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.param_sets = [item.strip().upper() for item in args.param_sets.split(",") if item.strip()]
    audit = run(args)
    for set_id, rec in audit["param_sets"].items():
        alarm = rec["alarm"]
        print(
            f"[{set_id}] filter_tracks={rec['verdict_counts'].get('filter', 0)} "
            f"exempt={rec['verdict_counts'].get('exempt', 0)} "
            f"keep={rec['verdict_counts'].get('keep', 0)} "
            f"max_area_change={alarm['max_area_change_pct']:.2f}% "
            f"max_vol_change={alarm['max_volume_change_pct']:.2f}% "
            f"blocked={alarm['blocked_for_default_publish']}"
        )
    print(f"raw_unchanged={audit['raw_integrity']['unchanged']} dry_run={audit['dry_run']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
