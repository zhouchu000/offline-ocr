import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage
from offline_word.photo_canvas import PhotoCanvas


@pytest.mark.parametrize('rotation', [0, 90, 180, 270])
def test_rotated_canvas_round_trips_source_coordinates_and_selections(qtbot, tmp_path, rotation):
    source = tmp_path/'page.png'
    image = QImage(600, 800, QImage.Format.Format_RGB32)
    image.fill(QColor('white'))
    image.save(str(source))
    canvas = PhotoCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(700, 540)
    canvas.show()
    canvas.set_image(source)
    canvas.set_adjustments(rotation=rotation)
    qtbot.wait(20)
    point = QPointF(120, 250)
    mapped = canvas.image_to_display(point)
    restored = canvas.display_to_image(mapped)
    assert abs(restored.x()-point.x()) < 2 and abs(restored.y()-point.y()) < 2
    events = []
    canvas.selection_changed.connect(events.append)
    canvas.set_interaction_mode('crop')
    end = canvas.image_to_display(QPointF(400, 650))
    qtbot.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, pos=mapped.toPoint())
    qtbot.mouseMove(canvas.viewport(), end.toPoint())
    qtbot.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, pos=end.toPoint())
    assert len(events) == 1
    assert all(abs(a-b) < 3 for a,b in zip(events[0], (120,250,400,650)))


def test_zoom_and_pan_reveal_the_requested_image_region(qtbot, tmp_path):
    source = tmp_path/'page.png'
    image = QImage(2000, 3000, QImage.Format.Format_RGB32)
    image.fill(QColor('white'))
    image.save(str(source))
    canvas = PhotoCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(600, 500)
    canvas.show()
    canvas.set_image(source)
    for _ in range(6): canvas.zoom_in()
    canvas.reveal_bbox((100, 2700, 500, 2950))
    center = canvas.image_to_display(QPointF(300, 2825))
    assert canvas.viewport().rect().contains(center.toPoint())
