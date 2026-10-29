from dataclasses import asdict
import hashlib
import pickle

import numpy as np
from PIL import Image, ImageDraw
import pytest

from offline_word.domain import Block, Cell, ImageJob, PageResult, normalize_table_cells, table_needs_review
from offline_word.images import prepare_image
from offline_word.paper import PaperDetection, apply_paper_mask, detect_paper_quad, review_outside_paper, rotate_polygon


def paper_scene(fraction=.5, *, two=False):
    image = Image.new('RGB', (1000, 900), '#51565d')
    draw = ImageDraw.Draw(image)
    if two:
        boxes = [(40, 130, 440, 760), (540, 140, 940, 770)]
    else:
        h = int((1000*900*fraction/0.8)**.5)
        w = int(h*.8)
        boxes = [((1000-w)//2, (900-h)//2, (1000+w)//2, (900+h)//2)]
    for left, top, right, bottom in boxes:
        draw.rectangle((left, top, right, bottom), fill='#f7f5ee')
        for y in range(top+30, bottom-25, 35):
            for x in range(left+20, right-20, 19):
                draw.rectangle((x, y, x+8, y+15), fill='#222222')
    return image, boxes


@pytest.mark.parametrize('fraction', [.3, .5, .7])
def test_detects_small_and_large_paper_without_editing_input(fraction):
    image, boxes = paper_scene(fraction)
    original = image.tobytes()
    detection = detect_paper_quad(image)
    assert detection.status == 'suggested'
    xs, ys = zip(*detection.quad)
    left, top, right, bottom = boxes[0]
    assert min(xs) <= left+2 and max(xs) >= right-2
    assert min(ys) <= top+2 and max(ys) >= bottom-2
    assert image.tobytes() == original


def test_competing_pages_and_plain_scan_do_not_choose_one_page():
    image, _ = paper_scene(two=True)
    assert detect_paper_quad(image).quad is None
    assert detect_paper_quad(Image.new('RGB', (800, 1100), 'white')).quad is None


def test_mask_retains_inside_edge_and_original_pixels():
    image = Image.new('RGB', (100, 100), '#444444')
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 80, 80), fill='white')
    draw.rectangle((19, 25, 22, 70), fill='black')
    original = image.tobytes()
    masked = apply_paper_mask(image, [(20,20),(80,20),(80,80),(20,80)])
    assert masked.getpixel((19, 40)) == (0,0,0)
    assert masked.getpixel((0, 0)) == (255,255,255)
    assert image.tobytes() == original


def test_photo_is_never_automatically_cropped_and_manual_selection_wins(tmp_path, monkeypatch):
    from offline_word import images, paper
    image, _ = paper_scene()
    path = tmp_path/'photo.png'
    image.save(path)
    original = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(images, '_deskew_angle', lambda image: None)
    monkeypatch.setattr(images, '_reduce_photo_shadows', lambda image: image)
    job = ImageJob('p', str(path), mode='photo')
    prepared = prepare_image(job, tmp_path/'session')
    with Image.open(prepared) as output:
        assert output.size == image.size
        assert output.tobytes() == image.tobytes()
    assert job.paper_status == 'suggested'
    monkeypatch.setattr(paper, 'detect_paper_quad', lambda image: pytest.fail('manual crop must bypass detector'))
    job.crop = (10, 20, 990, 880)
    cropped = prepare_image(job, tmp_path/'session')
    with Image.open(cropped) as output:
        assert output.size == (980, 860)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original


@pytest.mark.parametrize('clockwise', [0, 90, 180, 270])
def test_paper_geometry_follows_processed_rotation(tmp_path, monkeypatch, clockwise):
    from offline_word import images
    image, _ = paper_scene()
    source = tmp_path/'page.png'
    image.save(source)
    quad = [(100.,100.),(800.,100.),(800.,700.),(100.,700.)]
    job = ImageJob('p', str(source), mode='photo', rotation=clockwise, paper_quad=quad, paper_status='suggested')
    monkeypatch.setattr(images, '_deskew_angle', lambda image: None)
    prepare_image(job, tmp_path/'session')
    assert job.processed_paper_polygon == rotate_polygon(quad, image.width, image.height, clockwise)


def test_boundary_review_keeps_all_text_images_tables_and_manual_edits():
    blocks = [Block('inside','text',(30,30,60,60),'正文'),
              Block('cross','text',(10,30,35,60),'边缘文字',edited_text='人工修改'),
              Block('outside','text',(0,0,10,10),'纸外文字'),
              Block('table','table',(70,70,99,99), cells=[Cell(0,0,'表格文字')]),
              Block('image','image',(0,75,15,99))]
    page = PageResult('p',0,100,100,'public.png',blocks,paper_status='suggested',paper_polygon=[(20,20),(80,20),(80,80),(20,80)])
    before = [(b.id,b.text,b.kind) for b in blocks]
    assert review_outside_paper(page) == 4
    assert [(b.id,b.text,b.kind) for b in page.blocks] == before
    assert page.blocks[1].edited_text == '人工修改'
    assert not blocks[0].needs_review
    page.paper_status = 'kept'
    assert review_outside_paper(page) == 0


def test_geometry_review_flag_survives_data_and_process_roundtrips():
    cells = normalize_table_cells([Cell(0,0,'原文', edited_text='人工修改',colspan=2),Cell(0,1,'重叠原文')],1,2)
    assert table_needs_review(cells)
    assert asdict(cells[0])['needs_geometry_review'] is True
    assert pickle.loads(pickle.dumps(cells))[0].needs_geometry_review
    assert cells[0].text == '人工修改'


def test_failed_optional_paper_detection_still_prepares_the_whole_image(tmp_path, monkeypatch):
    from offline_word import images, paper
    image, _ = paper_scene()
    source = tmp_path/'public.png'
    image.save(source)
    def failure(image):
        raise RuntimeError('optional detector unavailable')
    monkeypatch.setattr(paper, 'detect_paper_quad', failure)
    monkeypatch.setattr(images, '_deskew_angle', lambda image: None)
    monkeypatch.setattr(images, '_reduce_photo_shadows', lambda image: image)
    job = ImageJob('public',str(source),mode='photo')
    with Image.open(prepare_image(job,tmp_path/'session')) as result:
        assert result.tobytes() == image.tobytes()
    assert job.paper_status == 'unavailable' and job.paper_quad is None
