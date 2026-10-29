"""Explicit inspection/restoration of conservatively filtered OCR fragments."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLabel, QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QVBoxLayout
from .review_widgets import SourceCrop


def show_filtered_text(window):
    job = window.current_job
    if not job or not job.result or not job.result.filtered_text:
        return
    dialog = QDialog(window)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dialog.setWindowTitle('查看过滤文字')
    dialog.setWindowModality(Qt.WindowModality.WindowModal)
    dialog.resize(600, 560)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(24, 24, 24, 24)
    layout.setSpacing(12)
    note = QLabel('图码或装饰中的过滤文字，可对照原图恢复。')
    note.setWordWrap(True)
    layout.addWidget(note)
    listing = QListWidget()
    listing.setAccessibleName('本页过滤文字')
    crop = SourceCrop()
    text = QPlainTextEdit()
    text.setReadOnly(True)
    text.setAccessibleName('过滤前的原始识别文字')
    restore = QPushButton('恢复到文档')
    restore.setObjectName('primary')
    for fragment in job.result.filtered_text:
        item = QListWidgetItem(('已恢复 · ' if fragment.restored else '') + fragment.text[:80])
        item.setData(Qt.ItemDataRole.UserRole, fragment.id)
        listing.addItem(item)
    def selected():
        item = listing.currentItem()
        return next((f for f in job.result.filtered_text if item and f.id == item.data(Qt.ItemDataRole.UserRole)), None)
    def refresh(*_):
        fragment = selected()
        text.setPlainText(fragment.text if fragment else '')
        crop.set_source(job.result.processed_image_path, fragment.bbox if fragment else None, (job.result.width, job.result.height))
        restore.setEnabled(bool(fragment and not fragment.restored))
    def apply():
        from .nontext import restore_filtered_text
        fragment = selected()
        if fragment and restore_filtered_text(job.result, fragment.id):
            listing.currentItem().setText('已恢复 · ' + fragment.text[:80])
            window.review_changed(job.id)
            window._refresh_current_page()
            window._set_status('文字已恢复；请核对内容与位置。')
            refresh()
        else:
            note.setText('原位置已有人工修改，已保留当前内容。')
    listing.currentItemChanged.connect(refresh)
    restore.clicked.connect(apply)
    for widget, stretch in ((listing, 1), (crop, 0), (text, 1), (restore, 0)):
        layout.addWidget(widget, stretch)
    close = QPushButton('完成')
    close.clicked.connect(dialog.accept)
    layout.addWidget(close)
    listing.setCurrentRow(0)
    window._filtered_dialog = dialog
    dialog.open()
