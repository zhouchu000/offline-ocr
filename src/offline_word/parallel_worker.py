"""Two isolated OCR lanes with a shared page queue and bounded CPU budget.

Native Paddle engines cannot safely share mutable predictors between threads.
Each lane therefore owns one persistent child process. The GUI schedules pages,
retains source order, and never sends one page to two lanes.
"""
from collections import deque
from dataclasses import dataclass

from .domain import ImageJob
from .performance import parallel_capacity, working_set_gib
from .security import OfflineError
from .worker import OCRWorker


@dataclass
class _Lane:
    worker: OCRWorker
    active: ImageJob | None = None
    finished: bool = False


class ParallelOCRWorker:
    def __init__(self, model_root, session_dir, *, worker_factory=OCRWorker, capacity=None):
        self.model_root = model_root
        self.session_dir = session_dir
        self.worker_factory = worker_factory
        self.capacity = capacity
        self.lanes = []
        self.pending = deque()
        self._seen = set()
        self._events = []
        self._running = False
        self._closing = False
        self._cancelled = False
        self.lane_count = 0

    @property
    def busy(self):
        return self._running

    def start(self, jobs):
        if self.busy:
            raise OfflineError('worker_still_running')
        if not jobs:
            raise OfflineError('empty_queue')
        if len({job.id for job in jobs}) != len(jobs):
            raise OfflineError('duplicate_job_id')
        capacity, per_lane_threads = self.capacity or parallel_capacity(jobs)
        self._working_set = working_set_gib(jobs)
        self.lane_count = min(capacity, len(jobs))
        threads = max(1, capacity * per_lane_threads // self.lane_count)
        self.pending = deque(jobs)
        self._seen = {job.id for job in jobs}
        self._running = True
        self._closing = self._cancelled = False
        self.lanes = []
        try:
            for _ in range(self.lane_count):
                job = self.pending.popleft()
                worker = self.worker_factory(self.model_root, self.session_dir, cpu_threads=threads)
                lane = _Lane(worker, job)
                self.lanes.append(lane)
                worker.start([job], keep_alive=True)
        except Exception:
            self.close()
            raise

    def add_jobs(self, jobs):
        if not self.busy or self._closing or self._cancelled:
            return False
        added = []
        seen = set(self._seen)
        for job in jobs:
            if job.id not in seen:
                added.append(job)
                seen.add(job.id)
        if added and self.lane_count == 2 and working_set_gib(added) > self._working_set:
            # Larger newly imported pages need a fresh admission check after
            # this batch. The GUI already retains and continues deferred jobs.
            return False
        self._seen.update(job.id for job in added)
        self.pending.extend(added)
        self._events.append({'event': 'queue_added', 'job_ids': [job.id for job in added]})
        return True

    def remove_jobs(self, job_ids):
        if not self.busy or self._closing or self._cancelled:
            return False
        requested = set(job_ids)
        removed = requested & {job.id for job in self.pending}
        self.pending = deque(job for job in self.pending if job.id not in removed)
        if removed:
            self._events.append({'event': 'queue_removed', 'job_ids': sorted(removed)})
        remaining = requested - removed
        for lane in self.lanes:
            if lane.active and lane.active.id in remaining and lane.active.status == 'pending':
                if lane.worker.remove_jobs([lane.active.id]):
                    remaining.remove(lane.active.id)
        if remaining:
            self._events.append({'event': 'queue_remove_rejected', 'job_ids': sorted(remaining)})
        return True

    @staticmethod
    def _fail_active(lane, events):
        if lane.active is not None:
            job = lane.active
            job.status, job.error_code = 'failed', 'worker_process_failed'
            events.append({'event': 'page_error', 'job_id': job.id, 'code': job.error_code})
            lane.active = None

    def poll(self):
        if self._cancelled:
            for lane in self.lanes:
                lane.worker.poll()  # Drain shutdown state; discard stale OCR events.
            if not any(lane.worker.busy for lane in self.lanes):
                self._running = self._cancelled = False
                return [{'event': 'cancelled'}]
            return []
        if not self.busy:
            return []
        events, self._events = self._events, []
        for lane in self.lanes:
            if lane.finished:
                continue
            try:
                received = lane.worker.poll()
            except Exception:
                received = []
                lane.worker.close()
            for event in received:
                kind = event.get('event')
                if kind in {'finished', 'cancelled'}:
                    self._fail_active(lane, events)
                    lane.finished = True
                    continue
                if kind in {'page_done', 'page_error'} and lane.active and lane.active.id == event.get('job_id'):
                    lane.active = None
                elif kind == 'queue_removed' and lane.active and lane.active.id in event.get('job_ids', []):
                    lane.active = None
                events.append(event)
            if not lane.worker.busy and not lane.finished:
                self._fail_active(lane, events)
                lane.finished = True
        for lane in self.lanes:
            if not self._closing and not lane.finished and lane.active is None and self.pending:
                job = self.pending[0]
                if lane.worker.add_jobs([job]):
                    lane.active = self.pending.popleft()
                else:
                    lane.worker.close()
                    lane.finished = True
        if self.pending and all(lane.finished for lane in self.lanes):
            while self.pending:
                job = self.pending.popleft()
                job.status, job.error_code = 'failed', 'worker_process_failed'
                events.append({'event': 'page_error', 'job_id': job.id, 'code': job.error_code})
        if not self.pending and all(lane.active is None for lane in self.lanes) and not self._closing:
            self._closing = True
            for lane in self.lanes:
                if not lane.finished:
                    try:
                        lane.worker.finish_queue()
                    except (OSError, EOFError):
                        lane.worker.close()
                        lane.finished = True
        if all(lane.finished for lane in self.lanes):
            self._running = False
            events.append({'event': 'finished'})
        return events

    def cancel(self):
        self._cancelled = True
        self.pending.clear()
        self._events.clear()
        self._stop_lanes('cancel')

    def close(self):
        self.pending.clear()
        try:
            self._stop_lanes('close')
        finally:
            self._running = any(lane.worker.busy for lane in self.lanes)

    def _stop_lanes(self, method):
        failed = False
        for lane in self.lanes:
            try:
                getattr(lane.worker, method)()
            except Exception:
                failed = True
        if failed:
            raise OfflineError('worker_shutdown_failed')
