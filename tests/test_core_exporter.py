from pathlib import Path
import errno

import pytest
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, RGBColor
from PIL import Image

from offline_word.domain import Block, Cell, PageResult
from offline_word import exporter
from offline_word.exporter import export_docx


def _page(tmp_path: Path) -> PageResult:
    preview = tmp_path / "page.png"
    preview.write_bytes(b"not used by text export")
    return PageResult(
        id="page-1", page_index=0, width=100, height=100, processed_image_path=str(preview),
        blocks=[
            Block(id="heading", kind="title", bbox=(0, 0, 1, 1), original_text="标题"),
            Block(id="table", kind="table", bbox=(0, 0, 1, 1), rows=2, cols=2, cells=[
                Cell(row=0, col=0, original_text="合并", rowspan=1, colspan=2),
                Cell(row=1, col=0, original_text="", edited_text=""),
                Cell(row=1, col=1, original_text="原文", edited_text="修改后"),
            ]),
            Block(id="body", kind="text", bbox=(0, 0, 1, 1), original_text="正文"),
        ],
    )


def test_export_docx_keeps_empty_edits_table_merges_and_latest_text(tmp_path: Path):
    result = export_docx([_page(tmp_path)], tmp_path / "out.docx")

    document = Document(result)
    assert [paragraph.text for paragraph in document.paragraphs] == ["标题", "正文"]
    assert document.paragraphs[0].runs[0].font.color.rgb == RGBColor(0, 0, 0)
    table = document.tables[0]
    assert table.cell(0, 0).text == "合并"
    assert table.cell(0, 1)._tc is table.cell(0, 0)._tc
    assert table.cell(1, 0).text == ""
    assert table.cell(1, 1).text == "修改后"


def test_export_docx_does_not_overwrite_and_allocates_unique_path(tmp_path: Path):
    first = export_docx([_page(tmp_path)], tmp_path / "out.docx")
    second = export_docx([_page(tmp_path)], tmp_path / "out.docx")

    assert first.name == "out.docx"
    assert second.name == "out (1).docx"


def test_export_docx_rejects_missing_figure_instead_of_silent_drop(tmp_path: Path):
    page = _page(tmp_path)
    page.blocks.append(Block(id="figure", kind="image", bbox=(0, 0, 1, 1), image_path=str(tmp_path / "gone.png")))

    with pytest.raises(FileNotFoundError, match="figure_missing"):
        export_docx([page], tmp_path / "out.docx")


def test_export_docx_rejects_empty_page_selection(tmp_path: Path):
    with pytest.raises(ValueError, match="no_pages"):
        export_docx([], tmp_path / "out.docx")


def test_export_docx_caps_tall_figure_inside_printable_page_height(tmp_path: Path):
    figure = tmp_path / "tall.png"
    Image.new("RGB", (100, 2_000), "white").save(figure)
    page = _page(tmp_path)
    page.blocks.append(Block(id="figure", kind="image", bbox=(0, 0, 1, 1), image_path=str(figure)))

    output = export_docx([page], tmp_path / "tall.docx")

    shape = Document(output).inline_shapes[0]
    assert shape.width <= Inches(6.25)
    assert shape.height <= Inches(8.75)


def test_wide_figure_fits_actual_a4_section_margins(tmp_path):
    figure = tmp_path / 'wide.png'
    Image.new('RGB', (2000, 100), 'white').save(figure)
    page = _page(tmp_path)
    page.blocks.append(Block(id='image', kind='image', bbox=(0, 0, 1, 1), image_path=str(figure)))
    document = Document(export_docx([page], tmp_path / 'wide.docx'))
    section = document.sections[0]
    assert document.inline_shapes[0].width <= section.page_width - section.left_margin - section.right_margin


def test_export_docx_handles_cross_volume_private_temp_move(tmp_path, monkeypatch):
    def cross_volume(*_):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(exporter.os, "replace", cross_volume)

    output = export_docx([_page(tmp_path)], tmp_path / "cross-volume.docx")

    assert output.is_file()
    assert Document(output).tables[0].cell(1, 1).text == "修改后"


def test_export_docx_removes_reserved_target_when_cross_volume_copy_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(exporter.os, "replace", lambda *_: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross-device link")))
    monkeypatch.setattr(exporter.shutil, "copyfile", lambda *_: (_ for _ in ()).throw(OSError(errno.EIO, "copy failed")))

    with pytest.raises(OSError, match="copy failed"):
        export_docx([_page(tmp_path)], tmp_path / "copy-fails.docx")

    assert not (tmp_path / "copy-fails.docx").exists()


def test_export_docx_uses_source_coordinates_for_editable_paragraph_layout(tmp_path: Path):
    page = PageResult(
        id="layout",
        page_index=0,
        width=1000,
        height=1400,
        processed_image_path=str(tmp_path / "page.png"),
        blocks=[
            Block(id="title", kind="title", bbox=(220, 80, 780, 150), original_text="居中标题"),
            Block(id="lead", kind="text", bbox=(100, 260, 900, 330), original_text="左侧正文"),
            Block(id="right", kind="text", bbox=(600, 720, 930, 780), original_text="右侧短注"),
        ],
    )

    output = export_docx([page], tmp_path / "layout.docx")
    document = Document(output)
    title, lead, right = document.paragraphs

    assert title.alignment == WD_ALIGN_PARAGRAPH.CENTER
    assert title.runs[0].font.size.pt >= 16
    assert lead.paragraph_format.left_indent is not None
    assert 2 <= lead.paragraph_format.left_indent.mm <= 5
    assert right.paragraph_format.left_indent.mm > lead.paragraph_format.left_indent.mm
    assert right.paragraph_format.space_before.pt >= 12
    assert right.paragraph_format.right_indent is not None


def test_export_docx_invalid_bbox_falls_back_without_extreme_spacing(tmp_path: Path):
    page = PageResult(
        id="fallback",
        page_index=0,
        width=0,
        height=0,
        processed_image_path=str(tmp_path / "page.png"),
        blocks=[Block(id="body", kind="text", bbox=(0, 0, 0, 0), original_text="正文")],
    )

    output = export_docx([page], tmp_path / "fallback.docx")
    paragraph = Document(output).paragraphs[0]

    assert paragraph.text == "正文"
    assert paragraph.paragraph_format.space_before is None


def test_export_docx_starts_following_source_page_without_a_break_only_paragraph(tmp_path: Path):
    first = PageResult(
        id="one", page_index=0, width=1000, height=1400, processed_image_path="unused",
        blocks=[Block(id="one-body", kind="text", bbox=(100, 100, 900, 1300), original_text="第一页")],
    )
    second = PageResult(
        id="two", page_index=0, width=1000, height=1400, processed_image_path="unused",
        blocks=[Block(id="two-body", kind="text", bbox=(100, 100, 900, 300), original_text="第二页")],
    )

    output = export_docx([first, second], tmp_path / "two-pages.docx")
    paragraphs = Document(output).paragraphs

    assert [paragraph.text for paragraph in paragraphs] == ["第一页", "第二页"]
    assert paragraphs[1].paragraph_format.page_break_before is True


def test_export_estimates_print_size_and_line_pitch_from_source_lines(tmp_path: Path):
    from offline_word.domain import TextLine
    line = "中文印刷文字" * 4
    block = Block(id="body", kind="text", bbox=(180, 220, 960, 315), original_text=line * 2)
    block.text_lines = [
        TextLine(line, (180, 220, 960, 260), 0.99),
        TextLine(line, (180, 275, 960, 315), 0.99),
    ]
    page = PageResult(id="page", page_index=0, width=1200, height=1700,
                      processed_image_path="unused.png", blocks=[block])
    path = export_docx([page], tmp_path / "layout.docx")
    paragraph = next(p for p in Document(path).paragraphs if p.text)
    assert 15.5 <= paragraph.runs[0].font.size.pt <= 16.5
    assert 26 <= paragraph.paragraph_format.line_spacing.pt <= 28
    block.edited_text = "人工完整修改后的内容"
    edited = Document(export_docx([page], tmp_path / "edited.docx"))
    assert next(p for p in edited.paragraphs if p.text).text == block.edited_text


def test_source_line_breaks_fit_printed_text_but_manual_line_breaks_are_preserved(tmp_path: Path):
    from offline_word.domain import TextLine
    first, second = "原稿的第一行", "原稿的第二行"
    block = Block(id="body", kind="text", bbox=(100, 100, 900, 300), original_text=first + second)
    block.text_lines = [TextLine(first, (100, 100, 900, 150)), TextLine(second, (100, 200, 900, 250))]
    page = PageResult(id="p", page_index=0, width=1000, height=1400, processed_image_path="unused", blocks=[block])
    rendered = Document(export_docx([page], tmp_path / "source-lines.docx"))
    assert "\n".join(p.text for p in rendered.paragraphs if p.text) == first + "\n" + second
    block.edited_text = first + "\n\n" + second
    edited = Document(export_docx([page], tmp_path / "edited-lines.docx"))
    assert next(p for p in edited.paragraphs if p.text).text == block.edited_text
    block.edited_text = None
    block.original_text = "保留全角（1）"
    block.text_lines = [TextLine("保留全角(1)", (100, 100, 900, 150))]
    punctuation = Document(export_docx([page], tmp_path / "punctuation.docx"))
    assert next(p for p in punctuation.paragraphs if p.text).text == "保留全角（1）"
