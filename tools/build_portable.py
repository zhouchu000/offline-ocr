"""Assemble already pinned Windows binaries; assembly is not execution validation."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import tempfile
import zipfile

from packaging.tags import compatible_tags, cpython_tags, parse_tag


from offline_word.version import __version__ as VERSION
from offline_word.releases import load_releases
_AMD64_MACHINE = 0x8664
_SETUPTOOLS_LAUNCHERS = {
    'cli.exe', 'cli-32.exe', 'cli-arm64.exe',
    'gui.exe', 'gui-32.exe', 'gui-arm64.exe',
}


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def check_windows_wheels(site: Path) -> None:
    supported = set(cpython_tags((3, 12), ['cp312'], ['win_amd64']))
    supported.update(compatible_tags((3, 12), 'cp312', ['win_amd64']))
    wheels = list(site.glob('*.dist-info/WHEEL'))
    if not wheels:
        raise ValueError('Windows wheel metadata missing')
    for wheel in wheels:
        tags = [line.split(':', 1)[1].strip() for line in wheel.read_text().splitlines() if line.startswith('Tag:')]
        if not tags or not any(parse_tag(t) & supported for t in tags):
            raise ValueError('non-Windows or incompatible wheel included')
    if any(p.is_symlink() for p in site.rglob('*')):
        raise ValueError('site-packages symlinks are not portable')


def _pe_machine(path: Path) -> int:
    with path.open('rb') as stream:
        dos_header = stream.read(64)
        if len(dos_header) < 64 or dos_header[:2] != b'MZ':
            raise ValueError('invalid Windows binary included')
        offset = int.from_bytes(dos_header[0x3C:0x40], 'little')
        stream.seek(offset)
        pe_header = stream.read(6)
    if len(pe_header) != 6 or pe_header[:4] != b'PE\0\0':
        raise ValueError('invalid Windows binary included')
    return int.from_bytes(pe_header[4:6], 'little')


def check_windows_binaries(runtime: Path) -> None:
    for path in runtime.rglob('*'):
        if path.is_file() and path.suffix.casefold() in {'.exe', '.dll', '.pyd'}:
            if _pe_machine(path) != _AMD64_MACHINE:
                raise ValueError('non-amd64 Windows binary included')


def write_checksums(folder: Path) -> dict:
    checks = {p.relative_to(folder).as_posix(): digest(p) for p in sorted(folder.rglob('*'))
              if p.is_file() and p.name != 'checksums.json'}
    (folder / 'checksums.json').write_text(json.dumps(checks, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return checks


def assemble(runtime_zip: Path, site: Path, models: Path, source: Path, output: Path, extras: list[Path] | None = None) -> None:
    if output.exists():
        raise FileExistsError('portable destination already exists')
    check_windows_wheels(site)
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.portable-', dir=output.parent))
    try:
        runtime = stage / 'runtime'
        runtime.mkdir()
        with zipfile.ZipFile(runtime_zip) as z:
            for item in z.infolist():
                path = PurePosixPath(item.filename)
                if path.is_absolute() or '..' in path.parts or '\\' in item.filename or ':' in item.filename:
                    raise ValueError('unsafe runtime archive path')
                if stat.S_ISLNK(item.external_attr >> 16):
                    raise ValueError('runtime archive links forbidden')
                z.extract(item, runtime)
        for executable in ('python.exe', 'pythonw.exe', 'python312.dll'):
            if not (runtime / executable).read_bytes().startswith(b'MZ'):
                raise ValueError('Windows runtime executable missing or invalid')
        runtime_site = runtime / 'Lib' / 'site-packages'
        shutil.copytree(site, runtime_site,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'bin', 'Scripts'))
        for name in _SETUPTOOLS_LAUNCHERS:
            (runtime_site / 'setuptools' / name).unlink(missing_ok=True)
        # Qt's wheel already redistributes these CRT libraries. Place them in
        # the executable directory as well: a spawned OCR process imports
        # OpenCV/Paddle before Qt has had an opportunity to load the CRT.
        for pattern in ('msvcp140*.dll', 'concrt140.dll', 'vcruntime140*.dll'):
            for dll in (site / 'PySide6').glob(pattern):
                if not (runtime / dll.name).exists():
                    shutil.copy2(dll, runtime / dll.name)
        check_windows_binaries(runtime)
        shutil.copytree(source, stage / 'app', ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.egg-info'))
        shutil.copytree(models, stage / 'models')
        (runtime / 'python312._pth').write_text('python312.zip\n.\nLib/site-packages\n../app\nimport site\n', encoding='utf-8')
        (stage / 'launch.pyw').write_text(
            'import multiprocessing\n'
            'if __name__ == "__main__":\n'
            '    multiprocessing.freeze_support()\n'
            '    try:\n'
            '        from offline_word.__main__ import main\n'
            '        main()\n'
            '    except Exception:\n'
            '        import ctypes\n'
            '        ctypes.windll.user32.MessageBoxW(None, "程序无法启动（startup_failed）。请运行环境检查并核对安装文件。", "离线图片转 Word", 16)\n',
            encoding='utf-8')
        (stage / '启动程序.cmd').write_text(
            '@echo off\nsetlocal\ncd /d "%~dp0"\nstart "" "%~dp0runtime\\pythonw.exe" -B "%~dp0launch.pyw"\n',
            encoding='ascii', newline='\r\n')
        (stage / '检查环境.cmd').write_text(
            '@echo off\nsetlocal\ncd /d "%~dp0"\n"%~dp0runtime\\python.exe" -B -m offline_word --self-check\npause\n',
            encoding='ascii', newline='\r\n')
        shutil.copy2(Path(__file__).with_name('verify_package.py'), stage / 'verify_package.py')
        (stage / '核对文件.cmd').write_text(
            '@echo off\nsetlocal\ncd /d "%~dp0"\n"%~dp0runtime\\python.exe" -B "%~dp0verify_package.py"\npause\n',
            encoding='ascii', newline='\r\n')
        info = {'format': 1, 'application_version': VERSION, 'target': 'Windows 10/11 x64',
                'python_version': '3.12.10', 'assembled_on': platform.system(),
                'assembled_at': datetime.now(timezone.utc).isoformat(),
                'windows_execution_verified': False, 'accuracy_verified': False,
                'runtime_archive_sha256': digest(runtime_zip)}
        (stage / 'build-info.json').write_text(json.dumps(info, indent=2) + '\n', encoding='utf-8')
        for item in extras or []:
            if item.is_dir():
                shutil.copytree(item, stage / item.name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            else:
                shutil.copy2(item, stage / item.name)
        write_checksums(stage)
        if output.exists():
            raise FileExistsError('portable destination appeared during build')
        stage.rename(output)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument('--runtime-zip', type=Path, required=True)
    p.add_argument('--runtime-lock', type=Path, required=True)
    p.add_argument('--site-packages', type=Path, required=True)
    p.add_argument('--models', type=Path, required=True)
    p.add_argument('--source', type=Path, default=Path('src'))
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--extra', type=Path, action='append', default=[])
    args = p.parse_args()
    lock = json.loads(args.runtime_lock.read_text())
    if digest(args.runtime_zip) != lock['sha256']:
        raise ValueError('runtime checksum mismatch')
    from offline_word.security import validate_models
    validate_models(args.models)
    load_releases(args.source / 'offline_word' / 'releases.json')
    assemble(args.runtime_zip, args.site_packages, args.models, args.source, args.output, args.extra)
    print('Portable folder assembled. Windows execution and accuracy require separate validation.')


if __name__ == '__main__':
    main()
