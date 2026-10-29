import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

import pytest

from offline_word.releases import load_releases
from tools.build_source_release import ROOT_FILES, PUBLIC_DOCS, audit_file, build_source_release
from offline_word.security import REQUIRED_MODEL_NAMES, validate_models, OfflineError
from tools.sync_changelog import render_changelog


def test_public_changelog_matches_the_bundled_release_catalog():
    root = Path(__file__).resolve().parents[1]
    assert (root / 'CHANGELOG.md').read_text(encoding='utf-8') == render_changelog(load_releases())


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    for name in ROOT_FILES | PUBLIC_DOCS:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('public fixture\n', encoding='utf-8')
    (root / 'src/offline_word').mkdir(parents=True)
    (root / 'src/offline_word/main.py').write_text('pass\n')
    (root / 'src/offline_word/releases.json').write_text(json.dumps([
        {'version': '0.1.0', 'date': '2026-10-13', 'title': '测试版本', 'changes': ['测试内容']}
    ]), encoding='utf-8')
    model_root = root / 'models'
    model_root.mkdir()
    manifest = {'schema': 1, 'versions': {'paddleocr': '3.7.0', 'paddlex': '3.7.0', 'paddlepaddle': '3.3.0'}, 'models': {}}
    for name in REQUIRED_MODEL_NAMES:
        directory = model_root / name
        directory.mkdir()
        data = ('fixture-' + name).encode()
        (directory / 'inference.pdiparams').write_bytes(data)
        manifest['models'][name] = {'directory': name, 'files': {'inference.pdiparams': hashlib.sha256(data).hexdigest()}}
    (model_root / 'manifest.json').write_text(json.dumps(manifest))
    (root / 'models.lock.json').write_text(json.dumps({'schema': 1, 'models': {name: {} for name in REQUIRED_MODEL_NAMES}}))
    for name in ('validation/private.txt', 'website/private.txt', 'docs/internal.md', 'src/private.key'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('private fixture')
    for command in (['git', 'init', '-q'], ['git', 'config', 'user.name', 'Test'],
                    ['git', 'config', 'user.email', 'test@example.invalid'], ['git', 'add', '.'],
                    ['git', 'commit', '-qm', 'fixture']):
        subprocess.run(command, cwd=root, check=True)
    return root


def test_source_export_excludes_internal_material_and_history_and_verifies_bytes(repository, tmp_path):
    output = tmp_path / 'public'
    report = build_source_release(repository, output, '0.1.0', include_models=False)
    assert report['audit_passed'] and not report['includes_git_history']
    assert not report['includes_models']
    tree = output / 'OfflineOCR-0.1.0-source'
    manifest = json.loads((tree / 'SOURCE_MANIFEST.json').read_text())
    for name, digest in manifest['files'].items():
        assert hashlib.sha256((tree / name).read_bytes()).hexdigest() == digest
    for name in ('.git', 'validation', 'website', 'docs/internal.md', 'src/private.key'):
        assert not (tree / name).exists()
    with zipfile.ZipFile(output / 'OfflineOCR-0.1.0-source.zip') as archive:
        assert len(archive.namelist()) == len(manifest['files']) + 1
    with pytest.raises(FileExistsError):
        build_source_release(repository, output, '0.1.0')


def test_source_export_rejects_uncommitted_changes(repository, tmp_path):
    (repository / 'src/offline_word/main.py').write_text('changed\n')
    with pytest.raises(subprocess.CalledProcessError):
        build_source_release(repository, tmp_path / 'public', '0.1.0')
    assert not (tmp_path / 'public').exists()


def test_full_source_defaults_to_all_models_and_validates_extracted_weights(repository, tmp_path):
    output = tmp_path / 'complete'
    report = build_source_release(repository, output, '0.1.0')
    assert report['includes_models']
    tree = output / 'OfflineOCR-0.1.0-source-with-models'
    manifest = json.loads((tree / 'SOURCE_MANIFEST.json').read_text())
    assert len(validate_models(tree / 'models')['models']) == len(REQUIRED_MODEL_NAMES)
    with zipfile.ZipFile(output / (tree.name + '.zip')) as archive:
        assert len(archive.namelist()) == len(manifest['files']) + 1
        for name, digest in manifest['files'].items():
            assert hashlib.sha256(archive.read(tree.name + '/' + name)).hexdigest() == digest
    assert not (tree / '.git').exists()


def test_full_source_rejects_damaged_model_without_publishing(repository, tmp_path):
    model = next((repository / 'models').glob('*/inference.pdiparams'))
    model.write_bytes(b'damaged')
    # Models are not tracked in a real checkout; keep the fixture source clean.
    subprocess.run(['git', 'add', 'models'], cwd=repository, check=True)
    subprocess.run(['git', 'commit', '-qm', 'damaged fixture'], cwd=repository, check=True)
    with pytest.raises(OfflineError):
        build_source_release(repository, tmp_path / 'complete', '0.1.0')
    assert not (tmp_path / 'complete').exists()


def test_full_source_rejects_unlisted_model_material(repository, tmp_path):
    (repository / 'models/private-notes.txt').write_text('local material')
    with pytest.raises(ValueError, match='public_models_unexpected_file'):
        build_source_release(repository, tmp_path / 'complete', '0.1.0')
    assert not (tmp_path / 'complete').exists()


@pytest.mark.parametrize('value', [
    '-----BEGIN ' + 'PRIVATE KEY-----',
    'ghp_' + 'a' * 36,
    'C:/' + 'Users/private-user/Desktop/input.jpg',
])
def test_source_audit_rejects_sensitive_values_without_echoing_them(value):
    with pytest.raises(ValueError, match='public_source_sensitive_content') as caught:
        audit_file('tools/example.py', value.encode('utf-8'))
    assert value not in str(caught.value)
