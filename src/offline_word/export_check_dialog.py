"""A compact advisory step for an actual export with geometry findings."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout


class ExportCheckDialog(QDialog):
    def __init__(self, window, issues, on_continue):
        super().__init__(window)
        self.setWindowTitle('导出前检查')
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(660, 450)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        title = QLabel(f'{len(issues)} 处待核对')
        title.setObjectName('paneTitle')
        layout.addWidget(title)
        note = QLabel('检查不修改文字。导出后请核对 Word 排版。')
        note.setWordWrap(True)
        note.setObjectName('caption')
        layout.addWidget(note)
        self.list = QListWidget()
        for issue in issues:
            number = next((i for i,j in enumerate(window.jobs,1) if j.id == issue.page_id), 0)
            item = QListWidgetItem(f'第 {number} 页 · {issue.message}')
            item.setData(Qt.ItemDataRole.UserRole, (issue.page_id, issue.block_ids[0]))
            self.list.addItem(item)
        self.list.setCurrentRow(0)
        layout.addWidget(self.list, 1)
        actions = QHBoxLayout()
        locate = QPushButton('定位核对')
        def navigate():
            item = self.list.currentItem()
            if item:
                key = item.data(Qt.ItemDataRole.UserRole)
                self.reject()
                window._locate_review_item(*key)
        locate.clicked.connect(navigate)
        self.list.itemDoubleClicked.connect(navigate)
        actions.addWidget(locate)
        actions.addStretch()
        cancel = QPushButton('取消')
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        proceed = QPushButton('保留内容并导出')
        proceed.setObjectName('primary')
        proceed.clicked.connect(self.accept)
        actions.addWidget(proceed)
        layout.addLayout(actions)
        self.accepted.connect(on_continue)
