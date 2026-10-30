"""质量门：从父项目质量复核产物读取所选 run 的真实质量等级。

口径（计划 §3.5 / 父项目 docs/execution_status.md）：
- mass_recheck_g15.json 记录 E_mass 与 gate_1e-3；PASS 不能推导“预测正确”。
- FAIL-EXPLAINED 是解释性失败，**不计入通过**。
- 没有记录的 run 一律 `unrecorded`，UI 不得显示 PASS。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

GATE_VALUES = {"PASS", "FAIL-EXPLAINED", "FAIL", "unrecorded"}


def _read_json_tolerant(path: Path) -> Any:
    """UTF-8 优先，失败再试 GBK/Latin-1（父项目历史文件编码混杂）。"""
    raw = path.read_bytes()
    last: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            return json.loads(raw.decode(encoding))
        except Exception as exc:
            last = exc
    raise ValueError(f"无法解析 {path}: {last}")


def find_mass_recheck(parent_root: Path) -> Path:
    return Path(parent_root) / "data" / "catalog" / "mass_recheck_g15.json"


def load_mass_gate(parent_root: str | Path, run_path_rel: str) -> dict[str, Any]:
    """按父项目相对路径（如 `outputs/jiulong/2024-09-yagi/scen_free_200m_sv6h`）取质量门。"""
    parent_root = Path(parent_root)
    rel = str(run_path_rel).replace("\\", "/").strip("/")
    doc_path = find_mass_recheck(parent_root)
    if not doc_path.is_file():
        return {"mass_gate": "unrecorded", "e_mass": None, "cause": "质量复核文件不存在", "source": None}
    doc = _read_json_tolerant(doc_path)
    runs = doc.get("runs") or {}
    record = None
    for key, value in runs.items():
        if str(key).replace("\\", "/").strip("/").lower() == rel.lower():
            record = value
            break
    if record is None:
        return {
            "mass_gate": "unrecorded",
            "e_mass": None,
            "cause": "该 run 未进入质量复核表（不得显示 PASS）",
            "source": str(doc_path),
        }
    gate = str(record.get("gate_1e-3") or "unrecorded")
    if gate not in GATE_VALUES:
        gate = "unrecorded"
    return {
        "mass_gate": gate,
        "e_mass": record.get("e_mass_132"),
        "cause": record.get("cause"),
        "excluded_from_pass_count": bool(record.get("excluded_from_pass_count", False)),
        "residual_pct_of_in": record.get("residual_pct_of_in"),
        "source": str(doc_path),
        "counts": doc.get("counts"),
        "formula": doc.get("formula"),
    }


def merge_quality(case_quality: dict[str, Any], mass_gate: dict[str, Any]) -> dict[str, Any]:
    """案例级质量摘要 = 配置文案 + 质量复核实测值。"""
    merged = dict(case_quality)
    merged["mass_gate"] = mass_gate.get("mass_gate", "unrecorded")
    merged["e_mass"] = mass_gate.get("e_mass")
    if mass_gate.get("cause"):
        merged.setdefault("mass_gate_cause", mass_gate["cause"])
    if mass_gate.get("counts"):
        merged["mass_counts_overall"] = mass_gate["counts"]
    merged.setdefault("mass_gate_formula", mass_gate.get("formula"))
    return merged
