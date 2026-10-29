"""Cross-page review selection for low-confidence OCR content."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json

from .domain import Block, ImageJob
from .proofread import comparison_key


LOW_CONFIDENCE_THRESHOLD = 0.90


@dataclass(frozen=True)
class ReviewItem:
    """One editable OCR block and the page image needed to verify it."""

    job_id: str
    page_number: int
    processed_image_path: str
    block: Block
    reasons: tuple[str, ...]
    categories: tuple[str, ...] = ()
    risk: int = 0
    fingerprint: str = ''
    confirmed: bool = False


def _review_reasons(block: Block, threshold: float) -> tuple[str, ...]:
    reasons: list[str] = []
    if block.confidence is not None and block.confidence <= threshold:
        reasons.append(f"低置信度 {block.confidence:.0%}")
    elif block.minimum_line_confidence is not None and block.minimum_line_confidence <= threshold:
        reasons.append(f"低置信度文字行 {block.minimum_line_confidence:.0%}")
    if any(cell.confidence is not None and cell.confidence <= threshold for cell in block.cells):
        reasons.append("低置信度表格单元格")
    proofread_status = block.proofread_status
    if block.proofread_passes and proofread_status == "consistent" and block.proofread_passes < 3:
        proofread_status = "incomplete"
    if proofread_status == "majority":
        reasons.append("多次识别多数结果待核对")
    elif proofread_status == "conflict":
        reasons.append("多次识别结果不一致")
    elif proofread_status == "incomplete":
        reasons.append("多次识别未完成")
    candidate_sets = [block.candidates, *(cell.candidates for cell in block.cells)]
    if any(len({comparison_key(value) for value in values}) > 1 for values in candidate_sets):
        reasons.append("存在待人工选择的候选")
    if block.detail_candidate_regions:
        reasons.append("原图细查发现候选")
    if block.kind == "table" and (not block.cells or block.confidence is None):
        reasons.append("表格结构需核对")
    if block.needs_review and not reasons:
        reasons.append("识别结果需复核")
    return tuple(reasons)


def collect_review_items(
    jobs: list[ImageJob], *, threshold: float = LOW_CONFIDENCE_THRESHOLD,
    sort_by: str = 'page', category: str = 'all', include_confirmed: bool = False,
) -> list[ReviewItem]:
    """Collect reviewable text/table blocks in queue and reading order."""
    items: list[ReviewItem] = []
    for page_number, job in enumerate(jobs, start=1):
        if job.result is None:
            continue
        from .export_checks import inspect_page
        issues = inspect_page(job.result)
        for block in job.result.blocks:
            if block.kind == "image" and not block.text and not block.cells:
                continue
            reasons = list(_review_reasons(block, threshold))
            categories = []
            if any(reason.startswith('低置信度') for reason in reasons):
                categories.append('low')
            if any('候选' in reason or '多次识别' in reason for reason in reasons):
                categories.append('conflict')
            if block.recognition_source == 'recovered' or ':recovered-' in block.id:
                reasons.append('漏行检查补回的文字')
                categories.append('recovered')
            if block.kind == 'table' and reasons:
                categories.append('table')
            if 'paper_edge' in block.review_tags:
                reasons.append('文字位于建议纸面边缘')
                categories.append('boundary')
            related = [issue for issue in issues if block.id in issue.block_ids]
            for issue in related:
                reasons.append(issue.message)
                categories.append('overlap' if issue.code == 'recovery_overlap' else issue.code)
            if not reasons:
                continue
            if not categories:
                categories.append('other')
            value = asdict(block)
            value.pop('review_confirmed_signature', None)
            neighbors = {bid for issue in related for bid in issue.block_ids if bid != block.id}
            context = [(b.id, b.bbox, b.text, [(line.bbox, line.text) for line in b.text_lines])
                       for b in job.result.blocks if b.id in neighbors]
            fingerprint = hashlib.sha256(json.dumps([value, reasons, context], ensure_ascii=False,
                sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
            confirmed = block.review_confirmed_signature == fingerprint
            if (confirmed and not include_confirmed) or (category != 'all' and category not in categories):
                continue
            risk = max([issue.severity*100 for issue in related] + [
                180 if 'recovered' in categories else 0,
                160 if 'conflict' in categories else 0,
                100 + round((1-min(v if v is not None else 1 for v in
                                  (block.confidence, block.minimum_line_confidence)))*90)
                if 'low' in categories else 0])
            items.append(
                ReviewItem(
                    job_id=job.id,
                    page_number=page_number,
                    processed_image_path=job.result.processed_image_path,
                    block=block,
                    reasons=tuple(reasons), categories=tuple(dict.fromkeys(categories)),
                    risk=risk, fingerprint=fingerprint, confirmed=confirmed,
                )
            )
    if sort_by == 'risk':
        items.sort(key=lambda item: (-item.risk, item.page_number))
    return items


def confirm_review(jobs: list[ImageJob], job_id: str, block_id: str) -> bool:
    for item in collect_review_items(jobs, include_confirmed=True):
        if item.job_id == job_id and item.block.id == block_id:
            item.block.review_confirmed_signature = item.fingerprint
            return True
    return False


class ReviewIndex:
    """Cache expensive page analysis; owners invalidate after in-place edits.

    Result replacement and queue changes are detected without serializing text.
    A page is the invalidation unit because geometry checks involve neighbors.
    """
    def __init__(self):
        self._pages = {}

    def invalidate(self, job_id):
        self._pages.pop(job_id, None)

    def clear(self):
        self._pages.clear()

    def page_items(self, job):
        entry = self._pages.get(job.id)
        if entry is None or entry[0] is not job.result:
            entry = (job.result, collect_review_items([job], include_confirmed=True))
            self._pages[job.id] = entry
        return entry[1]

    def collect(self, jobs, *, sort_by='page', category='all', include_confirmed=False):
        live = {job.id for job in jobs}
        for key in self._pages.keys() - live:
            self._pages.pop(key)
        items = []
        for number, job in enumerate(jobs, 1):
            for item in self.page_items(job):
                confirmed = item.block.review_confirmed_signature == item.fingerprint
                if confirmed and not include_confirmed:
                    continue
                if category != 'all' and category not in item.categories:
                    continue
                items.append(replace(item, page_number=number, confirmed=confirmed)
                             if item.page_number != number or item.confirmed != confirmed else item)
        if sort_by == 'risk':
            items.sort(key=lambda item: (-item.risk, item.page_number))
        return items

    def confirm(self, jobs, job_id, block_id):
        job = next((job for job in jobs if job.id == job_id), None)
        if job:
            for item in self.page_items(job):
                if item.block.id == block_id:
                    item.block.review_confirmed_signature = item.fingerprint
                    return True
        return False
