"""Read-only integrity validation without delaying the first window frame."""
from PySide6.QtCore import QThread


class ModelCheck(QThread):
    def __init__(self, parent, root, validate):
        super().__init__(parent)
        self.root, self.validate = root, validate
        self.code = ''

    def run(self):
        try:
            self.validate(self.root)
        except Exception as error:
            self.code = getattr(error, 'code', 'MODEL_UNAVAILABLE')
