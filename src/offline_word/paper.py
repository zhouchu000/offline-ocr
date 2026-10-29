"""Conservative paper suggestions. Detection never authorizes deleting text."""
from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np
from PIL import Image, ImageDraw, ImageFilter


@dataclass(frozen=True)
class PaperDetection:
    quad: list[tuple[float, float]] | None = None
    confidence: float = 0.0
    status: str = "not_found"


def detect_paper_quad(image: Image.Image) -> PaperDetection:
    """Suggest one complete sheet using bounded, multi-scale evidence.

    Coordinates refer to the EXIF-oriented source. Competing sheets, clipped
    borders and weak contrast cause abstention rather than choosing a page.
    """
    import cv2
    from .images import _canonical_quad

    candidates: list[tuple[float, np.ndarray]] = []
    for edge in (720, 1080):
        probe = image.convert("RGB")
        probe.thumbnail((edge, edge), Image.Resampling.LANCZOS)
        rgb = np.asarray(probe)
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        h, w = gray.shape
        if min(w, h) < 80:
            return PaperDetection()
        smooth = cv2.GaussianBlur(gray, (5, 5), 0)
        _, bright = cv2.threshold(smooth, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        masks = (cv2.Canny(smooth, 40, 130), bright,
                 cv2.adaptiveThreshold(smooth, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 9))
        for mask in masks:
            contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
            for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:40]:
                area = cv2.contourArea(contour)
                if not .12 <= area / (w * h) <= .96:
                    continue
                points = cv2.approxPolyDP(contour, .018 * cv2.arcLength(contour, True), True).reshape(-1, 2)
                if points.shape != (4, 2) or not cv2.isContourConvex(points):
                    continue
                try:
                    quad = _canonical_quad(points.tolist(), w, h)
                except Exception:
                    continue
                if any(x <= 2 or y <= 2 or x >= w - 3 or y >= h - 3 for x, y in quad):
                    continue  # A partial page needs a user's crop decision.
                lengths = [float(np.linalg.norm(quad[(i+1) % 4] - quad[i])) for i in range(4)]
                if min(lengths) < 30 or min(lengths[0], lengths[2]) / max(lengths[0], lengths[2]) < .55 or min(lengths[1], lengths[3]) / max(lengths[1], lengths[3]) < .55:
                    continue
                aspect = (lengths[0] + lengths[2]) / (lengths[1] + lengths[3])
                if not .42 <= aspect <= 2.4:
                    continue
                inside = np.zeros_like(gray)
                cv2.fillConvexPoly(inside, np.rint(quad).astype(np.int32), 255)
                kernel = np.ones((11, 11), np.uint8)
                inner = cv2.erode(inside, kernel) > 0
                ring = (cv2.dilate(inside, kernel) > 0) & (inside == 0)
                if not inner.any() or ring.sum() < 30:
                    continue
                paper_level = float(np.median(gray[inner]))
                contrast = paper_level - float(np.median(gray[ring]))
                if paper_level < 130 or contrast < 18:
                    continue
                ink = ((gray < min(150, paper_level - 45)) & inner).astype(np.uint8)
                count, _, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
                components = sum(3 <= stats[i, cv2.CC_STAT_WIDTH] <= w * .08
                                 and 5 <= stats[i, cv2.CC_STAT_HEIGHT] <= h * .08
                                 and stats[i, cv2.CC_STAT_AREA] >= 10 for i in range(1, count))
                if components < 6:
                    continue  # Blank objects/rectangular graphics are not paper evidence.
                score = min(.98, .65 + min(contrast / 300, .2) + min(components / 400, .13))
                scaled = quad * np.array([image.width / w, image.height / h], dtype=np.float32)
                # Merge inner/outer edge detections of the same sheet.
                if any(np.mean(np.linalg.norm(scaled - old, axis=1)) < max(image.size) * .025 for _, old in candidates):
                    continue
                candidates.append((score, scaled))
    if not candidates:
        return PaperDetection()
    candidates.sort(key=lambda value: value[0], reverse=True)
    score, best = candidates[0]
    best_area = cv2.contourArea(best)
    for _, other in candidates[1:]:
        overlap, _ = cv2.intersectConvexConvex(best, other)
        if overlap / min(best_area, cv2.contourArea(other)) < .75:
            return PaperDetection(status="ambiguous")
    if best_area / (image.width * image.height) < .24:
        return PaperDetection(status="ambiguous")
    # Expand outwards, never inwards: preserve printing close to a paper edge.
    center = best.mean(axis=0)
    distance = np.linalg.norm(best - center, axis=1)
    best = center + (best - center) * (1 + max(3, max(image.size) * .003) / distance[:, None])
    best[:, 0] = np.clip(best[:, 0], 0, image.width - 1)
    best[:, 1] = np.clip(best[:, 1], 0, image.height - 1)
    return PaperDetection([tuple(map(float, point)) for point in best], round(score, 3), "suggested")


def apply_paper_mask(image: Image.Image, quad: list, *, fill="white") -> Image.Image:
    """Mask only an explicitly accepted quad; keep a small outward safety edge."""
    from .images import _canonical_quad
    points = _canonical_quad(quad, image.width, image.height)
    mask = Image.new("L", image.size)
    ImageDraw.Draw(mask).polygon([tuple(point) for point in points], fill=255)
    mask = mask.filter(ImageFilter.MaxFilter(5))
    return Image.composite(image.convert("RGB"), Image.new("RGB", image.size, fill), mask)


def rotate_polygon(quad: list, width: int, height: int, clockwise: int) -> list[tuple[float, float]]:
    result = list(quad)
    for _ in range((clockwise % 360) // 90):
        result = [(height - 1 - y, x) for x, y in result]
        width, height = height, width
    return result


def review_outside_paper(page) -> int:
    """Flag uncertain geometry, including crossings, without removing any block."""
    if not page.paper_polygon or page.paper_status != "suggested":
        return 0
    import cv2
    polygon = np.asarray(page.paper_polygon, dtype=np.float32)
    if polygon.shape != (4, 2) or not np.isfinite(polygon).all():
        return 0
    flagged = 0
    for block in page.blocks:
        left, top, right, bottom = block.bbox
        area = (right - left) * (bottom - top)
        if area <= 0 or not math.isfinite(area):
            continue
        rectangle = np.array([(left, top), (right, top), (right, bottom), (left, bottom)], np.float32)
        overlap, _ = cv2.intersectConvexConvex(polygon, rectangle)
        if overlap / area < .98:
            block.needs_review = True
            if "paper_edge" not in block.review_tags:
                block.review_tags.append("paper_edge")
            flagged += 1
    page.paper_review_count = flagged
    return flagged
