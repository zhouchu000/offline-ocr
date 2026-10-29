from pathlib import Path

from PIL import Image
from PySide6.QtWidgets import QDialogButtonBox
import pytest

from offline_word.domain import Block, ImageJob, PageResult
from offline_word.gui import MainWindow
from offline_word.project_ui import PasswordDialog

PASSWORD = '公开测试项目口令12345'


@pytest.fixture
def window(qtbot, tmp_path):
    session = tmp_path/'session'
    session.mkdir()
    image = tmp_path/'公开样张.png'
    Image.new('RGB', (800, 1000), 'white').save(image)
    w = MainWindow(tmp_path/'models', session)
    w._confirm_discard_results = lambda *a, **k: True
    qtbot.addWidget(w)
    w.set_jobs([ImageJob('p', str(image))])
    w.apply_page_result('p', PageResult('p', 0, 800, 1000, str(image),
        [Block('p:recovered-0', 'text', (30,30,700,70), '原识别', edited_text='人工校对')]))
    return w


def wait(qtbot, controller):
    qtbot.waitUntil(lambda: not controller.busy, timeout=15000)


def test_password_confirmation_and_clear(window):
    dialog = PasswordDialog(window, saving=True)
    ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert not ok.isEnabled()
    dialog.password.setText(PASSWORD)
    assert not ok.isEnabled()
    dialog.confirmation.setText(PASSWORD)
    assert ok.isEnabled()
    assert dialog.take_password() == PASSWORD
    assert not dialog.password.text() and not dialog.confirmation.text()
    dialog.close()


def test_project_save_open_preserves_gui_current_on_failure_and_restores_settings(window, qtbot, tmp_path):
    controller = window.project_controller
    window.export_layout_combo.setCurrentIndex(window.export_layout_combo.findData('paragraph'))
    window.remove_page_numbers_checkbox.setChecked(True)
    target = tmp_path/'work.ocrproj'
    controller.start(target, PASSWORD, saving=True)
    wait(qtbot, controller)
    assert target.is_file() and not controller.dirty()
    window.jobs[0].result.blocks[0].edited_text = '尚未保存的新修改'
    assert controller.dirty()
    original = window.jobs[0]
    controller.start(target, '错误口令123456789', saving=False)
    wait(qtbot, controller)
    assert window.jobs[0] is original
    assert window.jobs[0].result.blocks[0].text == '尚未保存的新修改'
    window.export_layout_combo.setCurrentIndex(0)
    controller.start(target, PASSWORD, saving=False)
    wait(qtbot, controller)
    assert window.jobs[0].result.blocks[0].text == '人工校对'
    assert window.jobs[0].result.blocks[0].original_text == '原识别'
    assert window.jobs[0].display_name == '公开样张.png'
    assert '公开样张.png' in window.inspector_title.text()
    assert window.export_layout_combo.currentData() == 'paragraph'
    assert window.remove_page_numbers_checkbox.isChecked()
    assert Path(window.jobs[0].source_path).is_file()
    assert not controller.dirty()


def test_save_blocks_close_until_finished(window, qtbot, tmp_path):
    from PySide6.QtGui import QCloseEvent
    controller = window.project_controller
    controller.start(tmp_path/'work.ocrproj', PASSWORD, saving=True)
    window._continue_after_batch = True
    window._queued_during_run = {'p'}
    window.jobs[0].status = 'pending'
    window.start_processing()
    window._continue_added_jobs()
    assert window._worker is None and window._continue_after_batch
    window._continue_after_batch = False
    window.jobs[0].status = 'done'
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted()
    wait(qtbot, controller)
    assert controller.path is not None


def test_idle_cache_timer_does_not_change_active_batch_or_continue_queue(window):
    window._batch_job_ids = {'p', 'waiting'}
    window._queued_during_run = {'waiting'}
    window._continue_after_batch = True
    window._expire_idle_workers()
    assert window._batch_job_ids == {'p', 'waiting'}
    assert window._queued_during_run == {'waiting'}
    assert window._continue_after_batch
