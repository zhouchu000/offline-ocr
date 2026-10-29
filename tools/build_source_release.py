"""Export an audited source snapshot without local data or Git history."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import zipfile

from offline_word.releases import load_releases
from offline_word.security import validate_models
from offline_word.version import __version__


ROOT_FILES = {
    '.gitignore', '.gitattributes', 'README.md', 'LICENSE', 'NOTICE',
    'THIRD_PARTY_NOTICES.md', 'CONTRIBUTING.md', 'SECURITY.md', 'CHANGELOG.md',
    'pyproject.toml', 'dev.cmd', 'requirements-win.lock',
    'requirements-quality.lock', 'models.lock.json',
    'runtime-downloads/python-embed.lock.json',
}
PUBLIC_DOCS = {'docs/BUILD.md', 'docs/DEVELOPMENT.md', 'docs/OPEN_SOURCE.md', 'docs/VALIDATION.md'}
PUBLIC_TREES = {'src', 'tests', 'tools', 'packaging', 'licenses', '.github'}
FORBIDDEN_PARTS = {
    '.git', '.venv', '__pycache__', 'node_modules', 'models', 'runtime',
    'build', 'dist', 'validation', 'website', '.pytest_cache', '.ruff_cache',
}
FORBIDDEN_SUFFIXES = {'.pyc', '.pem', '.key', '.p12', '.pfx', '.ocrproj', '.ocrupdate', '.docx'}
SECRET_PATTERNS = (
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----'),
    re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b'),
    re.compile(r'\bAKIA[A-Z0-9]{16}\b'),
    re.compile(r'\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b'),
    re.compile(r'[A-Z]:[\\/](?:Users[\\/](?!Public\b|Default\b)[^\s\\/]+|OCR-0\.3\.0-Dev)', re.I),
    re.compile('/' + r'Users/(?!runner\b)[^\s/]+', re.I),
)
TEXT_SUFFIXES = {'.py', '.json', '.toml', '.md', '.txt', '.yml', '.yaml', '.cs', '.xaml', '.nsi', '.lock', '.cmd', '.svg'}
PUBLIC_AGENTS = '''# 开发约定

先阅读 README.md、docs/DEVELOPMENT.md 和 CONTRIBUTING.md。

- 应用运行保持离线；模型须使用显式本地路径并校验。
- 保留原图与原始识别结果，人工校对优先；候选须确认后采用。
- 默认资源配置面向八代酷睿、无独显环境。
- 修改后运行相关测试；界面变化检查实际 Qt 渲染与键盘操作。
- 版本号与 releases.json 同步，使用 tools.sync_changelog 更新 CHANGELOG.md。
- 不提交真实资料、口令、私钥、个人路径、运行环境或缓存。模型随完整发布附件提供，保留来源、许可和校验清单。
'''


def is_public(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:
        return False
    if not path.parts or any(part in FORBIDDEN_PARTS for part in path.parts):
        return False
    if path.suffix.lower() in FORBIDDEN_SUFFIXES or path.name.startswith('.env'):
        return False
    return name in ROOT_FILES or name in PUBLIC_DOCS or path.parts[0] in PUBLIC_TREES


def audit_file(name: str, data: bytes) -> None:
    if len(data) >= 100 * 1024**2:
        raise ValueError(f'github_file_size_limit: {name}')
    if PurePosixPath(name).suffix.lower() in TEXT_SUFFIXES or name in ROOT_FILES:
        try:
            value = data.decode('utf-8')
        except UnicodeDecodeError:
            if not name.startswith('licenses/'):
                raise ValueError(f'public_source_invalid_text_encoding: {name}') from None
            # Some original upstream licenses use Latin-1. Keep their bytes
            # unchanged while still scanning ASCII secrets and local paths.
            value = data.decode('latin-1')
        if any(pattern.search(value) for pattern in SECRET_PATTERNS):
            # Report the file only, never the matched sensitive value.
            raise ValueError(f'public_source_sensitive_content: {name}')


def collect_files(root: Path) -> dict[str, bytes]:
    root = root.resolve()
    subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--'], cwd=root, check=True)
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode('utf-8').split('\0')
    result = {}
    for name in sorted(filter(is_public, filter(None, names))):
        source = root / name
        if source.is_symlink() or not source.resolve().is_relative_to(root):
            raise ValueError(f'public_source_unsafe_path: {name}')
        if any(parent.is_symlink() for parent in source.parents if parent != root and parent.is_relative_to(root)):
            raise ValueError(f'public_source_unsafe_path: {name}')
        data = source.read_bytes()
        audit_file(name, data)
        result[name] = data
    missing = (ROOT_FILES | PUBLIC_DOCS) - result.keys()
    if missing:
        raise ValueError('public_source_required_files_missing: ' + ', '.join(sorted(missing)))
    if not any(name.startswith('src/offline_word/') for name in result):
        raise ValueError('public_source_application_missing')
    result['AGENTS.md'] = PUBLIC_AGENTS.encode('utf-8')
    return result


def collect_models(root: Path) -> dict[str, tuple[Path, str]]:
    """Include only the complete hash-locked model tree, never arbitrary local files."""
    model_root = root / 'models'
    if model_root.is_junction():
        raise ValueError('public_models_unsafe_path')
    manifest = validate_models(model_root)
    catalog = json.loads((root / 'models.lock.json').read_text(encoding='utf-8'))
    if set(manifest['models']) != set(catalog['models']):
        raise ValueError('public_models_catalog_mismatch')
    if {p.name for p in model_root.iterdir()} != set(manifest['models']) | {'manifest.json'}:
        raise ValueError('public_models_unexpected_file')
    paths = {'models/manifest.json': (model_root / 'manifest.json', '')}
    for model, entry in manifest['models'].items():
        for relative, digest in entry['files'].items():
            path = model_root / model / relative
            if any(p.is_junction() for p in (path, *path.parents) if p.is_relative_to(model_root)):
                raise ValueError('public_models_unsafe_path')
            if path.suffix.lower() in TEXT_SUFFIXES:
                audit_file('models/' + model + '/' + relative, path.read_bytes())
            paths['models/' + model + '/' + relative] = (path, digest)
    return paths


def file_hash(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def build_source_release(root: Path, output: Path, version: str = __version__, *, include_models: bool = True) -> dict:
    root, output = root.resolve(), output.resolve()
    if output.exists():
        raise FileExistsError('public_source_output_exists')
    load_releases(root / 'src/offline_word/releases.json', current=version)
    files = collect_files(root)
    models = collect_models(root) if include_models else {}
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    manifest = {
        'format': 1, 'application_version': version, 'source_commit': commit,
        'includes_git_history': False, 'includes_models': include_models,
        'files': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
    }
    folder = f'OfflineOCR-{version}-source' + ('-with-models' if include_models else '')
    output.parent.mkdir(parents=True, exist_ok=True)
    # Own only a new temporary directory, never an existing checkout or output.
    with tempfile.TemporaryDirectory(prefix='.public-source-', dir=output.parent) as temporary:
        stage = Path(temporary)
        if stage.resolve().parent != output.parent:
            raise ValueError('public_source_stage_outside_destination')
        tree = stage / folder
        for name, data in files.items():
            target = tree / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        # Stream large weights: no extra gigabyte-sized in-memory byte dictionary.
        for name, (source, expected) in models.items():
            target = tree / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            digest = file_hash(target)
            if expected and digest != expected:
                raise ValueError('public_models_changed_during_copy')
            manifest['files'][name] = digest
        if include_models:
            validate_models(tree / 'models')
        (tree / 'SOURCE_MANIFEST.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        archive = stage / (folder + '.zip')
        with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
            for name in sorted([*manifest['files'], 'SOURCE_MANIFEST.json']):
                entry = zipfile.ZipInfo(folder + '/' + name, (1980, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = 0o100644 << 16
                with (tree / name).open('rb') as source, bundle.open(entry, 'w', force_zip64=True) as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
        with zipfile.ZipFile(archive) as bundle:
            if bundle.testzip() is not None:
                raise ValueError('public_source_zip_failed')
        if archive.stat().st_size >= 2 * 1024**3:
            raise ValueError('github_release_asset_size_limit')
        archive_hash = file_hash(archive)
        (stage / 'SHA256SUMS.txt').write_text(f'{archive_hash}  {archive.name}\n', encoding='utf-8')
        report = {
            'application_version': version, 'source_commit': commit,
            'files': len(manifest['files']), 'zip_bytes': archive.stat().st_size,
            'zip_sha256': archive_hash, 'audit_passed': True,
            'includes_git_history': False, 'includes_models': include_models,
            'model_files': len(models),
        }
        (stage / 'source-report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        # Atomic publication; refuse a destination created by another process.
        stage.rename(output)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--code-only', action='store_true', help='Export the repository upload tree; full release includes models by default')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output or root / 'build' / ('public-source-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    print(json.dumps(build_source_release(root, output, include_models=not args.code_only), ensure_ascii=False))


if __name__ == '__main__':
    main()
