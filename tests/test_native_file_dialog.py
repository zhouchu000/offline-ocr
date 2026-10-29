from pathlib import Path

from PySide6.QtWidgets import QFileDialog

from offline_word import gui
from offline_word.file_browser import LocalFileMode
from offline_word.native_file_dialog import NativeFileDialog


def test_windows_selectors_use_native_modes_and_preserve_workspace(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "_use_native_file_dialogs", lambda: True)
    window = gui.MainWindow(tmp_path / "models", tmp_path / "session")
    qtbot.addWidget(window)
    for mode, expected in [(LocalFileMode.EXISTING_FILES, QFileDialog.FileMode.ExistingFiles),
                           (LocalFileMode.DIRECTORY, QFileDialog.FileMode.Directory),
                           (LocalFileMode.SAVE_FILE, QFileDialog.FileMode.AnyFile)]:
        dialog = window._dialog("选择", mode)
        assert isinstance(dialog, NativeFileDialog)
        assert dialog.fileMode() == expected
        assert not dialog.testOption(QFileDialog.Option.DontUseNativeDialog)
        assert dialog.supportedSchemes() == ["file"]
        assert window.central_stack.indexOf(dialog) == -1
        assert window._dialog("选择", mode) is dialog


def test_windows_selector_cancel_then_accept_once(qtbot, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(gui, "_use_native_file_dialogs", lambda: True)
    window = gui.MainWindow(tmp_path / "models", tmp_path / "session")
    qtbot.addWidget(window)
    dialog = window._dialog("选择图片", LocalFileMode.EXISTING_FILES)
    # Test event handling without launching an OS modal panel in unattended tests.
    monkeypatch.setattr(dialog, "open", lambda: None)
    received = []
    window._open_file_browser(dialog, received.append)
    dialog.reject()
    qtbot.waitUntil(lambda: window._active_file_browser is None)
    assert received == []
    assert window.central_stack.currentWidget() is window.workspace_root
    selected = [str(tmp_path / "中文 甲.png"), str(tmp_path / "中文 乙.jpg")]
    monkeypatch.setattr(dialog, "selectedFiles", lambda: selected)
    window._open_file_browser(dialog, received.append)
    dialog.finished.emit(1)
    qtbot.waitUntil(lambda: window._active_file_browser is None)
    assert received == [selected]
    assert window.central_stack.currentWidget() is window.workspace_root
