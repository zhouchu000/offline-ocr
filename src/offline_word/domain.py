"""Small, picklable domain objects shared by the desktop and OCR process."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class Cell:
    row: int
    col: int
    original_text: str = ""
    edited_text: str | None = None
    rowspan: int = 1
    colspan: int = 1
    confidence: float | None = None
    candidates: list[str] = field(default_factory=list)
    proofread_status: str = "consistent"
    proofread_source: str | None = None
    proofread_passes: int = 0
    needs_review: bool = False
    bbox: tuple[float, float, float, float] | None = None
    needs_geometry_review: bool = False

    @property
    def text(self) -> str:
        return self.original_text if self.edited_text is None else self.edited_text


@dataclass
class TextLine:
    text: str
    bbox: tuple[float, float, float, float]
    confidence: float | None = None
    ink_bbox: tuple[float, float, float, float] | None = None


@dataclass
class Block:
    id: str
    kind: str
    bbox: tuple[float, float, float, float]
    original_text: str = ""
    edited_text: str | None = None
    confidence: float | None = None
    cells: list[Cell] = field(default_factory=list)
    rows: int = 0
    cols: int = 0
    image_path: str | None = None
    candidates: list[str] = field(default_factory=list)
    needs_review: bool = False
    proofread_status: str = "consistent"
    proofread_source: str | None = None
    proofread_passes: int = 0
    detail_reviewed_regions: int = 0
    detail_candidate_regions: int = 0
    detail_review_status: str = "not_run"
    minimum_line_confidence: float | None = None
    text_lines: list[TextLine] = field(default_factory=list)
    font_name: str | None = None
    font_bold: bool = False
    bold_ranges: list[tuple[int, int]] = field(default_factory=list)
    recognition_source: str = "baseline"
    review_tags: list[str] = field(default_factory=list)
    review_confirmed_signature: str | None = None

    @property
    def text(self) -> str:
        return self.original_text if self.edited_text is None else self.edited_text


@dataclass
class FilteredText:
    id: str
    text: str
    bbox: tuple[float, float, float, float]
    source_block_id: str
    cell_row: int | None = None
    cell_col: int | None = None
    restored: bool = False


@dataclass
class PageResult:
    id: str
    page_index: int
    width: int
    height: int
    processed_image_path: str
    blocks: list[Block] = field(default_factory=list)
    proofread_status: str = "consistent"
    proofread_completed_passes: int = 0
    recognition_warning: str | None = None
    detail_reviewed_regions: int = 0
    detail_candidate_regions: int = 0
    detail_review_status: str = "not_run"
    text_only: bool = False
    nontext_regions: list[list[tuple[float, float]]] = field(default_factory=list)
    paper_polygon: list[tuple[float, float]] | None = None
    source_paper_quad: list[tuple[float, float]] | None = None
    paper_status: str = "not_run"
    paper_confidence: float = 0.0
    paper_review_count: int = 0
    performance_profile: str = "sequential"
    filtered_text: list[FilteredText] = field(default_factory=list)


@dataclass
class ImageJob:
    id: str
    source_path: str
    frame_index: int = 0
    mode: str = "scan"
    rotation: int = 0
    crop: tuple | None = None
    perspective: list | None = None
    status: str = "pending"
    error_code: str | None = None
    result: PageResult | None = None
    auto_proofread: bool = False
    text_only: bool = False
    performance_profile: str = "sequential"
    diagnostics: bool = False
    paper_quad: list[tuple[float, float]] | None = None
    paper_confidence: float = 0.0
    paper_status: str = "not_run"
    processed_paper_polygon: list[tuple[float, float]] | None = None
    display_name: str | None = None


def normalize_table_cells(cells: Iterable[Cell], rows: int, cols: int) -> list[Cell]:
    """Return a complete, non-overlapping table grid without losing editable text.

    OCR may return duplicate cells, invalid spans, or omit blank cells.  The first
    valid cell in reading order owns its rectangle; later overlapping detections
    are discarded, and uncovered locations become editable empty cells.
    """
    supplied = list(cells)
    if rows <= 0:
        rows = max((max(0, int(cell.row)) + max(1, int(cell.rowspan)) for cell in supplied), default=0)
    if cols <= 0:
        cols = max((max(0, int(cell.col)) + max(1, int(cell.colspan)) for cell in supplied), default=0)
    if rows <= 0 or cols <= 0:
        return []

    occupied = [[False for _ in range(cols)] for _ in range(rows)]
    anchors: dict[tuple[int, int], Cell] = {}
    for cell in supplied:
        original_geometry = (cell.row, cell.col, cell.rowspan, cell.colspan)
        row = min(max(int(cell.row), 0), rows - 1)
        col = min(max(int(cell.col), 0), cols - 1)
        rowspan = min(max(int(cell.rowspan), 1), rows - row)
        colspan = min(max(int(cell.colspan), 1), cols - col)
        if any(occupied[current_row][current_col]
               for current_row in range(row, row + rowspan)
               for current_col in range(col, col + colspan)):
            # A second OCR detection cannot become a Word cell without replacing
            # its owner. Keep its original text with that owner and flag it for
            # review instead of silently dropping it.
            for (anchor_row, anchor_col), owner in anchors.items():
                overlaps = (
                    anchor_row < row + rowspan and row < anchor_row + owner.rowspan
                    and anchor_col < col + colspan and col < anchor_col + owner.colspan
                )
                if overlaps:
                    preserved = cell.original_text or cell.text
                    if preserved and preserved not in owner.original_text.split("\n"):
                        owner.original_text = f"{owner.original_text}\n{preserved}".strip("\n")
                    owner.needs_geometry_review = True
                    break
            continue
        cell.row, cell.col = row, col
        cell.rowspan, cell.colspan = rowspan, colspan
        if original_geometry != (row, col, rowspan, colspan):
            cell.needs_geometry_review = True
        anchors[(row, col)] = cell
        for current_row in range(row, row + rowspan):
            for current_col in range(col, col + colspan):
                occupied[current_row][current_col] = True

    normalized: list[Cell] = []
    for row in range(rows):
        for col in range(cols):
            if (row, col) in anchors:
                normalized.append(anchors[(row, col)])
            elif not any(
                anchor_row <= row < anchor_row + cell.rowspan
                and anchor_col <= col < anchor_col + cell.colspan
                for (anchor_row, anchor_col), cell in anchors.items()
            ):
                normalized.append(Cell(row=row, col=col))
    return normalized


def table_needs_review(cells: Iterable[Cell]) -> bool:
    """Whether normalisation retained malformed/overlapping OCR input for review."""
    return any(cell.needs_geometry_review for cell in cells)
