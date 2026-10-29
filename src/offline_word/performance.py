"""Bounded CPU parallelism; never change a user's recognition quality."""
import os
from contextlib import contextmanager

from .security import cpu_thread_limit


def default_profile():
    value = os.environ.get("OFFLINE_WORD_PERF", "sequential")
    return value if value in {"sequential", "parallel"} else "sequential"


def available_memory_gib():
    try:
        import psutil
        return psutil.virtual_memory().available / (1024 ** 3)
    except Exception:
        return None


def parallel_capacity(jobs, *, available_gib=None, cpu_count=None):
    """Reserve two complete model working sets and room for Windows/UI.

    This is an admission guard, not a hard memory cap or a target-PC claim.
    Unknown free memory takes the conservative single-worker path.
    """
    cpu_count = max(1, cpu_count if cpu_count is not None else os.cpu_count() or 1)
    budget = min(cpu_count, cpu_thread_limit())
    available = available_memory_gib() if available_gib is None else available_gib
    per_worker = working_set_gib(jobs)
    count = 2 if cpu_count >= 4 and budget >= 2 and available is not None and available >= 2 * per_worker + 1.5 else 1
    return count, max(1, budget // count)


def working_set_gib(jobs):
    if any(job.auto_proofread for job in jobs):
        return 9.5
    if all(job.text_only for job in jobs):
        return 2.5
    from PIL import Image
    from .security import assert_local_path
    for job in jobs:
        try:
            with Image.open(assert_local_path(job.source_path, 'remote_image_path')) as image:
                image.seek(job.frame_index)
                if image.width * image.height > 2_000_000:
                    return 5.0
        except Exception:
            return 5.0
    return 3.0


@contextmanager
def child_cpu_environment(threads):
    """Bound each spawned interpreter before it imports NumPy or Paddle."""
    if threads is None:
        yield
        return
    if threads not in {1, 2, 3, 4}:
        raise ValueError('invalid_cpu_threads')
    keys = ('OFFLINE_WORD_CPU_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS',
            'OPENBLAS_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS', 'NUMEXPR_NUM_THREADS')
    previous = {key: os.environ.get(key) for key in keys}
    try:
        os.environ.update({key: str(threads) for key in keys})
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
