"""Run, test and build the source checkout using the shipped Windows runtime."""
from __future__ import annotations

import argparse
from datetime import datetime
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from offline_word.version import __version__


def check_source_origin(root: Path, origin: Path) -> None:
    if origin.resolve().parent != (root / 'src/offline_word').resolve():
        raise RuntimeError('Python is not loading the current source checkout; use dev.cmd with the shipped runtime.')


def build_commands(root: Path, executable: Path, output: Path, version: str) -> list[list[str]]:
    payload = output / f'OfflineWord-{version}-Windows-x64'
    portable = [str(executable), '-B', '-m', 'tools.build_portable',
                '--runtime-zip', str(root / 'runtime-downloads/python-3.12.10-embed-amd64.zip'),
                '--runtime-lock', str(root / 'runtime-downloads/python-embed.lock.json'),
                '--site-packages', str(root / 'runtime/windows-python/Lib/site-packages'),
                '--models', str(root / 'models'), '--source', str(root / 'src'), '--output', str(payload)]
    for name in ('README.md', 'LICENSE', 'NOTICE', 'THIRD_PARTY_NOTICES.md', 'licenses', 'docs', 'requirements-win.lock', 'models.lock.json'):
        portable.extend(['--extra', str(root / name)])
    installer = [str(executable), '-B', '-m', 'tools.build_windows_installer', '--payload', str(payload),
                 '--output', str(output / f'离线图片转Word-{version}-Windows-x64-离线安装程序.exe'),
                 '--makensis', str(root / 'toolchains/nsis/makensis.exe')]
    return [portable, installer]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'check', 'test', 'build'), nargs='?', default='run')
    parser.add_argument('--output', type=Path)
    args, rest = parser.parse_known_args()
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.find_spec('offline_word')
    if spec is None or spec.origin is None:
        raise RuntimeError('Current source package is unavailable; use dev.cmd.')
    check_source_origin(root, Path(spec.origin))
    executable = Path(sys.executable)
    environment = os.environ.copy()
    if args.action == 'build':
        if rest:
            parser.error('build accepts only --output')
        version = __version__
        output = args.output or root / 'build' / datetime.now().strftime('windows-%Y%m%d-%H%M%S')
        output = output.resolve()
        if output.exists():
            raise FileExistsError('Build output already exists; choose a new --output directory.')
        commands = build_commands(root, executable, output, version)
    elif args.action == 'test':
        environment.setdefault('QT_QPA_PLATFORM', 'offscreen')
        commands = [[str(executable), '-B', '-m', 'pytest', *(rest or ['-q'])]]
    else:
        command = [str(executable), '-B', '-m', 'offline_word', '--model-dir', str(root / 'models')]
        if args.action == 'check':
            command.append('--self-check')
        commands = [command + rest]
    for command in commands:
        result = subprocess.run(command, cwd=root, env=environment)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
