from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
import pytest

from offline_word.domain import ImageJob
from offline_word.images import prepare_image
from offline_word.text_enhancement import enhance_faint_text


def page(*, ink=243, background=255):
    image = Image.new('RGB', (580, 220), (background,) * 3)
    font = ImageFont.truetype(str(Path(__file__).resolve().parents[1] / 'src/offline_word/assets/MiSans-Regular.ttf'), 22)
    draw = ImageDraw.Draw(image)
    for index, text in enumerate(('请核对附件及原始编号。', '金额1,024.50元；编号AB-2026。', '保留标点、细字及相邻文字。')):
        draw.text((20, 20 + 56 * index), text, font=font, fill=(ink,) * 3)
    return image


def test_faint_text_gains_contrast_without_moving_or_erasing_ink():
    original = page()
    before = np.asarray(original).copy()
    after = np.asarray(enhance_faint_text(original))
    assert after.shape == before.shape
    assert np.array_equal(np.asarray(original), before)
    ink = before[:, :, 0] < 255
    assert np.all(after[ink] <= before[ink])
    assert int(after[ink].min()) < 100
    assert np.array_equal(after[~ink], before[~ink])


@pytest.mark.parametrize('image', [
    Image.new('RGB', (300, 200), 'white'),
    Image.new('RGB', (300, 200), (240, 240, 240)),
    Image.fromarray(np.random.default_rng(18).integers(240, 256, (200, 300, 3), dtype=np.uint8)),
    Image.fromarray(np.random.default_rng(19).integers(240, 256, (200, 300), dtype=np.uint8)).convert('RGB'),
    Image.fromarray(np.clip(np.random.default_rng(20).normal(247, 2, (200, 300)), 0, 255).astype('uint8')).convert('RGB'),
])
def test_flat_background_and_noise_do_not_become_text(image):
    assert np.array_equal(np.asarray(enhance_faint_text(image)), np.asarray(image))


def test_clear_pages_and_dark_pages_are_unchanged():
    for image in (page(ink=25), page(ink=80, background=120)):
        assert enhance_faint_text(image) is image


def test_enhancement_is_used_by_preparation_and_source_stays_unchanged(tmp_path):
    original = tmp_path / 'source.png'
    page().save(original)
    saved = original.read_bytes()
    result = prepare_image(ImageJob('faint', str(original), mode='scan'), tmp_path / 'session')
    assert original.read_bytes() == saved
    with Image.open(result) as processed:
        assert processed.size == (580, 220)
        assert np.asarray(processed).min() < 100
