"""Real Qt interactions and resource lifetime for the native glass workspace."""
from dataclasses import asdict
import hashlib
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QAbstractAnimation, QPoint, QSettings
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QWidget
from offline_word import theme
from offline_word.domain import Block, ImageJob, PageResult
from offline_word.fluent import DesktopEffects, MaterialSurface, resize_hit
from offline_word.gui import MainWindow


@pytest.fixture
def window(qtbot,tmp_path,monkeypatch):
    monkeypatch.setattr(theme,'QSettings',lambda *a:QSettings(str(tmp_path/'appearance.ini'),QSettings.Format.IniFormat))
    image=QImage(840,1188,QImage.Format.Format_RGB32)
    image.fill(QColor('white'))
    source=tmp_path/'sample.png'
    assert image.save(str(source))
    w=MainWindow(tmp_path/'models',tmp_path/'session')
    w._confirm_discard_results=lambda *a,**k:True
    w.set_jobs([ImageJob('page',str(source))])
    w.apply_page_result('page',PageResult('page',0,840,1188,str(source),[
        Block('body','text',(60,240,760,420),'原始识别文字。',candidates=['正确候选文字。'])]))
    w._effects=DesktopEffects(False,True,True)
    w.show()
    qtbot.addWidget(w)
    yield w
    w._worker=None
    w.close()


@pytest.mark.parametrize('point,expected', [((0,0),13),((500,0),12),((999,0),14),
    ((0,400),10),((999,400),11),((0,799),16),((500,799),15),((999,799),17),((500,400),0)])
def test_resize_hit_all_edges_and_interior(point,expected):
    assert resize_hit(*point,1000,800,8)==expected


def test_effect_preferences_only_store_appearance_and_preserve_edited_data(window,qtbot):
    window.show_document()
    window.select_block('body')
    window.proofread_panel.edit.setPlainText('人工校对文字。')
    page=asdict(window.current_job.result)
    image=hashlib.sha256(open(window.current_job.source_path,'rb').read()).hexdigest()
    document=window.document_editor.document()
    theme.set_effect(window,'motion',False)
    theme.set_effect(window,'glass',False)
    theme.set_appearance(window,'dark')
    assert asdict(window.current_job.result)==page
    assert window.document_editor.document() is document
    assert image==hashlib.sha256(open(window.current_job.source_path,'rb').read()).hexdigest()
    assert theme.saved_effect('motion') is False and theme.saved_effect('glass') is False
    assert set(theme.QSettings().allKeys())=={'glass','motion','theme'}
    assert window.drawer._backdrop.isNull()
    window.document_editor.undo()
    assert window.current_job.result.blocks[0].text=='原始识别文字。'


def test_completed_transitions_and_closed_drawer_release_pixel_buffers(window,qtbot):
    window.show_document()
    qtbot.waitUntil(lambda:window.motion.layer._image.isNull())
    assert window.motion.animation.state()==QAbstractAnimation.State.Stopped
    window.select_block('body')
    assert window.drawer.isVisible() and not window.drawer._backdrop.isNull()
    window.proofread_panel.candidates.setCurrentRow(0)
    window.proofread_panel.adopt_only_action.trigger()
    assert window.current_job.result.blocks[0].text=='正确候选文字。'
    window.close_context_panel()
    assert window.drawer._backdrop.isNull()
    assert window.drawer.animation.state()==QAbstractAnimation.State.Stopped


def test_busy_and_reduced_motion_have_no_transition_frames(window,qtbot):
    window.motion.capture()
    window.motion.reveal()
    window._worker=SimpleNamespace()
    window._refresh_action_states()
    assert window.motion.layer._image.isNull()
    window.motion.capture()
    window.motion.reveal()
    assert window.motion.layer._image.isNull()
    window._worker=None
    window._effects=DesktopEffects(False,False,False)
    window.motion.capture()
    window.show_document()
    window.select_block('body')
    assert window.motion.animation.state()==QAbstractAnimation.State.Stopped
    assert window.drawer.animation.state()==QAbstractAnimation.State.Stopped
    assert window.drawer._backdrop.isNull()


def test_escape_closes_context_without_cancelling_worker(window,qtbot,monkeypatch):
    cancelled=[]
    monkeypatch.setattr(window,'cancel_processing',lambda:cancelled.append(True))
    window.show_document()
    window.select_block('body')
    window.dismiss_or_cancel()
    assert not window.drawer.isVisible() and cancelled==[]
    window.dismiss_or_cancel()
    assert cancelled==[True]


def test_repeated_resize_reanchors_panel_inside_stage_and_preserves_editor(window,qtbot):
    window.show_document()
    window.select_block('body')
    document=window.document_editor.document()
    theme.set_effect(window,'motion',False,persist=False)
    for width,height in [(1100,680),(1440,900),(1100,760)]:
        window.resize(width,height)
        qtbot.wait(20)
        assert window.views.rect().contains(window.drawer.geometry())
        assert window.proofread_scroll.horizontalScrollBar().maximum()==0
        assert window.document_editor.document() is document


def test_width_fit_and_theme_change_preserve_source_coordinate_roundtrip(window,qtbot):
    window.show_document()
    view=window.processed_preview
    view.fit_width()
    qtbot.wait(20)  # Settle the scrollbar/layout before comparing screen points.
    point=QPoint(260,330)
    before=view.image_to_display(point)
    theme.set_appearance(window,'dark',persist=False)
    after=view.display_to_image(before)
    assert abs(after.x()-point.x())<3 and abs(after.y()-point.y())<3
    assert view.horizontalScrollBar().maximum()==0


def test_glass_repaint_replaces_opaque_pixels_instead_of_accumulating_tint(qtbot):
    surface=MaterialSurface()
    qtbot.addWidget(surface)
    surface.resize(80,80)
    surface.setProperty('nativeGlass',True)
    image=QImage(80,80,QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor('white'))  # An earlier opaque appearance already filled it.
    painter=QPainter(image)
    surface.render(painter,QPoint(),renderFlags=QWidget.RenderFlag.DrawChildren)
    painter.end()
    assert image.pixelColor(40,40).alpha()==150


def test_transition_preserves_high_density_text_pixels(window,monkeypatch):
    snapshot=QPixmap(window.views.size()*2)
    snapshot.setDevicePixelRatio(2)
    snapshot.fill(QColor('white'))
    monkeypatch.setattr(window.views,'grab',lambda:snapshot)
    window.motion.capture()
    assert window.motion.layer._image.devicePixelRatio()==2
    assert window.motion.layer._image.size()==snapshot.size()
    window.motion.stop()


def test_folder_import_is_accessible_from_compact_add_menu(window,qtbot):
    window.import_more_button.click()
    qtbot.waitUntil(window.import_menu.isVisible)
    assert window.import_folder_button.isVisible()
    window.import_folder_button.click()
    qtbot.waitUntil(lambda:window._active_file_browser is not None)
    assert not window.import_menu.isVisible()
    window._active_file_browser.reject()
