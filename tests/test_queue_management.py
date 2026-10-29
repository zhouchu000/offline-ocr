from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QMessageBox

from offline_word.gui import MainWindow
from offline_word.domain import Block, ImageJob, PageResult


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    value = MainWindow(tmp_path / 'models', tmp_path / 'session')
    paths = []
    for name in ('a', 'b', 'c'):
        path = tmp_path / f'{name}.png'
        Image.new('RGB', (200, 100), 'white').save(path)
        paths.append(path)
    value._import_paths([str(p) for p in paths])
    value.show()
    yield value
    value._worker = None
    monkeypatch.setattr(QMessageBox, 'warning', lambda *a, **kw: QMessageBox.StandardButton.Discard)
    value.close()


def test_append_keeps_selected_page_and_latest_edits(window, tmp_path):
    window.queue_list.setCurrentRow(1)
    job = window.current_job
    result = PageResult(job.id, 0, 200, 100, job.source_path, [Block('body', 'text', (0, 0, 180, 80), '原文')])
    window.apply_page_result(job.id, result)
    window.show_document()
    window.document_editor.replace_target('body', '已校对')
    new = tmp_path / 'new.png'
    Image.new('RGB', (200, 100), 'white').save(new)
    window._import_paths([str(new)])
    assert window.current_job is job
    assert result.blocks[0].text == '已校对'
    assert len(window.jobs) == 4


def test_multiple_selected_pages_are_removed_without_deleting_sources(window):
    assert window.queue_list.selectionMode() == QAbstractItemView.SelectionMode.ExtendedSelection
    paths = [Path(job.source_path) for job in window.jobs]
    window.queue_list.clearSelection()
    window.queue_list.item(0).setSelected(True)
    window.queue_list.item(2).setSelected(True)
    window.remove_button.click()
    assert [job.source_path for job in window.jobs] == [str(paths[1])]
    assert all(path.is_file() for path in paths)
    assert window.import_button.isEnabled() and window.remove_button.isEnabled()


def test_import_deduplicates_file_and_frame_without_resetting_results(window):
    first = window.jobs[0]
    window._import_paths([first.source_path])
    assert len(window.jobs) == 3
    assert window.jobs[0] is first


def test_delete_only_applies_when_queue_has_focus(window, qtbot):
    window.queue_list.setCurrentRow(1)
    window.queue_list.setFocus()
    qtbot.keyClick(window.queue_list, Qt.Key.Key_Delete)
    assert len(window.jobs) == 2


def test_file_dialog_open_error_cannot_leave_queue_locked(window, monkeypatch):
    dialog = window._dialog('选择图片', __import__('offline_word.file_browser', fromlist=['LocalFileMode']).LocalFileMode.EXISTING_FILES)
    monkeypatch.setattr(dialog, 'open', lambda: (_ for _ in ()).throw(RuntimeError('unavailable')))
    window._open_file_browser(dialog, window._import_paths)
    assert window._active_file_browser is None
    assert window.import_button.isEnabled() and window.remove_button.isEnabled()


def test_invalid_native_selection_and_duplicate_finish_do_not_lock_idle_list(window, monkeypatch, qtbot):
    from offline_word.file_browser import LocalFileMode
    dialog = window._dialog('选择图片', LocalFileMode.EXISTING_FILES)
    monkeypatch.setattr(dialog, 'open', lambda: None)
    monkeypatch.setattr(dialog, 'selectedFiles', lambda: (_ for _ in ()).throw(RuntimeError('unavailable')))
    window._open_file_browser(dialog, window._import_paths)
    dialog.finished.emit(1)
    dialog.finished.emit(1)
    qtbot.waitUntil(lambda: window._active_file_browser is None)
    assert len(window.jobs) == 3
    assert window.import_button.isEnabled() and window.remove_button.isEnabled()


class LiveWorker:
    busy = True

    def __init__(self):
        self.added = []
        self.removed = []

    def add_jobs(self, jobs):
        self.added.extend(jobs)
        return True

    def remove_jobs(self, ids):
        self.removed.extend(ids)
        return True

    def close(self):
        self.busy = False


def test_processing_allows_append_and_only_removes_confirmed_waiting_pages(window, tmp_path):
    live = LiveWorker()
    window._worker = live
    first, waiting, last = window.jobs
    window._batch_job_ids = {j.id for j in window.jobs}
    window._handle_worker_event({'event': 'page_started', 'job_id': first.id})
    assert window.import_button.isEnabled()
    window.queue_list.clearSelection()
    window.queue_list.item(0).setSelected(True)
    window.queue_list.item(1).setSelected(True)
    window.remove_button.click()
    assert live.removed == [waiting.id]
    assert len(window.jobs) == 3  # No premature removal while the child can be starting this page.
    window._handle_worker_event({'event': 'queue_removed', 'job_ids': [waiting.id]})
    assert [j.id for j in window.jobs] == [first.id, last.id]
    new = tmp_path / 'during.png'
    Image.new('RGB', (200, 100), 'white').save(new)
    window._import_paths([str(new)])
    assert len(live.added) == 1 and live.added[0] is window.jobs[-1]
    assert window.current_job is first


def test_removal_that_loses_start_race_keeps_page_and_result(window):
    live = LiveWorker()
    window._worker = live
    window._batch_job_ids = {j.id for j in window.jobs}
    current = window.jobs[0]
    window.remove_button.click()
    window._handle_worker_event({'event': 'page_started', 'job_id': current.id})
    window._handle_worker_event({'event': 'queue_remove_rejected', 'job_ids': [current.id]})
    assert current in window.jobs and not window._removal_requests
    assert not window.remove_button.isEnabled()


def test_added_page_survives_finished_event_that_was_already_in_pipe(window, tmp_path, monkeypatch):
    live = LiveWorker()
    window._worker = live
    for job in window.jobs:
        job.status = 'done'
    new = tmp_path / 'late.png'
    Image.new('RGB', (200, 100), 'white').save(new)
    window._import_paths([str(new)])
    starts = []
    monkeypatch.setattr(window, 'start_processing', lambda: starts.append('continue'))
    window._handle_worker_event({'event': 'finished'})
    assert starts == ['continue']
    assert window.jobs[-1].status == 'pending'


def test_finished_event_resolves_unacknowledged_removal_without_resurrecting_row(window):
    live = LiveWorker()
    window._worker = live
    window._batch_job_ids = {j.id for j in window.jobs}
    window.remove_button.click()
    window._handle_worker_event({'event': 'finished'})
    assert len(window.jobs) == 2 and window._worker is None
    assert not window._removal_requests
    assert window.import_button.isEnabled() and window.remove_button.isEnabled()
