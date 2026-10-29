"""Paper suggestions and recognition preferences, separated from the main shell."""
from __future__ import annotations

from dataclasses import replace
import os

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal
from PySide6.QtWidgets import QDialog, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout

from .diagnostics import DiagnosticRecorder
from .paper import PaperDetection, detect_paper_quad


class _DetectionSignals(QObject):
    finished = Signal(object, object)


class _DetectionTask(QRunnable):
    def __init__(self, job):
        super().__init__()
        self.job = job
        self.snapshot = replace(job, result=None)
        self.signals = _DetectionSignals()

    def run(self):
        from .images import _load_oriented_rgb
        try:
            with _load_oriented_rgb(self.snapshot) as image:
                result = detect_paper_quad(image)
        except Exception:
            result = PaperDetection(status="unavailable")
        self.signals.finished.emit(self.job, result)


class RecognitionControls(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.tasks = {}
        self.diagnostics = DiagnosticRecorder()
        self.diagnostic_dialog = None
        window.profile_combo.currentIndexChanged.connect(self.profile_changed)
        window.diagnostics_checkbox.setChecked(os.environ.get("OFFLINE_WORD_DIAG") == "1")
        window.diagnostics_checkbox.toggled.connect(self._diagnostics_changed)
        self._diagnostics_changed(window.diagnostics_checkbox.isChecked())
        self.profile_changed()

    def apply_to_job(self, job):
        w = self.window
        job.performance_profile = str(w.profile_combo.currentData())
        job.auto_proofread = w.auto_proofread_checkbox.isChecked()
        job.text_only = w.export_layout_combo.currentData() == "text_only"
        job.diagnostics = w.diagnostics_checkbox.isChecked()

    def profile_changed(self):
        w = self.window
        parallel = w.profile_combo.currentData() == "parallel"
        w.profile_note.setText("同时处理两张图片；内存不足时转为顺序识别。" if parallel else "逐张处理，降低内存占用。")
        w.profile_badge.setText("双任务并行" if parallel else "")
        w.profile_badge.setVisible(parallel)
        w.auto_proofread_checkbox.setEnabled(w._worker is None)
        if hasattr(w, "motion"):
            w._refresh_action_states()

    def _diagnostics_changed(self, enabled):
        self.window.view_diagnostics_button.setVisible(enabled)
        if not enabled:
            self.diagnostics.clear()
            if self.diagnostic_dialog is not None:
                self.diagnostic_dialog.reject()

    def show_diagnostics(self):
        if self.diagnostic_dialog is not None:
            self.diagnostic_dialog.raise_()
            return
        dialog = QDialog(self.window)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle("本次诊断记录")
        dialog.resize(620, 440)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addWidget(QLabel("仅记录阶段耗时与内存采样；退出后清除。"))
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(self.diagnostics.text() if self.diagnostics.events else "暂无记录。启用诊断后执行识别。")
        layout.addWidget(text)
        done = QPushButton("完成")
        done.clicked.connect(dialog.accept)
        layout.addWidget(done)
        self.diagnostic_dialog = dialog
        dialog.finished.connect(lambda: setattr(self, 'diagnostic_dialog', None))
        dialog.open()

    def refresh_paper(self):
        w = self.window
        job = w.current_job
        visible = job is not None and job.mode == "photo" and job.crop is None and job.perspective is None and job.paper_status not in {"kept", "manual", "confirmed"}
        w.paper_bar.setVisible(visible)
        w.source_preview.set_paper_suggestion(job.paper_quad if visible and job.paper_status == "suggested" else None)
        if not visible:
            return
        busy = w._worker is not None or w._active_file_browser is not None
        if job.paper_status == "not_run" and not busy:
            job.paper_status = "detecting"
            task = _DetectionTask(job)
            self.tasks[job.id] = task
            task.signals.finished.connect(self._detected)
            QThreadPool.globalInstance().start(task)
        status = job.paper_status
        w.paper_note.setText({
            "not_run": "识别后可预览纸面建议",
            "detecting": "正在检测纸面…",
            "suggested": "纸面范围待确认；未确认前保留整图",
            "ambiguous": "纸面范围不明确，请手动选择",
        }.get(status, "未确定纸面范围，可手动裁剪"))
        w.accept_paper_button.setVisible(status == "suggested")
        for button in (w.accept_paper_button, w.adjust_paper_button, w.keep_paper_button):
            button.setEnabled(not busy)

    def _detected(self, job, result):
        self.tasks.pop(job.id, None)
        w = self.window
        if not any(item is job for item in w.jobs) or job.mode != "photo" or job.crop is not None or job.perspective is not None or job.paper_status != "detecting":
            return
        job.paper_quad, job.paper_confidence, job.paper_status = result.quad, result.confidence, result.status
        self.refresh_paper()

    def accept_paper(self):
        w = self.window
        job = w.current_job
        if job is None or job.paper_status != "suggested" or not job.paper_quad or w._worker:
            return
        quad = list(job.paper_quad)
        confidence = job.paper_confidence
        if w.apply_perspective(quad):
            job.paper_quad, job.paper_confidence, job.paper_status = quad, confidence, "confirmed"
            self.refresh_paper()
            w._set_status("纸面范围已确认；识别仅处理框内内容，原图保留。")

    def keep_whole_image(self):
        w = self.window
        job = w.current_job
        if job is None or w._worker:
            return
        job.paper_status = "kept"
        self.refresh_paper()
        w._set_status("已保留整图。")

    def adjust_paper(self):
        self.window.crop_button.setChecked(True)
        self.window._toggle_crop()
