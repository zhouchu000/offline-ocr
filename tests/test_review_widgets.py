from PySide6.QtGui import QColor, QImage, QTextDocument
from offline_word.review_widgets import SourceCrop, difference_html
from offline_word.theme import DARK


def test_crop_maps_processed_coordinates_and_invalid_regions_clear(qtbot, tmp_path):
    image = QImage(200, 100, QImage.Format.Format_RGB32)
    image.fill(QColor('blue'))
    path = tmp_path/'public.png'
    assert image.save(str(path))
    before = path.read_bytes()
    crop = SourceCrop()
    qtbot.addWidget(crop)
    crop.set_source(path, (50, 0, 100, 50), (100, 50))
    assert crop.sceneRect().width() == 106  # x = 100 with six pixels of context.
    assert crop.sceneRect().height() == 100
    assert crop.scene().items()[0].pixmap().toImage().pixelColor(50, 50) == QColor('blue')
    for box in ((4, 4, 4, 5), (10, 0, 0, 5), (float('nan'), 0, 5, 5), (300, 300, 400, 400)):
        crop.set_source(path, box, (100, 50))
        assert crop.sceneRect().isEmpty()
    assert path.read_bytes() == before


def test_crop_refreshes_replaced_file_and_does_not_keep_previous_page(qtbot, tmp_path):
    import os
    path = tmp_path/'public.png'
    image = QImage(50, 50, QImage.Format.Format_RGB32)
    image.fill(QColor('blue'))
    assert image.save(str(path))
    crop = SourceCrop()
    qtbot.addWidget(crop)
    crop.set_source(path, (0, 0, 50, 50), (50, 50))
    previous = path.stat().st_mtime_ns
    image.fill(QColor('red'))
    assert image.save(str(path))
    os.utime(path, ns=(previous + 1_000_000_000, previous + 1_000_000_000))
    crop.set_source(path, (0, 0, 50, 50), (50, 50))
    assert crop.scene().items()[0].pixmap().toImage().pixelColor(20, 20) == QColor('red')
    crop.set_source(None, None, (0, 0))
    assert crop._image.isNull() and crop.sceneRect().isEmpty()


def test_long_diff_is_bounded_and_html_remains_plain_text():
    text = '<b>长段落</b>' * 2000
    result = difference_html('长段落' * 6000, text, DARK)
    assert len(result) < 20000 and '完整对照' in result
    doc = QTextDocument()
    doc.setHtml(result)
    assert doc.toPlainText().startswith('<b>长段落</b>')
