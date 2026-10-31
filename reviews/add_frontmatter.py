"""为 2d_hydro_project/docs/ 下的 Markdown 补 YAML frontmatter。

按 documentation-standard skill 的 document-model 字段：
  doc_type: tutorial | how-to | reference | explanation
  status:   active | draft | deprecated
  topic:    主题标识
  canonical: true（该主题的权威文档）
  scope:    project

规则：
- 已有 frontmatter（首行 ---）的文件跳过，不重复添加。
- 每个文件按显式映射表赋值，不做内容猜测。
- 只插入头部，不改正文任何字节。

用法：
  uv run --no-project --python 3.12 python reviews/add_frontmatter.py --dry-run
  uv run --no-project --python 3.12 python reviews/add_frontmatter.py --apply
"""
from __future__ import annotations

import argparse
import pathlib
import sys

DOCS = pathlib.Path(__file__).resolve().parents[1] / "2d_hydro_project" / "docs"

# path (相对 docs/) -> (doc_type, status, topic, canonical)
MAPPING: dict[str, tuple[str, str, str, bool]] = {
    # 状态与总览
    "execution_status.md": ("reference", "active", "execution-status", True),
    "final_report.md": ("explanation", "active", "final-report", True),
    "stage_summary_3cases.md": ("explanation", "active", "stage-summary", True),
    # 操作手册
    "RUNBOOK.md": ("how-to", "active", "runbook", True),
    # 索引
    "README.md": ("reference", "active", "docs-index", True),
    # 久隆治理
    "jiulong_100m_rerun.md": ("reference", "active", "jiulong-100m-rerun", True),
    "jiulong_phase34_candidates.md": ("reference", "active", "jiulong-phase34", True),
    # 阶段报告
    "s1_validation.md": ("reference", "active", "s1-validation", True),
    "stage0_report.md": ("reference", "active", "stage0-report", True),
    "stage1_data_baseline.md": ("reference", "active", "stage1-data-baseline", True),
    "stage2_benchmarks.md": ("reference", "active", "stage2-benchmarks", True),
    "stage2_build_report.md": ("reference", "active", "stage2-build", True),
    "stage3_manning.md": ("reference", "active", "stage3-manning", True),
    "stage3_ningming_convergence.md": ("reference", "active", "stage3-ningming-convergence", True),
    "stage3_ningming_ensemble.md": ("reference", "active", "stage3-ningming-ensemble", True),
    "stage3_ningming_run1.md": ("reference", "active", "stage3-ningming-run1", True),
    "stage3_sensitivity.md": ("reference", "active", "stage3-sensitivity", True),
    "stage4_karst.md": ("reference", "active", "stage4-karst", True),
    # ADR
    "adr/ADR-0001-工程结构与Python环境.md": ("explanation", "active", "adr-0001", True),
    "adr/ADR-0002-三案例CRS基线.md": ("explanation", "active", "adr-0002", True),
    "adr/ADR-0003-模型获取路径.md": ("explanation", "active", "adr-0003", True),
    "adr/ADR-0004-公开数据降级模式与垂直基准策略.md": ("explanation", "active", "adr-0004", True),
    "adr/ADR-0005-变电站整体化范围调整.md": ("explanation", "active", "adr-0005", True),
    "adr/ADR-0006-结果完整性更正与质量门口径.md": ("explanation", "active", "adr-0006", True),
    "adr/ADR-0007-执行状态与降级边界.md": ("explanation", "active", "adr-0007", True),
    "adr/ADR-0008-模型定版与桥涵结构使用限制.md": ("explanation", "active", "adr-0008", True),
    "adr/ADR-0009-宁明河槽刻蚀停用与地形分层显示.md": ("explanation", "active", "adr-0009", True),
    "adr/ADR-0010-久隆直接降雨模型治理规范.md": ("explanation", "active", "adr-0010", True),
}


def build_frontmatter(doc_type: str, status: str, topic: str, canonical: bool) -> str:
    return (
        "---\n"
        f"doc_type: {doc_type}\n"
        f"status: {status}\n"
        f"topic: {topic}\n"
        f"canonical: {'true' if canonical else 'false'}\n"
        "scope: project\n"
        "---\n\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    changed, skipped, missing = [], [], []
    for rel, meta in MAPPING.items():
        path = DOCS / rel
        if not path.is_file():
            missing.append(rel)
            continue
        text = path.read_text(encoding="utf-8")
        if text.startswith("---\n"):
            skipped.append(rel)
            continue
        new_text = build_frontmatter(*meta) + text
        if args.apply:
            path.write_text(new_text, encoding="utf-8", newline="\n")
        changed.append(rel)

    print(f"changed={len(changed)} skipped={len(skipped)} missing={len(missing)}")
    for r in changed:
        print(f"  {'WOULD ADD' if args.dry_run else 'ADDED'} {r}")
    for r in skipped:
        print(f"  SKIP (has frontmatter) {r}")
    for r in missing:
        print(f"  MISSING {r}")
    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
