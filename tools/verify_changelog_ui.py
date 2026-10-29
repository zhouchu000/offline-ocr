"""Check actual Windows release-history interactions and capture owned widgets."""
import argparse
import json
from pathlib import Path

from PySide6.QtCore import QAbstractAnimation, QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from offline_word import theme
from offline_word.domain import ImageJob
from offline_word.gui import MainWindow
from offline_word.security import SessionStore, deny_network
from tools.capture_glass_ui import public_document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    app = QApplication([])
    if app.platformName() != 'windows':
        raise RuntimeError('native_windows_required')
    settings = QSettings(str(args.output / 'appearance.ini'), QSettings.Format.IniFormat)
    theme.QSettings = lambda *args: settings
    source = args.output / 'public-document.png'
    public_document(source)
    session = SessionStore()
    deny_network()
    window = MainWindow(args.output / 'no-models', session.path)
    report = {'versions': [], 'platform': app.platformName()}
    try:
        window.resize(1100, 680)
        window.set_jobs([ImageJob('public', str(source))])
        window.show()
        QTest.qWait(300)
        for appearance in ('dark', 'light'):
            theme.set_appearance(window, appearance, persist=False)
            window.appearance_button.click()
            QTest.qWait(150)
            window.release_history_button.click()
            dialog = window.release_dialog
            assert QApplication.activeModalWidget() is dialog
            for _ in range(40):
                if dialog.entrance.state() == QAbstractAnimation.State.Stopped:
                    break
                QTest.qWait(50)
            assert dialog.entrance.state() == QAbstractAnimation.State.Stopped
            combo = dialog.version_combo
            for release in dialog.releases:
                QTest.mouseClick(combo, Qt.MouseButton.LeftButton)
                QTest.qWait(30)
                index = combo.model().index(combo.findData(release.version), 0)
                combo.view().scrollTo(index)
                QTest.qWait(20)
                QTest.mouseClick(combo.view().viewport(), Qt.MouseButton.LeftButton,
                                 pos=combo.view().visualRect(index).center())
                QTest.qWait(20)
                assert combo.currentData() == release.version
                assert dialog.notes_title.text() == release.title
                body = [label.text() for label in dialog.notes_scroll.widget().findChildren(QLabel)]
                assert all(change in body for change in release.changes)
                assert dialog.notes_scroll.horizontalScrollBar().maximum() == 0
                report['versions'].append({'theme': appearance, 'version': release.version})
            combo.setCurrentIndex(0)
            QTest.qWait(150)
            assert dialog.grab().save(str(args.output / f'history-{appearance}.png'))
            # Native keyboard navigation after a mouse selection, including
            # popup cancellation and restoring the current release.
            combo.setFocus()
            QTest.keyClick(combo, Qt.Key.Key_Down)
            assert combo.currentData() == dialog.releases[1].version
            dialog.reject()
            QTest.qWait(60)
            assert window.appearance_dialog.isVisible()
            window.appearance_dialog.reject()
        report['pass'] = True
    finally:
        if window.release_dialog:
            window.release_dialog.reject()
        window.close()
        QTest.qWait(100)
        session.close()
        report['session_cleaned'] = not session.path.exists()
        (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
