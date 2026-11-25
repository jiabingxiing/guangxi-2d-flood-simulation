# -*- coding: utf-8 -*-
"""把 final_report.md 原样同步到 final_report.docx（保留现有样式约定）。

用法：
  uv run --no-project --python 3.12 --with python-docx python rebuild_final_report_docx.py

设计：Markdown 是唯一真源，docx 只是它的渲染副本。
不做复杂 Markdown 解析——只处理本项目实际用到的子集：
标题（#/##/###）、段落、无序列表、表格（管道表）、引用块（>）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import docx
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

HERE = Path(__file__).resolve().parent
MD = HERE / "2d_hydro_project" / "docs" / "final_report.md"
DOCX = HERE / "2d_hydro_project" / "docs" / "final_report.docx"

FONT = "微软雅黑"


def _fmt(run, size=10.5, bold=False):
    run.font.name = FONT
    run.font.size = Pt(size)
    run.bold = bold
    # 中文字体需要显式设置 eastAsia
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}rFonts")
    if rfonts is None:
        from docx.oxml import OxmlElement
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}eastAsia", FONT)


def add_md_runs(par, text: str, size=10.5) -> None:
    """把 **bold** 与 `code` 拆成 run，其余按普通文本。"""
    for part in re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            r = par.add_run(part[2:-2])
            _fmt(r, size, bold=True)
        elif part.startswith("`") and part.endswith("`"):
            r = par.add_run(part[1:-1])
            _fmt(r, size)
            r.font.name = "Consolas"
        else:
            r = par.add_run(part)
            _fmt(r, size)


def split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def is_separator(line: str) -> bool:
    s = line.strip()
    if not s.startswith("|"):
        return False
    cells = split_row(s)
    return all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c)


def build() -> None:
    md_text = MD.read_text(encoding="utf-8")
    body = md_text
    # 去掉 YAML frontmatter
    if body.startswith("---"):
        parts = body.split("---", 2)
        if len(parts) >= 3:
            body = parts[2]

    d = docx.Document()
    lines = body.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        line = raw.rstrip()
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # 表格：连续以 | 开头的行
        if stripped.startswith("|"):
            block = []
            while i < n and lines[i].strip().startswith("|"):
                block.append(lines[i].rstrip())
                i += 1
            rows = [b for b in block if not is_separator(b)]
            if rows:
                header = split_row(rows[0])
                data = [split_row(r) for r in rows[1:]]
                table = d.add_table(rows=1, cols=len(header))
                table.style = "Table Grid"
                for c, txt in enumerate(header):
                    cell = table.rows[0].cells[c]
                    cell.text = ""
                    add_md_runs(cell.paragraphs[0], txt, size=9)
                    for r in cell.paragraphs[0].runs:
                        r.bold = True
                for row in data:
                    cells = table.add_row().cells
                    for c in range(min(len(row), len(header))):
                        cells[c].text = ""
                        add_md_runs(cells[c].paragraphs[0], row[c], size=9)
                d.add_paragraph()
            continue

        # 标题
        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            if level == 1:
                p = d.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                add_md_runs(p, text, size=18)
            else:
                p = d.add_heading(level=min(level, 4))
                p.text = ""
                add_md_runs(p, text, size={2: 14, 3: 12, 4: 11}.get(level, 11))
            i += 1
            continue

        # 引用块
        if stripped.startswith(">"):
            text = stripped.lstrip(">").strip()
            if text:
                p = d.add_paragraph()
                p.paragraph_format.left_indent = Pt(18)
                add_md_runs(p, text, size=10)
            i += 1
            continue

        # 无序列表
        m = re.match(r"^[-*+]\s+(.*)$", stripped)
        if m:
            p = d.add_paragraph(style="List Bullet")
            add_md_runs(p, m.group(1), size=10.5)
            i += 1
            continue

        # 有序列表
        m = re.match(r"^(\d+)\.\s+(.*)$", stripped)
        if m:
            p = d.add_paragraph(style="List Number")
            add_md_runs(p, m.group(2), size=10.5)
            i += 1
            continue

        # 分隔线
        if re.fullmatch(r"-{3,}", stripped):
            i += 1
            continue

        # 普通段落
        p = d.add_paragraph()
        add_md_runs(p, stripped, size=10.5)
        i += 1

    d.save(str(DOCX))
    print(f"rebuilt {DOCX}")


if __name__ == "__main__":
    if not MD.is_file():
        sys.exit(f"missing source: {MD}")
    build()