"""Advisory geometry checks and a mandatory text-count check before publishing.

Geometry findings never remove or rewrite content. The DOCX check compares
non-whitespace character multiplicities: it detects export losses/duplication,
not text which the OCR never recognized or visual overlap in an Office renderer.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import math

from .domain import PageResult, normalize_table_cells
from .security import OfflineError


@dataclass(frozen=True)
class ExportIssue:
    page_id: str
    block_ids: tuple[str, ...]
    code: str
    message: str
    severity: int = 1


def _valid(box):
    return (len(box) == 4 and all(math.isfinite(v) for v in box)
            and box[2] > box[0] and box[3] > box[1])


def _overlap(a, b):
    if not _valid(a) or not _valid(b):
        return 0.0
    width = max(0, min(a[2], b[2])-max(a[0], b[0]))
    height = max(0, min(a[3], b[3])-max(a[1], b[1]))
    return width*height/min((a[2]-a[0])*(a[3]-a[1]), (b[2]-b[0])*(b[3]-b[1]))


def inspect_page(page: PageResult, *, layout_mode='source') -> list[ExportIssue]:
    issues = []
    emitted = set()
    def add(blocks, code, message, severity=1):
        key = (tuple(sorted(blocks)), code)
        if key not in emitted:
            emitted.add(key)
            issues.append(ExportIssue(page.id, tuple(blocks), code, message, severity))
    units = []
    for block in page.blocks:
        if block.kind == 'image':
            continue
        if not _valid(block.bbox):
            add([block.id], 'geometry', '文字坐标无效，使用连续排版', 2)
            continue
        x, y, r, b = block.bbox
        if x < -1 or y < -1 or r > page.width+1 or b > page.height+1:
            add([block.id], 'boundary', '文字区域超出图片边界', 2)
        if block.kind not in {'text', 'title'}:
            continue
        recovered = block.recognition_source == 'recovered' or ':recovered-' in block.id
        lines = [(line.bbox, line.text) for line in block.text_lines] or [(block.bbox, block.text)]
        for box, text in lines:
            if _valid(box) and text.strip():
                units.append((box, text, block.id, recovered))
        for polygon in page.nontext_regions:
            if not polygon:
                continue
            left, right = min(p[0] for p in polygon), max(p[0] for p in polygon)
            top, bottom = min(p[1] for p in polygon), max(p[1] for p in polygon)
            margin = min(12, max(3, (b-y)*.2))
            if _overlap(block.bbox, (left-margin, top-margin, right+margin, bottom+margin)) > .1:
                add([block.id], 'code_nearby', '文字邻近图码，待核对')
                break
    active = []
    for unit in sorted(units, key=lambda u: u[0][1]):
        box, text, block_id, recovered = unit
        active = [prior for prior in active if prior[0][3] > box[1]]
        for prior_box, prior_text, prior_id, prior_recovered in active:
            if prior_id == block_id:
                continue
            overlap = _overlap(box, prior_box)
            if overlap < .35:
                continue
            if layout_mode == 'source' and (recovered or prior_recovered):
                add([prior_id, block_id], 'recovery_overlap', '补回文字位置交叠，待核对排版', 3)
            key = ''.join(text.split())
            if len(key) >= 12 and key == ''.join(prior_text.split()):
                add([prior_id, block_id], 'duplicate', '邻近位置文字相同，疑似重复', 2)
        active.append(unit)
    return issues


def expected_text(pages):
    fragments = []
    for page in pages:
        for block in page.blocks:
            if block.kind == 'image':
                continue
            if block.kind == 'table':
                cells = normalize_table_cells(deepcopy(block.cells), block.rows, block.cols)
                fragments.extend(cell.text for cell in cells)
                if not cells:
                    fragments.append(block.text)
            else:
                fragments.append(block.text)
    return ''.join(fragments)


def assert_text_coverage(document, expected: str):
    actual = ''.join(node.text or '' for node in document.element.iter(
        '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t'))
    def characters(value):
        return Counter(c for c in value if not c.isspace())
    if characters(actual) != characters(expected):
        # Never put document text in an error, log or diagnostic report.
        raise OfflineError('export_text_coverage_failed')
