"""Plain content export, independent of source-page typography and geometry."""

import re
import statistics
from dataclasses import replace

from .domain import PageResult
from .line_geometry import complete_line_geometry


def reading_order(blocks):
    """Resolve obvious text columns using empty gutters, without a layout model.

    Short labels/table cells retain row order. Full-width headings divide the
    page before the columns beneath them are traversed from left to right.
    """
    if len(blocks) < 2:
        return list(blocks)
    height = statistics.median(max(1, b.bbox[3]-b.bbox[1]) for b in blocks)
    for axis in (0, 1):
        ordered = sorted(blocks, key=lambda b: (b.bbox[axis], b.bbox[1-axis]))
        end = ordered[0].bbox[axis+2]
        gaps = []
        for index, block in enumerate(ordered[1:], 1):
            gap = block.bbox[axis]-end
            if gap > 0:
                if axis == 1 or (gap >= height*1.25 and min(index, len(ordered)-index) >= 2
                                and all(statistics.median(len(b.text.strip()) for b in side) >= 10
                                        for side in (ordered[:index], ordered[index:]))):
                    gaps.append((gap, index))
            end = max(end, block.bbox[axis+2])
        if gaps:
            def opens_columns(index):
                if axis == 0:
                    return False
                for side in (ordered[:index], ordered[index:]):
                    columns = sorted(side, key=lambda block: block.bbox[0])
                    if len(columns) < 4:
                        continue
                    edge = columns[0].bbox[2]
                    for i, block in enumerate(columns[1:], 1):
                        if block.bbox[0]-edge >= height*1.25 and min(i, len(columns)-i) >= 2:
                            return True
                        edge = max(edge, block.bbox[2])
                return False
            _, split = max(gaps, key=lambda gap: (opens_columns(gap[1]), gap[0], -abs(gap[1]-len(ordered)/2)))
            return reading_order(ordered[:split])+reading_order(ordered[split:])
    return sorted(blocks, key=lambda b: (b.bbox[1], b.bbox[0]))


def continuous_text(pages: list[PageResult]) -> str:
    fragments = []
    for page in pages:
        blocks = []
        for block in page.blocks:
            if block.edited_text is None and block.kind in {"text", "title"} and complete_line_geometry(block):
                # Keep exact baseline punctuation: normalization is only a
                # geometry test, not permission to substitute OCR spellings.
                lines = sorted(block.text_lines, key=lambda line: (line.bbox[1], line.bbox[0]))
                if "".join("".join(line.text.split()) for line in lines) == "".join(block.original_text.split()):
                    blocks.extend(replace(block, original_text=line.text, bbox=line.bbox) for line in lines)
                    continue
            blocks.append(block)
        for block in reading_order(blocks):
            if block.kind == "image":
                continue
            values = ([cell.text for cell in sorted(block.cells, key=lambda cell: (cell.row, cell.col))]
                      if block.kind == "table" and block.cells else [block.text])
            for value in values:
                fragments.extend(" ".join(line.split()) for line in value.splitlines() if line.strip())
    return _join_fragments(fragments)


def unwrap_text(text: str) -> str:
    """Join printed rows while retaining English word boundaries."""
    return _join_fragments([" ".join(line.split()) for line in text.splitlines() if line.strip()])


def _join_fragments(fragments) -> str:
    result = []
    previous = ""
    for fragment in fragments:
        # Preserve English word boundaries, without inserting spaces into Chinese
        # merely because the original printed page wrapped to another line.
        separator = " " if previous and re.search(r"[A-Za-z0-9.,!?;:)]$", previous) and re.match(r"[A-Za-z0-9(]", fragment) else ""
        result.append(separator + fragment)
        previous = fragment
    return "".join(result)
