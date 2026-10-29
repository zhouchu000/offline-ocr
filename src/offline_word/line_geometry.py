"""Keep printed-line coordinates tied to the text they actually describe."""
from __future__ import annotations

import unicodedata
import math


def text_key(text: str) -> str:
    return unicodedata.normalize("NFKC", "".join(text.split()))


def complete_line_geometry(block) -> bool:
    if not all(len(line.bbox) == 4 and all(math.isfinite(value) for value in line.bbox)
               and line.bbox[2] > line.bbox[0] and line.bbox[3] > line.bbox[1]
               for line in block.text_lines):
        return False
    lines = sorted(block.text_lines, key=lambda line: (line.bbox[1], line.bbox[0]))
    return bool(lines) and text_key("".join(line.text for line in lines)) == text_key(block.original_text)


def same_printed_line(first, second) -> bool:
    a, b, c, d = first
    x, y, z, w = second
    height = min(d-b, w-y)
    width = min(c-a, z-x)
    return height > 0 and width > 0 and (
        min(d, w)-max(b, y) >= height*.5
        and min(c, z)-max(a, x) >= width*.5
    )
