"""Penpot workflows preserve edited data and use the actual Word export."""
from dataclasses import asdict
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from PySide6.QtCore import QAbstractAnimation, QPoint, QSettings, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QImage
from PySide6.QtWidgets import QLabel
from offline_word.domain import Block, Cell, ImageJob, PageResult, TextLine
from offline_word.gui import MainWindow
from offline_word import theme


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(theme,'QSettings',lambda *args:QSettings(str(tmp_path/'appearance.ini'),QSettings.Format.IniFormat))
    image=QImage(840,1200,QImage.Format.Format_RGB32)
    image.fill(QColor('white'))
    source=tmp_path/'公开文档.png'
    assert image.save(str(source))
    w=MainWindow(tmp_path/'models',tmp_path/'session')
    w._models_ready=True
    w._confirm_discard_results=lambda *a,**k:True
    w.set_jobs([ImageJob(f'p{i}',str(source)) for i in range(2)])
    for index,job in enumerate(w.jobs):
        blocks=[
            Block(f'title{index}','title',(80,70,760,140),'公开服务协议',text_lines=[TextLine('公开服务协议',(80,70,760,140))]),
            Block(f'body{index}','text',(80,200,760,400),'原始识别。\n第二行文字。',edited_text='已校对文字。\n第二行文字。',confidence=.84,candidates=['完整候选一。','完整候选二。'],text_lines=[TextLine('原始识别。',(80,200,760,240)),TextLine('第二行文字。',(80,280,760,320))]),
            Block(f'table{index}','table',(80,450,760,700),cells=[Cell(0,0,'合并表头',colspan=2),Cell(1,0,'项目甲'),Cell(1,1,'128.50')],rows=2,cols=2),
            Block(f'folio{index}','text',(400,1130,440,1160),str(index+1),text_lines=[TextLine(str(index+1),(400,1130,440,1160))]),
        ]
        w.apply_page_result(job.id,PageResult(job.id,index,840,1200,str(source),blocks))
    w.show()
    qtbot.wait(20)
    yield w
    w._worker=None
    qtbot.waitUntil(lambda: not w.export_controller.busy, timeout=10000)
    qtbot.waitUntil(lambda: w._model_task is None)
    w.close()


def normalized(text):
    return ''.join(text.split())


def preview_text(canvas):
    seen=set()
    text=[]
    for page in canvas._pages:
        for document in [page.document,*(overlay[2] for overlay in page.overlays)]:
            if id(document) not in seen:
                seen.add(id(document))
                text.append(document.toPlainText())
    return normalized(''.join(text))


@pytest.mark.parametrize('mode',['source','flow','paragraph','text_only'])
@pytest.mark.parametrize('remove_folios',[False,True])
def test_preview_contains_actual_export_text_and_does_not_modify_results(window,qtbot,mode,remove_folios):
    before=[asdict(job.result) for job in window.jobs]
    original=Path(window.jobs[0].source_path).read_bytes()
    window.export_layout_combo.setCurrentIndex(window.export_layout_combo.findData(mode))
    window.remove_page_numbers_checkbox.setChecked(remove_folios)
    window.show_export_preview()
    qtbot.waitUntil(lambda: not window.export_controller.busy, timeout=10000)
    qtbot.wait(40)
    assert window.views.currentWidget() is window.preview_view,window.status_label.text()
    assert window._preview_path.is_file()
    with ZipFile(window._preview_path) as archive:
        xml=ET.fromstring(archive.read('word/document.xml'))
    expected=normalized(''.join(node.text or '' for node in xml.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t')))
    assert preview_text(window.preview_canvas)==expected
    assert '已校对文字。' in expected and '原始识别。' not in expected
    assert [asdict(job.result) for job in window.jobs]==before
    assert Path(window.jobs[0].source_path).read_bytes()==original
    assert window.preview_page_list.count()==len(window.preview_canvas._pages)
    assert window.export_navigation_button.isChecked()
    thumbnail = window.preview_page_list.item(0).icon()
    assert thumbnail.pixmap(QSize(66,93), QIcon.Mode.Selected).toImage() == thumbnail.pixmap(QSize(66,93)).toImage()
    window.theme_button.click()
    assert preview_text(window.preview_canvas)==expected
    window.preview_canvas.zoom(1.2)
    window.preview_canvas.show_page(1)
    assert window.preview_page_list.currentRow()==window.preview_canvas._index


def test_theme_switch_keeps_edit_undo_and_original_pixels_and_remembers_only_appearance(window,qtbot):
    window.show_document()
    window.select_block('body0')
    window.proofread_panel.edit.setPlainText('人工输入后切换深色模式。')
    assert window.jobs[0].result.blocks[1].edited_text=='人工输入后切换深色模式。'
    original=window.processed_preview._image.copy()
    document=window.document_editor.document()
    window.theme_button.click()
    assert window.property('appearance')=='dark'
    assert window.document_editor.document() is document
    assert window.processed_preview._image==original
    assert window.proofread_panel.edit.toPlainText()=='人工输入后切换深色模式。'
    assert theme.saved_appearance()=='dark'
    settings=theme.QSettings()
    assert settings.allKeys()==['theme']
    window.document_editor.undo()
    assert window.jobs[0].result.blocks[1].text=='已校对文字。\n第二行文字。'
    window.theme_button.click()
    assert theme.saved_appearance()=='light'


def test_inline_candidate_adoption_and_table_cell_edit_reach_export(window,qtbot):
    window.show_document()
    window.select_block('body0')
    panel=window.proofread_panel
    assert panel.candidates.count()==2
    assert window.jobs[0].result.blocks[1].text=='已校对文字。\n第二行文字。'
    panel.candidates.setCurrentRow(1)
    panel.adopt_only_action.trigger()
    assert window.jobs[0].result.blocks[1].text=='完整候选二。'
    assert window.jobs[0].result.blocks[1].original_text=='原始识别。\n第二行文字。'
    # Risk-first navigation can move to another page after adopting a candidate.
    window.queue_list.setCurrentRow(0)
    window.select_block('table0')
    assert panel.tabs.currentIndex()==1
    panel.target_picker.setCurrentIndex(2)
    panel.edit.setPlainText('256.00')
    assert window.jobs[0].result.blocks[2].cells[2].text=='256.00'
    window.show_export_preview()
    qtbot.waitUntil(lambda: not window.export_controller.busy, timeout=10000)
    assert '完整候选二。' in preview_text(window.preview_canvas)
    assert '256.00' in preview_text(window.preview_canvas)


@pytest.mark.parametrize('mode',['light','dark'])
def test_choice_labels_are_visible_and_legible_and_buttons_change_settings(window,qtbot,mode):
    theme.set_appearance(window,mode,persist=False)
    window.show_export_options()
    qtbot.wait(30)
    for card in window.export_mode_cards:
        for label in card.findChildren(QLabel):
            assert label.isVisible() and label.height()>=label.fontMetrics().height()
            assert card.rect().contains(label.geometry()),label.text()
    window.export_mode_cards[3].click()
    assert window.export_layout_combo.currentData()=='text_only'
    assert window.export_mode_cards[3].isChecked()
    window.export_options.reject()


def test_separate_preview_uses_individual_files_and_cleans_previous_preview(window, qtbot):
    window.export_layout_combo.setCurrentIndex(3)
    window.show_export_preview()
    qtbot.waitUntil(lambda: not window.export_controller.busy, timeout=10000)
    previous=window._preview_path
    window.separate_output_radio.setChecked(True)
    window.show_export_preview()
    qtbot.waitUntil(lambda: not window.export_controller.busy, timeout=10000)
    assert len(window._preview_paths)==2
    assert len(window.preview_canvas._pages)==2
    assert not previous.exists()
    assert all(path.is_file() for path in window._preview_paths)
    assert '表格  0 个' in window.preview_stats.text()


def test_source_frames_and_table_are_previewed_on_their_original_pages(window, qtbot):
    for job in window.jobs:
        for block in job.result.blocks:
            block.edited_text=None
    window.show_export_preview()
    qtbot.waitUntil(lambda: not window.export_controller.busy, timeout=10000)
    assert len(window.preview_canvas._pages)==2
    assert all(paper.overlays for paper in window.preview_canvas._pages)
    for paper in window.preview_canvas._pages:
        assert all(0<=x<paper.size.width() and 0<=y<paper.size.height() for x,y,_doc in paper.overlays)
    with ZipFile(window._preview_path) as archive:
        xml=ET.fromstring(archive.read('word/document.xml'))
    expected=normalized(''.join(node.text or '' for node in xml.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t')))
    assert preview_text(window.preview_canvas)==expected


@pytest.mark.parametrize('size', [(1100,680),(1440,900)])
def test_long_chinese_target_does_not_push_proofreading_outside_window(window,qtbot,size):
    window.jobs[0].result.blocks[1].edited_text = '本协议由甲方（委托方）与乙方（服务方）本着平等互利的原则，经协商确认。'*4
    window.resize(*size)
    window.show_document()
    window.select_block('body0')
    qtbot.waitUntil(lambda: window.drawer.animation.state() == QAbstractAnimation.State.Stopped,
                    timeout=2000)
    panel=window.proofread_panel
    assert panel.content.width() <= window.proofread_scroll.viewport().width()
    # Reading content may scroll; confirmation and navigation never scroll away.
    panel.original_toggle.click()
    for widget in (panel.target_picker,panel.edit,panel.original,panel.candidates,panel.rerecognize):
        window.proofread_scroll.ensureWidgetVisible(widget,0,4)
        qtbot.wait(5)
        assert window.rect().contains(widget.mapTo(window,widget.rect().bottomRight()))
        assert window.rect().contains(widget.mapTo(window,QPoint()))
        for action in (panel.apply, panel.confirm, panel.next):
            assert action.isVisible()
            assert window.rect().contains(action.mapTo(window, action.rect().bottomRight()))
            assert panel.footer.rect().contains(action.mapTo(panel.footer, QPoint()))
    assert not panel.source_crop.sceneRect().isEmpty()
    assert panel.edit.horizontalScrollBar().maximum()==0


@pytest.mark.parametrize('size', [(1100,680),(1440,900)])
def test_preview_thumbnail_rail_keeps_one_column_at_supported_widths(window,qtbot,size):
    window.resize(*size)
    window.show_export_preview()
    qtbot.waitUntil(lambda: not window.export_controller.busy, timeout=10000)
    qtbot.wait(30)
    listing=window.preview_page_list
    first=listing.visualItemRect(listing.item(0))
    second=listing.visualItemRect(listing.item(1))
    assert first.left()==second.left() and second.top()>first.bottom()
    assert listing.horizontalScrollBar().maximum()==0


def test_settings_are_grouped_and_theme_changes_keep_unsaved_edits(window, qtbot):
    window.show_document()
    window.select_block('body0')
    window.proofread_panel.edit.setPlainText('未保存的人工校对。')
    window.appearance_button.click()
    qtbot.wait(20)
    assert window.appearance_dialog.isVisible()
    assert window.theme_selector.isVisible() and window.release_history_button.isVisible()
    window.theme_button.click()
    assert window.jobs[0].result.blocks[1].text == '未保存的人工校对。'
    window.appearance_dialog.accept()
    assert not window.theme_button.isVisible()
    assert window.proofread_panel.edit.toPlainText() == '未保存的人工校对。'


def test_source_crop_tracks_cells_pages_and_zoom_without_changing_geometry(window, qtbot):
    from dataclasses import asdict
    before = [asdict(job.result) for job in window.jobs]
    window.show_document()
    window.select_block('body0')
    crop = window.proofread_panel.source_crop
    first = crop._identity
    scale = crop.transform().m11()
    crop.zoom(1.3)
    assert crop.transform().m11() > scale
    window.proofread_panel.refresh()
    assert crop.transform().m11() > scale  # Polling must not reset user's zoom.
    window.select_block('table0')
    assert crop._identity != first and not crop.sceneRect().isEmpty()
    window.queue_list.setCurrentRow(1)
    window.select_block('body1')
    assert window.proofread_panel._key == 'body1'
    assert [asdict(job.result) for job in window.jobs] == before


def test_review_preview_follows_selection_and_clears_when_filter_is_empty(window, qtbot):
    window._render_review_summary()
    qtbot.wait(20)
    listing = window.review_list
    assert listing.horizontalScrollBar().maximum() == 0
    listing.setCurrentRow(1)
    selected = listing.currentItem().data(Qt.ItemDataRole.UserRole)
    item = window._visible_review_items[selected]
    assert item.block.text in window.review_detail_text.toPlainText()
    assert not window.review_source_crop.sceneRect().isEmpty()
    window._populate_review_list()
    assert listing.currentItem().data(Qt.ItemDataRole.UserRole) == selected
    window.review_filter_combo.setCurrentIndex(window.review_filter_combo.findData('recovered'))
    assert listing.count() == 0
    assert window.review_detail_text.toPlainText() == ''
    assert window.review_source_crop.sceneRect().isEmpty()
    assert not window.review_locate_button.isEnabled()


def test_diff_display_never_changes_candidate_value_or_interprets_ocr_markup(window):
    from offline_word.review_widgets import difference_html
    from PySide6.QtGui import QTextDocument
    before = '原文多余字😀<img src=x>'
    after = '原文😀<b>候选</b>'
    markup = difference_html(before, after, theme.DARK)
    assert '<img src=x>' not in markup and '<b>候选</b>' not in markup
    assert '<s>' in markup and 'font-weight:600' in markup
    doc = QTextDocument()
    doc.setHtml(markup)
    assert '<b>候选</b>' in doc.toPlainText()
    block = window.jobs[0].result.blocks[1]
    block.candidates = [after]
    window.show_document()
    window.select_block(block.id)
    panel = window.proofread_panel
    panel.adopt_only_action.trigger()
    assert block.text == after
    assert block.original_text == '原始识别。\n第二行文字。'


def test_typing_refreshes_candidate_comparison_and_no_candidates_keeps_confirm_visible(window, qtbot):
    window.show_document()
    window.select_block('body0')
    panel = window.proofread_panel
    panel.edit.setPlainText('修改中的内容。')
    assert panel.difference_delegate.original == '修改中的内容。'
    block = window.jobs[0].result.blocks[1]
    block.candidates = []
    panel.refresh()
    qtbot.wait(10)
    assert not panel.candidates.isVisible() and not panel.apply.isVisible()
    assert panel.no_candidates.isVisible() and panel.confirm.isVisible()
    panel.confirm.click()
    assert block.text == '修改中的内容。'
