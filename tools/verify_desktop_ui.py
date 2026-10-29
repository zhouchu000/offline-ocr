"""Native owned Qt window -> real local OCR queue updates -> four Word modes."""
import argparse
import json
import multiprocessing
from pathlib import Path
import time
import traceback


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('fixtures', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--package', action='store_true')
    parser.add_argument('--parallel', action='store_true')
    parser.add_argument('--cancel-retry', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    import faulthandler
    fault_log = (args.output/'fault.txt').open('w', encoding='utf-8')
    faulthandler.enable(file=fault_log)
    def debug(value):
        with (args.output/'trace.txt').open('a', encoding='utf-8') as trace:
            trace.write(str(value)+'\n')
    root = Path.cwd()
    from offline_word.security import SessionStore, configure_offline_env, deny_network
    report = {'status': 'running', 'package': args.package, 'worker_starts': 0}
    start = time.monotonic()
    with SessionStore() as session:
        session_path = session.path
        configure_offline_env(root / 'models', session.path)
        deny_network()
        from offline_word import gui
        expected = root / ('app/offline_word' if args.package else 'src/offline_word')
        assert Path(gui.__file__).resolve().parent == expected.resolve()
        from PySide6.QtCore import QTimer, QPoint
        from PySide6.QtWidgets import QApplication, QDialog
        from docx import Document
        app = QApplication([])
        window = gui.MainWindow(root/'models', session.path)
        window.resize(1440,900)
        window.show()
        if args.parallel:
            window.profile_combo.setCurrentIndex(window.profile_combo.findData('parallel'))
        timer = QTimer()
        state = {'step': 'empty'}
        original_start = gui.OCRWorker._start_process
        def counted_start(worker, target, args):
            report['worker_starts'] += 1
            return original_start(worker, target, args)
        gui.OCRWorker._start_process = counted_start
        paths = sorted(args.fixtures.resolve().glob('*.png'))
        assert len(paths) >= 6, 'Expected the six public repro-036 fixtures'

        def capture(name):
            assert window.grab().save(str(args.output/name))

        def tick():
            try:
                if state.get('logged') != state['step']:
                    debug(state['step'])
                    state['logged'] = state['step']
                if time.monotonic()-start > 240:
                    raise TimeoutError('live queue timeout')
                if state['step'] == 'empty':
                    assert window._models_ready
                    capture('empty.png')
                    window._import_paths([str(p) for p in paths[:3]])
                    state['remove_row'] = 2 if args.parallel else 1
                    state['removed'] = window.jobs[state['remove_row']].id
                    window.start_button.click()
                    if args.parallel:
                        assert window._worker.lane_count == 2, 'Need sufficient real available memory to validate two lanes'
                    window.theme_button.click()
                    report['theme_during_ocr']=window.property('appearance')
                    state['step'] = 'wait-start'
                    return
                if state['step'] == 'wait-start':
                    if window.jobs[0].status != 'processing':
                        return
                    if args.parallel and window.jobs[1].status != 'processing':
                        return
                    if args.cancel_retry and not state.get('retried'):
                        workers = [lane.worker for lane in window._worker.lanes]
                        state['cancelled_pids'] = [worker._process.pid for worker in workers]
                        window.cancel_processing()
                        state['step'] = 'wait-cancelled'
                        return
                    state['expected_starts'] = report['worker_starts']
                    assert window.import_button.isEnabled()
                    window.queue_list.setCurrentRow(state['remove_row'])
                    window.remove_button.click()
                    assert state['removed'] in window._removal_requests
                    window.import_button.click()
                    dialog = window._active_file_browser
                    assert isinstance(dialog, QDialog)
                    dialog.selectedFiles = lambda: [str(paths[5])]
                    # Real QDialog dismissal boundary, no mouse/keyboard OS input.
                    # Allow the native helper to create its OS window first;
                    # forcing completion in the same signal stack is not a user action.
                    QTimer.singleShot(400, lambda: QDialog.done(dialog, 1))
                    state['step'] = 'wait-added'
                    return
                if state['step'] == 'wait-cancelled':
                    if window._worker:
                        return
                    import psutil
                    assert all(not psutil.pid_exists(pid) for pid in state['cancelled_pids'])
                    assert any(job.status == 'cancelled' for job in window.jobs)
                    state['retried'] = True
                    report['cancelled_processes'] = len(state['cancelled_pids'])
                    window.retry_failed()
                    assert window._worker
                    if args.parallel: assert window._worker.lane_count == 2
                    state['step'] = 'wait-start'
                    return
                if state['step'] == 'wait-added':
                    if window._active_file_browser is not None:
                        return
                    assert any(Path(j.source_path) == paths[5] for j in window.jobs)
                    window.queue_list.setCurrentRow(0)
                    capture('processing.png')
                    state['step'] = 'wait-done'
                    return
                if state['step'] == 'wait-done':
                    if window._worker:
                        return
                    assert len(window.jobs) == 3 and all(j.status == 'done' for j in window.jobs), window.status_label.text()
                    assert all(j.id != state['removed'] for j in window.jobs)
                    assert report['worker_starts'] == state['expected_starts'], 'models reloaded when appending'
                    assert window.import_button.isEnabled() and window.remove_button.isEnabled()
                    texts = [''.join(b.text for b in j.result.blocks if b.kind != 'image') for j in window.jobs]
                    for text in texts:
                        for expected_text in ['本地文档识别验证', '正常文字', '二维码说明文字', '128.50', '不得误删']:
                            assert expected_text in text, (expected_text, text)
                        assert '\nw\n' not in text
                    assert all(any(b.kind == 'image' for b in j.result.blocks) for j in window.jobs)
                    # Edit through the visible inline panel, then verify every export consumes it.
                    window.show_document()
                    block = next(b for b in window.current_job.result.blocks if b.kind == 'text' and '不得误删' in b.text)
                    window.select_block(block.id)
                    original_text = block.original_text
                    window.proofread_panel.edit.setPlainText(block.text + '人工校对验证。')
                    assert block.original_text == original_text and '人工校对验证。' in block.text
                    exports = {}
                    for mode in ('source', 'flow', 'paragraph', 'text_only'):
                        target = args.output / f'{mode}.docx'
                        window.export_layout_combo.setCurrentIndex(window.export_layout_combo.findData(mode))
                        window._open_file_browser = lambda dialog, accept, _path=target: accept([str(_path)])
                        window.request_export(separate=False)
                        from offline_word.export_check_dialog import ExportCheckDialog
                        if isinstance(app.activeModalWidget(), ExportCheckDialog):
                            report['export_checks_presented'] = True
                            app.activeModalWidget().accept()
                        window.show_export_preview()
                        assert window.views.currentWidget() is window.preview_view, window.status_label.text()
                        assert '人工校对验证。' in ''.join(p.document.toPlainText()+''.join(o[2].toPlainText() for o in p.overlays) for p in window.preview_canvas._pages)
                        doc = Document(target)
                        text = ''.join(p.text for p in doc.paragraphs)
                        assert '二维码说明文字' in text
                        assert '人工校对验证。' in text
                        if mode == 'text_only':
                            assert len(doc.paragraphs) == 1 and not doc.inline_shapes and not doc.tables
                            assert '<w:br' not in doc.element.xml
                        if mode == 'paragraph':
                            assert '<w:framePr' not in doc.element.xml
                            assert '<w:br' not in doc.element.xml
                        exports[mode] = {'paragraphs': len(doc.paragraphs), 'characters': len(text), 'images': len(doc.inline_shapes)}
                    window.export_layout_combo.setCurrentIndex(window.export_layout_combo.findData('paragraph'))
                    window.show_document()
                    report['exports'] = exports
                    report['ocr_elapsed_seconds'] = round(time.monotonic()-start, 3)
                    state['step'] = 'settle'
                    return
                if state['step'] == 'settle':
                    capture('completed-1360.png')
                    window.show_gallery()
                    capture('gallery-completed-1360.png')
                    window.show_document()
                    window.resize(1100, 680)
                    state['step'] = 'small'
                    return
                if state['step'] == 'small':
                    capture('completed-1100.png')
                    window.full_document_button.click()
                    state['step'] = 'editor-settle'
                    return
                if state['step'] == 'editor-settle':
                    capture('comparison-1100.png')
                    for widget in (window.document_editor, window.details_button, window.export_navigation_button, window.compare_button, window.page_combo):
                        assert widget.isVisible()
                        assert window.rect().contains(widget.mapTo(window, QPoint()))
                        assert window.rect().contains(widget.mapTo(window, widget.rect().bottomRight())), (widget.accessibleName(), str(widget.geometry()))
                    window.theme_button.click()
                    capture('comparison-light-1100.png')
                    report.update(status='ok', queue_add_remove_verified=True, native_qt=True,
                                  logical_size=[window.width(), window.height()], scale=window.devicePixelRatioF())
                    state['step'] = 'native-frame'
                    return
                if state['step'] == 'native-frame':
                    from offline_word import theme
                    from PySide6.QtGui import QFontDatabase, QFontInfo
                    assert 'MiSans' in QFontDatabase.applicationFontFamilies(theme._font_ids[0])
                    assert QFontInfo(window.import_button.font()).family() == 'MiSans'
                    report['bundled_misans'] = True
                    report['glass_result'] = window._dwm_result
                    theme.set_effect(window, 'glass', False, persist=False)
                    assert window._dwm_result == 'opaque'
                    theme.set_effect(window, 'glass', True, persist=False)
                    window.maximize_button.click()
                    state['step'] = 'maximized'
                    return
                if state['step'] == 'maximized':
                    assert window.isMaximized()
                    report['maximized'] = [window.width(), window.height()]
                    window.maximize_button.click()
                    state['step'] = 'restored'
                    return
                if state['step'] == 'restored':
                    assert not window.isMaximized()
                    assert window.width() == 1100 and window.height() == 680
                    window.minimize_button.click()
                    state['step'] = 'minimized'
                    return
                if state['step'] == 'minimized':
                    assert window.isMinimized()
                    assert window.motion.layer._image.isNull()
                    window.showNormal()
                    report['window_controls_verified'] = True
                    timer.stop()
                    window.close()
                    app.exit(0)
            except Exception:
                debug(traceback.format_exc())
                timer.stop()
                report['status'] = 'failed'
                report['error'] = traceback.format_exc()
                window._confirm_discard_results = lambda *a, **k: True
                window.close()
                app.exit(1)
        timer.timeout.connect(tick)
        timer.start(50)
        result = app.exec()
        debug(f'app exec returned {result}')
    report['session_cleaned'] = not session_path.exists()
    (args.output/'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return result


if __name__ == '__main__':
    multiprocessing.freeze_support()
    raise SystemExit(main())
