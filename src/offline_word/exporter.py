"""Editable DOCX export with deliberate, failure-visible figure handling."""

from __future__ import annotations

import os
import errno
import math
import shutil
import statistics
import unicodedata
from pathlib import Path
from typing import cast

from docx import Document
from docx.document import Document as DocumentType
from docx.styles.style import ParagraphStyle
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_TABLE_DIRECTION, WD_ROW_HEIGHT_RULE
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Emu, Inches, Mm, Pt, RGBColor
from PIL import Image

from .domain import Block, PageResult, normalize_table_cells, table_needs_review
from .security import SessionStore, assert_local_path
from .line_geometry import complete_line_geometry
from .source_layout import (add_page_anchor, page_line_pitch, position_paragraph,
                            position_table, styled_segments, usable_source_geometry, edits_need_flow,
                            positioned_text_parts)
from .table_geometry import grid_boundaries


def _set_run_font(run, font_name: str, size: float = 12) -> None:
    run.font.name = font_name
    run.font.size = Pt(size)
    run.font.color.rgb = RGBColor(0, 0, 0)
    run._element.rPr.rFonts.set(qn("w:eastAsia"), font_name)


def _configure_document(document: DocumentType) -> None:
    section = document.sections[0]
    section.page_width = Mm(210)
    section.page_height = Mm(297)
    section.left_margin = Mm(18)
    section.right_margin = Mm(18)
    section.top_margin = Mm(18)
    section.bottom_margin = Mm(18)
    style = cast(ParagraphStyle, document.styles["Normal"])
    style.font.name = "宋体"
    style.font.size = Pt(12)
    assert style._element is not None
    style._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    heading = cast(ParagraphStyle, document.styles["Heading 1"])
    heading.font.name = "黑体"
    heading.font.size = Pt(16)
    heading.font.color.rgb = RGBColor(0, 0, 0)
    assert heading._element is not None
    heading._element.rPr.rFonts.set(qn("w:eastAsia"), "黑体")


def _safe_bbox(page: PageResult, block: Block) -> tuple[float, float, float, float] | None:
    if page.width <= 0 or page.height <= 0 or len(block.bbox) != 4:
        return None
    try:
        left, top, right, bottom = (float(value) for value in block.bbox)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (left, top, right, bottom)):
        return None
    left = min(max(left, 0.0), float(page.width))
    right = min(max(right, 0.0), float(page.width))
    top = min(max(top, 0.0), float(page.height))
    bottom = min(max(bottom, 0.0), float(page.height))
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _source_text_metrics(page: PageResult, block: Block) -> tuple[float | None, float | None]:
    """Estimate readable print size and line pitch from real OCR line geometry."""
    if page.width <= 0 or page.height <= 0:
        return None, None
    sizes: list[float] = []
    centers: list[float] = []
    for line in block.text_lines:
        try:
            left, top, right, bottom = (float(value) for value in line.bbox)
        except (ValueError, TypeError):
            continue
        if not all(math.isfinite(value) for value in (left, top, right, bottom)) or right <= left or bottom <= top:
            continue
        units = sum(0.33 if char.isspace() else 1.0 if unicodedata.east_asian_width(char) in {"W", "F"} else 0.56
                    for char in line.text)
        if units < (2 if block.kind == "table" else 4):
            continue
        size = (right - left) / page.width * (210 / 25.4 * 72) / units
        if 7 <= size <= 30:
            sizes.append(size)
            centers.append((top + bottom) / 2)
    if not sizes:
        if block.original_text.strip().isdigit() and block.text_lines:
            ink = block.text_lines[0].ink_bbox
            if ink is not None:
                height = (ink[3]-ink[1])/page.height*(297/25.4*72)
                return min(12.0, max(6.0, round(height*1.2*2)/2)), None
        return None, None
    # Round down to Word's half-point units: rounding up by even 0.25 pt can
    # force a full-width last character onto an extra line on every source row.
    # OCR boxes bound ink, whereas Office lays out full glyph advances. Leave
    # a small width allowance for font substitution and mixed Latin/CJK text.
    size = min(22.0, max(8.0, math.floor(statistics.median(sizes) * 0.97 * 2) / 2))
    centers.sort()
    pitches = [(following - prior) / page.height * (297 / 25.4 * 72)
               for prior, following in zip(centers, centers[1:])
               if following > prior]
    valid_pitches = [pitch for pitch in pitches if size * 1.05 <= pitch <= size * 2.4]
    pitch = statistics.median(valid_pitches) if valid_pitches else size * 1.25
    return size, pitch


def _paragraph_layout(
    paragraph,
    page: PageResult,
    block: Block,
    previous_bottom: float | None,
    *,
    page_break_before: bool = False,
) -> float | None:
    if page_break_before:
        paragraph.paragraph_format.page_break_before = True
    bbox = _safe_bbox(page, block)
    if bbox is None:
        return previous_bottom
    left, top, right, bottom = bbox
    section = paragraph.part.document.sections[-1]
    page_width = section.page_width
    page_height = section.page_height
    left_indent = max(0, round(page_width * left / page.width - section.left_margin))
    right_indent = max(0, round(page_width * (page.width - right) / page.width - section.right_margin))
    paragraph.paragraph_format.left_indent = Emu(left_indent)
    paragraph.paragraph_format.right_indent = Emu(right_indent)
    source_size, source_pitch = _source_text_metrics(page, block)
    paragraph.paragraph_format.line_spacing = Pt(source_pitch) if source_pitch else 1.12
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.space_after = Pt(0 if source_pitch else (3 if block.kind == "title" else 2))
    gap = top if previous_bottom is None else max(0.0, top - previous_bottom)
    if gap > 0:
        # Word still needs room for the preceding editable paragraph. A capped
        # proportional gap preserves obvious blank areas without forcing an
        # accidental extra page when OCR boxes slightly overlap.
        page_height_points = page_height / 12_700
        if source_pitch and source_size:
            consumed = section.top_margin / 12_700 if previous_bottom is None else max(0, source_pitch - source_size)
            gap_points = min(144.0, max(0.0, gap / page.height * page_height_points - consumed))
        else:
            gap_points = min(36.0, max(0.0, gap / page.height * page_height_points - 4.0))
        if block.text.strip().isdigit() and len(block.text.strip()) <= 6 and top >= page.height * 0.90:
            # A source footer sits near the physical sheet edge. Reserve the
            # paragraph's full line box so a lone page number cannot spill.
            gap_points = min(gap_points, 5.0)
        paragraph.paragraph_format.space_before = Pt(gap_points)
    center = (left + right) / 2 / page.width
    width_ratio = (right - left) / page.width
    if block.kind == "title" and abs(center - 0.5) <= 0.15:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.paragraph_format.keep_with_next = True
    elif center >= 0.70 and width_ratio <= 0.45:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    else:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
        if len(block.text_lines) > 1 and source_size:
            first = min(block.text_lines, key=lambda line: line.bbox[1])
            indent_points = (first.bbox[0] - left) / page.width * (page_width / 12_700)
            if source_size * 0.5 <= indent_points <= source_size * 4:
                paragraph.paragraph_format.first_line_indent = Pt(indent_points)
    return bottom


def _text_with_source_breaks(block: Block) -> str:
    if block.edited_text is not None or not block.text_lines:
        return block.text
    lines = sorted(block.text_lines, key=lambda line: (line.bbox[1], line.bbox[0]))
    source_text = "\n".join(line.text for line in lines)
    # Only layout whitespace may differ. Never replace even one punctuation
    # mark or character from the recognised baseline with line-level text.
    if "".join(source_text.split()) == "".join(block.text.split()):
        return source_text
    return block.text


def _append_text(
    document: DocumentType,
    block: Block,
    page: PageResult,
    previous_bottom: float | None,
    *,
    page_break_before: bool = False,
    unwrap: bool = False,
    continuation=None,
    continuation_single_row: bool = False,
) -> float | None:
    source_size, _source_pitch = _source_text_metrics(page, block)
    # Reflow uses the actual paragraph text. Introducing geometry-based breaks
    # first could split a known English word and then insert a false word space.
    text = block.text if unwrap else _text_with_source_breaks(block)
    if unwrap and block.edited_text is None:
        from .text_output import unwrap_text
        text = unwrap_text(text)
    if continuation is not None:
        from .text_output import unwrap_text
        # Keep a word boundary when an English word continues on the next sheet.
        joined = unwrap_text(continuation.text+"\n"+text)
        separator = " " if joined == continuation.text+" "+text else ""
        if separator:
            continuation.add_run(separator)
        paragraph = continuation
        font, size = block.font_name or "宋体", source_size or 10.5
        # A paragraph that began as one row at the sheet bottom had no measured
        # leading or body margin yet. Use its continuation to recover both.
        if len(block.text_lines) > 1 and _source_pitch:
            spacing = paragraph.paragraph_format.line_spacing
            prior_pitch = spacing.pt if hasattr(spacing, "pt") else size*1.25
            paragraph.paragraph_format.line_spacing = Pt(max(prior_pitch, _source_pitch))
            paragraph.paragraph_format.line_spacing_rule = WD_LINE_SPACING.AT_LEAST
            bbox = _safe_bbox(page, block)
            if continuation_single_row and bbox:
                section = document.sections[-1]
                assert section.page_width is not None and section.left_margin is not None and section.right_margin is not None
                left, _, right, _ = bbox
                indent = max(0, round(section.page_width*left/page.width-section.left_margin))
                prior_indent = paragraph.paragraph_format.left_indent or 0
                delta = (prior_indent-indent)/12700
                if size*.5 <= delta <= size*4:
                    paragraph.paragraph_format.left_indent = Emu(indent)
                    paragraph.paragraph_format.first_line_indent = Pt(delta)
                    paragraph.paragraph_format.right_indent = Emu(max(0, round(
                        section.page_width*(page.width-right)/page.width-section.right_margin)))
    elif block.kind == "title":
        paragraph = document.add_heading(level=1)
        font = block.font_name or "黑体"
        size = source_size or (16 if previous_bottom is None else 13)
    else:
        paragraph = document.add_paragraph()
        font = block.font_name or "宋体"
        size = source_size or 10.5
    for segment, bold in styled_segments(block, text):
        run = paragraph.add_run(segment)
        _set_run_font(run, font, size)
        if block.font_name or block.bold_ranges:
            run.bold = bold
    if continuation is not None:
        bbox = _safe_bbox(page, block)
        return bbox[3] if bbox else previous_bottom
    bottom = _paragraph_layout(
        paragraph,
        page,
        block,
        previous_bottom,
        page_break_before=page_break_before,
    )
    if unwrap:
        paragraph.paragraph_format.line_spacing_rule = WD_LINE_SPACING.AT_LEAST
        if previous_bottom is None:
            paragraph.paragraph_format.space_before = Pt(0)
    return bottom


def _set_table_layout(table, page: PageResult, block: Block) -> None:
    bbox = _safe_bbox(page, block)
    if bbox is None:
        return
    left, _top, right, _bottom = bbox
    section = table.part.document.sections[-1]
    printable_width = section.page_width - section.left_margin - section.right_margin
    table_width = max(int(Mm(25)), round(section.page_width * (right - left) / page.width))
    table_width = min(printable_width, table_width)
    table.autofit = False
    boundaries = grid_boundaries(block.cells, len(table.columns), horizontal=True)
    widths = ([round(table_width * (b-a)/(boundaries[-1]-boundaries[0]))
               for a,b in zip(boundaries, boundaries[1:])] if boundaries else
              [round(table_width/max(1, len(table.columns)))]*len(table.columns))
    for index, column in enumerate(table.columns):
        column.width = Emu(widths[index])
        for cell in column.cells:
            cell.width = Emu(widths[index])
    row_boundaries = grid_boundaries(block.cells, len(table.rows), horizontal=False)
    if row_boundaries:
        for row, a, b in zip(table.rows, row_boundaries, row_boundaries[1:]):
            row.height = Emu(round(section.page_height*(b-a)/page.height))
            row.height_rule = WD_ROW_HEIGHT_RULE.AT_LEAST
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    properties = table._tbl.tblPr
    indentation = properties.find(qn("w:tblInd"))
    if indentation is None:
        indentation = OxmlElement("w:tblInd")
        properties.append(indentation)
    left_indent = max(0, round(section.page_width * left / page.width - section.left_margin))
    indentation.set(qn("w:w"), str(round(left_indent / 635)))
    indentation.set(qn("w:type"), "dxa")


def _append_table(
    document: DocumentType,
    block: Block,
    page: PageResult,
    previous_bottom: float | None,
    *,
    page_break_before: bool = False,
) -> float | None:
    cells = normalize_table_cells(block.cells, block.rows, block.cols)
    block.needs_review = block.needs_review or table_needs_review(cells)
    rows = block.rows or max((cell.row + cell.rowspan for cell in cells), default=0)
    cols = block.cols or max((cell.col + cell.colspan for cell in cells), default=0)
    if rows <= 0 or cols <= 0:
        if block.image_path:
            _append_figure(
                document,
                block,
                page,
                previous_bottom,
                page_break_before=page_break_before,
            )
            page_break_before = False
        if block.text:
            return _append_text(
                document,
                Block(id=block.id, kind="text", bbox=block.bbox, original_text=block.text),
                page,
                previous_bottom,
                page_break_before=page_break_before,
            )
        bbox = _safe_bbox(page, block)
        return bbox[3] if bbox else previous_bottom
    if block.needs_review and block.image_path:
        _append_figure(
            document,
            block,
            page,
            previous_bottom,
            page_break_before=page_break_before,
        )
        page_break_before = False
    if page_break_before:
        anchor = document.add_paragraph()
        anchor.paragraph_format.page_break_before = True
        anchor.paragraph_format.space_after = Pt(0)
    table = document.add_table(rows=rows, cols=cols)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.table_direction = WD_TABLE_DIRECTION.LTR
    table.style = "Table Grid"
    _set_table_layout(table, page, block)
    for cell in cells:
        anchor = table.cell(cell.row, cell.col)
        if cell.rowspan > 1 or cell.colspan > 1:
            anchor = anchor.merge(table.cell(cell.row + cell.rowspan - 1, cell.col + cell.colspan - 1))
        anchor.text = ""
        run = anchor.paragraphs[0].add_run(cell.text)
        _set_run_font(run, block.font_name or "宋体", _source_text_metrics(page, block)[0] or 10.5)
        anchor.paragraphs[0].paragraph_format.space_after = Pt(0)
    review_cells = [cell for cell in cells if bool(getattr(cell, "_table_needs_review", False))]
    if review_cells:
        paragraph = document.add_paragraph()
        run = paragraph.add_run("【需核对的原始表格文本】" + "\n".join(cell.original_text for cell in review_cells if cell.original_text))
        _set_run_font(run, "宋体")
    bbox = _safe_bbox(page, block)
    return bbox[3] if bbox else previous_bottom


def _append_figure(
    document: DocumentType,
    block: Block,
    page: PageResult,
    previous_bottom: float | None,
    *,
    page_break_before: bool = False,
) -> float | None:
    if not block.image_path:
        raise FileNotFoundError("figure_missing")
    figure_path = assert_local_path(block.image_path, "remote_figure_path")
    if not figure_path.is_file():
        raise FileNotFoundError("figure_missing")
    with Image.open(figure_path) as image:
        if image.width < 1 or image.height < 1:
            raise FileNotFoundError("figure_missing")
        section = document.sections[-1]
        assert section.page_width is not None and section.page_height is not None
        assert section.left_margin is not None and section.right_margin is not None
        assert section.top_margin is not None and section.bottom_margin is not None
        printable_width = int(section.page_width - section.left_margin - section.right_margin)
        printable_height = int(section.page_height - section.top_margin - section.bottom_margin)
        bbox = _safe_bbox(page, block)
        desired_width = printable_width
        if bbox is not None:
            desired_width = max(int(Mm(20)), round(section.page_width * (bbox[2] - bbox[0]) / page.width))
            desired_width = min(printable_width, desired_width)
        scale = min(desired_width / image.width, min(printable_height, int(Inches(8.75))) / image.height)
        width, height = Emu(round(image.width * scale)), Emu(round(image.height * scale))
    paragraph = document.add_paragraph()
    _paragraph_layout(
        paragraph,
        page,
        block,
        previous_bottom,
        page_break_before=page_break_before,
    )
    if paragraph.alignment is None:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.add_run().add_picture(str(figure_path), width=width, height=height)
    bbox = _safe_bbox(page, block)
    return bbox[3] if bbox else previous_bottom


def _reserve_unique_path(requested: Path) -> Path:
    requested = assert_local_path(requested, "remote_export_path")
    requested.parent.mkdir(parents=True, exist_ok=True)
    suffix = requested.suffix or ".docx"
    stem = requested.stem if requested.suffix else requested.name
    for index in range(10_000):
        target = requested.parent / (f"{stem}{suffix}" if index == 0 else f"{stem} ({index}){suffix}")
        try:
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        else:
            os.close(descriptor)
            return target
    raise FileExistsError("export_name_unavailable")


def export_docx(pages: list[PageResult], path: Path, *, layout_mode: str = "source", remove_page_numbers: bool = False, progress=None, check_cancel=None) -> Path:
    """Save page results as an editable DOCX without overwriting existing output."""
    if not pages:
        raise ValueError("no_pages")
    check = check_cancel or (lambda: None)
    check()
    if layout_mode not in {"source", "flow", "paragraph", "text_only"}:
        raise ValueError("invalid_layout_mode")
    if remove_page_numbers:
        from .page_numbers import without_page_numbers
        pages, _ = without_page_numbers(pages)
    if layout_mode == "paragraph":
        from .paragraph_output import paragraph_page
        pages = [paragraph_page(page) for page in pages]
    from .export_checks import assert_text_coverage, expected_text
    expected = expected_text(pages)
    document = Document()
    if layout_mode == "text_only":
        from .text_output import continuous_text
        # A single neutral style avoids per-glyph theme fallback in Word.
        # No source headings, emphasis, indents or line breaks are copied.
        style = cast(ParagraphStyle, document.styles["Normal"])
        style.font.name = "宋体"
        style.font.size = Pt(12)
        style.font.bold = False
        style.font.italic = False
        assert style._element is not None
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
        document.add_paragraph(continuous_text(pages))
        assert_text_coverage(document, continuous_text(pages))
        check()
        if progress:
            progress(len(pages), len(pages))
        return _save_document(document, path, check_cancel=check)
    _configure_document(document)
    last_block = None
    last_paragraph = None
    for page_index, page in enumerate(pages):
        check()
        source_layout = layout_mode == "source" and usable_source_geometry(page) and all(
            _safe_bbox(page, block) is not None
            and not (block.kind == "table" and block.needs_review)
            and not edits_need_flow(block)
            and (block.kind in {"image", "table"} or complete_line_geometry(block))
            for block in page.blocks
        )
        pitch = page_line_pitch(page) if source_layout else None
        body_sizes = [_source_text_metrics(page, block)[0] for block in page.blocks
                      if block.kind == "text" and len(block.text_lines) > 1]
        body_size = statistics.median([size for size in body_sizes if size]) if any(body_sizes) else None
        if source_layout:
            add_page_anchor(document, page_break_before=page_index > 0)
        previous_bottom: float | None = None
        export_blocks = []
        for block in page.blocks:
            source_size, source_pitch = _source_text_metrics(page, block)
            parts = positioned_text_parts(block, page, body_size or source_size or 10.5, source_pitch or pitch) if (
                source_layout and block.kind in {"text", "title"}) else [block]
            export_blocks.extend(parts)
        for block_index, block in enumerate(export_blocks):
            check()
            body = document.element.body
            previous = body[-2] if len(body) > 1 else None
            page_break_before = not source_layout and layout_mode != "paragraph" and page_index > 0 and block_index == 0
            if block.kind == "table":
                previous_bottom = _append_table(
                    document,
                    block,
                    page,
                    previous_bottom,
                    page_break_before=page_break_before,
                )
            elif block.kind == "image":
                previous_bottom = _append_figure(
                    document,
                    block,
                    page,
                    previous_bottom,
                    page_break_before=page_break_before,
                )
            else:
                continuation = None
                if layout_mode == "paragraph" and block_index == 0 and last_block is not None and last_paragraph is not None:
                    from .paragraph_output import continues_on_next_page
                    if continues_on_next_page(last_block,block):
                        continuation = last_paragraph
                previous_bottom = _append_text(
                    document,
                    block,
                    page,
                    previous_bottom,
                    page_break_before=page_break_before,
                    unwrap=layout_mode == "paragraph",
                    continuation=continuation,
                    continuation_single_row=continuation is not None and last_block is not None and len(last_block.text_lines) == 1,
                )
            last_block = block
            from docx.text.paragraph import Paragraph
            from docx.table import Table
            appended = list(previous.itersiblings()) if previous is not None else list(body)
            paragraphs = [Paragraph(node, document) for node in appended if node.tag == qn('w:p')]
            tables = [Table(node, document) for node in appended if node.tag == qn('w:tbl')]
            last_paragraph = (paragraphs[-1] if paragraphs else last_paragraph) if block.kind in {"text", "title"} else None
            if source_layout:
                bbox = _safe_bbox(page, block)
                for paragraph in paragraphs:
                    size = (body_size if block.kind == "text" and len(block.text.strip()) > 8
                            else _source_text_metrics(page, block)[0]) or 10.5
                    if block.kind != "image":
                        for run in paragraph.runs:
                            run.font.size = Pt(size)
                    local_pitch = _source_text_metrics(page, block)[1] if len(block.text_lines) > 1 else pitch
                    position_paragraph(paragraph, page, block, size, local_pitch or pitch, bbox)
                for table in tables:
                    position_table(table, page, bbox)
                    for row in table.rows:
                        for cell in row.cells:
                            for paragraph in cell.paragraphs:
                                paragraph.paragraph_format.space_after = Pt(0)
                                for run in paragraph.runs:
                                    _set_run_font(run, block.font_name or next((b.font_name for b in page.blocks if b.font_name), "宋体"),
                                                  _source_text_metrics(page, block)[0] or body_size or 10.5)

        if progress:
            progress(page_index + 1, len(pages))
    check()
    assert_text_coverage(document, expected)
    return _save_document(document, path, check_cancel=check)


def _save_document(document, path: Path, *, check_cancel=lambda: None) -> Path:
    check_cancel()
    target = _reserve_unique_path(Path(path))
    try:
        # Generated content stays in the private, crash-cleaned session store
        # until the document is complete.  The reserved target prevents a
        # filename race; a cross-volume copy may briefly expose that target.
        with SessionStore() as session:
            temporary_path = session.path / "export.docx"
            document.save(temporary_path)
            check_cancel()
            try:
                os.replace(temporary_path, target)
            except OSError as error:
                if error.errno != errno.EXDEV:
                    raise
                shutil.copyfile(temporary_path, target)
        return target
    except Exception:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise
