"""Scene-based image inspection; saved geometry always uses original pixels."""
from pathlib import Path
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QImage, QImageReader, QPainter, QPen, QPixmap, QPolygonF, QTransform
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QGraphicsScene, QGraphicsView


class PhotoCanvas(QGraphicsView):
    selection_changed = Signal(tuple)
    perspective_changed = Signal(list)
    block_activated = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setObjectName('photoCanvas')
        self.setAccessibleName('图片画布')
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self._colours = {'stage': '#E9E6DD', 'primary': '#B03A26', 'warning': '#886713'}
        self.setBackgroundBrush(QColor(self._colours['stage']))
        self.setMinimumSize(280, 200)
        self._image = QImage()
        self._image_path = self._identity = self._item = None
        self._blocks = []
        self._selected_block_id = None
        self._mode = 'idle'
        self._points = []
        self._crop_start = self._crop_end = self._crop = None
        self._perspective = []
        self._rotation = 0
        self._fit = True
        self._paper_quad = None

    def set_image(self, path, *, frame_index=0):
        identity = (str(path), frame_index) if path else None
        if identity == self._identity:
            return not self._image.isNull()
        self._identity = identity
        self._image_path = Path(path) if path else None
        self.scene().clear()
        self._item = None
        self._image = QImage()
        self._blocks = []
        self._rotation = 0
        self._crop = None
        self._perspective = []
        self._paper_quad = None
        self.clear_interaction()
        if path:
            reader = QImageReader(str(path))
            reader.setAutoTransform(True)
            if frame_index and not reader.jumpToImage(frame_index):
                self._image = self._pillow_frame(path, frame_index)
            else:
                self._image = reader.read()
            if self._image.isNull():
                self._image = self._pillow_frame(path, frame_index)
        if not self._image.isNull():
            self._item = self.scene().addPixmap(QPixmap.fromImage(self._image))
            self._item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
            shadow = QGraphicsDropShadowEffect()
            shadow.setBlurRadius(20)
            shadow.setOffset(0, 3)
            shadow.setColor(QColor(0, 0, 0, 35))
            self._item.setGraphicsEffect(shadow)
            self.scene().setSceneRect(QRectF(self._image.rect()))
        else:
            self.scene().setSceneRect(QRectF())
        self.fit_image()
        return not self._image.isNull()

    @staticmethod
    def _pillow_frame(path, frame):
        try:
            from PIL import Image, ImageOps
            with Image.open(path) as source:
                source.seek(frame)
                value = ImageOps.exif_transpose(source).convert('RGBA')
                return QImage(value.tobytes(), value.width, value.height, value.width * 4, QImage.Format.Format_RGBA8888).copy()
        except Exception:
            return QImage()

    def set_adjustments(self, *, rotation=0, crop=None, perspective=None):
        self._rotation = rotation % 360
        self._crop = crop
        self._perspective = perspective or []
        self.fit_image()
        self.viewport().update()

    def set_paper_suggestion(self, quad):
        self._paper_quad = quad
        self.viewport().update()

    def set_blocks(self, blocks, selected_id=None):
        self._blocks = [(str(b.id), tuple(b.bbox)) for b in blocks]
        self._block_kinds = {str(b.id): b.kind for b in blocks}
        self._review_ids = {str(b.id) for b in blocks if b.needs_review or b.candidates or (b.confidence is not None and b.confidence < .9)}
        self._selected_block_id = selected_id
        self.viewport().update()

    def set_theme(self, colours):
        self._colours = colours
        self.setBackgroundBrush(QBrush(Qt.BrushStyle.NoBrush))
        self.viewport().setAutoFillBackground(False)
        self.viewport().update()

    def set_selected_block(self, block_id):
        self._selected_block_id = block_id
        self.viewport().update()

    def set_interaction_mode(self, mode):
        if mode not in {'idle', 'crop', 'perspective'}:
            raise ValueError('invalid canvas mode')
        self.clear_interaction()
        self._mode = mode
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag if mode == 'idle' else QGraphicsView.DragMode.NoDrag)
        self.viewport().setCursor(Qt.CursorShape.OpenHandCursor if mode == 'idle' else Qt.CursorShape.CrossCursor)

    def clear_interaction(self):
        self._mode = 'idle'
        self._points = []
        self._crop_start = self._crop_end = None
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.viewport().update()

    def display_to_image(self, point):
        if self._image.isNull():
            return None
        inverse, valid = self.viewportTransform().inverted()
        if not valid:
            return None
        value = inverse.map(QPointF(point))
        if not QRectF(self._image.rect()).contains(value):
            return None
        return QPointF(max(0, min(self._image.width()-1, value.x())), max(0, min(self._image.height()-1, value.y())))

    def image_to_display(self, point):
        return self.viewportTransform().map(QPointF(point)) if not self._image.isNull() else None

    def fit_image(self):
        self._fit = True
        self.setTransform(QTransform().rotate(self._rotation))
        if self._item:
            self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)
            self.scale(.94, .94)

    def fit_width(self):
        """Reading view: retain source pixels and use scrollbars for a tall page."""
        self._fit = 'width'
        self.setTransform(QTransform().rotate(self._rotation))
        if self._item:
            bounds=self.transform().mapRect(self._item.boundingRect())
            factor=max(.01,min(620,self.viewport().width()-64)/max(1,bounds.width()))
            self.scale(factor,factor)
            self.centerOn(self._item.boundingRect().center().x(),self.viewport().height()/(2*factor))

    def zoom_in(self):
        self._zoom(1.2)

    def zoom_out(self):
        self._zoom(1/1.2)

    def _zoom(self, factor):
        current = (self.transform().m11()**2 + self.transform().m12()**2)**0.5
        if 0.015 <= current * factor <= 8:
            self._fit = False
            self.scale(factor, factor)

    def reveal_bbox(self, bbox):
        self.ensureVisible(QRectF(QPointF(bbox[0], bbox[1]), QPointF(bbox[2], bbox[3])), 20, 20)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._fit == 'width':
            self.fit_width()
        elif self._fit:
            self.fit_image()

    def wheelEvent(self, event):
        self._zoom(1.2 if event.angleDelta().y() > 0 else 1/1.2)
        event.accept()

    def mousePressEvent(self, event):
        point = self.display_to_image(event.position())
        if point is not None and event.button() == Qt.MouseButton.LeftButton:
            if self._mode == 'crop':
                self._crop_start = self._crop_end = point
                event.accept()
                return
            if self._mode == 'perspective':
                self._points.append(point)
                if len(self._points) == 4:
                    points = [(round(p.x(), 2), round(p.y(), 2)) for p in self._points]
                    self._points = []
                    self.perspective_changed.emit(points)
                self.viewport().update()
                event.accept()
                return
            for block_id, bbox in reversed(self._blocks):
                if QRectF(QPointF(bbox[0], bbox[1]), QPointF(bbox[2], bbox[3])).contains(point):
                    self.block_activated.emit(block_id)
                    break
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._mode == 'crop' and self._crop_start is not None:
            point = self.display_to_image(event.position())
            if point is not None:
                self._crop_end = point
            self.viewport().update()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._mode == 'crop' and self._crop_start is not None and event.button() == Qt.MouseButton.LeftButton:
            point = self.display_to_image(event.position()) or self._crop_end
            rect = QRectF(self._crop_start, point).normalized()
            self._crop_start = self._crop_end = None
            if rect.width() >= 2 and rect.height() >= 2:
                self.selection_changed.emit((rect.left(), rect.top(), rect.right(), rect.bottom()))
            self.viewport().update()
            return
        super().mouseReleaseEvent(event)

    def drawForeground(self, painter, rect):
        super().drawForeground(painter, rect)
        painter.save()
        if self._paper_quad and not self._crop and not self._perspective:
            suggestion_pen = QPen(QColor(self._colours['primary']), 2, Qt.PenStyle.DashLine)
            suggestion_pen.setCosmetic(True)
            painter.setPen(suggestion_pen)
            colour = QColor(self._colours['primary'])
            colour.setAlpha(15)
            painter.setBrush(colour)
            painter.drawPolygon(QPolygonF([QPointF(*point) for point in self._paper_quad]))
        pen = QPen(QColor(self._colours['primary']), 2)
        pen.setCosmetic(True)
        painter.setPen(pen)
        fill = QColor(self._colours['primary'])
        fill.setAlpha(20)
        painter.setBrush(fill)
        for block_id, bbox in self._blocks:
            if block_id == self._selected_block_id or block_id in getattr(self, '_review_ids', set()):
                colour = self._colours['primary'] if block_id == self._selected_block_id else self._colours['warning']
                overlay_pen = QPen(QColor(colour), 2 if block_id == self._selected_block_id else 1)
                overlay_pen.setCosmetic(True)
                painter.setPen(overlay_pen)
                painter.drawRect(QRectF(QPointF(bbox[0], bbox[1]), QPointF(bbox[2], bbox[3])))
        painter.setPen(pen)
        if self._crop:
            painter.drawRect(QRectF(QPointF(*self._crop[:2]), QPointF(*self._crop[2:])))
        if self._crop_start is not None and self._crop_end is not None:
            painter.drawRect(QRectF(self._crop_start, self._crop_end).normalized())
        points = self._points or [QPointF(*p) for p in self._perspective]
        if points:
            if len(points) == 4:
                painter.drawPolygon(QPolygonF(points))
            else:
                painter.drawPolyline(QPolygonF(points))
            scale = max(0.01, (self.transform().m11()**2 + self.transform().m12()**2)**0.5)
            painter.setBrush(QColor(self._colours['primary']))
            for point in points:
                painter.drawEllipse(point, 4/scale, 4/scale)
        painter.restore()
