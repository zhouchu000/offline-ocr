"""Remove confidently isolated folios from an export copy, never the OCR result."""
from __future__ import annotations

from dataclasses import dataclass, replace
import math
import re
import unicodedata

from .domain import Block, PageResult, TextLine


@dataclass(frozen=True)
class _Unit:
    block: int
    text: str
    bbox: tuple
    start: int = 0
    end: int = 0
    line: TextLine | None = None


def _valid_box(box, page: PageResult) -> bool:
    return (len(box) == 4 and all(isinstance(v, (int, float)) and math.isfinite(v) for v in box)
            and 0 <= box[0] < box[2] <= page.width and 0 <= box[1] < box[3] <= page.height)


def _units(page: PageResult) -> list[_Unit]:
    units = []
    for index, block in enumerate(page.blocks):
        if block.kind not in {"text", "title"}:
            units.append(_Unit(index, "", block.bbox))
            continue
        lines = (sorted(block.text_lines, key=lambda line: (line.bbox[1], line.bbox[0]))
                 if all(_valid_box(line.bbox, page) for line in block.text_lines) else [])
        positions = [i for i, char in enumerate(block.original_text) if not char.isspace()]
        # An edited paragraph or incomplete line map cannot be cut safely. It
        # can still be removed as a whole when its latest text is just a folio.
        if (block.edited_text is None and lines and all(_valid_box(line.bbox, page) for line in lines)
                and "".join("".join(line.text.split()) for line in lines)
                == "".join(block.original_text.split()) and positions
                and all(line.text.strip() for line in lines)):
            offset = 0
            for line in lines:
                length = len("".join(line.text.split()))
                start, end = positions[offset], positions[offset+length-1]+1
                units.append(_Unit(index, block.original_text[start:end], line.bbox, start, end, line))
                offset += length
        else:
            units.append(_Unit(index, block.text, block.bbox))
    return units


def _number(text: str) -> tuple[str, int, bool] | None:
    """Return notation, value, and whether another page must corroborate it."""
    text = "".join(unicodedata.normalize("NFKC", text).split())
    chinese = r"[零〇一二三四五六七八九十百千]{1,8}"
    if re.fullmatch(rf"第(?:[0-9]{{1,4}}|{chinese})页(?:(?:[,，/、]?共)(?:[0-9]{{1,4}}|{chinese})页)?", text):
        return "explicit", 0, False
    if re.fullmatch(r"page[0-9]{1,4}(?:of[0-9]{1,4})?", text, re.IGNORECASE):
        return "explicit", 0, False
    decorated = re.fullmatch(r"[-—–·•]([0-9]{1,4})[-—–·•]|\(([0-9]{1,4})\)|\[([0-9]{1,4})\]", text)
    if decorated and int(next(value for value in decorated.groups() if value)) > 0:
        return "explicit", 0, False
    if re.fullmatch(r"[0-9]{1,4}", text):
        value = int(text)
        # A bare year is too ambiguous to delete, even at the sheet edge.
        return ("decimal", value, False) if 0 < value < 1000 else None
    fraction = re.fullmatch(r"([0-9]{1,4})/([0-9]{1,4})", text)
    if fraction:
        value, total = map(int, fraction.groups())
        return (f"fraction:{total}", value, True) if 0 < value <= total else None
    if text and re.fullmatch(r"(?=[IVXLCDM]+$)M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})", text.upper()):
        values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
        digits = [values[char] for char in text.upper()]
        value = sum(-a if a < b else a for a, b in zip(digits, digits[1:]+[0]))
        return "roman", value, True
    return None


def _margin(unit: _Unit, units: list[_Unit], page: PageResult) -> str | None:
    if page.width <= 0 or page.height <= 0 or not _valid_box(unit.bbox, page):
        return None
    left, top, right, bottom = unit.bbox
    height = bottom-top
    if height > page.height*.045 or right-left > page.width*.30:
        return None
    edge = "top" if bottom <= page.height*.12 else "bottom" if top >= page.height*.88 else None
    if edge is None:
        return None
    center = (left+right)/2/page.width
    if not (center <= .22 or .38 <= center <= .62 or center >= .78):
        return None
    others = [other for other in units if other is not unit and _valid_box(other.bbox, page)]
    if not others:
        return None
    gap = max(height*1.4, page.height*.015)
    # Includes figures and whole tables: their numbers and captions cannot be
    # reclassified merely because they happen to lie near the bottom margin.
    if any((other.bbox[3] > top-gap if edge == "bottom" else other.bbox[1] < bottom+gap)
           for other in others):
        return None
    return edge


def _trim_block(block: Block, remaining: list[_Unit]) -> Block:
    start, end = remaining[0].start, remaining[-1].end
    lines = [unit.line for unit in remaining]
    bbox = (min(line.bbox[0] for line in lines), min(line.bbox[1] for line in lines),
            max(line.bbox[2] for line in lines), max(line.bbox[3] for line in lines))
    ranges = [(max(a, start)-start, min(b, end)-start) for a, b in block.bold_ranges
              if max(a, start) < min(b, end)]
    return replace(block, original_text=block.original_text[start:end], text_lines=lines,
                   bbox=bbox, bold_ranges=ranges)


def without_page_numbers(pages: list[PageResult]) -> tuple[list[PageResult], int]:
    """Project a batch for export; uncertain numbers and manual edits survive.

    Date-like fractions and Roman letters need consecutive-page evidence. Run
    once for the whole queue so separate exports use the same decision as merge.
    """
    page_units = [_units(page) for page in pages]
    candidates = []
    for page_index, (page, units) in enumerate(zip(pages, page_units)):
        for index, unit in enumerate(units):
            number = _number(unit.text)
            edge = _margin(unit, units, page) if number else None
            if edge:
                # A solitary numeric heading is not enough evidence of a
                # header folio; require a matching sequence on another page.
                if number[0] == "decimal" and (edge == "top" or page.blocks[unit.block].kind == "title"):
                    number = (number[0], number[1], True)
                candidates.append((page_index, index, edge, number))
    removed = set()
    for page_index, index, edge, (family, value, needs_sequence) in candidates:
        if not needs_sequence or any(
            other_page != page_index and other_edge == edge and other_number[0] == family
            and other_number[1]-value == other_page-page_index
            for other_page, _, other_edge, other_number in candidates
        ):
            removed.add((page_index, index))
    result = []
    for page_index, (page, units) in enumerate(zip(pages, page_units)):
        blocks = []
        for block_index, block in enumerate(page.blocks):
            indexed = [(index, unit) for index, unit in enumerate(units) if unit.block == block_index]
            kept = [unit for index, unit in indexed if (page_index, index) not in removed]
            if len(kept) == len(indexed):
                blocks.append(block)
            elif kept:
                blocks.append(_trim_block(block, kept))
        result.append(replace(page, blocks=blocks))
    return result, len(removed)
