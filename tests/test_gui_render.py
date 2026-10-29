"""Verify actual Qt geometry and save public, synthetic UI evidence."""
from pathlib import Path
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QFont
from PySide6.QtWidgets import QApplication, QMessageBox, QTextEdit
from offline_word.domain import Block, Cell, ImageJob, PageResult
from offline_word.gui import MainWindow


@pytest.fixture
def workspace(qtbot, tmp_path, monkeypatch):
    image = QImage(960,1280,QImage.Format.Format_RGB32)
    image.fill(QColor('#FFFFFF'))
    painter = QPainter(image)
    painter.setPen(QColor('#333333'))
    font = QFont('Microsoft YaHei UI'); font.setPixelSize(30)
    painter.setFont(font)
    painter.drawText(90,140,'文档整理与归档')
    font.setPixelSize(23); painter.setFont(font)
    for i in range(14):
        painter.drawText(90, 230+i*52, '公开测试材料，用于验证图像识别与文档编辑。')
    painter.end()
    source = tmp_path/'公开材料.png'
    image.save(str(source))
    window = MainWindow(tmp_path/'models', tmp_path/'session')
    window._models_ready = True
    window._set_model_badge('模型已就绪','success')
    window._set_status('可以添加图片。')
    window._confirm_discard_results = lambda *a, **k: True
    window.show()
    yield window, source
    window._worker = None
    window.close()


def in_window(window, widgets):
    for widget in widgets:
        assert widget.isVisible(), widget.accessibleName()
        assert window.rect().contains(widget.mapTo(window, QPoint()))
        assert window.rect().contains(widget.mapTo(window, widget.rect().bottomRight()))


def save(window, name):
    output = Path(__file__).resolve().parents[1]/'validation/ui-0310-verified'
    output.mkdir(parents=True, exist_ok=True)
    capture=window.grab()
    image=capture.toImage().scaled(window.size(),Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation)
    assert image.save(str(output/name))


def populate(window, source):
    window.set_jobs([ImageJob(f'page-{i}',str(source)) for i in range(9)])
    job = window.jobs[0]
    cells = [Cell(r,c,text) for r,row in enumerate([['项目','数量','状态'],['图像文档','9','已识别'],['校对记录','2','待确认']]) for c,text in enumerate(row)]
    result = PageResult(job.id,0,960,1280,str(source),[
        Block('title','title',(90,80,860,160),'文档整理与归档',confidence=.99),
        Block('body','text',(90,200,860,420),'将纸质材料整理为可以编辑的文档，让文字重新流动。扫描件和照片中的正文、标题与表格，都可以在这里集中校对。', confidence=.98),
        Block('review','text',(90,430,860,630),'先核对关键字，再检查数字与单位。识别存在差异时，可以展开原图对照，也可以查看原始识别文字与其他候选。', confidence=.84,candidates=['候选文字，需手动采用。']),
        Block('table','table',(90,650,860,870), rows=3,cols=3,cells=cells),
        Block('last','text',(90,900,860,1100),'校对完成后，选择适合的排版方式导出 Word。全部识别在本机完成。',confidence=.99),
    ])
    window.apply_page_result(job.id,result)
    return result


@pytest.mark.parametrize('size', [(1100,680),(1440,900),(1600,900)])
@pytest.mark.parametrize('theme', ['light','dark'])
def test_glass_views_fit_supported_sizes_in_both_themes(workspace, qtbot, size, theme):
    window, source = workspace
    from offline_word.theme import set_appearance
    set_appearance(window,theme,persist=False)
    window.resize(*size)
    qtbot.wait(40)
    save(window,f'empty-{theme}-{size[0]}.png')
    in_window(window,[window.import_button,window.import_more_button,window.empty_import_button,window.appearance_button])
    populate(window,source)
    qtbot.wait(40)
    assert window.queue_list.horizontalScrollBar().maximum() == 0
    in_window(window,[window.import_button,window.import_more_button,window.remove_button,window.start_button])
    save(window,f'gallery-{theme}-{size[0]}.png')
    window.document_button.click()
    qtbot.wait(40)
    assert len(window.findChildren(QTextEdit)) == 1
    assert window.queue_list.isVisible() and not window.source_preview.isVisible()
    assert not window.drawer.isVisible()
    window.details_button.click()
    qtbot.wait(220)
    in_window(window,[window.processed_preview,window.proofread_panel.edit,window.details_button,window.export_navigation_button,window.page_combo,window.review_summary_button])
    assert window.document_editor.horizontalScrollBar().maximum() == 0
    save(window,f'document-{theme}-{size[0]}.png')
    window.full_document_button.click()
    qtbot.wait(40)
    in_window(window,[window.document_editor,window.details_button,window.queue_list])
    assert window.document_editor.width() >= 280
    save(window,f'full-document-{theme}-{size[0]}.png')
    window.show_inspector()
    qtbot.wait(40)
    in_window(window,[window.source_preview,window.rotate_left_button,window.rotate_right_button,window.crop_button,window.perspective_button,window.reset_button])
    save(window,f'inspector-{theme}-{size[0]}.png')


def test_long_document_caret_scroll_and_selection_survive_progress(workspace, qtbot):
    window, source = workspace
    result = populate(window,source)
    result.blocks.extend([Block(f'long-{i}','text',(90,900,860,1100),'可编辑段落。'*35) for i in range(20)])
    window.show_document()
    window.show_full_document()
    editor = window.document_editor
    editor.select_target('long-12')
    qtbot.wait(40)
    before = editor.textCursor().position(), editor.verticalScrollBar().value(), editor.document()
    for _ in range(8):
        window._handle_worker_event({'event':'page_integrity_progress','job_id':'another-page'})
        window._sync_queue_items()
        QApplication.processEvents()
    after = editor.textCursor().position(), editor.verticalScrollBar().value(), editor.document()
    assert before == after


def test_on_demand_dialogs_fit_and_show_complete_candidates(workspace, qtbot):
    window, source = workspace
    populate(window,source)
    window.show_document()
    window.document_editor.select_target('review')
    window.show_text_details()
    qtbot.wait(30)
    window.candidate_picker.setCurrentIndex(1)
    in_window(window._details_dialog,[window.original_text_view,window.candidate_text_view,window.apply_candidate_button])
    save(window._details_dialog,'details.png')
    window._details_dialog.accept()
    window._render_review_summary()
    qtbot.wait(30)
    assert window.review_list.count() == 2
    save(window._review_dialog,'review-list.png')
    window._review_dialog.accept()
    window.show_export_options()
    qtbot.wait(30)
    in_window(window.export_options,[*window.export_mode_cards,window.remove_page_numbers_checkbox,window.export_confirm_button,window.export_preview_button])
    save(window.export_options,'export.png')
    window.export_options.accept()
