from collections import deque
import os
from types import SimpleNamespace

import pytest

from offline_word.diagnostics import DiagnosticProbe, DiagnosticRecorder
from offline_word.domain import ImageJob, PageResult
from offline_word.parallel_worker import ParallelOCRWorker
from offline_word.performance import child_cpu_environment, parallel_capacity
from offline_word.task_queue import PageTaskQueue


class FakeWorker:
    def __init__(self, *args, cpu_threads):
        self.cpu_threads = cpu_threads
        self.busy = False
        self.events = []
        self.jobs = []
        self.starts = 0
        self.closed = False

    def start(self, jobs, *, keep_alive):
        assert keep_alive
        self.starts += 1
        self.jobs.extend(jobs)
        self.busy = True

    def add_jobs(self, jobs):
        self.jobs.extend(jobs)
        return True

    def remove_jobs(self, ids):
        self.events.append({'event': 'queue_removed', 'job_ids': ids})
        return True

    def finish_queue(self):
        self.events.append({'event': 'finished'})
        self.busy = False

    def complete(self):
        job = self.jobs[-1]
        job.status = 'done'
        job.result = PageResult(job.id, 0, 100, 100, 'public.png')
        self.events.append({'event': 'page_done', 'job_id': job.id, 'result': job.result})

    def poll(self):
        events, self.events = self.events, []
        return events

    def cancel(self):
        self.busy = False

    def close(self):
        self.closed = True
        self.cancel()


@pytest.fixture
def pool(monkeypatch):
    monkeypatch.setattr('offline_word.parallel_worker.parallel_capacity', lambda jobs: (2, 2))
    return ParallelOCRWorker('models', 'session', worker_factory=FakeWorker)


def jobs(count):
    return [ImageJob(str(i), f'public-{i}.png') for i in range(count)]


def test_two_lanes_share_work_without_reloading_or_reordering(pool):
    pages = jobs(5)
    pool.start(pages)
    first, second = [lane.worker for lane in pool.lanes]
    assert [first.jobs[0].id, second.jobs[0].id] == ['0','1']
    second.complete()
    events = pool.poll()
    assert [e['job_id'] for e in events if e['event'] == 'page_done'] == ['1']
    assert second.jobs[-1].id == '2'
    assert first.jobs[-1].id == '0'
    second.complete(); pool.poll()
    assert second.jobs[-1].id == '3'
    first.complete(); pool.poll()
    assert first.jobs[-1].id == '4'
    first.complete(); second.complete()
    assert not any(e['event'] == 'finished' for e in pool.poll())
    assert [e['event'] for e in pool.poll()] == ['finished']
    assert not pool.busy and pool.poll() == []
    assert first.starts == second.starts == 1
    assert [page.id for page in pages] == ['0','1','2','3','4']
    assert all(page.status == 'done' for page in pages)


def test_remove_waiting_tasks_add_more_and_reject_running_removal(pool):
    pages = jobs(4)
    pool.start(pages)
    pages[0].status = 'processing'
    assert pool.remove_jobs(['0','2'])
    events = pool.poll()
    assert {'event':'queue_removed','job_ids':['2']} in events
    assert {'event':'queue_remove_rejected','job_ids':['0']} in events
    assert pool.add_jobs([ImageJob('later','added.png')])
    pool.lanes[1].worker.complete()
    pool.poll()
    assert pool.lanes[1].active.id == '3'
    pool.lanes[1].worker.complete()
    pool.poll()
    assert pool.lanes[1].active.id == 'later'
    assert all(job.id != '2' for lane in pool.lanes for job in lane.worker.jobs)


def test_remove_dispatched_but_not_started_then_lane_reuses_engine(pool):
    pool.start(jobs(3))
    assert pool.remove_jobs(['0'])
    pool.poll()
    assert pool.lanes[0].active.id == '2'
    assert pool.lanes[0].worker.starts == 1


def test_crashed_lane_does_not_lose_other_pages(pool):
    pages = jobs(4)
    pool.start(pages)
    pool.lanes[0].worker.busy = False
    events = pool.poll()
    assert {'event':'page_error','job_id':'0','code':'worker_process_failed'} in events
    other = pool.lanes[1].worker
    other.complete(); pool.poll()
    assert other.jobs[-1].id == '2'
    other.complete(); pool.poll()
    other.complete(); pool.poll()
    assert pool.poll() == [{'event':'finished'}]
    assert [job.status for job in pages] == ['failed','done','done','done']


def test_cancel_closes_both_lanes_without_late_results(pool):
    pool.start(jobs(5))
    pool.lanes[0].worker.complete()
    pool.cancel()
    assert pool.poll() == [{'event':'cancelled'}]
    assert pool.poll() == [] and not pool.busy
    pool.close()
    assert all(lane.worker.closed for lane in pool.lanes)


@pytest.mark.parametrize('available,cpu,precise,text_only,expected', [
    (7,8,False,False,1),(16,8,False,False,2),(16,8,True,False,1),
    (24,8,True,False,2),(8,4,False,True,2),(24,2,False,False,1)])
def test_memory_and_cpu_admission_preserves_full_quality(available,cpu,precise,text_only,expected):
    job = ImageJob('p','public.png',auto_proofread=precise,text_only=text_only)
    count, threads = parallel_capacity([job], available_gib=available, cpu_count=cpu)
    assert count == expected and threads*count <= 4
    assert job.auto_proofread == precise and job.text_only == text_only


def test_child_thread_budget_restored_after_spawn_exception(monkeypatch):
    monkeypatch.setenv('OFFLINE_WORD_CPU_THREADS','3')
    monkeypatch.delenv('OMP_NUM_THREADS',raising=False)
    with pytest.raises(RuntimeError):
        with child_cpu_environment(2):
            assert os.environ['OFFLINE_WORD_CPU_THREADS'] == os.environ['OMP_NUM_THREADS'] == '2'
            raise RuntimeError('spawn')
    assert os.environ['OFFLINE_WORD_CPU_THREADS'] == '3' and 'OMP_NUM_THREADS' not in os.environ


@pytest.mark.parametrize('budget,expected', [(1,(1,1)),(2,(2,1)),(3,(2,1)),(4,(2,2))])
def test_parallel_respects_explicit_total_cpu_budget(monkeypatch, budget, expected):
    monkeypatch.setenv('OFFLINE_WORD_CPU_THREADS', str(budget))
    assert parallel_capacity(jobs(2), available_gib=24, cpu_count=8) == expected


def test_more_expensive_added_jobs_are_deferred_without_being_accepted(pool, monkeypatch):
    monkeypatch.setattr('offline_word.parallel_worker.working_set_gib', lambda pages: 9.5 if any(p.auto_proofread for p in pages) else 3)
    pool.start(jobs(3))
    later = ImageJob('later','public.png', auto_proofread=True)
    assert not pool.add_jobs([later])
    assert later.id not in pool._seen
    assert [job.id for job in pool.pending] == ['2']
    assert later.auto_proofread


def test_failure_stopping_one_lane_still_stops_the_other(pool):
    from offline_word.security import OfflineError
    pool.start(jobs(2))
    def failure():
        raise OSError('shutdown')
    pool.lanes[0].worker.cancel = failure
    with pytest.raises(OfflineError, match='worker_shutdown_failed'):
        pool.cancel()
    assert not pool.lanes[1].worker.busy
    assert pool.busy and pool.poll() == []
    pool.lanes[0].worker.busy = False
    assert pool.poll() == [{'event':'cancelled'}]


def test_keepalive_queue_waits_for_new_job_and_stops_only_on_finish():
    commands = deque([{'action':'add','jobs':[ImageJob('p','public.png')]}, {'action':'finish'}])
    sender = SimpleNamespace(send=lambda event: None)
    receiver = SimpleNamespace(poll=lambda *args: bool(commands), recv=commands.popleft)
    queue = PageTaskQueue([],sender,receiver,keep_alive=True)
    assert queue.next_job().id == 'p'
    assert queue.next_job() is None


def test_diagnostics_default_off_and_allowlist_strips_all_user_data():
    events = []
    sender = SimpleNamespace(send=events.append)
    DiagnosticProbe(sender).stage('page_done')
    assert not events
    recorder = DiagnosticRecorder()
    for _ in range(400):
        recorder.record({'stage':'recognize','elapsed_ms':2.2,'rss_mb':128, 'job_id':'私人正文', 'path':'secret.png', 'traceback':'敏感信息'})
    recorder.record({'stage':'私人正文','elapsed_ms':1,'rss_mb':3})
    recorder.record({'stage':'recognize','elapsed_ms':float('nan'),'rss_mb':3})
    assert len(recorder.events) == 256
    assert '私人' not in recorder.text() and 'secret' not in recorder.text() and '敏感' not in recorder.text()
    assert set(recorder.events[0]) == {'stage','elapsed_ms','rss_mb'}
    recorder.clear()
    assert recorder.text() == '[]'
