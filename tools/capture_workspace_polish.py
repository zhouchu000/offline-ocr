"""Capture the real Qt workspace with public fixtures and isolated preferences."""
from pathlib import Path
import argparse
import json
import traceback
from PySide6.QtCore import QPoint, QSettings, QTimer
from PySide6.QtWidgets import QApplication
from offline_word.gui import MainWindow
from offline_word.domain import ImageJob
from offline_word.security import SessionStore, deny_network
from offline_word import theme
from tools.capture_glass_ui import public_document, populate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[1]
    app = QApplication([])
    original_settings = theme.QSettings
    theme.QSettings = lambda *args: QSettings(str(output/'appearance.ini'), QSettings.Format.IniFormat)
    theme.load_ui_font()
    public_document(output/'public.png')
    session = SessionStore()
    deny_network()
    w = MainWindow(root/'models', session.path)
    w._confirm_discard_results = lambda *a, **k: True
    w.resize(1100, 680)
    theme.set_appearance(w, 'dark', persist=False)
    theme.set_effect(w, 'motion', False, persist=False)
    w.show()
    report = {'captures': [], 'data': 'Public synthetic document; seeded OCR candidates, no inference benchmark.'}

    def capture(name, widget=w):
        assert widget.grab().save(str(output/(name+'.png')))
        report['captures'].append(name)

    def check_footer():
        p = w.proofread_panel
        for control in (p.apply, p.confirm, p.next):
            assert control.isVisible()
            assert p.footer.rect().contains(control.mapTo(p.footer, QPoint()))
            assert w.rect().contains(control.mapTo(w, control.rect().bottomRight()))

    def flow():
        yield 400
        capture('01-empty')
        w.appearance_button.click()
        yield 300
        capture('02-settings', w.appearance_dialog)
        w.appearance_dialog.reject()
        w.set_jobs([ImageJob('public-'+str(i), str(output/'public.png')) for i in range(4)])
        w.show_gallery()
        yield 300
        capture('03-import')
        w.settings_button.click()
        yield 300
        capture('04-recognition')
        w.show_gallery()
        populate(w, output/'public.png')
        w.show_document()
        w.select_block('body')
        yield 300
        check_footer()
        panel = w.proofread_panel
        report['comparison'] = {
            'delegate': type(panel.candidates.itemDelegate()).__name__,
            'tracks_current_text': panel.difference_delegate.original == panel.edit.toPlainText(),
            'deletion_presented': '昇算' in panel.difference_delegate.document(panel.candidates.model().index(0, 0), panel.candidates.width()).toPlainText(),
        }
        assert all(report['comparison'][key] for key in ('tracks_current_text', 'deletion_presented'))
        capture('05-candidate-detail', panel.candidates)
        capture('05-review')
        w.resize(1440, 900)
        yield 300
        check_footer()
        capture('06-review-wide')
        w.resize(1100, 680)
        w.review_summary_button.click()
        yield 300
        assert w.review_list.horizontalScrollBar().maximum() == 0
        capture('07-review-list', w._review_dialog)
        w._review_dialog.reject()
        w.export_button.click()
        yield 300
        capture('08-export', w.export_options)
        w.export_options.reject()
        theme.set_appearance(w, 'light', persist=False)
        yield 300
        check_footer()
        capture('09-review-light')
        w.export_button.click()
        yield 300
        capture('10-export-light', w.export_options)
        w.export_options.reject()

    steps = flow()

    def advance():
        try:
            QTimer.singleShot(next(steps), advance)
        except StopIteration:
            report['pass'] = True
            w.close()
            app.quit()
        except Exception:
            report['error'] = traceback.format_exc()
            w.close()
            app.quit()

    QTimer.singleShot(0, advance)
    app.exec()
    session.close()
    theme.QSettings = original_settings
    report['session_cleaned'] = not session.path.exists()
    (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get('pass') and report['session_cleaned'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
