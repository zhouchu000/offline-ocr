"""Pure helpers for bounded, user-confirmed detail OCR suggestions."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .domain import Block, PageResult
from .proofread import comparison_key


_DETAIL_CONFIDENCE_THRESHOLD = 0.90
_DETAIL_STATUS_PRIORITY = {
    "not_run": 0,
    "consistent": 1,
    "candidate": 2,
    "conflict": 3,
    "incomplete": 4,
}


@dataclass(frozen=True)
class DetailTarget:
    block_index: int
    original_text: str
    bbox: tuple[float, float, float, float]
    confidence: float


@dataclass(frozen=True)
class DetailResult:
    text: str
    confidence: float | None = None


def _owning_text_block(page: PageResult, bbox: tuple[float, float, float, float]) -> int | None:
    center_x = (bbox[0] + bbox[2]) / 2
    center_y = (bbox[1] + bbox[3]) / 2
    for index, block in enumerate(page.blocks):
        if block.kind not in {"text", "title"}:
            continue
        left, top, right, bottom = block.bbox
        if left <= center_x <= right and top <= center_y <= bottom:
            return index
    return None


def detail_targets(
    page: PageResult,
    lines: list[tuple[str, tuple[float, float, float, float] | None, float | None]],
    *,
    limit: int = 8,
) -> list[DetailTarget]:
    """Choose a bounded set of the weakest printed-text lines on a page."""
    if limit <= 0:
        return []
    targets: list[DetailTarget] = []
    for text, bbox, confidence in lines:
        if not text or bbox is None or confidence is None or confidence > _DETAIL_CONFIDENCE_THRESHOLD:
            continue
        if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
            continue
        block_index = _owning_text_block(page, bbox)
        if block_index is None:
            continue
        targets.append(DetailTarget(block_index, text, bbox, float(confidence)))
    targets.sort(key=lambda item: (item.confidence, item.bbox[1], item.bbox[0]))
    return targets[:limit]


def _replace_unique_normalized(source: str, old: str, new: str) -> str | None:
    normalized_source_chars: list[str] = []
    source_positions: list[int] = []
    for index, character in enumerate(source):
        if character.isspace():
            continue
        normalized_source_chars.append(character)
        source_positions.append(index)
    normalized_source = "".join(normalized_source_chars)
    normalized_old = comparison_key(old)
    if not normalized_old:
        return None
    first = normalized_source.find(normalized_old)
    if first < 0 or normalized_source.find(normalized_old, first + 1) >= 0:
        return None
    start = source_positions[first]
    end = source_positions[first + len(normalized_old) - 1] + 1
    return source[:start] + new + source[end:]


def _append_unique(values: list[str], candidate: str) -> bool:
    key = comparison_key(candidate)
    if not key or any(comparison_key(value) == key for value in values):
        return False
    values.append(candidate)
    return True


def _merge_status(current: str, new: str) -> str:
    return new if _DETAIL_STATUS_PRIORITY.get(new, 0) > _DETAIL_STATUS_PRIORITY.get(current, 0) else current


def apply_detail_results(
    page: PageResult,
    target: DetailTarget,
    results: list[DetailResult],
) -> bool:
    """Attach safe full-block candidates while preserving all editable text."""
    if not 0 <= target.block_index < len(page.blocks):
        return False
    block: Block = page.blocks[target.block_index]
    block.detail_reviewed_regions += 1
    page.detail_reviewed_regions += 1
    block.needs_review = True

    nonempty: list[str] = [result.text.strip() for result in results if result.text.strip()]
    normalized = [comparison_key(text) for text in nonempty]
    source_key = comparison_key(target.original_text)
    counts = Counter(normalized)
    unique_texts: list[str] = []
    seen: set[str] = set()
    for text, key in zip(nonempty, normalized):
        if key in seen:
            continue
        seen.add(key)
        unique_texts.append(text)

    if len(results) < 3 or len(nonempty) < 3:
        status = "incomplete"
    elif len(counts) == 1 and next(iter(counts), "") == source_key:
        status = "consistent"
    elif counts and max(counts.values()) >= 2:
        status = "candidate"
    else:
        status = "conflict"

    added = False
    baseline = block.original_text
    safe_candidates: list[str] = []
    for text in unique_texts:
        if comparison_key(text) == source_key:
            continue
        replacement = _replace_unique_normalized(baseline, target.original_text, text)
        if replacement is not None:
            safe_candidates.append(replacement)
    if safe_candidates:
        if not block.candidates:
            block.candidates.append(baseline)
        elif not any(comparison_key(value) == comparison_key(baseline) for value in block.candidates):
            block.candidates.insert(0, baseline)
        for candidate in safe_candidates:
            added = _append_unique(block.candidates, candidate) or added
    if added:
        block.detail_candidate_regions += 1
        page.detail_candidate_regions += 1
        block.proofread_source = block.proofread_source or "baseline"

    block.detail_review_status = _merge_status(block.detail_review_status, status)
    page.detail_review_status = _merge_status(page.detail_review_status, status)
    return added
