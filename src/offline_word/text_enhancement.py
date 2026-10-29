"""Conservative, geometry-preserving enhancement of faint printed text."""
from __future__ import annotations

import numpy as np
from PIL import Image


def enhance_faint_text(image: Image.Image) -> Image.Image:
    """Expand weak strokes only on bright, low-contrast document-like images.

    Clear pages, photographs, flat backgrounds and isolated noise pass through.
    No thresholding, erosion, cropping or generated strokes enter the OCR image.
    Analysis is bounded; a shared lookup table preserves every pixel coordinate.
    """
    if min(image.size) < 32:
        return image
    scale = min(1.0, 1600 / max(image.size))
    sample = image if scale == 1 else image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.Resampling.LANCZOS,
    )
    rgb = np.asarray(sample.convert('RGB'))
    gray = np.asarray(sample.convert('L'))
    low, high = np.percentile(gray, (.5, 99.5))
    span = float(high - low)
    if not 5 <= span <= 110 or high < 180 or float(np.mean(gray)) < 150:
        return image
    # Do not reinterpret coloured figures or photographs as faded paper.
    chroma = rgb.max(axis=2).astype(np.int16) - rgb.min(axis=2)
    if float(np.percentile(chroma, 90)) > 24:
        return image
    try:
        import cv2
    except ImportError:
        return image
    foreground = (gray < high - max(4.0, span * .45)).astype(np.uint8)
    count, _, stats, centers = cv2.connectedComponentsWithStats(foreground, 8)
    characters = []
    for index in range(1, count):
        _, _, width, height, area = stats[index]
        if (3 <= height <= min(100, gray.shape[0] * .20)
                and 2 <= width <= height * 4 and 5 <= area <= width * height * .94):
            characters.append((float(centers[index][1]), int(height)))
    if len(characters) < 4:
        return image
    # At least four character-sized components must form a plausible text row.
    # A bounded histogram avoids a quadratic scan on noisy page backgrounds.
    rows = np.zeros(gray.shape[0], dtype=np.int32)
    for center_y, height in characters:
        y = round(center_y)
        radius = max(1, round(height * .25))
        rows[max(0, y - radius):min(len(rows), y + radius + 1)] += 1
    if int(rows.max(initial=0)) < 4:
        return image
    gain = min(16.0, 210.0 / span)
    lut = np.clip(255.0 - (high - np.arange(256)) * gain, 0, 255).astype('uint8')
    return image.convert('RGB').point(lut.tolist() * 3)
