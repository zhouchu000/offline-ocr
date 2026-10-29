"""Compare the exact folder-run export against locally adjudicated references."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from tools.benchmark import cer
from offline_word.version import __version__


def _source_pages(path: Path) -> list[str]:
    document = Document(path)
    pages: list[str] = []
    parts: list[str] = []
    for element in document.element.body.iterchildren():
        if element.tag == qn("w:p"):
            paragraph = Paragraph(element, document)
            if paragraph.paragraph_format.page_break_before and parts:
                pages.append("\n".join(parts))
                parts = []
            if paragraph.text:
                parts.append(paragraph.text)
        elif element.tag == qn("w:tbl"):
            table = Table(element, document)
            seen = set()
            for row in table.rows:
                for cell in row.cells:
                    if cell._tc not in seen:
                        parts.append(cell.text)
                        seen.add(cell._tc)
    if parts:
        pages.append("\n".join(parts))
    return pages


def assess_run(
    run_dir: Path, reference_dir: Path, *,
    reference_method: str = "User-provided local reference text; preparation is not verified by this tool.",
) -> dict[str, object]:
    run = json.loads((run_dir / "真实样本-识别统计.json").read_text(encoding="utf-8"))
    filename = run.get("output_docx")
    if not filename or Path(filename).name != filename:
        raise ValueError("invalid_output_docx")
    output = run_dir / filename
    pages = _source_pages(output)
    references = [path.read_text(encoding="utf-8") for path in sorted(reference_dir.glob("*.txt"))]
    if not references or len(pages) != len(references) or len(references) != run["input_pages"]:
        raise ValueError("page_count_mismatch")
    results = [cer(reference, observed) for reference, observed in zip(references, pages)]
    errors = sum(int(result["errors"]) for result in results)
    characters = sum(int(result["reference"]) for result in results)
    return {
        "schema": 1,
        "application_version": run.get("application_version", __version__),
        "sample_class": "real_document_photos",
        **{key: run[key] for key in (
            "input_pages", "completed_pages", "failed_pages", "source_unchanged",
            "source_set_sha256", "mode", "review_items", "text_recovery_pages",
            "layout_fallback_pages", "elapsed_seconds", "peak_rss_bytes_process_tree",
        ) if key in run},
        "docx_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "reference_pages": len(references),
        "reference_characters": characters,
        "character_errors": errors,
        "cer": errors / characters if characters else None,
        "photo_cer_acceptance_target": 0.01,
        "photo_cer_target_met": bool(characters and errors / characters <= 0.01 and not run["failed_pages"]),
        "reference_method": reference_method,
        "reference_limitation": "Accuracy is relative to the supplied reference; any errors shared with the reference remain undetected.",
        "normalization": "Whitespace excluded; punctuation, digit, case and fullwidth/halfwidth differences count as errors.",
        "page_results": [
            {"page": index, "errors": result["errors"], "reference_characters": result["reference"], "cer": result["cer"]}
            for index, result in enumerate(results, start=1)
        ],
        "rendered_pages": None,
        "raw_text_or_paths_in_report": False,
        "word_wps_windows_validation": "PENDING",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--reference-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference-method", default="User-provided local reference text; preparation is not verified by this tool.")
    args = parser.parse_args()
    report = assess_run(args.run_dir, args.reference_dir, reference_method=args.reference_method)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("reference_pages", "reference_characters", "character_errors", "cer")}))


if __name__ == "__main__":
    main()
