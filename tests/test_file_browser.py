from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTreeWidgetItem

from offline_word.file_browser import LocalFileBrowser, LocalFileMode, LocalFileResult


def _item(dialog: LocalFileBrowser, name: str) -> QTreeWidgetItem:
    for index in range(dialog.file_tree.topLevelItemCount()):
        item = dialog.file_tree.topLevelItem(index)
        if Path(item.data(0, Qt.ItemDataRole.UserRole)).name == name:
            return item
    raise AssertionError(f"missing item: {name}")


def test_app_file_browser_filters_and_returns_multiple_images(qtbot, tmp_path: Path):
    first = tmp_path / "甲.png"
    second = tmp_path / "乙.jpg"
    ignored = tmp_path / "说明.txt"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    ignored.write_text("ignored", encoding="utf-8")
    (tmp_path / "子目录").mkdir()

    dialog = LocalFileBrowser(None, "导入图片", LocalFileMode.EXISTING_FILES)
    qtbot.addWidget(dialog)
    dialog.setNameFilter("图片文件 (*.jpg *.jpeg *.png *.bmp *.tif *.tiff)")
    assert dialog.setDirectory(tmp_path)

    visible = {
        Path(dialog.file_tree.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole)).name
        for index in range(dialog.file_tree.topLevelItemCount())
    }
    assert visible == {"甲.png", "乙.jpg", "子目录"}

    _item(dialog, "甲.png").setSelected(True)
    _item(dialog, "乙.jpg").setSelected(True)
    dialog._accept_selection()

    assert dialog.result() == LocalFileResult.ACCEPTED
    assert dialog.selectedFiles() == [str(first), str(second)]


def test_app_file_browser_navigates_without_qfiledialog_or_filesystem_model(qtbot, tmp_path: Path):
    child = tmp_path / "资料"
    child.mkdir()
    (child / "page.png").write_bytes(b"image")
    dialog = LocalFileBrowser(None, "导入图片", LocalFileMode.EXISTING_FILES)
    qtbot.addWidget(dialog)
    dialog.setNameFilter("图片文件 (*.png)")
    assert dialog.setDirectory(tmp_path)

    dialog._activate_item(_item(dialog, "资料"), 0)

    assert dialog.current_directory == child
    assert _item(dialog, "page.png").data(0, Qt.ItemDataRole.UserRole) == str(child / "page.png")


def test_app_file_browser_keeps_directory_items_alive_when_navigating(qtbot, tmp_path: Path):
    """Avoid destroying Cocoa accessibility elements while the app is running."""
    root_image = tmp_path / "根目录.png"
    root_image.write_bytes(b"root")
    child = tmp_path / "资料"
    child.mkdir()
    (child / "子目录.png").write_bytes(b"child")
    dialog = LocalFileBrowser(None, "导入图片", LocalFileMode.EXISTING_FILES)
    qtbot.addWidget(dialog)
    dialog.setNameFilter("图片文件 (*.png)")
    assert dialog.setDirectory(tmp_path)
    original_item = _item(dialog, root_image.name)

    assert dialog.setDirectory(child)
    assert dialog.setDirectory(tmp_path)

    assert _item(dialog, root_image.name) is original_item


def test_app_file_browser_directory_and_docx_save_results(qtbot, tmp_path: Path):
    folder = tmp_path / "导出目录"
    folder.mkdir()
    directory_dialog = LocalFileBrowser(None, "选择文件夹", LocalFileMode.DIRECTORY, accept_label="选择")
    qtbot.addWidget(directory_dialog)
    assert directory_dialog.setDirectory(tmp_path)
    _item(directory_dialog, "导出目录").setSelected(True)
    directory_dialog._accept_selection()
    assert directory_dialog.selectedFiles() == [str(folder)]

    save_dialog = LocalFileBrowser(None, "导出 Word", LocalFileMode.SAVE_FILE, accept_label="保存")
    qtbot.addWidget(save_dialog)
    assert save_dialog.setDirectory(folder)
    save_dialog.setDefaultSuffix("docx")
    save_dialog.selectFile("识别结果")
    save_dialog._accept_selection()
    assert save_dialog.selectedFiles() == [str(folder / "识别结果.docx")]
