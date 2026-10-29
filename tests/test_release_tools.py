import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest


def archive(path, entries):
    with tarfile.open(path, 'w') as tar:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_prepare_rejects_changed_archive_without_output(tmp_path):
    from tools.prepare_models import prepare
    source = tmp_path / 'archives'
    source.mkdir()
    archive(source / 'model.tar', {'model/inference.json': b'{}'})
    catalog = {'schema': 1, 'models': {'model': {'archive': 'model.tar', 'sha256': '0' * 64}}}
    with pytest.raises(ValueError, match='checksum'):
        prepare(catalog, source, tmp_path / 'out')
    assert not (tmp_path / 'out' / 'manifest.json').exists()


@pytest.mark.parametrize('member', ['../outside', '/tmp/outside', 'model/../../outside'])
def test_prepare_refuses_tar_path_escape(tmp_path, member):
    from tools.prepare_models import prepare
    source = tmp_path / 'archives'
    source.mkdir()
    digest = archive(source / 'model.tar', {member: b'bad'})
    catalog = {'schema': 1, 'models': {'model': {'archive': 'model.tar', 'sha256': digest}}}
    with pytest.raises(ValueError):
        prepare(catalog, source, tmp_path / 'out')
    assert not (tmp_path / 'outside').exists()


def test_prepare_materializes_models_and_actual_file_hashes(tmp_path):
    from tools.prepare_models import prepare
    source = tmp_path / 'archives'
    source.mkdir()
    digest = archive(source / 'model.tar', {'model/inference.json': b'{}', 'model/inference.pdiparams': b'weights'})
    catalog = {'schema': 1, 'models': {'model': {'archive': 'model.tar', 'sha256': digest}}}
    result = prepare(catalog, source, tmp_path / 'out')
    saved = json.loads((tmp_path / 'out' / 'manifest.json').read_text())
    assert saved == result
    assert saved['models']['model']['files']['inference.pdiparams'] == hashlib.sha256(b'weights').hexdigest()
    assert (tmp_path / 'out' / 'model' / 'inference.json').read_bytes() == b'{}'


def test_prepare_preserves_existing_destination(tmp_path):
    from tools.prepare_models import prepare
    out = tmp_path / 'models'
    out.mkdir()
    (out / 'keep').write_text('existing')
    with pytest.raises(FileExistsError):
        prepare({'schema': 1, 'models': {}}, tmp_path, out)
    assert (out / 'keep').read_text() == 'existing'
