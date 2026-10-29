"""Conservative pixel evidence for codes and ornaments mistaken for text.

No code is decoded, opened or uploaded. The image used by preview/export is
never changed. Ambiguous content is retained rather than filtered by score.
"""

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from .domain import Block, FilteredText, PageResult, TextLine
from .line_geometry import complete_line_geometry
from .proofread import comparison_key


def rectangle(box):
    left, top, right, bottom = box
    return [(left, top), (right, top), (right, bottom), (left, bottom)]


def bounds(polygon):
    return (min(p[0] for p in polygon), min(p[1] for p in polygon),
            max(p[0] for p in polygon), max(p[1] for p in polygon))


def covered_by_nontext(box, polygons, *, threshold=1-1e-6):
    """Text must be fully contained; partial overlap may include real words.

    Explicit lower thresholds are only for non-destructive ownership/overlap
    queries. The tiny default tolerance covers floating-point intersection error.
    """
    import cv2
    import numpy as np
    if box is None or box[2] <= box[0] or box[3] <= box[1]:
        return False
    area = (box[2]-box[0]) * (box[3]-box[1])
    candidate = np.array(rectangle(box), dtype=np.float32)
    for polygon in polygons:
        boundary = bounds(polygon)
        if min(box[2], boundary[2]) <= max(box[0], boundary[0]) or min(box[3], boundary[3]) <= max(box[1], boundary[1]):
            continue
        overlap, _ = cv2.intersectConvexConvex(candidate, np.asarray(polygon, dtype=np.float32))
        if overlap >= area * threshold:
            return True
    return False


def mask_nontext(image, polygons, *, origin=(0, 0), scale=(1, 1)):
    """Mask only the interior of verified polygons in an OCR-only image copy."""
    if not polygons:
        return image
    mask = Image.new('L', image.size, 0)
    draw = ImageDraw.Draw(mask)
    for polygon in polygons:
        draw.polygon([((x-origin[0])*scale[0], (y-origin[1])*scale[1])
                      for x, y in polygon], fill=255)
    # Rasterisation includes boundary pixels. Keep a one-pixel safety rim so
    # touching captions and rotated corners cannot be whitened by rounding.
    mask = mask.filter(ImageFilter.MinFilter(3))
    result = image.copy()
    result.paste('white', mask=mask)
    return result


@dataclass
class NonTextImage:
    gray: object
    scale: float
    polygons: list

    @classmethod
    def read(cls, path: Path):
        import cv2
        import numpy as np
        with Image.open(path) as image:
            size = image.size
            image = image.convert('L')
            image.thumbnail((1800, 1800), Image.Resampling.BILINEAR)
            gray = np.asarray(image)
        scale = size[0] / gray.shape[1]
        polygons = []
        if min(gray.shape) >= 40 and int(gray.min()) < 220:
            try:
                found, points = cv2.QRCodeDetector().detectMulti(gray)
                if found and points is not None:
                    polygons.extend((polygon * scale).tolist() for polygon in points)
            except cv2.error:
                pass
            polygons.extend(rectangle(tuple(value * scale for value in box)) for box in _barcode_boxes(gray))
        return cls(gray, scale, polygons)

    def is_ornament(self, box, text=''):
        import cv2
        import numpy as np
        left, top, right, bottom = [round(value / self.scale) for value in box]
        left, top = max(0, left), max(0, top)
        crop = self.gray[top:min(self.gray.shape[0], bottom), left:min(self.gray.shape[1], right)]
        if not crop.size or crop.shape[0] < 3:
            return False
        # Include faint captions/numerators in the evidence against deletion.
        ink = np.asarray(crop < 235, dtype=np.uint8)
        count, _labels, stats, _centers = cv2.connectedComponentsWithStats(ink, 8)
        if count <= 1:
            return False
        largest = stats[1:][np.argmax(stats[1:, cv2.CC_STAT_AREA])]
        width, height, area = largest[2:]
        # Any separate ink can be a numerator, caption or a small real glyph.
        if not (width >= max(90, height * 12)
                and width >= max(1, len(comparison_key(text))) * max(5, height) * 3
                and area == ink.sum()):
            return False
        x, y = largest[:2]
        stroke = ink[y:y+height, x:x+width]
        columns = stroke.sum(axis=0)
        if np.any(columns == 0) or columns.max() > np.median(columns)*2.5:
            return False
        centers = (stroke * np.arange(height)[:, None]).sum(axis=0) / columns
        # Limit automatic rejection to repeated thin zig-zags/waves. A plain
        # rule, fraction bar or text touching an underline remains ambiguous.
        if np.ptp(centers) < max(3, height*.45):
            return False
        smooth = np.convolve(centers, np.ones(5)/5, mode='valid')
        slopes = np.diff(smooth)
        signs = np.sign(slopes[np.abs(slopes) > .15])
        return bool(np.count_nonzero(np.diff(signs)) >= 6)


def _barcode_boxes(gray):
    """Dense parallel bars with positive evidence and a clear text-free border.

    Close only two-pixel vertical breaks in a temporary binary copy. This can
    reconstruct interrupted bars but cannot bridge horizontal glyph gaps.
    Uneven bars must still share at least half their height. Non-bar ink in the
    candidate rectangle or its narrow border vetoes the entire mask.
    """
    import cv2
    import numpy as np
    ink = np.asarray(gray < 170, dtype=np.uint8)
    faint_ink = gray < 235
    closed = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, np.ones((3, 1), np.uint8))
    _count, labels, stats, _centers = cv2.connectedComponentsWithStats(closed, 8)
    bars = [(*map(int, s), index) for index, s in enumerate(stats[1:], 1)
            if s[3] >= 24 and s[3] >= s[2]*6 and s[4] >= s[2]*s[3]*.94]
    groups = []
    for bar in sorted(bars, key=lambda s: (s[1]+s[3]/2, s[0])):
        for group in groups:
            common_top = max(bar[1], max(b[1] for b in group))
            common_bottom = min(bar[1]+bar[3], min(b[1]+b[3] for b in group))
            longest = max(bar[3], max(b[3] for b in group))
            if common_bottom-common_top >= longest*.5:
                group.append(bar)
                break
        else:
            groups.append([bar])
    result = []
    for group in groups:
        # Split columns / distant codes before evaluating their shared band.
        runs = [[]]
        for bar in sorted(group):
            if runs[-1] and bar[0] - (runs[-1][-1][0]+runs[-1][-1][2]) > max(5, min(bar[3], runs[-1][-1][3])*.25):
                runs.append([])
            runs[-1].append(bar)
        for run in runs:
            if len(run) < 18:
                continue
            widths = np.array([b[2] for b in run])
            # Repeated I/l/1 glyphs and fence/plot strokes usually have one
            # width. Require substantial populations of both narrow and wide bars.
            if np.percentile(widths, 80) < np.percentile(widths, 20)*1.5:
                continue
            gaps = [b[0]-(a[0]+a[2]) for a, b in zip(run, run[1:])]
            if min(gaps) < 1 or max(gaps) > max(4, float(np.median(widths))*2.5):
                continue
            left, top = min(b[0] for b in run), min(b[1] for b in run)
            right, bottom = max(b[0]+b[2] for b in run), max(b[1]+b[3] for b in run)
            if right-left < max(96, (bottom-top)*2.5):
                continue
            occupancy = sum(b[4] for b in run) / ((right-left)*(bottom-top))
            if not .25 <= occupancy <= .78:
                continue
            region_labels = np.unique(labels[top:bottom, left:right])
            bar_labels = [b[5] for b in run]
            if set(region_labels)-{0, *bar_labels}:
                continue
            # Faint characters may be absent from the dark-bar segmentation.
            # Retain the region if even faint ink cannot be attributed to a bar.
            belongs_to_bar = np.isin(labels[top:bottom, left:right], bar_labels)
            if np.any(faint_ink[top:bottom, left:right] & ~belongs_to_bar):
                continue
            gutter = max(2, round(float(np.median(widths))))
            border = faint_ink[max(0, top-gutter):min(ink.shape[0], bottom+gutter),
                               max(0, left-gutter):min(ink.shape[1], right+gutter)]
            if border.sum() != faint_ink[top:bottom, left:right].sum():
                continue
            result.append((left, top, right, bottom))
    return result


def _remove_exact_lines(block, bad):
    if complete_line_geometry(block):
        kept = [line for line in block.text_lines if line not in bad]
        block.original_text = '\n'.join(line.text for line in kept)
        block.text_lines = kept
        if kept:
            block.bbox = bounds([point for line in kept for point in rectangle(line.bbox)])
        return
    # Partial geometry: only a unique whole logical line is safe to remove.
    # A substring may instead belong to a real sentence whose geometry is absent.
    for line in bad:
        parts = block.original_text.splitlines(keepends=True)
        needle = comparison_key(line.text)
        matches = [index for index, part in enumerate(parts) if comparison_key(part) == needle]
        if needle and len(matches) == 1:
            parts.pop(matches[0])
            block.original_text = ''.join(parts).strip()
            block.text_lines.remove(line)
        else:
            block.needs_review = True


def filter_nontext(page: PageResult) -> PageResult:
    analysis = NonTextImage.read(Path(page.processed_image_path))
    regions = analysis.polygons
    kept = []
    def remember(block, text, box, cell=None):
        if text.strip():
            import uuid
            page.filtered_text.append(FilteredText('filtered-' + uuid.uuid4().hex,
                text, tuple(box), block.id, cell.row if cell else None, cell.col if cell else None))
    for block in page.blocks:
        if block.edited_text is not None or block.kind == 'image':
            kept.append(block)
            continue
        # Never delete a whole layout block by its aggregate rectangle: line
        # geometry can be incomplete and cells may have no coordinates at all.
        if block.kind == 'table':
            for cell in block.cells:
                if cell.edited_text is not None:
                    continue
                if covered_by_nontext(cell.bbox, regions):
                    remember(block, cell.original_text, cell.bbox, cell)
                    cell.original_text = ''
                    cell.candidates = []
                elif covered_by_nontext(cell.bbox, regions, threshold=1e-6):
                    cell.needs_review = True
            kept.append(block)
            continue
        bad = []
        for line in block.text_lines:
            if covered_by_nontext(line.bbox, regions):
                bad.append(line)
            elif covered_by_nontext(line.bbox, regions, threshold=1e-6):
                block.needs_review = True
            elif analysis.is_ornament(line.bbox, line.text):
                regions.append(rectangle(line.bbox))
                bad.append(line)
        if bad:
            _remove_exact_lines(block, bad)
            for line in bad:
                if line not in block.text_lines:
                    remember(block, line.text, line.bbox)
        elif not block.text_lines and analysis.is_ornament(block.bbox, block.original_text):
            regions.append(rectangle(block.bbox))
            remember(block, block.original_text, block.bbox)
            continue
        elif not block.text_lines and covered_by_nontext(block.bbox, regions, threshold=1e-6):
            block.needs_review = True
        if block.original_text.strip():
            kept.append(block)
    page.blocks = kept
    page.nontext_regions = regions
    if not page.text_only:
        owners = [rectangle(block.bbox) for block in kept if block.kind in {'image', 'table'}]
        for index, polygon in enumerate(regions):
            box = bounds(polygon)
            if not covered_by_nontext(box, owners, threshold=.8):
                image = Block(f'{page.id}:nontext-{index}', 'image', box)
                # Keep source reading order, and preserve the actual code in
                # rich exports through the ordinary image-crop pipeline.
                position = next((i for i, b in enumerate(page.blocks) if b.bbox[1] > box[1]), len(page.blocks))
                page.blocks.insert(position, image)
                owners.append(rectangle(box))
    return page


def restore_filtered_text(page: PageResult, fragment_id: str) -> bool:
    """Explicit restoration never replaces text subsequently edited by a user."""
    fragment = next((item for item in page.filtered_text if item.id == fragment_id), None)
    if fragment is None or fragment.restored:
        return False
    if fragment.cell_row is not None:
        block = next((block for block in page.blocks if block.id == fragment.source_block_id), None)
        cell = next((cell for cell in block.cells if cell.row == fragment.cell_row and cell.col == fragment.cell_col), None) if block else None
        if cell is None or cell.edited_text is not None or cell.text.strip():
            return False
        cell.edited_text = fragment.text
        cell.needs_review = True
        block.needs_review = True
    else:
        import uuid
        block = Block('restored-' + uuid.uuid4().hex, 'text', fragment.bbox, fragment.text,
                      edited_text=fragment.text, needs_review=True, proofread_source='manual_restoration',
                      recognition_source='recovered', text_lines=[TextLine(fragment.text, fragment.bbox)])
        position = next((i for i, other in enumerate(page.blocks) if other.bbox[1] > block.bbox[1]), len(page.blocks))
        page.blocks.insert(position, block)
    # Remove only automatically created code pictures covering this fragment;
    # original illustrations and adjacent text are unchanged.
    page.blocks = [block for block in page.blocks if not (block.kind == 'image' and
        block.id.startswith(page.id + ':nontext-') and covered_by_nontext(fragment.bbox, [rectangle(block.bbox)]))]
    page.nontext_regions = [region for region in page.nontext_regions if not covered_by_nontext(fragment.bbox, [region])]
    fragment.restored = True
    return True
