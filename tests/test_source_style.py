from pathlib import Path
import os
import pytest
from PIL import Image, ImageDraw, ImageFont
from offline_word.domain import Block, PageResult, TextLine


@pytest.mark.parametrize('wrapped', [False, True])
def test_print_style_uses_pixels_for_font_and_bold_without_rewriting_text(tmp_path, wrapped):
    from offline_word.source_style import annotate_source_style
    font_path = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'Fonts/simfang.ttf'
    if not font_path.is_file():
        pytest.skip('requires the locally installed Windows reference font')
    font = ImageFont.truetype(str(font_path), 48)
    prefix, suffix = '这是加粗引导句', '这里是正常的正文内容'
    text = prefix + suffix
    image = Image.new('RGB', (1200, 800), 'white')
    draw = ImageDraw.Draw(image)
    draw.text((100, 100), prefix, font=font, fill='black', stroke_width=1)
    if not wrapped:
        draw.text((100 + font.getlength(prefix), 100), suffix, font=font, fill='black')
    else:
        text = prefix
    draw.text((100, 180), suffix * 2, font=font, fill='black')
    first_box = draw.textbbox((100, 100), text, font=font)
    second_box = draw.textbbox((100, 180), suffix * 2, font=font)
    path = tmp_path / 'page.png'
    image.save(path)
    block = Block('text', 'text', (100, 100, 1100, 230), original_text=text + suffix * 2,
                  text_lines=[TextLine(text, first_box), TextLine(suffix * 2, second_box)])
    page = PageResult('p', 0, 1200, 800, str(path), [block])
    annotate_source_style(page)
    assert block.font_name == '仿宋'
    assert block.bold_ranges and block.bold_ranges[0][0] == 0
    assert block.bold_ranges[0][1] <= len(prefix) + 1
    assert block.original_text == text + suffix * 2
    assert block.edited_text is None


def test_missing_style_image_does_not_prevent_exportable_ocr():
    from offline_word.source_style import annotate_source_style
    block = Block('text', 'text', (10, 10, 90, 90), original_text='保留正文')
    page = PageResult('p', 0, 100, 100, 'missing.png', [block])
    annotate_source_style(page)
    assert block.original_text == '保留正文'
