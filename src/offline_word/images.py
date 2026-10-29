"""Import and conservative local image preprocessing."""

from __future__ import annotations

import math
import re
import uuid
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from .domain import ImageJob
from .security import OfflineError, assert_local_path
from .text_enhancement import enhance_faint_text


SUPPORTED_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
_MAX_PIXELS = 100_000_000
_MAX_SIDE = 16_000
_OCR_MAX_PIXELS = 12_000_000
_OCR_MAX_SIDE = 4_096


def _natural_key(path: Path) -> list[object]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", path.name)]


def _failed_job(path: Path, code: str) -> ImageJob:
    return ImageJob(id=uuid.uuid4().hex, source_path=str(path), status="failed", error_code=code)


def import_images(paths: list[str]) -> list[ImageJob]:
    """Create page jobs in natural filename order, expanding multi-frame TIFFs."""
    expanded: list[Path] = []
    jobs: list[ImageJob] = []
    for source in paths:
        try:
            path = assert_local_path(source, "remote_image_path").expanduser()
        except OfflineError:
            jobs.append(_failed_job(Path("remote-image"), "remote_image_path"))
            continue
        if path.is_dir():
            try:
                for item in path.iterdir():
                    try:
                        item = assert_local_path(item, "remote_image_path")
                    except OfflineError:
                        jobs.append(_failed_job(item, "remote_image_path"))
                        continue
                    if item.is_file():
                        expanded.append(item)
            except OSError:
                expanded.append(path)
        else:
            expanded.append(path)
    for path in sorted(expanded, key=_natural_key):
        if path.suffix.casefold() not in SUPPORTED_SUFFIXES:
            jobs.append(_failed_job(path, "unsupported_image"))
            continue
        try:
            with Image.open(path) as image:
                frame_count = int(getattr(image, "n_frames", 1))
                if frame_count < 1 or frame_count > 1_000:
                    raise ValueError
            for frame_index in range(frame_count):
                jobs.append(ImageJob(id=uuid.uuid4().hex, source_path=str(path), frame_index=frame_index))
        except (OSError, UnidentifiedImageError, ValueError, Image.DecompressionBombError):
            jobs.append(_failed_job(path, "invalid_image"))
    return jobs


def _load_oriented_rgb(job: ImageJob) -> Image.Image:
    try:
        with Image.open(assert_local_path(job.source_path, "remote_image_path")) as opened:
            if job.frame_index < 0 or job.frame_index >= int(getattr(opened, "n_frames", 1)):
                raise OfflineError("invalid_image")
            opened.seek(job.frame_index)
            image = ImageOps.exif_transpose(opened).convert("RGB")
    except OfflineError:
        raise
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        raise OfflineError("invalid_image") from None
    if image.width > _MAX_SIDE or image.height > _MAX_SIDE or image.width * image.height > _MAX_PIXELS:
        raise OfflineError("image_too_large")
    return image


def _crop_bounds(image: Image.Image, crop: tuple) -> tuple[int, int, int, int]:
    if len(crop) != 4 or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in crop):
        raise OfflineError("invalid_crop")
    left, top, right, bottom = crop
    left, top = max(0, math.floor(left)), max(0, math.floor(top))
    right, bottom = min(image.width, math.ceil(right)), min(image.height, math.ceil(bottom))
    if left >= right or top >= bottom:
        raise OfflineError("invalid_crop")
    return left, top, right, bottom


def _crop(image: Image.Image, crop: tuple) -> Image.Image:
    return image.crop(_crop_bounds(image, crop))


def _canonical_quad(points: list, width: int, height: int) -> np.ndarray:
    if len(points) != 4:
        raise OfflineError("invalid_perspective")
    try:
        quad = np.asarray(points, dtype=np.float32)
    except (TypeError, ValueError):
        raise OfflineError("invalid_perspective") from None
    if quad.shape != (4, 2) or not np.isfinite(quad).all() or (quad[:, 0] < 0).any() or (quad[:, 1] < 0).any():
        raise OfflineError("invalid_perspective")
    if (quad[:, 0] > width - 1).any() or (quad[:, 1] > height - 1).any():
        raise OfflineError("invalid_perspective")
    sums = quad.sum(axis=1)
    differences = np.diff(quad, axis=1).reshape(-1)
    ordered = np.array([quad[np.argmin(sums)], quad[np.argmin(differences)], quad[np.argmax(sums)], quad[np.argmax(differences)]], dtype=np.float32)
    if len({tuple(point) for point in ordered}) != 4:
        raise OfflineError("invalid_perspective")
    # Cross products use a fixed traversal and reject a bow-tie/non-convex quad.
    cross_products = []
    for index in range(4):
        first = ordered[(index + 1) % 4] - ordered[index]
        second = ordered[(index + 2) % 4] - ordered[(index + 1) % 4]
        cross_products.append(float(np.cross(first, second)))
    if min(cross_products) <= 0 <= max(cross_products):
        raise OfflineError("invalid_perspective")
    return ordered


def _perspective(image: Image.Image, points: list) -> Image.Image:
    try:
        import cv2
    except ImportError:
        raise OfflineError("preprocess_unavailable") from None
    quad = _canonical_quad(points, image.width, image.height)
    top = np.linalg.norm(quad[1] - quad[0])
    bottom = np.linalg.norm(quad[2] - quad[3])
    left = np.linalg.norm(quad[3] - quad[0])
    right = np.linalg.norm(quad[2] - quad[1])
    target_width, target_height = int(round(max(top, bottom))), int(round(max(left, right)))
    if target_width < 2 or target_height < 2 or target_width * target_height > _MAX_PIXELS:
        raise OfflineError("invalid_perspective")
    target = np.array([[0, 0], [target_width - 1, 0], [target_width - 1, target_height - 1], [0, target_height - 1]], dtype=np.float32)
    transformed = cv2.warpPerspective(np.asarray(image), cv2.getPerspectiveTransform(quad, target), (target_width, target_height), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    return Image.fromarray(transformed)


def _deskew_angle(image: Image.Image) -> float | None:
    try:
        import cv2
    except ImportError:
        return None
    grayscale = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2GRAY)
    # ``np.where`` returns (row=y, column=x); OpenCV geometry requires (x, y).
    points = np.column_stack(np.where(grayscale < 100))[:, ::-1]
    if len(points) < 100:
        return None
    angle = float(cv2.minAreaRect(points.astype(np.float32))[2])
    # OpenCV reports the short-side angle on one tilt direction.  Normalising
    # into [-45, 45] makes positive correction straighten either slope.
    if angle > 45:
        angle -= 90
    if abs(angle) > 7 or abs(angle) < 0.15:
        return None
    return angle


def _apply_deskew(image: Image.Image, angle: float | None) -> Image.Image:
    if angle is None:
        return image
    try:
        import cv2
    except ImportError:
        return image
    matrix = cv2.getRotationMatrix2D((image.width / 2, image.height / 2), angle, 1.0)
    transformed = cv2.warpAffine(np.asarray(image), matrix, (image.width, image.height), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    return Image.fromarray(transformed)


def _deskew(image: Image.Image) -> Image.Image:
    return _apply_deskew(image, _deskew_angle(image))


def _reduce_photo_shadows(image: Image.Image) -> Image.Image:
    try:
        import cv2
    except ImportError:
        return image
    source = np.asarray(image)
    gray = cv2.cvtColor(source, cv2.COLOR_RGB2GRAY)
    background = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (41, 41)))
    normalized = cv2.divide(gray, background, scale=255)
    return Image.fromarray(cv2.cvtColor(normalized, cv2.COLOR_GRAY2RGB))


def _fit_for_ocr(image: Image.Image) -> Image.Image:
    """Bound working-set memory while keeping a roughly 300-DPI A4 page."""
    side_scale = min(1.0, _OCR_MAX_SIDE / max(image.width, image.height))
    pixel_scale = min(1.0, math.sqrt(_OCR_MAX_PIXELS / (image.width * image.height)))
    scale = min(side_scale, pixel_scale)
    if scale >= 1.0:
        return image
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return image.resize(size, Image.Resampling.LANCZOS)


def _prepare_image_paths(job: ImageJob, session_dir: Path, *, keep_detail: bool) -> tuple[Path, Path]:
    image = _load_oriented_rgb(job)
    from .paper import PaperDetection, apply_paper_mask, detect_paper_quad, rotate_polygon
    job.processed_paper_polygon = None
    if job.mode == "photo" and job.crop is None and job.perspective is None and job.paper_status in {"not_run", "detecting"}:
        try:
            detected = detect_paper_quad(image)
        except Exception:
            # A failed optional suggestion must never prevent whole-image OCR.
            detected = PaperDetection(status="unavailable")
        job.paper_quad, job.paper_confidence, job.paper_status = detected.quad, detected.confidence, detected.status
    polygon = job.paper_quad if job.paper_status == "suggested" and job.crop is None and job.perspective is None else None
    perspective = job.perspective
    if job.crop is not None:
        left, top, right, bottom = _crop_bounds(image, job.crop)
        image = image.crop((left, top, right, bottom))
        if perspective is not None:
            try:
                perspective = [(float(point[0]) - left, float(point[1]) - top) for point in perspective]
            except (IndexError, TypeError, ValueError):
                raise OfflineError("invalid_perspective") from None
    if perspective is not None:
        if job.paper_status == "confirmed":
            image = apply_paper_mask(image, perspective)
        image = _perspective(image, perspective)
    if job.rotation % 90 != 0:
        raise OfflineError("invalid_rotation")
    if job.rotation % 360:
        if polygon:
            polygon = rotate_polygon(polygon, image.width, image.height, job.rotation)
        image = image.rotate(-(job.rotation % 360), expand=True)

    detail = image
    image = _fit_for_ocr(image)
    if polygon:
        polygon = [(x * image.width / detail.width, y * image.height / detail.height) for x, y in polygon]
    detail_is_distinct = keep_detail and detail.size != image.size
    if job.mode == "photo":
        image = _reduce_photo_shadows(image)
        if detail_is_distinct:
            detail = _reduce_photo_shadows(detail)
    image = enhance_faint_text(image)
    if detail_is_distinct:
        detail = enhance_faint_text(detail)
    angle = _deskew_angle(image)
    if polygon and angle is not None:
        import cv2
        matrix = cv2.getRotationMatrix2D((image.width / 2, image.height / 2), angle, 1.0)
        transformed_points = [matrix @ np.array([x, y, 1]) for x, y in polygon]
        polygon = [(float(point[0]), float(point[1])) for point in transformed_points]
    job.processed_paper_polygon = polygon
    image = _apply_deskew(image, angle)
    if detail_is_distinct:
        detail = _apply_deskew(detail, angle)
    if image.width > _MAX_SIDE or image.height > _MAX_SIDE or image.width * image.height > _MAX_PIXELS:
        raise OfflineError("image_too_large")
    output_dir = assert_local_path(session_dir, "remote_path") / "processed"
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    output = output_dir / f"{re.sub(r'[^a-zA-Z0-9_-]', '_', job.id) or uuid.uuid4().hex}.png"
    image.save(output, format="PNG", optimize=False)
    try:
        output.chmod(0o600)
    except OSError:
        pass
    if not detail_is_distinct:
        return output, output
    detail_output = output_dir / f"detail-{re.sub(r'[^a-zA-Z0-9_-]', '_', job.id) or uuid.uuid4().hex}.png"
    detail.save(detail_output, format="PNG", optimize=False)
    try:
        detail_output.chmod(0o600)
    except OSError:
        pass
    return output, detail_output


def prepare_image(job: ImageJob, session_dir: Path) -> Path:
    """Write an orientation-corrected, locally processed PNG; never modify source."""
    return _prepare_image_paths(job, session_dir, keep_detail=False)[0]


def prepare_image_with_detail(job: ImageJob, session_dir: Path) -> tuple[Path, Path]:
    """Return the bounded OCR page and an aligned higher-resolution copy when useful."""
    return _prepare_image_paths(job, session_dir, keep_detail=True)
