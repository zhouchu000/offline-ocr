"""Build a clean committed release, including only public documentation."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys

from offline_word.version import __version__
from tools.windows_dev import build_commands
from tools.build_source_release import PUBLIC_DOCS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--extras', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if subprocess.check_output(['git','diff','HEAD','--name-only'], cwd=root):
        raise RuntimeError('Commit source changes before building a release.')
    if args.output.exists() or args.extras.exists():
        raise FileExistsError('Release output or extras already exists.')
    docs = args.extras.resolve()/'docs'
    docs.mkdir(parents=True)
    names = subprocess.check_output(['git','ls-files','-z','--','docs'], cwd=root).decode('utf-8').split('\0')
    for name in (name for name in names if name in PUBLIC_DOCS):
        target = docs/Path(name).relative_to('docs')
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(root/name,target)
    commands = build_commands(root,Path(sys.executable),args.output.resolve(),__version__)
    commands[0] = [str(docs) if part==str(root/'docs') else part for part in commands[0]]
    for command in commands:
        subprocess.run(command,cwd=root,check=True)


if __name__=='__main__':
    main()
