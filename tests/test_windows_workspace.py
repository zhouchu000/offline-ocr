from pathlib import Path
import json
import subprocess
import zipfile

import pytest

from tools import assemble_windows_workspace as workspace


def test_dev_runtime_resolves_current_source_and_never_packaged_app(tmp_path):
    source = tmp_path / 'old-runtime'
    source.mkdir()
    (source / 'python312._pth').write_text('../app\n')
    (source / 'python.exe').write_bytes(b'MZ fixture')
    dest = tmp_path / 'project/runtime/windows-python'
    workspace.copy_dev_runtime(source, dest)
    paths = (dest / 'python312._pth').read_text().splitlines()
    assert '../../src' in paths
    assert '../dev-site' in paths
    # Windows embedded Python strips a terminal '..' unless it ends in a slash.
    assert '../../' in paths
    assert '../app' not in paths
    assert (dest / '../../src').resolve() == tmp_path / 'project/src'


def test_wheel_extraction_rejects_escape_and_unpinned_bytes(tmp_path):
    wheel = tmp_path / 'bad.whl'
    with zipfile.ZipFile(wheel, 'w') as z:
        z.writestr('../escaped.py', 'bad')
    with pytest.raises(ValueError, match='unsafe'):
        workspace.extract_safe_zip(wheel, tmp_path / 'site')
    assert not (tmp_path / 'escaped.py').exists()
    lock = tmp_path / 'lock.json'
    lock.write_text(json.dumps({wheel.name: '0' * 64}))
    with pytest.raises(ValueError, match='checksum'):
        workspace.install_dev_wheels(tmp_path, lock, tmp_path / 'dev-site')


def test_clone_keeps_commits_but_excludes_cache_and_machine_remote(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    subprocess.run(['git', 'init', '-q', str(source)], check=True)
    subprocess.run(['git', '-C', str(source), 'config', 'user.name', 'Test'], check=True)
    subprocess.run(['git', '-C', str(source), 'config', 'user.email', 'test@example.invalid'], check=True)
    (source / '.gitignore').write_text('.venv/\n')
    (source / 'main.py').write_text('pass\n')
    subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(source), 'commit', '-qm', 'fixture'], check=True)
    (source / '.venv').mkdir()
    (source / '.venv/secret').write_text('not portable')
    dest = tmp_path / 'clone'
    commit = workspace.clone_clean_source(source, dest)
    assert (dest / '.git').is_dir()
    assert not (dest / '.venv').exists()
    assert subprocess.check_output(['git', '-C', str(dest), 'remote'], text=True) == ''
    assert subprocess.check_output(['git', '-C', str(dest), 'rev-parse', 'HEAD'], text=True).strip() == commit
    (source / 'main.py').write_text('changed\n')
    with pytest.raises(ValueError, match='uncommitted'):
        workspace.clone_clean_source(source, tmp_path / 'dirty-copy')
    assert not (tmp_path / 'dirty-copy').exists()


def test_manifest_detects_changes_while_git_metadata_remains_mutable(tmp_path):
    (tmp_path / 'project/.git').mkdir(parents=True)
    (tmp_path / 'project/.git/index').write_text('mutable')
    (tmp_path / 'project/src').mkdir()
    (tmp_path / 'project/src/main.py').write_text('original')
    workspace.write_manifest(tmp_path)
    (tmp_path / 'project/.git/index').write_text('updated by git status')
    assert workspace.verify_manifest(tmp_path)['pass']
    (tmp_path / 'project/src/main.py').write_text('changed')
    assert not workspace.verify_manifest(tmp_path)['pass']


def test_assembly_refuses_existing_destination_before_modifying_it(tmp_path):
    output = tmp_path / 'keep'
    output.mkdir()
    (output / 'existing.txt').write_text('keep')
    with pytest.raises(FileExistsError):
        workspace.assemble(tmp_path, tmp_path, tmp_path, tmp_path, tmp_path, output)
    assert (output / 'existing.txt').read_text() == 'keep'
