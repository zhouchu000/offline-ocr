"""Conservative association of table HTML with its own local cell geometry."""
from __future__ import annotations

import math
import statistics


def grid_boundaries(cells, count, *, horizontal):
    """Resolve a grid including merged cells; reject contradictory evidence."""
    edges = [[] for _ in range(count + 1)]
    for cell in cells:
        if cell.bbox is None:
            continue
        start = cell.col if horizontal else cell.row
        span = cell.colspan if horizontal else cell.rowspan
        if start < 0 or start + span > count:
            return None
        edges[start].append(cell.bbox[0 if horizontal else 1])
        edges[start + span].append(cell.bbox[2 if horizontal else 3])
    if not edges[0] or not edges[-1]:
        return None
    extent = statistics.median(edges[-1]) - statistics.median(edges[0])
    if extent <= 0 or any(values and max(values)-min(values) > extent*.04 for values in edges):
        return None
    # An entirely merged row/column may have no internal boundary. There is no
    # evidence for its individual widths, so retain the ordinary table fallback.
    if any(not values for values in edges):
        return None
    boundaries = [statistics.median(values) for values in edges]
    return boundaries if all(b > a for a,b in zip(boundaries, boundaries[1:])) else None


def attach_table_geometry(block, results):
    """Never associate by table-list index or overwrite the authoritative HTML."""
    if not isinstance(results, list) or not block.cells or block.needs_review:
        return
    matches = []
    left, top, right, bottom = block.bbox
    for result in results:
        if not isinstance(result, dict) or result.get('pred_html') != block.original_text:
            continue
        boxes = result.get('cell_box_list')
        if not isinstance(boxes, list) or len(boxes) != len(block.cells):
            continue
        converted = []
        for raw in boxes:
            try:
                box = tuple(float(value) for value in raw)
            except (TypeError, ValueError):
                break
            if (len(box) != 4 or not all(math.isfinite(v) for v in box)
                    or box[2] <= box[0] or box[3] <= box[1]
                    or not left <= (box[0]+box[2])/2 <= right
                    or not top <= (box[1]+box[3])/2 <= bottom):
                break
            converted.append(box)
        if len(converted) == len(block.cells):
            matches.append(converted)
    if len(matches) != 1:
        return
    for cell, box in zip(block.cells, matches[0]):
        cell.bbox = box
    if (grid_boundaries(block.cells, block.cols, horizontal=True) is None
            or grid_boundaries(block.cells, block.rows, horizontal=False) is None):
        for cell in block.cells:
            cell.bbox = None
