"""Page thumbnail rail with original pixels and a bounded in-memory cache."""
from collections import OrderedDict
from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QImage, QImageReader, QPainter
from PySide6.QtWidgets import QStyle, QStyledItemDelegate
from .theme import colours, icon


class GalleryDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._thumbnails = OrderedDict()

    def _thumbnail(self, identity):
        if not identity:
            return QImage()
        key = tuple(identity)
        if key in self._thumbnails:
            self._thumbnails.move_to_end(key)
            return self._thumbnails[key]
        reader = QImageReader(key[0])
        reader.setAutoTransform(True)
        available = not key[1] or reader.jumpToImage(key[1])
        size = reader.size()
        if size.isValid():
            reader.setScaledSize(size.scaled(QSize(96, 128), Qt.AspectRatioMode.KeepAspectRatio))
        value = reader.read() if available else QImage()
        if value.isNull():
            from .photo_canvas import PhotoCanvas
            value = PhotoCanvas._pillow_frame(key[0], key[1])
        if not value.isNull():
            value = value.scaled(96, 128, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self._thumbnails[key] = value
        if len(self._thumbnails) > 96:
            self._thumbnails.popitem(last=False)
        return value

    def sizeHint(self, option, index):
        return QSize(96, 119)

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        palette = colours(self.parent())
        rect = QRectF(option.rect)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(palette['tint' if selected else 'subtle']))
            painter.drawRoundedRect(rect.adjusted(9,3,-9,-3),5,5)
        if selected:
            painter.fillRect(QRectF(rect.x()+3, rect.center().y()-12, 3, 24), QColor(palette['primary']))
        thumbnail = self._thumbnail(index.data(Qt.ItemDataRole.UserRole + 1))
        paper = QRectF(rect.center().x()-26, rect.y()+11, 52, 73)
        painter.setPen(QColor(palette['border']))
        painter.setBrush(QColor('#FFFFFF'))
        painter.drawRoundedRect(paper, 2, 2)
        if not thumbnail.isNull():
            size = thumbnail.size().scaled(48, 69, Qt.AspectRatioMode.KeepAspectRatio)
            target = QRectF(0, 0, size.width(), size.height())
            target.moveCenter(paper.center())
            painter.drawImage(target, thumbnail)
        lines = str(index.data(Qt.ItemDataRole.DisplayRole) or '').splitlines()
        font = QFont(option.font)
        font.setPixelSize(12)
        painter.setFont(font)
        painter.setPen(QColor(palette['text']))
        painter.drawText(QRectF(rect.x(),rect.y()+85,rect.width(),17), Qt.AlignmentFlag.AlignCenter, str(index.row()+1))
        status = lines[1] if len(lines) > 1 else ''
        review_count = index.data(Qt.ItemDataRole.UserRole + 2) or 0
        status = f'{review_count} 处待复核' if review_count and status == '完成' else status
        tone = 'warning' if review_count else {'完成': 'success', '失败': 'error', '处理中': 'primary'}.get(status, 'muted')
        painter.setPen(QColor(palette[tone]))
        font.setPixelSize(11)
        painter.setFont(font)
        painter.drawText(QRectF(rect.x()+4,rect.y()+101,rect.width()-8,15), Qt.AlignmentFlag.AlignCenter, status)
        painter.restore()
