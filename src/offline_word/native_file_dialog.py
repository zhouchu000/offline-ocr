"""Windows system file panels; imported only on Windows by the main window."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QWidget

from .file_browser import LocalFileMode


class NativeFileDialog(QFileDialog):
    def __init__(self, parent: QWidget, title: str, mode: LocalFileMode,
                 *, accept_label: str | None = None) -> None:
        super().__init__(parent, title)
        self.setOption(QFileDialog.Option.DontUseNativeDialog, False)
        self.setSupportedSchemes(["file"])
        self.setWindowModality(Qt.WindowModality.WindowModal)
        if mode == LocalFileMode.EXISTING_FILES:
            self.setFileMode(QFileDialog.FileMode.ExistingFiles)
        elif mode == LocalFileMode.DIRECTORY:
            self.setFileMode(QFileDialog.FileMode.Directory)
            self.setOption(QFileDialog.Option.ShowDirsOnly, True)
        else:
            self.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
            self.setFileMode(QFileDialog.FileMode.AnyFile)
        if accept_label:
            self.setLabelText(QFileDialog.DialogLabel.Accept, accept_label)

    def reset_for_open(self) -> None:
        self.setResult(0)
        self.selectFile("")
