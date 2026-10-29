"""Create a minimal, relocatable Git workspace with offline Windows tooling."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tempfile
import zipfile

MANIFEST = 'TRANSFER-SHA256.json'


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def extract_safe_zip(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        for entry in bundle.infolist():
            path = PurePosixPath(entry.filename)
            if (path.is_absolute() or '..' in path.parts or '\\' in entry.filename
                    or ':' in entry.filename or stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError('unsafe archive member')
        destination.mkdir(parents=True, exist_ok=True)
        bundle.extractall(destination)


def copy_dev_runtime(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.DS_Store', '._*'))
    (destination / 'python312._pth').write_text(
        'python312.zip\n.\nLib/site-packages\n../../src\n../dev-site\n../../\nimport site\n', encoding='ascii')


def install_dev_wheels(wheels: Path, lock: Path, destination: Path) -> None:
    pins = json.loads(lock.read_text(encoding='utf-8'))
    if not isinstance(pins, dict) or not pins:
        raise ValueError('empty dev dependency lock')
    for name, expected in pins.items():
        if Path(name).name != name or digest(wheels / name) != expected:
            raise ValueError('dev wheel checksum mismatch')
    for name in sorted(pins):
        if not name.endswith('-none-any.whl'):
            raise ValueError('dev wheel must be platform independent')
        extract_safe_zip(wheels / name, destination)


def clone_clean_source(source: Path, destination: Path) -> str:
    if subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain'], text=True):
        raise ValueError('source has uncommitted files')
    commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    subprocess.run(['git', 'clone', '--quiet', '--no-local', str(source), str(destination)], check=True)
    subprocess.run(['git', '-C', str(destination), 'remote', 'remove', 'origin'], check=True)
    for name, value in (('core.filemode', 'false'), ('core.autocrlf', 'false')):
        subprocess.run(['git', '-C', str(destination), 'config', name, value], check=True)
    return commit


def _manifest_files(folder: Path):
    for path in sorted(folder.rglob('*')):
        relative = path.relative_to(folder)
        if any(part in {'.git', '__pycache__', '.pytest_cache'} or part.startswith('._') for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError('workspace symlink is not portable')
        if path.is_file() and relative.as_posix() != MANIFEST and path.name != '.DS_Store':
            yield relative.as_posix(), path


def write_manifest(folder: Path) -> dict[str, str]:
    files = {name: digest(path) for name, path in _manifest_files(folder)}
    (folder / MANIFEST).write_text(json.dumps(files, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return files


def verify_manifest(folder: Path) -> dict:
    expected = json.loads((folder / MANIFEST).read_text(encoding='utf-8'))
    actual = {name: digest(path) for name, path in _manifest_files(folder)}
    missing = set(expected) - set(actual)
    extra = set(actual) - set(expected)
    changed = [name for name in expected.keys() & actual.keys() if expected[name] != actual[name]]
    return {'pass': not (missing or extra or changed), 'checked': len(expected),
            'missing': len(missing), 'extra': len(extra), 'changed': len(changed)}


def assemble(project: Path, payload: Path, wheels: Path, dev_lock: Path, nsis_zip: Path, output: Path) -> dict:
    if output.exists():
        raise FileExistsError('handoff destination already exists')
    from tools.build_windows_installer import validate_payload
    payload_report = validate_payload(payload)
    nsis_lock = json.loads((project / 'packaging/windows/nsis.lock.json').read_text(encoding='utf-8'))
    if digest(nsis_zip) != nsis_lock['windows_zip']['sha256']:
        raise ValueError('NSIS archive checksum mismatch')
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.handoff-', dir=output.parent))
    try:
        target = stage / 'project'
        commit = clone_clean_source(project, target)
        copy_dev_runtime(payload / 'runtime', target / 'runtime/windows-python')
        install_dev_wheels(wheels, dev_lock, target / 'runtime/dev-site')
        shutil.copytree(payload / 'models', target / 'models')
        # A small simulated page makes the first Windows inference check usable
        # without transferring any of the user's real documents or font files.
        from tools.generate_samples import _draw_page, choose_font, make_text
        sample_dir = target / 'validation/public-smoke'
        sample_dir.mkdir(parents=True)
        sample_text = make_text(1)
        _draw_page(sample_text, choose_font(), 1).save(sample_dir / 'sample.png')
        (sample_dir / 'reference.txt').write_text(sample_text + '\n', encoding='utf-8')
        runtime_lock = json.loads((project / 'runtime-downloads/python-embed.lock.json').read_text(encoding='utf-8'))
        runtime_archive = project / 'runtime-downloads/python-3.12.10-embed-amd64.zip'
        if digest(runtime_archive) != runtime_lock['sha256']:
            raise ValueError('Python runtime checksum mismatch')
        shutil.copy2(runtime_archive, target / 'runtime-downloads' / runtime_archive.name)
        nsis_stage = stage / '.nsis-extract'
        extract_safe_zip(nsis_zip, nsis_stage)
        nsis_source = nsis_stage / f'nsis-{nsis_lock["version"]}'
        nsis_target = target / 'toolchains/nsis'
        nsis_target.mkdir(parents=True)
        for name in ('Bin', 'Contrib', 'Include', 'Plugins', 'Stubs', 'makensis.exe', 'nsisconf.nsh', 'COPYING'):
            source = nsis_source / name
            if source.is_dir():
                shutil.copytree(source, nsis_target / name)
            else:
                shutil.copy2(source, nsis_target / name)
        shutil.rmtree(nsis_stage)
        shutil.copy2(target / 'docs/Windows开发交接.md', stage / '开始使用.md')
        report = {'schema': 1, 'source_commit': commit, 'application_version': payload_report['application_version'],
                  'source_includes_git_history': True, 'machine_remote_removed': True,
                  'runtime_import_path': 'project/src', 'windows_execution_verified': False,
                  'public_smoke_sample': 'project/validation/public-smoke/sample.png',
                  'payload': payload_report, 'dev_wheels': json.loads(dev_lock.read_text(encoding='utf-8')),
                  'excluded': ['macOS .venv', 'old releases', 'build caches', 'private images and OCR text', 'Git machine configuration'],
                  'git_verification': 'Run git fsck in project; mutable .git is excluded from file checksums.'}
        (stage / 'TRANSFER.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        write_manifest(stage)
        if output.exists():
            raise FileExistsError('handoff destination appeared during assembly')
        stage.rename(output)
        return report
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify', type=Path)
    parser.add_argument('--project', type=Path, default=Path.cwd())
    parser.add_argument('--payload', type=Path)
    parser.add_argument('--wheels', type=Path)
    parser.add_argument('--dev-lock', type=Path, default=Path('packaging/windows/dev-wheels.lock.json'))
    parser.add_argument('--nsis-zip', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.verify:
        result = verify_manifest(args.verify)
    else:
        if any(value is None for value in (args.payload, args.wheels, args.nsis_zip, args.output)):
            parser.error('--payload, --wheels, --nsis-zip and --output are required')
        result = assemble(args.project.resolve(), args.payload, args.wheels, args.dev_lock, args.nsis_zip, args.output)
    print(json.dumps(result, ensure_ascii=False))
    if result.get('pass') is False:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
