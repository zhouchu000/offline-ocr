"""Page-coordinate Word frames over native editable paragraphs and tables."""
from __future__ import annotations

import statistics
from dataclasses import replace

from .line_geometry import complete_line_geometry, text_key

from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt


def edits_need_flow(block) -> bool:
    if block.edited_text is not None and (
        len(block.text) > len(block.original_text)*1.1+4
        or block.text.count('\n')+1 > max(1, len(block.text_lines))
    ):
        return True
    return any(cell.edited_text is not None and (
        len(cell.text) > len(cell.original_text)*1.1+4
        or cell.text.count('\n') > cell.original_text.count('\n')
    ) for cell in block.cells)


def usable_source_geometry(page) -> bool:
    return page.width > 0 and page.height > 0 and any(
        line.bbox[2] > line.bbox[0] and line.bbox[3] > line.bbox[1]
        for block in page.blocks for line in block.text_lines
    )


def positioned_text_parts(block, page, size, pitch):
    """Only split a paragraph when uniform Word leading would move its rows.

    A detector omission creates a hole in the original paragraph. Each known
    row keeps its own location, leaving room for a separately recovered row.
    """
    if block.edited_text is not None or len(block.text_lines) < 2 or not complete_line_geometry(block):
        return [block]
    lines = sorted(block.text_lines, key=lambda line: (line.bbox[1], line.bbox[0]))
    actual_pitch = max(size*1.05, pitch or size*1.25)
    centers = [(line.bbox[1]+line.bbox[3])/2/page.height*841.89 for line in lines]
    if all(abs(center-centers[0]-index*actual_pitch) <= max(2, size*.2)
           for index, center in enumerate(centers)):
        return [block]
    # Preserve every original character, including full-width punctuation.
    original = [(i, char) for i, char in enumerate(block.original_text) if not char.isspace()]
    parts = []
    cursor = 0
    for line in lines:
        length = len("".join(line.text.split()))
        if not length or cursor+length > len(original):
            return [block]
        start, end = original[cursor][0], original[cursor+length-1][0]+1
        text = block.original_text[start:end]
        if text_key(text) != text_key(line.text):
            return [block]
        ranges = [(max(a, start)-start, min(b, end)-start)
                  for a, b in block.bold_ranges if a < end and b > start]
        parts.append(replace(block, original_text=text, bbox=line.bbox,
                             text_lines=[replace(line, text=text)], bold_ranges=ranges))
        cursor += length
    return parts if cursor == len(original) else [block]


def page_line_pitch(page) -> float | None:
    centers = sorted(set(round((line.bbox[1]+line.bbox[3])/2, 1)
                         for block in page.blocks if block.kind != 'table'
                         for line in block.text_lines if len(line.text.strip()) >= 8))
    pitches = [(b-a)/page.height*841.89 for a,b in zip(centers, centers[1:])]
    pitches = [p for p in pitches if 10 <= p <= 45]
    return statistics.median(pitches) if pitches else None


def add_page_anchor(document, *, page_break_before: bool) -> None:
    paragraph = document.add_paragraph()
    pf = paragraph.paragraph_format
    pf.line_spacing = Pt(1)
    pf.space_before = pf.space_after = Pt(0)
    pf.page_break_before = page_break_before
    pf.keep_with_next = False
    pf.widow_control = False
    # Empty anchor has no text and no fixed-height container around real text.
    mark = OxmlElement('w:rPr')
    size = OxmlElement('w:sz')
    size.set(qn('w:val'), '2')
    mark.append(size)
    paragraph._p.get_or_add_pPr().append(mark)


def position_paragraph(paragraph, page, block, size, pitch, bbox) -> None:
    left, top, right, bottom = bbox
    if block.text_lines and all(line.ink_bbox is not None for line in block.text_lines):
        # Recovery crops include padding for recognition. The padding is not
        # part of the printed paragraph's indentation.
        left = max(left, min(line.ink_bbox[0] for line in block.text_lines))
    section = paragraph.part.document.sections[-1]
    width_pt, height_pt = section.page_width.pt, section.page_height.pt
    width = (right-left)/page.width*width_pt
    # OCR boxes bound ink. Word also reserves glyph side bearings and CJK
    # punctuation space; a small allowance prevents phantom continuation rows.
    allowance = min(size*.55, width_pt-right/page.width*width_pt)
    pf = paragraph.paragraph_format
    pf.left_indent = pf.right_indent = Pt(0)
    pf.space_before = pf.space_after = Pt(0)
    pf.keep_with_next = False
    pf.widow_control = False
    pf.line_spacing = Pt(max(size*1.05, pitch or size*1.25))
    if block.kind == 'image':
        # Exact text line height clips inline drawings in Word.
        pf.line_spacing = 1.0
    pf.page_break_before = False
    pf.first_line_indent = Pt(0)
    first = min(block.text_lines, key=lambda line: line.bbox[1]) if block.text_lines else None
    actual_pitch = max(size*1.05, pitch or size*1.25)
    if first is not None and first.ink_bbox is not None:
        top = first.ink_bbox[1]
        # A Word frame begins at the top of its line box, whereas source
        # coordinates bound visible ink. Compensate the line-box leading.
        top -= max(0, actual_pitch-size) * .5 / height_pt * page.height
    if len(block.text_lines) > 1:
        indent = ((first.ink_bbox or first.bbox)[0]-left)/page.width*width_pt
        if size*.4 <= indent <= size*4:
            pf.first_line_indent = Pt(indent)
    paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
    frame = OxmlElement('w:framePr')
    values = {'w': round((width+allowance)*20), 'x': round(left/page.width*width_pt*20),
              'y': round(top/page.height*height_pt*20), 'hAnchor': 'page', 'vAnchor': 'page',
              'wrap': 'none', 'hRule': 'auto', 'hSpace': 0, 'vSpace': 0}
    for name, value in values.items():
        frame.set(qn('w:'+name), str(value))
    paragraph._p.get_or_add_pPr().append(frame)


def position_table(table, page, bbox) -> None:
    left, top, _right, _bottom = bbox
    section = table.part.document.sections[-1]
    properties = OxmlElement('w:tblpPr')
    values = {'horzAnchor': 'page', 'vertAnchor': 'page',
              # Word positions the text edge, not the outer border. Make the
              # inset explicit instead of inheriting it from an Office theme.
              'tblpX': round(left/page.width*section.page_width.pt*20)+108,
              'tblpY': round(top/page.height*section.page_height.pt*20),
              'leftFromText': 0, 'rightFromText': 0, 'topFromText': 0, 'bottomFromText': 0}
    for name, value in values.items():
        properties.set(qn('w:'+name), str(value))
    table._tbl.tblPr.append(properties)
    margins = OxmlElement('w:tblCellMar')
    for side, width in (('left', 108), ('right', 108), ('top', 0), ('bottom', 0)):
        margin = OxmlElement('w:'+side)
        margin.set(qn('w:w'), str(width))
        margin.set(qn('w:type'), 'dxa')
        margins.append(margin)
    table._tbl.tblPr.append(margins)
    indent = table._tbl.tblPr.find(qn('w:tblInd'))
    if indent is not None:
        indent.set(qn('w:w'), '0')


def styled_segments(block, text):
    """Apply appearance offsets without replacing OCR or the user's edits."""
    if block.edited_text is not None or not block.bold_ranges:
        yield text, block.font_bold
        return
    original = [(i,c) for i,c in enumerate(block.original_text) if not c.isspace()]
    if ''.join(c for _,c in original) != ''.join(text.split()):
        yield text, block.font_bold
        return
    flags = [any(a <= i < b for a,b in block.bold_ranges) for i,_ in original]
    index = 0
    current = ''
    weight = block.font_bold
    for char in text:
        bold = flags[index] if not char.isspace() and index < len(flags) else weight
        if not char.isspace():
            index += 1
        if current and bold != weight:
            yield current, weight
            current = ''
        current += char
        weight = bold
    if current:
        yield current, weight
