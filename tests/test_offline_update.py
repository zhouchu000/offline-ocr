import json
import shutil
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import pytest

from offline_word.offline_update import (apply_update, digest, ensure_update_finished, journal_path,
    read_signed_manifest, recover_update, rollback_update, verify_tree)
from offline_word.security import OfflineError
from offline_word.update_lock import installation_lock
from tools.build_offline_update import create_delta


def release(root, version):
    root.mkdir()
    for name, value in {'launch.pyw':'print("public fixture")', 'runtime/python.exe':'unchanged runtime fixture',
                        'models/model.bin':'unchanged model fixture', 'app/version.py':version,
                        'build-info.json':json.dumps({'application_version':version})}.items():
        target = root/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value)
    if version == '0.4.0':
        (root/'old.txt').write_text('old public file')
    else:
        (root/'new.txt').write_text('new public file')
    checks = {p.relative_to(root).as_posix():digest(p) for p in root.rglob('*') if p.is_file()}
    (root/'checksums.json').write_text(json.dumps(checks))
    return root


@pytest.fixture
def delta(tmp_path):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    base = release(tmp_path/'base', '0.4.0')
    new = release(tmp_path/'new', '0.5.0')
    path = tmp_path/'test.ocrupdate'
    manifest = create_delta(base, new, path, key)
    target = tmp_path/'installed'
    shutil.copytree(base, target)
    return target, path, manifest, public, key


def test_signed_delta_updates_and_manual_rollback_restores_every_byte(delta):
    root, path, manifest, public, _ = delta
    result = apply_update(path, root, public_key=public)
    assert result['version'] == '0.5.0' and result['rollback_available']
    verify_tree(root, manifest['new_files'])
    ensure_update_finished(root)
    rollback_update(root, public_key=public)
    verify_tree(root, manifest['base_files'])
    assert not (root/'new.txt').exists()


def test_repeating_completed_update_is_idempotent_and_keeps_original_backup(delta):
    root, path, manifest, public, _ = delta
    apply_update(path, root, public_key=public)
    original = (journal_path(root)/'backup/app/version.py').read_bytes()
    result = apply_update(path, root, public_key=public)
    assert result['recovered'] == 'already_complete' and result['changed_files'] == 0
    assert (journal_path(root)/'backup/app/version.py').read_bytes() == original
    verify_tree(root, manifest['new_files'])


def test_interrupted_manual_rollback_can_be_resumed(delta, monkeypatch):
    import offline_word.offline_update as updater
    root, path, manifest, public, _ = delta
    apply_update(path, root, public_key=public)
    replace = updater.os.replace
    class SimulatedTermination(BaseException): pass
    def terminate(source, destination):
        replace(source, destination)
        if str(source).split('restore-')[-1] != str(source):
            raise SimulatedTermination
    with monkeypatch.context() as patch:
        patch.setattr(updater.os, 'replace', terminate)
        with pytest.raises(SimulatedTermination):
            rollback_update(root, public_key=public)
    with pytest.raises(OfflineError, match='update_recovery_required'):
        ensure_update_finished(root)
    rollback_update(root, public_key=public)
    verify_tree(root, manifest['base_files'])


@pytest.mark.parametrize('step', range(5))
def test_each_mutation_failure_restores_complete_old_release(delta, step):
    root, path, manifest, public, _ = delta
    def fail(index):
        if index == step:
            raise OSError('injected disk error')
    with pytest.raises(OSError):
        apply_update(path, root, public_key=public, fault=fail)
    verify_tree(root, manifest['base_files'])


def test_process_interruption_can_resume_safely_without_trusting_installed_code(delta):
    root, path, manifest, public, _ = delta
    class SimulatedTermination(BaseException): pass
    def terminate(index):
        if index == 2:
            raise SimulatedTermination
    with pytest.raises(SimulatedTermination):
        apply_update(path, root, public_key=public, fault=terminate)
    with pytest.raises(OfflineError, match='update_recovery_required'):
        ensure_update_finished(root)
    # A fresh invocation recovers the interrupted transaction and applies anew.
    result = apply_update(path, root, public_key=public)
    assert result['recovered'] == 'rolled_back'
    verify_tree(root, manifest['new_files'])


def test_bad_signature_has_no_installation_side_effects(delta):
    root, path, manifest, _, _ = delta
    wrong = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    with pytest.raises(OfflineError, match='invalid_update_signature'):
        apply_update(path, root, public_key=wrong)
    verify_tree(root, manifest['base_files'])
    assert not journal_path(root).exists()
    assert not (root.parent/('.'+root.name+'.update.lock')).exists()


def test_corrupt_delta_is_rejected_before_mutation(delta, tmp_path):
    root, path, manifest, public, _ = delta
    corrupt = tmp_path/'corrupt.ocrupdate'
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(corrupt,'w') as output:
        for item in source.infolist():
            content = source.read(item)
            if item.filename == 'payload/app/version.py':
                content = b'malicious replacement'
            output.writestr(item, content)
    with pytest.raises(OfflineError, match='update_payload_damaged'):
        apply_update(corrupt, root, public_key=public)
    verify_tree(root, manifest['base_files'])
    assert not journal_path(root).exists()


@pytest.mark.parametrize('name', ['../outside','/absolute','runtime/x:ads','CON.txt','app/x./a',
                                  'app/../x','app\\x','app//x', 'app/aux.log', 'app/\x01'])
def test_even_signed_unsafe_paths_are_rejected(delta, name):
    _, _, manifest, public, key = delta
    manifest['new_files'][name] = 'a'*64
    data = json.dumps(manifest).encode()
    with pytest.raises(OfflineError):
        read_signed_manifest(data, key.sign(data), public_key=public)


def test_modified_base_and_runtime_changes_are_refused(delta):
    root, path, manifest, public, key = delta
    (root/'launch.pyw').write_text('local modification')
    with pytest.raises(OfflineError, match='update_base_files_changed'):
        apply_update(path, root, public_key=public)
    assert (root/'launch.pyw').read_text() == 'local modification'
    manifest['new_files']['runtime/python.exe'] = 'b'*64
    data = json.dumps(manifest).encode()
    with pytest.raises(OfflineError, match='update_requires_full_installer'):
        read_signed_manifest(data, key.sign(data), public_key=public)


def test_corrupt_backup_prevents_any_restore(delta):
    root, path, manifest, public, _ = delta
    apply_update(path, root, public_key=public)
    (journal_path(root)/'backup/app/version.py').write_text('corruption')
    with pytest.raises(OfflineError, match='update_backup_damaged'):
        rollback_update(root, public_key=public)
    verify_tree(root, manifest['new_files'])


def test_shared_application_lock_blocks_update_and_exclusive_blocks_startup(delta):
    root, path, manifest, public, _ = delta
    with installation_lock(root, shared=True):
        with installation_lock(root, shared=True):
            with pytest.raises(OfflineError, match='application_or_update_running'):
                apply_update(path, root, public_key=public)
    with installation_lock(root):
        with pytest.raises(OfflineError, match='application_or_update_running'):
            with installation_lock(root, shared=True):
                pass
    verify_tree(root, manifest['base_files'])


def test_unfinished_staging_is_cleaned_without_modifying_installation(delta):
    root, _, manifest, public, _ = delta
    journal = journal_path(root)
    journal.mkdir()
    (journal/'manifest.json').write_bytes(b'partial')
    with installation_lock(root):
        assert recover_update(root, public_key=public) == 'staging_cleaned'
    assert not journal.exists()
    verify_tree(root, manifest['base_files'])
