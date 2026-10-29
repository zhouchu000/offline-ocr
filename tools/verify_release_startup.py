"""Verify the actual GUI entry point with isolated release-read preferences."""
import argparse
import json
from pathlib import Path
import sys
import traceback


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--preferences', type=Path, required=True)
    parser.add_argument('--repeat', action='store_true')
    parser.add_argument('--package', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    from PySide6.QtCore import QSettings, QTimer
    from offline_word import __main__, gui, release_dialog
    from offline_word.version import __version__
    root = Path.cwd()
    expected = root / ('app/offline_word' if args.package else 'src/offline_word')
    assert Path(gui.__file__).resolve().parent == expected.resolve()
    settings = QSettings(str(args.preferences.resolve()), QSettings.Format.IniFormat)
    original_preferences = release_dialog.ReleasePreferences
    release_dialog.ReleasePreferences = lambda: original_preferences(settings)
    report = {'version':__version__, 'repeat':args.repeat, 'package':args.package}
    original = gui.MainWindow.__init__

    def initialize(window, *positional, **keyword):
        original(window, *positional, **keyword)
        report['session_path'] = str(window.session_dir)
        steps = workflow(window)
        def tick():
            try:
                delay = next(steps)
                QTimer.singleShot(delay, tick)
            except StopIteration:
                report['pass'] = True
                window.close()
            except Exception:
                report.update({'pass':False,'error':traceback.format_exc()})
                if getattr(window,'release_dialog',None): window.release_dialog.reject()
                window.close()
        QTimer.singleShot(1000, tick)

    def workflow(window):
        for _ in range(300):
            if window._model_task is None:
                break
            yield 100
        assert window._models_ready and not window.jobs
        assert window.source_stack.currentWidget() is window.empty_stage
        dialog = getattr(window,'release_dialog',None)
        if args.repeat:
            assert dialog is None, 'Already-read version was shown automatically'
        else:
            assert dialog is not None and dialog.isVisible() and dialog.pages.currentIndex() == 0
            assert dialog.grab().save(str(args.output/'welcome.png'))
            dialog.next_button.click()
            yield 400
            assert dialog.pages.currentIndex() == 1
            assert dialog.motion.layer._image.isNull()
            assert dialog.grab().save(str(args.output/'notes.png'))
            dialog.next_button.click()
            yield 100
            assert original_preferences(settings).has_seen()
        window.release_history_button.click()
        yield 400
        history = window.release_dialog
        assert history is not None and history.pages.currentIndex() == 1
        assert history.version_combo.findData('0.3.10') >= 0
        history.reject()
        report['manual_history'] = True

    gui.MainWindow.__init__ = initialize
    sys.argv = ['offline-word']
    exit_code = __main__.main()
    session = Path(report.pop('session_path'))
    report.update(exit_code=exit_code, session_cleaned=not session.exists())
    (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)
    assert report.get('pass') and exit_code == 0 and report['session_cleaned'], report


if __name__ == '__main__':
    main()
