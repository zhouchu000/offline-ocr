"""Exercise the real updater executable against an isolated release copy."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import time

from offline_word.offline_update import digest, verify_tree


def file_map(root):
    value = json.loads((root/'checksums.json').read_bytes())
    value['checksums.json'] = digest(root/'checksums.json')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('base', 'new', 'updater', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    target = (args.output/'installed').resolve()
    shutil.copytree(args.base, target)
    old, new = file_map(args.base), file_map(args.new)
    verify_tree(target, old)
    report = {'pass': False, 'isolated_target': str(target), 'steps': []}
    updater = args.updater.resolve()
    support = updater.parent/'support'
    cli = [str(support/'runtime/python.exe'), '-I', '-B', str(support/'update_cli.py'),
           '--target', str(target), '--package', str(support/'update.ocrupdate')]
    # This is our own bounded helper, launched from the copied installation.
    process = subprocess.Popen([str(target/'runtime/python.exe'), '-I', '-B', '-c',
        'import time; print("ready", flush=True); time.sleep(90)'],
        stdout=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        assert process.stdout.readline().strip() == 'ready'
        blocked = subprocess.run(cli, capture_output=True, timeout=120)
        assert blocked.returncode == 1 and b'application_or_update_running' in blocked.stdout
        verify_tree(target, old)
        report['steps'].append({'action': 'running_application_refused', 'pass': True})
    finally:
        process.terminate()
        process.wait(timeout=10)
        process.stdout.close()
    for action, options, expected in (
        ('update', [], new), ('repeat_update', [], new),
        ('rollback', ['/ROLLBACK'], old), ('update_after_rollback', [], new)):
        begin = time.monotonic()
        # NSIS requires /D to be the final argument and its path unquoted.
        command = '"'+str(updater)+'" /S '+ ' '.join(options)+' /D='+str(target)
        result = subprocess.run(command, timeout=300, creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0, (action, result.returncode)
        verify_tree(target, expected)
        report['steps'].append({'action': action, 'pass': True,
            'verified_files': len(expected), 'elapsed_seconds': round(time.monotonic()-begin, 3)})
        print(json.dumps(report['steps'][-1]), flush=True)
    report.update(pass_=True, updater_sha256=digest(updater))
    report['pass'] = report.pop('pass_')
    (args.output/'report.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
