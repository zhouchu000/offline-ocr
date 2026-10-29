"""Chinese PySide6 desktop interface for the offline image-to-DOCX workflow."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Callable

from PySide6.QtCore import QSize, QTimer, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QListWidgetItem,
    QMessageBox, QPlainTextEdit, QVBoxLayout,
)

from .domain import Block, Cell, ImageJob, PageResult
from .exporter import export_docx as export_docx
from .file_browser import LocalFileBrowser, LocalFileMode, LocalFileResult
from .images import import_images
from .review import ReviewIndex
from .security import validate_models
from .theme import FAILURE_COLOR, PRIMARY_COLOR
from .fluent import FluentWindow
from .worker import OCRWorker


IMAGE_FILTER = "图片文件 (*.jpg *.jpeg *.png *.bmp *.tif *.tiff)"
STATUS_TEXT = {
    "pending": "待处理",
    "processing": "处理中",
    "done": "完成",
    "failed": "失败",
    "cancelled": "已取消",
}


def _use_native_file_dialogs() -> bool:
    return sys.platform == "win32"


def _safe_error_code(error: Exception | object) -> str:
    code = getattr(error, "code", None)
    if code:
        return str(code)
    return "UNAVAILABLE"


class MainWindow(FluentWindow):
    """The application shell. Domain objects remain the single source of edits."""

    def __init__(self, model_root: Path, session_dir: Path) -> None:
        super().__init__()
        self.model_root = Path(model_root)
        self.session_dir = Path(session_dir)
        self.jobs: list[ImageJob] = []
        self.review_index = ReviewIndex()
        self._review_dirty = set()
        self._review_timer = QTimer(self)
        self._review_timer.setSingleShot(True)
        self._review_timer.timeout.connect(self._flush_review_changes)
        self._exported_signatures: dict[str, tuple[object, ...]] = {}
        self._current_job_id: str | None = None
        self._selected_block_id: str | None = None
        self._details_dialog = None
        self._review_dialog = None
        self._details_key = ""
        self._prepared_paths: dict[str, Path] = {}
        self._worker: OCRWorker | None = None
        self._engine_cache = None
        self._batch_job_ids: set[str] = set()
        self._queued_during_run: set[str] = set()
        self._removal_requests: set[str] = set()
        self._continue_after_batch = False
        self._pending_region: tuple[str, str] | None = None
        self._local_region_error: str | None = None
        self._models_ready = False
        self._model_task = None
        self._close_after_model_check = False
        self._default_image_mode = 'scan'
        self._preview_path = None
        self._preview_paths = []
        self._model_code = "MODEL_UNAVAILABLE"
        self._active_file_browser: LocalFileBrowser | QDialog | None = None
        self._file_browser_cache: dict[tuple[str, LocalFileMode, str | None], LocalFileBrowser | QDialog] = {}
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(80)
        self._poll_timer.timeout.connect(self._poll_worker)
        self._cache_timer = QTimer(self)
        self._cache_timer.setInterval(5000)
        self._cache_timer.timeout.connect(self._expire_idle_workers)
        self._cache_timer.start()

        self.setWindowTitle("离线图片转 Word")
        area = self.screen().availableGeometry()
        self.setMinimumSize(min(860, area.width()), min(540, area.height()))
        self.resize(min(1440, area.width()), min(900, area.height()))
        from .export_ui import ExportController
        self.export_controller = ExportController(self)
        self._build_ui()
        self.export_layout_combo.currentIndexChanged.connect(self._refresh_action_states)
        self._check_models()

    def _build_ui(self) -> None:
        from .workspace_ui import build_workspace
        build_workspace(self)

    def _make_shortcuts(self) -> None:
        for text, shortcut, callback in (
            ("导入图片", "Ctrl+O", self.import_files),
            ("查找页面", "Ctrl+L", self._focus_page_search),
            ("开始识别", "Ctrl+Return", self.start_processing),
            ("收起面板或停止识别", "Escape", self.dismiss_or_cancel),
            ("下一处待复核", "F6", self.proofread_panel.next_review),
            ("上一处待复核", "Shift+F6", lambda: self.proofread_panel.next_review(direction=-1)),
            ("确认本处并继续", "Ctrl+Shift+Return", self.proofread_panel.confirm_and_next),
        ):
            action = QAction(text, self)
            action.setShortcut(shortcut)
            action.triggered.connect(callback)
            self.addAction(action)

    def _check_models(self) -> None:
        from .model_check import ModelCheck
        self._set_model_badge('正在校验模型', 'pending')
        self._model_task = ModelCheck(self, self.model_root, validate_models)
        self._model_task.finished.connect(self._models_checked)
        self._refresh_action_states()
        self._model_task.start()

    def _models_checked(self):
        if self._model_task is None:
            return
        task, self._model_task = self._model_task, None
        if task.code:
            self._models_ready = False
            self._model_code = task.code
            self._set_model_badge("模型不可用", "error")
            self._set_status(f"模型不可用（{self._model_code}），识别前须安装并校验离线模型。", warning=True)
        else:
            self._models_ready = True
            self._model_code = ""
            self._set_model_badge("模型已就绪", "success")
            self._set_status(
                "模型就绪。添加图片后开始识别。"
                if not self.jobs
                else "模型校验完成。"
            )
        self._refresh_action_states()
        task.deleteLater()
        if self._close_after_model_check:
            self.close()

    def _set_model_badge(self, text: str, tone: str) -> None:
        self.model_badge.setText(text)
        self.model_badge.setProperty("badgeTone", tone)
        self.model_badge.style().unpolish(self.model_badge)
        self.model_badge.style().polish(self.model_badge)

    def _set_status(self, text: str, *, warning: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setToolTip(text)
        self.status_label.setProperty("statusTone", "warning" if warning else "secondary")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        if hasattr(self, 'task_stage'):
            self.task_stage.setText(text)

    @property
    def current_job(self) -> ImageJob | None:
        if not self._current_job_id:
            return None
        return next((job for job in self.jobs if job.id == self._current_job_id), None)

    def set_jobs(self, jobs: list[ImageJob]) -> None:
        self.review_index.clear()
        self.jobs = list(jobs)
        job_ids = {job.id for job in self.jobs}
        self._exported_signatures = {
            job_id: signature for job_id, signature in self._exported_signatures.items() if job_id in job_ids
        }
        self._current_job_id = self.jobs[0].id if self.jobs else None
        self._selected_block_id = None
        self._rebuild_queue()
        if self.jobs:
            self.queue_list.setCurrentRow(0)
            if self._models_ready:
                self._set_status(f"已载入 {len(self.jobs)} 页，等待识别。")
            else:
                self._set_status(
                    f"已载入 {len(self.jobs)} 页；模型不可用（{self._model_code}），无法开始识别。",
                    warning=True,
                )
        else:
            self._clear_current_page()
            self._set_status("尚未导入图片。")
        self._refresh_action_states()

    def import_files(self) -> None:
        if self._active_file_browser is not None:
            return
        dialog = self._dialog("导入图片", LocalFileMode.EXISTING_FILES)
        dialog.setNameFilter(IMAGE_FILTER)
        self._open_file_browser(dialog, self._import_paths, allow_busy=True)

    def import_folder(self) -> None:
        if self._active_file_browser is not None:
            return
        dialog = self._dialog("导入图片文件夹", LocalFileMode.DIRECTORY, accept_label="选择")
        self._open_file_browser(dialog, self._import_paths, allow_busy=True)

    def _dialog(
        self,
        title: str,
        mode: LocalFileMode,
        *,
        accept_label: str | None = None,
    ) -> LocalFileBrowser | QDialog:
        key = (title, mode, accept_label)
        dialog = self._file_browser_cache.get(key)
        if dialog is None:
            if _use_native_file_dialogs():
                from .native_file_dialog import NativeFileDialog

                dialog = NativeFileDialog(self, title, mode, accept_label=accept_label)
            else:
                dialog = LocalFileBrowser(self.central_stack, title, mode, accept_label=accept_label)
            self._file_browser_cache[key] = dialog
        else:
            dialog.reset_for_open()
        return dialog

    def _open_file_browser(self, dialog: LocalFileBrowser | QDialog, on_accepted: Callable[[list[str]], None], *, allow_busy: bool = False) -> None:
        """Keep either selector alive across asynchronous acceptance/cancellation."""
        if self._active_file_browser is not None or (self._worker is not None and not allow_busy):
            return
        self._active_file_browser = dialog
        if not isinstance(dialog, QDialog):
            if self.central_stack.indexOf(dialog) < 0:
                self.central_stack.addWidget(dialog)
            self.central_stack.setCurrentWidget(dialog)

        finish_scheduled = False

        def finished(result: int) -> None:
            nonlocal finish_scheduled
            if self._active_file_browser is not dialog or finish_scheduled:
                return
            finish_scheduled = True
            try:
                paths = list(dialog.selectedFiles()) if result == int(LocalFileResult.ACCEPTED) else []
            except Exception:
                paths = []
                self._set_status("未能读取所选路径，请重新添加。", warning=True)

            def complete_after_dismissal() -> None:
                if self._active_file_browser is not dialog:
                    return
                try:
                    # Keep the embedded browser and all of its native macOS
                    # accessibility objects alive for the main-window lifetime.
                    # Destroying and recreating these widgets mid-session can
                    # make Qt/Cocoa release a QMacAccessibilityElement twice.
                    self.central_stack.setCurrentWidget(self.workspace_root)
                    if paths:
                        on_accepted(paths)
                finally:
                    dialog.finished.disconnect(finished)
                    self._active_file_browser = None
                    self._refresh_action_states()
                    self._continue_added_jobs()

            # Do not parse an image or dispose the browser from inside its
            # dismissal callback. Let Qt finish that event first.
            QTimer.singleShot(0, complete_after_dismissal)

        dialog.finished.connect(finished)
        self._refresh_action_states()
        try:
            dialog.open()
        except Exception:
            dialog.finished.disconnect(finished)
            self._active_file_browser = None
            self.central_stack.setCurrentWidget(self.workspace_root)
            self._refresh_action_states()
            self._set_status("无法打开文件选择窗口，请重试。", warning=True)

    def _import_paths(self, paths: list[str]) -> None:
        try:
            imported = import_images(paths)
        except Exception:
            self._set_status("导入失败：无法读取所选图片。", warning=True)
            return
        identities = {(str(Path(job.source_path).resolve()).casefold(), job.frame_index) for job in self.jobs}
        added = []
        for job in imported:
            job.mode = self._default_image_mode
            key = (str(Path(job.source_path).resolve()).casefold(), job.frame_index)
            if key not in identities:
                identities.add(key)
                added.append(job)
        if not added:
            self._set_status("所选图片已在列表中。" if imported else "没有找到支持的图片。")
            return
        selected_ids = {str(item.data(Qt.ItemDataRole.UserRole)) for item in self.queue_list.selectedItems()}
        self.jobs.extend(added)
        if self._current_job_id is None:
            self._current_job_id = added[0].id
        if self._worker:
            for job in added:
                self.recognition_controls.apply_to_job(job)
            pending = [job for job in added if job.status == "pending"]
            self._queued_during_run.update(job.id for job in pending)
            self._continue_after_batch = True
            if self._worker.add_jobs(pending):
                self._batch_job_ids.update(job.id for job in pending)
        self._rebuild_queue()
        for index, job in enumerate(self.jobs):
            if job.id in selected_ids:
                self.queue_list.item(index).setSelected(True)
        self._refresh_action_states()
        self._set_status(f"已添加 {len(added)} 张图片，共 {len(self.jobs)} 张。" + ("将接续识别。" if self._worker else ""))
        failed = sum(1 for job in imported if job.status == "failed")
        if failed:
            self._set_status(f"已导入 {len(imported)} 页，其中 {failed} 页无法读取。", warning=True)

    def _on_job_selected(self, current: QListWidgetItem | None, previous: QListWidgetItem | None) -> None:
        if current is None:
            self._current_job_id = None
            self._clear_current_page()
            self._refresh_action_states()
            return
        self._current_job_id = str(current.data(Qt.ItemDataRole.UserRole))
        self._selected_block_id = None
        self._refresh_current_page()

    def _rebuild_queue(self) -> None:
        selected_id = self._current_job_id
        self.queue_list.blockSignals(True)
        self.queue_list.clear()
        selected_row = -1
        for index, job in enumerate(self.jobs):
            item = QListWidgetItem()
            self._update_queue_item(item, index, job)
            self.queue_list.addItem(item)
            if job.id == selected_id:
                selected_row = index
        self.queue_list.blockSignals(False)
        self.queue_stack.setCurrentWidget(self.queue_list if self.jobs else self.queue_empty_label)
        if selected_row >= 0:
            self.queue_list.setCurrentRow(selected_row)
        self._refresh_progress()

    def _update_queue_item(self, item: QListWidgetItem, index: int, job: ImageJob) -> None:
        """Update a queue row without replacing the item or changing selection."""
        source_name = job.display_name or Path(job.source_path).name or "未命名图片"
        frame = f" · 第 {job.frame_index + 1} 帧" if job.frame_index else ""
        status_text = "移除中" if job.id in self._removal_requests else STATUS_TEXT.get(job.status, "未知")
        text = f"{index + 1:02d}  {source_name}{frame}\n{status_text}"
        if item.text() != text:
            item.setText(text)
        if item.data(Qt.ItemDataRole.UserRole) != job.id:
            item.setData(Qt.ItemDataRole.UserRole, job.id)
            item.setData(Qt.ItemDataRole.UserRole + 1, [job.source_path, job.frame_index])
        tooltip = f'{source_name}{frame}\n{status_text}'
        if job.error_code:
            tooltip += f'（{job.error_code}）'
        if item.toolTip() != tooltip:
            item.setToolTip(tooltip)
        if item.sizeHint() != QSize(220, 76):
            item.setSizeHint(QSize(220, 76))
        item.setData(Qt.ItemDataRole.UserRole + 2, sum(
            entry.block.review_confirmed_signature != entry.fingerprint
            for entry in self.review_index.page_items(job)) if job.result else 0)
        color = FAILURE_COLOR if job.status == "failed" else PRIMARY_COLOR if job.status == "processing" else None
        if color is None:
            item.setData(Qt.ItemDataRole.ForegroundRole, None)
        elif item.foreground().color() != QColor(color):
            item.setForeground(QColor(color))

    def _sync_queue_items(self) -> None:
        """Apply worker status changes in place so polling cannot make panes jump."""
        if self.queue_list.count() != len(self.jobs) or any(
            self.queue_list.item(index).data(Qt.ItemDataRole.UserRole) != job.id
            for index, job in enumerate(self.jobs)
        ):
            self._rebuild_queue()
            return
        for index, job in enumerate(self.jobs):
            self._update_queue_item(self.queue_list.item(index), index, job)
        self._refresh_progress()

    def _refresh_progress(self) -> None:
        total = len(self.jobs)
        terminal = sum(1 for job in self.jobs if job.status in {"done", "failed", "cancelled"})
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(terminal)
        count = f"完成 {terminal}/{total} 页" if total else "完成 0/0 页"
        self.queue_count_label.setText(str(total))
        self.progress_bar.setVisible(bool(total))
        self.progress_count_label.setVisible(bool(total))
        self._sync_page_combo()
        self.progress_count_label.setText(count)
        if hasattr(self, 'right_stack'):
            self._sync_shell()

    def move_current(self, offset: int) -> None:
        job = self.current_job
        if not job or self._worker:
            return
        index = self.jobs.index(job)
        target = index + offset
        if not 0 <= target < len(self.jobs):
            return
        self.jobs[index], self.jobs[target] = self.jobs[target], self.jobs[index]
        self._rebuild_queue()

    def remove_current(self) -> None:
        if self._active_file_browser is not None:
            return
        selected = {str(item.data(Qt.ItemDataRole.UserRole)) for item in self.queue_list.selectedItems()}
        jobs = [job for job in self.jobs if job.id in selected and job.id not in self._removal_requests
                and job.status != "processing" and (not self._pending_region or job.id != self._pending_region[0])]
        if not jobs:
            return
        if not self._confirm_discard_results(jobs, title="移除所选图片", message="所选图片有尚未导出的结果。移除会丢弃这些结果和修改，原图片文件保留。是否继续？"):
            return
        waiting = [job.id for job in jobs if self._worker and job.id in self._batch_job_ids and job.status == "pending"]
        immediate = {job.id for job in jobs} - set(waiting)
        self._remove_jobs_now(immediate)
        if waiting:
            self._removal_requests.update(waiting)
            # If the worker is already exiting, _finish_worker resolves these
            # requests after receiving its last results. Never guess that a
            # pending row has not just started in the child.
            self._worker.remove_jobs(waiting)
            self._set_status("正在移除等待中的任务…")
        else:
            self._set_status(f"已移除 {len(immediate)} 张图片，原文件保留。")
        self._sync_queue_items()
        self._refresh_action_states()

    def _remove_jobs_now(self, job_ids: set[str]) -> None:
        if not job_ids:
            return
        index = self.jobs.index(self.current_job) if self.current_job else 0
        self.jobs = [job for job in self.jobs if job.id not in job_ids]
        for job_id in job_ids:
            self._prepared_paths.pop(job_id, None)
            self._exported_signatures.pop(job_id, None)
        self._queued_during_run.difference_update(job_ids)
        self._removal_requests.difference_update(job_ids)
        self._batch_job_ids.difference_update(job_ids)
        if self._current_job_id in job_ids:
            self._current_job_id = self.jobs[min(index, len(self.jobs) - 1)].id if self.jobs else None
        self._rebuild_queue()
        if self.jobs:
            self.queue_list.setCurrentRow(self.jobs.index(self.current_job))
        else:
            self._clear_current_page()
        self._refresh_action_states()

    def _continue_added_jobs(self) -> None:
        if self._worker or self._active_file_browser or self.project_controller.busy or not self._continue_after_batch:
            return
        self._continue_after_batch = False
        if any(job.id in self._queued_during_run and job.status == "pending" for job in self.jobs):
            self.start_processing()

    def _refresh_current_page(self) -> None:
        job = self.current_job
        if job is None:
            self._clear_current_page()
            return
        if self.views.currentWidget() is self.inspector_view:
            self.source_preview.set_image(job.source_path, frame_index=job.frame_index)
            self.source_preview.set_adjustments(rotation=job.rotation, crop=job.crop, perspective=job.perspective)
            self.inspector_title.setText(job.display_name or Path(job.source_path).name)
        if self.views.currentWidget() is self.document_view:
            self._render_editors(job.result)
            self._load_comparison()
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(1 if job.mode == 'photo' else 0)
        self.mode_combo.blockSignals(False)
        self.mode_cards[1 if job.mode == 'photo' else 0].setChecked(True)
        self._sync_page_combo()
        self._refresh_action_states()

    def _clear_current_page(self) -> None:
        self.source_preview.set_image(None)
        self.processed_preview.set_image(None)
        self._render_editors(None)
        if not self.jobs:
            self.show_gallery()

    def _render_editors(self, result: PageResult | None) -> None:
        self.document_editor.set_page(result)
        warnings = {
            'layout_fallback': '版面识别未完成，文字已保留；请核对表格与阅读顺序。',
            'text_recovery': '漏识文字已补回；请核对内容与排版。',
        }
        text = warnings.get(result.recognition_warning, '') if result else ''
        if result and result.paper_review_count:
            text = ' · '.join(filter(None, (text, f'{result.paper_review_count} 处内容位于纸面边缘或外侧，已保留，待核对。')))
        if result and result.proofread_completed_passes:
            status = {'consistent': '结果一致', 'majority': '候选待复核', 'conflict': '差异待复核', 'incomplete': '部分未完成'}.get(result.proofread_status, '待复核')
            text = ' · '.join(filter(None, (text, f'精细校对 {result.proofread_completed_passes}/3 · {status}')))
        if result and result.detail_reviewed_regions:
            text = ' · '.join(filter(None, (text, f'原图细查 {result.detail_reviewed_regions} 处，{result.detail_candidate_regions} 处候选')))
        self.page_notice.setText(text)
        self.page_notice.setVisible(bool(text))
        self.document_target_changed(self.document_editor.current_block_id)
        if self._details_dialog and self._details_dialog.isVisible():
            self._refresh_text_details()

    def show_gallery(self):
        self.motion.capture()
        self.close_context_panel()
        self.views.setCurrentWidget(self.gallery_view)
        self.gallery_button.setChecked(True)
        self._refresh_current_page() if self.current_job else self._refresh_action_states()
        self.motion.reveal()

    def show_document(self):
        ready = [job for job in self.jobs if job.result is not None]
        if not ready:
            return
        if self.current_job not in ready:
            self.queue_list.setCurrentRow(self.jobs.index(ready[0]))
        self.motion.capture()
        self.close_context_panel()
        self.views.setCurrentWidget(self.document_view)
        self.document_button.setChecked(True)
        self._refresh_current_page()
        self.motion.reveal()

    def show_inspector(self):
        if not self.current_job:
            return
        self.close_context_panel()
        self.views.setCurrentWidget(self.inspector_view)
        self.gallery_button.setChecked(True)
        self._refresh_current_page()

    def open_selected_image(self, _item=None):
        if self.current_job and self.current_job.result:
            self.show_document()
        else:
            self.show_inspector()

    def _focus_page_search(self):
        if any(job.result for job in self.jobs):
            self.show_document()
            self.page_combo.setFocus()
            self.page_combo.lineEdit().selectAll()

    def _sync_page_combo(self):
        if not hasattr(self, 'page_combo'):
            return
        ready = [job for job in self.jobs if job.result is not None]
        entries = [(f'{self.jobs.index(job)+1:02d}  {job.display_name or Path(job.source_path).name}', job.id) for job in ready]
        previous = [(self.page_combo.itemText(i), self.page_combo.itemData(i)) for i in range(self.page_combo.count())]
        editor = self.page_combo.lineEdit()
        searching = editor.hasFocus() and self.page_combo.currentData() == self._current_job_id
        query, cursor = editor.text(), editor.cursorPosition()
        self.page_combo.blockSignals(True)
        if previous != entries:
            self.page_combo.clear()
            for text, job_id in entries:
                self.page_combo.addItem(text, job_id)
        self.page_combo.setCurrentIndex(self.page_combo.findData(self._current_job_id))
        if searching:
            editor.setText(query)
            editor.setCursorPosition(cursor)
        else:
            editor.setCursorPosition(0)
        self.page_combo.blockSignals(False)
        current = self.page_combo.currentIndex()
        self.previous_page_button.setEnabled(current > 0)
        self.next_page_button.setEnabled(0 <= current < len(ready)-1)

    def select_document_page(self, index):
        job_id = self.page_combo.itemData(index)
        if job_id:
            job = next((j for j in self.jobs if j.id == job_id), None)
            if job:
                self.queue_list.setCurrentRow(self.jobs.index(job))

    def navigate_page(self, offset):
        index = self.page_combo.currentIndex() + offset
        if 0 <= index < self.page_combo.count():
            self.motion.capture()
            self.page_combo.setCurrentIndex(index)
            self.motion.reveal()

    def toggle_comparison(self):
        self.motion.capture()
        enabled = self.compare_button.isChecked()
        self.comparison_panel.setVisible(enabled)
        self.document_surface.setVisible(not enabled)
        self.full_document_button.setChecked(not enabled)
        self._load_comparison()
        self.motion.reveal()

    def show_full_document(self):
        self.compare_button.setChecked(False)
        self.toggle_comparison()
        self.document_editor.setFocus(Qt.FocusReason.OtherFocusReason)

    def _load_comparison(self):
        job = self.current_job
        if not self.compare_button.isChecked() or not job or not job.result:
            return
        previous=self.processed_preview._identity
        self.processed_preview.set_image(job.result.processed_image_path)
        if self.processed_preview._identity != previous:
            self.processed_preview.fit_image()
        self.processed_preview.set_blocks(job.result.blocks, self._selected_block_id)

    def document_target_changed(self, block_id):
        self._selected_block_id = block_id or None
        target = self.document_editor.current_target
        self.processed_preview.set_selected_block(block_id)
        if target:
            name = '表格单元格' if isinstance(target, Cell) else self._kind_label(target.kind)
            score = f' · 置信度 {target.confidence:.0%}' if target.confidence is not None else ''
            if target.confidence is None and isinstance(target, Cell):
                score = ' · 置信度未知，待复核'
            self.context_label.setText(name + score)
        else:
            self.context_label.setText('编辑正文')
        self.details_button.setEnabled(target is not None)
        if hasattr(self, 'proofread_panel'):
            self.proofread_panel.refresh()

    def select_block(self, block_id: str, *, focus_editor: bool = False) -> None:
        self.document_editor.select_target(block_id)
        self._selected_block_id = block_id
        self.processed_preview.set_selected_block(block_id)
        job = self.current_job
        if job and job.result:
            block = next((b for b in job.result.blocks if b.id == block_id), None)
            if block:
                self.processed_preview.reveal_bbox(block.bbox)
        if self.views.currentWidget() is self.document_view and self.document_editor.current_target:
            self.show_proofreading(toggle=False)
        if focus_editor:
            if self.document_editor.isVisible():
                self.document_editor.setFocus(Qt.FocusReason.OtherFocusReason)
            elif hasattr(self, 'proofread_panel'):
                self.proofread_panel.edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def _kind_label(self, kind: str) -> str:
        return {'text': '段落', 'title': '标题', 'table': '表格', 'image': '图片'}.get(kind, '内容')

    def show_text_details(self):
        if not self.document_editor.current_target:
            return
        if self._details_dialog is None:
            from .workspace_ui import button, label
            dialog = QDialog(self)
            dialog.setWindowTitle('原始与候选')
            dialog.resize(580, 510)
            layout = QVBoxLayout(dialog)
            layout.setContentsMargins(24, 24, 24, 24)
            layout.setSpacing(14)
            self.details_title = label('', 'paneTitle')
            layout.addWidget(self.details_title)
            layout.addWidget(label('原始识别'))
            self.original_text_view = QPlainTextEdit()
            self.original_text_view.setAccessibleName('原始识别文本')
            self.original_text_view.setReadOnly(True)
            layout.addWidget(self.original_text_view, 1)
            self.candidate_picker = QComboBox()
            self.candidate_picker.setAccessibleName('识别候选文字')
            self.candidate_picker.currentIndexChanged.connect(self._candidate_selection_changed)
            layout.addWidget(self.candidate_picker)
            self.candidate_text_view = QPlainTextEdit()
            self.candidate_text_view.setReadOnly(True)
            self.candidate_text_view.setAccessibleName('候选完整文字')
            layout.addWidget(self.candidate_text_view, 1)
            actions = QHBoxLayout()
            self.rerecognize_button = button('局部重新识别', self._rerecognize_selected)
            self.apply_candidate_button = button('采用候选', self._apply_selected_candidate, 'primary')
            actions.addWidget(self.rerecognize_button)
            actions.addStretch()
            actions.addWidget(self.apply_candidate_button)
            layout.addLayout(actions)
            self._details_dialog = dialog
        self._details_key = self.document_editor.current_key
        self._refresh_text_details()
        self._details_dialog.open()

    def _refresh_text_details(self):
        target = self.document_editor.current_target
        if target is None:
            self._details_dialog.hide()
            return
        self.details_title.setText(self.context_label.text())
        self.original_text_view.setPlainText(target.original_text)
        self.candidate_picker.clear()
        self.candidate_picker.addItem('选择候选' if target.candidates else '暂无候选', None)
        for index, value in enumerate(target.candidates, 1):
            self.candidate_picker.addItem(f'{index}. ' + value.replace('\n', ' ')[:48], value)
        self.rerecognize_button.setEnabled(isinstance(target, Block) and target.kind in {'text', 'title'} and self._worker is None and self._models_ready)

    def _candidate_selection_changed(self, _index):
        if not hasattr(self, 'candidate_text_view'):
            return
        value = self.candidate_picker.currentData()
        self.candidate_text_view.setPlainText(value or '')
        self.apply_candidate_button.setEnabled(value is not None)

    def _apply_selected_candidate(self):
        value = self.candidate_picker.currentData()
        target = self.document_editor.current_target
        if value is not None and target and self.document_editor.replace_target(self._details_key, value):
            target.proofread_source = 'manual_candidate'
            target.needs_review = False
            self.review_changed(self._current_job_id)
            self._set_status('候选已采用，支持撤销。')
            self._details_dialog.accept()

    def _rerecognize_selected(self):
        target = self.document_editor.current_target
        if isinstance(target, Block):
            self.rerecognize_block(target)
            if hasattr(self, 'rerecognize_button'):
                self.rerecognize_button.setEnabled(False)
            self.proofread_panel.rerecognize.setEnabled(False)

    def _render_review_summary(self):
        from .review_dialog import build_review_dialog
        if self._review_dialog is None:
            build_review_dialog(self)
        self._populate_review_list()
        self._review_dialog.open()

    def _populate_review_list(self, _index=None):
        from .review_dialog import populate_review_list
        populate_review_list(self)

    def _open_review_selection(self, _item=None):
        item = self.review_list.currentItem()
        if item:
            job_id, block_id = item.data(Qt.ItemDataRole.UserRole)
            self._review_dialog.accept()
            self._locate_review_item(job_id, block_id)

    def _locate_review_item(self, job_id, block_id):
        job = next((j for j in self.jobs if j.id == job_id), None)
        if job:
            self.queue_list.setCurrentRow(self.jobs.index(job))
            self.show_document()
            self.compare_button.setChecked(True)
            self.toggle_comparison()
            self.select_block(block_id, focus_editor=True)

    def show_export_options(self):
        if self._worker or not any(j.result for j in self.jobs):
            return
        self.export_mode_picker.setCurrentIndex(self.export_layout_combo.currentIndex())
        self.export_mode_changed(self.export_mode_picker.currentIndex())
        self.export_count_label.setText(f'选择排版模式 · {sum(j.result is not None for j in self.jobs)} 页已识别完成')
        self.export_options.open()

    def export_mode_changed(self, index):
        self.export_layout_combo.setCurrentIndex(index)
        descriptions = {
            'source': '保持原稿的文字位置、换行和分页。',
            'flow': '文字连续排列，保留原稿换行和图片分页。',
            'paragraph': '保留段落与样式，由 Word 自然换行、分页。',
            'text_only': '提取连续文字，不保留图片、样式与换行。',
        }
        self.export_mode_note.setText(descriptions.get(self.export_layout_combo.currentData(), ''))
        for number, card in enumerate(self.export_mode_cards):
            card.setChecked(number == index)

    def export_with_options(self, separate):
        self.export_options.accept()
        QTimer.singleShot(0, lambda: self.request_export(separate=separate))

    @staticmethod
    def _page_signature(page: PageResult) -> tuple[object, ...]:
        return (
            page.id,
            page.page_index,
            page.width,
            page.height,
            page.processed_image_path,
            tuple(
                (
                    block.id,
                    block.kind,
                    block.bbox,
                    block.original_text,
                    block.edited_text,
                    block.confidence,
                    block.image_path,
                    block.needs_review,
                    tuple(
                        (
                            cell.row,
                            cell.col,
                            cell.original_text,
                            cell.edited_text,
                            cell.rowspan,
                            cell.colspan,
                            cell.confidence,
                        )
                        for cell in block.cells
                    ),
                )
                for block in page.blocks
            ),
        )

    def _has_unexported_results(self) -> bool:
        return any(
            job.result is not None and self._exported_signatures.get(job.id) != self._page_signature(job.result)
            for job in self.jobs
        )

    def _confirm_discard_results(self, jobs: list[ImageJob], *, title: str, message: str) -> bool:
        project = self.project_controller
        dirty = [job for job in jobs if job.result is not None
                 and self._exported_signatures.get(job.id) != self._page_signature(job.result)
                 and not project.has_saved(job)]
        if not dirty and not (jobs is self.jobs and project.dirty()):
            return True
        answer = QMessageBox.warning(
            self,
            title,
            message,
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Discard

    def _mark_pages_exported(self, pages: list[PageResult]) -> None:
        for page in pages:
            self._exported_signatures[page.id] = self._page_signature(page)

    def _has_user_edits(self, job: ImageJob) -> bool:
        if not job.result:
            return False
        for block in job.result.blocks:
            if block.edited_text is not None:
                return True
            if any(cell.edited_text is not None for cell in block.cells):
                return True
        return False

    def _confirm_invalidation(self) -> bool:
        job = self.current_job
        if not job or not job.result or not self._has_user_edits(job):
            return True
        answer = QMessageBox.warning(
            self,
            "确认重新预处理",
            "预处理会清除当前页的识别结果和手动修改。是否继续？",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Discard

    def _invalidate_result(self) -> None:
        job = self.current_job
        if not job:
            return
        job.result = None
        self._exported_signatures.pop(job.id, None)
        if job.status not in {"failed", "processing"}:
            job.status = "pending"
        self._prepared_paths.pop(job.id, None)
        self._selected_block_id = None
        self._refresh_current_page()
        self._rebuild_queue()

    def _change_mode(self) -> None:
        job = self.current_job
        if not job or self._worker:
            return
        mode = str(self.mode_combo.currentData())
        if job.mode == mode:
            return
        if not self._confirm_invalidation():
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(1 if job.mode == "photo" else 0)
            self.mode_combo.blockSignals(False)
            return
        job.mode = mode
        if job.crop is None and job.perspective is None:
            job.paper_quad = None
            job.paper_status = 'not_run'
            job.paper_confidence = 0.0
        self._invalidate_result()

    def _toggle_crop(self) -> None:
        enabled = self.crop_button.isChecked()
        self.perspective_button.setChecked(False)
        self.source_preview.set_interaction_mode("crop" if enabled else "idle")
        self.preview_hint_label.setText("拖动框选裁剪区域。" if enabled else "滚轮缩放 · 拖动查看")

    def _toggle_perspective(self) -> None:
        enabled = self.perspective_button.isChecked()
        self.crop_button.setChecked(False)
        self.source_preview.set_interaction_mode("perspective" if enabled else "idle")
        self.preview_hint_label.setText("依次选择左上、右上、右下、左下四点。" if enabled else "滚轮缩放 · 拖动查看")

    def apply_crop(self, crop: tuple[float, float, float, float]) -> bool:
        job = self.current_job
        if not job or self._worker or not self._confirm_invalidation():
            return False
        job.crop = crop
        job.paper_status = 'manual'
        job.paper_quad = None
        self.crop_button.setChecked(False)
        self.source_preview.set_interaction_mode("idle")
        self._invalidate_result()
        self._set_status("已更新裁剪区域；请重新识别该页。")
        return True

    def apply_perspective(self, points: list[tuple[float, float]]) -> bool:
        job = self.current_job
        if not job or self._worker or len(points) != 4 or not self._confirm_invalidation():
            return False
        from .images import _canonical_quad, _load_oriented_rgb
        try:
            with _load_oriented_rgb(job) as image:
                _canonical_quad(points, image.width, image.height)
        except Exception:
            self._set_status('四点未构成有效纸面，请重新选择。', warning=True)
            return False
        job.perspective = points
        job.paper_status = 'manual'
        job.paper_quad = None
        self.perspective_button.setChecked(False)
        self.source_preview.set_interaction_mode("idle")
        self._invalidate_result()
        self._set_status("已更新透视校正；请重新识别该页。")
        return True

    def rotate_current(self, delta: int) -> None:
        job = self.current_job
        if not job or self._worker or not self._confirm_invalidation():
            return
        job.rotation = (int(job.rotation) + delta) % 360
        self._invalidate_result()
        self._set_status("已更新旋转角度；请重新识别该页。")

    def reset_preprocessing(self) -> None:
        job = self.current_job
        if not job or self._worker or (job.crop is None and job.perspective is None and not job.rotation and job.paper_status == 'not_run'):
            return
        if not self._confirm_invalidation():
            return
        job.crop = None
        job.perspective = None
        job.rotation = 0
        job.paper_quad = None
        job.paper_status = 'not_run'
        job.paper_confidence = 0.0
        self._invalidate_result()
        self._set_status("已重置预处理；请重新识别该页。")

    def start_processing(self) -> None:
        if self._worker or self._active_file_browser or self.project_controller.busy or self.export_controller.busy:
            return
        if not self._models_ready:
            self._set_status(f"模型不可用（{self._model_code}），无法开始识别。", warning=True)
            return
        restore_layout = self.export_layout_combo.currentData() != "text_only"
        rebuild = [job for job in self.jobs if job.result is not None and job.result.text_only and restore_layout]
        if rebuild and not self._confirm_discard_results(
            rebuild, title="重新识别排版", message="重新识别将替换这些页面的纯文字结果及校对修改。是否继续？"
        ):
            return
        jobs = [job for job in self.jobs if job.status in {"pending", "failed", "cancelled"} or job in rebuild]
        if not jobs:
            self._set_status("没有待处理页面。")
            return
        for job in jobs:
            job.status = "pending"
            job.error_code = None
            self.recognition_controls.apply_to_job(job)
        try:
            self._batch_job_ids = {job.id for job in jobs}
            self._worker = self._new_batch_worker(jobs)
            self._worker.start(jobs)
        except Exception as error:
            self._worker = None
            code = _safe_error_code(error)
            for job in jobs:
                job.status = "failed"
                job.error_code = code
            self._sync_queue_items()
            self._set_status(f"无法启动离线识别（{code}）。", warning=True)
            return
        self._set_status(f"正在离线识别 {len(jobs)} 页，可随时取消。")
        if self.profile_combo.currentData() == 'parallel':
            actual = self._worker.lane_count
            self.profile_badge.setText('双任务并行' if actual == 2 else '并行模式 · 当前单任务')
            self._set_status('正在同时识别两张图片。' if actual == 2 else
                             '只有一张待处理图片，当前使用单任务。' if len(jobs) == 1 else
                             '可用内存或 CPU 资源不足，当前使用单任务，识别质量不变。')
        self._poll_timer.start()
        self._sync_queue_items()
        self._refresh_action_states()

    def cancel_processing(self) -> None:
        if not self._worker:
            return
        try:
            self._worker.cancel()
        except Exception:
            self._finish_worker(cancelled=True)
        else:
            self._set_status("正在取消当前识别任务…")

    def retry_failed(self) -> None:
        if self._worker:
            return
        failed = [job for job in self.jobs if job.status in {"failed", "cancelled"}]
        if not failed:
            self._set_status("没有需要重试的失败页面。")
            return
        for job in failed:
            job.status = "pending"
            job.error_code = None
        self._sync_queue_items()
        self.start_processing()

    def _poll_worker(self) -> None:
        worker = self._worker
        if not worker:
            self._poll_timer.stop()
            return
        try:
            events = worker.poll()
        except Exception:
            self._finish_worker(cancelled=False, failed=True)
            return
        for event in events:
            if self._handle_worker_event(event):
                return
        if self._worker is worker and not worker.busy:
            self._finish_worker(cancelled=False, failed=True)
            return
        if events:
            self._sync_queue_items()

    def _handle_worker_event(self, event: dict) -> bool:
        """Apply one worker-pipe event and report whether processing has ended."""
        kind = event.get("type") or event.get("event")
        job_id = event.get("job_id")
        job = next((candidate for candidate in self.jobs if candidate.id == job_id), None)
        if kind == 'diagnostic':
            if self.diagnostics_checkbox.isChecked():
                self.recognition_controls.diagnostics.record(event)
        elif kind in {'engine_stage', 'page_stage'}:
            stage = {'model_validate': '正在校验模型', 'module_import': '正在加载识别组件',
                     'model_create': '正在加载模型', 'prepare': '正在准备图片',
                     'recognize': '正在识别文字', 'integrity': '正在检查漏行'}.get(event.get('stage'))
            if stage:
                self._set_status(stage + '…')
        elif kind == "page_started" and job:
            job.status = "processing"
            self._queued_during_run.discard(job.id)
            self._refresh_action_states()
        elif kind == "queue_removed":
            self._remove_jobs_now(set(event.get("job_ids", [])))
            self._set_status("已移除等待中的任务，原文件保留。")
        elif kind == "queue_remove_rejected":
            self._removal_requests.difference_update(event.get("job_ids", []))
            self._set_status("所选任务已经开始识别，已保留该页。")
            self._refresh_action_states()
        elif kind == "page_progress":
            pass_index = int(event.get("pass_index") or 0)
            pass_count = int(event.get("pass_count") or 0)
            if pass_count > 1:
                self.proofread_progress_badge.setText(f"三次识别 第 {pass_index}/{pass_count} 次")
                self.proofread_progress_badge.setVisible(True)
            self._set_status(f"正在辅助校对：第 {pass_index}/{pass_count} 次识别。")
        elif kind == "page_integrity_progress":
            self.proofread_progress_badge.setText("文字完整性检查")
            self.proofread_progress_badge.setVisible(True)
            self._set_status("正在检查版面漏行…")
        elif kind == "page_detail_progress":
            region_index = int(event.get("region_index") or 0)
            region_count = int(event.get("region_count") or 0)
            pass_index = int(event.get("pass_index") or 0)
            pass_count = int(event.get("pass_count") or 0)
            self.proofread_progress_badge.setText(
                f"原图细查 {region_index}/{region_count} · {pass_index}/{pass_count}"
            )
            self.proofread_progress_badge.setVisible(True)
            self._set_status(
                f"正在原图细查：第 {region_index}/{region_count} 处，第 {pass_index}/{pass_count} 次。"
            )
        elif kind == "page_done" and job:
            result = event.get("result")
            if isinstance(result, PageResult):
                job.result = result
                if job.paper_status in {'not_run', 'detecting'}:
                    job.paper_quad, job.paper_status = result.source_paper_quad, result.paper_status
                    job.paper_confidence = result.paper_confidence
                self._exported_signatures.pop(job.id, None)
                job.status = "done"
                job.error_code = None
                self._prepared_paths[job.id] = Path(result.processed_image_path)
                if job.id == self._current_job_id:
                    self._refresh_current_page()
        elif kind == "page_error" and job:
            code = str(event.get("code") or "OCR_FAILED")
            job.status = "failed"
            job.error_code = code
        elif kind == "region_error":
            self._apply_region_error(job_id, str(event.get("code") or "OCR_FAILED"))
        elif kind == "region_done":
            self._apply_region_candidates(job_id, list(event.get("candidates") or []))
        elif kind == "cancelled":
            self._finish_worker(cancelled=True)
            return True
        elif kind == "finished":
            self._finish_worker(cancelled=False)
            return True
        return False

    def _apply_region_candidates(self, job_id: object, candidates: list[str]) -> None:
        if not self._pending_region or self._pending_region[0] != job_id:
            return
        job = next((candidate for candidate in self.jobs if candidate.id == job_id), None)
        if not job or not job.result:
            return
        block = next((candidate for candidate in job.result.blocks if candidate.id == self._pending_region[1]), None)
        if block is None:
            return
        block.candidates = candidates
        block.needs_review = bool(candidates)
        self.review_changed(job.id)
        self._pending_region = None
        if job.id == self._current_job_id:
            self._render_editors(job.result)
        self._set_status("局部识别完成。选择候选或编辑文字。")

    def _apply_region_error(self, job_id: object, code: str) -> None:
        if not self._pending_region or self._pending_region[0] != job_id:
            return
        job = next((candidate for candidate in self.jobs if candidate.id == job_id), None)
        if not job or not job.result:
            return
        self._pending_region = None
        self._local_region_error = f"局部识别失败（{code}）；当前结果与修改已保留。"

    def rerecognize_block(self, block: Block) -> None:
        job = self.current_job
        if not job or not job.result:
            return
        if not self._models_ready:
            self._set_status(f"模型不可用（{self._model_code}），无法局部识别。", warning=True)
            return
        if self._worker and self._worker.busy:
            self._set_status("识别任务进行中，完成后可局部识别。", warning=True)
            return
        try:
            if not self._worker:
                self._worker = self._new_region_worker(job)
            self._local_region_error = None
            self._pending_region = (job.id, block.id)
            self._worker.start_region(job, block.bbox)
            self._poll_timer.start()
            self._refresh_action_states()
            self._set_status("正在局部重新识别…")
        except Exception as error:
            self._worker = None
            self._pending_region = None
            self._set_status(f"无法启动局部识别（{_safe_error_code(error)}）。", warning=True)

    def _finish_worker(self, *, cancelled: bool, failed: bool = False) -> None:
        worker, self._worker = self._worker, None
        self._poll_timer.stop()
        self.proofread_progress_badge.setVisible(False)
        if worker:
            try:
                worker.close()
            except Exception:
                pass
        removable = {job.id for job in self.jobs if job.id in self._removal_requests
                     and job.result is None and job.status in {"pending", "cancelled"}}
        self._remove_jobs_now(removable)
        self._removal_requests.clear()
        pending_region = self._pending_region
        if cancelled:
            self._continue_after_batch = False
            self._queued_during_run.clear()
            self._pending_region = None
            for job in self.jobs:
                if job.status == "processing":
                    job.status = "cancelled"
            self._set_status(
                "局部识别已取消；当前结果与修改已保留。"
                if pending_region
                else "识别已取消；未完成页面可重试。",
                warning=True,
            )
        elif failed:
            self._continue_after_batch = False
            self._queued_during_run.clear()
            self._pending_region = None
            for job in self.jobs:
                if job.status in {"pending", "processing"} and (not self._batch_job_ids or job.id in self._batch_job_ids):
                    job.status = "failed"
                    job.error_code = "worker_process_failed"
            self._set_status(
                "局部识别异常结束；已保留当前页的识别结果和修改（错误代码：worker_process_failed）。"
                if pending_region
                else "识别进程异常结束；失败页面可重试（错误代码：worker_process_failed）。",
                warning=True,
            )
        elif self._local_region_error:
            self._set_status(self._local_region_error, warning=True)
            self._local_region_error = None
        else:
            completed = sum(1 for job in self.jobs if job.result is not None)
            failed_count = sum(1 for job in self.jobs if job.status == "failed")
            failure_codes = sorted({
                str(job.error_code)
                for job in self.jobs
                if job.status == "failed" and job.error_code
            })
            failure_suffix = f"（错误代码：{', '.join(failure_codes)}）" if failure_codes else ""
            fallback_count = sum(
                1
                for job in self.jobs
                if job.result is not None and job.result.recognition_warning == "layout_fallback"
            )
            fallback_suffix = (
                f"其中 {fallback_count} 页已使用普通文字识别，版面需核对。"
                if fallback_count
                else ""
            )
            recovered_count = sum(
                1
                for job in self.jobs
                if job.result is not None and job.result.recognition_warning == "text_recovery"
            )
            recovery_suffix = (
                f"其中 {recovered_count} 页经完整性检查恢复了漏行，排版需核对。"
                if recovered_count
                else ""
            )
            self._set_status(
                f"识别完成：{completed} 页可导出。{fallback_suffix}{recovery_suffix}"
                if not failed_count
                else f"识别完成：{completed} 页可导出，{failed_count} 页失败。{failure_suffix}{fallback_suffix}{recovery_suffix}",
                warning=bool(failed_count or fallback_count or recovered_count),
            )
        if self._review_dialog and self._review_dialog.isVisible():
            self._populate_review_list()
        if self._details_dialog and self._details_dialog.isVisible():
            self._refresh_text_details()
        self._sync_queue_items()
        self._refresh_action_states()
        self._batch_job_ids.clear()
        self._continue_added_jobs()

    def _warm_cache(self):
        if self._engine_cache is None:
            from .warm_worker import WarmWorkerCache
            self._engine_cache = WarmWorkerCache(self.model_root,self.session_dir)
        return self._engine_cache

    def _new_batch_worker(self,jobs):
        return self._warm_cache().batch(jobs,parallel=self.profile_combo.currentData()=='parallel')

    def _new_region_worker(self,job):
        return self._warm_cache().region(job)

    def _expire_idle_workers(self):
        if self._engine_cache is not None:
            self._engine_cache.expire()

    def request_export(self, *, separate: bool) -> None:
        pages = [job.result for job in self.jobs if job.result is not None]
        if not pages:
            self._set_status("暂无已识别页面，无法导出 Word。", warning=True)
            return
        if self.export_layout_combo.currentData() != "text_only" and any(page.text_only for page in pages):
            self._set_status("当前结果仅含文字。还原排版须重新识别。", warning=True)
            return
        incomplete = [job for job in self.jobs if job.result is None]
        if incomplete:
            answer = QMessageBox.question(
                self,
                "存在未完成页面",
                f"{len(incomplete)} 页未完成。是否仅导出已完成的 {len(pages)} 页？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        from .export_checks import inspect_page
        check_pages = pages
        if self.remove_page_numbers_checkbox.isChecked():
            from .page_numbers import without_page_numbers
            check_pages, _ = without_page_numbers(pages)
        confirmed = {(item.job_id, item.block.id) for item in self.review_items(include_confirmed=True)
                     if item.confirmed}
        issues = [issue for page in check_pages for issue in inspect_page(page,
                  layout_mode=self.export_layout_combo.currentData())
                  if not all((issue.page_id, bid) in confirmed for bid in issue.block_ids)]
        if issues:
            from .export_check_dialog import ExportCheckDialog
            self._export_check_dialog = ExportCheckDialog(self, issues, lambda: self._export(pages, separate))
            self._export_check_dialog.open()
        else:
            self._export(pages, separate)

    def _export(self, pages: list[PageResult], separate: bool) -> None:
        if self._active_file_browser is not None or self._worker is not None:
            return
        if self.export_controller.busy:
            return
        if separate:
            dialog = self._dialog("选择 Word 导出文件夹", LocalFileMode.DIRECTORY, accept_label="选择")
        else:
            dialog = self._dialog("导出合并文档", LocalFileMode.SAVE_FILE, accept_label="保存")
            dialog.setDefaultSuffix("docx")
            dialog.setNameFilter("Word 文档 (*.docx)")
            dialog.selectFile("识别结果.docx")
        self._open_file_browser(dialog, lambda paths: self.export_controller.start(pages, paths[0], separate=separate))

    def _refresh_action_states(self) -> None:
        busy = self._worker is not None
        self.motion.refresh()
        if busy and self.drawer.animation.state() == self.drawer.animation.State.Running:
            self.drawer.animation.stop()
            self.drawer.setGeometry(self.drawer.desired_geometry())
        dialog_open = self._active_file_browser is not None or self.project_controller.busy or self.export_controller.busy
        blocked = busy or dialog_open
        current = self.current_job is not None
        self.start_button.setEnabled(self._models_ready and bool(self.jobs) and not blocked)
        self.auto_proofread_checkbox.setEnabled(not blocked)
        self.profile_combo.setEnabled(not blocked)
        self.diagnostics_checkbox.setEnabled(not blocked)
        self.cancel_button.setEnabled(busy)
        self.cancel_button.setVisible(busy)
        self.retry_button.setEnabled(not blocked and any(job.status in {"failed", "cancelled"} for job in self.jobs))
        self.retry_button.setVisible(any(job.status in {"failed", "cancelled"} for job in self.jobs))
        for widget in (
            self.import_button,
            self.import_folder_button,
            self.mode_combo,
            self.rotate_left_button,
            self.rotate_right_button,
            self.crop_button,
            self.perspective_button,
            self.reset_button,
            self.move_up_button,
            self.move_down_button,
        ):
            widget.setEnabled(current and not blocked if widget not in {self.import_button, self.import_folder_button} else not dialog_open)
        selected = {str(item.data(Qt.ItemDataRole.UserRole)) for item in self.queue_list.selectedItems()}
        self.remove_button.setEnabled(not dialog_open and any(
            job.id in selected and job.status != "processing" and job.id not in self._removal_requests
            and (not self._pending_region or job.id != self._pending_region[0]) for job in self.jobs))
        can_export = bool([job for job in self.jobs if job.result]) and not blocked
        self.export_merge_button.setEnabled(can_export)
        self.export_separate_button.setEnabled(can_export)
        self.export_layout_combo.setEnabled(not blocked)
        self.remove_page_numbers_checkbox.setEnabled(not blocked)
        self.export_button.setEnabled(can_export)
        self.export_navigation_button.setEnabled(can_export)
        self.export_preview_button.setEnabled(can_export)
        self.export_confirm_button.setEnabled(can_export)
        self.preview_export_button.setEnabled(can_export)
        self.command_preview_button.setEnabled(can_export)
        self.command_preview_button.setVisible(any(j.result for j in self.jobs))
        self.empty_import_button.setEnabled(not dialog_open)
        self.import_more_button.setEnabled(not dialog_open)
        self.document_button.setEnabled(any(job.result for job in self.jobs))
        self.review_summary_button.setEnabled(any(job.result for job in self.jobs))
        self.review_summary_button.setText(f'待复核 {len(self.review_items())}')
        self.selection_label.setText(f'已选 {len(selected)} 张' if selected else 'Ctrl 多选')
        self.selection_label.setToolTip('双击图片打开 · Ctrl / Shift 多选 · Delete 移除')
        for control in (self.move_up_button, self.move_down_button, self.remove_button):
            control.setVisible(bool(selected))
        self.start_button.setVisible(not busy)
        self.move_up_button.setEnabled(not blocked and len(selected) == 1 and self.current_job is not None and self.jobs.index(self.current_job) > 0)
        self.move_down_button.setEnabled(not blocked and len(selected) == 1 and self.current_job is not None and self.jobs.index(self.current_job) < len(self.jobs)-1)
        pending = any(job.status in {'pending', 'failed', 'cancelled'} or (job.result and job.result.text_only and self.export_layout_combo.currentData() != 'text_only') for job in self.jobs)
        self.start_button.setEnabled(self._models_ready and pending and not blocked)
        self.motion.refresh()
        self._sync_shell()
        if hasattr(self, 'recognition_controls'):
            self.recognition_controls.refresh_paper()

    def _sync_shell(self):
        """Update small presentation fields without rebuilding editors or scenes."""
        if not hasattr(self, 'right_stack'):
            return
        current_view = self.views.currentWidget()
        previewing = current_view is self.preview_view
        proofreading = current_view is self.document_view
        (self.export_navigation_button if previewing else self.document_button if proofreading else self.gallery_button).setChecked(True)
        self.left_stack.setCurrentWidget(self.preview_sidebar if previewing else self.queue_sidebar)
        self.right_stack.setCurrentWidget(self.preview_info if previewing else self.proofread_panel if proofreading else self.recognition_panel)
        self.queue_title.setText('页面' if proofreading else '图片')
        active = next((job for job in self.jobs if job.status == 'processing'), None)
        self.header_context.setText(f'{len(self.jobs)} 张图片' if self.jobs else '暂无任务')
        self.workspace_title.setText('Word 预览' if previewing else '文档校对' if proofreading else '文档工作区')
        current=self.current_job
        self.workspace_meta.setText(f'第 {self.jobs.index(current)+1} / {len(self.jobs)} 页' if current else '添加待识别图片')
        if current:
            self.workspace_title.setToolTip(current.display_name or Path(current.source_path).name)
        self.recognition_heading.setText('识别任务' if self._worker else '识别设置')
        self.task_card.setVisible(bool(self.jobs))
        self.left_stack.setVisible(bool(self.jobs))
        self.export_layout_combo.setVisible(bool(self.jobs))
        self.task_title.setText('正在识别' if active else '任务完成' if self.jobs and all(j.status == 'done' for j in self.jobs) else '等待识别')
        self.task_card.setToolTip(self.task_stage.text())
        self.source_stack.setCurrentWidget(self.source_preview if self.current_job else self.empty_stage)
        self.image_tools.setVisible(self.current_job is not None)
        self.image_footer.setVisible(self.current_job is not None)
        self.preview_hint_label.setVisible(self.current_job is not None)
        self.export_button.setVisible(any(j.result for j in self.jobs))
        self.start_button.setVisible(self._worker is None and any(j.status != 'done' for j in self.jobs))
        self.selection_bar.setVisible(bool(self.jobs))
        for card in self.mode_cards:
            card.setEnabled(self._worker is None and self._active_file_browser is None)
        if proofreading:
            self.proofread_panel.refresh()
        self.project_controller.refresh()

    def close_context_panel(self):
        if hasattr(self,'drawer'):
            self.drawer.close_panel()
            self.details_button.setChecked(False)

    def dismiss_or_cancel(self):
        if self.drawer.isVisible():
            self.close_context_panel()
        else:
            self.cancel_processing()

    def show_recognition_settings(self):
        self.motion.stop()
        if self.views.currentWidget() is not self.gallery_view:
            self.show_gallery()
            self.motion.stop()
        self.drawer_title.setText('识别选项')
        self.right_stack.setCurrentWidget(self.recognition_panel)
        self.drawer.open_panel()

    def show_proofreading(self, *, toggle=True):
        if self.views.currentWidget() is not self.document_view:
            return
        if toggle and self.drawer.isVisible() and self.right_stack.currentWidget() is self.proofread_panel:
            self.close_context_panel()
            return
        self.motion.stop()
        self.drawer_title.setText('校对建议')
        self.right_stack.setCurrentWidget(self.proofread_panel)
        self.proofread_panel.refresh()
        self.drawer.open_panel()
        self.details_button.setChecked(True)

    def show_preview_information(self):
        self.drawer_title.setText('导出信息')
        self.right_stack.setCurrentWidget(self.preview_info)
        self.drawer.open_panel()

    def _document_edited(self):
        self.review_changed(self._current_job_id)

    def review_items(self, **options):
        return self.review_index.collect(self.jobs, **options)

    def review_changed(self, job_id):
        self.review_index.invalidate(job_id)
        self._review_dirty.add(job_id)
        self._review_timer.start(0)

    def _flush_review_changes(self):
        dirty, self._review_dirty = self._review_dirty, set()
        for index, job in enumerate(self.jobs):
            if job.id in dirty and index < self.queue_list.count():
                self._update_queue_item(self.queue_list.item(index), index, job)
        self.review_summary_button.setText(f'待复核 {len(self.review_items())}')
        self.proofread_panel.refresh()
        if self._review_dialog and self._review_dialog.isVisible():
            self._populate_review_list()

    def choose_image_mode(self, index):
        if self._worker or self._active_file_browser:
            return
        self.mode_combo.setCurrentIndex(index)
        if self.current_job:
            index = 1 if self.current_job.mode == 'photo' else 0
        self._default_image_mode = 'photo' if index else 'scan'
        self.mode_cards[index].setChecked(True)

    def toggle_theme(self):
        from .theme import set_appearance
        set_appearance(self, 'dark' if self.theme_button.isChecked() else 'light')

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'left_stack'):
            self.left_stack.setFixedWidth(88 if self.width() < 1100 else 104 if self.width() < 1280 else 112)
            self.preview_page_list.setGridSize(QSize(84,124))
            compact = self.width() < 960 and bool(self.jobs)
            self.workspace_title.setVisible(not compact)
            self.workspace_meta.setVisible(not compact)
            self.preview_hint_label.setVisible(not compact and self.current_job is not None)

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls()
        if urls and all(url.isLocalFile() for url in urls) and self._active_file_browser is None:
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls and all(url.isLocalFile() for url in urls) and self._active_file_browser is None:
            self._import_paths([url.toLocalFile() for url in urls])
            event.acceptProposedAction()

    def show_export_preview(self):
        if self._worker or self._active_file_browser or self.export_controller.busy:
            return
        pages = [job.result for job in self.jobs if job.result]
        if pages:
            try:
                self.export_controller.start(pages, separate=self.separate_output_radio.isChecked(), preview=True)
            except Exception as error:
                self._set_status(f'预览无法开始（{_safe_error_code(error)}）。', warning=True)

    def _add_preview_thumbnail(self, paper):
        image = QImage(132, 186, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(132/paper.size.width(),186/paper.size.height())
        paper.paint(painter,None)
        painter.end()
        thumbnail = QPixmap.fromImage(image)
        thumbnail_icon = QIcon(thumbnail)
        for icon_mode in (QIcon.Mode.Active, QIcon.Mode.Selected):
            thumbnail_icon.addPixmap(thumbnail, icon_mode)
        self.preview_page_list.addItem(QListWidgetItem(thumbnail_icon,f'第 {self.preview_page_list.count()+1} 页'))

    def _present_export_preview(self, task):
        for previous in self._preview_paths:
            previous.unlink(missing_ok=True)
        self._preview_paths = task.paths
        self._preview_path = task.paths[0]
        self.export_options.accept()
        self.close_context_panel()
        self.views.setCurrentWidget(self.preview_view)
        title = self.export_layout_combo.itemText(self.export_layout_combo.findData(task.mode))
        note = ('去除页码 · ' if task.remove_numbers else '保留页码 · ') + ('逐图分别导出' if task.separate else '合并导出')
        self.preview_mode_label.setText(title)
        self.preview_options_label.setText(note)
        self.preview_left_mode.setText(title)
        self.preview_left_options.setText(note)
        documents = task.packages
        self.preview_stats.setText(f'原图  {len(task.pages)} 张\n\n文字  {sum(bool(p.text.strip()) for doc in documents for p in doc.paragraphs)} 段\n\n表格  {sum(len(doc.tables) for doc in documents)} 个\n\n插图  {sum(len(doc.inline_shapes) for doc in documents)} 张')
        self.preview_page_list.setCurrentRow(0)
        self._sync_shell()
        QTimer.singleShot(0,lambda:self.preview_canvas.fit_page('page'))

    def _preview_page_changed(self,index,count):
        self.preview_page_label.setText(f'第 {index+1} / {count} 页')
        self.preview_previous.setEnabled(index>0)
        self.preview_next.setEnabled(index<count-1)
        if hasattr(self,'preview_page_list'):
            self.preview_page_list.blockSignals(True)
            self.preview_page_list.setCurrentRow(index)
            self.preview_page_list.blockSignals(False)

    def apply_page_result(self, job_id: str, result: PageResult) -> None:
        """Apply a completed worker result; useful for the worker event boundary and Qt tests."""
        job = next((candidate for candidate in self.jobs if candidate.id == job_id), None)
        if not job:
            return
        job.result = result
        self._exported_signatures.pop(job.id, None)
        job.status = "done"
        job.error_code = None
        self._prepared_paths[job.id] = Path(result.processed_image_path)
        self._sync_queue_items()
        if job.id == self._current_job_id:
            self._refresh_current_page()
        self._refresh_action_states()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._model_task is not None:
            self._close_after_model_check = True
            event.ignore()
            return
        if self.export_controller.busy:
            self.export_controller.cancel()
            self._set_status("正在取消文档生成，完成后可退出。")
            event.ignore()
            return
        if self.project_controller.busy:
            event.ignore()
            return
        if not self._confirm_discard_results(
            self.jobs,
            title="确认退出",
            message="仍有尚未保存的识别结果、项目设置或修改。是否放弃并退出？",
        ):
            event.ignore()
            return
        self._poll_timer.stop()
        self._cache_timer.stop()
        self.motion.stop()
        self.drawer.close_panel()
        if self._worker:
            try:
                self._worker.cancel()
                self._worker.close()
            except Exception:
                pass
            self._worker = None
        if self._engine_cache is not None:
            self._engine_cache.close()
        event.accept()
