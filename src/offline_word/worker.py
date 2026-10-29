"""Isolated, cancellable PP-StructureV3 execution over an anonymous pipe."""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import re
import sys
from collections.abc import Mapping
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from .accuracy import DetailResult, DetailTarget, apply_detail_results, detail_targets
from .domain import Block, Cell, ImageJob, PageResult, TextLine, normalize_table_cells, table_needs_review
from .images import prepare_image, prepare_image_with_detail
from .diagnostics import CURRENT_PROBE, measured
from .line_geometry import complete_line_geometry, same_printed_line
from .proofread import comparison_key, reconcile_page
from .nontext import NonTextImage, bounds as polygon_bounds, covered_by_nontext, filter_nontext, mask_nontext
from .security import (
    OfflineError,
    assert_local_path,
    claim_session_lease,
    configure_offline_env,
    cpu_thread_limit,
    deny_network,
    validate_models,
)


_MODEL_OPTIONS = {
    "layout_detection": "PP-DocLayout_plus-L",
    "doc_orientation_classify": "PP-LCNet_x1_0_doc_ori",
    "text_detection": "PP-OCRv6_medium_det",
    "textline_orientation": "PP-LCNet_x1_0_textline_ori",
    "text_recognition": "PP-OCRv6_medium_rec",
    "table_classification": "PP-LCNet_x1_0_table_cls",
    "wired_table_structure_recognition": "SLANeXt_wired",
    "wireless_table_structure_recognition": "SLANet_plus",
    "wired_table_cells_detection": "RT-DETR-L_wired_table_cell_det",
    "wireless_table_cells_detection": "RT-DETR-L_wireless_table_cell_det",
    # PaddleX 3.7's stock PP-StructureV3 config reuses the document
    # orientation model here; the manifest therefore needs no 11th model tree.
    "table_orientation_classify": "PP-LCNet_x1_0_doc_ori",
}
_LOW_CONFIDENCE = 0.90
_DETAIL_MAX_SIDE = 2_048
_WINDOWS_DETECTION_MAX_SIDE = 2_560


def native_windows_path(
    value: Path | str,
    *,
    platform: str | None = None,
    kernel32: Any | None = None,
) -> str:
    """Use an existing 8.3 alias when a Windows native library sees Unicode."""
    original = str(value)
    if (platform or os.name) != "nt" or original.isascii():
        return original
    try:
        import ctypes

        api = kernel32 or ctypes.windll.kernel32
        buffer = ctypes.create_unicode_buffer(32_768)
        length = int(api.GetShortPathNameW(original, buffer, len(buffer)))
        shortened = buffer.value if 0 < length < len(buffer) else ""
        if shortened and shortened.isascii():
            return shortened
    except Exception:
        pass
    return original


def _windows_cpu_options(*, safe_cpu: bool) -> dict[str, object]:
    """Keep compatible oneDNN convolution fusions without broken PIR placement.

    Paddle 3.3 cannot convert DoubleAttribute arrays used by interpolation.
    Disabling only its placement pass leaves those operators on ordinary CPU
    kernels while retaining convolution fusions. No model/precision reduction.
    """
    if safe_cpu:
        return {"enable_mkldnn": False}
    return {
        "enable_mkldnn": True,
        "engine": "paddle_static",
        "engine_config": {"run_mode": "mkldnn", "cpu_threads": cpu_thread_limit(platform="nt"),
                          "mkldnn_cache_capacity": 1, "delete_pass": ["onednn_placement_pass"]},
    }


def pipeline_options(model_root: Path, *, platform: str | None = None, safe_cpu: bool = False) -> dict[str, object]:
    """Return the complete explicit-local PPStructureV3 configuration.

    ``ocr_version`` is intentionally absent: PaddleOCR 3.7.0 rejects v6 there,
    while the local directories/names are still supplied directly to every main
    and nested table OCR submodule.
    """
    root = assert_local_path(model_root, "remote_model_path")
    native_root = native_windows_path(root, platform=platform)
    model_dir = lambda name: str(Path(native_root) / name)
    options: dict[str, object] = {
        "device": "cpu",
        "cpu_threads": cpu_thread_limit(platform=platform),
        "mkldnn_cache_capacity": 1,
        "layout_detection_model_name": _MODEL_OPTIONS["layout_detection"],
        "layout_detection_model_dir": model_dir(_MODEL_OPTIONS["layout_detection"]),
        "doc_orientation_classify_model_name": _MODEL_OPTIONS["doc_orientation_classify"],
        "doc_orientation_classify_model_dir": model_dir(_MODEL_OPTIONS["doc_orientation_classify"]),
        "text_detection_model_name": _MODEL_OPTIONS["text_detection"],
        "text_detection_model_dir": model_dir(_MODEL_OPTIONS["text_detection"]),
        "textline_orientation_model_name": _MODEL_OPTIONS["textline_orientation"],
        "textline_orientation_model_dir": model_dir(_MODEL_OPTIONS["textline_orientation"]),
        "textline_orientation_batch_size": 1,
        "text_recognition_model_name": _MODEL_OPTIONS["text_recognition"],
        "text_recognition_model_dir": model_dir(_MODEL_OPTIONS["text_recognition"]),
        "text_recognition_batch_size": 1,
        "table_classification_model_name": _MODEL_OPTIONS["table_classification"],
        "table_classification_model_dir": model_dir(_MODEL_OPTIONS["table_classification"]),
        "wired_table_structure_recognition_model_name": _MODEL_OPTIONS["wired_table_structure_recognition"],
        "wired_table_structure_recognition_model_dir": model_dir(_MODEL_OPTIONS["wired_table_structure_recognition"]),
        "wireless_table_structure_recognition_model_name": _MODEL_OPTIONS["wireless_table_structure_recognition"],
        "wireless_table_structure_recognition_model_dir": model_dir(_MODEL_OPTIONS["wireless_table_structure_recognition"]),
        "wired_table_cells_detection_model_name": _MODEL_OPTIONS["wired_table_cells_detection"],
        "wired_table_cells_detection_model_dir": model_dir(_MODEL_OPTIONS["wired_table_cells_detection"]),
        "wireless_table_cells_detection_model_name": _MODEL_OPTIONS["wireless_table_cells_detection"],
        "wireless_table_cells_detection_model_dir": model_dir(_MODEL_OPTIONS["wireless_table_cells_detection"]),
        "table_orientation_classify_model_name": _MODEL_OPTIONS["table_orientation_classify"],
        "table_orientation_classify_model_dir": model_dir(_MODEL_OPTIONS["table_orientation_classify"]),
        "use_doc_orientation_classify": True,
        "use_doc_unwarping": False,
        "use_textline_orientation": True,
        "use_table_recognition": True,
        "use_formula_recognition": False,
        "use_chart_recognition": False,
        "use_seal_recognition": False,
        "use_region_detection": False,
    }
    if (platform or os.name) == "nt":
        options.update(_windows_cpu_options(safe_cpu=safe_cpu))
        # Bound only the detector tensor. PaddleX maps its boxes back to the
        # full input, from which the recognizer still crops sharp text lines.
        options["text_det_limit_side_len"] = _WINDOWS_DETECTION_MAX_SIDE
        options["text_det_limit_type"] = "max"
    return options


def plain_ocr_options(model_root: Path, *, platform: str | None = None, safe_cpu: bool = False) -> dict[str, object]:
    """Return a fully local text-only fallback using the same PP-OCRv6 models."""
    root = assert_local_path(model_root, "remote_model_path")
    native_root = native_windows_path(root, platform=platform)
    model_dir = lambda name: str(Path(native_root) / name)
    options: dict[str, object] = {
        "device": "cpu",
        "cpu_threads": cpu_thread_limit(platform=platform),
        "doc_orientation_classify_model_name": _MODEL_OPTIONS["doc_orientation_classify"],
        "doc_orientation_classify_model_dir": model_dir(_MODEL_OPTIONS["doc_orientation_classify"]),
        "text_detection_model_name": _MODEL_OPTIONS["text_detection"],
        "text_detection_model_dir": model_dir(_MODEL_OPTIONS["text_detection"]),
        "textline_orientation_model_name": _MODEL_OPTIONS["textline_orientation"],
        "textline_orientation_model_dir": model_dir(_MODEL_OPTIONS["textline_orientation"]),
        "textline_orientation_batch_size": 1,
        "text_recognition_model_name": _MODEL_OPTIONS["text_recognition"],
        "text_recognition_model_dir": model_dir(_MODEL_OPTIONS["text_recognition"]),
        "text_recognition_batch_size": 1,
        "use_doc_orientation_classify": True,
        "use_doc_unwarping": False,
        "use_textline_orientation": True,
    }
    if (platform or os.name) == "nt":
        options.update(_windows_cpu_options(safe_cpu=safe_cpu))
        options["text_det_limit_side_len"] = _WINDOWS_DETECTION_MAX_SIDE
        options["text_det_limit_type"] = "max"
    return options


def detail_recognition_options(model_root: Path, *, platform: str | None = None, safe_cpu: bool = False) -> dict[str, object]:
    """Bind the optional line-detail pass to the bundled recognition model."""
    root = assert_local_path(model_root, "remote_model_path")
    native_root = native_windows_path(root, platform=platform)
    options: dict[str, object] = {
        "model_name": _MODEL_OPTIONS["text_recognition"],
        "model_dir": str(Path(native_root) / _MODEL_OPTIONS["text_recognition"]),
        "device": "cpu",
        "cpu_threads": cpu_thread_limit(platform=platform),
        "mkldnn_cache_capacity": 1,
    }
    if (platform or os.name) == "nt":
        options.update(_windows_cpu_options(safe_cpu=safe_cpu))
    return options


def _native(value: Any) -> Any:
    if hasattr(value, "tolist"):
        return _native(value.tolist())
    if isinstance(value, Mapping):
        return {str(key): _native(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_native(item) for item in value]
    if hasattr(value, "item"):
        try:
            return value.item()
        except ValueError:
            pass
    return value


def _prediction_data(prediction: Any) -> dict[str, Any]:
    # PaddleX raw mappings hold LayoutBlock objects; its ``.json`` view turns
    # them into plain content/bbox dictionaries wrapped in ``{"res": ...}``.
    # The preprocessor preview is read from the raw mapping before this helper.
    if isinstance(prediction, Mapping) and hasattr(prediction, "json"):
        raw = prediction.json
    else:
        raw = dict(prediction) if isinstance(prediction, Mapping) else getattr(prediction, "json", prediction)
    if callable(raw):
        raw = raw()
    if isinstance(raw, dict) and isinstance(raw.get("doc_preprocessor_res"), dict):
        raw = dict(raw)
        preprocessor = dict(raw["doc_preprocessor_res"])
        # These images can be multi-megapixel arrays.  The caller that needs the
        # output preview reads it directly before normalising the JSON payload.
        for key in ("input_img", "rot_img", "output_img"):
            preprocessor.pop(key, None)
        raw["doc_preprocessor_res"] = preprocessor
        raw.pop("imgs_in_doc", None)
    raw = _native(raw)
    if isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, dict) and isinstance(raw.get("res"), dict):
        raw = raw["res"]
    if not isinstance(raw, dict):
        raise ValueError("invalid_prediction")
    return raw


def _bbox(value: Any) -> tuple[float, float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return (0.0, 0.0, 0.0, 0.0)
    try:
        coordinates = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0, 0.0)
    return coordinates  # type: ignore[return-value]


def _kind(label: object) -> str:
    normalized = str(label).casefold()
    if normalized in {"title", "doc_title", "paragraph_title"} or normalized.endswith("_title"):
        return "title"
    if "table" in normalized:
        return "table"
    if normalized in {"image", "figure", "chart"} or "image" in normalized or "figure" in normalized:
        return "image"
    return "text"


class _TableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cells: list[Cell] = []
        self._row = -1
        self._next_col = 0
        self._occupied: set[tuple[int, int]] = set()
        self._current: dict[str, Any] | None = None

    @staticmethod
    def _span(attributes: dict[str, str | None], key: str) -> int:
        try:
            return max(1, int(attributes.get(key) or "1"))
        except ValueError:
            return 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag == "tr":
            self._row += 1
            self._next_col = 0
        elif tag in {"td", "th"} and self._row >= 0 and self._current is None:
            while (self._row, self._next_col) in self._occupied:
                self._next_col += 1
            attributes = dict(attrs)
            self._current = {
                "row": self._row,
                "col": self._next_col,
                "rowspan": self._span(attributes, "rowspan"),
                "colspan": self._span(attributes, "colspan"),
                "parts": [],
            }

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current["parts"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() not in {"td", "th"} or self._current is None:
            return
        current = self._current
        text = re.sub(r"\s+", " ", "".join(current["parts"])).strip()
        cell = Cell(
            row=current["row"], col=current["col"], original_text=text,
            rowspan=current["rowspan"], colspan=current["colspan"],
        )
        self.cells.append(cell)
        for row in range(cell.row, cell.row + cell.rowspan):
            for col in range(cell.col, cell.col + cell.colspan):
                self._occupied.add((row, col))
        self._next_col = cell.col + cell.colspan
        self._current = None


def _table_cells(table_result: Any) -> tuple[list[Cell], int, int]:
    if not isinstance(table_result, dict):
        return [], 0, 0
    html = table_result.get("pred_html", "")
    parser = _TableHTMLParser()
    if isinstance(html, str):
        try:
            parser.feed(html)
            parser.close()
        except Exception:
            return [], 0, 0
    cells = parser.cells
    # ``pred_html`` is PP-Structure's table structure result.  Raw OCR lines
    # have neither a cell mapping nor a stable order for merged/empty cells, so
    # equal counts are not evidence that they can replace editable HTML text.
    rows = max((cell.row + cell.rowspan for cell in cells), default=0)
    cols = max((cell.col + cell.colspan for cell in cells), default=0)
    return normalize_table_cells(cells, rows, cols), rows, cols


def _matched_confidences(bbox: tuple[float, float, float, float], overall: Any) -> list[float]:
    if not isinstance(overall, dict):
        return []
    boxes, scores = overall.get("rec_boxes"), overall.get("rec_scores")
    if not isinstance(boxes, list) or not isinstance(scores, list):
        return []
    left, top, right, bottom = bbox
    values: list[float] = []
    for line_bbox, score in zip(boxes, scores):
        if not isinstance(line_bbox, (list, tuple)) or len(line_bbox) != 4:
            continue
        try:
            line_left, line_top, line_right, line_bottom = (float(value) for value in line_bbox)
            confidence = float(score)
        except (TypeError, ValueError):
            continue
        overlap_width = max(0.0, min(right, line_right) - max(left, line_left))
        overlap_height = max(0.0, min(bottom, line_bottom) - max(top, line_top))
        center_inside = left <= (line_left + line_right) / 2 <= right and top <= (line_top + line_bottom) / 2 <= bottom
        if center_inside or overlap_width * overlap_height > 0:
            values.append(confidence)
    return values


def _confidence_for_bbox(bbox: tuple[float, float, float, float], overall: Any) -> float | None:
    values = _matched_confidences(bbox, overall)
    return sum(values) / len(values) if values else None


def _mean_confidence(cells: list[Cell]) -> float | None:
    scores = [cell.confidence for cell in cells if cell.confidence is not None]
    return sum(scores) / len(scores) if scores else None


def _apply_bbox_confidence(block: Block, overall: Any) -> None:
    """Expose the average confidence while reviewing any weak matched line."""
    values = _matched_confidences(block.bbox, overall)
    if values:
        block.confidence = sum(values) / len(values)
        block.minimum_line_confidence = min(values)
        block.needs_review = block.needs_review or min(values) <= _LOW_CONFIDENCE


def _overall_lines(overall: Any) -> list[tuple[str, tuple[float, float, float, float] | None, float | None]]:
    if not isinstance(overall, dict) or not isinstance(overall.get("rec_texts"), list):
        return []
    boxes = overall.get("rec_boxes")
    scores = overall.get("rec_scores")
    lines: list[tuple[str, tuple[float, float, float, float] | None, float | None]] = []
    for index, value in enumerate(overall["rec_texts"]):
        text = str(value).strip()
        if not text:
            continue
        bbox: tuple[float, float, float, float] | None = None
        if isinstance(boxes, list) and index < len(boxes) and isinstance(boxes[index], (list, tuple)) and len(boxes[index]) == 4:
            bbox = _bbox(boxes[index])
        confidence: float | None = None
        if isinstance(scores, list) and index < len(scores):
            try:
                confidence = float(scores[index])
            except (TypeError, ValueError):
                pass
        lines.append((text, bbox, confidence))
    return lines


def _text_lines_for_block(
    text: str,
    overall: Any,
    region: tuple[float, float, float, float],
) -> list[TextLine]:
    """Match text only inside its own layout region, including repeated digits."""
    block_key = comparison_key(text)
    left, top, right, bottom = region
    return [
        TextLine(line_text, bbox, confidence)
        for line_text, bbox, confidence in _overall_lines(overall)
        if bbox is not None and comparison_key(line_text) in block_key
        and left <= (bbox[0] + bbox[2]) / 2 <= right
        and top <= (bbox[1] + bbox[3]) / 2 <= bottom
    ]


def _tight_bbox_for_lines(lines: list[TextLine]) -> tuple[float, float, float, float] | None:
    matched = [line.bbox for line in lines]
    if not matched:
        return None
    return (
        min(bbox[0] for bbox in matched),
        min(bbox[1] for bbox in matched),
        max(bbox[2] for bbox in matched),
        max(bbox[3] for bbox in matched),
    )


def _looks_like_edge_noise(
    text: str,
    bbox: tuple[float, float, float, float],
    confidence: float | None,
    width: int,
    height: int,
) -> bool:
    """Reject a tiny weak fragment clipped by the outer page boundary."""
    if confidence is None or confidence > 0.70 or len(comparison_key(text)) > 2:
        return False
    left, top, right, bottom = bbox
    touches_edge = left <= width * 0.015 or right >= width * 0.985 or top <= height * 0.015 or bottom >= height * 0.985
    small = (right - left) <= width * 0.04 and (bottom - top) <= height * 0.04
    return touches_edge and small


def _line_is_covered(
    text: str,
    bbox: tuple[float, float, float, float],
    blocks: list[Block],
) -> bool:
    left, top, right, bottom = bbox
    center_x, center_y = (left + right) / 2, (top + bottom) / 2
    for block in blocks:
        block_left, block_top, block_right, block_bottom = block.bbox
        if block_left <= center_x <= block_right and block_top <= center_y <= block_bottom:
            if block.kind == "image":
                return True
            represented = (
                "\n".join(cell.original_text for cell in block.cells)
                if block.kind == "table" and block.cells
                else block.original_text
            )
            needle = comparison_key(text)
            if needle and needle in comparison_key(represented):
                return True
    return False


def _insert_fallback_blocks(blocks: list[Block], fallback: list[Block]) -> list[Block]:
    """Insert missing OCR lines by position without reordering detected blocks."""
    ordered = list(blocks)
    for candidate in sorted(fallback, key=lambda block: (block.bbox[1], block.bbox[0])):
        candidate_key = ((candidate.bbox[1] + candidate.bbox[3]) / 2, candidate.bbox[0])
        insert_at = len(ordered)
        for index, existing in enumerate(ordered):
            existing_key = ((existing.bbox[1] + existing.bbox[3]) / 2, existing.bbox[0])
            if existing_key > candidate_key:
                insert_at = index
                break
        ordered.insert(insert_at, candidate)
    return ordered


def page_from_prediction(job: ImageJob, processed_path: Path, prediction: Any) -> PageResult:
    """Map documented PP-StructureV3 JSON into editable, processed-image blocks."""
    raw = _prediction_data(prediction)
    with Image.open(processed_path) as image:
        width, height = image.size
    overall = raw.get("overall_ocr_res")
    blocks: list[Block] = []
    parsed = raw.get("parsing_res_list")
    if not isinstance(parsed, list):
        parsed = []
    for index, entry in enumerate(parsed):
        if not isinstance(entry, dict):
            continue
        kind = _kind(entry.get("block_label"))
        block = Block(
            id=f"{job.id}:{entry.get('block_id', index)}",
            kind=kind,
            bbox=_bbox(entry.get("block_bbox")),
            original_text=str(entry.get("block_content") or ""),
        )
        block.text_lines = _text_lines_for_block(block.original_text, overall, block.bbox) if kind not in {"table", "image"} else []
        tight_bbox = _tight_bbox_for_lines(block.text_lines)
        if kind not in {"table", "image"} and tight_bbox is not None and complete_line_geometry(block):
            block.bbox = tight_bbox
        _apply_bbox_confidence(block, overall)
        if _looks_like_edge_noise(
            block.original_text, block.bbox, block.confidence, width, height
        ):
            continue
        if kind == "table":
            # Parsing blocks retain the authoritative HTML.  Never associate an
            # auxiliary result by its list position: PP-Structure can reorder it.
            block.cells, block.rows, block.cols = _table_cells({"pred_html": block.original_text})
            if not block.cells or table_needs_review(block.cells):
                block.needs_review = True
            if block.confidence is None:
                block.confidence = _mean_confidence(block.cells)
            from .table_geometry import attach_table_geometry
            attach_table_geometry(block, raw.get("table_res_list"))
            block.text_lines = _text_lines_for_block(
                "\n".join(cell.original_text for cell in block.cells), overall, block.bbox,
            )
        blocks.append(block)
    fallback: list[Block] = []
    for line_index, (text, bbox, confidence) in enumerate(_overall_lines(overall)):
        if bbox is None or _line_is_covered(text, bbox, blocks):
            continue
        if _looks_like_edge_noise(text, bbox, confidence, width, height):
            continue
        fallback.append(Block(
            id=f"{job.id}:ocr-{line_index}", kind="text", bbox=bbox, original_text=text,
            confidence=confidence, needs_review=confidence is not None and confidence <= _LOW_CONFIDENCE,
            minimum_line_confidence=confidence, text_lines=[TextLine(text, bbox, confidence)],
        ))
    if fallback:
        blocks = _insert_fallback_blocks(blocks, fallback)
    has_line_coordinates = any(bbox is not None for _text, bbox, _confidence in _overall_lines(overall))
    if not blocks and not has_line_coordinates and isinstance(overall, dict) and isinstance(overall.get("rec_texts"), list):
        # Older/partial payloads may contain text but no line coordinates.
        fallback_bbox = (0.0, 0.0, float(width), float(height))
        text = "\n".join(map(str, overall["rec_texts"])).strip()
        if text:
            block = Block(
                id=f"{job.id}:text", kind="text", bbox=fallback_bbox,
                original_text=text,
            )
            _apply_bbox_confidence(block, overall)
            blocks.append(block)
    return filter_nontext(PageResult(id=job.id, page_index=job.frame_index, width=width, height=height, processed_image_path=str(processed_path), blocks=blocks))


def page_from_plain_prediction(job: ImageJob, processed_path: Path, prediction: Any) -> PageResult:
    """Map text-only PaddleOCR output while clearly flagging lost layout."""
    raw = _prediction_data(prediction)
    overall = raw.get("overall_ocr_res") if isinstance(raw.get("overall_ocr_res"), dict) else raw
    with Image.open(processed_path) as image:
        width, height = image.size
    blocks: list[Block] = []
    for index, (text, bbox, confidence) in enumerate(_overall_lines(overall)):
        if bbox is not None and _looks_like_edge_noise(text, bbox, confidence, width, height):
            continue
        blocks.append(Block(
            id=f"{job.id}:plain-{index}",
            kind="text",
            bbox=bbox or (0.0, 0.0, float(width), float(height)),
            original_text=text,
            confidence=confidence,
            needs_review=not job.text_only or confidence is None or confidence < 0.90,
            minimum_line_confidence=confidence,
            text_lines=[TextLine(text, bbox, confidence)] if bbox is not None else [],
        ))
    if not blocks:
        texts = overall.get("rec_texts") if isinstance(overall, dict) else None
        text = "\n".join(str(value) for value in texts or [] if str(value)).strip()
        if text:
            blocks.append(Block(
                id=f"{job.id}:plain",
                kind="text",
                bbox=(0.0, 0.0, float(width), float(height)),
                original_text=text,
                needs_review=True,
            ))
    return filter_nontext(PageResult(
        id=job.id,
        page_index=job.frame_index,
        width=width,
        height=height,
        processed_image_path=str(processed_path),
        blocks=blocks,
        recognition_warning=None if job.text_only else "layout_fallback",
        text_only=job.text_only,
    ))


def _page_character_count(page: PageResult) -> int:
    values: list[str] = []
    for block in page.blocks:
        if block.kind == "table" and block.cells:
            values.extend(cell.original_text for cell in block.cells)
        elif block.kind != "image":
            values.append(block.original_text)
    return sum(len(comparison_key(value)) for value in values)


def _needs_text_completeness_check(page: PageResult) -> bool:
    """Detect a short orphan that commonly signals a silently dropped paragraph."""
    if page.height <= 0 or _page_character_count(page) < 50:
        return False
    for block in page.blocks:
        value = comparison_key(block.original_text)
        if block.kind not in {"text", "title"} or not 0 < len(value) <= 2:
            continue
        if block.bbox[1] < page.height * 0.65 and any(character.isalnum() for character in value):
            return True
    return False


def _prefer_complete_plain_page(baseline: PageResult, plain: PageResult) -> PageResult:
    """Use an explicit text-only recovery only when it restores substantial content."""
    baseline_chars = _page_character_count(baseline)
    plain_chars = _page_character_count(plain)
    required_gain = max(20, round(baseline_chars * 0.03))
    if plain_chars < baseline_chars + required_gain:
        return baseline
    plain.processed_image_path = baseline.processed_image_path
    plain.recognition_warning = "text_recovery"
    for block in plain.blocks:
        block.needs_review = True
        for cell in block.cells:
            cell.needs_review = True
    return plain


def _missing_line_regions(
    page: PageResult,
    image_path: Path,
    *,
    recognized_line_boxes: list[tuple[float, float, float, float]] | None = None,
    cropped_region: bool = False,
) -> list[tuple[float, float, float, float]]:
    """Find printed line bands that are absent from the parsed OCR blocks.

    Both recognition modes use a grayscale ink projection to find uncovered
    bands. Figures and tables remain owned by the layout parser.
    """
    import numpy as np

    with Image.open(image_path) as source:
        gray = np.asarray(mask_nontext(source.convert("L"), page.nontext_regions))
    height, width = gray.shape
    if height < 40 or width < 40:
        return []
    roi_left, roi_right = round(width * 0.08), round(width * 0.92)
    ink = gray[:, roi_left:roi_right] < 180
    row_threshold = max(8, round(ink.shape[1] * 0.007))
    active = ink.sum(axis=1) >= row_threshold
    raw_bands: list[tuple[int, int]] = []
    start: int | None = None
    for y, enabled in enumerate(active.tolist()):
        if enabled and start is None:
            start = y
        elif not enabled and start is not None:
            raw_bands.append((start, y - 1))
            start = None
    if start is not None:
        raw_bands.append((start, height - 1))

    merge_gap = max(3, round(height * 0.003))
    bands: list[tuple[int, int]] = []
    for top, bottom in raw_bands:
        if bands and top - bands[-1][1] <= merge_gap:
            bands[-1] = (bands[-1][0], bottom)
        else:
            bands.append((top, bottom))

    minimum_height = max(3, round(height * 0.002))
    # A single line can occupy much of a user selection's height. Whole-page
    # limits would reject precisely the large, clearly printed missed lines.
    maximum_height = max(20, round(height * (0.5 if cropped_region else 0.06)))
    x_padding = max(6, round(width * 0.015))
    y_padding = max(4, round(height * 0.004))
    regions: list[tuple[float, float, float, float]] = []
    for top, bottom in bands:
        band_height = bottom - top + 1
        if not minimum_height <= band_height <= maximum_height:
            continue
        center_y = (top + bottom) / 2
        if center_y < height * 0.04 or center_y > height * 0.96:
            continue
        band_ink = ink[top:bottom + 1]
        active_columns = np.flatnonzero(band_ink.sum(axis=0) >= max(1, round(band_height * 0.05)))
        if active_columns.size == 0:
            continue
        left = max(0, roi_left + int(active_columns[0]) - x_padding)
        right = min(width, roi_left + int(active_columns[-1]) + x_padding + 1)
        if right - left < width * 0.08:
            continue
        line_width = right - left
        covered = any(
            block.kind in {"image", "table"}
            and not covered_by_nontext(block.bbox, page.nontext_regions, threshold=.72)
            and block.bbox[1] <= center_y <= block.bbox[3]
            and max(0.0, min(right, block.bbox[2]) - max(left, block.bbox[0])) >= line_width * 0.25
            for block in page.blocks
        )
        if covered:
            continue
        coverage_boxes = recognized_line_boxes
        if coverage_boxes is None:
            coverage_boxes = [
                block.bbox for block in page.blocks
                if block.kind not in {"image", "table"} and comparison_key(block.original_text)
            ]
        for block_left, block_top, block_right, block_bottom in coverage_boxes:
            overlap_width = max(0.0, min(right, block_right) - max(left, block_left))
            coverage_height = max(0.0, block_bottom - block_top)
            if (
                coverage_height <= band_height * 3.5
                and block_top <= center_y <= block_bottom
                and overlap_width >= line_width * 0.25
            ):
                covered = True
                break
        if not covered:
            region = (
                float(left),
                float(max(0, top - y_padding)),
                float(right),
                float(min(height, bottom + y_padding + 1)),
            )
            # An uncovered decorative rule is ink, but is not a missing line.
            if not NonTextImage(gray, 1, []).is_ornament(region):
                regions.append(region)
    return regions


def _recovery_already_represented(blocks: list[Block], text: str, region: tuple) -> bool:
    """One physical line has one owner, even if OCR gives it two spellings."""
    from difflib import SequenceMatcher

    key = comparison_key(text)
    left, top, right, bottom = region
    for block in blocks:
        if block.kind not in {"text", "title"}:
            continue
        for line in block.text_lines:
            if not same_printed_line(line.bbox, region):
                continue
            existing = comparison_key(line.text)
            identical = existing == key
            close = min(len(existing), len(key)) >= 6 and SequenceMatcher(None, existing, key).ratio() >= .85
            if identical or close:
                if close and not identical:
                    # Keep the original baseline; expose a different spelling
                    # as a review candidate, never another floating paragraph.
                    block.needs_review = True
                    if block.original_text.count(line.text) == 1:
                        candidate = block.original_text.replace(line.text, text, 1)
                        for value in (block.text, candidate):
                            if value not in block.candidates:
                                block.candidates.append(value)
                return True
        if complete_line_geometry(block):
            # Identical sentences at distinct physical positions are real
            # repetitions, not duplicate detections.
            continue
        block_key = comparison_key(block.original_text)
        represented_occurrences = sum(comparison_key(line.text) == key for line in block.text_lines)
        if (block_key.count(key) > represented_occurrences
                and block.bbox[1] <= (top+bottom)/2 <= block.bbox[3]
                and min(right, block.bbox[2])-max(left, block.bbox[0]) >= (right-left)*.5):
            block.text_lines.append(TextLine(text, region))
            if complete_line_geometry(block):
                block.bbox = _tight_bbox_for_lines(block.text_lines)
            return True
    return False


def _recover_missing_lines(
    page: PageResult,
    image_path: Path,
    regions: list[tuple[float, float, float, float]],
    runtime: Any,
    session_dir: Path,
) -> int:
    """Recognize uncovered line bands and insert reviewable editable blocks."""
    if not regions:
        return 0
    recovered: list[Block] = []
    safe_page_id = re.sub(r"[^a-zA-Z0-9_-]", "_", page.id)
    target_dir = session_dir / "processed" / "integrity"
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with Image.open(image_path) as source:
        for index, region in enumerate(regions):
            left, top, right, bottom = (round(value) for value in region)
            crop = source.crop((left, top, right, bottom))
            if page.nontext_regions:
                crop = mask_nontext(crop.convert('RGB'), page.nontext_regions, origin=(left, top))
            crop = ImageOps.expand(crop, border=max(4, round(crop.height * 0.12)), fill="white")
            target = target_dir / f"{safe_page_id}-line-{index}.png"
            crop.save(target, format="PNG")
            try:
                result = runtime.predict_detail(target)
            except Exception:
                continue
            text = str(result.text).strip()
            text_key = comparison_key(text)
            if len(text_key) < 2:
                continue
            if _recovery_already_represented([*page.blocks, *recovered], text, region):
                continue
            recovered.append(Block(
                id=f"{page.id}:recovered-{index}",
                recognition_source="recovered",
                kind="text",
                bbox=region,
                original_text=text,
                confidence=result.confidence,
                needs_review=True,
                minimum_line_confidence=result.confidence,
                text_lines=[TextLine(text, region, result.confidence)],
            ))
    if not recovered:
        return 0
    page.blocks = _insert_fallback_blocks(page.blocks, recovered)
    # Recovering text cannot restore table structure lost by the plain engine.
    if page.recognition_warning != "layout_fallback":
        page.recognition_warning = "text_recovery"
    return len(recovered)


def _sync_processed_preview(prediction: Any, processed_path: Path) -> Path:
    """Persist PPStructure's orientation-corrected image for bbox/preview parity."""
    if not isinstance(prediction, dict):
        return processed_path
    preprocessor = prediction.get("doc_preprocessor_res")
    if not isinstance(preprocessor, dict):
        return processed_path
    output = preprocessor.get("output_img")
    angle = preprocessor.get("angle", -1)
    if output is None:
        if angle not in {-1, 0, None}:
            raise OfflineError("processed_preview_unavailable")
        return processed_path
    try:
        shape = output.shape
        if len(shape) == 2:
            preview = Image.fromarray(output)
        elif len(shape) == 3 and shape[2] == 3:
            # PaddleX uses OpenCV BGR arrays in its document preprocessor.
            preview = Image.fromarray(output[:, :, ::-1])
        else:
            raise ValueError
        temporary = processed_path.with_suffix(".pipeline.tmp.png")
        preview.save(temporary, format="PNG")
        temporary.chmod(0o600)
        temporary.replace(processed_path)
    except Exception:
        raise OfflineError("processed_preview_unavailable") from None
    return processed_path


def region_candidates_from_prediction(prediction: Any) -> list[str]:
    """Return candidate strings for an entire selected region, never one line each."""
    raw = _prediction_data(prediction)
    overall = raw.get("overall_ocr_res") if isinstance(raw.get("overall_ocr_res"), dict) else raw
    if not isinstance(overall, dict) or not isinstance(overall.get("rec_texts"), list):
        return []
    candidate = "\n".join(str(text) for text in overall["rec_texts"] if str(text)).strip()
    return [candidate] if candidate else []


def _detail_result_from_prediction(prediction: Any) -> DetailResult:
    raw = _prediction_data(prediction)
    text = str(raw.get("rec_text") or "").strip()
    confidence: float | None = None
    try:
        if raw.get("rec_score") is not None:
            confidence = float(raw["rec_score"])
    except (TypeError, ValueError):
        pass
    return DetailResult(text, confidence)


def _prediction_angle(prediction: Any) -> int:
    if not isinstance(prediction, Mapping):
        return 0
    preprocessor = prediction.get("doc_preprocessor_res")
    if not isinstance(preprocessor, Mapping):
        return 0
    try:
        angle = int(preprocessor.get("angle", 0))
    except (TypeError, ValueError):
        return 0
    # PaddleOCR documentation describes both degree labels and class ids for
    # this field across interfaces.  The installed local pipeline emits degrees.
    if angle in {1, 2, 3}:
        angle *= 90
    return angle if angle in {90, 180, 270} else 0


def _align_detail_to_prediction(detail_path: Path, prediction: Any) -> Path:
    """Rotate the retained detail image into the baseline prediction space."""
    angle = _prediction_angle(prediction)
    if not angle:
        return detail_path
    with Image.open(detail_path) as source:
        aligned = source.convert("RGB").rotate(
            angle,
            expand=True,
            resample=Image.Resampling.BICUBIC,
            fillcolor="white",
        )
    temporary = detail_path.with_suffix(".aligned.tmp.png")
    aligned.save(temporary, format="PNG", optimize=False)
    temporary.replace(detail_path)
    return detail_path


def _bounded_resize(image: Image.Image, scale: float) -> Image.Image:
    target_scale = min(scale, _DETAIL_MAX_SIDE / max(image.size))
    if target_scale <= 1.0:
        return image.copy()
    return image.resize(
        (max(1, round(image.width * target_scale)), max(1, round(image.height * target_scale))),
        Image.Resampling.LANCZOS,
    )


def _detail_variants(
    detail_path: Path,
    *,
    page_size: tuple[int, int],
    target: DetailTarget,
    session_dir: Path,
    target_index: int,
    nontext_regions: list | None = None,
) -> list[Path]:
    """Create padded original, contrast and sharpened line images."""
    page_width, page_height = page_size
    if page_width <= 0 or page_height <= 0:
        return []
    with Image.open(detail_path) as opened:
        detail = opened.convert("RGB")
    scale_x = detail.width / page_width
    scale_y = detail.height / page_height
    if nontext_regions:
        detail = mask_nontext(detail, nontext_regions, scale=(scale_x, scale_y))
    left, top, right, bottom = target.bbox
    left, right = round(left * scale_x), round(right * scale_x)
    top, bottom = round(top * scale_y), round(bottom * scale_y)
    width, height = max(1, right - left), max(1, bottom - top)
    pad_x = max(4, round(width * 0.15))
    pad_y = max(4, round(height * 0.15))
    crop = detail.crop((
        max(0, left - pad_x),
        max(0, top - pad_y),
        min(detail.width, right + pad_x),
        min(detail.height, bottom + pad_y),
    ))
    white_border = max(4, round(crop.height * 0.15))
    crop = ImageOps.expand(crop, border=white_border, fill="white")
    variants = [
        crop,
        ImageEnhance.Contrast(_bounded_resize(crop, 1.5)).enhance(1.12),
        _bounded_resize(crop, 2.0).filter(ImageFilter.UnsharpMask(radius=1, percent=115, threshold=3)),
    ]
    output_dir = assert_local_path(session_dir, "remote_path") / "processed"
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths: list[Path] = []
    for pass_index, variant in enumerate(variants, start=1):
        path = output_dir / f"detail-review-{target_index}-pass-{pass_index}.png"
        variant.save(path, format="PNG", optimize=False)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        paths.append(path)
    return paths


def _save_figure_crops(page: PageResult, session_dir: Path) -> None:
    figures = Path(session_dir) / "figures"
    figures.mkdir(mode=0o700, parents=True, exist_ok=True)
    with Image.open(page.processed_image_path) as image:
        for block in page.blocks:
            if block.kind not in {"image", "table"}:
                continue
            left, top, right, bottom = (round(value) for value in block.bbox)
            left, top = max(0, left), max(0, top)
            right, bottom = min(image.width, right), min(image.height, bottom)
            if left >= right or top >= bottom:
                block.needs_review = True
                continue
            target = figures / f"{re.sub(r'[^a-zA-Z0-9_-]', '_', block.id)}.png"
            crop = image.crop((left, top, right, bottom))
            if block.id.startswith(f"{page.id}:nontext-"):
                polygon = next((p for p in page.nontext_regions if all(
                    abs(a-b) < .01 for a, b in zip(polygon_bounds(p), block.bbox))), None)
                if polygon:
                    # A rotated code's bounding rectangle may also contain
                    # nearby text in its corners. Do not export that text twice.
                    crop = crop.convert('RGBA')
                    alpha = Image.new('L', crop.size, 0)
                    ImageDraw.Draw(alpha).polygon([(x-left, y-top) for x, y in polygon], fill=255)
                    crop.putalpha(alpha)
            crop.save(target, format="PNG")
            try:
                target.chmod(0o600)
            except OSError:
                pass
            block.image_path = str(target)


def _cpu_acceleration_error(error: Exception) -> bool:
    return any(token in str(error).casefold() for token in ("onednn", "mkldnn", "convertpirattribute"))


def _build_cpu_engine(constructor: Any, options: Any, model_root: Path, *, safe_cpu: bool) -> Any:
    try:
        with measured('model_create'):
            return constructor(**options(model_root, safe_cpu=safe_cpu))
    except Exception as error:
        if os.name != "nt" or safe_cpu or not _cpu_acceleration_error(error):
            raise
        return constructor(**options(model_root, safe_cpu=True))


def _create_engine(model_root: Path, session_dir: Path, *, safe_cpu: bool = False) -> Any:
    with measured('model_validate'):
        validate_models(model_root)
    configure_offline_env(model_root, session_dir)
    deny_network()
    try:
        with measured('module_import'):
            from paddleocr import PPStructureV3
    except ImportError:
        raise OfflineError("ocr_unavailable") from None
    try:
        return _build_cpu_engine(PPStructureV3, pipeline_options, model_root, safe_cpu=safe_cpu)
    except Exception:
        raise OfflineError("ocr_init_failed") from None


def _create_plain_engine(model_root: Path, session_dir: Path, *, safe_cpu: bool = False) -> Any:
    """Create local OCR for text mode, regions and structure recovery."""
    try:
        with measured('module_import'):
            from paddleocr import PaddleOCR
    except ImportError:
        raise OfflineError("ocr_unavailable") from None
    try:
        return _build_cpu_engine(PaddleOCR, plain_ocr_options, model_root, safe_cpu=safe_cpu)
    except Exception:
        raise OfflineError("ocr_fallback_init_failed") from None


def _create_detail_engine(model_root: Path, session_dir: Path, *, safe_cpu: bool = False) -> Any:
    """Lazily load only PP-OCRv6 recognition for optional line refinement."""
    try:
        with measured('module_import'):
            from paddleocr import TextRecognition
    except ImportError:
        raise OfflineError("ocr_detail_unavailable") from None
    try:
        return _build_cpu_engine(TextRecognition, detail_recognition_options, model_root, safe_cpu=safe_cpu)
    except Exception:
        raise OfflineError("ocr_detail_init_failed") from None


def _silence_child_output() -> None:
    """Keep Paddle/native diagnostics out of a GUI or packaged-app console."""
    try:
        sink = open(os.devnull, "w", encoding="utf-8")
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
        sys.stdout = sink
        sys.stderr = sink
    except OSError:
        # The safety of OCR events does not depend on a best-effort console sink.
        pass


def _proofread_variant(source: Path, target: Path, pass_index: int) -> Path:
    """Save one enhanced OCR input while leaving the prepared baseline untouched."""
    with Image.open(source) as source_image:
        image = source_image.copy()
    if pass_index == 2:
        variant = ImageEnhance.Contrast(image).enhance(1.12)
    elif pass_index == 3:
        variant = image.filter(ImageFilter.UnsharpMask(radius=1, percent=115, threshold=3))
    else:
        raise ValueError("proofread variants only support passes 2 and 3")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    variant.save(target, format="PNG")
    return target


def _proofread_variant_path(session_dir: Path, job_id: str, pass_index: int) -> Path:
    safe_job_id = re.sub(r"[^a-zA-Z0-9_-]", "_", job_id)
    return session_dir / "processed" / f"proofread-{safe_job_id}-pass-{pass_index}.png"


def _predict_image(engine: Any, image_path: Path) -> Any:
    """Run PaddleX from BGR pixels so native code never reopens a Unicode path."""
    import numpy as np

    with Image.open(image_path) as image:
        pixels = np.asarray(image.convert("RGB"))[:, :, ::-1].copy()
    return next(iter(engine.predict(pixels)))


class _PredictionRuntime:
    """Keep structure and fallback engines page-sequential in one child process."""

    def __init__(self, model_root: Path, session_dir: Path, structure_engine: Any):
        self.model_root = model_root
        self.session_dir = session_dir
        self.structure_engine = structure_engine
        self.structure_available = structure_engine is not None
        self.structure_deferred = False
        self.plain_engine: Any | None = None
        self.plain_init_error: str | None = None
        self.detail_engine: Any | None = None
        self.detail_init_error: str | None = None
        self._safe_cpu_engines: set[str] = set()
        self._used_engines = set()

    @staticmethod
    def _predict(engine: Any, image_path: Path) -> Any:
        try:
            return _predict_image(engine, image_path)
        except MemoryError:
            raise OfflineError("ocr_memory_failed") from None

    def _predict_with_cpu_retry(self, name: str, factory: Any, image_path: Path) -> Any:
        try:
            with measured('inference' if name in self._used_engines else 'first_inference'):
                result = self._predict(getattr(self, name), image_path)
            self._used_engines.add(name)
            return result
        except Exception as error:
            if os.name != "nt" or name in self._safe_cpu_engines or not _cpu_acceleration_error(error):
                raise
            self._safe_cpu_engines.add(name)
            setattr(self, name, None)
            setattr(self, name, factory(self.model_root, self.session_dir, safe_cpu=True))
            return self._predict(getattr(self, name), image_path)

    def _predict_plain(self, image_path: Path) -> Any:
        try:
            if self.plain_engine is None:
                if self.plain_init_error is not None:
                    raise OfflineError(self.plain_init_error)
                try:
                    self.plain_engine = _create_plain_engine(self.model_root, self.session_dir)
                except OfflineError as error:
                    self.plain_init_error = error.code
                    raise
            return self._predict_with_cpu_retry("plain_engine", _create_plain_engine, image_path)
        except OfflineError:
            raise
        except Exception:
            raise OfflineError("ocr_inference_failed") from None

    def predict_baseline(self, image_path: Path, *, text_only: bool = False) -> tuple[str, Any]:
        if self.structure_deferred and not text_only:
            self.structure_deferred = False
            try:
                self.structure_engine = _create_engine(self.model_root, self.session_dir)
                self.structure_available = True
            except OfflineError as error:
                if error.code != 'ocr_init_failed':
                    raise
        if self.structure_available and not text_only:
            try:
                return "structure", self._predict_with_cpu_retry("structure_engine", _create_engine, image_path)
            except OfflineError:
                raise
            except Exception:
                # PP-StructureV3 has Windows-specific failures that do not affect
                # ordinary PaddleOCR. Disable it for later pages in this batch.
                self.structure_available = False
        return "plain", self._predict_plain(image_path)

    def predict_comparison(self, image_path: Path, mode: str) -> Any:
        if mode == "structure" and self.structure_available:
            return self._predict_with_cpu_retry("structure_engine", _create_engine, image_path)
        return self._predict_plain(image_path)

    def predict_plain_completeness(self, image_path: Path) -> Any:
        return self._predict_plain(image_path)

    def predict_detail(self, image_path: Path) -> DetailResult:
        try:
            if self.detail_engine is None:
                if self.detail_init_error is not None:
                    raise OfflineError(self.detail_init_error)
                try:
                    self.detail_engine = _create_detail_engine(self.model_root, self.session_dir)
                except OfflineError as error:
                    self.detail_init_error = error.code
                    raise
            return _detail_result_from_prediction(self._predict_with_cpu_retry("detail_engine", _create_detail_engine, image_path))
        except OfflineError:
            raise
        except Exception:
            raise OfflineError("ocr_detail_failed") from None


def _prepare_for_worker(job: ImageJob, session_dir: Path) -> Path:
    try:
        return prepare_image(job, session_dir)
    except OfflineError:
        raise
    except Exception:
        raise OfflineError("image_prepare_failed") from None


def _prepare_with_detail_for_worker(job: ImageJob, session_dir: Path) -> tuple[Path, Path]:
    try:
        return prepare_image_with_detail(job, session_dir)
    except OfflineError:
        raise
    except Exception:
        raise OfflineError("image_prepare_failed") from None


def _create_runtime(model_root: Path, session_dir: Path, *, text_only: bool = False) -> _PredictionRuntime:
    if text_only:
        with measured('model_validate'):
            validate_models(model_root)
        configure_offline_env(model_root, session_dir)
        deny_network()
        runtime = _PredictionRuntime(model_root, session_dir, None)
        runtime.structure_deferred = True
        return runtime
    try:
        structure_engine = _create_engine(model_root, session_dir)
    except OfflineError as error:
        if error.code != "ocr_init_failed":
            raise
        structure_engine = None
    return _PredictionRuntime(model_root, session_dir, structure_engine)


def _page_for_prediction(job: ImageJob, processed: Path, prediction: Any, mode: str, *, update_paper_geometry=True) -> tuple[Path, PageResult]:
    try:
        with Image.open(processed) as source:
            source_width, source_height = source.size
        processed = _sync_processed_preview(prediction, processed)
        if mode == "structure":
            page = page_from_prediction(job, processed, prediction)
        else:
            page = page_from_plain_prediction(job, processed, prediction)
        page.performance_profile = job.performance_profile
        page.paper_status = job.paper_status
        page.paper_confidence = job.paper_confidence
        page.source_paper_quad = job.paper_quad
        if job.processed_paper_polygon:
            from .paper import rotate_polygon
            angle = _prediction_angle(prediction)
            polygon = rotate_polygon(job.processed_paper_polygon, source_width, source_height, -angle)
            rotated_width, rotated_height = (source_height, source_width) if angle in {90, 270} else (source_width, source_height)
            page.paper_polygon = [(x * page.width / rotated_width, y * page.height / rotated_height) for x, y in polygon]
            if update_paper_geometry:
                job.processed_paper_polygon = page.paper_polygon
        return processed, page
    except OfflineError:
        raise
    except Exception:
        raise OfflineError("ocr_result_parse_failed") from None


def _overall_from_prediction(prediction: Any) -> Any:
    raw = _prediction_data(prediction)
    overall = raw.get("overall_ocr_res")
    return overall if isinstance(overall, dict) else raw


def _run_detail_review(
    page: PageResult,
    baseline_prediction: Any,
    detail_path: Path,
    runtime: Any,
    sender: Any,
    job_id: str,
    session_dir: Path,
) -> None:
    """Attach optional suggestions; every failure leaves the baseline exportable."""
    try:
        lines = [line for line in _overall_lines(_overall_from_prediction(baseline_prediction))
                 if not covered_by_nontext(line[1], page.nontext_regions)]
        targets = detail_targets(page, lines)
    except Exception:
        return
    for region_index, target in enumerate(targets, start=1):
        results: list[DetailResult] = []
        try:
            variants = _detail_variants(
                detail_path,
                page_size=(page.width, page.height),
                target=target,
                session_dir=session_dir,
                target_index=region_index - 1,
                nontext_regions=page.nontext_regions,
            )
        except Exception:
            continue
        for pass_index, variant in enumerate(variants, start=1):
            try:
                results.append(runtime.predict_detail(variant))
            except Exception:
                pass
            sender.send({
                "event": "page_detail_progress",
                "job_id": job_id,
                "region_index": region_index,
                "region_count": len(targets),
                "pass_index": pass_index,
                "pass_count": len(variants),
            })
        apply_detail_results(page, target, results)


def _publish_page(page: PageResult, session_dir: Path, sender: Any, job_id: str) -> None:
    """Finalize every recognition mode through the same appearance boundary."""
    from .paper import review_outside_paper
    review_outside_paper(page)
    if page.text_only:
        from .text_output import reading_order
        page.blocks = reading_order(page.blocks)
    else:
        from .source_style import annotate_source_style
        annotate_source_style(page)
        _save_figure_crops(page, session_dir)
    sender.send({"event": "page_done", "job_id": job_id, "result": page})


def _process_pages(model_root: str, session_dir: str, jobs: list[ImageJob], sender: Any, *, commands: Any = None, keep_alive: bool = False, runtime: _PredictionRuntime | None = None, work_dir: Path | None = None) -> _PredictionRuntime | None:
    _silence_child_output()
    lease = None
    from .diagnostics import DiagnosticProbe
    probe = DiagnosticProbe(sender, enabled=any(job.diagnostics for job in jobs))
    probe_token = CURRENT_PROBE.set(probe)
    try:
        # This marker remains locked after a GUI crash, so a later launch cannot
        # mistake an active OCR child for stale session data.
        lease = claim_session_lease(session_dir)
        model_path = Path(model_root)
        session_path = work_dir or Path(session_dir)
        if keep_alive and work_dir is None:
            session_path = session_path / f'lane-{os.getpid()}'
            session_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if runtime is None:
            runtime = _create_runtime(model_path, session_path, text_only=all(job.text_only for job in jobs))
            probe.stage("engine_init")
        from .task_queue import PageTaskQueue
        tasks = PageTaskQueue(jobs, sender, commands, keep_alive=keep_alive)
        while (job := tasks.next_job()) is not None:
            try:
                if job.performance_profile not in {"sequential", "parallel"}:
                    raise OfflineError("invalid_performance_profile")
                probe.enabled = job.diagnostics
                probe.begin_page()
                sender.send({"event": "page_started", "job_id": job.id})
                # The UI may have displayed a provisional preview.  Rebuild from
                # the job's current crop/rotation values for the worker pass.
                precise = job.auto_proofread
                sender.send({'event': 'page_stage', 'job_id': job.id, 'stage': 'prepare'})
                if precise:
                    processed, detail = _prepare_with_detail_for_worker(job, session_path)
                else:
                    processed = _prepare_for_worker(job, session_path)
                    detail = processed
                probe.stage("prepare")
                sender.send({'event': 'page_stage', 'job_id': job.id, 'stage': 'recognize'})
                mode, prediction = runtime.predict_baseline(processed, text_only=job.text_only)
                probe.stage("recognize")
                processed, baseline = _page_for_prediction(job, processed, prediction, mode)
                sender.send({'event': 'page_stage', 'job_id': job.id, 'stage': 'integrity'})
                if mode in {"structure", "plain"}:
                    suspicious = _needs_text_completeness_check(baseline)
                    recovered_lines = 0
                    try:
                        recognized_line_boxes = [
                            bbox for _text, bbox, _confidence in _overall_lines(
                                _overall_from_prediction(prediction)
                            )
                            if bbox is not None
                        ]
                        missing_regions = _missing_line_regions(
                            baseline,
                            processed,
                            recognized_line_boxes=recognized_line_boxes,
                        )
                        if missing_regions:
                            sender.send({"event": "page_integrity_progress", "job_id": job.id})
                            recovered_lines = _recover_missing_lines(
                                baseline,
                                processed,
                                missing_regions,
                                runtime,
                                session_path,
                            )
                    except Exception:
                        # Keep the structure result if line-band recovery is
                        # unavailable for an unusual image or native runtime.
                        pass
                    if mode == "structure" and suspicious and not recovered_lines:
                        try:
                            sender.send({"event": "page_integrity_progress", "job_id": job.id})
                            plain_prediction = runtime.predict_plain_completeness(processed)
                            _, plain_page = _page_for_prediction(job, processed, plain_prediction, "plain")
                            recovered = _prefer_complete_plain_page(baseline, plain_page)
                            if recovered is plain_page:
                                baseline = recovered
                                prediction = plain_prediction
                                mode = "plain"
                        except Exception:
                            # The exportable structure baseline remains
                            # authoritative if the extra local OCR path fails.
                            pass
                if detail != processed:
                    _align_detail_to_prediction(detail, prediction)
                else:
                    detail = processed
                probe.stage("integrity")
                if not precise:
                    _publish_page(baseline, session_path, sender, job.id)
                    probe.stage("page_done")
                    continue

                sender.send({"event": "page_progress", "job_id": job.id, "pass_index": 1, "pass_count": 3})
                comparisons: list[PageResult] = []
                page: PageResult | None = None
                for pass_index in (2, 3):
                    try:
                        variant = _proofread_variant(
                            processed,
                            _proofread_variant_path(session_path, job.id, pass_index),
                            pass_index,
                        )
                        comparison_prediction = runtime.predict_comparison(variant, mode)
                        _, comparison = _page_for_prediction(job, variant, comparison_prediction, mode, update_paper_geometry=False)
                        comparisons.append(comparison)
                    except Exception:
                        page = reconcile_page(
                            baseline,
                            comparisons,
                            completed_passes=1 + len(comparisons),
                        )
                        break
                    sender.send({"event": "page_progress", "job_id": job.id, "pass_index": pass_index, "pass_count": 3})
                else:
                    page = reconcile_page(baseline, comparisons)
                assert page is not None
                _run_detail_review(page, prediction, detail, runtime, sender, job.id, session_path)
                probe.stage("proofread")
                _publish_page(page, session_path, sender, job.id)
                probe.stage("page_done")
            except OfflineError as error:
                probe.stage("page_error")
                sender.send({"event": "page_error", "job_id": job.id, "code": error.code})
            except Exception:
                probe.stage("page_error")
                sender.send({"event": "page_error", "job_id": job.id, "code": "ocr_processing_failed"})
    except OfflineError as error:
        for job in jobs:
            sender.send({"event": "page_error", "job_id": job.id, "code": error.code})
    except Exception:
        for job in jobs:
            sender.send({"event": "page_error", "job_id": job.id, "code": "ocr_worker_failed"})
    finally:
        CURRENT_PROBE.reset(probe_token)
        try:
            if lease is not None:
                lease.close()
        finally:
            try:
                sender.send({"event": "finished"})
            finally:
                sender.close()
    return runtime


def _process_queued_pages(model_root: str, session_dir: str, jobs: list[ImageJob], commands: Any, sender: Any) -> _PredictionRuntime | None:
    try:
        return _process_pages(model_root, session_dir, jobs, sender, commands=commands)
    finally:
        commands.close()


def _process_pool_pages(model_root: str, session_dir: str, jobs: list[ImageJob], commands: Any, sender: Any) -> _PredictionRuntime | None:
    try:
        return _process_pages(model_root, session_dir, jobs, sender, commands=commands, keep_alive=True)
    finally:
        commands.close()


def _process_region(model_root: str, session_dir: str, job: ImageJob, bbox: tuple, sender: Any, *, runtime: _PredictionRuntime | None = None, work_dir: Path | None = None) -> _PredictionRuntime | None:
    _silence_child_output()
    lease = None
    try:
        lease = claim_session_lease(session_dir)
        model_path = Path(model_root)
        session_path = work_dir or Path(session_dir)
        processed = (
            assert_local_path(job.result.processed_image_path, "remote_path")
            if job.result else Path()
        )
        if not processed.is_file():
            processed = _prepare_for_worker(job, session_path)
        with Image.open(processed) as image:
            left, top, right, bottom = (round(float(value)) for value in bbox)
            if left >= right or top >= bottom or left < 0 or top < 0 or right > image.width or bottom > image.height:
                raise OfflineError("invalid_region")
            region_path = session_path / "processed" / f"region-{re.sub(r'[^a-zA-Z0-9_-]', '_', job.id)}.png"
            region_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            crop = image.crop((left, top, right, bottom)).convert("RGB")
            # Selected text often touches the crop edge. Give the detector and
            # the same completeness check used for full pages some white margin.
            crop = ImageOps.expand(crop, border=max(12, round(crop.width * 0.12)), fill="white")
            crop.save(region_path, format="PNG")
        if runtime is None:
            validate_models(model_path)
            configure_offline_env(model_path, session_path)
            deny_network()
            runtime = _PredictionRuntime(model_path, session_path, None)
            runtime.structure_deferred = True
        mode, prediction = runtime.predict_baseline(region_path, text_only=True)
        region_path, page = _page_for_prediction(job, region_path, prediction, mode)
        text = "\n".join(block.text for block in page.blocks if block.kind not in {"image", "table"} and block.text).strip()
        candidates = [text] if text else []
        regions = _missing_line_regions(
            page, region_path, cropped_region=True,
            recognized_line_boxes=[box for _, box, _ in _overall_lines(_overall_from_prediction(prediction))
                                   if box is not None],
        )
        if _recover_missing_lines(page, region_path, regions, runtime, session_path):
            candidate = "\n".join(block.text for block in page.blocks if block.text).strip()
            if candidate:
                candidates = [candidate]
        sender.send({"event": "region_done", "job_id": job.id, "candidates": candidates})
    except OfflineError as error:
        sender.send({"event": "region_error", "job_id": job.id, "code": error.code})
    except Exception:
        sender.send({"event": "region_error", "job_id": job.id, "code": "ocr_processing_failed"})
    finally:
        if lease is not None:
            lease.close()
        sender.send({"event": "finished"})
        sender.close()
    return runtime


def _child_entry(target: Any, args: tuple[Any, ...]) -> None:
    """Run a worker target, then bypass unstable native-library finalizers."""
    exit_code = 0
    lifetime_guard: Any | None = None
    try:
        lifetime_guard = target(*args)  # noqa: F841 - retain native predictors until os._exit
    except BaseException:
        exit_code = 1
    finally:
        # PP-Structure has already released the session lease and closed its
        # one-way Pipe before returning.  Paddle's native objects can segfault
        # during Python's later atexit/GC phase on macOS, leaving a misleading
        # crash report after a successful page.  A hard process exit here is
        # safe because all application-owned output has been sent and closed.
        os._exit(exit_code)


class OCRWorker:
    """A restartable one-process OCR worker; no TCP/local HTTP transport is used."""

    def __init__(self, model_root: Path, session_dir: Path, *, cpu_threads: int | None = None):
        self.model_root = assert_local_path(model_root, "remote_model_path")
        self.session_dir = assert_local_path(session_dir, "remote_path")
        self._context = mp.get_context("spawn")
        self._process: mp.Process | None = None
        self._receiver: Any = None
        self._jobs: dict[str, ImageJob] = {}
        self._cancelled = False
        self._command_sender: Any = None
        self._cpu_threads = cpu_threads

    @property
    def busy(self) -> bool:
        return self._process is not None

    def _start_process(self, target: Any, args: tuple[Any, ...]) -> None:
        if self.busy:
            if self._cancelled:
                raise OfflineError("worker_still_running")
            raise RuntimeError("worker_busy")
        receiver, sender = self._context.Pipe(duplex=False)
        try:
            process = self._context.Process(
                target=_child_entry,
                args=(target, (*args, sender)),
                daemon=True,
            )
            from .performance import child_cpu_environment
            with child_cpu_environment(self._cpu_threads):
                process.start()
        except Exception:
            sender.close()
            receiver.close()
            raise
        self._receiver = receiver
        self._process = process
        self._cancelled = False
        sender.close()

    def start(self, jobs: list[ImageJob], *, keep_alive: bool = False) -> None:
        # Full hashes are checked in the child immediately before model load;
        # never trust a size/mtime cache or repeat the same I/O on the GUI thread.
        # Spawn imports this module before the child target runs, so establish
        # resource limits in the parent environment before Process.start().
        configure_offline_env(self.model_root, self.session_dir)
        self._jobs = {job.id: job for job in jobs}
        receiver, sender = self._context.Pipe(duplex=False)
        try:
            target = _process_pool_pages if keep_alive else _process_queued_pages
            self._start_process(target, (str(self.model_root), str(self.session_dir), jobs, receiver))
        except Exception:
            sender.close()
            raise
        finally:
            receiver.close()
        from .task_queue import CommandMailbox
        try:
            self._command_sender = CommandMailbox(sender)
        except Exception:
            sender.close()
            self.cancel()
            raise

    def add_jobs(self, jobs: list[ImageJob]) -> bool:
        """False means the GUI should start these pages after the batch exits."""
        if not self.busy or self._command_sender is None or self._cancelled:
            return False
        try:
            self._command_sender.send({'action': 'add', 'jobs': jobs})
        except (OSError, EOFError):
            return False
        self._jobs.update((job.id, job) for job in jobs)
        return True

    def remove_jobs(self, job_ids: list[str]) -> bool:
        """Keep rows visible until queue_removed or queue_remove_rejected arrives."""
        if not self.busy or self._command_sender is None or self._cancelled:
            return False
        try:
            self._command_sender.send({'action': 'remove', 'job_ids': job_ids})
            return True
        except (OSError, EOFError):
            return False

    def finish_queue(self) -> None:
        if self._command_sender is not None:
            self._command_sender.send({'action': 'finish'})

    def start_region(self, job: ImageJob, bbox: tuple) -> None:
        configure_offline_env(self.model_root, self.session_dir)
        self._jobs = {job.id: job}
        self._start_process(_process_region, (str(self.model_root), str(self.session_dir), job, bbox))

    def _release_process_if_finished(self, events: list[dict] | None = None) -> None:
        if self._process is not None and not self._process.is_alive():
            if events is not None:
                # The child can exit between the preceding poll() and this
                # is_alive() check. With all writers gone, read through EOF
                # before closing the pipe, including its final buffered event.
                self._drain_events(events, until_eof=True)
            self._process.join(timeout=0)
            self._process = None
        if self._process is None and self._receiver is not None:
            self._receiver.close()
            self._receiver = None
        if self._process is None and self._command_sender is not None:
            self._command_sender.close()
            self._command_sender = None

    def poll(self) -> list[dict]:
        if self._cancelled:
            self._release_process_if_finished()
            if self._process is None:
                self._cancelled = False
                return [{"event": "cancelled"}]
            return []
        events: list[dict] = []
        self._drain_events(events)
        self._release_process_if_finished(events)
        return events

    def _drain_events(self, events: list[dict], *, until_eof: bool = False) -> None:
        if self._receiver is not None:
            try:
                while until_eof or self._receiver.poll():
                    event = self._receiver.recv()
                    if event.get("event") == "page_done":
                        job = self._jobs.get(event["job_id"])
                        if job is not None:
                            job.status, job.error_code, job.result = "done", None, event["result"]
                    elif event.get("event") == "page_error":
                        job = self._jobs.get(event["job_id"])
                        if job is not None:
                            job.status, job.error_code = "failed", event["code"]
                    elif event.get("event") == "page_started":
                        job = self._jobs.get(event["job_id"])
                        if job is not None:
                            job.status = "processing"
                    elif event.get('event') == 'queue_removed':
                        for job_id in event.get('job_ids', []):
                            self._jobs.pop(job_id, None)
                    events.append(event)
            except (EOFError, OSError):
                pass

    def cancel(self) -> None:
        if self._command_sender is not None:
            self._command_sender.close()
            self._command_sender = None
        if self._process is not None:
            if self._process.is_alive():
                self._process.terminate()
            self._process.join(timeout=2)
            if self._process.is_alive():
                try:
                    self._process.kill()
                except (AttributeError, OSError):
                    pass
                self._process.join(timeout=2)
            if not self._process.is_alive():
                self._process = None
                if self._receiver is not None:
                    self._receiver.close()
                    self._receiver = None
        self._cancelled = True

    def close(self) -> None:
        self.cancel()
