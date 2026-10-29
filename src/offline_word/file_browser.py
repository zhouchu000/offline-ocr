"""Application-owned browser for selecting local images and output paths."""

from __future__ import annotations

from enum import Enum, IntEnum
import os
from pathlib import Path
import re

from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QColor, QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .security import OfflineError, assert_local_path


class LocalFileMode(Enum):
    EXISTING_FILES = "existing_files"
    DIRECTORY = "directory"
    SAVE_FILE = "save_file"


class LocalFileResult(IntEnum):
    REJECTED = 0
    ACCEPTED = 1


class LocalFileBrowser(QWidget):
    """Browse local paths with ordinary Qt widgets and Python directory reads."""

    finished = Signal(int)

    def __init__(
        self,
        parent: QWidget | None,
        title: str,
        mode: LocalFileMode,
        *,
        accept_label: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.mode = mode
        self._result = LocalFileResult.REJECTED
        self._selected_files: list[str] = []
        self._allowed_suffixes: set[str] = set()
        self._default_suffix = ""
        self._tree_cache: dict[tuple[str, tuple[str, ...]], QTreeWidget] = {}
        self.current_directory = self._initial_directory()
        self.setWindowTitle(title)
        self.setObjectName("localFileBrowser")
        self.setMinimumSize(680, 460)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 18)
        root.setSpacing(12)

        heading = QHBoxLayout()
        title_label = QLabel(title)
        title_label.setObjectName("fileBrowserTitle")
        privacy_label = QLabel("仅浏览本机文件")
        privacy_label.setObjectName("privacyBadge")
        heading.addWidget(title_label)
        heading.addStretch(1)
        heading.addWidget(privacy_label)
        root.addLayout(heading)

        location_row = QHBoxLayout()
        location_row.addWidget(QLabel("当前位置："))
        self.location_edit = QLineEdit(str(self.current_directory))
        self.location_edit.setAccessibleName("当前文件夹")
        self.location_edit.returnPressed.connect(self._go_to_typed_directory)
        location_row.addWidget(self.location_edit, 1)
        self.go_button = QPushButton("转到")
        self.go_button.setAccessibleName("转到输入的文件夹")
        self.go_button.clicked.connect(self._go_to_typed_directory)
        self.parent_button = QPushButton("上一级")
        self.parent_button.setAccessibleName("返回上一级文件夹")
        self.parent_button.clicked.connect(self._go_parent)
        self.home_button = QPushButton("个人文件夹")
        self.home_button.setAccessibleName("打开个人文件夹")
        self.home_button.clicked.connect(lambda: self.setDirectory(Path.home()))
        location_row.addWidget(self.go_button)
        location_row.addWidget(self.parent_button)
        location_row.addWidget(self.home_button)
        root.addLayout(location_row)

        self.file_tree_stack = QStackedWidget()
        self.file_tree_stack.setAccessibleName("本机文件列表")
        self.file_tree: QTreeWidget
        root.addWidget(self.file_tree_stack, 1)

        details = QHBoxLayout()
        self.filename_label = QLabel("文件名：" if mode != LocalFileMode.DIRECTORY else "选择位置：")
        self.filename_edit = QLineEdit()
        self.filename_edit.setAccessibleName(self.filename_label.text())
        self.filename_edit.setReadOnly(mode != LocalFileMode.SAVE_FILE)
        self.filename_edit.textChanged.connect(self._refresh_accept_state)
        details.addWidget(self.filename_label)
        details.addWidget(self.filename_edit, 1)
        root.addLayout(details)

        footer = QHBoxLayout()
        self.status_label = QLabel(self._hint_text())
        self.status_label.setObjectName("secondaryText")
        footer.addWidget(self.status_label, 1)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.reject)
        default_accept = "选择" if mode == LocalFileMode.DIRECTORY else ("保存" if mode == LocalFileMode.SAVE_FILE else "打开")
        self.accept_button = QPushButton(accept_label or default_accept)
        self.accept_button.setObjectName("primary")
        self.accept_button.clicked.connect(self._accept_selection)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.accept_button)
        root.addLayout(footer)

        self._populate()

    def _create_file_tree(self) -> QTreeWidget:
        tree = QTreeWidget()
        tree.setAccessibleName("本机文件列表")
        tree.setHeaderLabels(["名称", "类型"])
        tree.header().setStretchLastSection(False)
        tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        tree.setRootIsDecorated(False)
        tree.setAlternatingRowColors(True)
        tree.setUniformRowHeights(True)
        tree.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
            if self.mode == LocalFileMode.EXISTING_FILES
            else QAbstractItemView.SelectionMode.SingleSelection
        )
        tree.itemSelectionChanged.connect(self._selection_changed)
        tree.itemActivated.connect(self._activate_item)
        return tree

    def open(self) -> None:
        self.show()
        self.location_edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def reset_for_open(self) -> None:
        """Reset transient selection state before reusing this embedded page."""
        self._result = LocalFileResult.REJECTED
        self._selected_files = []
        self.file_tree.clearSelection()
        self.filename_edit.clear()
        self._populate()

    def accept(self) -> None:
        self._result = LocalFileResult.ACCEPTED
        self.finished.emit(int(self._result))

    def reject(self) -> None:
        self._result = LocalFileResult.REJECTED
        self.finished.emit(int(self._result))

    def result(self) -> LocalFileResult:
        return self._result

    def keyPressEvent(self, event: QKeyEvent) -> None:  # type: ignore[override]
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)

    @staticmethod
    def _initial_directory() -> Path:
        documents = Path.home() / "Documents"
        return documents if documents.is_dir() else Path.home()

    def setNameFilter(self, description: str) -> None:  # noqa: N802 - Qt-compatible API
        self._allowed_suffixes = {
            f".{suffix.casefold()}" for suffix in re.findall(r"\*\.([A-Za-z0-9]+)", description)
        }
        self._populate()

    def _hint_text(self) -> str:
        if self.mode == LocalFileMode.EXISTING_FILES:
            return "支持 JPG、PNG、BMP、TIFF；Shift / Command / Ctrl 多选。"
        if self.mode == LocalFileMode.DIRECTORY:
            return "选择本地文件夹；双击进入。"
        return "输入文档名称，自动补充 .docx 后缀。"

    def setDefaultSuffix(self, suffix: str) -> None:  # noqa: N802 - Qt-compatible API
        self._default_suffix = suffix.lstrip(".").casefold()

    def selectFile(self, value: str) -> None:  # noqa: N802 - Qt-compatible API
        path = Path(value).expanduser()
        if path.is_absolute():
            self.setDirectory(path.parent)
            value = path.name
        self.filename_edit.setText(value)

    def selectedFiles(self) -> list[str]:  # noqa: N802 - Qt-compatible API
        return list(self._selected_files)

    def setDirectory(self, directory: Path | str) -> bool:  # noqa: N802 - Qt-compatible API
        try:
            candidate = assert_local_path(directory, "remote_path")
        except OfflineError:
            self._show_error("无法打开该位置：只允许本机文件夹。")
            return False
        if not candidate.is_dir():
            self._show_error("无法打开该位置：文件夹不存在或不可读取。")
            return False
        self.current_directory = candidate
        self.location_edit.setText(str(candidate))
        self._populate()
        return True

    def _go_to_typed_directory(self) -> None:
        value = self.location_edit.text().strip()
        if value:
            self.setDirectory(value)

    def _go_parent(self) -> None:
        parent = self.current_directory.parent
        if parent != self.current_directory:
            self.setDirectory(parent)

    def _populate(self) -> None:
        cache_key = (str(self.current_directory), tuple(sorted(self._allowed_suffixes)))
        cached = self._tree_cache.get(cache_key)
        if cached is not None:
            self.file_tree = cached
            self.file_tree_stack.setCurrentWidget(cached)
            cached.clearSelection()
            self.parent_button.setEnabled(self.current_directory.parent != self.current_directory)
            self.status_label.setText(self._hint_text())
            self.status_label.setProperty("statusTone", "secondary")
            self.status_label.style().unpolish(self.status_label)
            self.status_label.style().polish(self.status_label)
            self._refresh_accept_state()
            return

        tree = self._create_file_tree()
        directories: list[Path] = []
        files: list[Path] = []
        try:
            with os.scandir(self.current_directory) as entries:
                for entry in entries:
                    if entry.name.startswith(".") or entry.is_symlink():
                        continue
                    path = self.current_directory / entry.name
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            directories.append(path)
                        elif entry.is_file(follow_symlinks=False) and self._file_is_visible(path):
                            files.append(path)
                    except OSError:
                        continue
        except OSError:
            self._show_error("无法读取当前文件夹。")
            return
        for path in sorted(directories, key=lambda item: item.name.casefold()):
            self._add_item(tree, path, "文件夹")
        for path in sorted(files, key=lambda item: item.name.casefold()):
            self._add_item(tree, path, self._file_type_label(path))
        self._tree_cache[cache_key] = tree
        self.file_tree_stack.addWidget(tree)
        self.file_tree = tree
        self.file_tree_stack.setCurrentWidget(tree)
        self.parent_button.setEnabled(self.current_directory.parent != self.current_directory)
        self.status_label.setText(self._hint_text())
        self.status_label.setProperty("statusTone", "secondary")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        self._refresh_accept_state()

    def _file_is_visible(self, path: Path) -> bool:
        if self.mode == LocalFileMode.DIRECTORY:
            return False
        if self.mode == LocalFileMode.SAVE_FILE:
            return path.suffix.casefold() == ".docx"
        return (
            not self._allowed_suffixes
            or path.suffix.casefold() in self._allowed_suffixes
            or path.suffix.casefold() == ".pdf"
        )

    def _file_type_label(self, path: Path) -> str:
        suffix = path.suffix.casefold()
        if suffix == ".pdf":
            return "不支持（暂不支持 PDF）"
        if self.mode == LocalFileMode.SAVE_FILE:
            return "Word 文档"
        return {
            ".jpg": "JPG 图片",
            ".jpeg": "JPEG 图片",
            ".png": "PNG 图片",
            ".bmp": "BMP 图片",
            ".tif": "TIFF 图片",
            ".tiff": "TIFF 图片",
        }.get(suffix, "图片")

    def _add_item(self, tree: QTreeWidget, path: Path, kind: str) -> None:
        item = QTreeWidgetItem([path.name, kind])
        item.setData(0, Qt.ItemDataRole.UserRole, str(path))
        if path.suffix.casefold() == ".pdf":
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable & ~Qt.ItemFlag.ItemIsEnabled)
            item.setForeground(1, QColor("#B42318"))
        elif self.mode == LocalFileMode.EXISTING_FILES and path.is_file():
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Unchecked)
        tree.addTopLevelItem(item)

    def _selection_changed(self) -> None:
        if self.mode == LocalFileMode.EXISTING_FILES:
            for index in range(self.file_tree.topLevelItemCount()):
                item = self.file_tree.topLevelItem(index)
                if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                    item.setCheckState(0, Qt.CheckState.Checked if item.isSelected() else Qt.CheckState.Unchecked)
        selected = [Path(item.data(0, Qt.ItemDataRole.UserRole)) for item in self.file_tree.selectedItems()]
        if self.mode == LocalFileMode.EXISTING_FILES:
            names = [path.name for path in selected if path.is_file()]
            self.filename_edit.setText("；".join(names))
        elif self.mode == LocalFileMode.DIRECTORY:
            target = next((path for path in selected if path.is_dir()), self.current_directory)
            self.filename_edit.setText(str(target))
        elif selected and selected[0].is_file():
            self.filename_edit.setText(selected[0].name)
        self._refresh_accept_state()

    def _activate_item(self, item: QTreeWidgetItem, _column: int) -> None:
        path = Path(item.data(0, Qt.ItemDataRole.UserRole))
        if path.is_dir():
            self.setDirectory(path)
            return
        if self.mode == LocalFileMode.EXISTING_FILES:
            self.file_tree.clearSelection()
            item.setSelected(True)
            self._accept_selection()
        elif self.mode == LocalFileMode.SAVE_FILE:
            self.filename_edit.setText(path.name)

    def _accept_selection(self) -> None:
        selected = [Path(item.data(0, Qt.ItemDataRole.UserRole)) for item in self.file_tree.selectedItems()]
        directories = [path for path in selected if path.is_dir()]
        if self.mode == LocalFileMode.EXISTING_FILES:
            files = [path for path in selected if path.is_file() and self._file_is_visible(path)]
            if not files and len(directories) == 1:
                self.setDirectory(directories[0])
                return
            if not files:
                self._show_error("请选择至少一张图片。")
                return
            self._selected_files = [str(path) for path in files]
        elif self.mode == LocalFileMode.DIRECTORY:
            self._selected_files = [str(directories[0] if directories else self.current_directory)]
        else:
            name = self.filename_edit.text().strip()
            if not name or Path(name).name != name or "/" in name or "\\" in name:
                self._show_error("请输入有效的 Word 文件名。")
                return
            if self._default_suffix and Path(name).suffix.casefold() != f".{self._default_suffix}":
                name = f"{name}.{self._default_suffix}"
            self._selected_files = [str(self.current_directory / name)]
        self.accept()

    def _refresh_accept_state(self) -> None:
        if self.mode == LocalFileMode.DIRECTORY:
            enabled = True
        elif self.mode == LocalFileMode.SAVE_FILE:
            enabled = bool(self.filename_edit.text().strip())
        else:
            enabled = any(
                Path(item.data(0, Qt.ItemDataRole.UserRole)).is_file() for item in self.file_tree.selectedItems()
            ) or any(
                Path(item.data(0, Qt.ItemDataRole.UserRole)).is_dir() for item in self.file_tree.selectedItems()
            )
        self.accept_button.setEnabled(enabled)

    def _show_error(self, message: str) -> None:
        self.status_label.setText(message)
        self.status_label.setProperty("statusTone", "warning")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
