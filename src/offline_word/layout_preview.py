"""Small vector examples of the four existing Word output layouts."""
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget
from .theme import colours


class LayoutPreview(QWidget):
    def __init__(self, mode, parent=None):
        super().__init__(parent)
        self.mode = mode
        self.setFixedSize(60, 78)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        palette = colours(self)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(palette['border']), 1))
        painter.setBrush(QColor(palette['surface']))
        painter.drawRoundedRect(QRectF(1, 1, 57, 75), 4, 4)
        painter.setPen(QPen(QColor(palette['primary']), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        if self.mode != 'text_only':
            painter.drawLine(12, 12, 37, 12)
        painter.setPen(QPen(QColor(palette['muted']), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        if self.mode == 'source':
            for y, right in [(23, 47), (29, 41), (35, 45)]:
                painter.drawLine(9, y, right, y)
            painter.drawRect(QRectF(9, 43, 37, 17))
            painter.drawLine(9, 51, 46, 51)
            painter.drawLine(25, 43, 25, 60)
            painter.drawLine(26, 68, 31, 68)
        elif self.mode == 'flow':
            for y, right in [(23, 43), (29, 33), (35, 46), (41, 37), (47, 42), (53, 30)]:
                painter.drawLine(9, y, right, y)
            painter.setPen(QPen(QColor(palette['primary']), 1, Qt.PenStyle.DashLine))
            painter.drawLine(6, 63, 51, 63)
        elif self.mode == 'paragraph':
            for top in (23, 46):
                painter.drawLine(16, top, 48, top)
                painter.drawLine(9, top+6, 48, top+6)
                painter.drawLine(9, top+12, 32, top+12)
        else:
            for y in range(14, 65, 6):
                painter.drawLine(9, y, 48 if y < 62 else 30, y)
