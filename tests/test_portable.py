import hashlib
import json
from pathlib import Path
import zipfile

import pytest
from offline_word.version import __version__


def pe_fixture(machine=0x8664):
    data = bytearray(128)
    data[:2] = b'MZ'
    data[0x3C:0x40] = (64).to_bytes(4, 'little')
    data[64:68] = b'PE\0\0'
    data[68:70] = machine.to_bytes(2, 'little')
    return bytes(data)


def minimal_inputs(tmp_path):
    runtime = tmp_path / 'runtime.zip'
    with zipfile.ZipFile(runtime, 'w') as z:
        z.writestr('python.exe', pe_fixture())
        z.writestr('pythonw.exe', pe_fixture())
        z.writestr('python312.zip', b'fixture')
        z.writestr('python312.dll', pe_fixture())
    site = tmp_path / 'site'
    site.mkdir()
    (site / 'example.py').write_text('value = 1\n')
    info = site / 'example-1.0.dist-info'
    info.mkdir()
    (info / 'WHEEL').write_text('Tag: py3-none-any\n')
    source = tmp_path / 'src' / 'offline_word'
    source.mkdir(parents=True)
    (source / '__main__.py').write_text('def main(): pass\n')
    models = tmp_path / 'models'
    models.mkdir()
    (models / 'manifest.json').write_text('{"schema": 1}')
    return runtime, site, models, source.parent


def test_portable_contains_isolated_runtime_and_no_false_acceptance(tmp_path):
    from tools.build_portable import assemble
    runtime, site, models, source = minimal_inputs(tmp_path)
    out = tmp_path / 'portable'
    assemble(runtime, site, models, source, out)
    pth = (out / 'runtime' / 'python312._pth').read_text()
    assert '../app' in pth and 'Lib/site-packages' in pth
    assert (out / 'app' / 'offline_word' / '__main__.py').is_file()
    assert (out / 'models' / 'manifest.json').is_file()
    launcher = (out / 'launch.pyw').read_text(encoding='utf-8')
    assert '核对安装文件' in launcher
    assert '是否已解压' not in launcher
    report = json.loads((out / 'build-info.json').read_text())
    assert report['application_version'] == __version__
    assert report['windows_execution_verified'] is False
    assert report['accuracy_verified'] is False
    checks = json.loads((out / 'checksums.json').read_text(encoding='utf-8'))
    assert checks['runtime/Lib/site-packages/example.py'] == hashlib.sha256((site / 'example.py').read_bytes()).hexdigest()


def test_portable_rejects_macos_wheels(tmp_path):
    from tools.build_portable import assemble
    runtime, site, models, source = minimal_inputs(tmp_path)
    (site / 'example-1.0.dist-info' / 'WHEEL').write_text('Tag: cp312-cp312-macosx_11_0_arm64\n')
    with pytest.raises(ValueError, match='Windows'):
        assemble(runtime, site, models, source, tmp_path / 'out')


@pytest.mark.parametrize('tag', ['cp311-cp311-win_amd64', 'cp313-abi3-win_amd64'])
def test_portable_rejects_wrong_python_abi(tmp_path, tag):
    from tools.build_portable import assemble
    runtime, site, models, source = minimal_inputs(tmp_path)
    (site / 'example-1.0.dist-info' / 'WHEEL').write_text(f'Tag: {tag}\n')
    with pytest.raises(ValueError, match='incompatible'):
        assemble(runtime, site, models, source, tmp_path / 'out')


def test_runtime_zip_escape_is_rejected(tmp_path):
    from tools.build_portable import assemble
    runtime, site, models, source = minimal_inputs(tmp_path)
    with zipfile.ZipFile(runtime, 'a') as z:
        z.writestr('../escape', 'bad')
    with pytest.raises(ValueError):
        assemble(runtime, site, models, source, tmp_path / 'out')
    assert not (tmp_path / 'escape').exists()


def test_rebuild_will_not_overwrite_existing_folder(tmp_path):
    from tools.build_portable import assemble
    runtime, site, models, source = minimal_inputs(tmp_path)
    out = tmp_path / 'out'
    out.mkdir()
    (out / 'keep').write_text('keep')
    with pytest.raises(FileExistsError):
        assemble(runtime, site, models, source, out)
    assert (out / 'keep').read_text() == 'keep'


def test_crt_is_available_to_worker_before_qt_is_imported(tmp_path):
    from tools.build_portable import assemble
    runtime, site, models, source = minimal_inputs(tmp_path)
    qt = site / 'PySide6'
    qt.mkdir()
    (qt / 'msvcp140.dll').write_bytes(pe_fixture())
    out = tmp_path / 'portable'
    assemble(runtime, site, models, source, out)
    assert (out / 'runtime' / 'msvcp140.dll').read_bytes() == pe_fixture()


def test_portable_rejects_non_amd64_runtime_binary(tmp_path):
    from tools.build_portable import assemble
    runtime, site, models, source = minimal_inputs(tmp_path)
    (site / 'wrong-arch.pyd').write_bytes(pe_fixture(0xAA64))
    with pytest.raises(ValueError, match='non-amd64'):
        assemble(runtime, site, models, source, tmp_path / 'out')


def test_portable_drops_unused_multiarch_setuptools_launchers(tmp_path):
    from tools.build_portable import assemble
    runtime, site, models, source = minimal_inputs(tmp_path)
    setuptools = site / 'setuptools'
    setuptools.mkdir()
    (setuptools / 'cli.exe').write_bytes(pe_fixture(0x014C))
    (setuptools / 'cli-arm64.exe').write_bytes(pe_fixture(0xAA64))
    out = tmp_path / 'portable'
    assemble(runtime, site, models, source, out)
    assert not (out / 'runtime' / 'Lib' / 'site-packages' / 'setuptools' / 'cli.exe').exists()
    assert not (out / 'runtime' / 'Lib' / 'site-packages' / 'setuptools' / 'cli-arm64.exe').exists()
