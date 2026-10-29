from copy import deepcopy

from docx import Document
import pytest

from offline_word.domain import Block, Cell, PageResult, TextLine
from offline_word.exporter import export_docx
from offline_word.page_numbers import without_page_numbers


def page_with_number(text="12", *, box=(480, 1320, 520, 1340), index=0):
    body = "2026 年共 12 项，编号 1234，金额 3.14 元。"
    return PageResult(f"p{index}", index, 1000, 1400, "unused.png", [
        Block("body", "text", (100, 200, 900, 1100), body,
              text_lines=[TextLine(body, (100, 200, 900, 230))]),
        Block("folio", "text", box, text, text_lines=[TextLine(text, box)]),
    ])


@pytest.mark.parametrize("text", ["1", "999", "１２", "- 12 -", "—12—", "(12)", "[12]",
                                  "第 12 页", "第十二页", "第12页，共30页", "Page 12", "Page 12 of 30"])
def test_removes_only_the_isolated_footer(text):
    page = page_with_number(text)
    original = deepcopy(page)
    pages, count = without_page_numbers([page])
    assert count == 1
    assert [block.id for block in pages[0].blocks] == ["body"]
    assert page == original


@pytest.mark.parametrize("text", ["2026", "12345", "0", "12.5", "3/12", "IV", "12元", "编号12",
                                  "请看第12页", "12、", "共12项", "附注 12", "12%", "1-12"])
def test_uncertain_digits_dates_and_body_references_survive(text):
    page = page_with_number(text)
    pages, count = without_page_numbers([page])
    assert count == 0 and pages == [page]


@pytest.mark.parametrize("box", [(480, 700, 520, 725), (480, 1120, 520, 1145),
                                 (300, 1320, 330, 1340), (0, 0, 1000, 1400),
                                 (480, float("nan"), 520, 1340), (480, 1320, 520, 1500)])
def test_body_numbers_close_to_body_and_untrusted_coordinates_survive(box):
    page = page_with_number(box=box)
    pages, count = without_page_numbers([page])
    assert count == 0 and len(pages[0].blocks) == 2


def test_numeric_table_cells_and_other_same_row_text_survive():
    page = page_with_number()
    page.blocks.append(Block("row", "text", (100, 1320, 400, 1340), "正文末行的数量"))
    page.blocks.append(Block("table", "table", (700, 1300, 900, 1380),
                             rows=1, cols=1, cells=[Cell(0, 0, "12")]))
    pages, count = without_page_numbers([page])
    assert count == 0 and pages == [page]


@pytest.mark.parametrize("texts", [("3/12", "4/12"), ("iv", "v"), ("IX", "X")])
def test_ambiguous_notation_requires_matching_batch_sequence(texts):
    pages = [page_with_number(text, index=index) for index, text in enumerate(texts)]
    filtered, count = without_page_numbers(pages)
    assert count == 2 and all(len(page.blocks) == 1 for page in filtered)
    unrelated = [page_with_number(texts[0], index=0), page_with_number(texts[0], index=1)]
    assert without_page_numbers(unrelated)[1] == 0


@pytest.mark.parametrize("box", [(30, 50, 110, 75), (890, 1320, 970, 1345)])
def test_explicit_top_and_outside_folios(box):
    assert without_page_numbers([page_with_number("第12页", box=box)])[1] == 1


def test_bare_numeric_heading_needs_repeated_header_evidence():
    pages = [page_with_number(str(i+1), box=(480, 50, 520, 75), index=i) for i in range(2)]
    assert without_page_numbers(pages[:1])[1] == 0
    assert without_page_numbers(pages)[1] == 2
    page = page_with_number()
    page.blocks[-1].kind = "title"
    assert without_page_numbers([page])[1] == 0


def test_partial_line_coordinates_cannot_crash_page_number_export():
    page = page_with_number()
    page.blocks[0].text_lines[0].bbox = ()
    assert without_page_numbers([page])[1] == 1


def test_removal_from_merged_paragraph_preserves_body_and_bold_ranges():
    body, number = "末段共 12 项，保留 2026。", "12"
    block = Block("merged", "text", (100, 1100, 900, 1340), body+"\n"+number,
                  font_name="楷体", bold_ranges=[(0, 4)], text_lines=[
                      TextLine(body, (100, 1100, 900, 1130)), TextLine(number, (480, 1320, 520, 1340))])
    page = PageResult("p", 0, 1000, 1400, "unused", [block])
    original = deepcopy(page)
    filtered, count = without_page_numbers([page])
    assert count == 1
    result = filtered[0].blocks[0]
    assert result.text == body and result.bbox == (100, 1100, 900, 1130)
    assert result.bold_ranges == [(0, 4)] and result.font_name == "楷体"
    assert len(result.text_lines) == 1 and page == original


def test_incomplete_and_manually_edited_paragraphs_are_not_cut_using_old_coordinates():
    page = page_with_number()
    page.blocks[1].edited_text = "请保留 12"
    assert without_page_numbers([page])[1] == 0
    page.blocks[1].edited_text = "第13页"
    assert without_page_numbers([page])[1] == 1
    page.blocks = [Block("mixed", "text", (100, 100, 900, 1340), "正文\n12",
                         text_lines=[TextLine("12", (480, 1320, 520, 1340))])]
    assert without_page_numbers([page])[1] == 0


@pytest.mark.parametrize("mode", ["source", "flow", "paragraph", "text_only"])
def test_export_toggle_removes_numbers_and_can_restore_them_without_ocr(tmp_path, mode):
    page = page_with_number()
    page.blocks[0].edited_text = "校对后有 12 项，保留 2026 和 3.14。"
    before = deepcopy(page)
    clean = Document(export_docx([page], tmp_path / "clean.docx", layout_mode=mode, remove_page_numbers=True))
    default = Document(export_docx([page], tmp_path / "default.docx", layout_mode=mode))
    clean_text = "".join(p.text for p in clean.paragraphs)
    default_text = "".join(p.text for p in default.paragraphs)
    assert clean_text == page.blocks[0].text
    assert default_text == clean_text+"12"
    assert page == before
    if mode == "text_only":
        assert len(clean.paragraphs) == 1 and "<w:br" not in clean.element.xml
