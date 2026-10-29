from offline_word.accuracy import DetailResult, DetailTarget, apply_detail_results, detail_targets
from offline_word.domain import Block, PageResult


def page(*blocks: Block) -> PageResult:
    return PageResult(
        id="page",
        page_index=0,
        width=1000,
        height=1200,
        processed_image_path="page.png",
        blocks=list(blocks),
    )


def test_detail_targets_choose_lowest_confidence_text_lines_and_cap_work():
    """Would fail if detail mode spends unbounded work or refines tables/images."""
    body = Block(id="body", kind="text", bbox=(0, 0, 500, 1000), original_text="正文")
    table = Block(id="table", kind="table", bbox=(500, 0, 1000, 500))
    image = Block(id="image", kind="image", bbox=(500, 500, 1000, 1000))
    lines = [
        (f"行{index}", (10, index * 40, 400, index * 40 + 30), 0.81 + index * 0.01)
        for index in range(10)
    ]
    lines.extend([
        ("表格低分", (600, 20, 900, 50), 0.10),
        ("图片低分", (600, 620, 900, 650), 0.20),
        ("正文高分", (10, 1050, 400, 1080), 0.99),
    ])

    targets = detail_targets(page(body, table, image), lines, limit=8)

    assert [target.original_text for target in targets] == [f"行{index}" for index in range(8)]
    assert all(target.block_index == 0 for target in targets)
    assert [target.confidence for target in targets] == sorted(target.confidence for target in targets)


def test_two_of_three_detail_results_add_full_block_candidate_without_replacing_baseline():
    """Would fail if a detail majority silently rewrites exported text."""
    block = Block(
        id="body",
        kind="text",
        bbox=(0, 0, 800, 300),
        original_text="甲行\n金额 1O0 元\n乙行",
        edited_text="用户已校对的正文",
        needs_review=True,
    )
    target = DetailTarget(0, "金额 1O0 元", (10, 80, 500, 120), 0.62)
    result_page = page(block)

    added = apply_detail_results(
        result_page,
        target,
        [
            DetailResult("金额 100 元", 0.97),
            DetailResult("金额 100 元", 0.96),
            DetailResult("金额 1O0 元", 0.91),
        ],
    )

    assert added is True
    assert block.original_text == "甲行\n金额 1O0 元\n乙行"
    assert block.edited_text == "用户已校对的正文"
    assert block.text == "用户已校对的正文"
    assert block.candidates == ["甲行\n金额 1O0 元\n乙行", "甲行\n金额 100 元\n乙行"]
    assert block.detail_reviewed_regions == 1
    assert block.detail_candidate_regions == 1
    assert block.detail_review_status == "candidate"
    assert result_page.detail_reviewed_regions == 1
    assert result_page.detail_candidate_regions == 1
    assert result_page.detail_review_status == "candidate"


def test_detail_replacement_matches_unique_text_while_ignoring_whitespace():
    """Would fail if OCR line whitespace prevents a safe full-block candidate."""
    block = Block(id="body", kind="text", bbox=(0, 0, 500, 100), original_text="金额 1O0 元")
    result_page = page(block)

    apply_detail_results(
        result_page,
        DetailTarget(0, "金额1O0元", (0, 0, 500, 100), 0.5),
        [DetailResult("金额100元", 0.9)] * 3,
    )

    assert block.candidates == ["金额 1O0 元", "金额100元"]


def test_consistent_detail_results_do_not_create_a_duplicate_candidate():
    """Would fail if agreement with the baseline creates a meaningless choice."""
    block = Block(id="body", kind="text", bbox=(0, 0, 500, 100), original_text="原始文字", needs_review=True)
    result_page = page(block)

    added = apply_detail_results(
        result_page,
        DetailTarget(0, "原始文字", (0, 0, 500, 100), 0.7),
        [DetailResult("原始文字", 0.99)] * 3,
    )

    assert added is False
    assert block.candidates == []
    assert block.needs_review is True
    assert block.detail_review_status == "consistent"
    assert result_page.detail_candidate_regions == 0


def test_three_way_detail_conflict_exposes_each_candidate_and_preserves_baseline():
    """Would fail if disagreement is hidden or one candidate is auto-selected."""
    block = Block(id="body", kind="text", bbox=(0, 0, 500, 100), original_text="甲")
    result_page = page(block)

    apply_detail_results(
        result_page,
        DetailTarget(0, "甲", (0, 0, 500, 100), 0.5),
        [DetailResult("乙", 0.9), DetailResult("丙", 0.8), DetailResult("丁", 0.7)],
    )

    assert block.text == "甲"
    assert block.candidates == ["甲", "乙", "丙", "丁"]
    assert block.detail_review_status == "conflict"
    assert block.needs_review is True


def test_ambiguous_line_occurrence_is_not_replaced_at_an_unknown_location():
    """Would fail if a repeated phrase can make the wrong paragraph candidate."""
    block = Block(id="body", kind="text", bbox=(0, 0, 500, 100), original_text="编号A；编号A")
    result_page = page(block)

    added = apply_detail_results(
        result_page,
        DetailTarget(0, "编号A", (0, 0, 200, 50), 0.5),
        [DetailResult("编号B", 0.9)] * 3,
    )

    assert added is False
    assert block.candidates == []
    assert block.detail_reviewed_regions == 1
    assert block.detail_candidate_regions == 0
    assert block.needs_review is True


def test_incomplete_detail_results_are_reported_without_discarding_a_candidate():
    """Would fail if one failed variant hides a useful surviving suggestion."""
    block = Block(id="body", kind="text", bbox=(0, 0, 500, 100), original_text="原文")
    result_page = page(block)

    apply_detail_results(
        result_page,
        DetailTarget(0, "原文", (0, 0, 500, 100), 0.5),
        [DetailResult("候选", 0.9), DetailResult("候选", 0.8)],
    )

    assert block.candidates == ["原文", "候选"]
    assert block.detail_review_status == "incomplete"
    assert result_page.detail_review_status == "incomplete"
