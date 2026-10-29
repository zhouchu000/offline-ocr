from copy import deepcopy

from docx import Document
import pytest

from offline_word.domain import Block, ImageJob, PageResult, TextLine
from offline_word.export_checks import assert_text_coverage, inspect_page
from offline_word.exporter import export_docx
from offline_word.review import collect_review_items, confirm_review
from offline_word.security import OfflineError


def page():
    text = '这是一段用于检查补回文字是否重叠的公开测试文本。'
    return PageResult('p', 0, 1000, 1400, '', [
        Block('p:0', 'text', (40, 40, 900, 200), text,
              text_lines=[TextLine(text, (40, 40, 900, 70))]),
        Block('p:recovered-1', 'text', (42, 45, 900, 75), text,
              recognition_source='recovered', text_lines=[TextLine(text, (42,45,900,75))])])


def test_overlap_checks_real_lines_and_never_edits_content():
    value = page()
    original = deepcopy(value)
    assert {i.code for i in inspect_page(value)} == {'recovery_overlap', 'duplicate'}
    assert value == original
    value.blocks[1].bbox = (40, 90, 900, 120)
    value.blocks[1].text_lines[0].bbox = value.blocks[1].bbox
    # The enclosing paragraph box overlaps but its actual text line does not.
    assert inspect_page(value) == []


def test_reflow_does_not_report_positioned_text_overlap():
    assert 'recovery_overlap' not in {i.code for i in inspect_page(page(), layout_mode='paragraph')}


def test_boundaries_and_code_neighbors_only_warn():
    value = page()
    value.blocks = value.blocks[:1]
    value.blocks[0].bbox = (-5, 40, 1002, 200)
    value.nontext_regions = [[(40, 60), (200,60), (200,190), (40,190)]]
    assert {i.code for i in inspect_page(value)} == {'boundary', 'code_nearby'}


def test_character_coverage_keeps_punctuation_and_duplicate_counts():
    doc = Document()
    doc.add_paragraph('正文\n测试，123')
    assert_text_coverage(doc, '正文 测试，123')
    for expected in ['正文测试，1234', '正文测试,123', '正文正文测试，123']:
        with pytest.raises(OfflineError, match='export_text_coverage_failed'):
            assert_text_coverage(doc, expected)


def test_missing_export_text_stops_publish(tmp_path, monkeypatch):
    import offline_word.exporter as exporter
    value = page()
    value.blocks = value.blocks[:1]
    monkeypatch.setattr(exporter, '_append_text', lambda *args, **kwargs: None)
    output = tmp_path/'result.docx'
    with pytest.raises(OfflineError, match='export_text_coverage_failed'):
        export_docx([value], output, layout_mode='flow')
    assert not output.exists()


def test_review_risk_categories_and_confirmation_invalidate_on_edit_or_neighbor_change():
    value = page()
    job = ImageJob('p', '', result=value)
    items = collect_review_items([job], sort_by='risk', category='recovered')
    assert len(items) == 1 and items[0].risk >= 300
    assert confirm_review([job], 'p', 'p:recovered-1')
    assert not collect_review_items([job], category='recovered')
    value.blocks[0].text_lines[0].bbox = (40, 42, 900, 72)
    assert collect_review_items([job], category='recovered')
    confirm_review([job], 'p', 'p:recovered-1')
    value.blocks[1].edited_text = '人工校对新文字'
    assert collect_review_items([job], category='recovered')


def test_recovered_ids_remain_valid_project_domain(tmp_path):
    from offline_word.project_store import _validate_jobs
    job = ImageJob('p', '', result=page())
    _validate_jobs([job])
