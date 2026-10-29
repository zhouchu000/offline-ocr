"""Run the public OCR acceptance corpus through the real OCR worker."""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import time
from pathlib import Path
from typing import Any, Iterable

import psutil

from offline_word.domain import Block, Cell, ImageJob, PageResult
from offline_word.proofread import comparison_key
from offline_word.security import OfflineError, SessionStore
from offline_word.worker import OCRWorker


def _without_whitespace(value: str) -> str:
    return "".join(str(value).split())


def _levenshtein(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, 1):
        current = [left_index]
        for right_index, right_char in enumerate(right, 1):
            current.append(min(
                current[-1] + 1,
                previous[right_index] + 1,
                previous[right_index - 1] + (left_char != right_char),
            ))
        previous = current
    return previous[-1]


def cer(reference: str, observed: str) -> dict[str, float | int]:
    """Return whitespace-insensitive CER; punctuation, digits and case count."""
    expected = _without_whitespace(reference)
    actual = _without_whitespace(observed)
    errors = _levenshtein(expected, actual)
    denominator = len(expected)
    return {"errors": errors, "reference": denominator, "cer": errors / denominator if denominator else (0.0 if not actual else 1.0)}


def extract_original_text(page: PageResult) -> str:
    """Flatten a PageResult in its OCR reading order, ignoring all edits."""
    parts: list[str] = []
    for block in page.blocks:
        if block.kind == "table":
            if block.cells:
                parts.extend(cell.original_text for cell in sorted(block.cells, key=lambda cell: (cell.row, cell.col)))
            elif block.original_text:
                parts.append(block.original_text)
        else:
            parts.append(block.original_text)
    return "\n".join(parts)


def extract_effective_text(page: PageResult) -> str:
    """Flatten the text that would be exported after OCR and proofreading."""
    parts: list[str] = []
    for block in page.blocks:
        if block.kind == "table":
            if block.cells:
                parts.extend(cell.text for cell in sorted(block.cells, key=lambda cell: (cell.row, cell.col)))
            elif block.text:
                parts.append(block.text)
        else:
            parts.append(block.text)
    return "\n".join(parts)


def build_job_specs(cases: list[dict[str, Any]], stress_count: int = 0) -> list[tuple[str, dict[str, Any]]]:
    """Build one base batch or exactly N unique round-robin stress jobs."""
    if stress_count < 0:
        raise ValueError("stress count must be non-negative")
    if stress_count == 0:
        return [(str(case["id"]), case) for case in cases]
    if not cases:
        raise ValueError("at least one public case is required for stress runs")
    return [
        (f"stress-{index + 1:05d}-{cases[index % len(cases)]['id']}", cases[index % len(cases)])
        for index in range(stress_count)
    ]


def restart_passed(job: ImageJob) -> bool:
    """A restart passes only after a successful job with at least one block."""
    return job.status == "done" and job.result is not None and bool(job.result.blocks)


def summarize_results(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    errors = 0
    characters = 0
    failed = []
    for row in rows:
        result = cer(str(row.get("reference", "")), str(row.get("observed", "")))
        row["errors"] = int(result["errors"])
        row["reference_chars"] = int(result["reference"])
        row["cer"] = float(result["cer"])
        errors += row["errors"]
        characters += row["reference_chars"]
        if row.get("error"):
            failed.append(str(row["case_id"]))
    return {
        "pages": len(rows),
        "failed_pages": len(failed),
        "failed_case_ids": failed,
        "characters": characters,
        "errors": errors,
        "cer": errors / characters if characters else 0.0,
    }


def summarize_auto_rows(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Summarize baseline/effective CER and proofreading changes by class.

    The input rows may contain raw OCR strings while the function is running,
    but callers must pass the result through ``_public_rows`` before writing a
    report.  The worker benchmark classifies each automatic unit change against
    the page reference while all other units remain at their baseline text.
    """
    grouped: dict[str, list[dict[str, Any]]] = {"scan": [], "photo": []}
    for row in rows:
        case_class = str(row.get("class", ""))
        if case_class in grouped:
            grouped[case_class].append(row)

    report: dict[str, Any] = {}
    for case_class, class_rows in grouped.items():
        baseline_rows = [
            {"case_id": row.get("case_id", ""), "reference": row.get("reference", ""),
             "observed": row.get("baseline_observed", row.get("observed", "")),
             "error": row.get("error")}
            for row in class_rows
        ]
        auto_rows = [
            {"case_id": row.get("case_id", ""), "reference": row.get("reference", ""),
             "observed": row.get("auto_observed", row.get("observed", "")),
             "error": row.get("error")}
            for row in class_rows
        ]
        baseline_summary = summarize_results(baseline_rows)
        candidate_assisted = dict(baseline_summary)
        candidate_assisted["errors"] = sum(
            int(row.get(
                "candidate_assisted_errors",
                cer(str(row.get("reference", "")), str(row.get("baseline_observed", "")))["errors"],
            ))
            for row in class_rows
        )
        candidate_assisted["cer"] = (
            candidate_assisted["errors"] / candidate_assisted["characters"]
            if candidate_assisted["characters"] else 0.0
        )
        report[case_class] = {
            "baseline": baseline_summary,
            "auto_proofread": summarize_results(auto_rows),
            "candidate_assisted": candidate_assisted,
            "candidate_units": sum(int(row.get("candidate_units", 0)) for row in class_rows),
            "detail_reviewed_regions": sum(int(row.get("detail_reviewed_regions", 0)) for row in class_rows),
            "detail_candidate_regions": sum(int(row.get("detail_candidate_regions", 0)) for row in class_rows),
            "candidate_improved_units": sum(int(row.get("candidate_improved_units", 0)) for row in class_rows),
            "candidate_worsened_only_units": sum(int(row.get("candidate_worsened_only_units", 0)) for row in class_rows),
            "candidate_neutral_units": sum(int(row.get("candidate_neutral_units", 0)) for row in class_rows),
            "auto_changes": sum(int(row.get("auto_changes", 0)) for row in class_rows),
            "correct_changes": sum(int(row.get("correct_changes", 0)) for row in class_rows),
            "wrong_changes": sum(int(row.get("wrong_changes", 0)) for row in class_rows),
            "unresolved_changes": sum(int(row.get("unresolved_changes", 0)) for row in class_rows),
            "unresolved_conflicts": sum(int(row.get("unresolved_conflicts", 0)) for row in class_rows),
        }
    return report


def _auto_proofread_acceptance(rows: Iterable[dict[str, Any]], auto_quality: dict[str, dict[str, Any]]) -> bool:
    """Require benchmark proofreading to preserve every baseline exactly."""
    rows = list(rows)
    return bool(rows) and all(bool(row.get("baseline_preserved")) for row in rows) and all(
        values["auto_proofread"]["failed_pages"] == 0
        and values["auto_proofread"]["cer"] == values["baseline"]["cer"]
        and values["auto_changes"] == 0
        and values["wrong_changes"] == 0
        for values in auto_quality.values()
    )


def _sampled_rss(process: psutil.Process) -> int:
    total = 0
    try:
        total += process.memory_info().rss
        for child in process.children(recursive=True):
            try:
                total += child.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return total


def _public_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove raw OCR text and local paths before a report is serialized."""
    def safe_status(value: Any) -> str | None:
        if value is None:
            return None
        status = str(value)
        return status if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", status) else "unknown_error"

    clean: list[dict[str, Any]] = []
    for row in rows:
        clean.append({
            "case_id": row["case_id"],
            "class": row["class"],
            "reference_chars": row.get("reference_chars", 0),
            "errors": row.get("errors", 0),
            "cer": row.get("cer", 0.0),
            "baseline_errors": row.get("baseline_errors", row.get("errors", 0)),
            "baseline_cer": row.get("baseline_cer", row.get("cer", 0.0)),
            "auto_errors": row.get("auto_errors", row.get("errors", 0)),
            "auto_cer": row.get("auto_cer", row.get("cer", 0.0)),
            "baseline_preserved": bool(row.get("baseline_preserved", False)),
            "candidate_units": int(row.get("candidate_units", 0)),
            "detail_reviewed_regions": int(row.get("detail_reviewed_regions", 0)),
            "detail_candidate_regions": int(row.get("detail_candidate_regions", 0)),
            "detail_review_status": safe_status(row.get("detail_review_status")),
            "candidate_improved_units": int(row.get("candidate_improved_units", 0)),
            "candidate_worsened_only_units": int(row.get("candidate_worsened_only_units", 0)),
            "candidate_neutral_units": int(row.get("candidate_neutral_units", 0)),
            "candidate_assisted_errors": int(row.get("candidate_assisted_errors", row.get("errors", 0))),
            "candidate_assisted_cer": float(row.get("candidate_assisted_cer", row.get("cer", 0.0))),
            "auto_changes": row.get("auto_changes", 0),
            "correct_changes": row.get("correct_changes", 0),
            "wrong_changes": row.get("wrong_changes", 0),
            "unresolved_changes": row.get("unresolved_changes", 0),
            "unresolved_conflicts": row.get("unresolved_conflicts", 0),
            "error": safe_status(row.get("error")),
        })
    return clean


def _wait_for_worker(worker: OCRWorker, jobs: list[ImageJob], process: psutil.Process, deadline: float) -> tuple[list[dict], int]:
    events: list[dict] = []
    peak = _sampled_rss(process)
    finished = False
    reported = 0
    while time.monotonic() < deadline:
        batch = worker.poll()
        events.extend(batch)
        for event in batch:
            if event.get("event") in {"page_done", "page_error"}:
                reported += 1
                status = "done" if event["event"] == "page_done" else "error"
                print(f"finished {reported}/{len(jobs)} case={event.get('job_id', 'unknown')} status={status}", flush=True)
        peak = max(peak, _sampled_rss(process))
        if any(event.get("event") in {"finished", "cancelled"} for event in batch):
            finished = True
            break
        if not worker.busy:
            break
        time.sleep(0.02)
    if not finished and worker.busy:
        worker.cancel()
        raise RuntimeError("worker_timeout")
    peak = max(peak, _sampled_rss(process))
    completed = {event.get("job_id") for event in events if event.get("event") in {"page_done", "page_error"}}
    if completed != {job.id for job in jobs}:
        missing = len({job.id for job in jobs} - completed)
        raise RuntimeError(f"worker_incomplete_{missing}")
    return events, peak


def _page_text_units(page: PageResult) -> list[Block | Cell]:
    """Return export-ordered blocks and cells that form a page's text."""
    units: list[Block | Cell] = []
    for block in page.blocks:
        if block.kind == "table" and block.cells:
            units.extend(sorted(block.cells, key=lambda cell: (cell.row, cell.col)))
        else:
            units.append(block)
    return units


def _candidate_unit_count(page: PageResult) -> int:
    """Count units with a normalized candidate that differs from the baseline."""
    return sum(
        any(comparison_key(candidate) != comparison_key(unit.original_text) for candidate in unit.candidates)
        for unit in _page_text_units(page)
    )


def _candidate_assessment(page: PageResult, reference: str) -> dict[str, float | int]:
    """Measure the best user-selectable candidate outcome on public truth data.

    This is an acceptance-only oracle: it never writes ``edited_text`` and the
    application itself never has access to ground truth.
    """
    units = _page_text_units(page)
    baseline = [unit.original_text for unit in units]
    baseline_errors = int(cer(reference, "\n".join(baseline))["errors"])
    improved = 0
    worsened_only = 0
    neutral = 0

    for index, unit in enumerate(units):
        candidates: list[str] = []
        seen = {comparison_key(unit.original_text)}
        for candidate in unit.candidates:
            key = comparison_key(candidate)
            if not key or key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
        if not candidates:
            continue
        errors: list[int] = []
        for candidate in candidates:
            trial = list(baseline)
            trial[index] = candidate
            errors.append(int(cer(reference, "\n".join(trial))["errors"]))
        if min(errors) < baseline_errors:
            improved += 1
        elif min(errors) > baseline_errors:
            worsened_only += 1
        else:
            neutral += 1

    assisted = list(baseline)
    assisted_errors = baseline_errors
    for index, unit in enumerate(units):
        best_text = assisted[index]
        best_errors = assisted_errors
        seen: set[str] = set()
        for candidate in [unit.original_text, *unit.candidates]:
            key = comparison_key(candidate)
            if not key or key in seen:
                continue
            seen.add(key)
            trial = list(assisted)
            trial[index] = candidate
            trial_errors = int(cer(reference, "\n".join(trial))["errors"])
            if trial_errors < best_errors:
                best_text = candidate
                best_errors = trial_errors
        assisted[index] = best_text
        assisted_errors = best_errors

    reference_chars = int(cer(reference, "")["reference"])
    return {
        "candidate_improved_units": improved,
        "candidate_worsened_only_units": worsened_only,
        "candidate_neutral_units": neutral,
        "candidate_assisted_errors": assisted_errors,
        "candidate_assisted_cer": assisted_errors / reference_chars if reference_chars else 0.0,
    }


def _page_change_counts(page: PageResult) -> tuple[int, int]:
    """Automatic reconciliation never changes text; count only real review states."""
    unresolved = 0
    for block in page.blocks:
        if block.proofread_status in {"conflict", "incomplete"}:
            unresolved += 1
    return 0, unresolved


def _capture_job_row(row: dict[str, Any], job: ImageJob) -> None:
    if job.status != "done" or job.result is None:
        return
    row["baseline_observed"] = extract_original_text(job.result)
    row["auto_observed"] = extract_effective_text(job.result)
    # Keep the pre-existing observed/overall/classes fields tied to the first
    # OCR pass; the new auto_proofread fields carry the effective result.
    row["observed"] = row["baseline_observed"]
    row["error"] = None
    row["baseline_preserved"] = _without_whitespace(row["baseline_observed"]) == _without_whitespace(row["auto_observed"])
    row["candidate_units"] = _candidate_unit_count(job.result)
    row["detail_reviewed_regions"] = int(job.result.detail_reviewed_regions)
    row["detail_candidate_regions"] = int(job.result.detail_candidate_regions)
    row["detail_review_status"] = str(job.result.detail_review_status)
    row["auto_changes"], row["unresolved_conflicts"] = _page_change_counts(job.result)
    row["correct_changes"] = 0
    row["wrong_changes"] = 0
    row["unresolved_changes"] = 0
    baseline_result = cer(row["reference"], row["baseline_observed"])
    auto_result = cer(row["reference"], row["auto_observed"])
    row["baseline_errors"] = int(baseline_result["errors"])
    row["baseline_cer"] = float(baseline_result["cer"])
    row["auto_errors"] = int(auto_result["errors"])
    row["auto_cer"] = float(auto_result["cer"])
    row.update(_candidate_assessment(job.result, row["reference"]))


def _run_batch(model_root: Path, session_dir: Path, cases: list[dict[str, Any]], stress_count: int = 0, *, auto_proofread: bool = False) -> tuple[list[dict[str, Any]], int, float]:
    jobs: list[ImageJob] = []
    truth_by_job: dict[str, dict[str, Any]] = {}
    for job_id, case in build_job_specs(cases, stress_count):
        jobs.append(ImageJob(id=job_id, source_path=str(case["_image_path"]), mode=str(case["class"]), auto_proofread=auto_proofread))
        truth = dict(case["_truth"])
        truth_by_job[job_id] = truth
    process = psutil.Process(os.getpid())
    rows_by_id: dict[str, dict[str, Any]] = {
        job.id: {"case_id": job.id, "class": truth_by_job[job.id]["class"], "reference": truth_by_job[job.id]["text"], "observed": "", "baseline_observed": "", "auto_observed": "", "baseline_preserved": False, "candidate_units": 0, "auto_changes": 0, "correct_changes": 0, "wrong_changes": 0, "unresolved_changes": 0, "unresolved_conflicts": 0, "error": "worker_incomplete"}
        for job in jobs
    }
    started = time.monotonic()
    peak = _sampled_rss(process)
    worker = OCRWorker(model_root, session_dir)
    try:
        try:
            worker.start(jobs)
            _events, peak = _wait_for_worker(worker, jobs, process, time.monotonic() + 3600)
        except OfflineError as error:
            for row in rows_by_id.values():
                row["error"] = error.code
        except RuntimeError as error:
            safe_code = "worker_timeout" if str(error) == "worker_timeout" else "worker_incomplete"
            for job in jobs:
                row = rows_by_id[job.id]
                if job.status == "done" and job.result is not None:
                    _capture_job_row(row, job)
                elif job.status == "failed" and job.error_code:
                    row["error"] = job.error_code
                else:
                    row["error"] = safe_code
        else:
            for job in jobs:
                row = rows_by_id[job.id]
                if job.status == "done" and job.result is not None:
                    _capture_job_row(row, job)
                elif job.error_code:
                    row["error"] = job.error_code
    finally:
        worker.close()
    return list(rows_by_id.values()), peak, time.monotonic() - started


def _run_cancellation_restart(model_root: Path, session_dir: Path, case: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Exercise terminate-and-restart with one real OCRWorker job.

    This is opt-in with stress runs because it deliberately initializes the
    native OCR stack twice.  The first job is cancelled immediately after the
    child is spawned; the second job must emit a terminal page event.
    """
    process = psutil.Process(os.getpid())
    peak = _sampled_rss(process)
    job = ImageJob(id="cancellation-check", source_path=str(case["_image_path"]), mode=str(case["class"]))
    worker = OCRWorker(model_root, session_dir)
    try:
        worker.start([job])
        worker.cancel()
        cancel_events = worker.poll()
        peak = max(peak, _sampled_rss(process))
        if not any(event.get("event") == "cancelled" for event in cancel_events):
            return {"status": "failed", "reason": "cancel_event_missing"}, peak
    except OfflineError as error:
        return {"status": "not_run", "reason": error.code}, peak
    finally:
        worker.close()

    restart_job = ImageJob(id="restart-check", source_path=str(case["_image_path"]), mode=str(case["class"]))
    restarted = OCRWorker(model_root, session_dir)
    try:
        restarted.start([restart_job])
        _events, restart_peak = _wait_for_worker(restarted, [restart_job], process, time.monotonic() + 3600)
        peak = max(peak, restart_peak, _sampled_rss(process))
        if not restart_passed(restart_job):
            return {"status": "failed", "reason": "restart_result_missing"}, peak
        return {"status": "pass", "reason": "cancelled_and_restarted"}, peak
    except OfflineError as error:
        return {"status": "not_run", "reason": error.code}, peak
    except RuntimeError as error:
        # Only fixed, local status codes are written to the report.
        reason = str(error) if str(error) in {"worker_timeout"} else "restart_incomplete"
        return {"status": "failed", "reason": reason}, peak
    finally:
        restarted.close()


def _class_summary(rows: list[dict[str, Any]], case_class: str, target: float) -> dict[str, Any]:
    summary = summarize_results(row for row in rows if row["class"] == case_class)
    summary.update({"class": case_class, "target": target, "pass": summary["pages"] > 0 and summary["failed_pages"] == 0 and summary["cer"] <= target})
    return summary


def run_benchmark(dataset: Path, models: Path, output: Path, stress_count: int = 0) -> dict[str, Any]:
    total_started = time.monotonic()
    manifest_path = dataset / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases: list[dict[str, Any]] = []
    for item in manifest.get("cases", []):
        case = dict(item)
        case["_image_path"] = dataset / str(item["image"])
        case["_truth"] = json.loads((dataset / str(item["ground_truth"])).read_text(encoding="utf-8"))
        cases.append(case)
    if len(cases) != 40:
        raise ValueError("public dataset must contain exactly 40 cases")
    if stress_count < 0:
        raise ValueError("stress count must be non-negative")

    base_rows: list[dict[str, Any]] = []
    stress_rows: list[dict[str, Any]] = []
    max_rss = 0
    walltime = 0.0
    cancellation = {"status": "not_run", "reason": "bounded stress run is opt-in"}
    with SessionStore(output.parent / ".benchmark-sessions") as session:
        if stress_count > 0:
            cancellation, cancel_peak = _run_cancellation_restart(models, session.path, cases[0])
            max_rss = max(max_rss, cancel_peak)
        # One three-pass run contains both the first-pass baseline and the
        # effective voted result, so both CERs come from the same input batch.
        base_rows, peak, elapsed = _run_batch(models, session.path, cases, auto_proofread=True)
        max_rss = max(max_rss, peak)
        walltime += elapsed
        auto_quality = summarize_auto_rows(base_rows)
        accuracy = {"platform": {"os": platform.system(), "arch": platform.machine()},
                    "baseline": {case_class: values["baseline"] for case_class, values in auto_quality.items()},
                    "auto_proofread": {case_class: values["auto_proofread"] for case_class, values in auto_quality.items()},
                    "candidate_assisted": {case_class: values["candidate_assisted"] for case_class, values in auto_quality.items()},
                    "candidate_units": {case_class: values["candidate_units"] for case_class, values in auto_quality.items()},
                    "detail_reviewed_regions": {case_class: values["detail_reviewed_regions"] for case_class, values in auto_quality.items()},
                    "detail_candidate_regions": {case_class: values["detail_candidate_regions"] for case_class, values in auto_quality.items()},
                    "candidate_improved_units": {case_class: values["candidate_improved_units"] for case_class, values in auto_quality.items()},
                    "candidate_worsened_only_units": {case_class: values["candidate_worsened_only_units"] for case_class, values in auto_quality.items()},
                    "candidate_neutral_units": {case_class: values["candidate_neutral_units"] for case_class, values in auto_quality.items()},
                    "auto_changes": {case_class: values["auto_changes"] for case_class, values in auto_quality.items()},
                    "correct_changes": {case_class: values["correct_changes"] for case_class, values in auto_quality.items()},
                    "wrong_changes": {case_class: values["wrong_changes"] for case_class, values in auto_quality.items()},
                    "unresolved_changes": {case_class: values["unresolved_changes"] for case_class, values in auto_quality.items()},
                    "unresolved_conflicts": {case_class: values["unresolved_conflicts"] for case_class, values in auto_quality.items()},
                    "overall": summarize_results(base_rows),
                    "classes": {"scan": _class_summary(base_rows, 'scan', 0.005),
                                "photo": _class_summary(base_rows, 'photo', 0.01)},
                    "cases": _public_rows(base_rows), "walltime_seconds": elapsed,
                    "max_rss_bytes_process_tree": peak, "stress_validation": "separate"}
        output.parent.mkdir(parents=True, exist_ok=True)
        checkpoint = output.with_name(output.stem + '-accuracy.json')
        checkpoint.write_text(json.dumps(accuracy, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(f"Accuracy scan CER={accuracy['classes']['scan']['cer']:.4%} photo CER={accuracy['classes']['photo']['cer']:.4%}", flush=True)
        if stress_count > 0:
            stress_rows, peak, elapsed = _run_batch(models, session.path, cases, stress_count=stress_count)
            max_rss = max(max_rss, peak)
            walltime += elapsed
    overall = summarize_results(base_rows)
    auto_quality = summarize_auto_rows(base_rows)
    auto_proofread_pass = _auto_proofread_acceptance(base_rows, auto_quality)
    report_rows = _public_rows(base_rows)
    scan = _class_summary(base_rows, "scan", 0.005)
    photo = _class_summary(base_rows, "photo", 0.01)
    stress_summary = summarize_results(stress_rows) if stress_rows else None
    report = {
        "schema": 1,
        "dataset": {"pages": len(cases), "stress_count": stress_count, "characters": sum(int(case["characters"]) for case in cases)},
        "platform": {"os": platform.system(), "release": platform.release(), "arch": platform.machine()},
        "walltime_seconds": time.monotonic() - total_started,
        "processing_walltime_seconds": walltime,
        "max_rss_bytes_process_tree": max_rss,
        "overall": overall,
        "classes": {"scan": scan, "photo": photo},
        "baseline": {case_class: values["baseline"] for case_class, values in auto_quality.items()},
        "auto_proofread": {case_class: values["auto_proofread"] for case_class, values in auto_quality.items()},
        "candidate_assisted": {case_class: values["candidate_assisted"] for case_class, values in auto_quality.items()},
        "candidate_units": {case_class: values["candidate_units"] for case_class, values in auto_quality.items()},
        "detail_reviewed_regions": {case_class: values["detail_reviewed_regions"] for case_class, values in auto_quality.items()},
        "detail_candidate_regions": {case_class: values["detail_candidate_regions"] for case_class, values in auto_quality.items()},
        "candidate_improved_units": {case_class: values["candidate_improved_units"] for case_class, values in auto_quality.items()},
        "candidate_worsened_only_units": {case_class: values["candidate_worsened_only_units"] for case_class, values in auto_quality.items()},
        "candidate_neutral_units": {case_class: values["candidate_neutral_units"] for case_class, values in auto_quality.items()},
        "auto_changes": {case_class: values["auto_changes"] for case_class, values in auto_quality.items()},
        "correct_changes": {case_class: values["correct_changes"] for case_class, values in auto_quality.items()},
        "wrong_changes": {case_class: values["wrong_changes"] for case_class, values in auto_quality.items()},
        "unresolved_changes": {case_class: values["unresolved_changes"] for case_class, values in auto_quality.items()},
        "unresolved_conflicts": {case_class: values["unresolved_conflicts"] for case_class, values in auto_quality.items()},
        "cases": report_rows,
        "stress": ({"requested_pages": stress_count, "completed_pages": stress_summary["pages"] - stress_summary["failed_pages"], "failed_pages": stress_summary["failed_pages"], "summary": stress_summary, "cases": _public_rows(stress_rows)} if stress_summary else {"requested_pages": 0, "completed_pages": 0, "failed_pages": 0, "summary": None, "cases": []}),
        "auto_proofread_pass": auto_proofread_pass,
        "pass": bool(scan["pass"] and photo["pass"] and overall["failed_pages"] == 0 and auto_proofread_pass and (not stress_count or cancellation["status"] == "pass") and (stress_summary is None or stress_summary["failed_pages"] == 0)),
        "windows_validation": "pending; this report was produced on the current host",
        "cancellation_restart": cancellation,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Run actual OCRWorker against public acceptance samples")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stress-count", type=int, default=0, help="process exactly N round-robin public inputs in one worker batch")
    args = parser.parse_args()
    report = run_benchmark(args.dataset, args.models, args.output, args.stress_count)
    print(f"OCR base_pages={report['overall']['pages']} CER={report['overall']['cer']:.4%} failed={report['overall']['failed_pages']} stress_pages={report['stress']['completed_pages']}/{report['stress']['requested_pages']}")
    if not report["pass"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
