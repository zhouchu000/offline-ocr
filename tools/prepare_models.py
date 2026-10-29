"""Build-time tool. The desktop app never imports this downloader/extractor."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile
import urllib.request

VERSIONS = {'paddleocr': '3.7.0', 'paddlex': '3.7.0', 'paddlepaddle': '3.3.0'}


def sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def _name(value: str) -> str:
    if value in ('.', '..') or not re.fullmatch(r'[A-Za-z0-9_+.\-]+', value):
        raise ValueError('invalid model or archive name')
    return value


def prepare(catalog: dict, archives: Path, output: Path) -> dict:
    """Verify all archives, extract without links/traversal, then publish a manifest."""
    if output.exists():
        raise FileExistsError('model destination already exists')
    if catalog.get('schema') != 1 or not catalog.get('models'):
        raise ValueError('invalid or empty model catalog')
    for model, entry in catalog['models'].items():
        _name(model)
        file = archives / _name(entry['archive'])
        if file.is_symlink() or sha256(file) != entry['sha256']:
            raise ValueError('archive checksum mismatch')
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.models-', dir=output.parent))
    manifest = {'schema': 1, 'versions': VERSIONS.copy(), 'models': {}}
    try:
        for model, entry in catalog['models'].items():
            target = stage / model
            target.mkdir()
            total = 0
            with tarfile.open(archives / entry['archive'], 'r:*') as tar:
                for member in tar:
                    name = PurePosixPath(member.name)
                    if name.is_absolute() or '..' in name.parts or '\\' in member.name or ':' in member.name:
                        raise ValueError('unsafe archive path')
                    if not name.parts or name.parts[0] not in (model, model + '_infer'):
                        raise ValueError('unexpected archive root')
                    if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                        raise ValueError('archive links and special files are forbidden')
                    relative = Path(*name.parts[1:])
                    dest = target / relative
                    if member.isdir():
                        dest.mkdir(parents=True, exist_ok=True)
                        continue
                    total += member.size
                    if total > 2 * 1024**3:
                        raise ValueError('model archive exceeds size limit')
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists():
                        raise ValueError('duplicate archive member')
                    source = tar.extractfile(member)
                    if source is None:
                        raise ValueError('missing archive data')
                    with source, dest.open('xb') as stream:
                        shutil.copyfileobj(source, stream)
            files = {p.relative_to(target).as_posix(): sha256(p)
                     for p in sorted(target.rglob('*')) if p.is_file()}
            if not files:
                raise ValueError('empty model archive')
            manifest['models'][model] = {'directory': model, 'files': files}
        (stage / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        if output.exists():
            raise FileExistsError('model destination appeared during preparation')
        stage.rename(output)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return manifest


def download(catalog: dict, archives: Path) -> None:
    archives.mkdir(parents=True, exist_ok=True)
    for entry in catalog['models'].values():
        dest = archives / _name(entry['archive'])
        if dest.is_file() and not dest.is_symlink() and sha256(dest) == entry['sha256']:
            continue
        if dest.exists():
            raise ValueError('existing archive checksum mismatch; inspect it before retrying')
        if not entry['url'].startswith('https://paddle-model-ecology.bj.bcebos.com/'):
            raise ValueError('unapproved model host')
        temp = dest.with_suffix('.part')
        try:
            with urllib.request.urlopen(entry['url'], timeout=60) as response, temp.open('xb') as stream:
                shutil.copyfileobj(response, stream)
            if sha256(temp) != entry['sha256']:
                raise ValueError('download checksum mismatch')
            os.replace(temp, dest)
        finally:
            temp.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description='Prepare pinned local models on a non-confidential build machine')
    parser.add_argument('--catalog', type=Path, default=Path('models.lock.json'))
    parser.add_argument('--archives', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--download', action='store_true', help='Explicitly permit build-time downloads')
    args = parser.parse_args()
    catalog = json.loads(args.catalog.read_text(encoding='utf-8'))
    if args.download:
        download(catalog, args.archives)
    result = prepare(catalog, args.archives, args.output)
    print(f"Verified and prepared {len(result['models'])} local models.")


if __name__ == '__main__':
    main()
