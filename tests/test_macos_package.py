import os
import plistlib
from offline_word.version import __version__
import hashlib
import json

import pytest

from tools.verify_package import verify


_MODEL_NAMES = (
    "PP-DocLayout_plus-L", "PP-OCRv6_medium_det", "PP-OCRv6_medium_rec",
    "PP-LCNet_x1_0_doc_ori", "PP-LCNet_x1_0_textline_ori", "PP-LCNet_x1_0_table_cls",
    "SLANeXt_wired", "SLANet_plus", "RT-DETR-L_wired_table_cell_det", "RT-DETR-L_wireless_table_cell_det",
)


def _write_valid_models(models):
    manifest_models = {}
    for name in _MODEL_NAMES:
        directory = models / name
        directory.mkdir(parents=True)
        model_file = directory / "model.pdmodel"
        model_file.write_text(name, encoding="utf-8")
        manifest_models[name] = {
            "directory": name,
            "files": {"model.pdmodel": hashlib.sha256(name.encode()).hexdigest()},
        }
    (models / "manifest.json").write_text(json.dumps({
        "schema": 1,
        "versions": {"paddleocr": "3.7.0", "paddlex": "3.7.0", "paddlepaddle": "3.3.0"},
        "models": manifest_models,
    }), encoding="utf-8")


@pytest.fixture
def fixture_app(tmp_path):
    app = tmp_path / "离线图片转Word.app"
    executable = app / "Contents" / "MacOS" / "离线图片转Word"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture-macos-executable")
    os.symlink("离线图片转Word", executable.parent / "current")
    return app


@pytest.fixture
def fixture_models(tmp_path):
    models = tmp_path / "models"
    models.mkdir()
    _write_valid_models(models)
    return models


def test_macos_release_layout_uses_external_models_and_safe_scripts(tmp_path, fixture_app, fixture_models):
    """Would fail if release models moved into the app or scripts attempted a download."""
    from tools.build_macos import assemble_macos_release

    release = assemble_macos_release(
        fixture_app,
        fixture_models,
        tmp_path / "out",
        project_root=tmp_path,
        codesign=False,
    )

    assert (release / "离线图片转Word.app").is_dir()
    assert (release / "models" / "manifest.json").is_file()
    assert not (release / "离线图片转Word.app" / "models").exists()
    info = plistlib.loads((release / "离线图片转Word.app" / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleDisplayName"] == "离线图片转Word"
    assert info["CFBundleShortVersionString"] == __version__
    assert info["CFBundleDevelopmentRegion"] == "zh_CN"
    assert info["CFBundleAllowMixedLocalizations"] is True
    assert info["CFBundleLocalizations"] == ["zh_CN"]
    checksums = json.loads((release / "checksums.json").read_text(encoding="utf-8"))
    assert checksums["离线图片转Word.app/Contents/MacOS/current"] == {
        "type": "symlink", "target": "离线图片转Word"
    }
    for script_name, required_argument in (("检查环境.command", "--self-check"), ("核对文件.command", "--verify-package")):
        script = release / script_name
        content = script.read_text(encoding="utf-8").casefold()
        assert "http" not in content
        assert "curl" not in content
        assert required_argument in content
        if os.name != "nt":
            assert os.stat(script).st_mode & 0o777 == 0o755
    assert verify(release)["pass"] is True


def test_macos_release_rejects_schema_only_model_manifest(tmp_path, fixture_app):
    """Would fail if direct assembly could bypass full model-tree integrity validation."""
    models = tmp_path / "models"
    models.mkdir()
    (models / "manifest.json").write_text('{"schema": 1}', encoding="utf-8")

    from offline_word.security import OfflineError
    from tools.build_macos import assemble_macos_release

    with pytest.raises(OfflineError, match="model_manifest_invalid"):
        assemble_macos_release(fixture_app, models, tmp_path / "out", project_root=tmp_path, codesign=False)


def test_macos_release_does_not_publish_before_final_integrity_validation(tmp_path, fixture_app, fixture_models, monkeypatch):
    """Would fail if a failed final check left a named release directory behind."""
    from offline_word import package_verify
    from tools import build_macos

    monkeypatch.setattr(package_verify, "verify", lambda _folder: {"pass": False})

    output = tmp_path / "out"
    with pytest.raises(RuntimeError, match="integrity"):
        build_macos.assemble_macos_release(
            fixture_app, fixture_models, output, project_root=tmp_path, codesign=False
        )

    assert not output.exists()
    assert not list(tmp_path.glob(".out-*"))


def test_macos_release_excludes_internal_build_plans_from_delivered_docs(tmp_path, fixture_app, fixture_models):
    """Would fail if the user-facing package exposed internal build planning files."""
    docs = tmp_path / "docs"
    (docs / "superpowers" / "plans").mkdir(parents=True)
    (docs / "使用说明.md").write_text("public", encoding="utf-8")
    (docs / "superpowers" / "plans" / "internal.md").write_text("internal", encoding="utf-8")

    from tools.build_macos import assemble_macos_release

    release = assemble_macos_release(
        fixture_app,
        fixture_models,
        tmp_path / "out",
        project_root=tmp_path,
        codesign=False,
    )

    assert (release / "docs" / "使用说明.md").is_file()
    assert not (release / "docs" / "superpowers").exists()


def test_build_macos_release_can_assemble_a_prebuilt_app_without_pyinstaller(tmp_path, fixture_app, fixture_models, monkeypatch):
    """Would fail if the public builder could only create an untestable in-place release."""
    from tools.build_macos import APP_NAME, build_macos_release
    # Layout assembly is portable; a real Apple signature is a macOS acceptance.
    monkeypatch.setattr("tools.build_macos._codesign", lambda _app: None)
    monkeypatch.setattr("tools.build_macos._verify_codesign", lambda _app: None)

    expected_app = tmp_path / "build" / "macos" / f"{APP_NAME}.app"
    expected_app.parent.mkdir(parents=True)
    fixture_app.rename(expected_app)

    release = build_macos_release(tmp_path, tmp_path / "release", run_pyinstaller=False)

    assert release == tmp_path / "release"
    assert (release / "models" / "manifest.json").is_file()
    assert verify(release)["pass"] is True


def test_pyinstaller_uses_builtin_pyside_hook_without_duplicate_collect_all(tmp_path, monkeypatch):
    """Would fail if PyInstaller collected Qt frameworks twice and could not stage the App."""
    from tools import build_macos

    source = tmp_path / "src" / "offline_word"
    source.mkdir(parents=True)
    (source / "__main__.py").write_text("", encoding="utf-8")
    captured = []

    def fake_run(command, **_kwargs):
        captured.extend(command)
        (tmp_path / "temporary" / "dist" / f"{build_macos.APP_NAME}.app").mkdir(parents=True)

    monkeypatch.setattr(build_macos.subprocess, "run", fake_run)

    build_macos._run_pyinstaller(tmp_path, tmp_path / "temporary")

    assert "--paths" in captured
    assert str(tmp_path / "src") in captured
    assert "--target-architecture" in captured
    assert "arm64" in captured
    assert not any(captured[index:index + 2] == ["--collect-all", "PySide6"] for index in range(len(captured)))
    for distribution in ("PySide6-Essentials", "paddleocr", "paddlex", "paddlepaddle", "python-docx"):
        assert any(captured[index:index + 2] == ["--copy-metadata", distribution] for index in range(len(captured)))
    paddlex_ocr_metadata = (
        "beautifulsoup4", "einops", "ftfy", "imagesize", "Jinja2", "latex2mathml", "lxml",
        "opencv-contrib-python", "openpyxl", "premailer", "pyclipper", "pypdfium2", "python-bidi",
        "regex", "safetensors", "scikit-learn", "scipy", "sentencepiece", "shapely", "tiktoken",
        "tokenizers",
    )
    for distribution in paddlex_ocr_metadata:
        assert any(
            captured[index:index + 2] == ["--copy-metadata", distribution]
            for index in range(len(captured))
        ), distribution
