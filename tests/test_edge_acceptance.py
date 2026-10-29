import os
from pathlib import Path

import pytest
from PIL import Image
from docx import Document

from offline_word.domain import Block, ImageJob, PageResult
from offline_word.exporter import export_docx
from offline_word.images import import_images, prepare_image
from offline_word.security import OfflineError


_FORMATS = ("jpg", "png", "bmp", "tiff")


def _page(job: ImageJob, processed: Path) -> PageResult:
    return PageResult(
        id=job.id,
        page_index=job.frame_index,
        width=32,
        height=24,
        processed_image_path=str(processed),
        blocks=[Block(
            id=f"{job.id}:text",
            kind="text",
            bbox=(0, 0, 32, 24),
            original_text="中文路径保留：会议资料，编号 2026-A。",
        )],
    )


@pytest.mark.parametrize("extension", _FORMATS)
def test_unicode_space_paths_round_trip_supported_images_to_editable_docx(tmp_path: Path, extension: str):
    source_dir = tmp_path / "原始 资料 中文"
    session_dir = tmp_path / "会话 临时 中文"
    output_dir = tmp_path / "导出 结果 中文"
    source_dir.mkdir()
    source = source_dir / f"会议 页面 中文.{extension}"
    Image.new("RGB", (32, 24), "white").save(source)

    [job] = import_images([str(source)])
    assert job.status == "pending"
    processed = prepare_image(job, session_dir)
    output_dir.mkdir()
    requested = output_dir / "结果 文档 中文.docx"
    requested.write_bytes(b"existing-marker")

    exported = export_docx([_page(job, processed)], requested)

    assert requested.read_bytes() == b"existing-marker"
    assert exported != requested
    assert "中文路径保留：会议资料，编号 2026-A。" in "\n".join(p.text for p in Document(exported).paragraphs)


def test_damaged_png_is_failed_and_prepare_image_exposes_safe_error(tmp_path: Path):
    damaged = tmp_path / "损坏 图片.png"
    damaged.write_bytes(b"not-a-png")

    [job] = import_images([str(damaged)])

    assert job.status == "failed"
    assert job.error_code == "invalid_image"
    with pytest.raises(OfflineError, match="invalid_image"):
        prepare_image(ImageJob(id="damaged", source_path=str(damaged)), tmp_path / "会话")


def test_pdf_is_rejected_by_suffix_without_image_parsing(tmp_path: Path):
    pdf = tmp_path / "资料.pdf"
    pdf.write_bytes(b"%PDF-1.7\nnot parsed")

    [job] = import_images([str(pdf)])

    assert job.status == "failed"
    assert job.error_code == "unsupported_image"


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permission test; Windows requires live ACL validation")
def test_posix_unwritable_output_directory_raises_and_restores_permissions(tmp_path: Path):
    output_dir = tmp_path / "只读 输出"
    output_dir.mkdir(mode=0o700)
    marker = output_dir / "marker.txt"
    marker.write_bytes(b"keep")
    page = PageResult(
        id="readonly",
        page_index=0,
        width=1,
        height=1,
        processed_image_path=str(tmp_path / "preview.png"),
        blocks=[Block(id="text", kind="text", bbox=(0, 0, 1, 1), original_text="中文")],
    )
    try:
        output_dir.chmod(0o500)
        with pytest.raises(PermissionError):
            export_docx([page], output_dir / "result.docx")
    finally:
        output_dir.chmod(0o700)

    assert marker.read_bytes() == b"keep"
