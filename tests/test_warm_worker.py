from types import SimpleNamespace

from offline_word.domain import ImageJob
from offline_word.warm_worker import WarmWorkerCache, _BatchSender


class ModelWorker:
    def __init__(self,*args,cpu_threads):
        self.cpu_threads=cpu_threads
        self.reusable=False
        self.busy=False
        self.closed=False
        self.rss_gib=2.5

    def start(self,jobs,*,keep_alive):
        self.busy=True
        self.reusable=False

    def cancel(self):
        self.closed=True
        self.busy=self.reusable=False

    close=cancel


def test_same_profile_reuses_idle_predictor_with_memory_bounded_extension():
    now=[0]
    cache=WarmWorkerCache('models','session',factory=ModelWorker,clock=lambda:now[0],memory=lambda:12)
    first=cache._borrow(4,(False,False))
    first.worker.reusable=True
    first.close()
    second=cache._borrow(4,(False,False))
    assert second.worker is first.worker and len(cache.active)==1
    second.close()
    now[0]=179
    cache.expire()
    assert len(cache.idle)==1
    now[0]=180
    cache.expire()
    assert cache.idle==[] and first.worker.closed


def test_cancellation_never_returns_predictor_to_idle_pool():
    cache=WarmWorkerCache('models','session',factory=ModelWorker,memory=lambda:12)
    borrowed=cache._borrow(4,(False,False))
    borrowed.worker.reusable=True
    borrowed.cancel();borrowed.close()
    assert not cache.active and not cache.idle and borrowed.worker.closed


def test_changed_profile_and_memory_pressure_release_old_predictors():
    memory=[12]
    cache=WarmWorkerCache('models','session',factory=ModelWorker,memory=lambda:memory[0])
    old=cache._borrow(4,(False,False))
    old.worker.reusable=True;old.close()
    current=cache._borrow(2,(False,False))
    assert old.worker.closed and current.worker is not old.worker
    current.worker.reusable=True;current.close()
    memory[0]=1
    cache.expire()
    assert current.worker.closed and not cache.idle


def test_region_borrows_existing_structure_process_without_another_model_copy():
    cache=WarmWorkerCache('models','session',factory=ModelWorker,memory=lambda:12)
    old=cache._borrow(2,(False,False))
    old.worker.reusable=True;old.close()
    region=cache.region(ImageJob('p','public.png'))
    assert region.worker is old.worker and not cache.idle
    region.close()
    cache.close()
    assert old.worker.closed and not cache.active


def test_batch_sender_keeps_pipe_open_but_emits_logical_completion():
    events=[]
    sender=_BatchSender(SimpleNamespace(send=events.append))
    sender.send({'event':'page_done','job_id':'p'})
    sender.send({'event':'finished'})
    sender.close()
    assert events==[{'event':'page_done','job_id':'p'},{'event':'batch_finished'}]
