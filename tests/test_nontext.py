import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw

from offline_word.domain import ImageJob
from offline_word.worker import page_from_plain_prediction, page_from_prediction, _missing_line_regions


@pytest.fixture
def coded_page(tmp_path):
    image = Image.new('RGB', (900, 1100), 'white')
    code = cv2.QRCodeEncoder_create().encode('OFFLINE-PUBLIC-TEST-036')
    image.paste(Image.fromarray(code).resize((180, 180), Image.Resampling.NEAREST), (520, 420))
    ImageDraw.Draw(image).rectangle((100, 205, 450, 222), fill='black')
    path = tmp_path / 'page.png'
    image.save(path)
    return path


def _prediction():
    return {'rec_texts': ['正常文字 128.50', '回田口'], 'rec_scores': [.60, .99],
            'rec_boxes': [[100, 200, 450, 230], [535, 455, 680, 475]]}


@pytest.mark.parametrize('mode', ['plain', 'structure'])
def test_qr_high_confidence_gibberish_removed_but_real_low_confidence_text_retained(coded_page, mode):
    job = ImageJob('p', str(coded_page), text_only=mode == 'plain')
    prediction = _prediction()
    if mode == 'plain':
        page = page_from_plain_prediction(job, coded_page, prediction)
    else:
        page = page_from_prediction(job, coded_page, {'overall_ocr_res': prediction, 'parsing_res_list': [
            {'block_label': 'text', 'block_bbox': [100, 200, 700, 600], 'block_content': '正常文字 128.50\n回田口'}]})
    assert '回田口' not in ''.join(b.original_text for b in page.blocks)
    assert '正常文字 128.50' in ''.join(b.original_text for b in page.blocks)
    assert any(b.kind == 'image' for b in page.blocks) == (mode == 'structure')


def test_zigzag_decoration_is_not_a_word_or_recovered_line(tmp_path):
    image = Image.new('RGB', (1200, 1600), 'white')
    draw = ImageDraw.Draw(image)
    for x in range(140, 950, 26):
        draw.line([(x, 1040), (x+13, 1070), (x+26, 1040)], fill='black', width=4)
    path = tmp_path / 'decoration.png'
    image.save(path)
    job = ImageJob('p', str(path), text_only=True)
    page = page_from_plain_prediction(job, path, {
        'rec_texts': ['w'], 'rec_boxes': [[139, 1034, 972, 1075]], 'rec_scores': [.646677]})
    assert not page.blocks
    assert not _missing_line_regions(page, path)


def test_unknown_symbols_without_graphic_evidence_are_preserved(tmp_path):
    path = tmp_path / 'unknown.png'
    Image.new('RGB', (900, 1100), 'white').save(path)
    page = page_from_plain_prediction(ImageJob('p', str(path), text_only=True), path, {
        'rec_texts': ['x', '11111111', '∑ α + β = 12'], 'rec_scores': [.30, .30, .60],
        'rec_boxes': [[100, 100, 110, 120], [100, 200, 260, 230], [100, 300, 320, 335]]})
    assert [b.text for b in page.blocks] == ['x', '11111111', '∑ α + β = 12']


@pytest.mark.parametrize('angle', [0, 12, 90])
def test_rotated_qr_is_detected_without_decoding_or_touching_source(tmp_path, angle):
    import hashlib
    from offline_word.nontext import NonTextImage, covered_by_nontext
    code = cv2.QRCodeEncoder_create().encode('NOT-A-NETWORK-REQUEST')
    image = Image.fromarray(code).resize((180, 180), Image.Resampling.NEAREST)
    image = image.rotate(angle, expand=True, fillcolor=255)
    page = Image.new('L', (700, 800), 'white')
    page.paste(image, (200, 300))
    path = tmp_path / 'code.png'
    page.save(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    analysis = NonTextImage.read(path)
    assert len(analysis.polygons) == 1
    assert covered_by_nontext((280, 380, 300, 400), analysis.polygons)
    assert not covered_by_nontext((200, 540, 400, 570), analysis.polygons)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_barcode_bars_filtered_while_number_caption_survives(tmp_path):
    path = tmp_path / 'bars.png'
    image = Image.new('RGB', (900, 1100), 'white')
    draw = ImageDraw.Draw(image)
    for x in range(150, 550, 7):
        draw.rectangle((x, 450, x+(2 if x % 2 else 4), 505), fill='black')
    image.save(path)
    page = page_from_plain_prediction(ImageJob('p', str(path), text_only=True), path, {
        'rec_texts': ['川川山川', '12345678'], 'rec_scores': [.97, .65],
        'rec_boxes': [[150, 455, 540, 500], [160, 525, 450, 550]]})
    assert [b.text for b in page.blocks] == ['12345678']


def test_complete_filter_does_not_reintroduce_rejected_raw_fallback(coded_page):
    page = page_from_plain_prediction(ImageJob('p', str(coded_page), text_only=True), coded_page, {
        'rec_texts': ['回田口'], 'rec_scores': [.99], 'rec_boxes': [[535, 455, 680, 475]]})
    assert not page.blocks


def test_code_between_two_text_fragments_does_not_hide_recovery(coded_page):
    image = Image.open(coded_page).convert('RGB')
    draw = ImageDraw.Draw(image)
    for x in list(range(100, 470, 28)) + list(range(720, 830, 28)):
        draw.rectangle((x, 450, x+18, 470), fill='black')
    image.save(coded_page)
    page = page_from_plain_prediction(ImageJob('p', str(coded_page)), coded_page, {'rec_texts': []})
    assert page.nontext_regions
    assert any(box[1] <= 460 <= box[3] for box in _missing_line_regions(page, coded_page))


def test_rotated_code_export_crop_cannot_duplicate_corner_text(tmp_path):
    from offline_word.domain import Block, PageResult
    from offline_word.worker import _save_figure_crops
    path = tmp_path / 'source.png'
    Image.new('RGB', (200, 200), 'black').save(path)
    polygon = [(100, 20), (180, 100), (100, 180), (20, 100)]
    block = Block('p:nontext-0', 'image', (20, 20, 180, 180))
    page = PageResult('p', 0, 200, 200, str(path), [block], nontext_regions=[polygon])
    _save_figure_crops(page, tmp_path / 'session')
    with Image.open(block.image_path) as crop:
        assert crop.getpixel((0, 0))[3] == 0
        assert crop.getpixel((80, 80))[3] == 255

@pytest.mark.parametrize('kind', ['text', 'table'])
def test_code_dominating_a_layout_block_cannot_erase_its_outside_caption(coded_page, kind):
    from offline_word.domain import Block, Cell, PageResult, TextLine
    from offline_word.nontext import filter_nontext, covered_by_nontext, NonTextImage
    box = (532, 432, 688, 614)
    bad_box = (535, 455, 680, 475)
    caption_box = (535, 602, 682, 614)
    assert covered_by_nontext(box, NonTextImage.read(coded_page).polygons, threshold=.72)
    if kind == 'text':
        block = Block('combined', kind, box, '回田口\n说明文字', text_lines=[TextLine('回田口', bad_box), TextLine('说明文字', caption_box)])
    else:
        block = Block('combined', kind, box, rows=1, cols=2, cells=[Cell(0, 0, '回田口', bbox=bad_box), Cell(0, 1, '说明文字', bbox=caption_box)])
    result = filter_nontext(PageResult('p',0,900,1100,str(coded_page),[block]))
    text = ''.join(b.text + ''.join(c.text for c in b.cells) for b in result.blocks)
    assert '说明文字' in text
    assert '回田口' not in text
