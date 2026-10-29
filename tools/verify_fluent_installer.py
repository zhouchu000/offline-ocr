"""Real WPF -> NSIS -> isolated install, cancellation, retry and uninstall."""
import argparse
import json
from pathlib import Path
import subprocess
import uuid

from offline_word.package_verify import verify
from offline_word.version import __version__
from tools.build_fluent_installer import wrap
from tools.build_windows_installer import _safe_define_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    # Reuse the public, small model fixture. Never run it as an OCR application.
    from tests.test_windows_installer import _portable_fixture
    payload = _portable_fixture(output)
    profile = output/'profile'
    installed = profile/'Programs/OfflineWord'
    protected = output/'outside-preserved.txt'
    protected.write_text('Keep outside the isolated install and uninstall roots.', encoding='utf-8')
    registry = 'Software\\OfflineWordInstallerAcceptance\\'+uuid.uuid4().hex
    script = (root/'packaging/windows/OfflineWord.nsi').read_text(encoding='utf-8')
    # Compile an isolated copy of the exact production script. The released
    # executable has none of these path/registry substitutions or QA switches.
    replacements = {
        '$LOCALAPPDATA': str(profile), '$SMPROGRAMS': str(output/'start-menu'),
        '$DESKTOP': str(output/'desktop'),
        'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\OfflineWord': registry,
    }
    for original, replacement in replacements.items():
        assert original in script
        script = script.replace(original, replacement)
    (output/'desktop').mkdir()
    (output/'start-menu').mkdir()
    source = output/'isolated.nsi'
    source.write_text(script, encoding='utf-8')
    backend = output/'backend.exe'
    subprocess.run([str(root/'toolchains/nsis/makensis.exe'), '-INPUTCHARSET', 'UTF8', '-V2',
        '-DPAYLOAD_FILES='+_safe_define_path(payload/'*.*'),
        '-DAPP_ICON='+_safe_define_path(payload/'app/offline_word/assets/app.ico'),
        '-DOUTPUT_FILE='+_safe_define_path(backend), '-DAPP_VERSION='+__version__,
        '-DAPP_VERSION_NUM='+__version__+'.0', '-DPAYLOAD_KB=200', '-DFLUENT_BACKEND', str(source)], check=True)
    frontend = output/'acceptance.exe'
    wrap(backend, frontend, kind='install', payload=payload, qa=True)
    subprocess.run([str(frontend), '--qa-install', str(installed), str(output/'ui')], timeout=90, check=True)
    ui = json.loads((output/'ui/report.json').read_text(encoding='utf-8-sig'))
    assert ui['pass'] and verify(installed)['pass']
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry) as key:
        assert winreg.QueryValueEx(key,'DisplayVersion')[0] == __version__
    assert (output/'desktop/离线图片转Word.lnk').is_file()
    uninstaller = profile/'OfflineWord-Uninstaller/uninstall.exe'
    # NSIS otherwise relaunches an asynchronous temporary uninstaller copy.
    # _?= makes this owned acceptance process synchronous at its original path.
    command = '"'+str(uninstaller)+'" /S _?='+str(uninstaller.parent)
    subprocess.run(command, check=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    assert not installed.exists()
    assert protected.read_text(encoding='utf-8') == 'Keep outside the isolated install and uninstall roots.'
    try:
        winreg.OpenKey(winreg.HKEY_CURRENT_USER,registry).Close()
        raise AssertionError('Acceptance uninstall registry key remains')
    except FileNotFoundError:
        pass
    assert not (output/'desktop/离线图片转Word.lnk').exists()
    result = dict(pass_=True, frontend=ui, isolated_uninstall=True,
                  outside_file_preserved=True, production_user_installation_modified=False)
    result['pass'] = result.pop('pass_')
    (output/'report.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
