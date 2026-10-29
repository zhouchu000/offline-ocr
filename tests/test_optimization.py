"""Regression coverage for responsive review, export and reversible filtering."""
from copy import deepcopy
import threading

import pytest
from PIL import Image
from PySide6.QtCore import QSettings, QTimer
from docx import Document

from offline_word import gui, review, theme
from offline_word.domain import Block, Cell, FilteredText, ImageJob, PageResult, TextLine
from offline_word.exporter import export_docx
from offline_word.nontext import restore_filtered_text
from offline_word.project_store import ProjectSnapshot, load_project, save_project
from offline_word.review import ReviewIndex
from offline_word.security import OfflineError
from offline_word.warm_worker import retention_seconds


def make_jobs(path, count=3):
    return [ImageJob(f'p{i}', str(path), status='done', result=PageResult(f'p{i}', i, 800, 1000,
        str(path), [Block(f'b{i}', 'text', (40,50,700,100), '公开测试文字', confidence=.8,
                         text_lines=[TextLine('公开测试文字', (40,50,700,100))], candidates=['公开修正文字'])])) for i in range(count)]


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    source = tmp_path / 'public.png'
    Image.new('RGB', (800,1000), 'white').save(source)
    monkeypatch.setattr(gui, 'validate_models', lambda root: {})
    monkeypatch.setattr(theme, 'QSettings', lambda *args: QSettings(str(tmp_path/'appearance.ini'), QSettings.Format.IniFormat))
    w = gui.MainWindow(tmp_path/'models', tmp_path/'session')
    w._confirm_discard_results = lambda *args, **kw: True
    w.set_jobs(make_jobs(source))
    w.show()
    qtbot.waitUntil(lambda: w._model_task is None)
    yield w
    if w.export_controller.busy:
        w.export_controller.cancel()
        qtbot.waitUntil(lambda: not w.export_controller.busy, timeout=10000)
    w.close()


def test_review_cache_rechecks_only_changed_page_and_keeps_reorder_correct(monkeypatch):
    jobs = make_jobs('public.png', 100)
    index = ReviewIndex()
    original = review.collect_review_items
    examined = []
    def collect(values, **options):
        examined.extend(job.id for job in values)
        return original(values, **options)
    monkeypatch.setattr(review, 'collect_review_items', collect)
    assert len(index.collect(jobs)) == 100
    examined.clear()
    jobs[10].result.blocks[0].edited_text = '用户修改'
    index.invalidate(jobs[10].id)
    index.collect(jobs)
    assert examined == ['p10']
    examined.clear()
    items = index.collect(list(reversed(jobs)))
    assert items[0].job_id == 'p99' and items[0].page_number == 1
    assert examined == []
    jobs[0].result = deepcopy(jobs[0].result)
    index.collect(jobs)
    assert examined == ['p0']


def test_edit_reopens_confirmation_and_updates_all_three_counts(window, qtbot):
    window.show_document()
    window.select_block('b0')
    window.proofread_panel.confirm_and_next()
    assert len(window.review_items()) == 2
    window._locate_review_item('p0', 'b0')
    window.proofread_panel.edit.insertPlainText('修改')
    qtbot.waitUntil(lambda: window.review_summary_button.text() == '待复核 3')
    assert '3' in window.proofread_panel.progress.text()
    from PySide6.QtCore import Qt
    assert window.queue_list.item(0).data(Qt.ItemDataRole.UserRole+2) == 1
    window.document_editor.undo()
    qtbot.waitUntil(lambda: window.review_summary_button.text() == '待复核 2')


def test_candidate_adoption_is_separate_from_explicit_confirmation(window, qtbot):
    window.show_document()
    window.select_block('b0')
    panel = window.proofread_panel
    panel._apply()
    assert window.document_editor.current_target.text == '公开修正文字'
    assert window._selected_block_id == 'b0'
    assert len(window.review_items()) == 3
    panel._apply(confirm=True)
    assert len(window.review_items()) == 2
    assert window._current_job_id != 'p0'


def test_adopt_and_confirm_never_confirms_table_or_recovered_position(window):
    page = window.jobs[0].result
    page.blocks[0].recognition_source = 'recovered'
    window.review_changed('p0')
    window.show_document()
    window.select_block('b0')
    assert not window.proofread_panel.adopt_confirm_action.isEnabled()
    window.proofread_panel._apply(confirm=True)
    assert len(window.review_items()) == 3


def test_opening_candidate_menu_does_not_apply_anything(window, qtbot):
    window.show_document()
    window.select_block('b0')
    panel = window.proofread_panel
    panel.apply_menu.popup(panel.apply.mapToGlobal(panel.apply.rect().bottomLeft()))
    qtbot.waitUntil(panel.apply_menu.isVisible)
    assert window.document_editor.current_target.text == '公开测试文字'
    panel.adopt_only_action.trigger()
    panel.apply_menu.hide()
    assert window.document_editor.current_target.text == '公开修正文字'
    assert len(window.review_items()) == 3


def test_page_search_survives_background_queue_refresh(window):
    window.page_combo.setFocus()
    window.page_combo.lineEdit().setText('搜索中的文件')
    window._sync_page_combo()
    assert window.page_combo.currentText() == '搜索中的文件'


def test_export_runs_off_gui_and_marks_snapshot_not_later_edit(window, qtbot, tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = export_docx
    def slow(pages, target, **options):
        entered.set()
        assert release.wait(5)
        return original(pages, target, **options)
    monkeypatch.setattr(gui, 'export_docx', slow)
    window.export_controller.start([window.jobs[0].result], tmp_path/'out.docx')
    try:
        qtbot.waitUntil(entered.is_set)
        ticks = []
        QTimer.singleShot(0, lambda: ticks.append(True))
        qtbot.waitUntil(lambda: bool(ticks))
        window.jobs[0].result.blocks[0].edited_text = '保存期间的新修改'
    finally:
        release.set()
    qtbot.waitUntil(lambda: not window.export_controller.busy)
    assert '公开测试文字' in ''.join(p.text for p in Document(tmp_path/'out.docx').paragraphs)
    assert window._exported_signatures['p0'] != window._page_signature(window.jobs[0].result)


def test_cancel_export_keeps_existing_file_and_publishes_no_partial(tmp_path):
    jobs = make_jobs('unused.png')
    destination = tmp_path/'out.docx'
    destination.write_bytes(b'existing file')
    cancel = [False]
    def check():
        if cancel[0]:
            raise OfflineError('export_cancelled')
    with pytest.raises(OfflineError, match='export_cancelled'):
        export_docx([job.result for job in jobs], destination,
                    progress=lambda *_: cancel.__setitem__(0, True), check_cancel=check)
    assert destination.read_bytes() == b'existing file'
    assert list(tmp_path.iterdir()) == [destination]


def test_separate_export_cancel_retains_and_marks_only_completed_pages(window, qtbot, tmp_path, monkeypatch):
    calls = []
    def exporter(pages, target, **options):
        calls.append(target)
        if len(calls) == 2:
            raise OfflineError('export_cancelled')
        return export_docx(pages, target, **options)
    monkeypatch.setattr(gui, 'export_docx', exporter)
    window.export_controller.start([job.result for job in window.jobs], tmp_path, separate=True)
    qtbot.waitUntil(lambda: not window.export_controller.busy)
    assert len(list(tmp_path.glob('*.docx'))) == 1
    assert set(window._exported_signatures) == {'p0'}


def test_cancelling_preview_construction_clears_partial_scene_and_private_docx(window, qtbot, monkeypatch):
    from offline_word import export_preview
    original = export_preview.iter_docx_pages
    def slow_pages(package):
        yield next(page for page in original(package) if page is not None)
        while True:
            yield None
    monkeypatch.setattr(export_preview, 'iter_docx_pages', slow_pages)
    window.show_export_preview()
    qtbot.waitUntil(lambda: window.export_controller._steps is not None)
    qtbot.waitUntil(lambda: bool(window.preview_canvas._pages))
    assert window.views.currentWidget() is window.preview_view
    paths = list(window.export_controller.task.paths)
    window.export_controller.cancel()
    qtbot.waitUntil(lambda: not window.export_controller.busy)
    assert all(not path.exists() for path in paths)
    assert not window.preview_canvas._pages
    assert not window._exported_signatures


def test_restore_filtered_text_is_explicit_idempotent_and_preserves_edits(tmp_path):
    page = make_jobs('public.png', 1)[0].result
    page.blocks[0].edited_text = '已经手工修改'
    page.filtered_text = [FilteredText('f0', '可能被误滤的文字', (40,140,700,180), 'b0')]
    assert restore_filtered_text(page, 'f0')
    assert not restore_filtered_text(page, 'f0')
    assert page.blocks[0].text == '已经手工修改'
    output = export_docx([page], tmp_path/'restored.docx', layout_mode='text_only')
    value = ''.join(p.text for p in Document(output).paragraphs)
    assert value.count('可能被误滤的文字') == 1
    assert '已经手工修改' in value


def test_restoration_cannot_replace_corrected_table_cell():
    page = make_jobs('public.png', 1)[0].result
    page.blocks = [Block('table', 'table', (0,0,100,100), cells=[Cell(0,0,'',edited_text='手工修正')], rows=1,cols=1)]
    page.filtered_text = [FilteredText('f0', '原始文字', (0,0,100,100), 'table', 0, 0)]
    assert not restore_filtered_text(page, 'f0')
    assert page.blocks[0].cells[0].text == '手工修正'


def test_encrypted_project_retains_filter_evidence_and_restore_state(tmp_path):
    source = tmp_path/'public.png'
    Image.new('RGB',(800,1000),'white').save(source)
    jobs = make_jobs(source, 1)
    jobs[0].result.filtered_text = [FilteredText('f0','原始过滤内容',(10,10,80,30),'b0')]
    restore_filtered_text(jobs[0].result, 'f0')
    path = tmp_path/'saved.ocrproject'
    save_project(ProjectSnapshot(jobs, {}), path, 'public-test-password')
    session = tmp_path/'session'
    session.mkdir()
    loaded = load_project(path, 'public-test-password', session)
    assert loaded.jobs[0].result.filtered_text == jobs[0].result.filtered_text
    assert loaded.jobs[0].result.filtered_text[0].restored


@pytest.mark.parametrize('free,rss,expected', [(None,1,0),(1,1,0),(1.8,2,60),(2.2,3,120),(3.2,2,180),(4,6,60),(4,0,60)])
def test_reuse_timeout_respects_memory_and_hard_ceiling(free,rss,expected):
    assert retention_seconds(free,rss) == expected


def test_model_validation_does_not_block_first_frame(qtbot, tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def validate(root):
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr(gui, 'validate_models', validate)
    w = gui.MainWindow(tmp_path/'models', tmp_path/'session')
    w._confirm_discard_results = lambda *a, **k: True
    try:
        w.show()
        qtbot.waitUntil(entered.is_set)
        assert w.isVisible() and not w.start_button.isEnabled()
        ticks = []
        QTimer.singleShot(0, lambda: ticks.append(True))
        qtbot.waitUntil(lambda: bool(ticks))
    finally:
        release.set()
        qtbot.waitUntil(lambda: w._model_task is None)
        w.close()
