"""Allowlisted, bounded diagnostic events; no OCR strings or file paths."""
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
import json
import math
import time

STAGES = frozenset({"engine_init", "model_validate", "module_import", "model_create", "first_inference", "inference", "prepare", "recognize", "integrity", "proofread", "page_done", "page_error"})
CURRENT_PROBE = ContextVar('ocr_diagnostic_probe', default=None)


@contextmanager
def measured(name):
    probe = CURRENT_PROBE.get()
    started = time.monotonic()
    if probe and name in {'model_validate', 'module_import', 'model_create'}:
        probe.sender.send({'event': 'engine_stage', 'stage': name})
    try:
        yield
    finally:
        if probe:
            probe.duration(name, (time.monotonic()-started)*1000)


def sanitized_event(event):
    if not isinstance(event, dict) or event.get("stage") not in STAGES:
        return None
    numbers = {}
    for key in ("elapsed_ms", "rss_mb"):
        value = event.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
            return None
        numbers[key] = round(float(value), 1)
    return {"stage": event["stage"], **numbers}


class DiagnosticRecorder:
    """Session-only ring. Closing the app removes it, even after a failed job."""
    def __init__(self):
        self.events = deque(maxlen=256)

    def record(self, event):
        value = sanitized_event(event)
        if value is not None:
            self.events.append(value)

    def clear(self):
        self.events.clear()

    def text(self):
        return json.dumps(list(self.events), ensure_ascii=False, indent=2)


class DiagnosticProbe:
    def __init__(self, sender, *, enabled=False):
        self.sender = sender
        self.enabled = enabled
        self.started = time.monotonic()

    def begin_page(self):
        self.started = time.monotonic()

    def stage(self, name):
        if not self.enabled:
            return
        now = time.monotonic()
        self.duration(name, (now-self.started)*1000)
        self.started = now

    def duration(self, name, elapsed_ms):
        if not self.enabled:
            return
        try:
            import psutil
            rss = psutil.Process().memory_info().rss / (1024 * 1024)
        except Exception:
            rss = 0.0
        value = sanitized_event({"stage": name, "elapsed_ms": elapsed_ms, "rss_mb": rss})
        if value is not None:
            self.sender.send({"event": "diagnostic", **value})
