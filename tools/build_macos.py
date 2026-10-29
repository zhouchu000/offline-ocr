"""Build a self-contained, ad-hoc-signed macOS arm64 release folder."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile


APP_NAME = "离线图片转Word"
from offline_word.version import __version__ as VERSION
from offline_word.releases import load_releases
RELEASE_NAME = f"OfflineWord-{VERSION}-macOS-arm64"
_SCRIPT_ROOT = Path(__file__).resolve().parents[1]
_IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
_DOCUMENT_IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "superpowers")
_PYINSTALLER_METADATA = (
    "PySide6-Essentials",
    "paddleocr",
    "paddlex",
    "paddlepaddle",
    "python-docx",
    # PaddleX checks distribution metadata for every member of its ``ocr``
    # extra before PP-StructureV3 can initialize. Collecting package code alone
    # does not satisfy that runtime dependency check in a frozen application.
    "beautifulsoup4",
    "einops",
    "ftfy",
    "imagesize",
    "Jinja2",
    "latex2mathml",
    "lxml",
    "opencv-contrib-python",
    "openpyxl",
    "premailer",
    "pyclipper",
    "pypdfium2",
    "python-bidi",
    "regex",
    "safetensors",
    "scikit-learn",
    "scipy",
    "sentencepiece",
    "shapely",
    "tiktoken",
    "tokenizers",
)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_checksums(folder: Path) -> dict[str, str | dict[str, str]]:
    checksums = {
        path.relative_to(folder).as_posix(): (
            {"type": "symlink", "target": os.readlink(path)} if path.is_symlink() else _digest(path)
        )
        for path in sorted(folder.rglob("*"))
        if (path.is_file() or path.is_symlink()) and path.name != "checksums.json"
    }
    (folder / "checksums.json").write_text(
        json.dumps(checksums, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return checksums


def _copy_path(source: Path, destination: Path, *, preserve_links: bool = False) -> None:
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=preserve_links, ignore=_IGNORED)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _fragment_path(project_root: Path) -> Path:
    preferred = project_root / "packaging" / "macos" / "Info.plist.fragment.json"
    return preferred if preferred.is_file() else _SCRIPT_ROOT / "packaging" / "macos" / "Info.plist.fragment.json"


def _apply_info_plist(app: Path, project_root: Path) -> None:
    info_path = app / "Contents" / "Info.plist"
    info_path.parent.mkdir(parents=True, exist_ok=True)
    info = plistlib.loads(info_path.read_bytes()) if info_path.is_file() else {}
    fragment = json.loads(_fragment_path(project_root).read_text(encoding="utf-8"))
    if not isinstance(fragment, dict) or not all(isinstance(key, str) for key in fragment):
        raise ValueError("invalid_info_plist_fragment")
    info.update(fragment)
    info.update(CFBundleShortVersionString=VERSION, CFBundleVersion=VERSION)
    info_path.write_bytes(plistlib.dumps(info, sort_keys=True))


def _write_command(folder: Path, name: str, argument: str) -> None:
    script = folder / name
    script.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"\n'
        f'exec "$ROOT/{APP_NAME}.app/Contents/MacOS/{APP_NAME}" {argument}\n',
        encoding="utf-8",
        newline="\n",
    )
    script.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)


def _copy_release_documents(project_root: Path, stage: Path) -> None:
    for name in ("README.md", "THIRD_PARTY_NOTICES.md"):
        source = project_root / name
        if source.is_file():
            _copy_path(source, stage / name)
    for name in ("docs", "licenses", "examples"):
        source = project_root / name
        if source.is_dir():
            if name == "docs":
                shutil.copytree(source, stage / name, ignore=_DOCUMENT_IGNORED)
            else:
                _copy_path(source, stage / name)


def _codesign(app: Path) -> None:
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)


def _verify_codesign(app: Path) -> None:
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)


def assemble_macos_release(
    app: Path,
    models: Path,
    output: Path,
    *,
    project_root: Path,
    codesign: bool = True,
) -> Path:
    """Stage an existing PyInstaller app with external models and safe launchers."""
    app = app.resolve()
    models = models.resolve()
    output = output.resolve()
    project_root = project_root.resolve()
    executable = app / "Contents" / "MacOS" / APP_NAME
    if not executable.is_file():
        raise ValueError("macos_app_executable_missing")
    if not (models / "manifest.json").is_file():
        raise ValueError("model_manifest_missing")
    from offline_word.security import validate_models

    validate_models(models)
    if output.exists():
        raise FileExistsError("macos_release_destination_already_exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    try:
        staged_app = stage / f"{APP_NAME}.app"
        _copy_path(app, staged_app, preserve_links=True)
        _apply_info_plist(staged_app, project_root)
        _copy_path(models, stage / "models")
        _copy_release_documents(project_root, stage)
        _write_command(stage, "检查环境.command", "--self-check")
        _write_command(stage, "核对文件.command", '"--verify-package" "$ROOT"')
        (stage / "build-info.json").write_text(
            json.dumps(
                {
                    "format": 1,
                    "application_version": VERSION,
                    "target": "macOS arm64",
                    "models_external": True,
                    "codesign": "ad-hoc",
                    "assembled_at": datetime.now(timezone.utc).isoformat(),
                    "macos_execution_verified": False,
                    "ocr_inference_verified": False,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        # Signing changes bundle executable and resource metadata. Sign before
        # checksums so the integrity manifest covers the signed bytes.
        if codesign:
            _codesign(staged_app)
        _write_checksums(stage)
        if codesign:
            _verify_codesign(staged_app)
        from offline_word.package_verify import verify

        if not verify(stage)["pass"]:
            raise RuntimeError("macos_release_integrity_check_failed")
        stage.rename(output)
        return output
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _ensure_arm64_build_host() -> None:
    if platform.system() != "Darwin" or platform.machine().casefold() != "arm64":
        raise OSError("macos_arm64_build_host_required")


def _run_pyinstaller(project_root: Path, temporary_root: Path) -> Path:
    source = project_root / "src"
    if not (source / "offline_word" / "__main__.py").is_file():
        raise ValueError("launcher_source_missing")
    dist = temporary_root / "dist"
    work = temporary_root / "work"
    spec = temporary_root / "spec"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source) + (
        os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else ""
    )
    command = [
        sys.executable,
        "-B",
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--onedir",
        "--target-architecture",
        "arm64",
        "--name",
        APP_NAME,
        "--osx-bundle-identifier",
        "com.offlineword.desktop",
        "--paths",
        str(source),
        "--collect-submodules",
        "offline_word",
        "--collect-data",
        "offline_word",
        "--collect-all",
        "paddle",
        "--collect-all",
        "paddlex",
        "--collect-all",
        "paddleocr",
        "--collect-all",
        "docx",
    ]
    for distribution in _PYINSTALLER_METADATA:
        command.extend(("--copy-metadata", distribution))
    command.extend([
        "--distpath",
        str(dist),
        "--workpath",
        str(work),
        "--specpath",
        str(spec),
        str(source / "offline_word" / "__main__.py"),
    ])
    subprocess.run(command, check=True, cwd=project_root, env=environment)
    app = dist / f"{APP_NAME}.app"
    if not app.is_dir():
        raise RuntimeError("pyinstaller_app_missing")
    return app


def build_macos_release(
    project_root: Path,
    output: Path,
    *,
    run_pyinstaller: bool = True,
    models: Path | None = None,
) -> Path:
    """Build and stage an ad-hoc-signed arm64 macOS release folder."""
    project_root = project_root.resolve()
    model_root = (models or project_root / "models").resolve()
    if run_pyinstaller:
        _ensure_arm64_build_host()
        from offline_word.security import validate_models

        validate_models(model_root)
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".offline-word-pyinstaller-", dir=output.parent) as temporary:
            app = _run_pyinstaller(project_root, Path(temporary))
            return assemble_macos_release(app, model_root, output, project_root=project_root)
    app = project_root / "build" / "macos" / f"{APP_NAME}.app"
    return assemble_macos_release(app, model_root, output, project_root=project_root)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the offline macOS arm64 release folder")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    load_releases(args.project_root / 'src' / 'offline_word' / 'releases.json')
    release = build_macos_release(args.project_root, args.output, models=args.models)
    print(json.dumps({"release": str(release), "target": "macOS arm64", "signed": "ad-hoc"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
