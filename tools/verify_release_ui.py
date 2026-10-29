"""Native Windows release dialog / photo suggestion evidence on public content."""
import argparse
import json
from pathlib import Path
import traceback

import numpy as np
from PySide6.QtCore import QPoint, QSettings, QTimer, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QLabel

from offline_word.domain import ImageJob
from offline_word.gui import MainWindow
from offline_word.release_dialog import ReleasePreferences, show_release_dialog
from offline_word.security import SessionStore, deny_network
from offline_word.theme import set_appearance, set_effect
from tools.capture_glass_ui import public_document
from tools.verify_fluent_composition import Background


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model-dir', type=Path, default=Path('models'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    app = QApplication([])
    session = SessionStore()
    deny_network()
    source = args.output/'public-document.png'
    public_document(source)
    from PIL import Image
    with Image.open(source) as page:
        photo = Image.new('RGB',(1320,1380),'#4f5864')
        photo.paste(page,(240,96))
        photo.save(args.output/'public-photo.png')
    background = Background()
    background.pattern = 'sample'
    background.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
    background.setGeometry(80,80,1320,868)
    background.show()
    w = MainWindow(args.model_dir,session.path)
    w.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
    w.resize(1280,828)
    w.move(100,100)
    w._confirm_discard_results = lambda *a,**k: True
    w.set_jobs([ImageJob('public',str(source))])
    marker = QLabel(w)
    marker.setStyleSheet('background:#17E33F')
    marker.setGeometry(5,5,6,6)
    marker.show()
    w.show()
    w.raise_()
    report = {'states':[], 'checks':{}}
    frames = []

    def capture(name, *, defer=False):
        p = w.mapToGlobal(QPoint())
        image = w.screen().grabWindow(0,p.x(),p.y(),w.width(),w.height()).toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        data = np.frombuffer(image.constBits(),dtype=np.uint8).reshape(image.height(),image.bytesPerLine()//4,4)[:,:image.width(),:3]
        ratio = image.width()/w.width()
        guard = data[round(6*ratio):round(10*ratio),round(6*ratio):round(10*ratio)].mean((0,1))
        assert guard[1] > 180 and guard[0] < 60, 'Owned test window marker missing'
        if defer: frames.append((name,image.copy()))
        else: assert image.save(str(args.output/f'{name}.png'))
        report['states'].append(name)

    def steps():
        for appearance in ('light','dark'):
            set_appearance(w,appearance,persist=False)
            set_effect(w,'motion',True,persist=False)
            set_effect(w,'glass',True,persist=False)
            for size in ((1280,828),(1100,680)):
                w.resize(*size)
                yield 300
                preferences = ReleasePreferences(QSettings(str(args.output/f'{appearance}-{size[0]}.ini'),QSettings.Format.IniFormat))
                dialog = show_release_dialog(w,welcome=True,preferences=preferences)
                yield 420
                capture(f'welcome-{appearance}-{size[0]}')
                dialog.next_button.click()
                if appearance == 'light' and size[0] == 1280:
                    for index in range(11):
                        yield 28
                        capture(f'motion-{index:02}',defer=True)
                yield 360
                capture(f'notes-{appearance}-{size[0]}')
                assert dialog.pages.currentIndex() == 1
                assert dialog.notes_scroll.horizontalScrollBar().maximum() == 0
                dialog.version_combo.setCurrentIndex(dialog.version_combo.findData('0.3.10'))
                yield 150
                capture(f'history-{appearance}-{size[0]}')
                dialog.next_button.click()
                assert preferences.has_seen()
                assert show_release_dialog(w,welcome=True,preferences=preferences) is None
                yield 150
            w.show_recognition_settings()
            w.profile_combo.setCurrentIndex(w.profile_combo.findData('parallel'))
            yield 350
            capture(f'settings-{appearance}')
            w.close_context_panel()
        w.set_jobs([ImageJob('photo',str(args.output/'public-photo.png'),mode='photo')])
        w.show_gallery()
        for _ in range(60):
            yield 100
            if w.current_job.paper_status != 'detecting': break
        assert w.current_job.paper_status == 'suggested'
        for appearance in ('light','dark'):
            set_appearance(w,appearance,persist=False)
            yield 250
            capture(f'paper-{appearance}')
        w.accept_paper_button.click()
        assert w.current_job.paper_status == 'confirmed'
        assert w.current_job.perspective is not None and not w.paper_bar.isVisible()
        yield 250
        capture('paper-confirmed')
        report['checks'].update(first_launch=True, repeat_suppressed=True, history=True, paper_requires_confirmation=True,
                                 light_dark=True, smallest_window=True, native_dpr=w.devicePixelRatioF())
        for name,image in frames:
            assert image.save(str(args.output/f'{name}.png'))
        report['pass'] = True

    sequence = steps()
    def next_step():
        try:
            delay = next(sequence)
        except StopIteration:
            finish()
        except Exception:
            report['error'] = traceback.format_exc()
            finish()
        else:
            QTimer.singleShot(delay,next_step)
    def finish():
        if getattr(w,'release_dialog',None): w.release_dialog.reject()
        w.close()
        background.close()
        session.close()
        report['session_cleaned'] = not session.path.exists()
        (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False),flush=True)
        app.exit(0 if report.get('pass') else 1)
    QTimer.singleShot(450,next_step)
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
