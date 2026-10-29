import json
from pathlib import Path

import pytest

from tools.benchmark import (
    cer,
    extract_original_text,
    extract_effective_text,
    summarize_auto_rows,
    summarize_results,
    build_job_specs,
    restart_passed,
    _page_change_counts,
    _public_rows,
    _capture_job_row,
    _candidate_assessment,
    _auto_proofread_acceptance,
    _class_summary,
)
from tools.generate_samples import CANVAS_SIZE, _photo_variant, generate_dataset
from offline_word.domain import Block, Cell, ImageJob, PageResult
from offline_word.proofread import reconcile_page


def test_cer_ignores_whitespace_but_counts_punctuation_digits_and_case():
    result = cer("甲 A-12，乙", "甲A-13,乙")

    assert result == {"errors": 2, "reference": 7, "cer": pytest.approx(2 / 7)}


def test_failed_pages_remain_in_cer_denominator_and_are_reported():
    rows = [
        {"case_id": "scan-001", "class": "scan", "reference": "甲乙", "observed": "甲", "error": None},
        {"case_id": "scan-002", "class": "scan", "reference": "丙丁", "observed": "", "error": "ocr_failed"},
    ]

    summary = summarize_results(rows)

    assert summary["pages"] == 2
    assert summary["failed_pages"] == 1
    assert summary["characters"] == 4
    assert summary["errors"] == 3
    assert summary["cer"] == pytest.approx(3 / 4)
    assert summary["failed_case_ids"] == ["scan-002"]


def test_auto_proofread_summary_aggregates_review_candidates_by_class():
    rows = [
        {
            "case_id": "scan-01", "class": "scan", "reference": "甲乙",
            "baseline_observed": "甲丙", "auto_observed": "甲丙",
            "baseline_preserved": True, "candidate_units": 2,
            "auto_changes": 0, "correct_changes": 0, "wrong_changes": 0,
            "unresolved_changes": 0, "unresolved_conflicts": 0, "error": None,
        },
        {
            "case_id": "scan-02", "class": "scan", "reference": "丙丁",
            "baseline_observed": "丙戊", "auto_observed": "丙戊",
            "baseline_preserved": True, "candidate_units": 1,
            "auto_changes": 0, "correct_changes": 0, "wrong_changes": 0,
            "unresolved_changes": 0, "unresolved_conflicts": 1, "error": None,
        },
    ]

    report = summarize_auto_rows(rows)

    assert report["scan"]["candidate_units"] == 3
    assert report["scan"]["auto_changes"] == 0
    assert report["scan"]["correct_changes"] == 0
    assert report["scan"]["wrong_changes"] == 0
    assert report["scan"]["unresolved_changes"] == 0
    assert report["scan"]["unresolved_conflicts"] == 1
    assert report["scan"]["detail_reviewed_regions"] == 0
    assert report["scan"]["detail_candidate_regions"] == 0
    assert report["scan"]["candidate_improved_units"] == 0
    assert report["scan"]["candidate_worsened_only_units"] == 0
    assert report["scan"]["candidate_neutral_units"] == 0
    assert report["scan"]["candidate_assisted"]["cer"] >= 0
    assert report["scan"]["baseline"]["cer"] >= 0
    assert report["scan"]["auto_proofread"]["cer"] >= 0


def test_extract_effective_text_uses_confirmed_edits_for_blocks_and_cells():
    page = PageResult(
        id="p", page_index=0, width=10, height=10, processed_image_path="private/path.png",
        blocks=[
            Block(id="t", kind="text", bbox=(0, 0, 1, 1), original_text="原文", edited_text="校对"),
            Block(id="table", kind="table", bbox=(0, 0, 1, 1), cells=[
                Cell(row=0, col=1, original_text="乙", edited_text="改乙"),
                Cell(row=0, col=0, original_text="甲"),
            ]),
        ],
    )

    assert extract_effective_text(page) == "校对\n甲\n改乙"


def test_extract_original_text_reads_table_cells_in_row_col_order_before_edits():
    page = PageResult(
        id="p", page_index=0, width=10, height=10, processed_image_path="private/path.png",
        blocks=[
            Block(id="t", kind="text", bbox=(0, 0, 1, 1), original_text="正文", edited_text="已编辑"),
            Block(id="table", kind="table", bbox=(0, 0, 1, 1), cells=[
                Cell(1, 1, original_text="丁", edited_text="错"),
                Cell(0, 1, original_text="乙"),
                Cell(0, 0, original_text="甲"),
            ]),
        ],
    )

    assert extract_original_text(page) == "正文\n甲\n乙\n丁"


def test_capture_preserves_baseline_and_counts_real_majority_candidates_per_text_and_table_cell():
    baseline = PageResult(
        id="p", page_index=0, width=10, height=10, processed_image_path="private/path.png",
        blocks=[
            Block(id="text", kind="text", bbox=(0, 0, 1, 1), original_text="caT"),
            Block(id="table", kind="table", bbox=(0, 1, 1, 2), rows=1, cols=2, cells=[
                Cell(row=0, col=0, original_text="dog"),
                Cell(row=0, col=1, original_text="q"),
            ]),
        ],
    )
    comparisons = [
        PageResult(
            id="p", page_index=0, width=10, height=10, processed_image_path="private/path.png",
            blocks=[
                Block(id="text", kind="text", bbox=(0, 0, 1, 1), original_text="cat"),
                Block(id="table", kind="table", bbox=(0, 1, 1, 2), rows=1, cols=2, cells=[
                    Cell(row=0, col=0, original_text="dig"),
                    Cell(row=0, col=1, original_text="z"),
                ]),
            ],
        ),
        PageResult(
            id="p", page_index=0, width=10, height=10, processed_image_path="private/path.png",
            blocks=[
                Block(id="text", kind="text", bbox=(0, 0, 1, 1), original_text="cat"),
                Block(id="table", kind="table", bbox=(0, 1, 1, 2), rows=1, cols=2, cells=[
                    Cell(row=0, col=0, original_text="dig"),
                    Cell(row=0, col=1, original_text="z"),
                ]),
            ],
        ),
    ]
    job = ImageJob(
        id="p",
        source_path="private/input.png",
        status="done",
        result=reconcile_page(baseline, comparisons),
    )
    row = {"case_id": "scan-01", "class": "scan", "reference": "cat\ndog\ny"}

    _capture_job_row(row, job)

    assert _page_change_counts(job.result) == (0, 0)
    assert row["baseline_preserved"] is True
    assert row["candidate_units"] == 3
    assert row["auto_changes"] == 0
    assert row["correct_changes"] == 0
    assert row["wrong_changes"] == 0
    assert row["unresolved_changes"] == 0
    assert row["unresolved_conflicts"] == 0
    assert row["baseline_errors"] == 2
    assert row["auto_errors"] == 2
    assert row["detail_reviewed_regions"] == 0
    assert row["detail_candidate_regions"] == 0
    assert row["candidate_improved_units"] == 1
    assert row["candidate_worsened_only_units"] == 1
    assert row["candidate_neutral_units"] == 1
    assert row["candidate_assisted_errors"] == 1


def test_candidate_assessment_measures_manual_best_case_without_editing_page():
    page = PageResult(
        id="p", page_index=0, width=10, height=10, processed_image_path="private/path.png",
        detail_reviewed_regions=3, detail_candidate_regions=2, detail_review_status="candidate",
        blocks=[
            Block(id="text", kind="text", bbox=(0, 0, 1, 1), original_text="caT", candidates=["caT", "cat"]),
            Block(id="table", kind="table", bbox=(0, 1, 1, 2), rows=1, cols=2, cells=[
                Cell(row=0, col=0, original_text="dog", candidates=["dog", "dig"]),
                Cell(row=0, col=1, original_text="q", candidates=["q", "z"]),
            ]),
        ],
    )

    assessment = _candidate_assessment(page, "cat\ndog\ny")

    assert assessment == {
        "candidate_improved_units": 1,
        "candidate_worsened_only_units": 1,
        "candidate_neutral_units": 1,
        "candidate_assisted_errors": 1,
        "candidate_assisted_cer": pytest.approx(1 / 7),
    }
    assert extract_original_text(page) == "caT\ndog\nq"
    assert all(unit.edited_text is None for unit in _page_units_for_test(page))


def _page_units_for_test(page: PageResult):
    for block in page.blocks:
        if block.kind == "table" and block.cells:
            yield from block.cells
        else:
            yield block


def test_public_rows_keep_status_codes_but_drop_text_and_paths():
    rows = [{
        "case_id": "scan-01", "class": "scan", "reference": "PRIVATE OCR TEXT",
        "baseline_observed": "PRIVATE OCR TEXT", "auto_observed": "PRIVATE OCR TEXT",
        "baseline_preserved": True, "candidate_units": 2,
        "detail_reviewed_regions": 3, "detail_candidate_regions": 1,
        "detail_review_status": "candidate",
        "candidate_improved_units": 1, "candidate_worsened_only_units": 0,
        "candidate_neutral_units": 1, "candidate_assisted_errors": 0,
        "candidate_assisted_cer": 0.0,
        "reference_chars": 3, "errors": 1, "cer": 1 / 3,
        "error": "/private/path/PRIVATE OCR TEXT",
    }]

    public = _public_rows(rows)

    assert public[0]["case_id"] == "scan-01"
    assert public[0]["baseline_preserved"] is True
    assert public[0]["candidate_units"] == 2
    assert public[0]["detail_reviewed_regions"] == 3
    assert public[0]["detail_candidate_regions"] == 1
    assert public[0]["detail_review_status"] == "candidate"
    assert public[0]["candidate_improved_units"] == 1
    assert public[0]["candidate_worsened_only_units"] == 0
    assert public[0]["candidate_neutral_units"] == 1
    assert public[0]["candidate_assisted_errors"] == 0
    assert public[0]["unresolved_changes"] == 0
    assert public[0]["error"] == "unknown_error"
    serialized = json.dumps(public, ensure_ascii=False)
    assert "PRIVATE OCR TEXT" not in serialized
    assert "/private/path" not in serialized


def test_capture_records_manual_effective_text_without_counting_it_as_automatic():
    job = ImageJob(id="p", source_path="private/input.png", status="done", result=PageResult(
        id="p", page_index=0, width=10, height=10, processed_image_path="private/path.png",
        blocks=[Block(
            id="b", kind="text", bbox=(0, 0, 1, 1), original_text="原始", edited_text="校对",
            proofread_source="manual_candidate",
        )],
    ))
    row = {"case_id": "scan-01", "class": "scan", "reference": "原始"}

    _capture_job_row(row, job)

    assert row["observed"] == "原始"
    assert row["baseline_observed"] == "原始"
    assert row["auto_observed"] == "校对"
    assert row["baseline_preserved"] is False
    assert row["auto_changes"] == 0
    assert row["wrong_changes"] == 0


def test_capture_normalizes_whitespace_when_checking_baseline_preservation():
    job = ImageJob(id="p", source_path="private/input.png", status="done", result=PageResult(
        id="p", page_index=0, width=10, height=10, processed_image_path="private/path.png",
        blocks=[Block(
            id="b", kind="text", bbox=(0, 0, 1, 1), original_text="甲 乙", edited_text="甲\n乙",
            proofread_source="manual_candidate",
        )],
    ))
    row = {"case_id": "scan-01", "class": "scan", "reference": "甲乙"}

    _capture_job_row(row, job)

    assert row["baseline_preserved"] is True


def test_auto_proofread_acceptance_requires_preserved_baseline_equal_cer_and_no_auto_changes():
    quality = {
        "scan": {"baseline": {"cer": 0.1}, "auto_proofread": {"cer": 0.1, "failed_pages": 0}, "auto_changes": 0, "wrong_changes": 0},
        "photo": {"baseline": {"cer": 0.2}, "auto_proofread": {"cer": 0.2, "failed_pages": 0}, "auto_changes": 0, "wrong_changes": 0},
    }

    assert _auto_proofread_acceptance([{"baseline_preserved": True}], quality) is True
    assert _auto_proofread_acceptance([{"baseline_preserved": False}], quality) is False
    assert _auto_proofread_acceptance(
        [{"baseline_preserved": True}],
        {**quality, "scan": {**quality["scan"], "wrong_changes": 1}},
    ) is False


def test_generator_writes_40_cases_with_content_and_fixed_canvas(tmp_path: Path):
    manifest = generate_dataset(tmp_path)

    assert len(manifest["cases"]) == 40
    assert {case["class"] for case in manifest["cases"]} == {"scan", "photo"}
    assert sum(case["characters"] for case in manifest["cases"]) >= 20_000
    assert sum(case["class"] == "scan" for case in manifest["cases"]) == 20
    assert sum(case["class"] == "photo" for case in manifest["cases"]) == 20
    for case in manifest["cases"]:
        assert case["characters"] >= 650
        image_path = tmp_path / case["image"]
        truth_path = tmp_path / case["ground_truth"]
        assert image_path.is_file()
        assert truth_path.is_file()
        assert json.loads(truth_path.read_text(encoding="utf-8"))["text"]
        from PIL import Image
        with Image.open(image_path) as image:
            assert image.size == CANVAS_SIZE


def test_stress_count_creates_one_continuous_batch_with_exact_unique_jobs():
    cases = [{"id": "scan-01", "class": "scan"}, {"id": "photo-01", "class": "photo"}]

    specs = build_job_specs(cases, stress_count=100)

    assert len(specs) == 100
    assert len({job_id for job_id, _case in specs}) == 100
    assert [case["id"] for _job_id, case in specs[:4]] == ["scan-01", "photo-01", "scan-01", "photo-01"]
    assert all(job_id.startswith("stress-") for job_id, _case in specs)


def test_restart_pass_requires_done_status_and_nonempty_result():
    failed = ImageJob(id="restart", source_path="sample.png", status="failed", error_code="ocr_failed")
    empty = ImageJob(id="restart", source_path="sample.png", status="done", result=None)
    done = ImageJob(id="restart", source_path="sample.png", status="done", result=PageResult(
        id="restart", page_index=0, width=1, height=1, processed_image_path="preview.png",
        blocks=[Block(id="b", kind="text", bbox=(0, 0, 1, 1), original_text="完成")],
    ))

    assert restart_passed(failed) is False
    assert restart_passed(empty) is False
    assert restart_passed(done) is True


def test_empty_class_summary_cannot_pass():
    summary = _class_summary([], "photo", 0.01)

    assert summary["pages"] == 0
    assert summary["pass"] is False


def test_photo_perspective_keeps_asymmetric_corner_orientation():
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (400, 600), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 199, 299), fill=(220, 30, 30))
    draw.rectangle((200, 0, 399, 299), fill=(30, 180, 30))
    draw.rectangle((200, 300, 399, 599), fill=(30, 60, 220))
    draw.rectangle((0, 300, 199, 599), fill=(220, 190, 30))

    result = _photo_variant(image, 1)

    assert result.getpixel((100, 150))[0] > result.getpixel((100, 150))[1]
    assert result.getpixel((300, 150))[1] > result.getpixel((300, 150))[0]
    assert result.getpixel((300, 450))[2] > result.getpixel((300, 450))[0]
    assert result.getpixel((100, 450))[0] > result.getpixel((100, 450))[2]
