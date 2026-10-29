import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


def test_model_location_follows_package_not_working_directory(tmp_path, monkeypatch):
    from offline_word.__main__ import locate_models
    original = locate_models()
    monkeypatch.chdir(tmp_path)
    assert locate_models() == original
    assert locate_models(tmp_path / 'custom-models') == tmp_path / 'custom-models'


def test_chinese_qt_catalog_localizes_file_dialog_controls(qapp):
    """Would fail if the Qt chooser still exposed English navigation labels."""
    from PySide6.QtCore import QLocale
    from PySide6.QtWidgets import QApplication
    from offline_word import __main__

    translator = __main__.install_chinese_qt_catalog(qapp)

    assert translator is not None
    assert not translator.isEmpty()
    assert QLocale().name() == "zh_CN"
    assert QApplication.translate("QFileDialog", "Back") == "后退"
    assert QApplication.translate("QFileSystemModel", "Name") == "名称"


def test_frozen_macos_model_location_is_sibling_of_app(monkeypatch):
    """Would fail if a frozen macOS build searched inside its .app bundle."""
    from offline_word.__main__ import locate_models

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "executable", "/release/离线图片转Word.app/Contents/MacOS/离线图片转Word")

    assert locate_models() == Path("/release/models").resolve()


def test_verify_package_switch_uses_frozen_runtime_and_returns_safe_json(tmp_path, monkeypatch, capsys):
    """Would fail if the macOS check script needed an installed Python interpreter."""
    from offline_word import __main__

    payload = tmp_path / "payload.txt"
    payload.write_text("verified", encoding="utf-8")
    (tmp_path / "checksums.json").write_text(
        json.dumps({"payload.txt": hashlib.sha256(b"verified").hexdigest()}), encoding="utf-8"
    )
    monkeypatch.setattr(sys, "argv", ["离线图片转Word", "--verify-package", str(tmp_path)])

    assert __main__.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "pass": True,
        "checked": 1,
        "links": 0,
        "missing": 0,
        "changed": 0,
        "extra": 0,
    }


def test_frozen_package_verifier_normalizes_cyclic_links_to_safe_json(tmp_path, monkeypatch, capsys):
    """Would fail if malformed links made the frozen verifier reveal a traceback."""
    from offline_word import __main__

    os.symlink("self", tmp_path / "self")
    (tmp_path / "checksums.json").write_text(
        json.dumps({"self": {"type": "symlink", "target": "self"}}), encoding="utf-8"
    )
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "argv", ["离线图片转Word", "--verify-package", str(tmp_path)])

    assert __main__.main() == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {"pass": False, "code": "package_verification_failed"}
    assert str(tmp_path) not in captured.out
    assert "Traceback" not in captured.out


def test_launcher_source_path_execution_keeps_self_check_in_package_context(tmp_path):
    """Would fail if PyInstaller executed __main__.py without resolving its package imports."""
    launcher = Path(__file__).parents[1] / "src" / "offline_word" / "__main__.py"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(launcher.parents[1])

    result = subprocess.run(
        [sys.executable, str(launcher), "--model-dir", str(tmp_path / "missing"), "--self-check"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stderr == ""
    assert json.loads(result.stdout)["code"] == "model_manifest_missing"


def test_launcher_source_path_execution_keeps_package_verifier_in_package_context(tmp_path):
    """Would fail if the frozen file checker could not import its bundled verifier."""
    launcher = Path(__file__).parents[1] / "src" / "offline_word" / "__main__.py"
    payload = tmp_path / "payload.txt"
    payload.write_text("verified", encoding="utf-8")
    (tmp_path / "checksums.json").write_text(
        json.dumps({"payload.txt": hashlib.sha256(b"verified").hexdigest()}), encoding="utf-8"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(launcher.parents[1])

    result = subprocess.run(
        [sys.executable, str(launcher), "--verify-package", str(tmp_path)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert json.loads(result.stdout)["pass"] is True


def test_self_check_missing_models_has_safe_code_only(tmp_path):
    from offline_word.__main__ import self_check
    result = self_check(tmp_path / 'private_name_DO_NOT_LOG')
    assert result['status'] == 'failed'
    assert isinstance(result['code'], str)
    assert 'private_name_DO_NOT_LOG' not in json.dumps(result)
    assert result['ocr_inference_verified'] is False
def test_dependency_diagnostics_hide_native_stdout_and_stderr():
    import subprocess
    import sys
    code = '''
import os
from offline_word.__main__ import quiet_dependency_output
with quiet_dependency_output():
    os.write(1, b"private native stdout\\n")
    os.write(2, b"private native stderr\\n")
    print("private Python output")
print("safe result")
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == 'safe result'
    assert result.stderr == ''


def _smoke_page(*, completed_passes: int = 3, edited_text: str | None = None):
    from offline_word.domain import Block, Cell, PageResult

    return PageResult(
        id="smoke-page",
        page_index=0,
        width=10,
        height=10,
        processed_image_path="private/processed.png",
        proofread_completed_passes=completed_passes,
        detail_reviewed_regions=4,
        detail_candidate_regions=1,
        detail_review_status="candidate",
        blocks=[
            Block(
                id="paragraph",
                kind="text",
                bbox=(0, 0, 1, 1),
                original_text="PRIVATE OCR TEXT",
                edited_text=edited_text,
                candidates=["PRIVATE OCR TEXT", "候选文本"],
            ),
            Block(
                id="table",
                kind="table",
                bbox=(0, 1, 1, 2),
                cells=[Cell(row=0, col=0, original_text="表格原文", candidates=["候选单元格"])],
            ),
        ],
    )


def _install_smoke_fakes(monkeypatch, *, page=None, outcome="done", noisy=False, cancel_raises=False):
    from offline_word import __main__

    state = SimpleNamespace(sessions=[], workers=[], configured=[], denied=0, validated=[])

    class FakeSession:
        def __init__(self):
            self.path = Path("private/session")
            self.closed = False
            state.sessions.append(self)

        def close(self):
            self.closed = True

    class FakeWorker:
        def __init__(self, model_root, session_dir):
            self.model_root = model_root
            self.session_dir = session_dir
            self.jobs = []
            self.busy = False
            self.cancelled = False
            self.closed = False
            state.workers.append(self)

        def start(self, jobs):
            self.jobs = jobs
            self.busy = True
            if noisy:
                print("PRIVATE OCR TEXT")
                os.write(2, b"private/input.png\\n")

        def poll(self):
            if outcome == "timeout":
                return []
            self.busy = False
            job = self.jobs[0]
            if outcome == "failed":
                job.status = "failed"
                job.error_code = "private_error"
            else:
                job.status = "done"
                job.result = page
            return [{"event": "page_error" if outcome == "failed" else "page_done"}]

        def cancel(self):
            self.cancelled = True
            self.busy = False
            if cancel_raises:
                raise RuntimeError("private cancellation failure")

        def close(self):
            self.closed = True

    def configure(model_root, session_dir):
        state.configured.append((model_root, session_dir))

    def validate(model_root):
        state.validated.append(model_root)
        return {"models": ["fixed-local-model"]}

    def deny():
        state.denied += 1

    dependencies = SimpleNamespace(
        ImageJob=__import__("offline_word.domain", fromlist=["ImageJob"]).ImageJob,
        OfflineError=RuntimeError,
        SessionStore=FakeSession,
        configure_offline_env=configure,
        validate_models=validate,
        deny_network=deny,
        OCRWorker=FakeWorker,
    )
    monkeypatch.setattr(__main__, "_load_smoke_dependencies", lambda: dependencies)
    return state


def test_smoke_ocr_runs_three_pass_worker_and_emits_only_safe_facts(tmp_path, monkeypatch, capsys):
    from offline_word import __main__

    state = _install_smoke_fakes(monkeypatch, page=_smoke_page(), noisy=True)
    result = __main__.smoke_ocr(tmp_path / "private-input.png", tmp_path / "models")

    assert result == {
        "status": "ok",
        "code": "ok",
        "ocr_inference_verified": True,
        "proofread_completed_passes": 3,
        "block_count": 2,
        "table_count": 1,
        "candidate_units": 2,
        "detail_reviewed_regions": 4,
        "detail_candidate_regions": 1,
        "detail_review_status": "candidate",
        "baseline_preserved": True,
    }
    assert state.workers[0].jobs[0].auto_proofread is True
    assert state.configured == [(tmp_path / "models", Path("private/session"))]
    assert state.denied == 1
    assert state.validated == [tmp_path / "models"]
    assert state.workers[0].closed is True
    assert state.sessions[0].closed is True
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    serialized = json.dumps(result, ensure_ascii=False)
    assert "PRIVATE OCR TEXT" not in serialized
    assert "private-input.png" not in serialized
    assert "processed.png" not in serialized


def test_smoke_ocr_timeout_and_failed_job_use_fixed_codes_and_cleanup(tmp_path, monkeypatch):
    from offline_word import __main__

    timeout_state = _install_smoke_fakes(monkeypatch, page=_smoke_page(), outcome="timeout")
    timed_out = __main__.smoke_ocr(tmp_path / "private-input.png", tmp_path / "models", timeout_seconds=0)

    assert timed_out == {"status": "failed", "code": "smoke_ocr_timeout", "ocr_inference_verified": False}
    assert timeout_state.workers[0].cancelled is True
    assert timeout_state.workers[0].closed is True
    assert timeout_state.sessions[0].closed is True

    failed_state = _install_smoke_fakes(monkeypatch, page=_smoke_page(), outcome="failed")
    failed = __main__.smoke_ocr(tmp_path / "private-input.png", tmp_path / "models")

    assert failed == {"status": "failed", "code": "smoke_ocr_failed", "ocr_inference_verified": False}
    assert failed_state.workers[0].closed is True
    assert failed_state.sessions[0].closed is True
    assert "private_error" not in json.dumps(failed)


def test_smoke_ocr_attempts_close_after_a_cancellation_error(tmp_path, monkeypatch):
    from offline_word import __main__

    state = _install_smoke_fakes(
        monkeypatch, page=_smoke_page(), outcome="timeout", cancel_raises=True,
    )
    result = __main__.smoke_ocr(tmp_path / "private-input.png", tmp_path / "models", timeout_seconds=0)

    assert result == {"status": "failed", "code": "smoke_ocr_cleanup_failed", "ocr_inference_verified": False}
    assert state.workers[0].closed is True
    assert state.sessions[0].closed is True


def test_smoke_ocr_rejects_incomplete_or_changed_baseline(tmp_path, monkeypatch):
    from offline_word import __main__

    _install_smoke_fakes(monkeypatch, page=_smoke_page(completed_passes=2))
    incomplete = __main__.smoke_ocr(tmp_path / "private-input.png", tmp_path / "models")
    assert incomplete == {"status": "failed", "code": "smoke_ocr_incomplete", "ocr_inference_verified": False}

    empty_page = _smoke_page()
    empty_page.blocks.clear()
    _install_smoke_fakes(monkeypatch, page=empty_page)
    empty = __main__.smoke_ocr(tmp_path / "private-input.png", tmp_path / "models")
    assert empty == {"status": "failed", "code": "smoke_ocr_incomplete", "ocr_inference_verified": False}

    _install_smoke_fakes(monkeypatch, page=_smoke_page(edited_text="changed"))
    changed = __main__.smoke_ocr(tmp_path / "private-input.png", tmp_path / "models")
    assert changed == {"status": "failed", "code": "smoke_ocr_baseline_changed", "ocr_inference_verified": False}


def test_smoke_ocr_normalizes_whitespace_before_comparing_the_baseline(tmp_path, monkeypatch):
    from offline_word import __main__

    _install_smoke_fakes(monkeypatch, page=_smoke_page(edited_text="PRIVATE\n OCR  TEXT"))
    result = __main__.smoke_ocr(tmp_path / "private-input.png", tmp_path / "models")

    assert result["code"] == "ok"
    assert result["baseline_preserved"] is True


def test_smoke_ocr_cli_emits_one_json_and_uses_safe_exit_codes(tmp_path, monkeypatch, capsys):
    from offline_word import __main__

    state = _install_smoke_fakes(monkeypatch, page=_smoke_page())
    model_root = tmp_path / "private-models"
    monkeypatch.setattr(sys, "argv", [
        "离线图片转Word", "--model-dir", str(model_root), "--smoke-ocr", str(tmp_path / "private-input.png"),
    ])

    assert __main__.main() == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    assert state.configured == [(model_root, Path("private/session"))]
    assert json.loads(captured.out) == {
        "status": "ok",
        "code": "ok",
        "ocr_inference_verified": True,
        "proofread_completed_passes": 3,
        "block_count": 2,
        "table_count": 1,
        "candidate_units": 2,
        "detail_reviewed_regions": 4,
        "detail_candidate_regions": 1,
        "detail_review_status": "candidate",
        "baseline_preserved": True,
    }

    monkeypatch.setattr(__main__, "smoke_ocr", lambda *_args: {"status": "failed", "code": "smoke_ocr_failed", "ocr_inference_verified": False})
    assert __main__.main() == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {"status": "failed", "code": "smoke_ocr_failed", "ocr_inference_verified": False}


def test_smoke_ocr_mode_is_mutually_exclusive_with_other_diagnostics(tmp_path, monkeypatch):
    from offline_word import __main__

    monkeypatch.setattr(
        sys,
        "argv",
        ["离线图片转Word", "--self-check", "--smoke-ocr", str(tmp_path / "private-input.png")],
    )
    with pytest.raises(SystemExit) as exit_info:
        __main__.main()
    assert exit_info.value.code == 2
