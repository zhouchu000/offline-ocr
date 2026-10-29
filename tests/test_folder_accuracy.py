import json
from pathlib import Path

from docx import Document

from tools.assess_folder_accuracy import assess_run


def test_accuracy_uses_reported_export_when_an_older_same_name_file_exists(tmp_path: Path):
    references = tmp_path / "reference"
    references.mkdir()
    (references / "001.txt").write_text("正确原文。", encoding="utf-8")
    old = Document()
    old.add_paragraph("过期结果")
    old.save(tmp_path / "真实样本-识别结果.docx")
    current = Document()
    current.add_paragraph("正确原文。")
    current.save(tmp_path / "真实样本-识别结果 (1).docx")
    (tmp_path / "真实样本-识别统计.json").write_text(json.dumps({
        "output_docx": "真实样本-识别结果 (1).docx",
        "input_pages": 1, "completed_pages": 1, "failed_pages": 0,
    }), encoding="utf-8")

    report = assess_run(tmp_path, references)

    assert report["character_errors"] == 0
    assert report["reference_characters"] == 5
    assert report["reference_pages"] == 1


def test_accuracy_rejects_missing_pages_instead_of_skipping_them(tmp_path: Path):
    references = tmp_path / "reference"
    references.mkdir()
    (references / "001.txt").write_text("甲乙", encoding="utf-8")
    (references / "002.txt").write_text("丙丁", encoding="utf-8")
    current = Document()
    current.add_paragraph("甲乙")
    current.save(tmp_path / "result.docx")
    (tmp_path / "真实样本-识别统计.json").write_text(json.dumps({
        "output_docx": "result.docx",
        "input_pages": 2, "completed_pages": 1, "failed_pages": 1,
    }), encoding="utf-8")

    import pytest
    with pytest.raises(ValueError, match="page_count_mismatch"):
        assess_run(tmp_path, references)
