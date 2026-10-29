"""Signed offline deltas with bounded parsing and a recoverable file transaction.

The updater runs from its own bundled runtime outside the installation. Signed
manifests bind both complete releases, including their checksum files. A retained
journal contains authenticated manifests and verified copies of every old file
changed/deleted. No arbitrary archive path is passed to extractall or a shell.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import uuid
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .security import OfflineError, assert_local_path
from .update_keys import UPDATE_PUBLIC_KEY_HEX
from .update_lock import installation_lock

MAX_MANIFEST = 16*1024*1024
MAX_FILE = 128*1024*1024
MAX_DELTA = 512*1024*1024
_HASH = re.compile(r'[0-9a-f]{64}')
_VERSION = re.compile(r'[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}')
_DEVICE = re.compile(r'(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', re.I)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def relative(name):
    if not isinstance(name, str) or not 1 <= len(name) <= 400:
        raise OfflineError('invalid_update_path')
    parts = PurePosixPath(name).parts
    if (not parts or '/'.join(parts) != name or PurePosixPath(name).is_absolute()
            or any(c in name for c in '\\:\x00<>"|?*')
            or any(p in {'.','..'} or p.endswith(('.', ' ')) or _DEVICE.fullmatch(p)
                   or any(ord(c) < 32 for c in p) for p in parts)):
        raise OfflineError('invalid_update_path')
    return parts


def _mapping(value):
    if not isinstance(value, dict) or not 1 <= len(value) <= 50000:
        raise OfflineError('invalid_update_manifest')
    seen = set()
    parents = set()
    for name, checksum in value.items():
        parts = relative(name)
        key = name.casefold()
        if key in seen or not isinstance(checksum, str) or not _HASH.fullmatch(checksum):
            raise OfflineError('invalid_update_manifest')
        seen.add(key)
        parents.update('/'.join(parts[:i]).casefold() for i in range(1, len(parts)))
    if seen & parents:
        raise OfflineError('invalid_update_manifest')
    for name in ('checksums.json', 'build-info.json', 'launch.pyw', 'runtime/python.exe'):
        if name not in value:
            raise OfflineError('invalid_update_manifest')


def read_signed_manifest(data, signature, *, public_key=None):
    if len(data) > MAX_MANIFEST or len(signature) != 64:
        raise OfflineError('invalid_update_signature')
    try:
        key = bytes.fromhex(UPDATE_PUBLIC_KEY_HEX) if public_key is None else public_key
        Ed25519PublicKey.from_public_bytes(key).verify(signature, data)
    except Exception:
        raise OfflineError('invalid_update_signature') from None
    try:
        value = json.loads(data)
        if not isinstance(value, dict) or set(value) != {'format','base_version','target_version','base_files','new_files'} or value['format'] != 1:
            raise ValueError
        if any(not isinstance(value[k], str) or not _VERSION.fullmatch(value[k]) for k in ('base_version','target_version')):
            raise ValueError
        if tuple(map(int,value['target_version'].split('.'))) <= tuple(map(int,value['base_version'].split('.'))):
            raise ValueError
        _mapping(value['base_files'])
        _mapping(value['new_files'])
        # Case-only renames and file/directory type changes complicate rollback.
        # These rare changes require the complete installer instead.
        union = dict(value['base_files'])
        union.update(value['new_files'])
        _mapping(union)
        for name in set(value['base_files']) | set(value['new_files']):
            if name.startswith(('runtime/', 'models/')) and value['base_files'].get(name) != value['new_files'].get(name):
                raise OfflineError('update_requires_full_installer')
        return value
    except OfflineError:
        raise
    except Exception:
        raise OfflineError('invalid_update_manifest') from None


@contextmanager
def package(path, *, public_key=None):
    path = assert_local_path(path, 'invalid_update_package')
    try:
        if path.stat().st_size > MAX_DELTA:
            raise OfflineError('update_package_too_large')
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 50002 or len({e.filename for e in entries}) != len(entries):
                raise OfflineError('invalid_update_archive')
            if (archive.getinfo('manifest.json').file_size > MAX_MANIFEST
                    or archive.getinfo('signature.bin').file_size != 64):
                raise OfflineError('invalid_update_manifest')
            data, signature = archive.read('manifest.json'), archive.read('signature.bin')
            value = read_signed_manifest(data, signature, public_key=public_key)
            changed = {k for k,v in value['new_files'].items() if value['base_files'].get(k) != v}
            expected = {'manifest.json','signature.bin'} | {'payload/'+name for name in changed}
            if {e.filename for e in entries} != expected:
                raise OfflineError('invalid_update_archive')
            if sum(e.file_size for e in entries) > MAX_DELTA:
                raise OfflineError('update_package_too_large')
            if any(e.is_dir() or e.flag_bits & 1 or e.file_size > MAX_FILE
                   or stat.S_ISLNK(e.external_attr >> 16)
                   or e.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED} for e in entries):
                raise OfflineError('invalid_update_archive')
            yield archive, value, data, signature
    except (KeyError, ValueError, zipfile.BadZipFile, EOFError):
        raise OfflineError('invalid_update_archive') from None


def _inventory(root):
    actual = set()
    def visit(folder):
        for entry in os.scandir(folder):
            item = Path(entry.path)
            status = entry.stat(follow_symlinks=False)
            if entry.is_symlink() or getattr(status, 'st_file_attributes', 0) & 0x400:
                raise OfflineError('update_link_not_allowed')
            if entry.is_dir(follow_symlinks=False):
                visit(item)
            elif entry.is_file(follow_symlinks=False):
                actual.add(item.relative_to(root).as_posix())
            else:
                raise OfflineError('update_special_file')
    visit(root)
    return actual


def verify_tree(root, expected):
    root = assert_local_path(root, 'invalid_update_root')
    if _inventory(root) != set(expected):
        raise OfflineError('update_base_files_changed')
    for name, checksum in expected.items():
        target = assert_local_path(root.joinpath(*relative(name)), 'invalid_update_path')
        if digest(target) != checksum:
            raise OfflineError('update_base_files_changed')


def _no_running_process(root):
    import psutil
    for process in psutil.process_iter(['pid', 'name']):
        if process.pid == os.getpid():
            continue
        try:
            executable = process.exe()
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            if (process.info.get('name') or '').casefold() in {'python.exe', 'pythonw.exe', 'offlineword.exe'}:
                raise OfflineError('application_process_unverifiable') from None
            continue
        if executable and Path(executable).resolve().is_relative_to(root.resolve()):
            raise OfflineError('application_or_update_running')


def journal_path(root):
    return assert_local_path(root.parent / ('.'+root.name+'.offline-update'), 'invalid_update_journal')


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _copy(source, target):
    target = assert_local_path(target, 'invalid_update_path')
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open('rb') as incoming, target.open('xb') as outgoing:
        shutil.copyfileobj(incoming, outgoing, 1024*1024)
        outgoing.flush()
        os.fsync(outgoing.fileno())


def _state(journal, phase):
    temporary = journal / ('state-'+uuid.uuid4().hex+'.tmp')
    _write(temporary, json.dumps({'phase':phase}).encode())
    os.replace(temporary, journal/'state.json')


def _phase(journal):
    path = assert_local_path(journal/'state.json', 'invalid_update_journal')
    if not path.exists():
        return 'staging'
    if path.stat().st_size > 256:
        raise OfflineError('invalid_update_journal')
    try:
        phase = json.loads(path.read_bytes())['phase']
        if phase not in {'staging', 'prepared', 'complete', 'rolled_back'}:
            raise ValueError
        return phase
    except Exception:
        raise OfflineError('invalid_update_journal') from None


def _safe_cleanup(root, journal):
    expected = journal_path(root)
    if journal != expected or journal.resolve().parent != root.resolve().parent:
        raise OfflineError('invalid_update_journal')
    _inventory(journal)  # reject every reparse point before recursive cleanup
    shutil.rmtree(journal)


def _journal_manifest(journal, public_key):
    data_path = assert_local_path(journal/'manifest.json', 'invalid_update_journal')
    sig_path = assert_local_path(journal/'signature.bin', 'invalid_update_journal')
    if data_path.stat().st_size > MAX_MANIFEST or sig_path.stat().st_size != 64:
        raise OfflineError('invalid_update_journal')
    return read_signed_manifest(data_path.read_bytes(), sig_path.read_bytes(), public_key=public_key)


def _rollback(root, journal, manifest):
    base, new = manifest['base_files'], manifest['new_files']
    changed = sorted(k for k in set(base) | set(new) if base.get(k) != new.get(k))
    # Check ALL backups and existing files before changing any installed byte.
    actual = _inventory(root)
    if actual - (set(base) | set(new)):
        raise OfflineError('update_base_files_changed')
    for name in set(base) | set(new):
        if name not in changed:
            if name not in actual or digest(root/name) != base[name]:
                raise OfflineError('update_base_files_changed')
        elif name in actual and digest(root/name) not in {base.get(name), new.get(name)}:
            raise OfflineError('update_base_files_changed')
    _inventory(journal)
    for name in changed:
        if name in base and digest(journal/'backup'/name) != base[name]:
            raise OfflineError('update_backup_damaged')
    for name in changed:
        destination = assert_local_path(root/name, 'invalid_update_path')
        if name in base:
            temporary = journal/('restore-'+uuid.uuid4().hex+'.tmp')
            _copy(journal/'backup'/name, temporary)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, destination)
        else:
            destination.unlink(missing_ok=True)
    verify_tree(root, base)
    _state(journal, 'rolled_back')


def recover_update(root, *, public_key=None, rollback_complete=False):
    """Caller must hold the installation's exclusive lock."""
    journal = journal_path(root)
    if not journal.exists():
        if rollback_complete:
            raise OfflineError('update_backup_missing')
        return None
    _inventory(journal)
    phase = _phase(journal)
    if phase == 'staging':
        # There was no installed-file mutation before the prepared marker.
        # A partially written manifest cannot authorize a restore operation.
        _safe_cleanup(root, journal)
        return 'staging_cleaned'
    manifest = _journal_manifest(journal, public_key)
    if phase == 'prepared' or (phase == 'complete' and rollback_complete):
        if phase == 'complete':
            verify_tree(root, manifest['new_files'])
            # A rollback can itself be interrupted. Mark it recoverable BEFORE
            # its first file replacement, just like a forward update.
            _state(journal, 'prepared')
        _rollback(root, journal, manifest)
        return 'rolled_back'
    verify_tree(root, manifest['new_files'] if phase == 'complete' else manifest['base_files'])
    return phase


def apply_update(package_path, target, *, public_key=None, progress=lambda stage: None, fault=None):
    root = assert_local_path(target, 'invalid_update_root')
    if not root.is_dir() or root == root.parent:
        raise OfflineError('invalid_update_root')
    # Verify the package before creating any installation-side files.
    with package(package_path, public_key=public_key) as (archive, manifest, data, signature):
        with installation_lock(root):
            _no_running_process(root)
            recovered = recover_update(root, public_key=public_key)
            if recovered == 'complete':
                previous = _journal_manifest(journal_path(root), public_key)
                if previous['new_files'] == manifest['new_files']:
                    # Also covers power loss after the complete marker but
                    # before the update wizard displayed its success page.
                    return {'version':manifest['target_version'], 'changed_files':0,
                            'rollback_available':True, 'recovered':'already_complete'}
            progress('checking')
            verify_tree(root, manifest['base_files'])
            info = json.loads((root/'build-info.json').read_bytes())
            if info.get('application_version') != manifest['base_version']:
                raise OfflineError('update_wrong_version')
            journal = journal_path(root)
            if journal.exists():
                # Retain older rollback archives rather than overwriting them.
                retained = root.parent / (journal.name+'-previous-'+uuid.uuid4().hex)
                assert_local_path(retained, 'invalid_update_journal')
                os.replace(journal, retained)
            journal.mkdir(mode=0o700)
            prepared = False
            try:
                _write(journal/'manifest.json', data)
                _write(journal/'signature.bin', signature)
                _state(journal, 'staging')
                base, new = manifest['base_files'], manifest['new_files']
                changed = sorted(k for k in set(base) | set(new) if base.get(k) != new.get(k))
                required = sum(archive.getinfo('payload/'+k).file_size for k in changed if k in new)
                required += sum((root/k).stat().st_size for k in changed if k in base)
                if shutil.disk_usage(root).free < required*2 + 64*1024*1024:
                    raise OfflineError('update_disk_space')
                progress('preparing')
                for name in changed:
                    if name in base:
                        _copy(root/name, journal/'backup'/name)
                        if digest(journal/'backup'/name) != base[name]:
                            raise OfflineError('update_base_files_changed')
                    if name in new:
                        stage = journal/'stage'/name
                        stage.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open('payload/'+name) as incoming, stage.open('xb') as output:
                            count = 0
                            while chunk := incoming.read(1024*1024):
                                count += len(chunk)
                                if count > MAX_FILE:
                                    raise OfflineError('update_package_too_large')
                                output.write(chunk)
                            output.flush()
                            os.fsync(output.fileno())
                        if digest(stage) != new[name]:
                            raise OfflineError('update_payload_damaged')
                _no_running_process(root)
                _state(journal, 'prepared')
                prepared = True
                progress('applying')
                for index, name in enumerate(changed):
                    destination = assert_local_path(root/name, 'invalid_update_path')
                    if name in new:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(journal/'stage'/name, destination)
                    else:
                        destination.unlink()
                    if fault is not None:
                        fault(index)
                progress('verifying')
                verify_tree(root, new)
                if json.loads((root/'build-info.json').read_bytes()).get('application_version') != manifest['target_version']:
                    raise OfflineError('invalid_update_manifest')
                _state(journal, 'complete')
                return {'version': manifest['target_version'], 'changed_files': len(changed),
                        'rollback_available': True, 'recovered': recovered}
            except Exception:
                if prepared:
                    try:
                        _rollback(root, journal, manifest)
                    except Exception:
                        raise OfflineError('update_rollback_pending') from None
                else:
                    _safe_cleanup(root, journal)
                raise


def rollback_update(target, *, public_key=None):
    root = assert_local_path(target, 'invalid_update_root')
    with installation_lock(root):
        _no_running_process(root)
        if recover_update(root, public_key=public_key, rollback_complete=True) != 'rolled_back':
            raise OfflineError('update_backup_missing')


def ensure_update_finished(root):
    """Called while the application owns its shared installation lock."""
    journal = journal_path(root)
    if journal.exists() and _phase(journal) not in {'complete', 'rolled_back'}:
        raise OfflineError('update_recovery_required')
