# -*- coding: utf-8 -*-
"""
脚本：generate_handbook_v2.py
功能：构建全面、详实、不压缩的《广西二维水动力模拟项目：全问答、深度原理解析与技术答辩手册（纠错升级版）》
同时生成：
1. Word 格式：广西二维水动力模拟项目_全问答与技术答辩手册(手机阅读优化版).docx
2. Markdown 格式：广西二维水动力模拟项目_全问答与技术答辩手册(手机阅读优化版).md
"""

import os
import sys
import docx
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls

if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

def create_doc():
    doc = docx.Document()
    for section in doc.sections:
        section.top_margin = Inches(0.5)
        section.bottom_margin = Inches(0.5)
        section.left_margin = Inches(0.5)
        section.right_margin = Inches(0.5)
        section.page_width = Inches(8.27)
        section.page_height = Inches(11.69)
    return doc

def set_cell_background(cell, fill_hex):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{fill_hex}"/>')
    tcPr.append(shd)

def set_cell_margins(cell, top=80, bottom=80, left=120, right=120):
    tcPr = cell._tc.get_or_add_tcPr()
    tcMar = parse_xml(f'<w:tcMar {nsdecls("w")}><w:top w:w="{top}" w:type="dxa"/><w:bottom w:w="{bottom}" w:type="dxa"/><w:left w:w="{left}" w:type="dxa"/><w:right w:w="{right}" w:type="dxa"/></w:tcMar>')
    tcPr.append(tcMar)

def set_cell_left_border(cell, color_hex="0F4C81", size="36"):
    tcPr = cell._tc.get_or_add_tcPr()
    borders = parse_xml(f'<w:tcBorders {nsdecls("w")}><w:top w:val="none"/><w:left w:val="single" w:sz="{size}" w:space="0" w:color="{color_hex}"/><w:bottom w:val="none"/><w:right w:val="none"/></w:tcBorders>')
    tcPr.append(borders)

def set_table_borders(table, color="D3D3D3", sz="4"):
    tblPr = table._tbl.tblPr
    borders = parse_xml(
        f'<w:tblBorders {nsdecls("w")}>'
        f'  <w:top w:val="single" w:sz="{sz}" w:space="0" w:color="{color}"/>'
        f'  <w:bottom w:val="single" w:sz="{sz}" w:space="0" w:color="{color}"/>'
        f'  <w:left w:val="none"/>'
        f'  <w:right w:val="none"/>'
        f'  <w:insideH w:val="single" w:sz="{sz}" w:space="0" w:color="{color}"/>'
        f'  <w:insideV w:val="none"/>'
        f'</w:tblBorders>'
    )
    tblPr.append(borders)

def format_run(run, font_name="微软雅黑", size_pt=10.5, bold=False, italic=False, color_rgb=(38, 38, 38)):
    run.font.name = font_name
    run.font.size = Pt(size_pt)
    run.bold = bold
    run.italic = italic
    run.font.color.rgb = RGBColor(*color_rgb)
    rPr = run._r.get_or_add_rPr()
    rFonts = parse_xml(f'<w:rFonts {nsdecls("w")} w:ascii="{font_name}" w:hAnsi="{font_name}" w:eastAsia="{font_name}" w:cs="{font_name}"/>')
    rPr.append(rFonts)

def add_p(doc, text="", font_name="微软雅黑", size_pt=10.5, bold=False, italic=False, color_rgb=(38, 38, 38),
          align=WD_ALIGN_PARAGRAPH.LEFT, space_before=2, space_after=3, line_spacing=1.25):
    p = doc.add_paragraph()
    p.alignment = align
    p.paragraph_format.space_before = Pt(space_before)
    p.paragraph_format.space_after = Pt(space_after)
    p.paragraph_format.line_spacing = line_spacing
    if text:
        run = p.add_run(text)
        format_run(run, font_name, size_pt, bold, italic, color_rgb)
    return p

def add_h1(doc, text):
    return add_p(doc, text, size_pt=14.0, bold=True, color_rgb=(27, 54, 93), space_before=14, space_after=6)

def add_h2(doc, text):
    return add_p(doc, text, size_pt=12.0, bold=True, color_rgb=(15, 76, 129), space_before=10, space_after=4)

def add_h3(doc, text):
    return add_p(doc, text, size_pt=11.0, bold=True, color_rgb=(43, 84, 126), space_before=6, space_after=2)

def add_question_box(doc, q_num, q_title, q_bg=""):
    table = doc.add_table(rows=1, cols=1)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    table.columns[0].width = Inches(7.27)
    cell = table.cell(0, 0)
    set_cell_background(cell, "F0F4F8")
    set_cell_margins(cell, top=100, bottom=100, left=140, right=140)
    set_cell_left_border(cell, "0F4C81", "36")
    
    p = cell.paragraphs[0]
    p.paragraph_format.space_before = Pt(1)
    p.paragraph_format.space_after = Pt(2)
    p.paragraph_format.line_spacing = 1.2
    run1 = p.add_run(f"【问答模块 {q_num}】{q_title}")
    format_run(run1, "微软雅黑", 11.0, bold=True, color_rgb=(15, 76, 129))
    
    if q_bg:
        p2 = cell.add_paragraph()
        p2.paragraph_format.space_before = Pt(2)
        p2.paragraph_format.space_after = Pt(1)
        p2.paragraph_format.line_spacing = 1.15
        run2 = p2.add_run(f"用户原声背景：{q_bg}")
        format_run(run2, "微软雅黑", 9.0, italic=True, color_rgb=(100, 110, 120))
    
    add_p(doc, "", space_before=2, space_after=2)

def add_alert_box(doc, alert_type, title, content):
    colors = {
        "NOTE": ("EBF3FB", "1F6FEB"),
        "WARNING": ("FFF8E7", "B08800"),
        "IMPORTANT": ("F0FBF0", "1A7F37"),
        "CRITICAL": ("FFEBE9", "CF222E")
    }
    bg_hex, border_hex = colors.get(alert_type, ("F0F4F8", "0F4C81"))
    
    table = doc.add_table(rows=1, cols=1)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    table.columns[0].width = Inches(7.27)
    cell = table.cell(0, 0)
    set_cell_background(cell, bg_hex)
    set_cell_margins(cell, top=80, bottom=80, left=120, right=120)
    set_cell_left_border(cell, border_hex, "28")
    
    p = cell.paragraphs[0]
    p.paragraph_format.space_before = Pt(1)
    p.paragraph_format.space_after = Pt(2)
    p.paragraph_format.line_spacing = 1.15
    run1 = p.add_run(f"[{alert_type}] {title}\n")
    format_run(run1, "微软雅黑", 10.0, bold=True, color_rgb=(27, 54, 93))
    run2 = p.add_run(content)
    format_run(run2, "微软雅黑", 9.5, bold=False, color_rgb=(50, 50, 50))
    add_p(doc, "", space_before=2, space_after=2)

def add_custom_table(doc, headers, data, col_widths=None):
    table = doc.add_table(rows=len(data) + 1, cols=len(headers))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    set_table_borders(table, "D3D3D3", "4")
    
    total_w = Inches(7.27)
    if col_widths is None:
        cw = total_w / len(headers)
        col_widths = [cw] * len(headers)
    
    hdr_row = table.rows[0]
    for idx, heading in enumerate(headers):
        cell = hdr_row.cells[idx]
        cell.width = col_widths[idx]
        set_cell_background(cell, "1B365D")
        set_cell_margins(cell, top=90, bottom=90, left=90, right=90)
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(1)
        p.paragraph_format.space_after = Pt(1)
        run = p.add_run(heading)
        format_run(run, "微软雅黑", 9.5, bold=True, color_rgb=(255, 255, 255))
        
    for r_idx, row_data in enumerate(data):
        row = table.rows[r_idx + 1]
        bg = "F9FBFD" if r_idx % 2 == 1 else "FFFFFF"
        for c_idx, val in enumerate(row_data):
            cell = row.cells[c_idx]
            cell.width = col_widths[c_idx]
            set_cell_background(cell, bg)
            set_cell_margins(cell, top=70, bottom=70, left=80, right=80)
            p = cell.paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT if c_idx == 0 or len(val) > 15 else WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.space_before = Pt(1)
            p.paragraph_format.space_after = Pt(1)
            p.paragraph_format.line_spacing = 1.15
            run = p.add_run(str(val))
            format_run(run, "微软雅黑", 9.0, color_rgb=(38, 38, 38))
            
    add_p(doc, "", space_before=3, space_after=3)

print("Helper functions compiled.")
