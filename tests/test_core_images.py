from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from offline_word.domain import ImageJob
from offline_word.images import import_images, prepare_image, prepare_image_with_detail


def test_perspective_preserves_corner_orientation_and_reading_direction():
    from offline_word.images import _perspective
    image = Image.new('RGB', (100, 60), 'white')
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
    from PIL import ImageDraw
    draw = ImageDraw.Draw(image)
    for box, color in zip([(0, 0, 49, 29), (50, 0, 99, 29), (0, 30, 49, 59), (50, 30, 99, 59)], colors):
        draw.rectangle(box, fill=color)
    result = _perspective(image, [(0, 0), (99, 0), (99, 59), (0, 59)])
    assert result.width > result.height
    assert [result.getpixel(p) for p in [(10, 10), (85, 10), (10, 45), (85, 45)]] == colors


def test_import_images_naturally_sorts_and_expands_tiff_frames(tmp_path: Path):
    for name in ("page10.png", "page2.png"):
        Image.new("RGB", (8, 6), "white").save(tmp_path / name)
    frame1 = Image.new("RGB", (5, 4), "red")
    frame2 = Image.new("RGB", (5, 4), "blue")
    frame1.save(tmp_path / "scan.tiff", save_all=True, append_images=[frame2])

    jobs = import_images([str(tmp_path / "page10.png"), str(tmp_path / "scan.tiff"), str(tmp_path / "page2.png")])

    assert [(Path(job.source_path).name, job.frame_index, job.status) for job in jobs] == [
        ("page2.png", 0, "pending"),
        ("page10.png", 0, "pending"),
        ("scan.tiff", 0, "pending"),
        ("scan.tiff", 1, "pending"),
    ]


def test_import_images_turns_unsupported_source_into_safe_failed_job(tmp_path: Path):
    unsupported = tmp_path / "notes.txt"
    unsupported.write_text("private details", encoding="utf-8")

    [job] = import_images([str(unsupported)])

    assert job.status == "failed"
    assert job.error_code == "unsupported_image"
    assert "private" not in (job.error_code or "")


def test_import_images_turns_remote_unc_source_into_safe_failed_job():
    [job] = import_images([r"\\server\share\scan.png"])

    assert job.status == "failed"
    assert job.error_code == "remote_image_path"


def test_import_images_validates_each_directory_entry_before_opening(tmp_path: Path):
    Image.new("RGB", (8, 6), "white").save(tmp_path / "local.png")
    external = tmp_path / "external.png"
    Image.new("RGB", (8, 6), "white").save(external)
    (tmp_path / "linked.png").symlink_to(external)

    jobs = import_images([str(tmp_path)])

    assert any(job.status == "failed" and job.error_code == "remote_image_path" for job in jobs)
    assert any(Path(job.source_path).name == "local.png" and job.status == "pending" for job in jobs)


def test_prepare_image_applies_original_coordinate_crop_then_clockwise_rotation(tmp_path: Path):
    source = tmp_path / "source.png"
    image = Image.new("RGB", (4, 3), "white")
    image.putpixel((1, 0), (255, 0, 0))
    image.putpixel((2, 1), (0, 0, 255))
    image.save(source)
    job = ImageJob(id="job", source_path=str(source), crop=(1, 0, 3, 2), rotation=90)

    processed = prepare_image(job, tmp_path / "session")

    with Image.open(processed) as result:
        assert result.size == (2, 2)
        assert result.getpixel((1, 0)) == (255, 0, 0)
        assert result.getpixel((0, 1)) == (0, 0, 255)


def test_prepare_image_translates_original_perspective_points_after_crop(tmp_path: Path):
    source = tmp_path / "source.png"
    image = Image.new("RGB", (100, 60), "white")
    from PIL import ImageDraw
    draw = ImageDraw.Draw(image)
    for box, color in zip(
        [(30, 20, 49, 29), (50, 20, 69, 29), (30, 30, 49, 39), (50, 30, 69, 39)],
        [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)],
    ):
        draw.rectangle(box, fill=color)
    image.save(source)
    job = ImageJob(
        id="composed", source_path=str(source), crop=(20, 10, 80, 50),
        perspective=[(30, 20), (70, 20), (70, 40), (30, 40)], rotation=90,
    )

    processed = prepare_image(job, tmp_path / "session")

    with Image.open(processed) as result:
        assert result.size == (20, 40)
        assert [result.getpixel(point) for point in [(2, 2), (17, 2), (2, 37), (17, 37)]] == [
            (0, 0, 255), (255, 0, 0), (255, 255, 0), (0, 255, 0),
        ]


def test_prepare_image_downscales_large_phone_photo_before_ocr(tmp_path: Path):
    source = tmp_path / "phone.jpg"
    Image.new("RGB", (5000, 4000), "white").save(source, quality=85)

    processed = prepare_image(ImageJob(id="phone", source_path=str(source)), tmp_path / "session")

    with Image.open(processed) as result:
        assert max(result.size) <= 4096
        assert result.width * result.height <= 12_000_000
        assert result.width / result.height == pytest.approx(1.25, rel=0.01)


def test_prepare_image_with_detail_retains_transformed_original_resolution(
    tmp_path: Path,
    monkeypatch,
):
    """Would fail if detail OCR can only reuse the already downscaled page."""
    from offline_word import images

    source = tmp_path / "source.png"
    image = Image.new("RGB", (12, 6), "white")
    image.putpixel((2, 1), (255, 0, 0))
    image.putpixel((9, 4), (0, 0, 255))
    image.save(source)
    source_bytes = source.read_bytes()
    monkeypatch.setattr(images, "_OCR_MAX_SIDE", 6)
    monkeypatch.setattr(images, "_OCR_MAX_PIXELS", 36)
    job = ImageJob(
        id="detail",
        source_path=str(source),
        crop=(2, 1, 10, 5),
        rotation=90,
        auto_proofread=True,
    )

    processed, detail = prepare_image_with_detail(job, tmp_path / "session")

    with Image.open(processed) as processed_image, Image.open(detail) as detail_image:
        assert processed_image.size == (3, 6)
        assert detail_image.size == (4, 8)
        assert detail_image.getpixel((3, 0)) == (255, 0, 0)
        assert detail_image.getpixel((0, 7)) == (0, 0, 255)
    assert processed != detail
    assert source.read_bytes() == source_bytes


def test_prepare_image_with_detail_reuses_processed_file_when_no_downscale_is_needed(tmp_path: Path):
    """Would fail if ordinary pages create a duplicate full-resolution image."""
    source = tmp_path / "source.png"
    Image.new("RGB", (40, 30), "white").save(source)

    processed, detail = prepare_image_with_detail(
        ImageJob(id="small", source_path=str(source), auto_proofread=True),
        tmp_path / "session",
    )

    assert detail == processed


@pytest.mark.parametrize("tilt", (-3, 3))
def test_deskew_straightens_small_tilt_in_both_directions(tilt: int):
    cv2 = pytest.importorskip("cv2")
    from PIL import ImageDraw
    from offline_word.images import _deskew

    image = Image.new("RGB", (800, 400), "white")
    draw = ImageDraw.Draw(image)
    for y in (100, 200, 300):
        draw.rectangle((100, y - 3, 700, y + 3), fill="black")
    tilted = image.rotate(tilt, resample=Image.Resampling.BICUBIC, fillcolor="white")

    deskewed = _deskew(tilted)
    gray = cv2.cvtColor(np.asarray(deskewed), cv2.COLOR_RGB2GRAY)
    points_xy = np.column_stack(np.where(gray < 100))[:, ::-1].astype(np.float32)
    angle = cv2.minAreaRect(points_xy)[2]
    horizontal_angle = angle - 90 if angle > 45 else angle

    assert abs(horizontal_angle) < 0.6
