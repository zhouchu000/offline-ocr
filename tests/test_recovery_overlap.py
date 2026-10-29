from pathlib import Path

from PIL import Image

from offline_word.accuracy import DetailResult
from offline_word.domain import Block, ImageJob, PageResult, TextLine
from offline_word import worker
from offline_word.exporter import export_docx
from docx import Document
from docx.oxml.ns import qn


def test_recovery_does_not_print_an_existing_paragraph_line_twice(tmp_path: Path):
    source = tmp_path / "page.png"
    Image.new("RGB", (1000, 1400), "white").save(source)
    first, second = "第一行是已识别的正文。", "第二行也是已识别的正文。"
    block = Block("body", "text", (100, 100, 900, 300), original_text=first+second)
    page = PageResult("p", 0, 1000, 1400, str(source), [block])

    class Runtime:
        def predict_detail(self, _path):
            return DetailResult(second, .99)

    worker._recover_missing_lines(page, source, [(95, 245, 905, 290)], Runtime(), tmp_path)
    assert "".join(b.text for b in page.blocks).count(second) == 1
    # Recovered geometry is still useful when the text already exists.
    assert any(line.text == second for line in block.text_lines)


def test_partial_line_coordinates_do_not_move_an_entire_paragraph_to_its_last_line(tmp_path: Path):
    source = tmp_path / "page.png"
    Image.new("RGB", (1000, 1400), "white").save(source)
    first, second = "第一行是已有正文。", "第二行是已有正文。"
    result = worker.page_from_prediction(ImageJob("p", str(source)), source, {
        "parsing_res_list": [{"block_label": "text", "block_bbox": [100, 100, 900, 300],
                              "block_content": first+second}],
        "overall_ocr_res": {"rec_texts": [second], "rec_boxes": [[100, 250, 900, 290]],
                            "rec_scores": [.99]},
    })
    assert result.blocks[0].bbox[1] == 100


def test_different_reading_of_same_physical_line_is_a_candidate(tmp_path):
    source = tmp_path / "page.png"
    Image.new("RGB", (1000, 1400), "white").save(source)
    text = "这一行已有识别结果需要人工确认。"
    block = Block("body", "text", (100, 100, 900, 140), original_text=text,
                  text_lines=[TextLine(text, (100, 100, 900, 140))])
    page = PageResult("p", 0, 1000, 1400, str(source), [block])
    alternative = text.replace("已有", "己有")
    class Runtime:
        def predict_detail(self, _path): return DetailResult(alternative, .99)
    worker._recover_missing_lines(page, source, [(95, 103, 905, 147)], Runtime(), tmp_path)
    assert len(page.blocks) == 1 and block.text == text
    assert block.needs_review and alternative in block.candidates


def test_missing_middle_line_does_not_shift_existing_lines_onto_the_recovery(tmp_path):
    texts = ["这是用于检查文字重叠的第%s行正文。" % number for number in "一二三四"]
    boxes = [(100, y, 900, y+32) for y in (100, 160, 220, 280)]
    original = Block("body", "text", (100, 100, 900, 312),
                     original_text=texts[0]+texts[2]+texts[3],
                     text_lines=[TextLine(texts[i], boxes[i]) for i in (0, 2, 3)])
    recovered = Block("recovered", "text", boxes[1], original_text=texts[1],
                      text_lines=[TextLine(texts[1], boxes[1])])
    page = PageResult("p", 0, 1000, 1400, "unused", [original, recovered])
    doc = Document(export_docx([page], tmp_path / "recovered.docx"))
    for i, text in enumerate(texts):
        para = next(p for p in doc.paragraphs if p.text == text)
        frame = para._p.pPr.find(qn("w:framePr"))
        expected = boxes[i][1]/page.height*doc.sections[0].page_height.pt*20
        assert abs(int(frame.get(qn("w:y")))-expected) < 2


def test_incomplete_geometry_cannot_anchor_a_long_paragraph_on_its_only_known_row(tmp_path):
    text = "第一行文字。第二行文字。第三行文字。"
    block = Block("body", "text", (100, 100, 900, 300), original_text=text,
                  text_lines=[TextLine("第三行文字。", (100, 260, 900, 300))])
    doc = Document(export_docx([PageResult("p", 0, 1000, 1400, "unused", [block])], tmp_path / "partial.docx"))
    assert text in [p.text for p in doc.paragraphs]
    assert all(p._p.pPr.find(qn("w:framePr")) is None for p in doc.paragraphs)


def test_normalized_ligatures_cannot_truncate_original_text_when_positioning_rows(tmp_path):
    text = "office第一行第二行完整原文"
    block = Block("body", "text", (100, 100, 900, 400), original_text=text,
                  text_lines=[TextLine("oﬃce第一行", (100, 100, 900, 135)),
                              TextLine("第二行完整原文", (100, 350, 900, 385))])
    doc = Document(export_docx([PageResult("p", 0, 1000, 1400, "unused", [block])], tmp_path / "ligature.docx"))
    assert "".join(p.text.replace("\n", "") for p in doc.paragraphs) == text
