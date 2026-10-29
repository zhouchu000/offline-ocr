"""Short-lived model reuse across batches and local corrections.

Paddle predictors stay inside their original process. Only idle workers are
cached, at most two for 60-180 seconds under memory pressure checks. Source
images/results are never cached by this parent-side service.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import time

from .performance import available_memory_gib, parallel_capacity
from .security import OfflineError, claim_session_lease, configure_offline_env, cpu_thread_limit
from .worker import OCRWorker, _process_pages, _process_region, _silence_child_output

IDLE_SECONDS=60.0
MAX_IDLE_SECONDS=180.0


def retention_seconds(free_gib, rss_gib):
    """Extend useful reuse only with measured headroom; never relax admission."""
    if free_gib is None or free_gib < 1.5:
        return 0.0
    if free_gib >= 3.0 and 0 < rss_gib <= 2.5:
        return MAX_IDLE_SECONDS
    if free_gib >= 2.0 and 0 < rss_gib <= 4.0:
        return 120.0
    return IDLE_SECONDS


class _BatchSender:
    def __init__(self, sender):
        self.sender=sender

    def send(self, event):
        self.sender.send({'event':'batch_finished'} if event.get('event')=='finished' else event)

    def close(self):
        pass  # This logical batch does not own the retained process pipe.


def _retained_process(model_root, session_dir, request, commands, sender):
    _silence_child_output()
    lease=claim_session_lease(session_dir)
    work_dir=Path(session_dir)/f'lane-{os.getpid()}'
    work_dir.mkdir(mode=0o700,parents=True,exist_ok=True)
    runtime=None
    try:
        while request:
            if request['action']=='batch':
                runtime=_process_pages(model_root,session_dir,request['jobs'],_BatchSender(sender),
                                       commands=commands,keep_alive=True,runtime=runtime,work_dir=work_dir)
            elif request['action']=='region':
                runtime=_process_region(model_root,session_dir,request['job'],request['bbox'],
                                        _BatchSender(sender),runtime=runtime,work_dir=work_dir)
            else:
                raise OfflineError('invalid_worker_command')
            # Independent ceiling also handles a lost parent/GUI state.
            if not commands.poll(MAX_IDLE_SECONDS+15):
                break
            request=commands.recv()
    except (EOFError,OSError):
        pass
    finally:
        lease.close()
        commands.close()
        sender.close()
    return runtime


class RetainedOCRWorker(OCRWorker):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.idle=False

    @property
    def busy(self):
        return self._process is not None and not self.idle

    @property
    def reusable(self):
        return self.idle and not self._cancelled and self._process is not None and self._process.is_alive()

    @property
    def rss_gib(self):
        if not self.reusable: return 0.0
        try:
            import psutil
            return psutil.Process(self._process.pid).memory_info().rss / 1024**3
        except Exception:
            return 0.0

    def _dispatch(self, request, jobs):
        if self.busy: raise OfflineError('worker_still_running')
        self._jobs={job.id:job for job in jobs}
        if self.reusable:
            self.idle=False
            self._command_sender.send(request)
            return
        if self._process is not None: self.close()
        self.idle=False
        configure_offline_env(self.model_root,self.session_dir)
        receiver,sender=self._context.Pipe(duplex=False)
        try:
            self._start_process(_retained_process,(str(self.model_root),str(self.session_dir),request,receiver))
            from .task_queue import CommandMailbox
            self._command_sender=CommandMailbox(sender)
        except Exception:
            sender.close()
            self.close()
            raise
        finally:
            receiver.close()

    def start(self,jobs,*,keep_alive=True):
        if not keep_alive: raise ValueError('retained_batch_requires_queue_finish')
        self._dispatch({'action':'batch','jobs':jobs},jobs)

    def start_region(self,job,bbox):
        self._dispatch({'action':'region','job':job,'bbox':bbox},[job])

    def poll(self):
        events=super().poll()
        for event in events:
            if event.get('event')=='batch_finished':
                self.idle=True
                self._jobs.clear()
                event['event']='finished'
        return events

    def cancel(self):
        self.idle=False
        super().cancel()


@dataclass
class _Cached:
    worker: RetainedOCRWorker
    key: tuple
    released: float


class _Borrowed:
    def __init__(self,cache,worker,key):
        self.cache,self.worker,self.key=cache,worker,key
        self.released=False
        self.cancelled=False

    def __getattr__(self,name):
        return getattr(self.worker,name)

    def cancel(self):
        self.cancelled=True
        self.worker.cancel()

    def close(self):
        if self.released: return
        self.released=True
        self.cache.active.discard(self)
        if not self.cancelled and self.worker.reusable:
            self.cache.idle.append(_Cached(self.worker,self.key,self.cache.clock()))
            self.cache.expire()
        else:
            self.worker.close()


class WarmWorkerCache:
    def __init__(self,model_root,session_dir,*,factory=RetainedOCRWorker,clock=time.monotonic,memory=available_memory_gib):
        self.model_root,self.session_dir=model_root,session_dir
        self.factory,self.clock,self.memory=factory,clock,memory
        self.idle=[]
        self.active=set()

    def expire(self):
        free=self.memory()
        retained=[]
        for entry in self.idle:
            if (not entry.worker.reusable or self.clock()-entry.released>=retention_seconds(free, entry.worker.rss_gib)
                    or free is None or free<1.5 or len(retained)>=2):
                entry.worker.close()
            else:
                retained.append(entry)
        self.idle=retained

    def _borrow(self,threads,profile,*,region=False):
        self.expire()
        key=(threads,*profile)
        match=next((entry for entry in self.idle if entry.key==key),None)
        if region and match is None:
            match=next(iter(self.idle),None)
            if match is not None: key=match.key
        if match is not None:
            self.idle.remove(match)
            worker=match.worker
        else:
            # A changed profile must not leave old predictors competing for RAM.
            self.clear_idle()
            worker=self.factory(self.model_root,self.session_dir,cpu_threads=threads)
        borrowed=_Borrowed(self,worker,key)
        self.active.add(borrowed)
        return borrowed

    def batch(self,jobs,*,parallel=False):
        from .parallel_worker import ParallelOCRWorker
        self.expire()
        profile=(all(job.text_only for job in jobs),any(job.auto_proofread for job in jobs))
        if parallel:
            # Count existing matching predictors as memory already allocated,
            # rather than requiring their working set a second time.
            free=self.memory()
            credit=sum(entry.worker.rss_gib for entry in self.idle if entry.key[1:]==profile)
            capacity=parallel_capacity(jobs,available_gib=free+credit if free is not None else None)
        else:
            capacity=(1,cpu_thread_limit())
        return ParallelOCRWorker(self.model_root,self.session_dir,capacity=capacity,
            worker_factory=lambda *args,cpu_threads:self._borrow(cpu_threads,profile))

    def region(self,job):
        return self._borrow(cpu_thread_limit(),(True,False),region=True)

    def clear_idle(self):
        idle,self.idle=self.idle,[]
        failures=[]
        for entry in idle:
            try:
                entry.worker.close()
            except Exception:
                failures.append(entry)
        self.idle=failures
        if failures:
            raise OfflineError('worker_shutdown_failed')

    def close(self):
        failures=[]
        for borrowed in list(self.active):
            try:
                borrowed.cancel()
                borrowed.close()
            except Exception as error: failures.append(error)
        self.clear_idle()
        if failures: raise OfflineError('worker_shutdown_failed')
