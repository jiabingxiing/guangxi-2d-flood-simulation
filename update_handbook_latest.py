# -*- coding: utf-8 -*-
"""
脚本：update_handbook_latest.py
功能：基于 Phase A~D 系统纠错成果，重新生成《广西二维水动力模拟项目_全问答与技术答辩手册(手机阅读优化版)》Word 与 Markdown 终版文件。
"""
import os
import sys
import docx
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls

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

def set_cell_margins(cell, top=70, bottom=70, left=100, right=100):
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
    return add_p(doc, text, font_name="微软雅黑", size_pt=13.5, bold=True, color_rgb=(15, 76, 129), space_before=14, space_after=5)

def add_h2(doc, text):
    return add_p(doc, text, font_name="微软雅黑", size_pt=11.5, bold=True, color_rgb=(46, 117, 182), space_before=9, space_after=3)

def add_h3(doc, text):
    return add_p(doc, text, font_name="微软雅黑", size_pt=10.5, bold=True, color_rgb=(31, 78, 121), space_before=6, space_after=2)

def add_question_box(doc, q_num, q_title, q_body):
    tbl = doc.add_table(rows=1, cols=1)
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    cell = tbl.cell(0, 0)
    set_cell_background(cell, "F0F4F8")
    set_cell_left_border(cell, "0F4C81", "36")
    set_cell_margins(cell, top=110, bottom=110, left=150, right=110)
    
    p = cell.paragraphs[0]
    p.paragraph_format.space_before = Pt(1)
    p.paragraph_format.space_after = Pt(2)
    p.paragraph_format.line_spacing = 1.25
    r_tag = p.add_run(f"【用户核心提问 · 序号 {q_num}】\n")
    format_run(r_tag, "微软雅黑", 11, bold=True, color_rgb=(15, 76, 129))
    r_title = p.add_run(f"核心议题：{q_title}\n")
    format_run(r_title, "微软雅黑", 10.5, bold=True, color_rgb=(31, 78, 121))
    
    p2 = cell.add_paragraph()
    p2.paragraph_format.space_before = Pt(2)
    p2.paragraph_format.space_after = Pt(1)
    p2.paragraph_format.line_spacing = 1.2
    r_body = p2.add_run(q_body)
    format_run(r_body, "微软雅黑", 9.5, italic=True, color_rgb=(60, 60, 60))
    
    p_spacer = doc.add_paragraph()
    p_spacer.paragraph_format.space_before = Pt(0)
    p_spacer.paragraph_format.space_after = Pt(3)

def add_callout(doc, text, title=""):
    tbl = doc.add_table(rows=1, cols=1)
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    cell = tbl.cell(0, 0)
    set_cell_background(cell, "EBF3FA")
    set_cell_left_border(cell, "2E75B6", "24")
    set_cell_margins(cell, top=90, bottom=90, left=140, right=90)
    p = cell.paragraphs[0]
    p.paragraph_format.space_before = Pt(1)
    p.paragraph_format.space_after = Pt(1)
    p.paragraph_format.line_spacing = 1.2
    if title:
        r_t = p.add_run(title + "\n")
        format_run(r_t, "微软雅黑", 10, bold=True, color_rgb=(15, 76, 129))
    r_c = p.add_run(text)
    format_run(r_c, "微软雅黑", 9.5, color_rgb=(40, 40, 40))
    doc.add_paragraph().paragraph_format.space_after = Pt(2)

def create_table(doc, headers, data):
    tbl = doc.add_table(rows=len(data) + 1, cols=len(headers))
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    set_table_borders(tbl, "D3D3D3", "4")
    
    for col_idx, h_text in enumerate(headers):
        cell = tbl.cell(0, col_idx)
        set_cell_background(cell, "0F4C81")
        set_cell_margins(cell, top=70, bottom=70, left=80, right=80)
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(1)
        p.paragraph_format.space_after = Pt(1)
        r = p.add_run(h_text)
        format_run(r, "微软雅黑", 9, bold=True, color_rgb=(255, 255, 255))
        
    for row_idx, row_data in enumerate(data):
        bg = "F9FBFD" if row_idx % 2 == 1 else "FFFFFF"
        for col_idx, val in enumerate(row_data):
            cell = tbl.cell(row_idx + 1, col_idx)
            set_cell_background(cell, bg)
            set_cell_margins(cell, top=50, bottom=50, left=70, right=70)
            p = cell.paragraphs[0]
            p.paragraph_format.space_before = Pt(1)
            p.paragraph_format.space_after = Pt(1)
            p.paragraph_format.line_spacing = 1.15
            r = p.add_run(str(val))
            format_run(r, "微软雅黑", 8.5, color_rgb=(38, 38, 38))
            
    p_sp = doc.add_paragraph()
    p_sp.paragraph_format.space_before = Pt(0)
    p_sp.paragraph_format.space_after = Pt(3)
    return tbl

print("Update framework ready.")
