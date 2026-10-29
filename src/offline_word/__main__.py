"""Desktop entry point; importing this module never initializes OCR or Qt."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
import importlib.metadata
import json
import multiprocessing
import os
from pathlib import Path
import platform
import sys
import time
from types import SimpleNamespace


_SMOKE_OCR_TIMEOUT_SECONDS = 600.0


def locate_models(override: Path | None = None) -> Path:
    if override is not None:
        return Path(override)
    if getattr(sys, 'frozen', False):
        executable = Path(sys.executable).resolve()
        if sys.platform == 'darwin':
            # .../<release>/<app>.app/Contents/MacOS/<executable>
            return executable.parents[3] / 'models'
        return executable.parent / 'models'
    return Path(__file__).resolve().parents[2] / 'models'


@contextmanager
def quiet_dependency_output():
    """Suppress Python and native dependency diagnostics during environment checks."""
    saved = {}
    with open(os.devnull, 'w', encoding='utf-8') as sink:
        for stream in (sys.stdout, sys.stderr):
            if stream is not None:
                stream.flush()
        for descriptor in (1, 2):
            try:
                saved[descriptor] = os.dup(descriptor)
                os.dup2(sink.fileno(), descriptor)
            except OSError:
                pass  # pythonw may have no native console descriptors.
        try:
            with redirect_stdout(sink), redirect_stderr(sink):
                yield
        finally:
            sink.flush()
            for descriptor, original in saved.items():
                try:
                    os.dup2(original, descriptor)
                finally:
                    os.close(original)


def self_check(model_root: Path) -> dict:
    from offline_word.security import SessionStore, configure_offline_env, deny_network, validate_models, OfflineError
    result = {'status': 'failed', 'code': 'environment_failed', 'system': platform.system(),
              'architecture': platform.machine(), 'python': platform.python_version(),
              'ocr_inference_verified': False, 'versions': {}}
    # All dependency output is suppressed, including third-party exceptions containing paths.
    session = None
    try:
        session = SessionStore()
        configure_offline_env(model_root, session.path)
        manifest = validate_models(model_root)
        deny_network()
        with quiet_dependency_output():
            for name in ('PySide6-Essentials', 'paddleocr', 'paddlex', 'paddlepaddle', 'python-docx'):
                result['versions'][name] = importlib.metadata.version(name)
            import paddle  # noqa: F401
            import paddleocr  # noqa: F401
            from PySide6 import QtWidgets  # noqa: F401
        result.update(status='imports_and_models_ready', code='ok', model_count=len(manifest['models']))
    except OfflineError as exc:
        result['code'] = exc.code
    except Exception:
        result['code'] = 'dependency_import_failed'
    finally:
        if session is not None:
            session.close()
    return result


def _load_smoke_dependencies() -> SimpleNamespace:
    """Load OCR-only dependencies without affecting ordinary launcher imports."""
    from offline_word.domain import ImageJob
    from offline_word.security import (
        OfflineError,
        SessionStore,
        configure_offline_env,
        deny_network,
        validate_models,
    )
    from offline_word.worker import OCRWorker

    return SimpleNamespace(
        ImageJob=ImageJob,
        OfflineError=OfflineError,
        SessionStore=SessionStore,
        configure_offline_env=configure_offline_env,
        validate_models=validate_models,
        deny_network=deny_network,
        OCRWorker=OCRWorker,
    )


def _smoke_failure(code: str) -> dict:
    return {'status': 'failed', 'code': code, 'ocr_inference_verified': False}


def _smoke_normalized_text(value: str) -> str:
    return ''.join(str(value).split())


def _smoke_candidate_units(page: object) -> int:
    total = 0
    for block in page.blocks:
        units = sorted(block.cells, key=lambda cell: (cell.row, cell.col)) if block.kind == 'table' and block.cells else [block]
        for unit in units:
            if any(
                _smoke_normalized_text(candidate) != _smoke_normalized_text(unit.original_text)
                for candidate in unit.candidates
            ):
                total += 1
    return total


def _smoke_baseline_preserved(page: object) -> bool:
    for block in page.blocks:
        if _smoke_normalized_text(block.text) != _smoke_normalized_text(block.original_text):
            return False
        for cell in block.cells:
            if _smoke_normalized_text(cell.text) != _smoke_normalized_text(cell.original_text):
                return False
    return True


def _smoke_result(job: object) -> dict:
    if job.status != 'done' or job.result is None:
        return _smoke_failure('smoke_ocr_failed')
    page = job.result
    if not page.blocks or page.proofread_completed_passes != 3:
        return _smoke_failure('smoke_ocr_incomplete')
    if not _smoke_baseline_preserved(page):
        return _smoke_failure('smoke_ocr_baseline_changed')
    return {
        'status': 'ok',
        'code': 'ok',
        'ocr_inference_verified': True,
        'proofread_completed_passes': int(page.proofread_completed_passes),
        'block_count': len(page.blocks),
        'table_count': sum(block.kind == 'table' for block in page.blocks),
        'candidate_units': _smoke_candidate_units(page),
        'detail_reviewed_regions': int(page.detail_reviewed_regions),
        'detail_candidate_regions': int(page.detail_candidate_regions),
        'detail_review_status': str(page.detail_review_status),
        'baseline_preserved': True,
    }


def smoke_ocr(image: Path, model_root: Path, *, timeout_seconds: float = _SMOKE_OCR_TIMEOUT_SECONDS) -> dict:
    """Run one private three-pass OCR job and return a text-free diagnostic."""
    result = _smoke_failure('smoke_ocr_setup_failed')
    session = None
    worker = None
    started = False
    try:
        # Suppress both parent-side libraries and the spawned OCR child's inherited
        # descriptors. Only ``main`` prints the completed safe result afterwards.
        with quiet_dependency_output():
            try:
                dependencies = _load_smoke_dependencies()
                session = dependencies.SessionStore()
                dependencies.configure_offline_env(model_root, session.path)
                dependencies.validate_models(model_root)
                dependencies.deny_network()
                worker = dependencies.OCRWorker(model_root, session.path)
                job = dependencies.ImageJob(
                    id='smoke-ocr', source_path=str(image), auto_proofread=True,
                )
                worker.start([job])
                started = True
                deadline = time.monotonic() + max(0.0, timeout_seconds)
                while time.monotonic() < deadline:
                    worker.poll()
                    if job.status in {'done', 'failed'} or not worker.busy:
                        break
                    time.sleep(0.02)
                if job.status == 'done':
                    result = _smoke_result(job)
                elif job.status == 'failed':
                    result = _smoke_failure('smoke_ocr_failed')
                elif time.monotonic() >= deadline:
                    worker.cancel()
                    result = _smoke_failure('smoke_ocr_timeout')
                else:
                    result = _smoke_failure('smoke_ocr_failed')
            except Exception:
                result = _smoke_failure('smoke_ocr_failed' if started else 'smoke_ocr_setup_failed')
            finally:
                cleanup_failed = False
                if worker is not None:
                    if result['code'] != 'ok':
                        try:
                            worker.cancel()
                        except Exception:
                            cleanup_failed = True
                    try:
                        worker.close()
                    except Exception:
                        cleanup_failed = True
                if session is not None:
                    try:
                        session.close()
                    except Exception:
                        cleanup_failed = True
                if cleanup_failed:
                    result = _smoke_failure('smoke_ocr_cleanup_failed')
    except Exception:
        return _smoke_failure('smoke_ocr_failed')
    return result


def install_chinese_qt_catalog(app):
    """Install Qt's bundled Simplified Chinese catalog for standard widgets."""
    from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator

    QLocale.setDefault(QLocale("zh_CN"))
    translator = QTranslator(app)
    catalog = (
        Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))
        / "qtbase_zh_CN.qm"
    )
    if not translator.load(str(catalog)):
        return None
    app.installTranslator(translator)
    return translator


def main() -> int:
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description='Offline image to editable Word')
    parser.add_argument('--model-dir', type=Path)
    parser.add_argument('--diag', action='store_true', help='Enable session-only, text-free timing diagnostics')
    diagnostics = parser.add_mutually_exclusive_group()
    diagnostics.add_argument('--self-check', action='store_true')
    diagnostics.add_argument('--verify-package', type=Path, metavar='FOLDER')
    diagnostics.add_argument('--smoke-ocr', type=Path, metavar='IMAGE')
    args = parser.parse_args()
    if args.diag:
        os.environ['OFFLINE_WORD_DIAG'] = '1'
    if args.verify_package is not None:
        from offline_word.package_verify import verify
        try:
            result = verify(args.verify_package)
        except (OSError, TypeError, ValueError):
            result = {'pass': False, 'code': 'package_verification_failed'}
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['pass'] else 1
    models = locate_models(args.model_dir)
    if args.smoke_ocr is not None:
        result = smoke_ocr(args.smoke_ocr, models)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['code'] == 'ok' else 1
    if args.self_check:
        result = self_check(models)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['code'] == 'ok' else 1
    from offline_word.security import SessionStore, configure_offline_env, deny_network
    session = SessionStore()
    installation = ExitStack()
    try:
        configure_offline_env(models, session.path)
        deny_network()
        root = Path(__file__).resolve().parents[2]
        if (root/'checksums.json').is_file():
            from offline_word.update_lock import installation_lock
            from offline_word.offline_update import ensure_update_finished
            installation.enter_context(installation_lock(root, shared=True))
            ensure_update_finished(root)
        from PySide6.QtWidgets import QApplication
        from offline_word.gui import MainWindow
        app = QApplication(sys.argv[:1])
        app.setApplicationName('离线图片转 Word')
        from offline_word.theme import application_icon
        app.setWindowIcon(application_icon())
        app.setOrganizationName('OfflineWord')
        from offline_word.version import __version__
        app.setApplicationVersion(__version__)
        # Keep the translator referenced for the lifetime of the event loop.
        qt_translator = install_chinese_qt_catalog(app)
        window = MainWindow(models, session.path)
        window.show()
        from PySide6.QtCore import QTimer
        from offline_word.release_dialog import show_release_dialog
        QTimer.singleShot(350, lambda: show_release_dialog(window, welcome=True) if window.isVisible() else None)
        try:
            return app.exec()
        finally:
            del qt_translator
    finally:
        session.close()
        installation.close()


if __name__ == '__main__':
    raise SystemExit(main())
