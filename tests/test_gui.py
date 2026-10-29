from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PIL import Image
from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QColor, QCloseEvent, QImage
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QMessageBox,
    QWidget,
)

from offline_word.domain import Block, Cell, ImageJob, PageResult
from offline_word import gui
from offline_word import worker
from offline_word.file_browser import LocalFileBrowser, LocalFileMode, LocalFileResult
from offline_word.gui import MainWindow


class DialogProbe(QWidget):
    """Minimal browser double for asynchronous lifecycle tests."""

    finished = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._selected_files: list[str] = []

    def open(self) -> None:
        self.show()

    def done(self, result: LocalFileResult) -> None:
        self.finished.emit(int(result))

    def selectFile(self, value: str) -> None:  # noqa: N802 - dialog-compatible test API
        self._selected_files = [value]

    def selectedFiles(self) -> list[str]:  # noqa: N802 - dialog-compatible test API
        return list(self._selected_files)

    def setNameFilter(self, _value: str) -> None:  # noqa: N802 - dialog-compatible test API
        return None

    def setDefaultSuffix(self, _value: str) -> None:  # noqa: N802 - dialog-compatible test API
        return None


@pytest.fixture(autouse=True)
def discard_unexported_windows_after_each_test(qtbot, monkeypatch):
    """Drive the real close path after assertions so Qt teardown cannot block on a modal dialog."""
    # The embedded selector remains the macOS path. Windows is covered separately.
    monkeypatch.setattr(gui, "_use_native_file_dialogs", lambda: False)
    add_widget = qtbot.addWidget

    def add_nonblocking_widget(widget, *args, **kwargs):
        if isinstance(widget, MainWindow):
            return None
        return add_widget(widget, *args, **kwargs)

    monkeypatch.setattr(qtbot, "addWidget", add_nonblocking_widget)
    yield
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args, **_kwargs: QMessageBox.StandardButton.Discard)
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, MainWindow):
            widget.close()


def _edit(window, key, text):
    window.show_document()
    assert window.document_editor.replace_target(key, text)


def _job(job_id: str, path: Path) -> ImageJob:
    return ImageJob(id=job_id, source_path=str(path))


def _page(job_id: str, image_path: Path, text: str = "原始文字") -> PageResult:
    return PageResult(
        id=job_id,
        page_index=0,
        width=200,
        height=100,
        processed_image_path=str(image_path),
        blocks=[Block(id=f"{job_id}-block", kind="text", bbox=(10, 10, 120, 45), original_text=text)],
    )


def _image(path: Path) -> Path:
    image = QImage(200, 100, QImage.Format.Format_RGB32)
    image.fill(QColor("#f6f4ef"))
    assert image.save(str(path))
    return path


def test_imported_pages_keep_model_missing_state_explicit(qtbot, tmp_path: Path):
    """Would fail if importing a page implies OCR can run without verified models."""
    source = _image(tmp_path / "source.png")
    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)
    window.set_jobs([_job("a", source)])
    window.show()

    assert not window.start_button.isEnabled()
    assert "模型不可用" in window.status_label.text()


def test_file_dialogs_use_chinese_app_browser_without_platform_file_panel(qtbot, tmp_path: Path):
    """Would fail if the app returned to a platform or Qt file chooser."""
    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)

    import_dialog = window._dialog("导入图片", LocalFileMode.EXISTING_FILES)
    folder_dialog = window._dialog("导入图片文件夹", LocalFileMode.DIRECTORY)
    export_dialog = window._dialog("导出合并 DOCX", LocalFileMode.SAVE_FILE, accept_label="保存")

    for dialog, accept in (
        (import_dialog, "打开"),
        (folder_dialog, "选择"),
        (export_dialog, "保存"),
    ):
        assert isinstance(dialog, LocalFileBrowser)
        assert dialog.location_edit.accessibleName() == "当前文件夹"
        assert dialog.accept_button.text() == accept
        assert dialog.cancel_button.text() == "取消"
        assert dialog.file_tree.headerItem().text(0) == "名称"
        assert dialog.file_tree.headerItem().text(1) == "类型"
        dialog.deleteLater()


def test_main_window_uses_app_file_browser_instead_of_qfiledialog(qtbot, tmp_path: Path):
    """Would fail if production returned to the crashing platform chooser path."""
    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)

    dialog = window._dialog("导入图片", LocalFileMode.EXISTING_FILES)

    assert isinstance(dialog, LocalFileBrowser)
    assert not isinstance(dialog, QDialog)
    assert dialog.accept_button.text() == "打开"
    assert dialog.cancel_button.text() == "取消"
    dialog.deleteLater()


def test_real_file_browser_is_embedded_and_imports_selected_image(qtbot, tmp_path: Path):
    """Exercise the production browser without creating or closing a native window."""
    source = _image(tmp_path / "公开样张.png")
    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)
    window.show()

    window.import_files()
    browser = window._active_file_browser
    assert isinstance(browser, LocalFileBrowser)
    assert window.central_stack.currentWidget() is browser
    assert browser.setDirectory(tmp_path)
    item = next(
        browser.file_tree.topLevelItem(index)
        for index in range(browser.file_tree.topLevelItemCount())
        if browser.file_tree.topLevelItem(index).text(0) == source.name
    )
    item.setSelected(True)
    browser._accept_selection()
    qtbot.waitUntil(lambda: window._active_file_browser is None)

    assert [Path(job.source_path) for job in window.jobs] == [source]
    assert window.central_stack.currentWidget() is window.workspace_root


def test_embedded_file_browser_reopens_repeatedly_without_a_stale_reference(qtbot, tmp_path: Path):
    """Reuse one browser so macOS accessibility objects are not destroyed mid-session."""
    source = _image(tmp_path / "重复导入.png")
    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)
    window.show()

    first_browser = None
    for _index in range(10):
        window.import_files()
        browser = window._active_file_browser
        assert isinstance(browser, LocalFileBrowser)
        assert not browser.isWindow()
        if first_browser is None:
            first_browser = browser
        else:
            assert browser is first_browser
        assert browser.setDirectory(tmp_path)
        item = next(
            browser.file_tree.topLevelItem(index)
            for index in range(browser.file_tree.topLevelItemCount())
            if browser.file_tree.topLevelItem(index).text(0) == source.name
        )
        item.setSelected(True)
        browser._accept_selection()
        qtbot.waitUntil(lambda: window._active_file_browser is None)
        assert window.central_stack.currentWidget() is window.workspace_root
        assert window.central_stack.indexOf(browser) >= 0

    assert len(window.jobs) == 1
    assert all(Path(job.source_path) == source for job in window.jobs)


def test_file_import_dialog_is_async_kept_alive_and_imports_only_after_accept(qtbot, monkeypatch, tmp_path: Path):
    """Would fail if image import enters a nested loop or loses the live browser."""
    source = _image(tmp_path / "source.png")
    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)

    dialog = DialogProbe(window)
    monkeypatch.setattr(window, "_dialog", lambda *_args, **_kwargs: dialog)

    window.import_files()

    assert window._active_file_browser is dialog
    assert window.central_stack.currentWidget() is dialog
    assert not window.import_button.isEnabled()
    assert not window.import_folder_button.isEnabled()
    window.import_folder()
    assert window._active_file_browser is dialog
    dialog.selectFile(str(source))
    dialog.done(LocalFileResult.ACCEPTED)

    # File parsing and dialog disposal happen on the next event-loop turn,
    # outside the browser's finished callback.
    assert window._active_file_browser is dialog
    assert window.jobs == []
    qtbot.waitUntil(lambda: window._active_file_browser is None)

    assert [Path(job.source_path) for job in window.jobs] == [source]
    assert window.import_button.isEnabled()
    assert window.import_folder_button.isEnabled()


def test_folder_import_dialog_rejection_does_not_import_or_keep_a_reference(qtbot, monkeypatch, tmp_path: Path):
    """Would fail if cancelling the asynchronous folder chooser changes the queued images."""
    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)

    dialog = DialogProbe(window)
    monkeypatch.setattr(window, "_dialog", lambda *_args, **_kwargs: dialog)

    window.import_folder()

    assert window._active_file_browser is dialog
    dialog.done(LocalFileResult.REJECTED)
    qtbot.waitUntil(lambda: window._active_file_browser is None)

    assert window.jobs == []
    assert window.import_button.isEnabled()
    assert window.import_folder_button.isEnabled()


@pytest.mark.parametrize('layout_mode', ['source', 'flow', 'paragraph', 'text_only'])
def test_merge_export_dialog_is_async_and_preserves_exported_state(qtbot, monkeypatch, tmp_path: Path, layout_mode):
    """Would fail if merge export blocks in exec() or marks a page before a selected save path exports."""
    source = _image(tmp_path / "source.png")
    destination = tmp_path / "合并结果.docx"
    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    window.export_layout_combo.setCurrentIndex(window.export_layout_combo.findData(layout_mode))
    qtbot.addWidget(window)
    window.set_jobs([_job("a", source)])
    window.apply_page_result("a", _page("a", source))

    dialog = DialogProbe(window)
    exported: list[tuple[list[str], Path, str]] = []
    monkeypatch.setattr(window, "_dialog", lambda *_args, **_kwargs: dialog)
    monkeypatch.setattr(
        gui,
        "export_docx",
        lambda pages, path, **kwargs: exported.append(([page.id for page in pages], Path(path), kwargs['layout_mode'])) or Path(path),
    )

    window._export([window.jobs[0].result], separate=False)

    assert window._active_file_browser is dialog
    assert not window.import_button.isEnabled()
    assert not window.export_separate_button.isEnabled()
    dialog.selectFile(str(destination))
    dialog.done(LocalFileResult.ACCEPTED)
    qtbot.waitUntil(lambda: window._active_file_browser is None)

    assert exported == [(["a"], destination, layout_mode)]
    assert not window._has_unexported_results()


def test_separate_export_dialog_is_async_and_uses_selected_folder(qtbot, monkeypatch, tmp_path: Path):
    """Would fail if separate export blocks in exec() or ignores its accepted directory."""
    source = _image(tmp_path / "source.png")
    destination = tmp_path / "分别导出"
    destination.mkdir()
    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)
    window.set_jobs([_job("a", source)])
    window.apply_page_result("a", _page("a", source))

    dialog = DialogProbe(window)
    exported: list[Path] = []
    monkeypatch.setattr(window, "_dialog", lambda *_args, **_kwargs: dialog)
    monkeypatch.setattr(gui, "export_docx", lambda _pages, path, **kwargs: exported.append(Path(path)) or Path(path))

    window._export([window.jobs[0].result], separate=True)

    assert window._active_file_browser is dialog
    dialog.selectFile(str(destination))
    dialog.done(LocalFileResult.ACCEPTED)
    qtbot.waitUntil(lambda: window._active_file_browser is None)

    assert exported == [destination / "识别结果_001.docx"]
    assert not window._has_unexported_results()


@pytest.mark.parametrize("separate", [False, True])
@pytest.mark.parametrize("layout_mode", ["source", "flow", "paragraph", "text_only"])
def test_page_number_toggle_uses_full_batch_and_keeps_original_results(qtbot, monkeypatch, tmp_path, separate, layout_mode):
    source = _image(tmp_path / "source.png")
    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)
    assert not window.remove_page_numbers_checkbox.isChecked()
    window.set_jobs([_job("a", source), _job("b", source)])
    for index, job in enumerate(window.jobs):
        result = PageResult(job.id, index, 1000, 1400, str(source), [
            Block("body", "text", (100, 200, 900, 900), "正文 2026 年共 12 项"),
            Block("number", "text", (480, 1320, 520, 1340), f"{index+1}/12")])
        window.apply_page_result(job.id, result)
    window.export_layout_combo.setCurrentIndex(window.export_layout_combo.findData(layout_mode))
    window.remove_page_numbers_checkbox.setChecked(True)
    dialog = DialogProbe(window)
    monkeypatch.setattr(window, "_dialog", lambda *_args, **_kwargs: dialog)
    exported = []
    monkeypatch.setattr(gui, "export_docx", lambda pages, path, **kwargs: exported.extend(pages) or Path(path))
    window.request_export(separate=separate)
    assert not window.remove_page_numbers_checkbox.isEnabled()
    dialog.selectFile(str(tmp_path if separate else tmp_path / "result.docx"))
    dialog.done(LocalFileResult.ACCEPTED)
    qtbot.waitUntil(lambda: window._active_file_browser is None)
    assert len(exported) == 2 and all(len(page.blocks) == 1 for page in exported)
    assert all(len(job.result.blocks) == 2 for job in window.jobs)
    assert not window._has_unexported_results()
    assert "已去除 2 处页码" in window.status_label.text()
    assert window.remove_page_numbers_checkbox.isEnabled()
    window.remove_page_numbers_checkbox.setChecked(False)
    exported.clear()
    window.request_export(separate=separate)
    dialog.done(LocalFileResult.ACCEPTED)
    qtbot.waitUntil(lambda: window._active_file_browser is None)
    assert len(exported) == 2 and all(len(page.blocks) == 2 for page in exported)


def test_text_edits_are_preserved_when_navigating_pages(qtbot, tmp_path: Path):
    """Would fail if page selection rebuilds editors from original OCR text."""
    source_a = _image(tmp_path / "a.png")
    source_b = _image(tmp_path / "b.png")
    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)
    window.set_jobs([_job("a", source_a), _job("b", source_b)])
    window.apply_page_result("a", _page("a", source_a))
    window.apply_page_result("b", _page("b", source_b, "第二页"))
    window.show()

    _edit(window, "a-block", "已修改内容")
    window.queue_list.setCurrentRow(1)
    window.queue_list.setCurrentRow(0)

    assert window.jobs[0].result.blocks[0].edited_text == "已修改内容"
    assert window.document_editor.toPlainText() == "已修改内容"


def test_crop_cancel_keeps_existing_result_and_confirm_discards_it(qtbot, monkeypatch, tmp_path: Path):
    """Would fail if preprocessing silently destroys reviewed OCR edits."""
    source = _image(tmp_path / "source.png")
    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)
    window.set_jobs([_job("a", source)])
    window.apply_page_result("a", _page("a", source))
    window.show()
    _edit(window, "a-block", "已复核文字")
    window.crop_button.setChecked(True)
    window.source_preview.set_interaction_mode("crop")

    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: QMessageBox.StandardButton.Cancel)
    assert not window.apply_crop((10, 10, 160, 80))
    assert window.jobs[0].result is not None

    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: QMessageBox.StandardButton.Discard)
    assert window.apply_crop((10, 10, 160, 80))
    assert window.jobs[0].crop == (10, 10, 160, 80)
    assert window.jobs[0].result is None
    assert not window.crop_button.isChecked()


def test_perspective_selection_finishes_its_four_point_mode(qtbot, tmp_path: Path):
    """Would fail if the perspective control remains armed after four source points."""
    source = _image(tmp_path / "perspective.png")
    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)
    window.set_jobs([_job("a", source)])
    window.perspective_button.setChecked(True)
    window.source_preview.set_interaction_mode("perspective")

    assert window.apply_perspective([(10, 10), (180, 10), (180, 90), (10, 90)])
    assert not window.perspective_button.isChecked()


def test_partial_export_requires_explicit_choice(qtbot, monkeypatch, tmp_path: Path):
    """Would fail if export omits pending pages without an explicit choice."""
    source_a = _image(tmp_path / "a.png")
    source_b = _image(tmp_path / "b.png")
    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)
    window.set_jobs([_job("a", source_a), _job("b", source_b)])
    window.apply_page_result("a", _page("a", source_a))
    window.show()
    calls: list[list[str]] = []
    monkeypatch.setattr(window, "_export", lambda pages, separate: calls.append([page.id for page in pages]))

    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Cancel)
    window.request_export(separate=False)
    assert calls == []

    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    window.request_export(separate=False)
    assert calls == [["a"]]


def test_source_preview_uses_the_selected_tiff_frame(qtbot, tmp_path: Path):
    """Would fail if TIFF jobs always show the first source frame."""
    source = tmp_path / "two-pages.tiff"
    Image.new("RGB", (40, 30), "#cf1f1f").save(
        source,
        save_all=True,
        append_images=[Image.new("RGB", (40, 30), "#1464d2")],
    )
    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)
    window.set_jobs([ImageJob(id="second", source_path=str(source), frame_index=1)])
    window.show()

    window.show_inspector()
    pixel = window.source_preview._image.pixelColor(20, 15)

    assert pixel.blue() > pixel.red()


def test_start_processing_leaves_preprocessing_to_the_worker(qtbot, monkeypatch, tmp_path: Path):
    """Would fail if clicking start prepares every queued page in the GUI thread."""
    source = _image(tmp_path / "source.png")
    started: list[list[str]] = []

    class FakeWorker:
        busy = True

        def __init__(self, *_args):
            pass

        def start(self, jobs):
            started.append([job.id for job in jobs])

    def unexpected_gui_preprocessing(*_args):
        raise AssertionError("GUI must not prepare batch pages")

    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)
    window.set_jobs([_job("pending", source), _job("failed", source), _job("cancelled", source)])
    window.jobs[1].status = "failed"
    window.jobs[2].status = "cancelled"
    window._models_ready = True
    monkeypatch.setattr(window, "_new_batch_worker", lambda jobs: FakeWorker())
    monkeypatch.setattr(gui, "prepare_image", unexpected_gui_preprocessing, raising=False)

    window.start_processing()

    assert started == [["pending", "failed", "cancelled"]]
    assert [job.status for job in window.jobs] == ["pending", "pending", "pending"]


def test_text_only_mode_is_passed_to_worker_and_cannot_claim_restored_layout(qtbot, monkeypatch, tmp_path):
    source = _image(tmp_path / "source.png")
    started = []
    class FakeWorker:
        busy = True
        def __init__(self, *_): pass
        def start(self, jobs): started.extend(jobs)
    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)
    window.set_jobs([_job("p", source)])
    window._models_ready = True
    monkeypatch.setattr(window, "_new_batch_worker", lambda jobs: FakeWorker())
    window.export_layout_combo.setCurrentIndex(window.export_layout_combo.findData("text_only"))
    window.start_processing()
    assert started[0].text_only
    assert not window.export_layout_combo.isEnabled()
    window._worker = None
    window._poll_timer.stop()
    page = _page("p", source)
    page.text_only = True
    window.apply_page_result("p", page)
    window.export_layout_combo.setCurrentIndex(window.export_layout_combo.findData("source"))
    window.request_export(separate=False)
    assert "重新识别" in window.status_label.text()
    assert window._active_file_browser is None


def test_auto_proofread_option_is_off_by_default_and_copied_to_started_jobs(qtbot, monkeypatch, tmp_path: Path):
    source = _image(tmp_path / "source.png")
    started: list[list[ImageJob]] = []

    class FakeWorker:
        busy = True

        def __init__(self, *_args):
            pass

        def start(self, jobs):
            started.append(list(jobs))

    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)
    window.set_jobs([_job("one", source), _job("two", source)])
    window._models_ready = True
    monkeypatch.setattr(window, "_new_batch_worker", lambda jobs: FakeWorker())

    assert not window.auto_proofread_checkbox.isChecked()
    window.auto_proofread_checkbox.setChecked(True)
    window.start_processing()

    assert started
    assert all(job.auto_proofread for job in started[0])
    assert not window.auto_proofread_checkbox.isEnabled()


def test_page_progress_shows_current_consensus_pass(qtbot, tmp_path: Path):
    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)

    window._handle_worker_event({"event": "page_progress", "job_id": "p", "pass_index": 2, "pass_count": 3})

    assert window.status_label.text() == "正在辅助校对：第 2/3 次识别。"
    assert not window.proofread_progress_badge.isHidden()
    assert window.proofread_progress_badge.text() == "三次识别 第 2/3 次"


def test_text_completeness_progress_is_visible(qtbot, tmp_path: Path):
    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)

    window._handle_worker_event({"event": "page_integrity_progress", "job_id": "p"})

    assert window.status_label.text() == "正在检查版面漏行…"
    assert window.proofread_progress_badge.text() == "文字完整性检查"


def test_detail_progress_shows_region_and_pass_without_rebuilding_results(qtbot, tmp_path: Path):
    """Would fail if original-detail work looks stalled after full-page pass three."""
    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)

    window._handle_worker_event({
        "event": "page_detail_progress",
        "job_id": "p",
        "region_index": 2,
        "region_count": 5,
        "pass_index": 3,
        "pass_count": 3,
    })

    assert window.status_label.text() == "正在原图细查：第 2/5 处，第 3/3 次。"
    assert not window.proofread_progress_badge.isHidden()
    assert window.proofread_progress_badge.text() == "原图细查 2/5 · 3/3"


def test_worker_poll_keeps_queue_and_result_widgets_stable_between_events(qtbot, tmp_path: Path):
    """Polling must not rebuild both panes every 80 ms while OCR is running."""
    source = _image(tmp_path / "source.png")

    class IncrementalWorker:
        busy = True

        def __init__(self):
            self.events = [[], [{"event": "page_started", "job_id": "a"}], []]

        def poll(self):
            return self.events.pop(0)

    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)
    window.set_jobs([_job("a", source)])
    window.apply_page_result("a", _page("a", source))
    queue_item = window.queue_list.item(0)
    window.show_document()
    editor = window.document_editor
    window._worker = IncrementalWorker()

    window._poll_worker()
    window._poll_worker()
    window._poll_worker()

    assert window.queue_list.item(0) is queue_item
    assert window.document_editor is editor
    assert "处理中" in queue_item.text()


def test_export_success_marks_page_clean_and_later_edit_marks_it_dirty(qtbot, monkeypatch, tmp_path: Path):
    """Would fail if close protection forgets a post-export edit."""
    source = _image(tmp_path / "source.png")
    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)
    window.set_jobs([_job("a", source)])
    window.apply_page_result("a", _page("a", source))
    dialog = DialogProbe(window)
    monkeypatch.setattr(window, "_dialog", lambda *_args, **_kwargs: dialog)
    monkeypatch.setattr(gui, "export_docx", lambda *_args, **_kwargs: tmp_path / "result.docx")

    window._export([window.jobs[0].result], separate=False)
    dialog.selectFile(str(tmp_path / "result.docx"))
    dialog.done(LocalFileResult.ACCEPTED)
    qtbot.waitUntil(lambda: window._active_file_browser is None)
    assert not window._has_unexported_results()
    _edit(window, "a-block", "导出后修改")

    assert window._has_unexported_results()


def test_cancelled_or_failed_export_leaves_an_edited_page_dirty(qtbot, monkeypatch, tmp_path: Path):
    """Would fail if an unsuccessful export suppresses later close/remove protection."""
    source = _image(tmp_path / "source.png")
    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)
    window.set_jobs([_job("a", source)])
    window.apply_page_result("a", _page("a", source))
    window._mark_pages_exported([window.jobs[0].result])
    _edit(window, "a-block", "尚未导出的修改")
    cancelled = DialogProbe(window)
    failed = DialogProbe(window)
    dialogs = iter([cancelled, failed])
    monkeypatch.setattr(window, "_dialog", lambda *_args, **_kwargs: next(dialogs))

    window._export([window.jobs[0].result], separate=False)
    cancelled.done(LocalFileResult.REJECTED)
    qtbot.waitUntil(lambda: window._active_file_browser is None)
    assert window._has_unexported_results()

    monkeypatch.setattr(gui, "export_docx", lambda *_args: (_ for _ in ()).throw(RuntimeError("disk_full")))
    window._export([window.jobs[0].result], separate=False)
    failed.selectFile(str(tmp_path / "result.docx"))
    failed.done(LocalFileResult.ACCEPTED)
    qtbot.waitUntil(lambda: window._active_file_browser is None)

    assert window._has_unexported_results()


def test_closing_unchanged_exported_results_does_not_prompt(qtbot, monkeypatch, tmp_path: Path):
    """Would fail if an already exported page still gets a discard confirmation."""
    source = _image(tmp_path / "source.png")
    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)
    window.set_jobs([_job("a", source)])
    window.apply_page_result("a", _page("a", source))
    window._mark_pages_exported([window.jobs[0].result])
    event = QCloseEvent()
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected prompt")))

    qtbot.waitUntil(lambda: window._model_task is None)
    window.closeEvent(event)

    assert event.isAccepted()


def test_finished_batch_shows_safe_failure_codes(qtbot, tmp_path: Path):
    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)
    first = _job("first", _image(tmp_path / "first.png"))
    second = _job("second", _image(tmp_path / "second.png"))
    first.status = second.status = "failed"
    first.error_code = "ocr_init_failed"
    second.error_code = "ocr_init_failed"
    window.set_jobs([first, second])

    window._finish_worker(cancelled=False)

    assert "2 页失败" in window.status_label.text()
    assert "ocr_init_failed" in window.status_label.text()


def test_finished_batch_counts_layout_fallback_pages(qtbot, tmp_path: Path):
    source = _image(tmp_path / "source.png")
    job = _job("plain", source)
    job.status = "done"
    job.result = _page("plain", source)
    job.result.recognition_warning = "layout_fallback"
    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)
    window.set_jobs([job])

    window._finish_worker(cancelled=False)

    assert "1 页可导出" in window.status_label.text()
    assert "1 页已使用普通文字识别，版面需核对" in window.status_label.text()


def test_removing_unexported_page_requires_discard_confirmation(qtbot, monkeypatch, tmp_path: Path):
    """Would fail if a completed page can disappear without protecting its OCR result."""
    source = _image(tmp_path / "source.png")
    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)
    window.set_jobs([_job("a", source)])
    window.apply_page_result("a", _page("a", source))
    window.show()
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args, **_kwargs: QMessageBox.StandardButton.Cancel)

    window.remove_current()

    assert [job.id for job in window.jobs] == ["a"]


def test_close_cancel_keeps_unexported_result_open(qtbot, monkeypatch, tmp_path: Path):
    """Would fail if closing silently discards an unexported recognized page."""
    source = _image(tmp_path / "source.png")
    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)
    window.set_jobs([_job("a", source)])
    window.apply_page_result("a", _page("a", source))
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args, **_kwargs: QMessageBox.StandardButton.Cancel)
    event = QCloseEvent()

    qtbot.waitUntil(lambda: window._model_task is None)
    window.closeEvent(event)

    assert not event.isAccepted()


def test_dead_worker_without_terminal_event_recovers_for_retry(qtbot, tmp_path: Path):
    """Would fail if a dead OCR child leaves the window permanently busy."""
    source = _image(tmp_path / "source.png")

    class DeadWorker:
        busy = False

        def poll(self):
            return []

        def close(self):
            pass

    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)
    window.set_jobs([_job("a", source)])
    window.jobs[0].status = "processing"
    window._worker = DeadWorker()

    window._poll_worker()

    assert window._worker is None
    assert window.jobs[0].status == "failed"
    assert window.jobs[0].error_code == "worker_process_failed"
    assert "worker_process_failed" in window.status_label.text()


def test_failed_local_rerecognition_preserves_completed_page_without_full_page_retry(qtbot, tmp_path: Path):
    """Would fail if a region OCR error demotes reviewed whole-page output to failed."""
    source = _image(tmp_path / "source.png")

    class RegionFailureWorker:
        busy = True

        def poll(self):
            return [
                {"event": "region_error", "job_id": "a", "code": "invalid_region"},
                {"event": "finished"},
            ]

        def close(self):
            pass

    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)
    window.set_jobs([_job("a", source)])
    page = _page("a", source)
    window.apply_page_result("a", page)
    _edit(window, "a-block", "人工校对内容")
    window._pending_region = ("a", "a-block")
    window._worker = RegionFailureWorker()

    window._poll_worker()

    assert window.jobs[0].status == "done"
    assert window.jobs[0].error_code is None
    assert page.blocks[0].edited_text == "人工校对内容"
    assert window._pending_region is None
    assert not window.retry_button.isEnabled()
    assert "局部识别失败" in window.status_label.text()
    assert "已保留" in window.status_label.text()


def test_region_error_through_real_worker_pipe_preserves_completed_page(qtbot, monkeypatch, tmp_path: Path):
    """Would fail if OCRWorker.poll mutates the shared job before GUI handles a region failure."""
    source = _image(tmp_path / "source.png")
    window = MainWindow(tmp_path / "missing-models", tmp_path / "session")
    qtbot.addWidget(window)
    window.set_jobs([_job("a", source)])
    page = _page("a", source)
    window.apply_page_result("a", page)
    _edit(window, "a-block", "人工校对内容")
    worker_instance = worker.OCRWorker(tmp_path, tmp_path)
    receiver, sender = worker_instance._context.Pipe(duplex=False)
    worker_instance._receiver = receiver
    worker_instance._jobs = {"a": window.jobs[0]}

    class Lease:
        def close(self):
            pass

    monkeypatch.setattr(worker, "_silence_child_output", lambda: None)
    monkeypatch.setattr(worker, "claim_session_lease", lambda _: Lease())
    monkeypatch.setattr(worker, "_create_engine", lambda *_: object())
    worker._process_region(str(tmp_path), str(tmp_path), window.jobs[0], (0, 0, 0, 10), sender)
    window._pending_region = ("a", "a-block")
    window._worker = worker_instance

    window._poll_worker()

    assert window.jobs[0].status == "done"
    assert window.jobs[0].error_code is None
    assert page.blocks[0].edited_text == "人工校对内容"
    assert window._pending_region is None
    assert not window.retry_button.isEnabled()
    assert "局部识别失败" in window.status_label.text()


def test_glass_workspace_keeps_queue_and_original_visible_with_contextual_editor(qtbot, tmp_path):
    source = _image(tmp_path/'source.png')
    window = MainWindow(tmp_path/'models', tmp_path/'session')
    window.set_jobs([_job('a', source)])
    window.show()
    assert window.views.currentWidget() is window.gallery_view
    assert not window.document_editor.isVisible()
    assert window.source_preview.isVisible()
    window.apply_page_result('a', _page('a', source))
    window.document_button.click()
    assert window.queue_list.isVisible() and window.processed_preview.isVisible()
    assert not window.drawer.isVisible()
    window.details_button.click()
    assert window.proofread_panel.edit.isVisible()
    window.full_document_button.click()
    assert window.document_editor.isVisible() and not window.processed_preview.isVisible()
    window.compare_button.click()
    assert window.processed_preview.isVisible() and not window.document_editor.isVisible()
    window.gallery_button.click()
    assert window.queue_list.isVisible() and not window.document_editor.isVisible()


def test_candidate_is_complete_read_only_and_only_adopted_on_request(qtbot, tmp_path):
    source = _image(tmp_path/'source.png')
    window = MainWindow(tmp_path/'models', tmp_path/'session')
    window.set_jobs([_job('a', source)])
    result = _page('a', source)
    block = result.blocks[0]
    candidate = '候选中的完整文字。' * 24
    block.candidates = [candidate]
    window.apply_page_result('a', result)
    window.show_document()
    window.document_editor.select_target(block.id)
    window.show_text_details()
    assert window.original_text_view.isReadOnly()
    assert window.original_text_view.toPlainText() == block.original_text
    window.candidate_picker.setCurrentIndex(1)
    assert window.candidate_text_view.toPlainText() == candidate
    assert block.edited_text is None
    window.apply_candidate_button.click()
    assert block.text == candidate
    assert block.proofread_source == 'manual_candidate'
    window.document_editor.undo()
    assert block.text == block.original_text


def test_cell_candidates_keep_other_cells_and_originals(qtbot, tmp_path):
    source = _image(tmp_path/'source.png')
    window = MainWindow(tmp_path/'models', tmp_path/'session')
    window.set_jobs([_job('a', source)])
    cell = Cell(0, 0, '甲', candidates=['候选甲'], confidence=.65)
    other = Cell(0, 1, '乙')
    table = Block('table', 'table', (10, 10, 180, 80), rows=1, cols=2, cells=[cell, other])
    result = _page('a', source)
    result.blocks = [table]
    window.apply_page_result('a', result)
    window.show_document()
    window.document_editor.select_target(window.document_editor.cell_key(table.id, cell))
    window.show_text_details()
    assert window.original_text_view.toPlainText() == '甲'
    assert not window.rerecognize_button.isEnabled()
    window.candidate_picker.setCurrentIndex(1)
    window.apply_candidate_button.click()
    assert cell.text == '候选甲' and other.text == '乙' and cell.original_text == '甲'


def test_review_list_filters_and_locates_across_pages_without_losing_edits(qtbot, tmp_path):
    source = _image(tmp_path/'source.png')
    window = MainWindow(tmp_path/'models', tmp_path/'session')
    window.set_jobs([_job('a', source), _job('b', source)])
    first, second = _page('a', source), _page('b', source)
    first.blocks[0].confidence = .65
    second.blocks[0].candidates = ['甲', '乙']
    second.blocks[0].proofread_status = 'conflict'
    second.blocks[0].proofread_passes = 3
    window.apply_page_result('a', first)
    window.apply_page_result('b', second)
    _edit(window, 'a-block', '保留校对')
    window._render_review_summary()
    assert window.review_list.count() == 2
    window.review_filter_combo.setCurrentIndex(window.review_filter_combo.findData('low'))
    assert window.review_list.count() == 1
    window.review_filter_combo.setCurrentIndex(window.review_filter_combo.findData('all'))
    window.review_sort_combo.setCurrentIndex(window.review_sort_combo.findData('page'))
    window.review_list.setCurrentRow(1)
    window._open_review_selection()
    assert window.current_job.id == 'b'
    assert window.document_editor.current_block_id == 'b-block'
    assert window.compare_button.isChecked()
    assert first.blocks[0].text == '保留校对'


@pytest.mark.parametrize('warning, expected', [('text_recovery', '补回'), ('layout_fallback', '版面识别未完成')])
def test_page_warning_is_visible_in_document_view(qtbot, tmp_path, warning, expected):
    source = _image(tmp_path/'source.png')
    window = MainWindow(tmp_path/'models', tmp_path/'session')
    window.set_jobs([_job('a', source)])
    result = _page('a', source)
    result.recognition_warning = warning
    window.apply_page_result('a', result)
    window.show()
    window.show_document()
    assert window.page_notice.isVisible() and expected in window.page_notice.text()


def test_unstructured_table_remains_editable_and_available_for_review(qtbot, tmp_path):
    source = _image(tmp_path/'source.png')
    window = MainWindow(tmp_path/'models', tmp_path/'session')
    window.set_jobs([_job('a', source)])
    result = _page('a', source)
    result.blocks = [Block('table', 'table', (10, 10, 180, 80), original_text='甲\t乙', needs_review=True)]
    window.apply_page_result('a', result)
    window.show_document()
    window.document_editor.replace_target('table', '甲\t已改乙')
    window._render_review_summary()
    assert window.review_list.count() == 1
    assert result.blocks[0].text == '甲\t已改乙'


def test_precise_proofreading_status_is_explained_once_without_paragraph_cards(qtbot, tmp_path):
    source = _image(tmp_path/'source.png')
    window = MainWindow(tmp_path/'models', tmp_path/'session')
    window.set_jobs([_job('a', source)])
    result = _page('a', source)
    result.proofread_status = 'majority'
    result.proofread_completed_passes = 3
    result.detail_reviewed_regions = 4
    result.detail_candidate_regions = 2
    window.apply_page_result('a', result)
    window.show_document()
    assert '3/3' in window.page_notice.text() and '4 处' in window.page_notice.text()
    assert not window.findChildren(QFrame, 'blockCard')
    assert not window.findChildren(QFrame, 'reviewCard')


def test_worker_candidate_event_does_not_reset_caret_text_or_undo(qtbot, tmp_path):
    source = _image(tmp_path/'source.png')
    window = MainWindow(tmp_path/'models', tmp_path/'session')
    window.set_jobs([_job('a', source)])
    result = _page('a', source)
    window.apply_page_result('a', result)
    _edit(window, 'a-block', '人工修改')
    cursor = window.document_editor.textCursor().position()
    window._pending_region = ('a', 'a-block')
    window._handle_worker_event({'event': 'region_done', 'job_id': 'a', 'candidates': ['候选']})
    assert result.blocks[0].text == '人工修改'
    assert window.document_editor.textCursor().position() == cursor
    window.document_editor.undo()
    assert result.blocks[0].text == '原始文字'


def test_export_options_mirror_mode_and_open_the_selected_path_flow(qtbot, tmp_path, monkeypatch):
    source = _image(tmp_path/'source.png')
    window = MainWindow(tmp_path/'models', tmp_path/'session')
    window.set_jobs([_job('a', source)])
    window.apply_page_result('a', _page('a', source))
    called = []
    monkeypatch.setattr(window, 'request_export', lambda *, separate: called.append((separate, window.export_layout_combo.currentData())))
    window.show_export_options()
    window.export_mode_picker.setCurrentIndex(window.export_mode_picker.findData('paragraph'))
    assert '自然换行、分页' in window.export_mode_note.text()
    window.export_separate_button.click()
    qtbot.waitUntil(lambda: bool(called))
    assert called == [(True, 'paragraph')]


def test_delete_in_document_never_removes_an_image_job(qtbot, tmp_path):
    source = _image(tmp_path/'source.png')
    window = MainWindow(tmp_path/'models', tmp_path/'session')
    window.set_jobs([_job('a', source)])
    window.apply_page_result('a', _page('a', source))
    window.show()
    window.show_document()
    editor = window.document_editor
    editor.setFocus()
    editor.selectAll()
    qtbot.keyClick(editor, Qt.Key.Key_Delete)
    assert len(window.jobs) == 1
    assert window.jobs[0].result.blocks[0].text == ''


def test_invalid_perspective_rejected_before_ocr(qtbot, tmp_path):
    source = _image(tmp_path/'source.png')
    window = MainWindow(tmp_path/'models', tmp_path/'session')
    window.set_jobs([_job('a', source)])
    assert not window.apply_perspective([(10,10)]*4)
    assert window.jobs[0].perspective is None
    assert '重新选择' in window.status_label.text()
