from pathlib import Path
from collections import UserDict

import numpy as np
import pytest


def test_worker_drains_final_pipe_event_after_child_exit(tmp_path):
    from types import SimpleNamespace
    from offline_word.worker import OCRWorker
    runner = OCRWorker(tmp_path/'models', tmp_path/'session')
    events = iter([{'event': 'finished'}])
    class Receiver:
        closed = False
        def poll(self):
            return False
        def recv(self):
            try:
                return next(events)
            except StopIteration:
                raise EOFError
        def close(self):
            self.closed = True
    receiver = Receiver()
    runner._receiver = receiver
    runner._process = SimpleNamespace(is_alive=lambda: False, join=lambda **kw: None)
    assert runner.poll() == [{'event': 'finished'}]
    assert not runner.busy and receiver.closed
from PIL import Image, ImageEnhance, ImageFilter

from offline_word.accuracy import DetailResult, DetailTarget
from offline_word.domain import Block, ImageJob, PageResult
from offline_word import worker
from offline_word.security import OfflineError
from offline_word.worker import (
    _sync_processed_preview,
    _table_cells,
    detail_recognition_options,
    native_windows_path,
    page_from_plain_prediction,
    page_from_prediction,
    pipeline_options,
    plain_ocr_options,
    region_candidates_from_prediction,
)


def test_detail_recognition_options_bind_only_the_local_v6_recognizer(tmp_path: Path):
    """Would fail if detail OCR can select a remote/default model."""
    options = detail_recognition_options(tmp_path / "models", platform="nt")

    assert options["model_name"] == "PP-OCRv6_medium_rec"
    assert options["model_dir"].endswith("PP-OCRv6_medium_rec")
    assert options["device"] == "cpu"
    assert 1 <= options["cpu_threads"] <= 4
    assert options["enable_mkldnn"] is True
    assert options["engine_config"]["delete_pass"] == ["onednn_placement_pass"]


def test_detail_variants_map_page_coordinates_to_high_resolution_and_add_padding(tmp_path: Path):
    """Would fail if the fine pass crops the downscaled coordinates directly."""
    detail = tmp_path / "detail.png"
    Image.new("RGB", (200, 100), (20, 30, 40)).save(detail)
    target = DetailTarget(0, "文字", (10, 10, 30, 20), 0.5)

    variants = worker._detail_variants(
        detail,
        page_size=(100, 50),
        target=target,
        session_dir=tmp_path,
        target_index=0,
    )

    assert len(variants) == 3
    with Image.open(variants[0]) as original, Image.open(variants[1]) as contrast, Image.open(variants[2]) as sharp:
        assert original.size == (60, 36)
        assert contrast.size == (90, 54)
        assert sharp.size == (120, 72)
        assert original.getpixel((0, 0)) == (255, 255, 255)


def test_align_detail_to_prediction_applies_the_pipeline_orientation(tmp_path: Path):
    """Would fail if low-confidence bboxes point at a differently rotated detail image."""
    detail = tmp_path / "detail.png"
    image = Image.new("RGB", (2, 1), "white")
    image.putpixel((0, 0), (255, 0, 0))
    image.putpixel((1, 0), (0, 255, 0))
    image.save(detail)

    worker._align_detail_to_prediction(detail, {"doc_preprocessor_res": {"angle": 90}})

    with Image.open(detail) as aligned:
        assert aligned.size == (1, 2)
        assert [aligned.getpixel((0, y)) for y in range(2)] == [(0, 255, 0), (255, 0, 0)]


def test_detail_engine_is_created_lazily_once_and_receives_bgr_pixels(tmp_path: Path, monkeypatch):
    """Would fail if every line reloads the recognition model or passes a private path."""
    image = tmp_path / "line.png"
    Image.new("RGB", (4, 3), (10, 20, 30)).save(image)
    created = 0
    inputs: list[np.ndarray] = []

    class Engine:
        def predict(self, pixels):
            inputs.append(pixels)
            yield {"res": {"rec_text": "候选", "rec_score": 0.97}}

    def create(*_args):
        nonlocal created
        created += 1
        return Engine()

    monkeypatch.setattr(worker, "_create_detail_engine", create)
    runtime = worker._PredictionRuntime(tmp_path / "models", tmp_path, None)

    first = runtime.predict_detail(image)
    second = runtime.predict_detail(image)

    assert first == second == DetailResult("候选", 0.97)
    assert created == 1
    assert len(inputs) == 2
    assert tuple(inputs[0][0, 0]) == (30, 20, 10)


def test_detail_review_runs_three_variants_and_emits_safe_progress(tmp_path: Path):
    """Would fail if optional detail OCR is not connected to page results."""
    detail = tmp_path / "detail.png"
    Image.new("RGB", (100, 50), "white").save(detail)
    result_page = PageResult(
        id="p",
        page_index=0,
        width=100,
        height=50,
        processed_image_path=str(detail),
        blocks=[Block(id="body", kind="text", bbox=(0, 0, 100, 50), original_text="金额 1O0 元")],
    )
    prediction = {"res": {"overall_ocr_res": {
        "rec_texts": ["金额 1O0 元"],
        "rec_boxes": [[10, 10, 90, 35]],
        "rec_scores": [0.55],
    }}}

    class Runtime:
        def __init__(self):
            self.values = iter([
                DetailResult("金额 100 元", 0.97),
                DetailResult("金额 100 元", 0.96),
                DetailResult("金额 1O0 元", 0.91),
            ])
            self.paths: list[Path] = []

        def predict_detail(self, path):
            self.paths.append(Path(path))
            return next(self.values)

    class Sender:
        def __init__(self):
            self.events: list[dict] = []

        def send(self, event):
            self.events.append(event)

    runtime = Runtime()
    sender = Sender()

    worker._run_detail_review(result_page, prediction, detail, runtime, sender, "p", tmp_path)

    assert len(runtime.paths) == 3
    assert result_page.blocks[0].candidates == ["金额 1O0 元", "金额 100 元"]
    assert result_page.detail_candidate_regions == 1
    assert [event["pass_index"] for event in sender.events] == [1, 2, 3]
    assert all(set(event) == {"event", "job_id", "region_index", "region_count", "pass_index", "pass_count"} for event in sender.events)


def test_detail_review_does_not_initialize_for_high_confidence_page(tmp_path: Path):
    """Would fail if high-confidence pages pay the extra model and time cost."""
    detail = tmp_path / "detail.png"
    Image.new("RGB", (100, 50), "white").save(detail)
    result_page = PageResult(
        id="p", page_index=0, width=100, height=50, processed_image_path=str(detail),
        blocks=[Block(id="body", kind="text", bbox=(0, 0, 100, 50), original_text="清晰正文")],
    )
    prediction = {"res": {"overall_ocr_res": {
        "rec_texts": ["清晰正文"], "rec_boxes": [[10, 10, 90, 35]], "rec_scores": [0.99],
    }}}

    class Runtime:
        def predict_detail(self, _path):
            raise AssertionError("detail engine must remain lazy")

    class Sender:
        def send(self, _event):
            raise AssertionError("no detail progress expected")

    worker._run_detail_review(result_page, prediction, detail, Runtime(), Sender(), "p", tmp_path)

    assert result_page.detail_reviewed_regions == 0


def test_detail_review_failure_keeps_exportable_baseline_and_remaining_candidates(tmp_path: Path):
    detail = tmp_path / "detail.png"
    Image.new("RGB", (100, 50), "white").save(detail)
    result_page = PageResult(
        id="p", page_index=0, width=100, height=50, processed_image_path=str(detail),
        blocks=[Block(id="body", kind="text", bbox=(0, 0, 100, 50), original_text="编号 A1O")],
    )
    prediction = {"res": {"overall_ocr_res": {
        "rec_texts": ["编号 A1O"], "rec_boxes": [[10, 10, 90, 35]], "rec_scores": [0.55],
    }}}

    class Runtime:
        def __init__(self):
            self.calls = 0

        def predict_detail(self, _path):
            self.calls += 1
            if self.calls == 1:
                raise OfflineError("ocr_detail_failed")
            return DetailResult("编号 A10", 0.95)

    class Sender:
        def __init__(self):
            self.events: list[dict] = []

        def send(self, event):
            self.events.append(event)

    sender = Sender()
    worker._run_detail_review(result_page, prediction, detail, Runtime(), sender, "p", tmp_path)

    assert result_page.blocks[0].original_text == "编号 A1O"
    assert result_page.blocks[0].edited_text is None
    assert result_page.blocks[0].candidates == ["编号 A1O", "编号 A10"]
    assert result_page.detail_review_status == "incomplete"
    assert len(sender.events) == 3


def test_auto_proofread_process_connects_original_detail_review_to_finished_page(tmp_path: Path, monkeypatch):
    """Would fail if detail helpers exist but the page worker never calls them."""
    processed = tmp_path / "processed.png"
    detail = tmp_path / "detail.png"
    Image.new("RGB", (100, 50), "white").save(processed)
    Image.new("RGB", (200, 100), "white").save(detail)
    job = ImageJob(id="p", source_path="unused.png", auto_proofread=True)

    class Lease:
        def close(self):
            pass

    class StructureEngine:
        def predict(self, _pixels):
            yield {"res": {"overall_ocr_res": {
                "rec_texts": ["金额 1O0 元"],
                "rec_boxes": [[10, 10, 90, 35]],
                "rec_scores": [0.55],
            }}}

    class DetailEngine:
        def predict(self, _pixels):
            yield {"res": {"rec_text": "金额 100 元", "rec_score": 0.97}}

    class Sender:
        def __init__(self):
            self.events: list[dict] = []

        def send(self, event):
            self.events.append(event)

        def close(self):
            pass

    sender = Sender()
    monkeypatch.setattr(worker, "_silence_child_output", lambda: None)
    monkeypatch.setattr(worker, "claim_session_lease", lambda _: Lease())
    monkeypatch.setattr(worker, "_create_engine", lambda *_: StructureEngine())
    monkeypatch.setattr(worker, "_create_detail_engine", lambda *_: DetailEngine())
    monkeypatch.setattr(worker, "prepare_image_with_detail", lambda *_: (processed, detail))
    monkeypatch.setattr(worker, "_sync_processed_preview", lambda _, path: path)
    monkeypatch.setattr(
        worker,
        "page_from_prediction",
        lambda current_job, path, _prediction: PageResult(
            id=current_job.id,
            page_index=0,
            width=100,
            height=50,
            processed_image_path=str(path),
            blocks=[Block(id="body", kind="text", bbox=(0, 0, 100, 50), original_text="金额 1O0 元", needs_review=True)],
        ),
    )
    monkeypatch.setattr(worker, "_save_figure_crops", lambda *_: None)

    worker._process_pages("models", str(tmp_path), [job], sender)

    done = next(event for event in sender.events if event["event"] == "page_done")
    assert done["result"].blocks[0].candidates == ["金额 1O0 元", "金额 100 元"]
    assert done["result"].detail_candidate_regions == 1
    assert len([event for event in sender.events if event["event"] == "page_detail_progress"]) == 3


def test_pipeline_options_bind_all_models_locally_and_disable_unwanted_pipelines(tmp_path: Path):
    model_root = tmp_path / "models"
    expected_names = {
        "PP-DocLayout_plus-L", "PP-OCRv6_medium_det", "PP-OCRv6_medium_rec",
        "PP-LCNet_x1_0_doc_ori", "PP-LCNet_x1_0_textline_ori", "PP-LCNet_x1_0_table_cls",
        "SLANeXt_wired", "SLANet_plus", "RT-DETR-L_wired_table_cell_det", "RT-DETR-L_wireless_table_cell_det",
    }

    options = pipeline_options(model_root)

    assert {Path(value).name for key, value in options.items() if key.endswith("_model_dir")} == expected_names
    assert options["text_detection_model_name"] == "PP-OCRv6_medium_det"
    assert options["text_recognition_model_name"] == "PP-OCRv6_medium_rec"
    assert options["device"] == "cpu"
    assert options["use_formula_recognition"] is False
    assert options["use_chart_recognition"] is False
    assert options["use_seal_recognition"] is False
    assert options["use_region_detection"] is False
    assert "ocr_version" not in options
    assert options["cpu_threads"] == min(4, __import__("os").cpu_count() or 1)
    assert options["mkldnn_cache_capacity"] == 1


def test_windows_pipeline_bounds_threads_and_disables_incompatible_placement(tmp_path: Path):
    options = pipeline_options(tmp_path / "models", platform="nt")

    assert 1 <= options["cpu_threads"] <= 4
    assert options["enable_mkldnn"] is True
    assert options["engine_config"]["delete_pass"] == ["onednn_placement_pass"]


@pytest.mark.parametrize("configure", [pipeline_options, plain_ocr_options])
def test_windows_detector_bounds_internal_tensor_without_resizing_recognition_source(tmp_path, configure):
    options = configure(tmp_path / "models", platform="nt")
    assert options["text_det_limit_type"] == "max"
    assert options["text_det_limit_side_len"] == 2560
    assert options["text_recognition_batch_size"] == 1


def test_plain_ocr_options_use_the_same_local_v6_models(tmp_path: Path):
    options = plain_ocr_options(tmp_path / "models", platform="nt")

    assert options["doc_orientation_classify_model_dir"].endswith("PP-LCNet_x1_0_doc_ori")
    assert options["text_detection_model_dir"].endswith("PP-OCRv6_medium_det")
    assert options["textline_orientation_model_dir"].endswith("PP-LCNet_x1_0_textline_ori")
    assert options["text_recognition_model_dir"].endswith("PP-OCRv6_medium_rec")
    assert options["use_doc_unwarping"] is False
    assert 1 <= options["cpu_threads"] <= 4
    assert options["enable_mkldnn"] is True
    assert options["engine_config"]["delete_pass"] == ["onednn_placement_pass"]


def test_native_windows_path_uses_existing_ascii_short_name_for_unicode_path():
    class Kernel32:
        @staticmethod
        def GetShortPathNameW(_source, target, _size):
            target.value = r"C:\\Users\\ZHOU~1\\AppData\\OfflineWord"
            return len(target.value)

    result = native_windows_path(
        r"C:\\Users\\中文用户\\AppData\\OfflineWord",
        platform="nt",
        kernel32=Kernel32(),
    )

    assert result == r"C:\\Users\\ZHOU~1\\AppData\\OfflineWord"


def test_plain_prediction_becomes_editable_review_blocks(tmp_path: Path):
    preview = tmp_path / "processed.png"
    Image.new("RGB", (120, 80), "white").save(preview)
    prediction = {"res": {
        "rec_texts": ["第一行", "第二行"],
        "rec_scores": [0.99, 0.88],
        "rec_boxes": [[2, 3, 80, 16], [2, 24, 100, 39]],
    }}

    page = page_from_plain_prediction(ImageJob(id="plain", source_path="unused.png"), preview, prediction)

    assert page.recognition_warning == "layout_fallback"
    assert [(block.text, block.bbox, block.confidence) for block in page.blocks] == [
        ("第一行", (2.0, 3.0, 80.0, 16.0), 0.99),
        ("第二行", (2.0, 24.0, 100.0, 39.0), 0.88),
    ]
    assert all(block.needs_review for block in page.blocks)


def test_page_from_prediction_makes_editable_table_from_nested_table_ocr(tmp_path: Path):
    preview = tmp_path / "processed.png"
    Image.new("RGB", (120, 80), "white").save(preview)
    job = ImageJob(id="job-1", source_path="never-read.png")
    raw = {"res": {
        "parsing_res_list": [
            UserDict({"block_id": 2, "block_label": "doc_title", "block_bbox": [1, 2, 100, 20], "block_content": "表格标题"}),
            UserDict({"block_id": 3, "block_label": "table", "block_bbox": [2, 22, 110, 70], "block_content": "<table><tr><td colspan='2'>甲</td></tr><tr><td>乙</td><td>丙</td></tr></table>"}),
        ],
        "table_res_list": [UserDict({
            "pred_html": "<table><tr><td colspan='2'>structure only</td></tr><tr><td>structure only</td><td>structure only</td></tr></table>",
            "table_ocr_pred": UserDict({"rec_texts": ["甲", "乙", "丙"], "rec_scores": [0.99, 0.90, 0.80]}),
        })],
        "overall_ocr_res": UserDict({
            "rec_boxes": [[2, 3, 30, 10], [3, 23, 30, 30]],
            "rec_scores": [0.98, 0.77],
        }),
    }}

    page = page_from_prediction(job, preview, raw)

    title, table = page.blocks
    assert (page.id, page.width, page.height) == ("job-1", 120, 80)
    assert (title.kind, title.text, title.bbox) == ("title", "表格标题", (1.0, 2.0, 100.0, 20.0))
    assert title.confidence == 0.98
    assert (table.kind, table.rows, table.cols) == ("table", 2, 2)
    assert table.confidence == 0.77
    assert [(cell.row, cell.col, cell.text, cell.rowspan, cell.colspan) for cell in table.cells] == [
        (0, 0, "甲", 1, 2), (1, 0, "乙", 1, 1), (1, 1, "丙", 1, 1),
    ]


def test_table_html_text_is_never_replaced_by_equal_count_raw_ocr_lines():
    cells, rows, cols = _table_cells({
        "pred_html": "<table><tr><td>甲 乙</td><td></td></tr></table>",
        "table_ocr_pred": {"rec_texts": ["甲", "乙"], "rec_scores": [0.99, 0.12]},
    })

    assert (rows, cols) == (1, 2)
    assert [(cell.text, cell.confidence) for cell in cells] == [("甲 乙", None), ("", None)]


def test_table_blocks_use_their_own_html_when_auxiliary_table_order_is_reversed(tmp_path: Path):
    preview = tmp_path / "processed.png"
    Image.new("RGB", (120, 100), "white").save(preview)
    raw = {"res": {
        "parsing_res_list": [
            {"block_id": 1, "block_label": "table", "block_bbox": [0, 0, 100, 30], "block_content": "<table><tr><td>第一表</td></tr></table>"},
            {"block_id": 2, "block_label": "table", "block_bbox": [0, 40, 100, 80], "block_content": "<table><tr><td>第二表</td></tr></table>"},
        ],
        "table_res_list": [
            {"pred_html": "<table><tr><td>第二表</td></tr></table>"},
            {"pred_html": "<table><tr><td>第一表</td></tr></table>"},
        ],
    }}

    page = page_from_prediction(ImageJob(id="two", source_path="unused.png"), preview, raw)

    assert [block.cells[0].text for block in page.blocks] == ["第一表", "第二表"]


def test_layout_keeps_uncovered_overall_ocr_body_and_flags_lowest_line_score(tmp_path: Path):
    preview = tmp_path / "processed.png"
    Image.new("RGB", (120, 80), "white").save(preview)
    raw = {"res": {
        "parsing_res_list": [
            {"block_id": 1, "block_label": "doc_title", "block_bbox": [0, 0, 100, 20], "block_content": "标题"},
        ],
        "overall_ocr_res": {
            "rec_texts": ["标题", "漏检正文"],
            "rec_boxes": [[0, 0, 100, 20], [0, 30, 100, 50]],
            "rec_scores": [0.99, 0.90],
        },
    }}

    page = page_from_prediction(ImageJob(id="fallback", source_path="unused.png"), preview, raw)

    assert [(block.kind, block.text) for block in page.blocks] == [("title", "标题"), ("text", "漏检正文")]
    assert page.blocks[1].confidence == 0.90
    assert page.blocks[1].needs_review is True


def test_layout_restores_ocr_line_omitted_inside_a_parsed_text_bbox(tmp_path: Path):
    """A broad layout box must not hide OCR text absent from its block content."""
    preview = tmp_path / "processed.png"
    Image.new("RGB", (120, 80), "white").save(preview)
    raw = {"res": {
        "parsing_res_list": [
            {
                "block_id": 1,
                "block_label": "text",
                "block_bbox": [0, 0, 110, 55],
                "block_content": "第二行仍被版面结构保留",
            },
        ],
        "overall_ocr_res": {
            "rec_texts": ["第一行曾被版面结构漏掉", "第二行仍被版面结构保留"],
            "rec_boxes": [[0, 0, 110, 18], [0, 30, 110, 50]],
            "rec_scores": [0.98, 0.99],
        },
    }}

    page = page_from_prediction(ImageJob(id="overlap", source_path="unused.png"), preview, raw)

    assert [block.text for block in page.blocks] == [
        "第一行曾被版面结构漏掉",
        "第二行仍被版面结构保留",
    ]
    assert page.blocks[1].bbox == (0.0, 30.0, 110.0, 50.0)


def test_low_confidence_short_fragment_touching_page_edge_is_excluded(tmp_path: Path):
    preview = tmp_path / "processed.png"
    Image.new("RGB", (1000, 1400), "white").save(preview)
    raw = {"res": {
        "parsing_res_list": [{
            "block_id": 1,
            "block_label": "text",
            "block_bbox": [985, 100, 1000, 135],
            "block_content": "晶",
        }],
        "overall_ocr_res": {
            "rec_texts": ["晶"],
            "rec_boxes": [[985, 100, 1000, 135]],
            "rec_scores": [0.53],
        },
    }}

    page = page_from_prediction(ImageJob(id="edge", source_path="unused.png"), preview, raw)

    assert page.blocks == []


def test_text_geometry_does_not_match_same_digit_on_another_part_of_page(tmp_path: Path):
    preview = tmp_path / "processed.png"
    Image.new("RGB", (1000, 1400), "white").save(preview)
    raw = {"res": {
        "parsing_res_list": [{"block_id": 1, "block_label": "text",
                              "block_bbox": [100, 100, 900, 220], "block_content": "编号12正文"}],
        "overall_ocr_res": {"rec_texts": ["编号12正文", "2"],
                            "rec_boxes": [[100, 110, 900, 180], [480, 1300, 500, 1330]],
                            "rec_scores": [0.87, 0.99]},
    }}
    page = page_from_prediction(ImageJob(id="geometry", source_path="unused.png"), preview, raw)
    assert page.blocks[0].bbox == (100.0, 110.0, 900.0, 180.0)
    assert page.blocks[0].minimum_line_confidence == 0.87
    assert [line.text for line in page.blocks[0].text_lines] == ["编号12正文"]


def test_clear_short_text_at_image_edge_is_preserved(tmp_path: Path):
    preview = tmp_path / "page.png"
    Image.new("RGB", (1000, 1400), "white").save(preview)
    raw = {"res": {"overall_ocr_res": {"rec_texts": ["甲"],
        "rec_boxes": [[985, 100, 1000, 135]], "rec_scores": [0.99]}}}
    page = page_from_prediction(ImageJob(id="edge", source_path="unused.png"), preview, raw)
    assert [block.text for block in page.blocks] == ["甲"]


def test_short_orphan_near_page_top_triggers_plain_text_completeness_check():
    suspicious = PageResult(
        id="p", page_index=0, width=1000, height=1400, processed_image_path="unused",
        blocks=[
            Block(id="orphan", kind="text", bbox=(100, 180, 140, 220), original_text="晶"),
            Block(id="body", kind="text", bbox=(100, 240, 900, 800), original_text="其余正文完整" * 20),
        ],
    )
    footer_only = PageResult(
        id="p", page_index=0, width=1000, height=1400, processed_image_path="unused",
        blocks=[Block(id="page", kind="text", bbox=(480, 1320, 520, 1360), original_text="2")],
    )

    assert worker._needs_text_completeness_check(suspicious) is True
    assert worker._needs_text_completeness_check(footer_only) is False


def test_substantially_more_complete_plain_page_replaces_incomplete_layout_text():
    baseline = PageResult(
        id="p", page_index=0, width=1000, height=1400, processed_image_path="processed.png",
        blocks=[Block(id="short", kind="text", bbox=(100, 100, 900, 500), original_text="少量正文")],
    )
    plain = PageResult(
        id="p", page_index=0, width=1000, height=1400, processed_image_path="plain.png",
        blocks=[
            Block(
                id="complete", kind="text", bbox=(100, 100, 900, 500),
                original_text="这是普通文字识别恢复的一整段内容，长度明显超过版面识别结果，因此不能继续静默遗漏。",
            )
        ],
    )

    selected = worker._prefer_complete_plain_page(baseline, plain)

    assert selected is plain
    assert selected.processed_image_path == "processed.png"
    assert selected.recognition_warning == "text_recovery"
    assert all(block.needs_review for block in selected.blocks)


def test_missing_line_regions_find_uncovered_text_band(tmp_path: Path):
    image_path = tmp_path / "page.png"
    image = Image.new("L", (400, 300), "white")
    # Two synthetic printed lines; the existing OCR result covers only the lower one.
    for y in range(70, 83):
        for x in range(55, 345):
            if (x // 7) % 2 == 0:
                image.putpixel((x, y), 20)
    for y in range(150, 163):
        for x in range(55, 345):
            if (x // 7) % 2 == 0:
                image.putpixel((x, y), 20)
    image.save(image_path)
    page = PageResult(
        id="p", page_index=0, width=400, height=300,
        processed_image_path=str(image_path),
        # Layout parsers can return one broad box even when OCR detected only
        # the final line within it.
        blocks=[Block(id="covered", kind="text", bbox=(45, 60, 355, 175), original_text="已有正文")],
    )

    regions = worker._missing_line_regions(
        page,
        image_path,
        recognized_line_boxes=[(45.0, 140.0, 355.0, 175.0)],
    )

    assert len(regions) == 1
    left, top, right, bottom = regions[0]
    assert left < 55 < right
    assert top < 70 < bottom

    oversized_regions = worker._missing_line_regions(
        page,
        image_path,
        recognized_line_boxes=[(45.0, 60.0, 355.0, 175.0)],
    )
    assert len(oversized_regions) == 2


@pytest.mark.parametrize("auto_proofread", [False, True])
@pytest.mark.parametrize("mode", ["structure", "plain"])
def test_worker_recovers_detector_omissions_in_both_modes(tmp_path: Path, monkeypatch, auto_proofread, mode):
    """The first three printed lines must survive even with proofreading off."""
    from PIL import ImageDraw

    source = tmp_path / "page.png"
    image = Image.new("RGB", (600, 800), "white")
    draw = ImageDraw.Draw(image)
    for y in (110, 155, 200, 245):
        for x in range(90, 500, 18):
            draw.rectangle((x, y, x + 10, y + 18), fill="black")
    image.save(source)
    job = ImageJob("p", str(source), auto_proofread=auto_proofread)
    raw = {"parsing_res_list": [{"block_label": "text", "block_bbox": [80, 100, 520, 275],
                                "block_content": "最后一行"}],
           "overall_ocr_res": {"rec_texts": ["最后一行"], "rec_boxes": [[85, 242, 520, 268]], "rec_scores": [0.99]}}

    class Engine:
        def predict(self, _pixels):
            yield raw

    class Sender:
        def __init__(self):
            self.events = []
        def send(self, event):
            self.events.append(event)
        def close(self):
            pass

    values = iter([DetailResult(text, 0.99) for text in ("第一行", "第二行", "第三行")])
    from types import SimpleNamespace
    monkeypatch.setattr(worker, "claim_session_lease", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(worker, "_silence_child_output", lambda: None)
    monkeypatch.setattr(worker, "_create_engine", lambda *_: Engine() if mode == "structure" else None)
    monkeypatch.setattr(worker, "_create_plain_engine", lambda *_: Engine())
    monkeypatch.setattr(worker, "prepare_image", lambda *_: source)
    monkeypatch.setattr(worker, "prepare_image_with_detail", lambda *_: (source, source))
    monkeypatch.setattr(worker._PredictionRuntime, "predict_detail", lambda *_: next(values))
    sender = Sender()
    worker._process_pages("models", str(tmp_path), [job], sender)
    page = next(event["result"] for event in sender.events if event["event"] == "page_done")
    assert [block.original_text for block in page.blocks] == ["第一行", "第二行", "第三行", "最后一行"]
    assert all(block.edited_text is None for block in page.blocks)
    assert all(block.needs_review for block in page.blocks[:3])
    if mode == "plain":
        assert page.recognition_warning == "layout_fallback"


def test_recover_missing_lines_inserts_reviewable_text_in_reading_order(tmp_path: Path):
    image_path = tmp_path / "page.png"
    Image.new("RGB", (400, 300), "white").save(image_path)
    page = PageResult(
        id="p", page_index=0, width=400, height=300,
        processed_image_path=str(image_path),
        blocks=[Block(id="later", kind="text", bbox=(50, 150, 350, 180), original_text="第二行")],
    )

    class Runtime:
        def predict_detail(self, path):
            return DetailResult(text="第一行", confidence=0.97)

    recovered = worker._recover_missing_lines(
        page,
        image_path,
        [(50.0, 65.0, 350.0, 90.0)],
        Runtime(),
        tmp_path,
    )

    assert recovered == 1
    assert [block.text for block in page.blocks] == ["第一行", "第二行"]
    assert page.blocks[0].needs_review is True
    assert page.blocks[0].confidence == 0.97
    assert page.recognition_warning == "text_recovery"


def test_recover_missing_lines_deduplicates_text_already_in_a_broad_layout_block(tmp_path: Path):
    image_path = tmp_path / "page.png"
    Image.new("RGB", (400, 300), "white").save(image_path)
    page = PageResult(
        id="p", page_index=0, width=400, height=300,
        processed_image_path=str(image_path),
        blocks=[Block(id="broad", kind="text", bbox=(50, 60, 350, 180), original_text="第二行")],
    )

    class Runtime:
        def __init__(self):
            self.values = iter(["第一行", "第二行"])

        def predict_detail(self, path):
            return DetailResult(text=next(self.values), confidence=0.98)

    count = worker._recover_missing_lines(
        page,
        image_path,
        [(50.0, 65.0, 350.0, 90.0), (50.0, 145.0, 350.0, 170.0)],
        Runtime(),
        tmp_path,
    )

    assert count == 1
    assert [block.text for block in page.blocks] == ["第一行", "第二行"]


@pytest.mark.parametrize("kind", ["image", "table"])
def test_missing_line_scan_does_not_transcribe_lines_inside_illustrations_or_tables(tmp_path: Path, kind):
    from PIL import ImageDraw

    image_path = tmp_path / "figure.png"
    image = Image.new("L", (400, 300), "white")
    ImageDraw.Draw(image).rectangle((60, 75, 340, 88), fill=20)
    image.save(image_path)
    page = PageResult(
        id="p", page_index=0, width=400, height=300, processed_image_path=str(image_path),
        blocks=[Block(id="figure", kind=kind, bbox=(40, 40, 360, 250))],
    )
    assert worker._missing_line_regions(page, image_path, recognized_line_boxes=[]) == []


def test_missing_line_recovery_preserves_identical_text_at_different_positions(tmp_path: Path):
    image_path = tmp_path / "page.png"
    Image.new("RGB", (400, 500), "white").save(image_path)
    page = PageResult(
        id="p", page_index=0, width=400, height=500, processed_image_path=str(image_path),
        blocks=[Block(id="first", kind="text", bbox=(50, 50, 350, 80), original_text="重复的标题")],
    )

    class Runtime:
        def predict_detail(self, path):
            return DetailResult(text="重复的标题", confidence=0.98)

    assert worker._recover_missing_lines(
        page, image_path, [(50.0, 350.0, 350.0, 380.0)], Runtime(), tmp_path,
    ) == 1
    assert [block.text for block in page.blocks] == ["重复的标题", "重复的标题"]


def test_sync_processed_preview_replaces_source_with_paddlex_oriented_bgr_image(tmp_path: Path):
    processed = tmp_path / "processed.png"
    Image.new("RGB", (2, 1), "white").save(processed)
    prediction = {"doc_preprocessor_res": {
        "angle": 90,
        "output_img": np.array([[[0, 0, 255], [0, 255, 0]]], dtype=np.uint8),
    }}

    _sync_processed_preview(prediction, processed)

    with Image.open(processed) as image:
        assert image.size == (2, 1)
        assert [image.getpixel((index, 0)) for index in range(2)] == [(255, 0, 0), (0, 255, 0)]


def test_region_candidates_join_all_recognized_lines_into_one_editable_candidate():
    candidates = region_candidates_from_prediction({"res": {"overall_ocr_res": {"rec_texts": ["第一行", "第二行"]}}})

    assert candidates == ["第一行\n第二行"]


def test_process_pages_reprepares_current_job_settings_after_page_started(tmp_path: Path, monkeypatch):
    source = tmp_path / "source.png"
    Image.new("RGB", (4, 4), "white").save(source)
    processed = tmp_path / "processed.png"
    Image.new("RGB", (4, 4), "white").save(processed)
    job = ImageJob(id="current", source_path=str(source), rotation=90)
    order: list[str] = []

    class Lease:
        def close(self):
            order.append("lease_closed")

    class Engine:
        def predict(self, _: str):
            yield {"res": {}}

    class Sender:
        def send(self, event):
            order.append(event["event"])

        def close(self):
            order.append("sender_closed")

    def prepare(current_job, _: Path):
        assert current_job.rotation == 90
        order.append("prepared")
        return processed

    monkeypatch.setattr(worker, "_silence_child_output", lambda: None)
    monkeypatch.setattr(worker, "claim_session_lease", lambda _: Lease())
    monkeypatch.setattr(worker, "_create_engine", lambda *_: Engine())
    monkeypatch.setattr(worker, "prepare_image", prepare)
    monkeypatch.setattr(worker, "_sync_processed_preview", lambda _, path: path)
    monkeypatch.setattr(worker, "page_from_prediction", lambda *_: PageResult(
        id=job.id, page_index=0, width=4, height=4, processed_image_path=str(processed)))
    monkeypatch.setattr(worker, "_save_figure_crops", lambda *_: None)

    worker._process_pages("models", str(tmp_path), [job], Sender())

    assert [step for step in order if step != "page_stage"][:2] == ["page_started", "prepared"]
    assert [step for step in order if step != "page_stage"][2:] == ["page_done", "lease_closed", "finished", "sender_closed"]


def test_process_pages_passes_bgr_pixels_instead_of_a_windows_unicode_path(tmp_path: Path, monkeypatch):
    source = tmp_path / "中文目录" / "原图.png"
    source.parent.mkdir()
    Image.new("RGB", (4, 3), (10, 20, 30)).save(source)
    job = ImageJob(id="unicode", source_path=str(source))
    inputs = []

    class Lease:
        def close(self):
            pass

    class Engine:
        def predict(self, value):
            inputs.append(value)
            yield {"res": {}}

    class Sender:
        def send(self, _event):
            pass

        def close(self):
            pass

    monkeypatch.setattr(worker, "_silence_child_output", lambda: None)
    monkeypatch.setattr(worker, "claim_session_lease", lambda _: Lease())
    monkeypatch.setattr(worker, "_create_engine", lambda *_: Engine())
    monkeypatch.setattr(worker, "prepare_image", lambda *_: source)
    monkeypatch.setattr(worker, "_sync_processed_preview", lambda _, path: path)
    monkeypatch.setattr(
        worker,
        "page_from_prediction",
        lambda *_: PageResult(
            id=job.id,
            page_index=0,
            width=4,
            height=3,
            processed_image_path=str(source),
            blocks=[],
        ),
    )
    monkeypatch.setattr(worker, "_save_figure_crops", lambda *_: None)

    worker._process_pages("models", str(tmp_path), [job], Sender())

    assert len(inputs) == 1
    assert isinstance(inputs[0], np.ndarray)
    assert inputs[0].shape == (3, 4, 3)
    assert tuple(inputs[0][0, 0]) == (30, 20, 10)


def test_process_pages_reports_unexpected_startup_failure_for_every_job(tmp_path: Path, monkeypatch):
    jobs = [
        ImageJob(id="first", source_path="first.png"),
        ImageJob(id="second", source_path="second.png"),
    ]

    class Sender:
        def __init__(self):
            self.events = []

        def send(self, event):
            self.events.append(event)

        def close(self):
            pass

    sender = Sender()
    monkeypatch.setattr(worker, "_silence_child_output", lambda: None)
    monkeypatch.setattr(
        worker,
        "claim_session_lease",
        lambda _: (_ for _ in ()).throw(RuntimeError("private startup detail")),
    )

    worker._process_pages("models", str(tmp_path), jobs, sender)

    assert sender.events == [
        {"event": "page_error", "job_id": "first", "code": "ocr_worker_failed"},
        {"event": "page_error", "job_id": "second", "code": "ocr_worker_failed"},
        {"event": "finished"},
    ]


def test_proofread_variants_apply_the_required_filter_without_changing_baseline(tmp_path: Path):
    baseline = tmp_path / "baseline.png"
    image = Image.new("RGB", (9, 9))
    image.putdata([
        ((index * 29) % 255, (index * 47) % 255, (index * 71) % 255)
        for index in range(81)
    ])
    image.save(baseline)
    with Image.open(baseline) as original:
        baseline_pixels = list(original.getdata())
        expected_contrast = list(ImageEnhance.Contrast(original).enhance(1.12).getdata())
        expected_sharp = list(original.filter(ImageFilter.UnsharpMask(radius=1, percent=115, threshold=3)).getdata())

    contrast = worker._proofread_variant(baseline, tmp_path / "contrast.png", 2)
    sharpened = worker._proofread_variant(baseline, tmp_path / "sharpened.png", 3)

    with Image.open(baseline) as original:
        assert list(original.getdata()) == baseline_pixels
    with Image.open(contrast) as enhanced:
        assert list(enhanced.getdata()) == expected_contrast
    with Image.open(sharpened) as enhanced:
        assert list(enhanced.getdata()) == expected_sharp


def test_auto_proofread_runs_three_sequential_predictions_and_emits_progress(tmp_path: Path, monkeypatch):
    source = tmp_path / "source.png"
    processed = tmp_path / "processed.png"
    Image.new("RGB", (16, 12), "white").save(source)
    Image.new("RGB", (16, 12), "white").save(processed)
    job = ImageJob(id="p", source_path=str(source), auto_proofread=True)
    saved: list[Path] = []

    class Lease:
        def close(self):
            pass

    class Engine:
        def __init__(self):
            self.calls: list[np.ndarray] = []
            self.active = 0
            self.active_peak = 0

        def predict(self, pixels):
            self.calls.append(pixels)
            self.active += 1
            self.active_peak = max(self.active_peak, self.active)
            try:
                yield {"pass": len(self.calls)}
            finally:
                self.active -= 1

    class Sender:
        def __init__(self):
            self.events: list[dict] = []

        def send(self, event):
            self.events.append(event)

        def close(self):
            pass

    engine = Engine()
    sender = Sender()

    def page_for_prediction(current_job, path, prediction):
        return PageResult(
            id=current_job.id,
            page_index=0,
            width=16,
            height=12,
            processed_image_path=str(path),
            blocks=[Block(id="body", kind="text", bbox=(0, 0, 10, 10), original_text=f"第 {prediction['pass']} 次")],
        )

    monkeypatch.setattr(worker, "_silence_child_output", lambda: None)
    monkeypatch.setattr(worker, "claim_session_lease", lambda _: Lease())
    monkeypatch.setattr(worker, "_create_engine", lambda *_: engine)
    monkeypatch.setattr(worker, "prepare_image_with_detail", lambda *_: (processed, processed))
    monkeypatch.setattr(worker, "_sync_processed_preview", lambda _, path: path)
    monkeypatch.setattr(worker, "page_from_prediction", page_for_prediction)
    monkeypatch.setattr(worker, "_save_figure_crops", lambda page, _: saved.append(Path(page.processed_image_path)))

    worker._process_pages("models", str(tmp_path), [job], sender)

    assert [event["pass_index"] for event in sender.events if event["event"] == "page_progress"] == [1, 2, 3]
    assert engine.active_peak == 1
    assert len(engine.calls) == 3
    assert all(isinstance(pixels, np.ndarray) for pixels in engine.calls)
    assert sender.events[-2]["event"] == "page_done"
    assert sender.events[-2]["result"].proofread_completed_passes == 3
    assert saved == [processed]


@pytest.mark.parametrize(("failed_pass", "completed_passes"), [(2, 1), (3, 2)])
def test_enhanced_pass_failure_publishes_exportable_incomplete_baseline(
    tmp_path: Path,
    monkeypatch,
    failed_pass: int,
    completed_passes: int,
):
    source = tmp_path / "source.png"
    processed = tmp_path / "processed.png"
    Image.new("RGB", (16, 12), "white").save(source)
    Image.new("RGB", (16, 12), "white").save(processed)
    job = ImageJob(id="p", source_path=str(source), auto_proofread=True)
    saved: list[Path] = []

    class Lease:
        def close(self):
            pass

    class Engine:
        def __init__(self):
            self.calls = 0

        def predict(self, _path: str):
            self.calls += 1
            if self.calls == failed_pass:
                raise RuntimeError("enhanced pass failed")
            yield {"pass": self.calls}

    class Sender:
        def __init__(self):
            self.events: list[dict] = []

        def send(self, event):
            self.events.append(event)

        def close(self):
            pass

    sender = Sender()

    monkeypatch.setattr(worker, "_silence_child_output", lambda: None)
    monkeypatch.setattr(worker, "claim_session_lease", lambda _: Lease())
    monkeypatch.setattr(worker, "_create_engine", lambda *_: Engine())
    monkeypatch.setattr(worker, "prepare_image_with_detail", lambda *_: (processed, processed))
    monkeypatch.setattr(worker, "_sync_processed_preview", lambda _, path: path)
    monkeypatch.setattr(
        worker,
        "page_from_prediction",
        lambda current_job, path, _prediction: PageResult(
            id=current_job.id,
            page_index=0,
            width=16,
            height=12,
            processed_image_path=str(path),
            blocks=[Block(id="body", kind="text", bbox=(0, 0, 10, 10), original_text="基线")],
        ),
    )
    monkeypatch.setattr(worker, "_save_figure_crops", lambda page, _: saved.append(Path(page.processed_image_path)))

    worker._process_pages("models", str(tmp_path), [job], sender)

    done = next(event for event in sender.events if event["event"] == "page_done")
    assert done["result"].proofread_status == "incomplete"
    assert done["result"].proofread_completed_passes == completed_passes
    assert done["result"].processed_image_path == str(processed)
    assert saved == [processed]
    assert not any(event["event"] == "page_error" for event in sender.events)


def test_auto_proofread_first_prediction_failure_remains_page_error(tmp_path: Path, monkeypatch):
    processed = tmp_path / "processed.png"
    Image.new("RGB", (16, 12), "white").save(processed)
    job = ImageJob(id="p", source_path="unused.png", auto_proofread=True)

    class Lease:
        def close(self):
            pass

    class Engine:
        def predict(self, _path: str):
            raise RuntimeError("baseline failed")

    class Sender:
        def __init__(self):
            self.events: list[dict] = []

        def send(self, event):
            self.events.append(event)

        def close(self):
            pass

    sender = Sender()
    monkeypatch.setattr(worker, "_silence_child_output", lambda: None)
    monkeypatch.setattr(worker, "claim_session_lease", lambda _: Lease())
    monkeypatch.setattr(worker, "_create_engine", lambda *_: Engine())
    monkeypatch.setattr(worker, "_create_plain_engine", lambda *_: (_ for _ in ()).throw(RuntimeError("fallback failed")))
    monkeypatch.setattr(worker, "prepare_image_with_detail", lambda *_: (processed, processed))

    worker._process_pages("models", str(tmp_path), [job], sender)

    assert [event["event"] for event in sender.events if event["event"] != "page_stage"] == ["page_started", "page_error", "finished"]
    assert next(event for event in sender.events if event["event"] == "page_error")["code"] == "ocr_inference_failed"


def test_local_rerecognition_still_uses_one_prediction(tmp_path: Path, monkeypatch):
    session_dir = tmp_path / "session"
    processed_dir = session_dir / "processed"
    processed_dir.mkdir(parents=True)
    processed = tmp_path / "processed.png"
    Image.new("RGB", (16, 12), "white").save(processed)
    job = ImageJob(
        id="p",
        source_path="unused.png",
        result=PageResult(id="p", page_index=0, width=16, height=12, processed_image_path=str(processed)),
    )

    class Lease:
        def close(self):
            pass

    class Engine:
        def __init__(self):
            self.calls: list[np.ndarray] = []

        def predict(self, pixels):
            self.calls.append(pixels)
            yield {"res": {"overall_ocr_res": {"rec_texts": ["局部文字"]}}}

    class Sender:
        def __init__(self):
            self.events: list[dict] = []

        def send(self, event):
            self.events.append(event)

        def close(self):
            pass

    engine = Engine()
    sender = Sender()
    monkeypatch.setattr(worker, "_silence_child_output", lambda: None)
    monkeypatch.setattr(worker, "claim_session_lease", lambda _: Lease())
    monkeypatch.setattr(worker, "validate_models", lambda *_: None)
    monkeypatch.setattr(worker, "configure_offline_env", lambda *_: None)
    monkeypatch.setattr(worker, "deny_network", lambda: None)
    monkeypatch.setattr(worker, "_create_engine", lambda *_: pytest.fail("region must not load layout/table models"))
    monkeypatch.setattr(worker, "_create_plain_engine", lambda *_: engine)

    worker._process_region("models", str(session_dir), job, (0, 0, 16, 12), sender)

    assert len(engine.calls) == 1
    assert isinstance(engine.calls[0], np.ndarray)
    assert sender.events[0] == {"event": "region_done", "job_id": "p", "candidates": ["局部文字"]}


@pytest.mark.parametrize('proofread', [False, True])
def test_both_recognition_modes_publish_source_style_before_page_done(tmp_path, monkeypatch, proofread):
    from offline_word import source_style
    source = tmp_path/'source.png'
    Image.new('RGB', (16, 12), 'white').save(source)
    job = ImageJob('p', str(source), auto_proofread=proofread)
    events = []

    class Lease:
        def close(self): pass

    class Sender:
        def send(self, event): events.append(event)
        def close(self): pass

    class Engine:
        def predict(self, _):
            yield {'parsing_res_list': [{'block_label': 'text', 'block_bbox': [1,1,15,11], 'block_content': '保留原文'}]}

    monkeypatch.setattr(worker, '_silence_child_output', lambda: None)
    monkeypatch.setattr(worker, 'claim_session_lease', lambda _: Lease())
    monkeypatch.setattr(worker, '_create_engine', lambda *_: Engine())
    monkeypatch.setattr(worker, '_prepare_for_worker', lambda *_: source)
    monkeypatch.setattr(worker, '_prepare_with_detail_for_worker', lambda *_: (source, source))
    monkeypatch.setattr(worker, '_save_figure_crops', lambda *_: None)
    monkeypatch.setattr(source_style, 'annotate_source_style', lambda page: setattr(page.blocks[0], 'font_name', '仿宋'))
    worker._process_pages('models', str(tmp_path), [job], Sender())
    page = next(event['result'] for event in events if event['event']=='page_done')
    assert page.blocks[0].font_name == '仿宋'
    assert page.blocks[0].original_text == '保留原文'


def test_local_rerecognition_recovers_large_missing_lines_in_short_crop(tmp_path, monkeypatch):
    from PIL import ImageDraw
    image = Image.new('RGB', (800, 280), 'white')
    draw = ImageDraw.Draw(image)
    for y in (20, 80, 140, 200):
        # Separate glyph components, not a single solid decorative rule.
        for x in range(20, 780, 32):
            draw.rectangle((x, y, x+22, y+25), fill='black')
    source = tmp_path/'source.png'
    image.save(source)
    job = ImageJob('p', str(source), result=PageResult('p', 0, 800, 280, str(source)))
    events = []

    class Sender:
        def send(self, event): events.append(event)
        def close(self): pass

    class Lease:
        def close(self): pass

    class Plain:
        def predict(self, pixels):
            padding = (pixels.shape[1]-800)//2
            yield {'rec_texts': ['第四行'], 'rec_scores': [.98],
                   'rec_boxes': [[20+padding, 200+padding, 780+padding, 225+padding]]}

    class Detail:
        texts = iter(['第一行', '第二行', '第三行'])
        def predict(self, pixels):
            yield {'rec_text': next(self.texts), 'rec_score': .98}

    for name in ('_silence_child_output', 'validate_models', 'configure_offline_env', 'deny_network'):
        monkeypatch.setattr(worker, name, lambda *args: None)
    monkeypatch.setattr(worker, 'claim_session_lease', lambda _: Lease())
    monkeypatch.setattr(worker, '_create_engine', lambda *_: pytest.fail('region loaded layout models'))
    monkeypatch.setattr(worker, '_create_plain_engine', lambda *_: Plain())
    monkeypatch.setattr(worker, '_create_detail_engine', lambda *_: Detail())
    worker._process_region('models', str(tmp_path/'session'), job, (0, 0, 800, 280), Sender())
    assert events[0]['candidates'] == ['第一行\n第二行\n第三行\n第四行']
    assert events[-1]['event'] == 'finished'
    assert not job.result.blocks


def test_structure_failure_falls_back_to_plain_ocr_and_keeps_page_exportable(tmp_path: Path, monkeypatch):
    processed = tmp_path / "processed.png"
    Image.new("RGB", (16, 12), "white").save(processed)
    job = ImageJob(id="p", source_path="unused.png")

    class Lease:
        def close(self):
            pass

    class StructureEngine:
        def predict(self, _pixels):
            raise RuntimeError("private structure failure")

    class PlainEngine:
        def predict(self, _pixels):
            yield {"res": {"rec_texts": ["已保留文字"], "rec_scores": [0.96], "rec_boxes": [[1, 2, 15, 10]]}}

    class Sender:
        def __init__(self):
            self.events = []

        def send(self, event):
            self.events.append(event)

        def close(self):
            pass

    sender = Sender()
    monkeypatch.setattr(worker, "_silence_child_output", lambda: None)
    monkeypatch.setattr(worker, "claim_session_lease", lambda _: Lease())
    monkeypatch.setattr(worker, "_create_engine", lambda *_: StructureEngine())
    monkeypatch.setattr(worker, "_create_plain_engine", lambda *_: PlainEngine())
    monkeypatch.setattr(worker, "prepare_image", lambda *_: processed)
    monkeypatch.setattr(worker, "_save_figure_crops", lambda *_: None)

    worker._process_pages("models", str(tmp_path), [job], sender)

    done = next(event for event in sender.events if event["event"] == "page_done")
    assert done["result"].blocks[0].text == "已保留文字"
    assert done["result"].recognition_warning == "layout_fallback"
    assert not any(event["event"] == "page_error" for event in sender.events)


def test_structure_initialization_failure_also_falls_back_to_plain_ocr(tmp_path: Path, monkeypatch):
    processed = tmp_path / "processed.png"
    Image.new("RGB", (16, 12), "white").save(processed)
    job = ImageJob(id="p", source_path="unused.png")

    class Lease:
        def close(self):
            pass

    class PlainEngine:
        def predict(self, _pixels):
            yield {"res": {"rec_texts": ["初始化降级"], "rec_scores": [0.95], "rec_boxes": [[1, 2, 15, 10]]}}

    class Sender:
        def __init__(self):
            self.events = []

        def send(self, event):
            self.events.append(event)

        def close(self):
            pass

    sender = Sender()
    monkeypatch.setattr(worker, "_silence_child_output", lambda: None)
    monkeypatch.setattr(worker, "claim_session_lease", lambda _: Lease())
    monkeypatch.setattr(worker, "_create_engine", lambda *_: (_ for _ in ()).throw(OfflineError("ocr_init_failed")))
    monkeypatch.setattr(worker, "_create_plain_engine", lambda *_: PlainEngine())
    monkeypatch.setattr(worker, "prepare_image", lambda *_: processed)
    monkeypatch.setattr(worker, "_save_figure_crops", lambda *_: None)

    worker._process_pages("models", str(tmp_path), [job], sender)

    done = next(event for event in sender.events if event["event"] == "page_done")
    assert done["result"].blocks[0].text == "初始化降级"
    assert done["result"].recognition_warning == "layout_fallback"


def test_unexpected_prepare_failure_reports_stage_code(tmp_path: Path, monkeypatch):
    job = ImageJob(id="p", source_path="unused.png")

    class Lease:
        def close(self):
            pass

    class Sender:
        def __init__(self):
            self.events = []

        def send(self, event):
            self.events.append(event)

        def close(self):
            pass

    sender = Sender()
    monkeypatch.setattr(worker, "_silence_child_output", lambda: None)
    monkeypatch.setattr(worker, "claim_session_lease", lambda _: Lease())
    monkeypatch.setattr(worker, "_create_engine", lambda *_: object())
    monkeypatch.setattr(worker, "prepare_image", lambda *_: (_ for _ in ()).throw(RuntimeError("private image detail")))

    worker._process_pages("models", str(tmp_path), [job], sender)

    assert next(event for event in sender.events if event["event"] == "page_error") == {"event": "page_error", "job_id": "p", "code": "image_prepare_failed"}


def test_failed_structure_engine_is_retained_but_not_retried(tmp_path: Path, monkeypatch):
    image = tmp_path / "page.png"
    Image.new("RGB", (8, 6), "white").save(image)

    class StructureEngine:
        def __init__(self):
            self.calls = 0

        def predict(self, _pixels):
            self.calls += 1
            raise RuntimeError("private failure")

    class PlainEngine:
        def predict(self, _pixels):
            yield {"res": {"rec_texts": []}}

    structure = StructureEngine()
    runtime = worker._PredictionRuntime(tmp_path / "models", tmp_path, structure)
    monkeypatch.setattr(worker, "_create_plain_engine", lambda *_: PlainEngine())

    assert runtime.predict_baseline(image)[0] == "plain"
    assert runtime.predict_baseline(image)[0] == "plain"
    assert structure.calls == 1
    assert runtime.structure_engine is structure


def test_plain_engine_initialization_failure_is_not_retried_for_every_page(tmp_path: Path, monkeypatch):
    image = tmp_path / "page.png"
    Image.new("RGB", (8, 6), "white").save(image)
    runtime = worker._PredictionRuntime(tmp_path / "models", tmp_path, None)
    attempts = 0

    def fail(*_args):
        nonlocal attempts
        attempts += 1
        raise OfflineError("ocr_fallback_init_failed")

    monkeypatch.setattr(worker, "_create_plain_engine", fail)

    for _ in range(2):
        with pytest.raises(OfflineError, match="ocr_fallback_init_failed"):
            runtime.predict_baseline(image)
    assert attempts == 1


def test_process_pages_closes_lease_before_terminal_event_after_startup_error(tmp_path: Path, monkeypatch):
    """Would fail if GUI can terminate a completed child before its lease cleanup."""
    job = ImageJob(id="current", source_path="never-read.png")
    order: list[str] = []

    class Lease:
        def close(self):
            order.append("lease_closed")

    class Sender:
        def send(self, event):
            order.append(event["event"])

        def close(self):
            order.append("sender_closed")

    monkeypatch.setattr(worker, "_silence_child_output", lambda: None)
    monkeypatch.setattr(worker, "claim_session_lease", lambda _: Lease())
    monkeypatch.setattr(worker, "_create_engine", lambda *_: (_ for _ in ()).throw(OfflineError("ocr_init_failed")))
    monkeypatch.setattr(worker, "_create_plain_engine", lambda *_: (_ for _ in ()).throw(OfflineError("ocr_fallback_init_failed")))

    worker._process_pages("models", str(tmp_path), [job], Sender())

    assert [step for step in order if step != "page_stage"] == ["page_started", "page_error", "lease_closed", "finished", "sender_closed"]


def test_start_process_failure_releases_pipe_state_before_safe_close(tmp_path: Path, monkeypatch):
    """Would fail if a spawn OSError leaves an unstarted process marked busy."""
    worker_instance = worker.OCRWorker(tmp_path, tmp_path)

    def fail_start(_process):
        raise OSError("spawn_failed")

    monkeypatch.setattr(worker.mp.context.SpawnProcess, "start", fail_start)

    with pytest.raises(OSError, match="spawn_failed"):
        worker_instance._start_process(lambda: None, ())

    assert worker_instance.busy is False
    assert worker_instance._process is None
    assert worker_instance._receiver is None
    worker_instance.close()


def test_child_entry_skips_unstable_native_atexit_after_target_cleanup(monkeypatch):
    """Would fail if Paddle objects reached Python finalization after a clean worker run."""
    events: list[object] = []

    class NativeGuard:
        def __del__(self):
            events.append("native_released")

    def target(value):
        events.append(("target", value))
        return NativeGuard()

    monkeypatch.setattr(worker.os, "_exit", lambda code: events.append(("exit", code)))

    worker._child_entry(target, ("finished",))

    assert events == [("target", "finished"), ("exit", 0), "native_released"]


def test_child_entry_reports_unhandled_target_failure_without_python_finalization(monkeypatch):
    """Would fail if an unexpected child exception were converted into a successful exit."""
    exits: list[int] = []

    def fail():
        raise RuntimeError("private details must not escape")

    monkeypatch.setattr(worker.os, "_exit", exits.append)

    worker._child_entry(fail, ())

    assert exits == [1]


def test_cancel_keeps_lingering_child_busy_and_refuses_a_second_process(tmp_path: Path):
    worker_instance = worker.OCRWorker(tmp_path, tmp_path)

    class LingeringProcess:
        def __init__(self):
            self.terminated = self.killed = 0

        def is_alive(self):
            return True

        def terminate(self):
            self.terminated += 1

        def kill(self):
            self.killed += 1

        def join(self, timeout=0):
            return None

    lingering = LingeringProcess()
    worker_instance._process = lingering

    worker_instance.cancel()

    assert worker_instance.busy is True
    assert (lingering.terminated, lingering.killed) == (1, 1)
    with pytest.raises(OfflineError, match="worker_still_running"):
        worker_instance._start_process(lambda: None, ())
