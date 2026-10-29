import pytest

from offline_word.domain import Block, Cell, PageResult
from offline_word.proofread import bbox_iou, comparison_key, reconcile_page


def block(text: str, *, bbox: tuple[float, float, float, float] = (0, 0, 100, 20)) -> Block:
    return Block(id="body", kind="text", bbox=bbox, original_text=text)


def page(*blocks: Block) -> PageResult:
    return PageResult(
        id="page",
        page_index=0,
        width=100,
        height=100,
        processed_image_path="page.png",
        blocks=list(blocks),
    )


def table_page(rows: int, cols: int, texts: list[str]) -> PageResult:
    return page(Block(
        id="table",
        kind="table",
        bbox=(0, 0, 100, 100),
        rows=rows,
        cols=cols,
        cells=[Cell(row=index // cols, col=index % cols, original_text=text) for index, text in enumerate(texts)],
    ))


def test_comparison_key_ignores_whitespace():
    assert comparison_key(" 金额\n 100 元 ") == "金额100元"


def test_bbox_iou_measures_overlap():
    assert bbox_iou((0, 0, 10, 10), (5, 0, 15, 10)) == pytest.approx(1 / 3)


def test_two_of_three_enhanced_results_preserve_baseline_and_expose_candidate_for_review():
    baseline = page(block("金额 1O0 元"))

    result = reconcile_page(baseline, [page(block("金额 100 元")), page(block("金额 100 元"))])

    assert result.blocks[0].original_text == "金额 1O0 元"
    assert result.blocks[0].edited_text is None
    assert result.blocks[0].text == "金额 1O0 元"
    assert result.blocks[0].proofread_source == "baseline"
    assert result.blocks[0].proofread_status == "majority"
    assert result.blocks[0].needs_review is True
    assert result.blocks[0].candidates == ["金额 1O0 元", "金额 100 元"]
    assert result.proofread_status == "majority"


def test_two_of_three_table_results_preserve_each_baseline_cell_and_require_review():
    result = reconcile_page(
        table_page(1, 2, ["金额 1O0 元", "保持"]),
        [
            table_page(1, 2, ["金额 100 元", "保持"]),
            table_page(1, 2, ["金额 100 元", "保持"]),
        ],
    )

    table = result.blocks[0]
    assert table.proofread_source == "baseline"
    assert table.needs_review is True
    assert table.cells[0].proofread_source == "baseline"
    assert table.cells[0].text == "金额 1O0 元"
    assert table.cells[0].needs_review is True
    assert table.cells[0].candidates == ["金额 1O0 元", "金额 100 元"]
    assert table.cells[1].proofread_source == "baseline"


def test_baseline_majority_with_one_divergent_candidate_requires_review():
    result = reconcile_page(
        page(block("原始")),
        [page(block("原始")), page(block("候选"))],
    )

    target = result.blocks[0]
    assert target.text == "原始"
    assert target.edited_text is None
    assert target.proofread_source == "baseline"
    assert target.proofread_status == "majority"
    assert target.needs_review is True
    assert target.candidates == ["原始", "候选"]


def test_whitespace_equivalent_results_are_consistent_and_preserve_baseline():
    result = reconcile_page(
        page(block("甲 乙")),
        [page(block("甲\n乙")), page(block("甲\t乙"))],
    )

    target = result.blocks[0]
    assert target.text == "甲 乙"
    assert target.edited_text is None
    assert target.proofread_source == "baseline"
    assert target.proofread_status == "consistent"
    assert target.needs_review is False


def test_page_table_candidates_dedupe_whitespace_variants_in_pass_order():
    result = reconcile_page(
        table_page(1, 2, ["甲 乙", "保持"]),
        [
            table_page(1, 2, ["甲\n乙", "保持"]),
            table_page(1, 2, ["甲\t乙", "保持"]),
        ],
    )

    assert result.blocks[0].candidates == ["甲 乙\x1f保持"]


@pytest.mark.parametrize(
    ("baseline_text", "candidate_text"),
    [
        ("Alice", "Alicia"),
        ("100", "101"),
        ("A,B", "A.B"),
        ("Alpha", "alpha"),
        ("汉字", "汉宇"),
    ],
)
def test_majority_candidates_never_auto_adopt_protected_character_types(baseline_text: str, candidate_text: str):
    result = reconcile_page(
        page(block(baseline_text)),
        [page(block(candidate_text)), page(block(candidate_text))],
    )

    target = result.blocks[0]
    assert target.text == baseline_text
    assert target.edited_text is None
    assert target.proofread_source == "baseline"
    assert target.proofread_status == "majority"
    assert target.needs_review is True
    assert target.candidates == [baseline_text, candidate_text]


def test_three_different_results_keep_baseline_and_require_review():
    baseline = page(block("甲"))

    result = reconcile_page(baseline, [page(block("乙")), page(block("丙"))])

    assert result.blocks[0].text == "甲"
    assert result.blocks[0].candidates == ["甲", "乙", "丙"]
    assert result.blocks[0].needs_review is True


def test_unmatched_blocks_keep_baseline_and_require_review():
    baseline = page(block("甲"))
    unmatched = page(block("乙", bbox=(200, 0, 300, 20)))

    result = reconcile_page(baseline, [unmatched, page(block("甲"))])

    assert result.blocks[0].text == "甲"
    assert result.blocks[0].candidates == ["甲"]
    assert result.blocks[0].proofread_status == "conflict"
    assert result.blocks[0].needs_review is True


def test_incomplete_comparisons_never_form_a_majority():
    result = reconcile_page(page(block("甲")), [page(block("乙"))], completed_passes=2)

    assert result.blocks[0].text == "甲"
    assert result.blocks[0].proofread_status == "incomplete"
    assert result.blocks[0].needs_review is True
    assert result.proofread_status == "incomplete"


def test_incomplete_table_keeps_baseline_and_marks_table_and_cells_for_review():
    result = reconcile_page(
        table_page(1, 2, ["甲", "乙"]),
        [table_page(1, 2, ["丙", "丁"])],
        completed_passes=2,
    )

    table = result.blocks[0]
    assert [cell.text for cell in table.cells] == ["甲", "乙"]
    assert table.proofread_status == "incomplete"
    assert table.needs_review is True
    assert [cell.proofread_status for cell in table.cells] == ["incomplete", "incomplete"]
    assert [cell.needs_review for cell in table.cells] == [True, True]


def test_empty_incomplete_page_has_incomplete_status():
    result = reconcile_page(page(), [], completed_passes=2)

    assert result.proofread_status == "incomplete"


def test_completed_passes_above_three_rejects_extra_comparisons():
    baseline = page(block("A"))
    comparisons = [page(block("C")), page(block("B")), page(block("B")), page(block("B"))]

    with pytest.raises(ValueError, match="between 1 and 3"):
        reconcile_page(baseline, comparisons, completed_passes=5)


def test_extra_comparisons_do_not_override_the_fixed_three_pass_conflict():
    result = reconcile_page(
        page(block("A")),
        [page(block("C")), page(block("B")), page(block("B")), page(block("B"))],
    )

    assert result.blocks[0].text == "A"
    assert result.blocks[0].candidates == ["A", "C", "B"]
    assert result.blocks[0].proofread_status == "conflict"
    assert result.proofread_status == "conflict"
    assert result.blocks[0].needs_review is True


def test_table_structure_mismatch_keeps_all_baseline_cells():
    result = reconcile_page(
        table_page(1, 2, ["甲", "乙"]),
        [table_page(1, 1, ["甲"]), table_page(1, 2, ["甲", "丙"])],
    )

    assert [cell.text for cell in result.blocks[0].cells] == ["甲", "乙"]
    assert result.blocks[0].proofread_status == "conflict"
