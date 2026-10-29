"""Animated, local-only release welcome and browsable history."""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPoint, QRect, QRectF, QSettings, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QScrollArea, QStackedWidget, QVBoxLayout, QWidget

from .fluent import MotionController
from .releases import __version__, load_releases
from .theme import colours, icon


class ReleasePreferences:
    def __init__(self, settings=None):
        self.settings = settings if settings is not None else QSettings("OfflineWord", "ReleaseHistory")

    def has_seen(self, version=__version__):
        return str(self.settings.value(f"seen/{version}", "false")).lower() in {"true", "1"}

    def mark_seen(self, version=__version__):
        self.settings.setValue(f"seen/{version}", True)
        self.settings.sync()


def show_release_dialog(window, *, welcome=False, preferences=None):
    """One dialog per window; startup is opt-in at the actual GUI entry point."""
    existing = getattr(window, "release_dialog", None)
    if existing is not None and existing.isVisible():
        existing.raise_()
        existing.activateWindow()
        return existing
    preferences = preferences if preferences is not None else ReleasePreferences()
    if welcome and preferences.has_seen():
        return None
    dialog = ReleaseDialog(window, welcome=welcome, preferences=preferences)
    window.release_dialog = dialog
    dialog.finished.connect(lambda: setattr(window, "release_dialog", None))
    dialog.open()
    return dialog


def _label(text, name, *, size=None):
    control = QLabel(text)
    control.setTextFormat(Qt.TextFormat.PlainText)
    control.setObjectName(name)
    control.setWordWrap(True)
    if size:
        control.setStyleSheet(f"font-size: {size}px; font-weight: 500;")
    return control


class ReleaseDialog(QDialog):
    def __init__(self, owner, *, welcome=False, preferences=None):
        super().__init__(owner, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.owner = owner
        self.welcome = welcome
        self.preferences = preferences if preferences is not None else ReleasePreferences()
        self.releases = load_releases()
        self._shown = False
        self._backdrop = QImage()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setWindowTitle("版本更新" if welcome else "更新日志")
        self.setAccessibleName(self.windowTitle())
        self.setFixedSize(640, 530)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 24, 36, 26)
        layout.setSpacing(20)
        header = QHBoxLayout()
        header.addWidget(_label("离线 OCR", "caption"), 1)
        self.close_button = QPushButton()
        self.close_button.setObjectName("quiet")
        self.close_button.setIcon(icon("x", colours(owner)["text"]))
        self.close_button.setAccessibleName("关闭更新日志")
        self.close_button.setToolTip("关闭")
        self.close_button.setFixedSize(32, 32)
        self.close_button.clicked.connect(self.reject)
        header.addWidget(self.close_button)
        layout.addLayout(header)
        self.pages = QStackedWidget()
        intro = QWidget()
        intro_layout = QVBoxLayout(intro)
        intro_layout.setContentsMargins(0, 12, 0, 12)
        intro_layout.setSpacing(12)
        intro_layout.addStretch()
        intro_layout.addWidget(_label("已更新至", "releaseHeading", size=28))
        self.version_label = _label(__version__, "releaseVersion", size=66)
        self.version_label.setStyleSheet(f"font-size: 66px; font-weight: 500; color: {colours(owner)['primary']};")
        intro_layout.addWidget(self.version_label)
        intro_layout.addWidget(_label(next(item.title for item in self.releases if item.version == __version__), "muted", size=17))
        intro_layout.addStretch()
        self.pages.addWidget(intro)
        history = QWidget()
        history_layout = QVBoxLayout(history)
        history_layout.setContentsMargins(0, 0, 0, 0)
        history_layout.setSpacing(16)
        title_row = QHBoxLayout()
        title_row.addWidget(_label("更新日志", "releaseHeading", size=28), 1)
        self.version_combo = QComboBox()
        self.version_combo.setAccessibleName("查看历史版本更新日志")
        for release in self.releases:
            self.version_combo.addItem(f"{release.version}" + (" · 当前版本" if release.version == __version__ else ""), release.version)
        self.version_combo.setCurrentIndex(self.version_combo.findData(__version__))
        title_row.addWidget(self.version_combo)
        history_layout.addLayout(title_row)
        self.notes_title = _label("", "paneTitle", size=17)
        self.notes_date = _label("", "caption")
        history_layout.addWidget(self.notes_title)
        history_layout.addWidget(self.notes_date)
        self.notes_scroll = QScrollArea()
        self.notes_scroll.setWidgetResizable(True)
        self.notes_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.notes_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        history_layout.addWidget(self.notes_scroll, 1)
        self.pages.addWidget(history)
        layout.addWidget(self.pages, 1)
        footer = QHBoxLayout()
        self.step_label = _label("", "caption")
        footer.addWidget(self.step_label, 1)
        self.back_button = QPushButton("返回")
        self.back_button.setObjectName("quiet")
        self.back_button.clicked.connect(lambda: self.set_page(0))
        footer.addWidget(self.back_button)
        self.next_button = QPushButton()
        self.next_button.setObjectName("primary")
        self.next_button.setMinimumWidth(142)
        self.next_button.setDefault(True)
        self.next_button.clicked.connect(self.advance)
        footer.addWidget(self.next_button)
        layout.addLayout(footer)
        self.motion = MotionController(owner, self.pages)
        self.motion.setParent(self)
        self.entrance = QVariantAnimation(self)
        self.entrance.setDuration(260)
        self.entrance.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.entrance.setStartValue(1.0)
        self.entrance.setEndValue(0.0)
        self.entrance.valueChanged.connect(self._enter_frame)
        self.entrance.finished.connect(lambda: self.setWindowOpacity(1))
        self.version_combo.currentIndexChanged.connect(self._select_release)
        # Qt's popup release guard can suppress a valid click just after
        # opening the list, particularly above another modal dialog. Commit
        # the view's validated item click; keyboard selection stays native.
        self.version_combo.view().clicked.connect(self._activate_release)
        self._select_release()
        self.set_page(0 if welcome else 1, animate=False)
        for control in self.findChildren(QWidget):
            control.ensurePolished()
            font = control.font()
            font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
            control.setFont(font)

    def _activate_release(self, index):
        required = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if not index.isValid() or index.flags() & required != required:
            return
        self.version_combo.setCurrentIndex(index.row())
        self.version_combo.hidePopup()

    def _select_release(self):
        release = next(item for item in self.releases if item.version == self.version_combo.currentData())
        self.notes_title.setText(release.title)
        self.notes_date.setText(release.date.replace("-", "."))
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 12, 0)
        layout.setSpacing(14)
        for index, change in enumerate(release.changes, 1):
            row = QHBoxLayout()
            row.setSpacing(12)
            number = _label(f"{index:02d}", "caption")
            number.setFixedWidth(24)
            row.addWidget(number, 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(_label(change, "releaseChange"), 1)
            layout.addLayout(row)
        layout.addStretch()
        self.notes_scroll.setWidget(content)

    def set_page(self, index, *, animate=True):
        if animate:
            self.motion.capture()
        self.pages.setCurrentIndex(index)
        self.step_label.setText(f"{index + 1} / 2" if self.welcome else "本地版本记录")
        self.back_button.setVisible(self.welcome and index == 1)
        self.next_button.setText("查看更新日志" if index == 0 else "完成" if self.welcome else "完成")
        if animate:
            self.motion.reveal()
        self.next_button.setFocus()

    def advance(self):
        if self.pages.currentIndex() == 0:
            self.set_page(1)
        else:
            self.accept()

    def showEvent(self, event):
        self._target_position = self.owner.mapToGlobal(self.owner.rect().center()) - self.rect().center()
        self.move(self._target_position)
        self._capture_backdrop()
        super().showEvent(event)
        self._shown = True
        if self.owner.motion.enabled:
            self.entrance.start()

    def _enter_frame(self, offset):
        self.move(self._target_position + QPoint(0, round(18 * offset)))
        self.setWindowOpacity(1 - .24 * offset)

    def _capture_backdrop(self):
        if self.owner.property("glassEnabled") is False or not self.owner._effects.glass:
            return
        from PIL import Image, ImageFilter
        area = QRect(self.owner.mapFromGlobal(self._target_position), self.size())
        shot = self.owner.grab(area).toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        if shot.isNull():
            return
        value = Image.frombytes("RGBA", (shot.width(), shot.height()), bytes(shot.constBits()), "raw", "RGBA", shot.bytesPerLine())
        value.thumbnail((320, 265))
        value = value.filter(ImageFilter.GaussianBlur(12))
        self._backdrop = QImage(value.tobytes(), value.width, value.height, value.width * 4, QImage.Format.Format_RGBA8888).copy()

    def paintEvent(self, event):
        palette = colours(self.owner)
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(rect, 12, 12)
        painter.setClipPath(path)
        tint = QColor(palette["surface"])
        if not self._backdrop.isNull():
            painter.drawImage(self.rect(), self._backdrop)
            tint.setAlpha(226)
        painter.fillPath(path, tint)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(palette["border"]), 1))
        painter.drawPath(path)

    def done(self, result):
        self.entrance.stop()
        self.motion.stop()
        self.setWindowOpacity(1)
        self._backdrop = QImage()
        if self.welcome and self._shown:
            self.preferences.mark_seen()
        super().done(result)
