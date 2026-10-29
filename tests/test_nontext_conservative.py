"""Reject clear graphic evidence without deleting uncertain surrounding text."""
from copy import deepcopy
import hashlib

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from offline_word.domain import Block, Cell, ImageJob, PageResult, TextLine
from offline_word.nontext import NonTextImage, covered_by_nontext, filter_nontext, mask_nontext
from offline_word.worker import page_from_plain_prediction, page_from_prediction, _missing_line_regions


def band_image(path, kind='uneven'):
    image = Image.new('L', (900, 600), 'white')
    draw = ImageDraw.Draw(image)
    for i, x in enumerate(range(150, 550, 7)):
        top = 220 + ((i*7) % 19 if kind in {'uneven', 'both'} else 0)
        bottom = 290 - ((i*11) % 17 if kind in {'uneven', 'both'} else 0)
        draw.rectangle((x, top, x+(2 if i % 2 else 4), bottom), fill=0)
    if kind in {'interrupted', 'both'}:
        for y in range(225, 290, 7):
            draw.rectangle((149, y, 555, y+1), fill=255)
    image.save(path)
    return image


@pytest.mark.parametrize('kind', ['uneven', 'interrupted', 'both'])
@pytest.mark.parametrize('mode', ['plain', 'structure'])
def test_clear_irregular_bands_filtered_without_losing_surrounding_text(tmp_path, kind, mode):
    path = tmp_path / 'band.png'
    image = band_image(path, kind)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=22)
    text = ['上方正文', '左侧说明', '右侧金额128.50', '下方编号12345678', '川川山川']
    boxes = [(150, 180, 550, 205), (15, 240, 140, 266),
             (565, 240, 860, 266), (150, 307, 550, 333), (153, 242, 545, 268)]
    for value, box in zip(['UPPER TEXT', 'LEFT TEXT', 'RIGHT 128.50', 'NUMBER 12345678'], boxes):
        draw.text(box[:2], value, font=font, fill=0)
    image.save(path)
    original = hashlib.sha256(path.read_bytes()).digest()
    raw = {'rec_texts': text, 'rec_boxes': boxes, 'rec_scores': [.55]*4+[.99]}
    job = ImageJob('p', str(path), text_only=mode == 'plain')
    if mode == 'plain':
        page = page_from_plain_prediction(job, path, raw)
    else:
        page = page_from_prediction(job, path, {'overall_ocr_res': raw, 'parsing_res_list': [
            {'block_label': 'text', 'block_bbox': [0, 170, 870, 350], 'block_content': '\n'.join(text)}]})
    result = ''.join(b.text for b in page.blocks if b.kind != 'image')
    assert '川川山川' not in result
    assert [item.text for item in page.filtered_text] == ['川川山川']
    assert not page.filtered_text[0].restored
    assert all(value in result for value in text[:4])
    assert page.nontext_regions
    assert any(b.kind == 'image' for b in page.blocks) == (mode == 'structure')
    assert hashlib.sha256(path.read_bytes()).digest() == original


@pytest.mark.parametrize('angle', [-.8, .8])
@pytest.mark.parametrize('kind', ['uneven', 'interrupted', 'both'])
def test_deskewed_uncertain_band_cannot_remove_a_mixed_text_line(tmp_path, angle, kind):
    import cv2
    path = tmp_path / 'deskewed.png'
    image = band_image(path, kind)
    matrix = cv2.getRotationMatrix2D((450, 300), angle, 1.0)
    image = Image.fromarray(cv2.warpAffine(np.asarray(image), matrix, (900, 600),
                                         flags=cv2.INTER_CUBIC, borderValue=255))
    image.save(path)
    draw = ImageDraw.Draw(image)
    draw.text((565, 240), 'KEEP 128.50', font=ImageFont.load_default(size=24), fill=210)
    image.save(path)
    page = page_from_plain_prediction(ImageJob('p', str(path), text_only=True), path,
                                     {'rec_texts': ['川川山川 正文128.50'], 'rec_boxes': [[150, 240, 780, 270]], 'rec_scores': [.99]})
    assert [b.text for b in page.blocks] == ['川川山川 正文128.50']
    assert not covered_by_nontext((565, 240, 760, 266), page.nontext_regions)


@pytest.mark.parametrize('kind', ['text', 'table'])
def test_line_or_cell_partially_covering_code_retains_all_text(tmp_path, kind):
    path = tmp_path / 'band.png'
    band_image(path, 'aligned')
    box = (150, 240, 595, 270)  # More than 85% inside the code, real text outside.
    text = '川川山川 正常说明'
    if kind == 'text':
        block = Block('mixed', kind, box, text, text_lines=[TextLine(text, box)])
    else:
        block = Block('mixed', kind, box, rows=1, cols=1, cells=[Cell(0, 0, text, bbox=box)])
    result = filter_nontext(PageResult('p', 0, 900, 600, str(path), [block], text_only=True))
    assert len(result.blocks) == 1
    kept = result.blocks[0]
    assert (kept.text if kind == 'text' else kept.cells[0].text) == text
    assert (kept.needs_review if kind == 'text' else kept.cells[0].needs_review)
    assert not result.filtered_text  # Uncertain text remains in the document.


@pytest.mark.parametrize('geometry', ['none', 'partial', 'table-unknown'])
def test_incomplete_geometry_cannot_delete_unlocated_caption(tmp_path, geometry):
    path = tmp_path / 'band.png'
    band_image(path, 'aligned')
    box = (155, 240, 535, 270)
    block = Block('unlocated', 'text', box, '川川山川\n说明必须保留')
    if geometry == 'partial':
        block.text_lines = [TextLine('川川山川', box)]
    elif geometry == 'table-unknown':
        block = Block('unlocated', 'table', box, rows=1, cols=2,
                      cells=[Cell(0, 0, '川川山川', bbox=box), Cell(0, 1, '说明必须保留')])
    result = filter_nontext(PageResult('p', 0, 900, 600, str(path), [block], text_only=True))
    assert len(result.blocks) == 1
    assert '说明必须保留' in ''.join(b.text + ''.join(c.text for c in b.cells) for b in result.blocks)


def test_partial_geometry_does_not_remove_a_substring_from_real_sentence(tmp_path):
    path = tmp_path / 'band.png'
    band_image(path, 'aligned')
    block = Block('ambiguous', 'text', (100, 200, 600, 330), '正文提到山川但不可删除',
                  text_lines=[TextLine('山川', (155, 240, 535, 270))])
    result = filter_nontext(PageResult('p', 0, 900, 600, str(path), [block], text_only=True))
    assert result.blocks[0].text == '正文提到山川但不可删除'
    assert result.blocks[0].needs_review


@pytest.mark.parametrize('ink', [0, 210])
def test_text_in_irregular_band_corner_vetoes_rectangular_mask(tmp_path, ink):
    path = tmp_path / 'band.png'
    image = band_image(path)
    # A small genuine component in a gap, not connected to a bar.
    ImageDraw.Draw(image).rectangle((190, 221, 192, 223), fill=ink)
    image.save(path)
    analysis = NonTextImage.read(path)
    assert not analysis.polygons


@pytest.mark.parametrize('side', ['left', 'right', 'top', 'bottom'])
@pytest.mark.parametrize('ink', [0, 210])
def test_touching_caption_vetoes_the_whole_band_mask(tmp_path, side, ink):
    path = tmp_path / 'touching.png'
    image = band_image(path, 'aligned')
    boxes = {'left': (148, 245, 149, 251), 'right': (552, 245, 553, 251),
             'top': (300, 218, 306, 219), 'bottom': (300, 291, 306, 292)}
    ImageDraw.Draw(image).rectangle(boxes[side], fill=ink)
    image.save(path)
    assert not NonTextImage.read(path).polygons


@pytest.mark.parametrize('value', ['11111111111111111111', 'IllIllIllIllIllIllIll',
                                 'x = 1 / 2 + 128.50', '||||||||||||||||||||'])
def test_actual_text_pixels_are_not_barcode_evidence(tmp_path, value):
    path = tmp_path / 'real-text.png'
    image = Image.new('L', (900, 600), 'white')
    ImageDraw.Draw(image).text((140, 220), value, font=ImageFont.load_default(size=32), fill=0)
    image.save(path)
    assert not NonTextImage.read(path).polygons


@pytest.mark.parametrize('ink', [0, 210])
def test_fraction_and_underline_with_text_cannot_be_removed_as_ornaments(tmp_path, ink):
    path = tmp_path / 'math.png'
    image = Image.new('L', (1200, 600), 'white')
    draw = ImageDraw.Draw(image)
    draw.line((150, 250, 950, 250), fill=0, width=3)
    draw.text((540, 222), '1', font=ImageFont.load_default(size=24), fill=ink)
    draw.text((540, 260), '2', font=ImageFont.load_default(size=24), fill=ink)
    image.save(path)
    box = (145, 218, 955, 290)
    analysis = NonTextImage.read(path)
    assert not analysis.is_ornament(box, '1/2')
    # An underlined word touching its rule remains genuine text as well.
    image = Image.new('L', (1200, 600), 'white')
    draw = ImageDraw.Draw(image)
    draw.line((150, 250, 950, 250), fill=0, width=3)
    draw.text((530, 231), 'HI', font=ImageFont.load_default(size=24), fill=ink)
    image.save(path)
    assert not NonTextImage.read(path).is_ornament((145, 228, 955, 254), 'HI')


def test_hand_correction_is_preserved_even_inside_code(tmp_path):
    path = tmp_path / 'band.png'
    band_image(path)
    box = (155, 240, 535, 270)
    blocks = [Block('edit', 'text', box, '乱码', edited_text='人工确认的正文'),
              Block('cells', 'table', box, rows=1, cols=1,
                    cells=[Cell(0, 0, '乱码', edited_text='人工确认的编号', bbox=box)])]
    expected = deepcopy(blocks)
    result = filter_nontext(PageResult('p', 0, 900, 600, str(path), blocks, text_only=True))
    assert result.blocks == expected


def test_missing_line_scan_keeps_text_immediately_outside_code(tmp_path):
    path = tmp_path / 'adjacent.png'
    image = band_image(path)
    ImageDraw.Draw(image).text((562, 245), 'KEEP ALL TEXT', font=ImageFont.load_default(size=26), fill=0)
    image.save(path)
    page = page_from_plain_prediction(ImageJob('p', str(path), text_only=True), path, {'rec_texts': []})
    assert page.nontext_regions
    regions = _missing_line_regions(page, path)
    assert any(left <= 580 < right and top <= 261 < bottom for left, top, right, bottom in regions)


@pytest.mark.parametrize('mode', ['L', 'RGB'])
@pytest.mark.parametrize('polygon', [[(30, 30), (90, 30), (90, 80), (30, 80)],
                                   [(60, 20), (100, 60), (60, 100), (20, 60)]])
def test_mask_keeps_original_and_every_pixel_outside_polygon(mode, polygon):
    import cv2
    source = Image.new(mode, (120, 120), 'black')
    before = source.tobytes()
    result = mask_nontext(source, [polygon])
    assert source.tobytes() == before
    assert result.getpixel((60, 60)) in (255, (255, 255, 255))
    pixels = np.asarray(result)
    contour = np.asarray(polygon, dtype=np.float32)
    for y in range(120):
        for x in range(120):
            if cv2.pointPolygonTest(contour, (x, y), False) <= 0:
                assert not np.any(pixels[y, x])


def test_recovery_and_detail_masks_keep_adjacent_caption_pixels(tmp_path):
    from types import SimpleNamespace
    from offline_word.worker import _recover_missing_lines, _detail_variants
    from offline_word.accuracy import DetailTarget
    path = tmp_path / 'adjacent.png'
    source = Image.new('RGB', (400, 200), 'white')
    draw = ImageDraw.Draw(source)
    draw.rectangle((60, 60, 179, 99), fill='black')
    draw.rectangle((180, 60, 181, 99), fill=(210, 210, 210))
    source.save(path)
    polygon = [(60, 60), (180, 60), (180, 100), (60, 100)]
    page = PageResult('p', 0, 400, 200, str(path), nontext_regions=[polygon])
    def predict(crop_path):
        with Image.open(crop_path) as crop:
            # 200x80 crop with a 10px border; caption begins at (150,30).
            assert crop.getpixel((150, 40)) == (210, 210, 210)
            assert crop.getpixel((100, 40)) == (255, 255, 255)
        return SimpleNamespace(text='保留说明', confidence=.9)
    assert _recover_missing_lines(page, path, [(40, 40, 240, 120)],
                                  SimpleNamespace(predict_detail=predict), tmp_path/'recovery') == 1
    target = DetailTarget(0, '保留说明', (40, 40, 240, 120), .9)
    variants = _detail_variants(path, page_size=(400, 200), target=target,
                               session_dir=tmp_path/'detail', target_index=0, nontext_regions=[polygon])
    with Image.open(variants[0]) as crop:
        # Expanded bbox starts at (10,28), with a 16px border.
        assert crop.getpixel((186, 52)) == (210, 210, 210)


def test_unstructured_speckles_are_preserved_for_review(tmp_path):
    path = tmp_path / 'speckles.png'
    image = Image.new('L', (900, 600), 'white')
    draw = ImageDraw.Draw(image)
    rng = np.random.default_rng(37)
    for x, y, w, h in zip(rng.integers(150, 550, 180), rng.integers(220, 280, 180),
                         rng.integers(2, 7, 180), rng.integers(2, 9, 180)):
        draw.rectangle((int(x), int(y), int(x+w), int(y+h)), fill=0)
    image.save(path)
    page = page_from_plain_prediction(ImageJob('p', str(path), text_only=True), path,
                                     {'rec_texts': ['未确定内容'], 'rec_boxes': [[150, 220, 560, 295]], 'rec_scores': [.3]})
    assert not page.nontext_regions
    assert page.blocks[0].text == '未确定内容'
