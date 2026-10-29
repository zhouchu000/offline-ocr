from pathlib import Path

from offline_word.domain import Block, Cell, ImageJob, PageResult
from offline_word.review import collect_review_items


def _job(job_id: str, image: Path, blocks: list[Block]) -> ImageJob:
    return ImageJob(
        id=job_id,
        source_path=str(image),
        status="done",
        result=PageResult(
            id=job_id,
            page_index=0,
            width=1000,
            height=1400,
            processed_image_path=str(image),
            blocks=blocks,
        ),
    )


def test_collect_review_items_aggregates_all_pages_in_queue_order(tmp_path: Path):
    image = tmp_path / "page.png"
    image.write_bytes(b"fixture")
    jobs = [
        _job(
            "page-1",
            image,
            [
                Block(id="clear", kind="text", bbox=(10, 10, 900, 50), original_text="清晰", confidence=0.98),
                Block(id="weak", kind="text", bbox=(20, 80, 900, 130), original_text="低置信度", confidence=0.90),
            ],
        ),
        _job(
            "page-2",
            image,
            [
                Block(
                    id="table",
                    kind="table",
                    bbox=(40, 100, 940, 500),
                    confidence=0.97,
                    rows=1,
                    cols=2,
                    cells=[
                        Cell(row=0, col=0, original_text="正常", confidence=0.99),
                        Cell(row=0, col=1, original_text="待核", confidence=0.62, needs_review=True),
                    ],
                ),
                Block(
                    id="conflict",
                    kind="text",
                    bbox=(40, 520, 940, 570),
                    original_text="识别冲突",
                    confidence=0.96,
                    proofread_status="conflict",
                    proofread_passes=3,
                ),
            ],
        ),
    ]

    items = collect_review_items(jobs)

    assert [(item.job_id, item.block.id) for item in items] == [
        ("page-1", "weak"),
        ("page-2", "table"),
        ("page-2", "conflict"),
    ]
    assert items[0].page_number == 1
    assert "低置信度 90%" in items[0].reasons
    assert "低置信度表格单元格" in items[1].reasons
    assert "多次识别结果不一致" in items[2].reasons


def test_collect_review_items_excludes_non_text_image_blocks(tmp_path: Path):
    image = tmp_path / "page.png"
    image.write_bytes(b"fixture")
    job = _job(
        "page-1",
        image,
        [Block(id="figure", kind="image", bbox=(0, 0, 100, 100), confidence=0.2, needs_review=True)],
    )

    assert collect_review_items([job]) == []


def test_collect_review_items_ignores_a_single_consistent_candidate(tmp_path: Path):
    job = ImageJob(id="page", source_path=str(tmp_path / "page.png"))
    job.result = PageResult(
        id=job.id,
        page_index=0,
        width=100,
        height=100,
        processed_image_path=str(tmp_path / "processed.png"),
        blocks=[Block(
            id="body",
            kind="text",
            bbox=(10, 10, 90, 30),
            original_text="三次一致",
            confidence=0.99,
            candidates=["三次一致"],
            proofread_status="consistent",
            proofread_passes=3,
        )],
    )

    assert collect_review_items([job]) == []


def test_review_uses_weakest_line_when_paragraph_average_is_high(tmp_path: Path):
    block = Block(id="p", kind="text", bbox=(0, 0, 100, 100), original_text="正文",
                  confidence=0.97, needs_review=True)
    block.minimum_line_confidence = 0.62
    items = collect_review_items([_job("page", tmp_path / "page.png", [block])])
    assert any(reason.startswith("低置信度") for reason in items[0].reasons)
