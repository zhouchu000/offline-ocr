"""Fluent light/dark materials and local-only appearance preferences."""
from pathlib import Path
import re

from PySide6.QtCore import QByteArray, QSettings, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QAbstractButton, QComboBox, QLabel

ASSETS = Path(__file__).parent / 'assets'
LIGHT = dict(window='#EFF2F5', surface='#FCFDFE', stage='#F5F6F8', veil='rgba(245,246,248,100)', subtle='#EBEFF3',
             text='#19212C', secondary='#354150', muted='#536071', border='#DDE2E8',
             primary='#0067C0', hover='#1479CB', tint='#DCEBFA', selected_border='#0067C0',
             success='#28714A', success_bg='#E8F3ED', warning='#826013', warning_bg='#FFF2D5',
             error='#B4232F', disabled='#8190A2', disabled_bg='#E7EDF4', selection='#CDE5FA',
             scrollbar='#BAC7D8', on_primary='#FFFFFF', rim='#F9FCFF')
DARK = dict(window='#262B33', surface='#2E343E', stage='#1D2229', veil='rgba(29,34,41,100)', subtle='#343C48',
            text='#F2F4F8', secondary='#CAD1DD', muted='#ABB6C5', border='#424C5B',
            primary='#78C7FF', hover='#9DD6FF', tint='#284965', selected_border='#78C7FF',
            success='#94D6B1', success_bg='#233E35', warning='#F0CD82', warning_bg='#463A26',
            error='#FFACA6', disabled='#91A0B5', disabled_bg='#303E53', selection='#315C7E',
            scrollbar='#52647C', on_primary='#102A43', rim='#4C5D75')
PRIMARY_COLOR = LIGHT['primary']
SUCCESS_COLOR = LIGHT['success']
WARNING_COLOR = LIGHT['warning']
FAILURE_COLOR = LIGHT['error']
SECONDARY_TEXT_COLOR = LIGHT['muted']
_icons = {}
_font_id = None
_font_ids = []


def load_ui_font():
    global _font_id
    if _font_id is None:
        for name in ('MiSans-Regular.ttf', 'MiSans-Medium.ttf', 'NotoSansSC.ttf'):
            path = ASSETS / name
            if path.is_file():
                font_id = QFontDatabase.addApplicationFont(str(path))
                if font_id >= 0:
                    _font_ids.append(font_id)
        _font_id = _font_ids[0] if _font_ids else -1
    return _font_id


def colours(widget=None):
    while widget is not None:
        mode = widget.property('appearance')
        if mode in ('light', 'dark'):
            return DARK if mode == 'dark' else LIGHT
        widget = widget.parentWidget()
    return LIGHT


def icon(name, colour=None):
    """Use supplied Penpot vectors and Microsoft Fluent caption icons."""
    key = (name, colour)
    if key in _icons:
        return _icons[key]
    path = ASSETS / (f'{name}.svg' if name.startswith('fluent-') else f'penpot-{name}.svg')
    if not path.is_file():
        return QIcon()
    source = path.read_text(encoding='utf-8')
    if colour:
        source = re.sub(r'#[0-9a-fA-F]{6}', colour, source)
    renderer = QSvgRenderer(QByteArray(source.encode('utf-8')))
    result = QIcon()
    for size in (16, 20, 24, 28, 32, 48, 64):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        result.addPixmap(pixmap)
    _icons[key] = result
    return result


def application_icon():
    return icon('scan', LIGHT['primary'])


def asset_pixmap(name, size, density=1):
    renderer = QSvgRenderer(str(ASSETS / f'penpot-{name}.svg'))
    pixmap = QPixmap(round(size.width()*density), round(size.height()*density))
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(density)
    return pixmap


def saved_appearance():
    value = QSettings('OfflineWord', 'Appearance').value('theme', 'light')
    return value if value in ('light', 'dark') else 'light'


def update_caption_icon(window):
    if not hasattr(window, 'maximize_button'):
        return
    name = 'fluent-restore' if window.isMaximized() else 'fluent-maximize'
    window.maximize_button.setIcon(icon(name, colours(window)['text']))


def saved_effect(name):
    if name not in ('motion', 'glass'):
        raise ValueError('invalid effect')
    return str(QSettings('OfflineWord', 'Appearance').value(name, 'true')).lower() in ('true', '1')


def set_effect(window, name, enabled, *, persist=True):
    if name not in ('motion', 'glass'):
        raise ValueError('invalid effect')
    window.setProperty(name+'Enabled', bool(enabled))
    if name == 'motion':
        window.motion.refresh()
        window.motion_button.setChecked(bool(enabled))
        if not enabled:
            window.drawer.animation.stop()
            if window.drawer.isVisible():
                window.drawer.setGeometry(window.drawer.desired_geometry())
    else:
        window.apply_backdrop()
        window.drawer.clear_backdrop()
        window.glass_action.setChecked(bool(enabled))
    if persist:
        settings = QSettings('OfflineWord', 'Appearance')
        settings.setValue(name, bool(enabled))
        settings.sync()


def set_appearance(window, mode, *, persist=True):
    if mode not in ('light', 'dark'):
        raise ValueError('invalid appearance')
    window.setProperty('appearance', mode)
    palette = DARK if mode == 'dark' else LIGHT
    font = QFont('MiSans')
    font.setPixelSize(14)
    font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    window.setFont(font)
    window.setStyleSheet(stylesheet(palette))
    qt_palette = QPalette()
    for role, key in [(QPalette.ColorRole.Window, 'surface'), (QPalette.ColorRole.WindowText, 'text'),
                      (QPalette.ColorRole.Base, 'surface'), (QPalette.ColorRole.AlternateBase, 'subtle'),
                      (QPalette.ColorRole.Text, 'text'), (QPalette.ColorRole.Button, 'surface'),
                      (QPalette.ColorRole.ButtonText, 'text'), (QPalette.ColorRole.Highlight, 'selection'),
                      (QPalette.ColorRole.HighlightedText, 'text'), (QPalette.ColorRole.ToolTipBase, 'surface'),
                      (QPalette.ColorRole.ToolTipText, 'text'), (QPalette.ColorRole.PlaceholderText, 'muted')]:
        qt_palette.setColor(role, QColor(palette[key]))
    qt_palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(palette['disabled']))
    window.setPalette(qt_palette)
    # Apply after QSS has resolved its font, otherwise it resets this preference.
    for kind in (QAbstractButton, QComboBox, QLabel):
        for control in window.findChildren(kind):
            control.ensurePolished()
            font = control.font()
            font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
            control.setFont(font)
    for control in window.findChildren(QAbstractButton):
        if name := control.property('iconName'):
            tone = 'on_primary' if control.objectName() == 'primary' else 'text'
            control.setIcon(icon(name, palette[tone]))
    if hasattr(window, 'import_more_button'):
        mode_asset = ASSETS / ('chevron-dark.svg' if mode == 'dark' else 'chevron-light.svg')
        window.import_more_button.setIcon(QIcon(str(mode_asset)))
    if hasattr(window, 'theme_selector'):
        window.theme_selector.blockSignals(True)
        window.theme_selector.setCurrentIndex(window.theme_selector.findData(mode))
        window.theme_selector.blockSignals(False)
    update_caption_icon(window)
    for name in ('source_preview', 'processed_preview'):
        canvas = getattr(window, name, None)
        if canvas:
            canvas.set_theme(palette)
    if hasattr(window, 'preview_canvas'):
        window.preview_canvas.set_theme(palette)
    if hasattr(window, 'theme_button'):
        window.theme_button.setChecked(mode == 'dark')
        window.theme_button.setText('切换为浅色' if mode == 'dark' else '切换为深色')
        window.theme_button.setAccessibleName('切换至浅色模式' if mode == 'dark' else '切换至深色模式')
    if hasattr(window, 'queue_list'):
        window.queue_list.viewport().update()
    if hasattr(window, 'drawer'):
        window.drawer.clear_backdrop()
    if hasattr(window, 'motion'):
        window.motion.stop()
    if hasattr(window, 'apply_backdrop') and window.isVisible():
        window.apply_backdrop()
    if persist:
        settings = QSettings('OfflineWord', 'Appearance')
        settings.setValue('theme', mode)
        settings.sync()


def stylesheet(palette):
    value = _STYLE
    for key in sorted(palette, key=len, reverse=True):
        value = value.replace('$' + key, palette[key])
    mode = 'dark' if palette is DARK else 'light'
    return value.replace('CHEVRON_PATH', (ASSETS / f'chevron-{mode}.svg').as_posix()).replace('CHECK_PATH', (ASSETS / f'check-{mode}.svg').as_posix())


_STYLE = '''
QWidget { font-family: "MiSans", "Microsoft YaHei UI", "Noto Sans SC", "PingFang SC", sans-serif; font-size: 14px; font-weight: 400; color: $text; }
QMainWindow { background: transparent; }
QWidget#workspaceRoot { background: transparent; }
QFrame#appHeader { background: transparent; border: 0; }
QFrame#commandBar { background: transparent; border-bottom: 1px solid $border; }
QFrame#sidebar, QWidget#sidebar { background: transparent; }
QStackedWidget#viewStage { background: $veil; border-top-left-radius: 12px; border: 0; }
QWidget#stage, QFrame#stage { background: transparent; }
QLabel { background: transparent; }
QLabel#appName { font-size: 13px; font-weight: 500; }
QLabel#paneTitle { font-size: 14px; font-weight: 500; }
QLabel#viewTitle { font-size: 18px; font-weight: 500; }
QLabel#emptyTitle { font-size: 24px; font-weight: 500; }
QLabel#muted { color: $secondary; }
QLabel#caption, QLabel#status { color: $muted; font-size: 12px; }
QLabel#offline, QLabel#modelBadge[badgeTone="success"] { color: $success; background: transparent; padding: 2px 6px; font-size: 11px; }
QLabel#modelBadge { color: $muted; background: transparent; padding: 2px 6px; font-size: 11px; }
QLabel#modelBadge[badgeTone="error"], QLabel#status[statusTone="warning"] { color: $error; }
QLabel#pageNotice { color: $warning; background: $warning_bg; border-radius: 4px; padding: 7px 10px; font-size: 11px; }
QLabel#count { color: $secondary; background: $disabled_bg; padding: 2px 8px; border-radius: 9px; }
QLabel#chip { color: $primary; background: $tint; border: 1px solid $border; border-radius: 5px; padding: 7px; font-size: 11px; }
QFrame#separator { background: $border; min-height: 1px; max-height: 1px; }
QFrame#statusBar { background: transparent; border-top: 1px solid $border; }
QFrame#softCard { background: $subtle; border: 1px solid $border; border-radius: 9px; }
QFrame#taskCard { background: $subtle; border: 0; border-radius: 9px; }
QFrame#emptyDrop { background: $subtle; border: 1px dashed $border; border-radius: 4px; }
QFrame#floatingTools { background: transparent; border: 0; }
QPushButton { background: $surface; border: 1px solid $border; border-radius: 7px; padding: 7px 12px; min-height: 18px; font-weight: 500; }
QPushButton:hover { background: $subtle; border-color: $muted; }
QPushButton:pressed, QPushButton:checked { background: $tint; border-color: $primary; color: $primary; }
QPushButton:focus { border-color: $primary; }
QPushButton:disabled { color: $disabled; background: $disabled_bg; border-color: $border; }
QPushButton::menu-indicator { image: url(CHEVRON_PATH); width: 10px; height: 8px; subcontrol-position: right center; right: 4px; }
QPushButton#primary { background: $primary; color: $on_primary; border-color: $primary; font-weight: 500; }
QPushButton#primary:hover { background: $hover; border-color: $hover; }
QPushButton#primary:disabled { background: $disabled_bg; color: $disabled; border-color: $border; }
QPushButton#quiet { background: transparent; border-color: transparent; color: $text; padding: 5px 8px; }
QPushButton#quiet:hover { background: $subtle; color: $text; }
QPushButton#quiet:disabled { color: $disabled; }
QPushButton#navigation { background: transparent; border: 1px solid transparent; padding: 5px 15px; color: $secondary; font-size: 14px; }
QPushButton#navigation:checked { color: $text; background: transparent; border-bottom: 3px solid $primary; border-radius: 0; }
QPushButton#navigation:disabled { color: $disabled; }
QPushButton#choiceCard { text-align: left; padding: 0; border-radius: 9px; min-height: 66px; max-height: 66px; }
QPushButton#choiceCard[large="true"] { min-height: 116px; max-height: 116px; border-radius: 12px; }
QPushButton#choiceCard:checked { border-color: $primary; background: $tint; }
QPushButton#choiceCard QLabel { background: transparent; }
QPushButton#choiceCard QLabel#paneTitle { color: $text; }
QPushButton#choiceCard QLabel#caption { color: $muted; }
QRadioButton { spacing: 6px; background: transparent; }
QRadioButton::indicator { width: 14px; height: 14px; border-radius: 8px; border: 1px solid $border; background: $surface; }
QRadioButton::indicator:checked { border: 4px solid $primary; width: 8px; height: 8px; background: $surface; }
QComboBox { background: $surface; border: 1px solid $border; border-radius: 6px; padding: 7px 26px 7px 10px; min-height: 16px; font-weight: 500; }
QComboBox:disabled { background: $disabled_bg; color: $disabled; }
QComboBox::drop-down { border: 0; width: 23px; }
QComboBox::down-arrow { image: url(CHEVRON_PATH); width: 12px; height: 8px; }
QComboBox QAbstractItemView { background: $surface; selection-background-color: $selection; selection-color: $text; border: 1px solid $border; outline: 0; }
QComboBox QLineEdit { background: transparent; border: 0; padding: 0; border-radius: 0; }
QCheckBox { spacing: 7px; color: $secondary; }
QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid $border; border-radius: 3px; background: $surface; }
QCheckBox::indicator:checked { background: $primary; border-color: $primary; image: url(CHECK_PATH); }
QCheckBox:disabled { color: $disabled; }
QListWidget#imageGallery { background: transparent; border: 0; outline: 0; }
QListWidget { background: $surface; border: 1px solid $border; border-radius: 5px; outline: 0; }
QListWidget::item { padding: 9px; }
QListWidget::item:selected { background: $tint; color: $text; }
QListWidget#previewPages { border: 0; }
QListWidget#previewPages::item { padding: 4px; border: 1px solid transparent; border-radius: 5px; }
QListWidget#previewPages::item:selected { border-color: $primary; }
QTextEdit#documentEditor { background: #FFFFFF; color: #202631; border: 1px solid $border; border-radius: 2px; selection-background-color: #CDE5FA; selection-color: #202631; }
QPlainTextEdit { background: $subtle; color: $text; border: 1px solid $border; border-radius: 7px; padding: 9px; font-size: 12px; selection-background-color: $selection; }
QPlainTextEdit:read-only { color: $secondary; }
QPlainTextEdit#proofreadEdit { background: $stage; font-size: 14px; padding: 8px; }
QPlainTextEdit#proofreadEdit:focus { border-color: $primary; }
QFrame#proofreadFooter { background: $surface; border-top: 1px solid $border; border-bottom-left-radius: 9px; border-bottom-right-radius: 9px; }
QFrame#proofreadFooter QPushButton { padding: 7px 6px; font-size: 12px; }
QTabBar#proofreadTabs::tab { padding: 6px 7px; font-size: 12px; border: 0; }
QTabBar#proofreadTabs::tab:selected { background: $tint; color: $primary; }
QPushButton#cropTool { background: transparent; border: 1px solid transparent; padding: 2px 4px; font-size: 11px; min-height: 16px; }
QPushButton#cropTool:hover, QPushButton#cropTool:checked { background: $tint; color: $primary; }
QPushButton#cropTool:focus { border-color: $primary; }
QGraphicsView#sourceCrop { background: #FFFFFF; border: 1px solid $border; border-radius: 7px; }
QListWidget#candidateList, QListWidget#reviewList { border: 0; background: transparent; }
QPushButton#segment:checked { background: $tint; border-color: $primary; color: $primary; }
QLineEdit { background: $subtle; color: $text; border: 1px solid $border; border-radius: 7px; padding: 9px; selection-background-color: $selection; }
QLineEdit:focus { border-color: $primary; }
QGraphicsView#photoCanvas, QGraphicsView#exportCanvas { border: 0; background: transparent; }
QScrollArea { background: transparent; border: 0; }
QScrollArea > QWidget > QWidget { background: transparent; }
QSplitter::handle { background: transparent; }
QSplitter#proofreadRegions::handle { background: $border; border-radius: 2px; margin: 2px 110px; }
QSplitter#proofreadRegions::handle:hover { background: $primary; }
QScrollBar:vertical { width: 7px; background: transparent; margin: 2px; }
QScrollBar::handle:vertical { background: $scrollbar; border-radius: 2px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QProgressBar { background: $border; border: 0; border-radius: 2px; }
QProgressBar::chunk { background: $primary; border-radius: 2px; }
QDialog, QMessageBox { background: $surface; }
QToolTip { background: $surface; color: $text; border: 1px solid $border; padding: 6px; }
QMenu { background: $surface; border: 1px solid $border; padding: 6px; }
QMenu::item { padding: 8px 24px; }
QMenu::item:selected { background: $selection; }
QTabBar::tab { background: $disabled_bg; padding: 7px 24px; border: 1px solid transparent; color: $muted; }
QTabBar::tab:selected { background: $surface; color: $text; border-color: $border; }
QPushButton#captionButton { background: transparent; border: 0; border-radius: 0; padding: 0; min-height: 38px; max-width: 44px; min-width: 44px; }
QPushButton#captionButton:hover { background: $tint; }
QPushButton#closeWindow:hover { background: #C42B1C; color: #FFFFFF; }
QFrame#glassDrawer QWidget#sidebar { background: transparent; }
QFrame#glassDrawer QScrollArea { background: transparent; }
QLabel#protectionStatus { color: $secondary; font-size: 11px; }
QPushButton#railAction { background: transparent; border-color: transparent; padding: 5px 2px; font-size: 11px; }
QPushButton#railAction:hover { background: $tint; }
'''
APP_STYLESHEET = stylesheet(LIGHT)


def proofread_badge(status):
    return {'consistent': ('一致', 'success'), 'majority': ('多数一致', 'review'),
            'conflict': ('存在差异', 'review'), 'incomplete': ('未完成', 'review')}.get(status, ('待复核', 'review'))
