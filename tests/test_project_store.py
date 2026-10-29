from dataclasses import asdict
import io
import os
import struct

from PIL import Image
import pytest

from offline_word.domain import Block, Cell, ImageJob, PageResult, TextLine
from offline_word.project_crypto import CHUNK, HEADER_SIZE, EncryptedReader, EncryptedWriter
from offline_word.project_store import ProjectSnapshot, load_project, save_project
from offline_word.security import OfflineError

PASSWORD='公开测试口令-Only-for-tests'


def snapshot(tmp_path):
    image=tmp_path/'公开测试图.png'
    Image.new('RGB',(400,600),'white').save(image)
    page=PageResult('page',0,400,600,str(image),[
        Block('body','text',(20,20,300,60),'原始识别',edited_text='人工修改',
              text_lines=[TextLine('原始识别',(20,20,300,60))],candidates=['另一候选']),
        Block('table','table',(20,100,300,160),rows=1,cols=1,
              cells=[Cell(0,0,'原表格',edited_text='校对表格',needs_geometry_review=True)]),
        Block('figure','image',(20,200,300,300),image_path=str(image))])
    return ProjectSnapshot([ImageJob('page',str(image),result=page,status='done'),
                            ImageJob('pending',str(image),mode='photo')],
                           {'layout_mode':'paragraph','remove_page_numbers':True})


def test_project_portable_roundtrip_preserves_text_edits_images_order_and_settings(tmp_path):
    original=snapshot(tmp_path)
    target=tmp_path/'work.ocrproj'
    save_project(original,target,PASSWORD)
    ciphertext=target.read_bytes()
    assert b'PK\x03\x04' not in ciphertext
    for text in ('人工修改','原始识别',str(tmp_path),'公开测试图.png'):
        assert text.encode() not in ciphertext
    (tmp_path/'公开测试图.png').unlink()
    session=tmp_path/'session';session.mkdir()
    loaded=load_project(target,PASSWORD,session)
    assert [job.id for job in loaded.jobs]==['page','pending']
    assert loaded.jobs[0].display_name=='公开测试图.png'
    result=loaded.jobs[0].result
    assert result.blocks[0].text=='人工修改' and result.blocks[0].original_text=='原始识别'
    assert result.blocks[1].cells[0].text=='校对表格'
    assert result.blocks[1].cells[0].needs_geometry_review
    assert result.blocks[2].image_path==loaded.jobs[0].source_path
    assert Image.open(loaded.jobs[0].source_path).size==(400,600)
    assert loaded.settings['layout_mode']=='paragraph' and loaded.settings['remove_page_numbers']
    assert loaded.jobs[1].status=='pending'


@pytest.mark.parametrize('mutation',['password','truncated','tail','header','chunk'])
def test_rejects_wrong_password_and_tampering_without_leaving_imported_files(tmp_path,mutation):
    target=tmp_path/'work.ocrproj'
    save_project(snapshot(tmp_path),target,PASSWORD)
    data=bytearray(target.read_bytes())
    password=PASSWORD
    if mutation=='password': password='这是错误的测试密码1234'
    if mutation=='truncated': data=data[:-20]
    if mutation=='tail': data+=b'junk'
    if mutation=='header': data[10]^=1
    if mutation=='chunk': data[HEADER_SIZE+10]^=1
    target.write_bytes(data)
    session=tmp_path/'session';session.mkdir()
    with pytest.raises(OfflineError): load_project(target,password,session)
    assert list(session.iterdir())==[]


def test_cipher_seek_across_record_boundaries_and_final_authentication():
    content=os.urandom(2*CHUNK+113)
    output=io.BytesIO()
    writer=EncryptedWriter(output,PASSWORD)
    for offset in range(0,len(content),77777): writer.write(content[offset:offset+77777])
    writer.finish()
    reader=EncryptedReader(io.BytesIO(output.getvalue()),PASSWORD)
    reader.verify_all()
    reader.seek(CHUNK-13)
    assert reader.read(100)==content[CHUNK-13:CHUNK+87]
    reader.seek(-37,2)
    assert reader.read()==content[-37:]
    reader.seek(len(content)+20)
    assert reader.read()==b''
    # Reordering authenticated full records must fail even with valid tags.
    data=output.getvalue()
    stride=CHUNK+20
    swapped=data[:HEADER_SIZE]+data[HEADER_SIZE+stride:HEADER_SIZE+2*stride]+data[HEADER_SIZE:HEADER_SIZE+stride]+data[HEADER_SIZE+2*stride:]
    with pytest.raises(OfflineError):
        EncryptedReader(io.BytesIO(swapped),PASSWORD).verify_all()


def test_failed_save_keeps_previous_encrypted_file_and_removes_temporary(tmp_path,monkeypatch):
    import offline_word.project_store as store
    target=tmp_path/'work.ocrproj'
    value=snapshot(tmp_path)
    save_project(value,target,PASSWORD)
    previous=target.read_bytes()
    def fail(*args): raise OSError('write failure')
    monkeypatch.setattr(store.os,'replace',fail)
    with pytest.raises(OSError): save_project(value,target,PASSWORD)
    assert target.read_bytes()==previous
    assert not list(tmp_path.glob('.ocr-project-*'))


def test_schema_rejects_unknown_fields_and_excessive_table_geometry(tmp_path):
    from offline_word.project_store import _decode, _validate_jobs
    job=snapshot(tmp_path).jobs[0]
    value=asdict(job)
    value['execute']='arbitrary code'
    with pytest.raises(ValueError): _decode(ImageJob,value)
    job.result.blocks[1].rows=1_000_000
    with pytest.raises(ValueError): _validate_jobs([job])


def test_reader_rejects_unbounded_record_length_before_allocating():
    output=io.BytesIO()
    writer=EncryptedWriter(output,PASSWORD)
    writer.write(b'abc');writer.finish()
    data=output.getvalue()
    data=data[:HEADER_SIZE]+struct.pack('>I',2**32-1)+data[HEADER_SIZE+4:]
    with pytest.raises(OfflineError): EncryptedReader(io.BytesIO(data),PASSWORD)


def test_project_limits_work_for_untrusted_line_and_table_geometry(tmp_path):
    from offline_word.project_store import _validate_jobs
    job=snapshot(tmp_path).jobs[0]
    job.result.blocks[1].cells[0].row=10**12
    with pytest.raises(ValueError): _validate_jobs([job])
    job.result.blocks[1].cells[0].row=0
    job.result.blocks[0].text_lines *= 5001
    with pytest.raises(ValueError, match='project_page_complexity'): _validate_jobs([job])
