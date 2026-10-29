"""Deterministic consensus helpers for repeated OCR page results."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy

from .domain import Block, Cell, PageResult


_MINIMUM_BLOCK_IOU = 0.5
_STATUS_PRIORITY = {"consistent": 0, "majority": 1, "conflict": 2, "incomplete": 3}


def comparison_key(text: str) -> str:
    """Return the whitespace-insensitive value used when comparing OCR text."""
    return "".join(str(text).split())


def bbox_iou(left, right) -> float:
    """Return the intersection-over-union of two ``(left, top, right, bottom)`` boxes."""
    intersection = max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    union = max(0.0, (left[2] - left[0]) * (left[3] - left[1])) + max(0.0, (right[2] - right[0]) * (right[3] - right[1])) - intersection
    return intersection / union if union else 0.0


def reconcile_page(
    baseline: PageResult,
    comparisons: list[PageResult],
    *,
    completed_passes: int = 3,
) -> PageResult:
    """Return a copied page that preserves baseline text and exposes comparisons.

    The baseline is the first OCR pass in a fixed one-to-three pass contract.
    Each later page is greedily paired with it by block kind and IoU; a block
    must have a counterpart in every completed pass before a majority can be
    reported as a candidate for explicit user adoption.
    """
    if not 1 <= completed_passes <= 3:
        raise ValueError("completed_passes must be between 1 and 3")

    result = deepcopy(baseline)
    expected_comparisons = max(completed_passes - 1, 0)
    comparison_pages = comparisons[:expected_comparisons]
    page_incomplete = completed_passes < 3 or len(comparison_pages) < expected_comparisons

    matches = [_match_blocks(baseline.blocks, comparison.blocks) for comparison in comparison_pages]
    statuses: list[str] = []
    for index, (baseline_block, result_block) in enumerate(zip(baseline.blocks, result.blocks)):
        comparison_blocks = [page_matches.get(index) for page_matches in matches]
        if result_block.kind == "table":
            status = _reconcile_table(
                result_block,
                baseline_block,
                comparison_blocks,
                completed_passes=completed_passes,
                page_incomplete=page_incomplete,
            )
        else:
            status = _reconcile_text_value(
                result_block,
                baseline_block.text,
                [block.text if block is not None else None for block in comparison_blocks],
                completed_passes=completed_passes,
                page_incomplete=page_incomplete,
            )
        statuses.append(status)

    result.proofread_completed_passes = completed_passes
    result.proofread_status = "incomplete" if page_incomplete else _aggregate_status(statuses)
    return result


def _match_blocks(baseline: list[Block], comparison: list[Block]) -> dict[int, Block]:
    """Greedily pair baseline blocks to one unused same-kind comparison block."""
    matches: dict[int, Block] = {}
    available = set(range(len(comparison)))
    for baseline_index, baseline_block in enumerate(baseline):
        candidates = [
            (bbox_iou(baseline_block.bbox, comparison[index].bbox), index)
            for index in available
            if comparison[index].kind == baseline_block.kind
        ]
        if not candidates:
            continue
        overlap, comparison_index = max(candidates, key=lambda candidate: (candidate[0], -candidate[1]))
        if overlap < _MINIMUM_BLOCK_IOU:
            continue
        available.remove(comparison_index)
        matches[baseline_index] = comparison[comparison_index]
    return matches


def _reconcile_text_value(
    target: Block | Cell,
    baseline_text: str,
    comparison_texts: list[str | None],
    *,
    completed_passes: int,
    page_incomplete: bool,
) -> str:
    values = [baseline_text, *(text for text in comparison_texts if text is not None)]
    target.candidates = _unique(values)
    target.proofread_passes = len(values)

    if page_incomplete:
        _mark_incomplete(target)
        return target.proofread_status
    if len(values) != completed_passes:
        target.proofread_status = "conflict"
        target.proofread_source = "baseline"
        target.needs_review = True
        return target.proofread_status

    keys = [comparison_key(value) for value in values]
    counts = Counter(keys)
    winner = next((value for value, key in zip(values, keys) if counts[key] > len(values) / 2), None)
    if winner is None:
        target.proofread_status = "conflict"
        target.proofread_source = "baseline"
        target.needs_review = True
        return target.proofread_status

    target.proofread_source = "baseline"
    target.proofread_status = "consistent" if len(counts) == 1 else "majority"
    if target.proofread_status == "majority":
        target.needs_review = True
    return target.proofread_status


def _reconcile_table(
    target: Block,
    baseline: Block,
    comparisons: list[Block | None],
    *,
    completed_passes: int,
    page_incomplete: bool,
) -> str:
    table_values = [_table_value(baseline), *(_table_value(block) for block in comparisons if block is not None)]
    target.candidates = _unique(table_values)
    target.proofread_passes = len(table_values)

    if page_incomplete:
        _mark_table_incomplete(target, baseline, comparisons, completed_passes)
        return target.proofread_status
    if len(comparisons) != completed_passes - 1 or any(block is None for block in comparisons) or any(
        block is not None and _table_structure(block) != _table_structure(baseline)
        for block in comparisons
    ):
        _mark_table_conflict(target, len(table_values))
        return target.proofread_status

    comparison_cells = [{(cell.row, cell.col): cell for cell in block.cells} for block in comparisons if block is not None]
    statuses = [
        _reconcile_text_value(
            target_cell,
            baseline_cell.text,
            [cells[(baseline_cell.row, baseline_cell.col)].text for cells in comparison_cells],
            completed_passes=completed_passes,
            page_incomplete=False,
        )
        for baseline_cell, target_cell in zip(baseline.cells, target.cells)
    ]
    target.proofread_status = _aggregate_status(statuses)
    target.proofread_source = "baseline"
    target.needs_review = target.needs_review or any(cell.needs_review for cell in target.cells)
    return target.proofread_status


def _mark_table_conflict(table: Block, completed_passes: int) -> None:
    table.proofread_status = "conflict"
    table.proofread_source = "baseline"
    table.needs_review = True
    for cell in table.cells:
        cell.candidates = [cell.text]
        cell.proofread_status = "conflict"
        cell.proofread_source = "baseline"
        cell.proofread_passes = completed_passes
        cell.needs_review = True


def _mark_incomplete(target: Block | Cell) -> None:
    target.proofread_status = "incomplete"
    target.proofread_source = "baseline"
    target.needs_review = True


def _mark_table_incomplete(
    table: Block,
    baseline: Block,
    comparisons: list[Block | None],
    completed_passes: int,
) -> None:
    _mark_incomplete(table)
    if any(block is None or _table_structure(block) != _table_structure(baseline) for block in comparisons):
        for cell in table.cells:
            cell.candidates = [cell.text]
            cell.proofread_passes = 1
            _mark_incomplete(cell)
        return

    comparison_cells = [{(cell.row, cell.col): cell for cell in block.cells} for block in comparisons if block is not None]
    for baseline_cell, target_cell in zip(baseline.cells, table.cells):
        _reconcile_text_value(
            target_cell,
            baseline_cell.text,
            [cells[(baseline_cell.row, baseline_cell.col)].text for cells in comparison_cells],
            completed_passes=completed_passes,
            page_incomplete=True,
        )


def _table_structure(block: Block) -> tuple[int, int, tuple[tuple[int, int, int, int], ...]]:
    return block.rows, block.cols, tuple(sorted(
        (cell.row, cell.col, cell.rowspan, cell.colspan) for cell in block.cells
    ))


def _table_value(block: Block) -> str:
    return "\x1f".join(cell.text for cell in block.cells)


def _aggregate_status(statuses: list[str]) -> str:
    return max(statuses, key=lambda status: _STATUS_PRIORITY[status], default="consistent")


def _unique(values: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = comparison_key(value)
        if key not in seen:
            seen.add(key)
            unique.append(value)
    return unique
