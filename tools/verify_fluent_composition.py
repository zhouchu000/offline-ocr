"""Verify real Windows composition, using only two test-owned windows.

Run with the Windows Qt platform, not offscreen. A solid-color response proves
desktop transparency, a fine checkerboard tests blur, and an opaque control
rejects false positives. All content is synthetic; no user desktop is saved.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import traceback

import numpy as np
from PySide6.QtCore import QPoint, QTimer, Qt
from PySide6.QtGui import QColor, QFontInfo, QImage, QPainter
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from tools.capture_glass_ui import populate, public_document
from offline_word.fluent import desktop_effects
from offline_word.gui import MainWindow
from offline_word.security import SessionStore, deny_network
from offline_word.theme import load_ui_font, set_appearance, set_effect


class Background(QWidget):
    pattern = '#164ECA'

    def paintEvent(self, event):
        p = QPainter(self)
        if self.pattern == 'checker':
            p.fillRect(self.rect(), Qt.GlobalColor.white)
            for x in range(0, self.width(), 4):
                for y in range(0, self.height(), 4):
                    if (x+y)//4 % 2:
                        p.fillRect(x, y, 4, 4, Qt.GlobalColor.black)
        elif self.pattern == 'sample':
            p.fillRect(self.rect(), QColor('#BED6E9'))
            p.fillRect(0, 0, 370, self.height(), QColor('#A1B7DC'))
            p.fillRect(400, 330, self.width(), 320, QColor('#D2DDD4'))
            p.fillRect(750, 0, 340, 350, QColor('#EADBD0'))
            p.setPen(QColor('#889CA6'))
            font = p.font()
            font.setPixelSize(44)
            p.setFont(font)
            p.drawText(500, 95, '公开测试窗口')
        else:
            p.fillRect(self.rect(), QColor(self.pattern))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--model-dir', type=Path, default=Path(__file__).resolve().parents[1]/'models')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    app = QApplication([])
    capabilities = desktop_effects()
    if not (capabilities.native and capabilities.glass and capabilities.motion):
        print('Native desktop glass and motion must be enabled; this is not an offscreen test.')
        return 77
    load_ui_font()
    public_document(args.output/'public-contract.png')
    session = SessionStore()
    deny_network()
    background = Background()
    background.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
    background.setGeometry(80, 80, 1320, 868)
    background.show()
    window = MainWindow(args.model_dir, session.path)
    window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
    window._confirm_discard_results = lambda *a, **k: True
    window.resize(1280, 828)
    window.move(100, 100)
    populate(window, args.output/'public-contract.png')
    marker = QLabel(window)
    marker.setStyleSheet('background:#17E33F')
    marker.setGeometry(5, 5, 6, 6)
    marker.show()
    window.show()
    window.raise_()
    window.activateWindow()
    report = {'checks': {}, 'states': [], 'motion_frames': []}

    def pixels(image):
        image = image.convertToFormat(QImage.Format.Format_RGBA8888)
        return np.frombuffer(image.constBits(), dtype=np.uint8).reshape(
            image.height(), image.bytesPerLine()//4, 4)[:, :image.width(), :3].copy()

    deferred_images = []

    def capture(name, *, defer=False):
        marker.raise_()
        marker.repaint()
        report.setdefault('activity', {})[name] = {'active': window.isActiveWindow(), 'dwm': window._dwm_result}
        point = window.mapToGlobal(QPoint())
        screen = window.screen()
        assert screen.geometry().contains(point+QPoint(window.width()-1, window.height()-1))
        image = screen.grabWindow(0, point.x(), point.y(), window.width(), window.height()).toImage()
        a = pixels(image)
        dpr = image.width()/window.width()
        guard = a[round(6*dpr):round(10*dpr), round(6*dpr):round(10*dpr)].mean((0, 1))
        assert guard[1] > 180 and guard[0] < 60, 'Owned-window marker missing; refusing screen evidence'
        if defer:
            deferred_images.append((name, image))
        else:
            assert image.save(str(args.output/f'{name}.png'))
        return a

    def region(a):
        k = a.shape[1]/window.width()
        return a[round(66*k):round(80*k), round(265*k):round(330*k)]

    def pattern(value):
        # DWM can occlude the covered Qt background, leaving its old pixels.
        # Repaint it while exposed before measuring the foreground material.
        window.hide()
        background.pattern = value
        background.repaint()
        app.processEvents()
        window.show()
        window.raise_()
        window.activateWindow()

    def steps():
        set_effect(window, 'motion', True, persist=False)
        for theme in ('light', 'dark'):
            set_appearance(window, theme, persist=False)
            for enabled in (True, False):
                set_effect(window, 'glass', enabled, persist=False)
                pattern('#164ECA')
                yield 400
                blue = region(capture(f'{theme}-{enabled}-blue')).mean((0, 1))
                pattern('#C62C4F')
                yield 400
                red = region(capture(f'{theme}-{enabled}-red')).mean((0, 1))
                delta = float(np.max(np.abs(red-blue)))
                report['checks'][f'{theme}-glass-{enabled}-delta'] = delta
                assert (delta > 3) if enabled else (delta < 1), (theme, enabled, delta)
            set_effect(window, 'glass', True, persist=False)
            pattern('checker')
            yield 450
            checker = region(capture(f'{theme}-blur'))
            deviation = float(checker.std((0, 1)).max())
            report['checks'][f'{theme}-checker-deviation'] = deviation
            assert deviation < 8, 'Fine background pattern remains sharp through the window'

        pattern('sample')
        for theme in ('light', 'dark'):
            set_appearance(window, theme, persist=False)
            for size in ((1280, 828), (1100, 680)):
                window.resize(*size)
                for view in ('document', 'proofread', 'settings', 'gallery', 'full-text', 'preview'):
                    window.close_context_panel()
                    if view in ('document', 'proofread', 'full-text'):
                        window.compare_button.setChecked(True)
                        window.toggle_comparison()
                        window.show_document()
                        if view == 'proofread': window.select_block('body')
                        if view == 'full-text': window.show_full_document()
                    elif view == 'settings': window.show_recognition_settings()
                    elif view == 'gallery': window.show_gallery()
                    else:
                        window.show_export_preview()
                        while window.export_controller.busy:
                            yield 50
                    window.raise_()
                    window.activateWindow()
                    yield 400
                    name = f'{view}-{theme}-{size[0]}'
                    capture(name)
                    report['states'].append(name)

        window.resize(1280, 828)
        set_appearance(window, 'light', persist=False)
        window.show_gallery()
        yield 450
        window.document_button.click()
        for i in range(10):
            yield 35
            a = capture(f'motion-{i:02}', defer=True)
            report['motion_frames'].append({'time': window.motion.animation.currentTime(),
                                           'visible': window.motion.layer.isVisible()})
            if i == 0: first = a
            if i == 5: middle = a
        report['checks']['motion-pixel-change'] = float(np.mean(np.abs(middle.astype(float)-first)))
        assert report['checks']['motion-pixel-change'] > .3
        assert window.motion.layer._image.isNull()
        for name, image in deferred_images:
            assert image.save(str(args.output/f'{name}.png'))
        font = window.import_button.font()
        report['font'] = {'family': QFontInfo(font).family(), 'weight': font.weight(),
                          'pixels': font.pixelSize(), 'hinting': font.hintingPreference().name,
                          'screen_scale': window.devicePixelRatioF()}
        report['dwm'] = window._dwm_result
        # Stable pixels after repeated repaint: translucent tints must not accumulate.
        before = region(capture('repaint-before')).mean((0, 1))
        for _ in range(5):
            window.workspace_root.update()
            yield 30
        after = region(capture('repaint-after')).mean((0, 1))
        assert np.max(np.abs(after-before)) < 1
        window.showMaximized()
        yield 350
        assert window.isMaximized()
        window.showNormal()
        yield 350
        assert window._dwm_result == 'desktop-acrylic'
        report['checks']['maximize-restore'] = True

    iterator = steps()

    def finish(error=None):
        report['pass'] = error is None
        if error: report['error'] = error
        window.close()
        background.close()
        session.close()
        report['session_cleaned'] = not session.path.exists()
        (args.output/'composition.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=True), flush=True)
        app.exit(1 if error else 0)

    def advance():
        try:
            delay = next(iterator)
            QTimer.singleShot(delay, advance)
        except StopIteration:
            finish()
        except Exception:
            finish(traceback.format_exc())

    QTimer.singleShot(500, advance)
    QTimer.singleShot(60000, lambda: finish('timeout'))
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
