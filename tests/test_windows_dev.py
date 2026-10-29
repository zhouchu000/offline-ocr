from pathlib import Path

import pytest

from tools import windows_dev
from offline_word.version import __version__


def test_source_guard_rejects_installed_or_old_checkout(tmp_path):
    root = tmp_path / 'current'
    windows_dev.check_source_origin(root, root / 'src/offline_word/__init__.py')
    with pytest.raises(RuntimeError, match='current source'):
        windows_dev.check_source_origin(root, tmp_path / 'old/app/offline_word/__init__.py')


def test_build_commands_use_shipped_runtime_models_and_current_source(tmp_path):
    root = tmp_path / 'project'
    output = tmp_path / 'new-build'
    commands = windows_dev.build_commands(root, Path('python.exe'), output, __version__)
    assert len(commands) == 2
    portable, installer = commands
    assert portable[portable.index('--source') + 1] == str(root / 'src')
    assert portable[portable.index('--site-packages') + 1] == str(root / 'runtime/windows-python/Lib/site-packages')
    assert portable[portable.index('--models') + 1] == str(root / 'models')
    assert installer[installer.index('--makensis') + 1] == str(root / 'toolchains/nsis/makensis.exe')
    assert str(output / f'OfflineWord-{__version__}-Windows-x64') in installer
    assert all('pip' not in command for command in commands)
