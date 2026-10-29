"""Presentation-only source crops and safe, compact text comparisons."""
from difflib import SequenceMatcher
from html import escape
import math
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap, QTextDocument
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView, QStyle, QStyledItemDelegate

from .theme import colours


def difference_html(original, candidate, palette):
    """Markup is generated here; OCR text is always escaped, never interpreted."""
    if max(len(original), len(candidate)) > 2048:
        # Bound the quadratic comparison for unusually long OCR blocks.
        text = escape(candidate[:4000]).replace('\n', '<br>')
        return text + '<br><small>完整内容见“完整对照”。</small>'
    parts = []
    for tag, a, b, c, d in SequenceMatcher(None, original, candidate, autojunk=False).get_opcodes():
        text = escape(candidate[c:d]).replace('\n', '<br>')
        if tag == 'equal':
            parts.append(text)
        elif tag == 'delete':
            removed = escape(original[a:b]).replace('\n', '<br>')
            parts.append(f'<span style="color:{palette["warning"]}"><s>{removed}</s></span>')
        else:
            parts.append(f'<span style="color:{palette["primary"]};font-weight:600">{text}</span>')
    return ''.join(parts)


class DifferenceDelegate(QStyledItemDelegate):
    def __init__(self, parent):
        super().__init__(parent)
        self.original = ''
        self._documents = {}

    def document(self, index, width):
        palette = colours(self.parent())
        text = index.data(Qt.ItemDataRole.UserRole) or ''
        key = (self.original, text, width, self.parent().font().toString(), palette['text'])
        if key in self._documents:
            return self._documents[key]
        doc = QTextDocument()
        doc.setDefaultFont(self.parent().font())
        doc.setDocumentMargin(0)
        doc.setHtml(f'<span style="color:{palette["text"]}">' +
                    difference_html(self.original, text, palette) + '</span>')
        doc.setTextWidth(max(40, width - 24))
        if len(self._documents) >= 12:
            self._documents.clear()
        self._documents[key] = doc
        return doc

    def sizeHint(self, option, index):
        doc = self.document(index, self.parent().viewport().width())
        return QSize(0, math.ceil(doc.size().height()) + 20)

    def paint(self, painter, option, index):
        palette = colours(self.parent())
        painter.save()
        painter.setClipRect(option.rect)
        rect = QRectF(option.rect).adjusted(1, 2, -1, -2)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.setPen(QPen(QColor(palette['primary'] if option.state & QStyle.StateFlag.State_HasFocus else palette['border']), 1))
        painter.setBrush(QColor(palette['tint'] if selected else palette['surface']))
        painter.drawRoundedRect(rect, 7, 7)
        painter.translate(option.rect.left() + 12, option.rect.top() + 10)
        self.document(index, option.rect.width()).drawContents(painter)
        painter.restore()


class SourceCrop(QGraphicsView):
    """Display a bounded crop of the processed page, without writing an image."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('sourceCrop')
        self.setAccessibleName('当前文字的原图局部，可放大并拖动查看')
        self.setScene(QGraphicsScene(self))
        self.setMinimumWidth(0)
        self.setFixedHeight(108)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self._identity = None
        self._path = None
        self._image = QImage()
        self._fit = True
        self._readable = False

    def set_source(self, path, bbox, page_size, *, image=None, line_height=None):
        try:
            stat = Path(path).stat() if path else None
            revision = (stat.st_size, stat.st_mtime_ns) if stat else None
        except OSError:
            revision = None
        image_key = (str(path), revision)
        identity = (image_key, tuple(bbox or ()), tuple(page_size))
        if identity == self._identity:
            return
        self._identity = identity
        self.scene().clear()
        self.resetTransform()
        if not path or not bbox or not all(math.isfinite(v) for v in bbox) or min(page_size) <= 0:
            self._image = QImage()
            self._path = None
            self.scene().setSceneRect(QRectF())
            return
        if image_key != self._path:
            self._image = image if image is not None and not image.isNull() else QImage(str(path))
            self._path = image_key
        if self._image.isNull():
            self.scene().setSceneRect(QRectF())
            return
        sx, sy = self._image.width() / page_size[0], self._image.height() / page_size[1]
        x1, y1, x2, y2 = bbox
        if x2 <= x1 or y2 <= y1:
            self.scene().setSceneRect(QRectF())
            return
        rect = QRectF(x1*sx-6, y1*sy-6, (x2-x1)*sx+12, (y2-y1)*sy+12).toAlignedRect()
        rect = rect.intersected(self._image.rect())
        if rect.isEmpty():
            self.scene().setSceneRect(QRectF())
            return
        crop = self._image.copy(rect)
        self._readable_scale = 18 / max(1, (line_height or min(28, y2-y1)) * sy)
        # The full page stays shared with the canvas. Bound the extra display pixmap.
        if max(crop.width(), crop.height()) > 2048:
            self._readable_scale *= max(crop.width(), crop.height()) / 2048
            crop = crop.scaled(2048, 2048, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        item = self.scene().addPixmap(QPixmap.fromImage(crop))
        item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self.scene().setSceneRect(item.boundingRect())
        self._fit = True
        self._readable = True
        self._fit_source()

    def fit_source(self):
        self._fit = True
        self._readable = False
        self._fit_source()

    def _fit_source(self):
        rect = self.sceneRect()
        if not rect.isEmpty():
            self.resetTransform()
            factor = min(2.0, max(1, self.viewport().width() - 4) / rect.width())
            if self._readable:
                # Preserve source-pixel readability; a long line can be panned.
                factor = max(min(2.0, self._readable_scale), factor)
            self.scale(factor, factor)
            self.horizontalScrollBar().setValue(0)
            self.verticalScrollBar().setValue(0)

    def zoom(self, factor):
        if self.sceneRect().isEmpty():
            return
        self._fit = False
        current = self.transform().m11()
        target = min(6.0, max(.08, current * factor))
        self.scale(target/current, target/current)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._fit:
            self._fit_source()


class ReviewRowDelegate(QStyledItemDelegate):
    """Two elided lines; full text remains available in the selected detail."""
    def sizeHint(self, option, index):
        return QSize(0, 68)

    def paint(self, painter, option, index):
        palette = colours(self.parent())
        heading, _, summary = (index.data() or '').partition('\n')
        painter.save()
        painter.setClipRect(option.rect)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if option.state & QStyle.StateFlag.State_Selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(palette['tint']))
            painter.drawRoundedRect(QRectF(option.rect).adjusted(1, 2, -1, -2), 8, 8)
        if option.state & QStyle.StateFlag.State_HasFocus:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(palette['primary']), 1))
            painter.drawRoundedRect(QRectF(option.rect).adjusted(1, 2, -1, -2), 8, 8)
        painter.setFont(option.font)
        metrics = painter.fontMetrics()
        rect = option.rect.adjusted(12, 8, -12, -8)
        painter.setPen(QColor(palette['text']))
        painter.drawText(rect, Qt.AlignmentFlag.AlignTop, metrics.elidedText(heading, Qt.TextElideMode.ElideRight, rect.width()))
        painter.setPen(QColor(palette['muted']))
        painter.drawText(rect, Qt.AlignmentFlag.AlignBottom, metrics.elidedText(summary, Qt.TextElideMode.ElideRight, rect.width()))
        painter.restore()
