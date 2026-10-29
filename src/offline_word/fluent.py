"""Windows composition and bounded, presentation-only Fluent effects.

No OCR geometry, document contents or filesystem snapshots live here. Glass
uses DWM on supported Windows versions; in-app blur is captured only on opening
a panel. Animations release their pixmaps when finished and never run at idle.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import sys

from PySide6.QtCore import (
    QEasingCurve, QEvent, QObject, QPoint, QRect, QRectF, Qt, QTimer,
    QVariantAnimation,
)
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap, QTransform
from PySide6.QtWidgets import QApplication, QFrame, QMainWindow, QWidget


@dataclass(frozen=True)
class DesktopEffects:
    native: bool = False
    glass: bool = True
    motion: bool = True


def desktop_effects() -> DesktopEffects:
    """Respect accessibility, remote sessions, transparency and battery saver."""
    if sys.platform != 'win32' or QApplication.platformName() != 'windows':
        return DesktopEffects()
    try:
        import winreg
        user = ctypes.windll.user32

        class HighContrast(ctypes.Structure):
            _fields_ = [('size', wintypes.UINT), ('flags', wintypes.DWORD), ('scheme', wintypes.LPWSTR)]

        contrast = HighContrast(ctypes.sizeof(HighContrast), 0, None)
        animation = wintypes.BOOL()
        if not user.SystemParametersInfoW(0x42, contrast.size, ctypes.byref(contrast), 0):
            return DesktopEffects(False, False, False)
        if not user.SystemParametersInfoW(0x1042, 0, ctypes.byref(animation), 0):
            return DesktopEffects(False, False, False)
        transparent = True
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as key:
                transparent = bool(winreg.QueryValueEx(key, 'EnableTransparency')[0])
        except FileNotFoundError:
            pass

        class PowerStatus(ctypes.Structure):
            _fields_ = [('ac', wintypes.BYTE), ('battery', wintypes.BYTE),
                        ('percent', wintypes.BYTE), ('saver', wintypes.BYTE),
                        ('life', wintypes.DWORD), ('full_life', wintypes.DWORD)]

        power = PowerStatus()
        saver = bool(ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(power)) and power.saver == 1)
        restricted = bool(contrast.flags & 1 or user.GetSystemMetrics(0x1000) or saver)
        native = sys.getwindowsversion().build >= 22621 and not restricted
        return DesktopEffects(native, transparent and not restricted, bool(animation.value) and not restricted)
    except (AttributeError, OSError, ValueError):
        return DesktopEffects(False, False, False)


def resize_hit(x, y, width, height, border):
    """Win32 non-client hit codes, in physical pixels (including negative monitors)."""
    left, right = x < border, x >= width - border
    top, bottom = y < border, y >= height - border
    if top:
        return 13 if left else 14 if right else 12
    if bottom:
        return 16 if left else 17 if right else 15
    return 10 if left else 11 if right else 0


class FluentWindow(QMainWindow):
    """An OS-managed resizable window with an app-owned title bar on Windows."""
    def __init__(self):
        super().__init__()
        self._native_frame = sys.platform == 'win32' and QApplication.platformName() == 'windows'
        self._effects = desktop_effects()
        self._dwm_result = 'opaque'
        if self._native_frame:
            self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
            # Set once, before the HWND is created; never toggle a shown window.
            # A layered (WA_TranslucentBackground) Qt HWND masks the DWM system
            # backdrop. Keep the normal backing store and let DWM compose it.
            self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)

    def apply_backdrop(self):
        self._effects = desktop_effects()
        enabled = self._native_frame and self._effects.native and self._effects.glass and self.property('glassEnabled') is not False
        if not self._native_frame:
            return False
        hwnd = wintypes.HWND(int(self.winId()))
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long

        def attribute(number, value):
            data = ctypes.c_int(value)
            return dwm.DwmSetWindowAttribute(hwnd, number, ctypes.byref(data), ctypes.sizeof(data))

        # Native style keeps resizing, taskbar, Win+arrow and OS snapping intact.
        user = ctypes.windll.user32
        get_style, set_style = user.GetWindowLongPtrW, user.SetWindowLongPtrW
        get_style.argtypes = [wintypes.HWND, ctypes.c_int]
        get_style.restype = ctypes.c_ssize_t
        set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        set_style.restype = ctypes.c_ssize_t
        style = get_style(hwnd, -16)
        # Keep resize / system menu / minimize / maximize, but not WS_CAPTION:
        # native caption glyphs would otherwise show through our glass title bar.
        desired = (style | 0x000F0000) & ~0x00C00000
        if style != desired:
            set_style(hwnd, -16, desired)
            user.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0037)
        attribute(20, int(self.property('appearance') == 'dark'))
        attribute(33, 2)  # DWM rounded corners; unsupported versions ignore it.

        class Margins(ctypes.Structure):
            _fields_ = [('left', ctypes.c_int), ('right', ctypes.c_int),
                        ('top', ctypes.c_int), ('bottom', ctypes.c_int)]

        # DWMSBT_TRANSIENTWINDOW is the documented desktop Acrylic material.
        result = attribute(38, 3 if enabled else 1)
        margins = Margins(*([-1] * 4 if enabled and result == 0 else [1] * 4))
        dwm.DwmExtendFrameIntoClientArea.argtypes = [wintypes.HWND, ctypes.POINTER(Margins)]
        extension = dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))
        active = enabled and result == 0 and extension == 0
        if not active:
            attribute(38, 1)
        self._dwm_result = 'desktop-acrylic' if active else 'opaque'
        self.setProperty('nativeGlass', active)
        if hasattr(self, 'workspace_root'):
            self.workspace_root.update()
        return active

    def showEvent(self, event):
        super().showEvent(event)
        self.apply_backdrop()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            from .theme import update_caption_icon
            update_caption_icon(self)
        if event.type() in (QEvent.Type.WindowStateChange, QEvent.Type.ActivationChange):
            if hasattr(self, 'motion') and (self.isMinimized() or not self.isActiveWindow()):
                self.motion.stop()

    def nativeEvent(self, event_type, message):
        if self._native_frame and event_type == b'windows_generic_MSG':
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0x83 and msg.wParam:  # WM_NCCALCSIZE
                if self.isMaximized():
                    rect = wintypes.RECT.from_address(msg.lParam)
                    border = round(8 * self.devicePixelRatioF())
                    rect.left += border
                    rect.top += border
                    rect.right -= border
                    rect.bottom -= border
                return True, 0
            if msg.message == 0x84 and hasattr(self, 'title_bar'):  # WM_NCHITTEST
                rect = wintypes.RECT()
                get_rect=ctypes.windll.user32.GetWindowRect
                get_rect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
                get_rect.restype=wintypes.BOOL
                if not get_rect(msg.hWnd, ctypes.byref(rect)):
                    return super().nativeEvent(event_type,message)
                x = ctypes.c_short(msg.lParam & 0xffff).value - rect.left
                y = ctypes.c_short((msg.lParam >> 16) & 0xffff).value - rect.top
                if not self.isMaximized() and not self.isFullScreen():
                    hit = resize_hit(x, y, rect.right-rect.left, rect.bottom-rect.top,
                                     max(6, round(6*self.devicePixelRatioF())))
                    if hit:
                        return True, hit
                point = QPoint(round(x/self.devicePixelRatioF()), round(y/self.devicePixelRatioF()))
                child = self.childAt(point)
                if child is self.maximize_button:
                    return True, 9  # Native maximize hit target also supports Win11 Snap.
                if child is self.title_bar or (child is not None and child.property('captionDrag')):
                    return True, 2
            if msg.message in (0x1A, 0x31E):  # settings/composition changed
                QTimer.singleShot(0, self._refresh_desktop_effects)
        return super().nativeEvent(event_type, message)

    def _refresh_desktop_effects(self):
        self.apply_backdrop()
        if hasattr(self, 'motion'):
            self.motion.refresh()
        if hasattr(self, 'drawer'):
            self.drawer.clear_backdrop()


class MaterialSurface(QWidget):
    """Subtle tint above DWM; deterministic opaque material when DWM is absent."""
    def paintEvent(self, event):
        from .theme import colours
        palette = colours(self)
        paint = QPainter(self)
        tint = QColor(palette['window'])
        if self.window().property('nativeGlass'):
            tint.setAlpha(150)
        # Replace alpha as well as color. Source-over on the previous opaque
        # appearance permanently hides DWM after toggling glass off and on.
        paint.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        paint.fillRect(self.rect(), tint)
        paint.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        paint.setPen(QPen(QColor(palette['rim']), 1))
        paint.drawRect(self.rect().adjusted(0, 0, -1, -1))


class TransitionLayer(QWidget):
    """Short perspective reveal of one cached image; input never gets transformed."""
    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self._image = QPixmap()
        self._amount = 0.0
        self.hide()

    def paintEvent(self, event):
        if self._image.isNull():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setOpacity(1-self._amount)
        transform = QTransform()
        transform.translate(self.width()/2, self.height()/2)
        transform.rotate(-3.5*self._amount, Qt.Axis.YAxis)
        transform.translate(-self.width()/2+20*self._amount, -self.height()/2)
        painter.setTransform(transform)
        painter.drawPixmap(self.rect(), self._image)

    def clear(self):
        self.hide()
        self._image = QPixmap()


class MotionController(QObject):
    def __init__(self, window, surface):
        super().__init__(window)
        self.window = window
        self.surface = surface
        self.layer = TransitionLayer(surface)
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(300)
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.animation.valueChanged.connect(self._step)
        self.animation.finished.connect(self.layer.clear)
        self._last_frame = -50
        surface.installEventFilter(self)

    @property
    def enabled(self):
        return (self.window.property('motionEnabled') is not False and self.window._effects.motion
                and self.window.isVisible() and not self.window.isMinimized()
                and getattr(self.window, '_worker', None) is None)

    def refresh(self):
        if not self.enabled:
            self.stop()

    def capture(self):
        self.stop()
        if not self.enabled or self.surface.width() < 10:
            return
        # Preserve native pixels: reducing a 175% screenshot to 100% visibly
        # softens every glyph during transitions. Skip very large captures
        # instead of shrinking text; the buffer only lives for this transition.
        if self.surface.width()*self.surface.height()*self.surface.devicePixelRatioF()**2 > 8_000_000:
            return
        snapshot = self.surface.grab()
        self.layer._image = snapshot

    def reveal(self):
        if self.layer._image.isNull() or not self.enabled:
            self.stop()
            return
        self.layer.setGeometry(self.surface.rect())
        self.layer._amount = 0
        self.layer.show()
        self.layer.raise_()
        self._last_frame = -50
        self.animation.start()

    def _step(self, amount):
        if self.animation.currentTime()-self._last_frame >= 32:
            self._last_frame = self.animation.currentTime()
            self.layer._amount = amount
            self.layer.update()

    def stop(self):
        self.animation.stop()
        self.layer.clear()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Hide, QEvent.Type.MouseButtonPress):
            self.stop()
        return False


class GlassDrawer(QFrame):
    """A bounded contextual surface: cached blur, no persistent background loop."""
    def __init__(self, window, parent):
        super().__init__(parent)
        self.owner = window
        self._backdrop = QImage()
        self.setObjectName('glassDrawer')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.hide()
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(260)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.valueChanged.connect(self._move_frame)
        parent.installEventFilter(self)
        self._target = QRect()

    def desired_geometry(self):
        area = self.parentWidget().rect().adjusted(12, 12, -12, -12)
        if self.owner.width() < 1100 and self.parentWidget().currentWidget() is not self.owner.document_view:
            # Keep navigation above and below the compact panel reachable.
            area.adjust(0, 52, 0, -48)
        width = min(390 if self.owner.width() >= 1280 else 350, area.width())
        height = area.height()
        return QRect(area.right()-width+1, area.top(), width, height)

    def open_panel(self):
        if self.isVisible():
            self.raise_()
            return
        self.animation.stop()
        self._target = self.desired_geometry()
        self.setGeometry(self._target)
        self._reserve_document_space(True)
        self.parentWidget().currentWidget().layout().activate()
        self._capture_backdrop()
        self.show()
        self.raise_()
        if self.owner.motion.enabled:
            self.animation.setStartValue(32.0)
            self.animation.setEndValue(0.0)
            self.animation.start()

    def close_panel(self):
        self.animation.stop()
        self.hide()
        self.clear_backdrop()
        self._reserve_document_space(False)

    def _reserve_document_space(self, opened):
        # Keep the selected text beside the tools instead of underneath them.
        # Compact displays share the toolbar/navigation but reserve the canvas.
        compact = self.owner.width() < 1100
        width = self.desired_geometry().width()+18 if opened else 0
        for control in (*self.owner.document_fit_controls, self.owner.details_button):
            control.setVisible(not (compact and opened))
        for name in ('document_splitter', 'document_page_bar', 'document_toolbar',
                     'source_stack', 'image_tools', 'image_footer', 'preview_toolbar', 'preview_surface'):
            widget = getattr(self.owner, name, None)
            if widget is not None:
                reserve = width
                if compact and name in ('image_tools', 'image_footer', 'preview_toolbar'):
                    reserve = 0
                widget.setContentsMargins(0, 0, reserve, 0)

    def _move_frame(self, offset):
        self.move(self._target.topLeft()+QPoint(round(offset), 0))

    def _capture_backdrop(self):
        self.clear_backdrop()
        if self.owner.property('glassEnabled') is False or not self.owner._effects.glass:
            return
        from PIL import Image, ImageFilter
        # Read only the app-owned panel footprint, never the desktop or other apps.
        shot = self.parentWidget().grab(self._target).toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        if shot.isNull():
            return
        value = Image.frombytes('RGBA', (shot.width(), shot.height()), bytes(shot.constBits()),
                                'raw', 'RGBA', shot.bytesPerLine())
        value.thumbnail((200, 360))
        value = value.filter(ImageFilter.GaussianBlur(8))
        self._backdrop = QImage(value.tobytes(), value.width, value.height, value.width*4,
                                QImage.Format.Format_RGBA8888).copy()

    def clear_backdrop(self):
        self._backdrop = QImage()
        self.update()

    def paintEvent(self, event):
        from .theme import colours
        palette = colours(self)
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        rect = QRectF(self.rect()).adjusted(.5,.5,-.5,-.5)
        from PySide6.QtGui import QPainterPath
        path = QPainterPath()
        path.addRoundedRect(rect, 8, 8)
        painter.setClipPath(path)
        tint = QColor(palette['surface'])
        if not self._backdrop.isNull():
            painter.drawImage(self.rect(), self._backdrop)
            tint.setAlpha(222)
        painter.fillPath(path, tint)
        painter.setPen(QPen(QColor(palette['border']), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, 8, 8)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Resize and self.isVisible():
            self.animation.stop()
            self._target = self.desired_geometry()
            self.setGeometry(self._target)
            self._reserve_document_space(True)
            self.clear_backdrop()
        return False
