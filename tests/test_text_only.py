from pathlib import Path

from docx import Document
from PIL import Image
import pytest

from offline_word.domain import Block, Cell, ImageJob, PageResult, TextLine
from offline_word.text_output import continuous_text
from offline_word.exporter import export_docx
from offline_word import worker, source_style


def test_text_only_exports_latest_text_without_source_formatting(tmp_path):
    page = PageResult("p", 0, 100, 100, "unused.png", blocks=[
        Block("title", "title", (0, 0, 100, 20), "旧标题", edited_text="新标题", font_bold=True),
        Block("body", "text", (0, 20, 100, 50), "第一行\n第二行\nHello\nworld", font_name="楷体"),
        Block("table", "table", (0, 50, 100, 80), rows=1, cols=2, cells=[
            Cell(0, 0, "姓名"), Cell(0, 1, "原名", edited_text="新名")]),
        Block("figure", "image", (0, 80, 100, 100), image_path="must-not-open.png"),
    ])
    other = PageResult("p2", 1, 100, 100, "unused.png", blocks=[
        Block("last", "text", (0, 0, 100, 40), "第三页文字", edited_text="后页文字")])
    doc = Document(export_docx([page, other], tmp_path / "text.docx", layout_mode="text_only"))
    assert len(doc.paragraphs) == 1
    assert doc.paragraphs[0].text == "新标题第一行第二行Hello world姓名新名后页文字"
    assert not doc.tables and not doc.inline_shapes
    xml = doc.element.xml
    for token in ("<w:br", "<w:framePr", "<w:pageBreakBefore", "<w:tbl", "<w:drawing", "Heading1", "楷体"):
        assert token not in xml
    assert all(run.bold is None for run in doc.paragraphs[0].runs)


@pytest.mark.parametrize("auto_proofread", [False, True])
def test_text_mode_uses_only_ocr_and_skips_source_appearance(tmp_path, monkeypatch, auto_proofread):
    image = tmp_path / "page.png"
    Image.new("RGB", (300, 100), "white").save(image)
    job = ImageJob("p", str(image))
    job.text_only = True
    job.auto_proofread = auto_proofread
    events = []
    class Lease:
        def close(self): pass
    class Sender:
        def send(self, event): events.append(event)
        def close(self): pass
    class Engine:
        def predict(self, _):
            yield {"rec_texts": ["第一行", "第二行"], "rec_scores": [0.99, 0.99],
                   "rec_boxes": [[10, 10, 290, 30], [10, 50, 290, 70]]}
    monkeypatch.setattr(worker, "_silence_child_output", lambda: None)
    monkeypatch.setattr(worker, "claim_session_lease", lambda _: Lease())
    monkeypatch.setattr(worker, "validate_models", lambda _: {})
    monkeypatch.setattr(worker, "configure_offline_env", lambda *_: None)
    monkeypatch.setattr(worker, "deny_network", lambda: None)
    monkeypatch.setattr(worker, "_create_engine", lambda *_: pytest.fail("Text mode loaded the structure/table engine"))
    monkeypatch.setattr(worker, "_create_plain_engine", lambda *_: Engine())
    monkeypatch.setattr(worker, "_prepare_for_worker", lambda *_: image)
    monkeypatch.setattr(worker, "_prepare_with_detail_for_worker", lambda *_: (image, image))
    monkeypatch.setattr(source_style, "annotate_source_style", lambda *_: pytest.fail("Text mode restored fonts"))
    worker._process_pages("models", str(tmp_path), [job], Sender())
    result = next(event["result"] for event in events if event["event"] == "page_done")
    assert "".join(b.original_text for b in result.blocks) == "第一行第二行"
    assert result.recognition_warning is None
    assert not any(block.needs_review for block in result.blocks)
    assert events[-1]["event"] == "finished"


def test_plain_text_follows_columns_beneath_a_full_width_heading():
    lines = [("文档标题", (100, 50, 900, 90)),
             ("左栏第一行完整文字。", (100, 120, 450, 150)),
             ("右栏第一行完整文字。", (550, 120, 900, 150)),
             ("左栏第二行完整文字。", (100, 180, 450, 210)),
             ("右栏第二行完整文字。", (550, 180, 900, 210))]
    page = PageResult("p", 0, 1000, 1400, "unused", [
        Block(str(i), "text", bbox, text) for i, (text, bbox) in enumerate(lines)])
    assert continuous_text([page]) == "".join(lines[i][0] for i in (0, 1, 3, 2, 4))


def test_flattening_inserts_a_recovered_middle_line_in_text_order():
    first, middle, last = "第一行正文。", "补回的中间行。", "最后一行正文。"
    body = Block("body", "text", (100, 100, 900, 300), first+last,
                 text_lines=[TextLine(first, (100, 100, 900, 130)), TextLine(last, (100, 260, 900, 290))])
    recovered = Block("recovered", "text", (100, 180, 900, 210), middle)
    page = PageResult("p", 0, 1000, 1400, "unused", [body, recovered])
    assert continuous_text([page]) == first+middle+last


def test_plain_mode_rejects_the_same_weak_clipped_edge_noise_as_layout_mode(tmp_path):
    source = tmp_path / "page.png"
    Image.new("RGB", (2100, 3000), "white").save(source)
    job = ImageJob("p", str(source), text_only=True)
    prediction = {"rec_texts": ["昌", "完整正文", "7"], "rec_scores": [.44, .99, .99],
                  "rec_boxes": [[2056, 271, 2093, 332], [326, 330, 1774, 422], [2056, 2400, 2093, 2450]]}
    page = worker.page_from_plain_prediction(job, source, prediction)
    assert [block.text for block in page.blocks] == ["完整正文", "7"]
