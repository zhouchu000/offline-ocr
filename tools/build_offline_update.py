"""Build a signed app-only delta and a small independent Windows updater."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from offline_word.offline_update import digest, package, read_signed_manifest, verify_tree
from offline_word.update_keys import UPDATE_PUBLIC_KEY_HEX
from tools.update_signing import load_key


def release_files(root):
    value = json.loads((root/'checksums.json').read_text(encoding='utf-8'))
    value['checksums.json'] = digest(root/'checksums.json')
    verify_tree(root, value)
    return value


def create_delta(base, new, destination, key):
    base_version = json.loads((base/'build-info.json').read_text(encoding='utf-8'))['application_version']
    target_version = json.loads((new/'build-info.json').read_text(encoding='utf-8'))['application_version']
    value = {'format':1, 'base_version':base_version, 'target_version':target_version,
             'base_files':release_files(base), 'new_files':release_files(new)}
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    signature = key.sign(data)
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    read_signed_manifest(data, signature, public_key=public)
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr('manifest.json', data)
        archive.writestr('signature.bin', signature)
        for name, checksum in value['new_files'].items():
            if value['base_files'].get(name) != checksum:
                archive.write(new/name, 'payload/'+name)
    with package(destination, public_key=public):
        pass
    return value


def independent_runtime(source_runtime, source_code, target):
    runtime = target/'runtime'
    runtime.mkdir(parents=True)
    for path in source_runtime.iterdir():
        if path.is_file() and path.suffix in {'.exe', '.dll', '.pyd', '.zip', '.txt'}:
            shutil.copy2(path, runtime/path.name)
    (runtime/'python312._pth').write_text('python312.zip\n.\nLib/site-packages\n..\nimport site\n', encoding='utf-8')
    site = runtime/'Lib/site-packages'
    site.mkdir(parents=True)
    for name in ('cryptography', 'psutil'):
        shutil.copytree(source_runtime/'Lib/site-packages'/name, site/name,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for path in (source_runtime/'Lib/site-packages').glob('_cffi_backend*.pyd'):
        shutil.copy2(path, site/path.name)
    for pattern in ('cryptography-*.dist-info', 'psutil-*.dist-info'):
        for path in (source_runtime/'Lib/site-packages').glob(pattern):
            shutil.copytree(path, site/path.name)
    code = target/'offline_word'
    code.mkdir()
    for name in ('__init__','domain','version','security','update_keys','update_lock','offline_update'):
        shutil.copy2(source_code/f'{name}.py', code/f'{name}.py')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('base','new','output','private','runtime','makensis'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.output.exists():
        raise FileExistsError('update_output_exists')
    key = load_key(args.private)
    if key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex() != UPDATE_PUBLIC_KEY_HEX:
        raise ValueError('release_key_mismatch')
    args.output.mkdir(parents=True)
    support = args.output/'support'
    independent_runtime(args.runtime, root/'src/offline_word', support)
    shutil.copy2(root/'tools/update_cli.py', support/'update_cli.py')
    manifest = create_delta(args.base, args.new, support/'update.ocrupdate', key)
    # Verify using only the independent runtime before making the executable.
    subprocess.run([str((support/'runtime/python.exe').resolve()), '-I', '-B', '-c',
        'from offline_word.offline_update import package; from pathlib import Path; '
        'p=package(Path("update.ocrupdate")); p.__enter__(); p.__exit__(None,None,None); print("independent_verifier_ok")'],
        cwd=support, check=True)
    output = args.output/f"离线OCR-{manifest['base_version']}-至-{manifest['target_version']}-离线更新.exe"
    backend = args.output/'backend-private'/'update.exe'
    backend.parent.mkdir()
    from tools.build_windows_installer import _safe_define_path
    command = [str(args.makensis.resolve()), '-INPUTCHARSET', 'UTF8', '-V2',
        '-DSUPPORT_FILES='+_safe_define_path(support/'*.*'),
        '-DOUTPUT_FILE='+_safe_define_path(backend), '-DFLUENT_BACKEND',
        '-DAPP_ICON='+_safe_define_path(root/'src/offline_word/assets/app.ico'),
        '-DBASE_VERSION='+manifest['base_version'], '-DTARGET_VERSION='+manifest['target_version'],
        str(root/'packaging/windows/OfflineUpdate.nsi')]
    subprocess.run(command, check=True)
    from tools.build_fluent_installer import wrap
    shell_report = wrap(backend, output, kind='update', version=manifest['target_version'],
                        base_version=manifest['base_version'])
    report = {'base_version':manifest['base_version'], 'target_version':manifest['target_version'],
              'changed_files':sum(manifest['base_files'].get(k)!=v for k,v in manifest['new_files'].items()),
              'exe_bytes':output.stat().st_size, 'exe_sha256':digest(output),
              'delta_bytes':(support/'update.ocrupdate').stat().st_size,
              'independent_verifier':True, 'installer_execution_verified':False,
              'fluent_frontend':shell_report}
    (args.output/'update-report.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
