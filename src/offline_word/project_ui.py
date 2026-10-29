"""Explicit project operations; the password lives only in the active operation."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, Slot
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QLabel,
                             QLineEdit, QProgressBar, QVBoxLayout)

from .file_browser import LocalFileMode
from .project_store import ProjectSnapshot, load_project, save_project


def job_signature(job):
    return hashlib.sha256(json.dumps(asdict(job), ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


class PasswordDialog(QDialog):
    def __init__(self, owner, *, saving):
        super().__init__(owner)
        self.setPalette(owner.palette())
        self.setWindowTitle('保存加密项目' if saving else '打开加密项目')
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)
        title = QLabel('设置项目口令' if saving else '输入项目口令')
        title.setObjectName('paneTitle')
        layout.addWidget(title)
        note = QLabel('保存图片与校对结果。口令遗失无法恢复。'
                      if saving else '口令仅用于本次解密，不予保存。')
        note.setObjectName('caption')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setMaxLength(1024)
        self.password.setPlaceholderText('至少 10 个字符')
        self.password.setAccessibleName('项目口令')
        layout.addWidget(self.password)
        self.confirmation = QLineEdit()
        self.confirmation.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirmation.setMaxLength(1024)
        self.confirmation.setPlaceholderText('再次输入口令')
        self.confirmation.setAccessibleName('确认项目口令')
        if saving:
            layout.addWidget(self.confirmation)
        else:
            self.confirmation.hide()
        show = QCheckBox('显示口令')
        show.toggled.connect(lambda value: [control.setEchoMode(QLineEdit.EchoMode.Normal
            if value else QLineEdit.EchoMode.Password) for control in (self.password, self.confirmation)])
        layout.addWidget(show)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText('加密保存' if saving else '解密打开')
        ok.setObjectName('primary')
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        def refresh():
            ok.setEnabled(len(self.password.text()) >= 10 and
                          (not saving or self.password.text() == self.confirmation.text()))
        self.password.textChanged.connect(refresh)
        self.confirmation.textChanged.connect(refresh)
        refresh()

    def take_password(self):
        value = self.password.text()
        self.password.clear()
        self.confirmation.clear()
        return value


class ProjectOperation(QThread):
    def __init__(self, owner, path, password, snapshot=None):
        super().__init__(owner)
        self.path, self.password, self.snapshot = path, password, snapshot
        self.session_dir = owner.window.session_dir
        self.result, self.error = None, None

    def run(self):
        try:
            self.result = (save_project(self.snapshot, self.path, self.password) if self.snapshot
                           else load_project(self.path, self.password, self.session_dir))
        except Exception as error:
            self.error = getattr(error, 'code', 'project_io_failed')
        finally:
            self.password = None


class ProgressDialog(QDialog):
    def __init__(self, owner, text):
        super().__init__(owner)
        self.setPalette(owner.palette())
        self.setWindowTitle(text)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        self.setFixedWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.addWidget(QLabel(text))
        progress = QProgressBar()
        progress.setRange(0, 0)
        layout.addWidget(progress)

    def reject(self):
        # Closing the session while the background writer owns images is unsafe.
        pass


class ProjectController(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.operation = None
        self.progress = None
        self.path = None
        self.saved_jobs = {}
        self.saved_order = []
        self.saved_settings = None

    @property
    def busy(self):
        return self.operation is not None

    def settings(self):
        w = self.window
        return {'layout_mode': w.export_layout_combo.currentData(),
                'remove_page_numbers': w.remove_page_numbers_checkbox.isChecked(),
                'auto_proofread': w.auto_proofread_checkbox.isChecked(),
                'performance_profile': w.profile_combo.currentData()}

    def has_saved(self, job):
        return self.saved_jobs.get(job.id) == job_signature(job)

    def dirty(self):
        return self.path is not None and (self.saved_order != [job.id for job in self.window.jobs]
            or self.saved_settings != self.settings() or any(not self.has_saved(job) for job in self.window.jobs))

    def choose(self, *, saving):
        w = self.window
        if self.busy or w._worker or w._active_file_browser or (saving and not w.jobs):
            return
        dialog = w._dialog('保存加密项目' if saving else '打开加密项目',
                           LocalFileMode.SAVE_FILE if saving else LocalFileMode.EXISTING_FILES)
        dialog.setNameFilter('加密 OCR 项目 (*.ocrproj)')
        if saving:
            dialog.setDefaultSuffix('ocrproj')
            dialog.selectFile(str(self.path) if self.path else '识别项目.ocrproj')
        def selected(paths):
            if len(paths) != 1:
                w._set_status('每次仅可打开一个项目。', warning=True)
                return
            if not saving and not w._confirm_discard_results(w.jobs, title='打开项目',
                    message='当前结果尚未保存。打开项目将替换当前任务，是否继续？'):
                return
            password_dialog = PasswordDialog(w, saving=saving)
            if password_dialog.exec() == QDialog.DialogCode.Accepted:
                password = password_dialog.take_password()
                self.start(Path(paths[0]), password, saving=saving)
            password_dialog.take_password()
            password_dialog.deleteLater()
        w._open_file_browser(dialog, selected)

    def start(self, path, password, *, saving):
        if self.busy or self.window._worker:
            return
        w = self.window
        if w._engine_cache:
            w._engine_cache.clear_idle()
        snapshot = ProjectSnapshot(deepcopy(w.jobs), self.settings()) if saving else None
        self.operation = ProjectOperation(self, path, password, snapshot)
        self.progress = ProgressDialog(w, '正在加密保存…' if saving else '正在验证并打开…')
        self.operation.finished.connect(self._finished)
        self.progress.open()
        w._refresh_action_states()
        self.operation.start()

    @Slot()
    def _finished(self):
        operation = self.operation
        self.operation = None
        self.progress.accept()
        self.progress.deleteLater()
        self.progress = None
        w = self.window
        if operation.error:
            message = ('口令错误或项目已损坏，当前任务已保留。'
                       if operation.error in {'project_password_or_integrity', 'invalid_project_file'}
                       else f'项目操作失败（{operation.error}），当前任务已保留。')
            w._set_status(message, warning=True)
        else:
            self.path = operation.path
            if operation.snapshot is None:
                snapshot = operation.result
                w._exported_signatures.clear()
                w._prepared_paths.clear()
                w._batch_job_ids.clear()
                w._queued_during_run.clear()
                w._removal_requests.clear()
                w._continue_after_batch = False
                w.set_jobs(snapshot.jobs)
                settings = snapshot.settings
                w.export_layout_combo.setCurrentIndex(w.export_layout_combo.findData(settings['layout_mode']))
                w.remove_page_numbers_checkbox.setChecked(settings['remove_page_numbers'])
                w.auto_proofread_checkbox.setChecked(settings['auto_proofread'])
                combo = w.profile_combo
                combo.setCurrentIndex(combo.findData(settings['performance_profile']))
                w._prepared_paths.update({job.id: Path(job.result.processed_image_path)
                                          for job in w.jobs if job.result})
                w._refresh_current_page()
            self.saved_jobs = {job.id: job_signature(job) for job in w.jobs}
            self.saved_order = [job.id for job in w.jobs]
            self.saved_settings = self.settings()
            w._set_status('项目已加密保存。' if operation.snapshot else '项目已打开。')
        operation.snapshot = operation.result = None
        operation.deleteLater()
        w._refresh_action_states()
        w._continue_added_jobs()

    def refresh(self):
        w = self.window
        allowed = not self.busy and w._worker is None and w._active_file_browser is None
        w.project_open_action.setEnabled(allowed)
        w.project_save_action.setEnabled(allowed and bool(w.jobs))
        w.project_button.setToolTip(('当前项目：' + self.path.name + '\n' if self.path else '')
            + '加密保存项目 · Ctrl+S')
