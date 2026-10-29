"""Conservative print-style evidence from local fonts and recognised line pixels.

No model downloads, text substitutions or font redistribution. Font matching is
optional: absent fonts or uncertain geometry leave the editable OCR untouched.
"""
from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
import statistics
import unicodedata

from PIL import Image, ImageDraw, ImageFont

from .domain import PageResult

_FONTS = {'宋体': 'simsun.ttc', '仿宋': 'simfang.ttf', '楷体': 'simkai.ttf', '黑体': 'simhei.ttf'}


def local_font_path(name: str) -> Path | None:
    filename = _FONTS.get(name)
    if not filename:
        return None
    path = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'Fonts' / filename
    return path if path.is_file() else None


def _mask(image):
    import cv2
    import numpy as np
    pixels = np.asarray(image.convert('L'))
    _, ink = cv2.threshold(pixels, 0, 1, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    ys, xs = np.where(ink)
    if not len(xs):
        return None
    return ink[ys.min():ys.max()+1, xs.min():xs.max()+1]


@lru_cache(maxsize=2048)
def _template(name: str, character: str):
    import cv2
    path = local_font_path(name)
    if path is None:
        return None
    font = ImageFont.truetype(str(path), 72)
    canvas = Image.new('L', (100, 130), 255)
    ImageDraw.Draw(canvas).text((3, 3), character, font=font, fill=0)
    ink = _mask(canvas)
    if ink is None:
        return None
    ink = cv2.resize(ink, (40, 40), interpolation=cv2.INTER_NEAREST)
    return ink, cv2.distanceTransform(1-ink, cv2.DIST_L2, 3)


def _glyphs(source, line):
    """Refine character advance estimates at low-ink column boundaries."""
    import cv2
    import numpy as np
    box = tuple(round(v) for v in line.bbox)
    if box[2] <= box[0] or box[3] <= box[1]:
        return
    ink = _mask(source.crop(box))
    if ink is None:
        return
    units = [1 if unicodedata.east_asian_width(c) in {'W', 'F'} else .5 for c in line.text]
    step = ink.shape[1] / max(1, sum(units))
    if step < 6:
        return
    columns = ink.sum(axis=0)
    position = 0.0
    for index, (character, unit) in enumerate(zip(line.text, units)):
        estimates = position*step, (position+unit)*step
        position += unit
        if not '\u4e00' <= character <= '\u9fff':
            continue
        boundaries = []
        for estimate in estimates:
            lo = max(0, round(estimate-step*.22))
            hi = min(ink.shape[1], round(estimate+step*.22)+1)
            if lo >= hi:
                boundaries.append(min(ink.shape[1], max(0, round(estimate))))
                continue
            choices = np.flatnonzero(columns[lo:hi] == columns[lo:hi].min()) + lo
            boundaries.append(int(choices[np.argmin(abs(choices-estimate))]))
        crop = ink[:, boundaries[0]:boundaries[1]]
        ys, xs = np.where(crop)
        if not len(xs):
            continue
        crop = crop[ys.min():ys.max()+1, xs.min():xs.max()+1]
        if crop.shape[1] < step*.45 or crop.shape[0] < ink.shape[0]*.5:
            continue
        yield index, character, cv2.resize(crop, (40, 40), interpolation=cv2.INTER_NEAREST)


def _distance(ink, template):
    import cv2
    target, target_distance = template
    distance = cv2.distanceTransform(1-ink, cv2.DIST_L2, 3)
    return float(distance[target > 0].mean() + target_distance[ink > 0].mean())


def annotate_source_style(page: PageResult) -> None:
    """Attach bounded visual hints; errors never invalidate a recognised page."""
    available = [name for name in _FONTS if local_font_path(name)]
    if not available:
        return
    try:
        with Image.open(page.processed_image_path) as image:
            _annotate(page, image.convert('L'), available)
    except Exception:
        # Appearance is optional, including OpenCV/font back-end failures.
        # A valid OCR result must remain available for review and export.
        return


def _annotate(page, source, available):
    import cv2
    import numpy as np
    for block in page.blocks:
        for line in block.text_lines:
            left, top, right, bottom = (round(v) for v in line.bbox)
            if right <= left or bottom <= top:
                continue
            pixels = np.asarray(source.crop((left, top, right, bottom)))
            _, ink = cv2.threshold(pixels, 0, 1, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
            ys, xs = np.where(ink)
            if len(xs):
                line.ink_bbox = (float(left+xs.min()), float(top+ys.min()), float(left+xs.max()+1), float(top+ys.max()+1))
    evidence = {}
    votes = []
    for block in page.blocks:
        if block.kind not in {'text', 'title', 'table'} or not block.text_lines:
            continue
        # At most four representative rows and 48 glyphs per block. Avoid
        # running another OCR pass merely to infer appearance.
        glyphs = []
        for line in block.text_lines[:4]:
            glyphs.extend((line, index, char, ink) for index, char, ink in _glyphs(source, line))
            if len(glyphs) >= 48:
                break
        glyphs = glyphs[:48]
        scores = {}
        for name in available:
            values = [_distance(ink, _template(name, char)) for _, _, char, ink in glyphs[::3]
                      if _template(name, char) is not None]
            if len(values) >= 3:
                scores[name] = statistics.median(values)
        ranked = sorted(scores, key=scores.get)
        limit = 2.2 if block.kind == 'title' else 1.65
        if ranked and scores[ranked[0]] < limit and (len(ranked) == 1 or scores[ranked[1]] > scores[ranked[0]] * 1.09):
            block.font_name = ranked[0]
            if block.kind == 'text':
                votes.append(block.font_name)
        evidence[block.id] = glyphs
    body_font = statistics.mode(votes) if votes else None
    ratios = []
    block_ratios = {}
    for block in page.blocks:
        if block.font_name is None and block.kind == 'text':
            block.font_name = body_font
        if not block.font_name:
            continue
        readings = []
        for line, index, char, ink in evidence.get(block.id, []):
            template = _template(block.font_name, char)
            if template is not None and _distance(ink, template) < 2.0:
                ratio = float(ink.sum() / max(1, template[0].sum()))
                readings.append((line, index, ratio))
                if block.kind == 'text':
                    ratios.append(ratio)
        block_ratios[block.id] = readings
    # Lead-in sampling contains bold text by design. The lower quartile is a
    # more stable regular-weight reference than the contaminated median.
    regular = sorted(ratios)[len(ratios)//4] if ratios else 1.0
    for block in page.blocks:
        readings = block_ratios.get(block.id, [])
        if block.kind == 'title':
            block.font_bold = not readings or statistics.median(v for _, _, v in readings) > regular * 1.10
        elif readings and block.text_lines:
            # Only restore a contiguous visually heavier lead-in, not isolated
            # noisy characters or a guessed semantic heading.
            head_lines = block.text_lines[:2]
            head = ''.join(line.text for line in head_lines)
            values = {}
            offset = 0
            for head_line in head_lines:
                values.update({offset+i: v for line, i, v in readings if line is head_line})
                offset += len(head_line.text)
            candidates = []
            for end in range(3, len(head)-2):
                before = [v for i,v in values.items() if i < end]
                after = [v for i,v in values.items() if i >= end]
                if len(before) < 3 or len(after) < 3:
                    continue
                heavy, light = statistics.median(before), statistics.median(after)
                if heavy > light*1.23 and heavy > regular*1.16 and light < regular*1.3:
                    cost = sum(abs(v-heavy) for v in before) + sum(abs(v-light) for v in after)
                    candidates.append((cost, end))
            if candidates:
                _, end = min(candidates)
                # Include punctuation attached to the visually bold lead-in.
                while end < len(head) and head[end] in '。，：；、':
                    end += 1
                if end >= 3:
                    first_key = ''.join(head[:end].split())
                    start = next((i for i,c in enumerate(block.original_text) if not c.isspace()), 0)
                    for stop in range(start+1, len(block.original_text)+1):
                        if ''.join(block.original_text[start:stop].split()) == first_key:
                            block.bold_ranges = [(start, stop)]
                            break
