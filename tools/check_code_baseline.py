"""Run pinned offline lint/type checks without changing the shipped runtime."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    quality = root/'toolchains/quality'
    ruff = quality/'bin/ruff.exe'
    pyright = quality/'pyright/dist/index.js'
    node = args.node or (Path(shutil.which('node')) if shutil.which('node') else None)
    if not ruff.is_file() or not pyright.is_file() or node is None:
        raise SystemExit('Missing development quality tools; see docs/工程检查.md. The OCR runtime is unchanged.')
    files = [f'src/offline_word/{name}.py' for name in ('domain','security','images','exporter')]
    for command in ([str(ruff),'check',*files], [str(node),str(pyright),'--pythonpath',sys.executable]):
        result = subprocess.run(command,cwd=root)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
