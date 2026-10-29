"""Cancellable snapshot export and time-sliced native DOCX preview."""
from copy import deepcopy
from pathlib import Path
import threading
import time
import uuid

from PySide6.QtCore import QObject, QPoint, QThread, QTimer, Signal
from PySide6.QtWidgets import QDialog, QLabel, QProgressBar, QPushButton, QVBoxLayout
from .security import OfflineError


class ExportTask(QThread):
    progress = Signal(int, int)

    def __init__(self, owner, pages, target, *, separate, mode, remove_numbers, preview, exporter):
        super().__init__(owner)
        self.pages, self.target = deepcopy(pages), Path(target)
        self.separate, self.mode, self.remove_numbers = separate, mode, remove_numbers
        self.preview, self.exporter = preview, exporter
        self.cancelled = threading.Event()
        self.paths, self.completed, self.packages = [], [], []
        self.error = None
        self.removed = 0

    def check(self):
        if self.cancelled.is_set():
            raise OfflineError('export_cancelled')

    def run(self):
        try:
            self.check()
            pages = self.pages
            if self.remove_numbers:
                from .page_numbers import without_page_numbers
                pages, self.removed = without_page_numbers(pages)
            groups = [[page] for page in pages] if self.separate else [pages]
            for index, group in enumerate(groups):
                self.check()
                target = (self.target / f'识别结果_{index+1:03d}.docx') if self.separate else self.target
                offset = index if self.separate else 0
                path = self.exporter(group, target, layout_mode=self.mode, check_cancel=self.check,
                    progress=lambda done, total: self.progress.emit(offset + done, len(pages)))
                self.paths.append(Path(path))
                self.completed.extend([self.pages[index]] if self.separate else self.pages)
            if self.preview:
                from docx import Document
                for path in self.paths:
                    self.check()
                    self.packages.append(Document(str(path)))
        except Exception as error:
            self.error = getattr(error, 'code', 'export_failed')


class ExportProgress(QDialog):
    def __init__(self, owner, cancel):
        super().__init__(owner)
        from PySide6.QtCore import Qt
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setWindowTitle('正在准备文档')
        self.setMinimumWidth(340)
        self._cancel = cancel
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        self.label = QLabel('正在准备文档…')
        self.bar = QProgressBar()
        self.button = QPushButton('取消')
        self.button.clicked.connect(self.reject)
        layout.addWidget(self.label)
        layout.addWidget(self.bar)
        layout.addWidget(self.button)

    def reject(self):
        self.button.setEnabled(False)
        self.label.setText('正在取消…')
        self._cancel()


class ExportController(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.task = None
        self.dialog = None
        self._steps = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._preview_step)

    @property
    def busy(self):
        return self.task is not None

    def start(self, pages, target=None, *, separate=False, preview=False):
        if self.busy:
            return
        w = self.window
        if preview:
            folder = w.session_dir / 'previews' / uuid.uuid4().hex
            folder.mkdir(mode=0o700, parents=True)
            target = folder if separate else folder / 'preview.docx'
        from .gui import export_docx
        self.task = ExportTask(self, pages, target, separate=separate,
            mode=w.export_layout_combo.currentData(), remove_numbers=w.remove_page_numbers_checkbox.isChecked(),
            preview=preview, exporter=export_docx)
        self.dialog = ExportProgress(w, self.cancel)
        self.dialog.bar.setRange(0, len(pages))
        self.task.progress.connect(self._progress)
        self.task.finished.connect(self._finished)
        self.dialog.open()
        w._refresh_action_states()
        self.task.start()

    def cancel(self):
        if self.task:
            self.task.cancelled.set()

    def _progress(self, done, total):
        self.dialog.bar.setValue(done)
        if not self.task.cancelled.is_set():
            self.dialog.label.setText(f'生成文档 {done}/{total} 页')

    def _finished(self):
        task, w = self.task, self.window
        if not task.preview:
            # Mark the exported snapshot, never a newer edit made while saving.
            w._mark_pages_exported(task.completed)
        if task.preview and not task.error and not task.cancelled.is_set():
            self.dialog.label.setText('正在加载排版预览…')
            self._steps = self._preview_pages()
            w.preview_canvas.clear_pages()
            w.preview_page_list.clear()
            self.timer.start(0)
            return
        self._complete()

    def _preview_pages(self):
        from .export_preview import iter_docx_pages
        for package in self.task.packages:
            yield from iter_docx_pages(package)

    def _preview_step(self):
        try:
            self.task.check()
            started = time.monotonic()
            while time.monotonic() - started < .008:
                page = next(self._steps)
                if page is not None:
                    w = self.window
                    w.preview_canvas.append_page(page)
                    w._add_preview_thumbnail(page)
                    if len(w.preview_canvas._pages) == 1:
                        w.export_options.accept()
                        w.close_context_panel()
                        w.views.setCurrentWidget(w.preview_view)
                        title = w.export_layout_combo.itemText(w.export_layout_combo.findData(self.task.mode))
                        w.preview_mode_label.setText(title)
                        w.preview_left_mode.setText(title)
                        w.preview_options_label.setText('正在加载其余页面…')
                        w.preview_left_options.setText('预览准备中')
                        w.preview_stats.setText('正在加载排版…')
                        w._sync_shell()
                        w.preview_canvas.fit_page('page')
                        self.dialog.move(w.mapToGlobal(QPoint(24, max(24, w.height()-self.dialog.height()-48))))
                    self.dialog.label.setText(f'已加载 {len(w.preview_canvas._pages)} 页预览')
            self.timer.start(0)
        except StopIteration:
            self.window._present_export_preview(self.task)
            self._complete()
        except Exception as error:
            self.task.error = getattr(error, 'code', 'preview_failed')
            self._complete()

    def _complete(self):
        task, w = self.task, self.window
        cancelled = task.error == 'export_cancelled' or (task.preview and task.cancelled.is_set())
        if task.preview and (task.error or cancelled):
            w.preview_canvas.clear_pages()
            w.preview_page_list.clear()
            for path in task.paths:
                path.unlink(missing_ok=True)
            w.show_document()
        if cancelled:
            w._set_status('已取消。' + (f'已完成的 {len(task.paths)} 个文件已保留。' if task.paths and not task.preview else ''))
        elif task.error:
            w._set_status(f'文档生成失败（{task.error}）。' +
                          (f'已完成 {len(task.paths)} 个文件。' if task.paths and not task.preview else ''), warning=True)
        elif not task.preview:
            w._set_status(f'已导出 {len(task.completed)} 页，生成 {len(task.paths)} 个文件。' +
                          (f'已去除 {task.removed} 处页码。' if task.remove_numbers else ''))
        self._steps = None
        self.task = None
        self.dialog.accept()
        self.dialog.deleteLater()
        self.dialog = None
        task.deleteLater()
        w._refresh_action_states()
