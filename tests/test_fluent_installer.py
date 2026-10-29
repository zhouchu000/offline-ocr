"""Compile and execute the native boundary; never install into the user's app."""
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess

import pytest

from tools.build_fluent_installer import application_palettes, wrap


def test_installer_palette_is_taken_from_the_existing_app():
    palettes = application_palettes(Path.cwd())
    assert palettes['light']['primary'] == '#0067C0'
    assert palettes['dark']['text'] == '#F2F4F8'


@pytest.fixture(scope='module')
def compiled_shell(tmp_path_factory):
    if os.name != 'nt':
        pytest.skip('Native Windows compiler and WPF runtime required')
    root = tmp_path_factory.mktemp('native_setup')
    backend = root/'backend.exe'
    backend.write_bytes(b'MZ'+b'public acceptance bytes'*1024)
    output = root/'setup.exe'
    info = wrap(backend, output, kind='install', qa=True)
    return output, info


def run_protocol(executable, output):
    result = subprocess.run([str(executable), '--qa-protocol', str(output)], timeout=20)
    return result.returncode, json.loads((output/'protocol.json').read_bytes())


def test_backend_is_prepared_and_verified_without_running_it(compiled_shell, tmp_path):
    executable, _ = compiled_shell
    code, report = run_protocol(executable, tmp_path/'valid')
    assert code == 0 and report == {'prepared':True, 'executed':False}


@pytest.mark.parametrize('damage', ['payload', 'footer', 'length', 'truncation'])
def test_damaged_embedded_backend_is_rejected_before_execution(compiled_shell, tmp_path, damage):
    executable, _ = compiled_shell
    copy = tmp_path/'damaged.exe'
    shutil.copy2(executable, copy)
    with copy.open('r+b') as stream:
        stream.seek(-24, 2)
        _, offset, length = struct.unpack('<8sqq', stream.read(24))
        if damage == 'payload':
            stream.seek(offset+20)
            original = stream.read(1)
            stream.seek(offset+20)
            stream.write(bytes([original[0]^1]))
        elif damage == 'footer':
            stream.seek(-24, 2)
            stream.write(b'BADMAGIC')
        elif damage == 'length':
            stream.seek(-8, 2)
            stream.write(struct.pack('<q', length+1))
        else:
            stream.truncate(stream.seek(0, 2)-4)
    code, report = run_protocol(copy, tmp_path/'result')
    assert code == 1 and report == {'prepared':False, 'code':'damaged_package'}


@pytest.mark.parametrize('options', [
    [['/S','/ROLLBACK'], ['/s','--unknown-option']],
    [['/S','/D=C:\\not-the-installation'], ['/S','--qa-install','C:\\not-the-installation']],
])
def test_full_installer_refuses_unsupported_silent_arguments(compiled_shell, options):
    executable, _ = compiled_shell
    for arguments in options:
        result = subprocess.run([str(executable), *arguments], timeout=10)
        assert result.returncode == 1
