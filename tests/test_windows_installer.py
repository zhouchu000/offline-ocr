import hashlib
import json
from offline_word.version import __version__
from pathlib import Path
from types import SimpleNamespace

import pytest


MODEL_NAMES = (
    "PP-DocLayout_plus-L",
    "PP-OCRv6_medium_det",
    "PP-OCRv6_medium_rec",
    "PP-LCNet_x1_0_doc_ori",
    "PP-LCNet_x1_0_textline_ori",
    "PP-LCNet_x1_0_table_cls",
    "SLANeXt_wired",
    "SLANet_plus",
    "RT-DETR-L_wired_table_cell_det",
    "RT-DETR-L_wireless_table_cell_det",
)


def _write_checksums(folder: Path) -> None:
    checksums = {
        path.relative_to(folder).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(folder.rglob("*"))
        if path.is_file() and path.name != "checksums.json"
    }
    (folder / "checksums.json").write_text(
        json.dumps(checksums, ensure_ascii=False), encoding="utf-8"
    )


def _portable_fixture(tmp_path: Path) -> Path:
    payload = tmp_path / f"OfflineWord-{__version__}-Windows-x64"
    icon = payload / 'app/offline_word/assets/app.ico'
    icon.parent.mkdir(parents=True)
    icon.write_bytes(Path('src/offline_word/assets/app.ico').read_bytes())
    (payload / "runtime").mkdir(parents=True)
    (payload / "runtime" / "python.exe").write_bytes(b"MZ-fixture")
    (payload / "runtime" / "pythonw.exe").write_bytes(b"MZ-fixture")
    (payload / "launch.pyw").write_text("pass\n", encoding="utf-8")
    (payload / "verify_package.py").write_text("pass\n", encoding="utf-8")
    (payload / "启动程序.cmd").write_text("start\n", encoding="utf-8")
    (payload / "检查环境.cmd").write_text("check\n", encoding="utf-8")
    (payload / "核对文件.cmd").write_text("verify\n", encoding="utf-8")
    models = payload / "models"
    models.mkdir()
    manifest_models = {}
    for name in MODEL_NAMES:
        model = models / name
        model.mkdir()
        content = name.encode("utf-8")
        (model / "model.pdmodel").write_bytes(content)
        manifest_models[name] = {
            "directory": name,
            "files": {"model.pdmodel": hashlib.sha256(content).hexdigest()},
        }
    (models / "manifest.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "versions": {
                    "paddleocr": "3.7.0",
                    "paddlex": "3.7.0",
                    "paddlepaddle": "3.3.0",
                },
                "models": manifest_models,
            }
        ),
        encoding="utf-8",
    )
    (payload / "build-info.json").write_text(
        json.dumps(
            {
                "format": 1,
                "application_version": __version__,
                "target": "Windows 10/11 x64",
                "windows_execution_verified": False,
                "accuracy_verified": False,
            }
        ),
        encoding="utf-8",
    )
    _write_checksums(payload)
    return payload


def test_nsis_script_is_chinese_per_user_and_installs_complete_offline_payload():
    script = Path("packaging/windows/OfflineWord.nsi").read_text(encoding="utf-8")

    assert "Unicode True" in script
    assert 'RequestExecutionLevel user' in script
    assert 'InstallDir "$LOCALAPPDATA\\Programs\\OfflineWord"' in script
    assert 'StrCpy $INSTDIR "$LOCALAPPDATA\\Programs\\OfflineWord"' in script
    assert 'Delete "$INSTDIR\\uninstall.exe"' in script
    assert "InstallDirRegKey" not in script
    assert '$LOCALAPPDATA\\Programs\\离线图片转Word\\*.*' in script
    assert 'ReadRegStr $0 HKCU "${APP_REG_KEY}" "InstallLocation"' in script
    assert "安全移除旧版程序文件" in script
    assert 'File /r "${PAYLOAD_FILES}"' in script
    assert "${AtLeastWin10}" in script
    assert "${RunningX64}" in script
    assert "仅支持 Windows 10/11 64 位系统" in script
    assert "$SMPROGRAMS\\离线图片转Word" in script
    assert "$DESKTOP\\离线图片转Word.lnk" in script
    assert "WriteUninstaller" in script
    assert "UninstallString" in script
    assert "MUI_LANGUAGE \"SimpChinese\"" in script
    assert "http://" not in script.casefold()
    assert "https://" not in script.casefold()
    assert "inetc" not in script.casefold()
    assert "nsisdl" not in script.casefold()


def test_nsis_uninstaller_is_confined_to_a_dedicated_ascii_directory():
    """Would fail if the uninstaller could recursively remove the Programs root."""
    script = Path("packaging/windows/OfflineWord.nsi").read_text(encoding="utf-8")

    assert '!define UNINSTALLER "$LOCALAPPDATA\\OfflineWord-Uninstaller\\uninstall.exe"' in script
    assert '!define UNINSTALLER "$LOCALAPPDATA\\Programs' not in script
    assert '$LOCALAPPDATA\\Programs\\离线图片转Word-卸载程序.exe' in script
    assert "Function un.onInit" in script
    assert 'StrCmp $INSTDIR "$LOCALAPPDATA\\OfflineWord-Uninstaller" uninstaller_location_ok' in script
    assert 'StrCmp $EXEDIR "$LOCALAPPDATA\\OfflineWord-Uninstaller"' not in script
    assert 'StrCpy $INSTDIR "$LOCALAPPDATA\\Programs\\OfflineWord"' in script
    assert 'RMDir /r "$LOCALAPPDATA\\Programs\\离线图片转Word"' in script


def test_nsis_toolchain_is_pinned_to_official_archives():
    lock = json.loads(Path("packaging/windows/nsis.lock.json").read_text(encoding="utf-8"))

    assert lock["version"] == "3.12"
    for artifact in ("windows_zip", "source_tarball"):
        assert lock[artifact]["url"].startswith("https://downloads.sourceforge.net/project/nsis/")
        digest = lock[artifact]["sha256"]
        assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")
    license_text = Path("licenses/NSIS-3.12-LICENSE.txt").read_text(encoding="utf-8")
    notices = Path("THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "Copyright (C) 1999-2026 Contributors" in license_text
    assert "ZLIB/LIBPNG LICENSE" in license_text
    assert "COMMON PUBLIC LICENSE VERSION 1.0" in license_text
    assert "NSIS 3.12" in notices and "licenses/NSIS-3.12-LICENSE.txt" in notices


def test_builder_validates_payload_and_invokes_compiler_without_overwrite(tmp_path, monkeypatch):
    from tools import build_windows_installer

    payload = _portable_fixture(tmp_path)
    output = tmp_path / f"离线图片转Word-{__version__}-Windows-x64-安装程序.exe"
    captured = {}

    def fake_run(command, **kwargs):
        if command[-1] == "-VERSION":
            return SimpleNamespace(stdout="v3.12\n")
        captured["command"] = command
        captured["kwargs"] = kwargs
        staged_output = Path(next(item for item in command if item.startswith("-DOUTPUT_FILE=")).split("=", 1)[1])
        staged_output.write_bytes(b"MZ-installer")

    monkeypatch.setattr(build_windows_installer.subprocess, "run", fake_run)
    report = build_windows_installer.build_installer(
        payload,
        output,
        makensis=tmp_path / "makensis",
        script=Path("packaging/windows/OfflineWord.nsi"),
    )

    assert captured["command"][0] == str(tmp_path / "makensis")
    assert captured["command"][1:3] == ["-INPUTCHARSET", "UTF8"]
    assert f"-DPAYLOAD_FILES={payload.resolve() / '*.*'}" in captured["command"]
    assert any(item == f"-DAPP_VERSION={__version__}" for item in captured["command"])
    assert any(item.startswith("-DOUTPUT_FILE=") for item in captured["command"])
    assert captured["kwargs"]["check"] is True
    assert report["application_version"] == __version__
    assert report["payload_files_checked"] == len(
        json.loads((payload / "checksums.json").read_text(encoding="utf-8"))
    )
    assert report["payload_models"] == 10
    assert report["nsis_version"] == "3.12"
    assert report["windows_execution_verified"] is False
    assert report["installer_sha256"] == hashlib.sha256(b"MZ-installer").hexdigest()

    with pytest.raises(FileExistsError):
        build_windows_installer.build_installer(
            payload,
            output,
            makensis=tmp_path / "makensis",
            script=Path("packaging/windows/OfflineWord.nsi"),
        )


def test_builder_rejects_modified_payload_before_makensis(tmp_path, monkeypatch):
    from tools import build_windows_installer

    payload = _portable_fixture(tmp_path)
    (payload / "launch.pyw").write_text("changed\n", encoding="utf-8")
    called = False

    def fake_run(*args, **_kwargs):
        nonlocal called
        called = True
        return SimpleNamespace(stdout="v3.12\n")

    monkeypatch.setattr(build_windows_installer.subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="integrity"):
        build_windows_installer.build_installer(
            payload,
            tmp_path / "installer.exe",
            makensis=tmp_path / "makensis",
            script=Path("packaging/windows/OfflineWord.nsi"),
        )
    assert called is False


def test_failed_compile_does_not_publish_installer(tmp_path, monkeypatch):
    from tools import build_windows_installer

    payload = _portable_fixture(tmp_path)
    output = tmp_path / "installer.exe"

    def fake_run(command, **_kwargs):
        if command[-1] == "-VERSION":
            return SimpleNamespace(stdout="v3.12\n")
        raise build_windows_installer.subprocess.CalledProcessError(1, "makensis")

    monkeypatch.setattr(build_windows_installer.subprocess, "run", fake_run)
    with pytest.raises(build_windows_installer.subprocess.CalledProcessError):
        build_windows_installer.build_installer(
            payload,
            output,
            makensis=tmp_path / "makensis",
            script=Path("packaging/windows/OfflineWord.nsi"),
        )
    assert not output.exists()


def test_builder_rejects_unpinned_nsis_version(tmp_path, monkeypatch):
    from tools import build_windows_installer

    payload = _portable_fixture(tmp_path)
    monkeypatch.setattr(
        build_windows_installer.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="v3.11\n"),
    )

    with pytest.raises(ValueError, match="NSIS version"):
        build_windows_installer.build_installer(
            payload,
            tmp_path / "installer.exe",
            makensis=tmp_path / "makensis",
            script=Path("packaging/windows/OfflineWord.nsi"),
        )
