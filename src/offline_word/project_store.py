"""Explicit, portable project snapshots. Never unpickle project input."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields, is_dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import types
from typing import get_args, get_origin, get_type_hints, Union
import uuid
import zipfile

from PIL import Image

from .domain import ImageJob
from .project_crypto import EncryptedReader, EncryptedWriter, MAX_BYTES
from .security import OfflineError, assert_local_path
from .version import __version__

MAX_MANIFEST = 32 * 1024 * 1024
MAX_ASSETS = 20000
_ASSET = re.compile(r'[0-9a-f]{64}')
_ID = re.compile(r'[A-Za-z0-9_:-]{1,128}')
_JOB_ID = re.compile(r'[A-Za-z0-9_-]{1,128}')
DEFAULT_SETTINGS = {'layout_mode':'source', 'remove_page_numbers':False,
                    'auto_proofread':False, 'performance_profile':'sequential'}


@dataclass
class ProjectSnapshot:
    jobs: list[ImageJob]
    settings: dict
    directory: Path | None = None


def _digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _settings(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_SETTINGS):
        raise OfflineError('invalid_project_settings')
    result = DEFAULT_SETTINGS | value
    if (result['layout_mode'] not in {'source','flow','paragraph','text_only'}
            or result['performance_profile'] not in {'sequential','parallel'}
            or type(result['remove_page_numbers']) is not bool or type(result['auto_proofread']) is not bool):
        raise OfflineError('invalid_project_settings')
    return result


def _decode(annotation, value, depth=0):
    if depth > 24:
        raise ValueError('project_nesting')
    origin, arguments = get_origin(annotation), get_args(annotation)
    if origin in {Union, types.UnionType}:
        for option in arguments:
            try:
                return _decode(option, value, depth+1)
            except (TypeError, ValueError):
                continue
        raise ValueError('project_field_type')
    if annotation is type(None):
        if value is not None: raise ValueError('project_field_type')
        return None
    if is_dataclass(annotation):
        if not isinstance(value, dict) or set(value) - {item.name for item in fields(annotation)}:
            raise ValueError('project_fields')
        hints = get_type_hints(annotation)
        return annotation(**{key:_decode(hints[key],item,depth+1) for key,item in value.items()})
    if origin in {list,tuple} or annotation in {list,tuple}:
        if not isinstance(value,(list,tuple)) or len(value)>100000:
            raise ValueError('project_sequence')
        if origin is tuple and arguments and arguments[-1] is not Ellipsis:
            if len(value)!=len(arguments): raise ValueError('project_tuple')
            return tuple(_decode(t,v,depth+1) for t,v in zip(arguments,value))
        if arguments:
            result = [_decode(arguments[0],v,depth+1) for v in value]
        else:
            # Bare lists/tuples are only the historical crop/perspective fields.
            result = [_finite_geometry(v) for v in value]
        return tuple(result) if origin is tuple or annotation is tuple else result
    if annotation is float:
        if type(value) not in {float,int} or not math.isfinite(value): raise ValueError('project_number')
        return float(value)
    if annotation in {str,int,bool}:
        if type(value) is not annotation: raise ValueError('project_field_type')
        if annotation is str and len(value)>2_000_000: raise ValueError('project_text_limit')
        return value
    raise ValueError('project_field_type')


def _finite_geometry(value):
    if isinstance(value,(tuple,list)) and len(value)<=4:
        return [_finite_geometry(v) for v in value]
    if type(value) in {float,int} and math.isfinite(value):
        return value
    raise ValueError('project_geometry')


def _validate_jobs(jobs):
    if not isinstance(jobs,list) or not 1<=len(jobs)<=1000:
        raise ValueError('project_job_limit')
    seen=set()
    for job in jobs:
        if not _JOB_ID.fullmatch(job.id) or job.id in seen or job.mode not in {'scan','photo'} or not 0<=job.frame_index<1000:
            raise ValueError('project_job')
        seen.add(job.id)
        if job.display_name is not None and (not 1 <= len(job.display_name) <= 255
                or any(ord(c) < 32 or c in '/\\' for c in job.display_name)):
            raise ValueError('project_display_name')
        if job.performance_profile not in {'sequential', 'parallel'}:
            raise ValueError('project_profile')
        if job.rotation%90 or (job.crop is not None and len(job.crop)!=4) or (job.perspective is not None and len(job.perspective)!=4):
            raise ValueError('project_geometry')
        if job.result is None:
            job.status='pending'
            continue
        page=job.result
        if page.id!=job.id or not 0<page.width<=45000 or not 0<page.height<=45000 or page.width*page.height>160_000_000:
            raise ValueError('project_page')
        if (len(page.blocks) > 2000 or len(page.nontext_regions) > 2000 or len(page.filtered_text) > 5000
                or sum(len(b.text_lines) for b in page.blocks) > 5000
                or any(len(polygon) > 64 for polygon in page.nontext_regions)):
            raise ValueError('project_page_complexity')
        ids=set()
        filtered_ids = set()
        for item in page.filtered_text:
            if (not _ID.fullmatch(item.id) or item.id in filtered_ids or not _ID.fullmatch(item.source_block_id)
                    or len(item.bbox) != 4 or not all(math.isfinite(v) for v in item.bbox)
                    or item.bbox[2] <= item.bbox[0] or item.bbox[3] <= item.bbox[1]
                    or ((item.cell_row is None) != (item.cell_col is None))
                    or (item.cell_row is not None and not (0 <= item.cell_row < 1000 and 0 <= item.cell_col < 1000))):
                raise ValueError('project_filtered_text')
            filtered_ids.add(item.id)
        for block in page.blocks:
            if not _ID.fullmatch(block.id) or block.id in ids or block.kind not in {'text','title','table','image'}:
                raise ValueError('project_block')
            ids.add(block.id)
            if not 0 <= block.rows <= 1000 or not 0 <= block.cols <= 1000 or block.rows*block.cols>100000:
                raise ValueError('project_table')
            for cell in block.cells:
                if min(cell.row,cell.col)<0 or max(cell.row,cell.col)>=1000 or min(cell.rowspan,cell.colspan)<1 or max(cell.rowspan,cell.colspan)>1000:
                    raise ValueError('project_cell')
            rows = block.rows or max((c.row+c.rowspan for c in block.cells), default=0)
            cols = block.cols or max((c.col+c.colspan for c in block.cells), default=0)
            if rows*cols > 100000:
                raise ValueError('project_table')
        job.status='done'
        job.error_code=None


def save_project(snapshot: ProjectSnapshot, destination: Path, password: str):
    destination=assert_local_path(destination,'remote_project_path')
    if not destination.parent.is_dir(): raise OfflineError('project_parent_missing')
    # Validate a detached copy so a successfully saved file can be reopened,
    # without normalizing or altering the live queue as a side effect of save.
    _validate_jobs([_decode(ImageJob, asdict(job)) for job in snapshot.jobs])
    metadata={'format':1,'application_version':__version__,'settings':_settings(snapshot.settings),'jobs':[],'assets':{}}
    assets={}
    def asset(name):
        path=assert_local_path(name,'remote_project_asset')
        size=path.stat().st_size
        if not 0<size<=MAX_BYTES: raise OfflineError('project_asset_size')
        digest=_digest(path)
        assets.setdefault(digest,path)
        metadata['assets'][digest]={'size':size}
        return digest
    for job in snapshot.jobs:
        value=asdict(job)
        value['display_name']=job.display_name or Path(job.source_path).name
        value['source_path']=asset(job.source_path)
        if job.result:
            value['result']['processed_image_path']=asset(job.result.processed_image_path)
            for block in value['result']['blocks']:
                if block['image_path']: block['image_path']=asset(block['image_path'])
        metadata['jobs'].append(value)
    if not 1<=len(metadata['jobs'])<=1000 or len(assets)>MAX_ASSETS:
        raise OfflineError('project_too_large')
    encoded=json.dumps(metadata,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
    if len(encoded)>MAX_MANIFEST: raise OfflineError('project_too_large')
    descriptor,temporary=tempfile.mkstemp(prefix='.ocr-project-',suffix='.tmp',dir=destination.parent)
    try:
        with os.fdopen(descriptor,'wb') as stream:
            encrypted=EncryptedWriter(stream,password)
            with zipfile.ZipFile(encrypted,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as archive:
                archive.writestr('project.json',encoded)
                for digest,path in assets.items():
                    total=0
                    checksum=hashlib.sha256()
                    with path.open('rb') as source,archive.open('assets/'+digest,'w',force_zip64=True) as output:
                        while chunk:=source.read(1024*1024):
                            output.write(chunk)
                            checksum.update(chunk)
                            total+=len(chunk)
                    if checksum.hexdigest()!=digest or total!=metadata['assets'][digest]['size']:
                        raise OfflineError('project_source_changed')
            encrypted.finish()
            stream.flush()
            os.fsync(stream.fileno())
        assert_local_path(destination,'remote_project_path')
        os.replace(temporary,destination)
        return destination
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_project(source: Path, password: str, session_dir: Path) -> ProjectSnapshot:
    source=assert_local_path(source,'remote_project_path')
    session_dir=assert_local_path(session_dir,'remote_project_session')
    directory=None
    try:
        with source.open('rb') as stream:
            encrypted=EncryptedReader(stream,password)
            encrypted.verify_all()
            with zipfile.ZipFile(encrypted) as archive:
                entries=archive.infolist()
                if not 1<len(entries)<=MAX_ASSETS+1 or len({e.filename for e in entries})!=len(entries):
                    raise ValueError('project_archive_entries')
                if any(e.compress_type!=zipfile.ZIP_STORED or e.flag_bits&1 or e.is_dir() for e in entries):
                    raise ValueError('project_archive_type')
                info=archive.getinfo('project.json')
                if info.file_size>MAX_MANIFEST: raise ValueError('project_manifest_limit')
                metadata=json.loads(archive.read(info))
                if not isinstance(metadata,dict) or metadata.get('format')!=1: raise ValueError('project_version')
                asset_info=metadata['assets']
                if not isinstance(asset_info,dict) or any(not _ASSET.fullmatch(name) for name in asset_info):
                    raise ValueError('project_assets')
                if {e.filename for e in entries}!={'project.json',*('assets/'+name for name in asset_info)}:
                    raise ValueError('project_assets')
                if not isinstance(metadata.get('jobs'), list) or not 1 <= len(metadata['jobs']) <= 1000:
                    raise ValueError('project_job_limit')
                jobs=[_decode(ImageJob,value) for value in metadata['jobs']]
                _validate_jobs(jobs)
                settings=_settings(metadata['settings'])
                directory=session_dir/('project-'+uuid.uuid4().hex)
                directory.mkdir(mode=0o700,parents=False)
                for digest,properties in asset_info.items():
                    info=archive.getinfo('assets/'+digest)
                    if type(properties.get('size')) is not int or not 0<info.file_size==properties['size']<=MAX_BYTES:
                        raise ValueError('project_asset_size')
                    target=directory/digest
                    checksum=hashlib.sha256()
                    with archive.open(info) as entry,target.open('xb') as output:
                        while chunk:=entry.read(1024*1024):
                            output.write(chunk)
                            checksum.update(chunk)
                    target.chmod(0o600)
                    if checksum.hexdigest()!=digest: raise ValueError('project_asset_digest')
                    with Image.open(target) as image:
                        if image.format not in {'JPEG','PNG','BMP','TIFF'}: raise ValueError('project_image_type')
                        if max(image.size) > 16000 or image.width*image.height > 100_000_000:
                            raise ValueError('project_image_limit')
                        image.verify()
                def resolve(name):
                    if name not in asset_info: raise ValueError('project_missing_asset')
                    return str(directory/name)
                for job in jobs:
                    job.source_path=resolve(job.source_path)
                    with Image.open(job.source_path) as image:
                        image.seek(job.frame_index)
                        if max(image.size) > 16000 or image.width*image.height > 100_000_000:
                            raise ValueError('project_image_limit')
                    if job.result:
                        job.result.processed_image_path=resolve(job.result.processed_image_path)
                        for block in job.result.blocks:
                            if block.image_path: block.image_path=resolve(block.image_path)
        return ProjectSnapshot(jobs,settings,directory)
    except Exception as error:
        if directory is not None:
            resolved=directory.resolve()
            if resolved.parent==session_dir.resolve() and directory.name.startswith('project-'):
                shutil.rmtree(directory)
        if isinstance(error,OfflineError): raise
        raise OfflineError('invalid_project_file') from None
