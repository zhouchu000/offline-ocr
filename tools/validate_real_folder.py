"""Run a user-provided image folder without persisting OCR text in logs/reports."""

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing
import os
import platform
import time
from pathlib import Path

import psutil

from offline_word.domain import Block, Cell, ImageJob
from offline_word.exporter import export_docx
from offline_word.images import import_images
from offline_word.proofread import comparison_key
from offline_word.review import collect_review_items
from offline_word.security import SessionStore, cpu_thread_limit
from offline_word.worker import OCRWorker


def _source_digest(jobs: list[ImageJob]) -> str:
    digest = hashlib.sha256()
    for job in jobs:
        path = Path(job.source_path)
        digest.update(path.name.encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _process_rss() -> int:
    process = psutil.Process(os.getpid())
    total = process.memory_info().rss
    for child in process.children(recursive=True):
        try:
            total += child.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return total


def _units(jobs: list[ImageJob]) -> list[Block | Cell]:
    units: list[Block | Cell] = []
    for job in jobs:
        if not job.result:
            continue
        for block in job.result.blocks:
            if block.kind == "table" and block.cells:
                units.extend(block.cells)
            elif block.kind != "image":
                units.append(block)
    return units


def _has_alternative_candidate(unit: Block | Cell) -> bool:
    return len({comparison_key(value) for value in unit.candidates}) > 1


def run(
    folder: Path,
    models: Path,
    output_dir: Path,
    *,
    auto_proofread: bool = True,
) -> dict[str, object]:
    jobs = [job for job in import_images([str(folder)]) if job.error_code is None]
    if not jobs:
        raise ValueError("no_images")
    for index, job in enumerate(jobs, start=1):
        job.id = f"real-{index:03d}"
        job.mode = "photo"
        job.auto_proofread = auto_proofread
    before = _source_digest(jobs)
    started = time.monotonic()
    peak_rss = _process_rss()
    event_counts: dict[str, int] = {}
    page_started: dict[str, float] = {}
    page_metrics: list[dict[str, object]] = []

    output_dir.mkdir(parents=True, exist_ok=True)
    with SessionStore(output_dir / ".real-sample-sessions") as session:
        worker = OCRWorker(models, session.path)
        try:
            worker.start(jobs)
            deadline = time.monotonic() + 7_200
            while time.monotonic() < deadline:
                events = worker.poll()
                for event in events:
                    kind = str(event.get("event", "unknown"))
                    event_counts[kind] = event_counts.get(kind, 0) + 1
                    job_id = str(event.get("job_id", ""))
                    if kind == "page_started":
                        page_started[job_id] = time.monotonic()
                    if kind in {"page_done", "page_error"}:
                        finished = event_counts.get("page_done", 0) + event_counts.get("page_error", 0)
                        status = "done" if kind == "page_done" else "error"
                        print(f"page {finished}/{len(jobs)} {status}", flush=True)
                        page = event.get("result")
                        page_metrics.append({
                            "page_index": next(index for index, job in enumerate(jobs, 1) if job.id == job_id),
                            "status": status,
                            "seconds": round(time.monotonic() - page_started.get(job_id, started), 3),
                            "recovered_lines": sum(":recovered-" in block.id for block in page.blocks) if page else 0,
                            "characters": sum(len("".join(block.original_text.split())) for block in page.blocks if block.kind != "image") if page else 0,
                        })
                peak_rss = max(peak_rss, _process_rss())
                if event_counts.get("finished"):
                    break
                if not worker.busy:
                    break
                time.sleep(0.05)
            else:
                worker.cancel()
                raise RuntimeError("worker_timeout")
            pages = [job.result for job in jobs if job.result is not None]
            output_docx = export_docx(pages, output_dir / "真实样本-识别结果.docx") if pages else None
            review_items = collect_review_items(jobs)
            units = _units(jobs)
            confidence_values = [float(unit.confidence) for unit in units if unit.confidence is not None]
            report: dict[str, object] = {
                "schema": 1,
                "platform": {"os": platform.system(), "arch": platform.machine()},
                "mode": "photo_high_accuracy" if auto_proofread else "photo_single_pass",
                "input_pages": len(jobs),
                "completed_pages": len(pages),
                "failed_pages": sum(job.status == "failed" for job in jobs),
                "failure_codes": sorted({job.error_code for job in jobs if job.error_code}),
                "source_unchanged": _source_digest(jobs) == before,
                "source_set_sha256": before,
                "output_docx": output_docx.name if output_docx else None,
                "blocks": sum(len(page.blocks) for page in pages),
                "text_units": len(units),
                "characters_without_whitespace": sum(len("".join(unit.original_text.split())) for unit in units),
                "confidence_observations": len(confidence_values),
                "mean_confidence": sum(confidence_values) / len(confidence_values) if confidence_values else None,
                "minimum_confidence": min(confidence_values) if confidence_values else None,
                "review_items": len(review_items),
                "candidate_units": sum(_has_alternative_candidate(unit) for unit in units),
                "detail_reviewed_regions": sum(page.detail_reviewed_regions for page in pages),
                "detail_candidate_regions": sum(page.detail_candidate_regions for page in pages),
                "text_recovery_pages": sum(page.recognition_warning == "text_recovery" for page in pages),
                "layout_fallback_pages": sum(page.recognition_warning == "layout_fallback" for page in pages),
                "baseline_preserved": all(unit.edited_text is None for unit in units),
                "event_counts": event_counts,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "cpu_threads": cpu_thread_limit(),
                "page_metrics": page_metrics,
                "peak_rss_bytes_process_tree": peak_rss,
                "accuracy_status": "requires_human_ground_truth",
            }
            report_path = output_dir / "真实样本-识别统计.json"
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return report
        finally:
            worker.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--single-pass", action="store_true")
    args = parser.parse_args()
    report = run(args.folder, args.models, args.output_dir, auto_proofread=not args.single_pass)
    print(
        json.dumps(
            {
                "completed_pages": report["completed_pages"],
                "failed_pages": report["failed_pages"],
                "review_items": report["review_items"],
                "source_unchanged": report["source_unchanged"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
