"""Build one offline Windows installer from a verified portable payload."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from offline_word.package_verify import verify
from offline_word.security import validate_models


from offline_word.version import __version__ as VERSION
APP_VERSION_NUM = f"{VERSION}.0"
NSIS_VERSION = "3.12"
EXPECTED_TARGET = "Windows 10/11 x64"
REQUIRED_FILES = (
    "app/offline_word/assets/app.ico",
    "runtime/python.exe",
    "runtime/pythonw.exe",
    "launch.pyw",
    "verify_package.py",
    "启动程序.cmd",
    "检查环境.cmd",
    "核对文件.cmd",
    "models/manifest.json",
    "build-info.json",
    "checksums.json",
)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_define_path(path: Path) -> str:
    # NSIS resolves File wildcards on the build host. Windows FindFirstFile
    # requires native separators; POSIX builds retain their native slashes.
    value = str(path.resolve())
    if any(character in value for character in ('"', "\n", "\r", "$")):
        raise ValueError("unsupported path for NSIS define")
    return value


def validate_payload(payload: Path) -> dict[str, int | str | bool]:
    payload = payload.resolve()
    if payload.is_symlink() or not payload.is_dir():
        raise ValueError("portable payload directory missing")
    for relative in REQUIRED_FILES:
        path = payload / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"portable payload file missing: {relative}")

    integrity = verify(payload)
    if not integrity["pass"]:
        raise ValueError("portable payload integrity check failed")

    build_info = json.loads((payload / "build-info.json").read_text(encoding="utf-8"))
    if build_info.get("application_version") != VERSION:
        raise ValueError("portable payload version mismatch")
    if build_info.get("target") != EXPECTED_TARGET:
        raise ValueError("portable payload target mismatch")
    if build_info.get("windows_execution_verified") is not False:
        raise ValueError("portable payload has invalid Windows evidence state")

    validate_models(payload / "models")
    model_manifest = json.loads((payload / "models" / "manifest.json").read_text(encoding="utf-8"))
    models = model_manifest.get("models")
    if not isinstance(models, dict) or len(models) != 10:
        raise ValueError("portable payload must contain all 10 pinned models")

    size_bytes = sum(path.stat().st_size for path in payload.rglob("*") if path.is_file())
    return {
        "application_version": VERSION,
        "payload_files_checked": int(integrity["checked"]),
        "payload_links": int(integrity["links"]),
        "payload_models": len(models),
        "payload_size_bytes": size_bytes,
        "windows_execution_verified": False,
    }


def _check_makensis_version(makensis: Path) -> str:
    result = subprocess.run(
        [str(makensis), "-VERSION"],
        check=True,
        capture_output=True,
        text=True,
    )
    version = result.stdout.strip().removeprefix("v")
    if version != NSIS_VERSION:
        raise ValueError(f"NSIS version mismatch: expected {NSIS_VERSION}, got {version or 'unknown'}")
    return version


def build_installer(
    payload: Path,
    output: Path,
    *,
    makensis: Path,
    script: Path,
    fluent: bool = False,
) -> dict[str, int | str | bool]:
    output = output.resolve()
    if output.exists():
        raise FileExistsError("installer output already exists")
    if output.suffix.casefold() != ".exe":
        raise ValueError("installer output must use .exe")
    if not script.is_file():
        raise FileNotFoundError("NSIS script missing")

    report = validate_payload(payload)
    report["nsis_version"] = _check_makensis_version(makensis)
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".windows-installer-", dir=output.parent))
    staged_output = stage / output.name
    try:
        command = [
            str(makensis),
            "-INPUTCHARSET",
            "UTF8",
            "-V2",
            f"-DPAYLOAD_FILES={_safe_define_path(payload / '*.*')}",
            f"-DAPP_ICON={_safe_define_path(payload / 'app/offline_word/assets/app.ico')}",
            f"-DOUTPUT_FILE={_safe_define_path(staged_output)}",
            f"-DAPP_VERSION={VERSION}",
            f"-DAPP_VERSION_NUM={APP_VERSION_NUM}",
            f"-DPAYLOAD_KB={math.ceil(int(report['payload_size_bytes']) / 1024)}",
            str(script.resolve()),
        ]
        environment = os.environ.copy()
        if fluent:
            command.insert(-1, '-DFLUENT_BACKEND')
        environment.setdefault("SOURCE_DATE_EPOCH", "946684800")
        subprocess.run(command, check=True, env=environment)
        if not staged_output.is_file():
            raise RuntimeError("NSIS did not produce a Windows executable")
        with staged_output.open("rb") as stream:
            if stream.read(2) != b"MZ":
                raise RuntimeError("NSIS did not produce a Windows executable")
        if output.exists():
            raise FileExistsError("installer output appeared during build")
        if fluent:
            from tools.build_fluent_installer import wrap
            report.update(wrap(staged_output, output, kind='install', payload=payload))
        else:
            staged_output.rename(output)
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    report.update(
        {
            "format": 1,
            "installer": output.name,
            "installer_size_bytes": output.stat().st_size,
            "installer_sha256": _digest(output),
            "installer_execution_verified": False,
        }
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--makensis",
        type=Path,
        default=Path(shutil.which("makensis") or "makensis"),
    )
    parser.add_argument(
        "--script",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "packaging" / "windows" / "OfflineWord.nsi",
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    report = build_installer(
        args.payload,
        args.output,
        makensis=args.makensis,
        script=args.script,
        fluent=True,
    )
    report_path = args.report or args.output.with_suffix(".json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    print("Installer assembled. Native Windows install, launch and uninstall remain pending.")


if __name__ == "__main__":
    main()
