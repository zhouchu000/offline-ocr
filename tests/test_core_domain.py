import pickle

from offline_word.domain import Block, Cell, ImageJob, PageResult, normalize_table_cells


def test_explicit_empty_edit_is_preserved_in_block_and_cell():
    cell = Cell(row=0, col=0, original_text="原文", edited_text="")
    block = Block(id="b", kind="text", bbox=(1, 2, 3, 4), original_text="原文", edited_text="")

    assert cell.text == ""
    assert block.text == ""


def test_normalize_table_cells_resolves_overlapping_spans_and_fills_holes():
    cells = [
        Cell(row=0, col=0, original_text="甲", rowspan=1, colspan=2),
        Cell(row=0, col=1, original_text="冲突"),
        Cell(row=1, col=1, original_text="乙"),
    ]

    normalized = normalize_table_cells(cells, rows=2, cols=3)

    assert [(cell.row, cell.col, cell.text, cell.rowspan, cell.colspan) for cell in normalized] == [
        (0, 0, "甲\n冲突", 1, 2),
        (0, 2, "", 1, 1),
        (1, 0, "", 1, 1),
        (1, 1, "乙", 1, 1),
        (1, 2, "", 1, 1),
    ]


def test_normalize_table_cells_clamps_out_of_bounds_span():
    normalized = normalize_table_cells([Cell(row=1, col=1, original_text="尾", rowspan=5, colspan=5)], rows=2, cols=2)

    assert [(cell.row, cell.col, cell.rowspan, cell.colspan) for cell in normalized] == [(0, 0, 1, 1), (0, 1, 1, 1), (1, 0, 1, 1), (1, 1, 1, 1)]
    assert normalized[-1].text == "尾"


def test_proofread_state_fields_are_serializable():
    cell = Cell(
        row=0,
        col=0,
        original_text="甲",
        candidates=["甲", "乙"],
        proofread_status="conflict",
        proofread_source="baseline",
        proofread_passes=3,
        needs_review=True,
    )
    page = PageResult(
        id="page",
        page_index=0,
        width=100,
        height=100,
        processed_image_path="page.png",
        blocks=[Block(
            id="table",
            kind="table",
            bbox=(0, 0, 10, 10),
            cells=[cell],
            candidates=["甲", "乙"],
            proofread_status="conflict",
            proofread_source="baseline",
            proofread_passes=3,
        )],
        proofread_status="conflict",
        proofread_completed_passes=3,
    )
    job = ImageJob(id="job", source_path="image.png", auto_proofread=True, result=page)

    restored = pickle.loads(pickle.dumps(job))

    assert restored.auto_proofread is True
    assert restored.result.proofread_status == "conflict"
    assert restored.result.proofread_completed_passes == 3
    assert restored.result.blocks[0].cells[0].needs_review is True
