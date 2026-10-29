"""Page-boundary command handling for a running, local OCR process."""

from collections import deque
from queue import SimpleQueue
from threading import Event, Thread

from .domain import ImageJob


class CommandMailbox:
    """Never block the GUI on a full process pipe during a long OCR page.

    The writer owns the connection. Closing is asynchronous: the child closes
    its reader on normal exit or is terminated on cancellation, releasing any
    write already in progress. Commands waiting in memory are discarded then.
    """

    def __init__(self, connection):
        self._connection = connection
        self._pending = SimpleQueue()
        self._closed = Event()
        self._thread = Thread(target=self._run, name='ocr-queue-commands', daemon=True)
        self._thread.start()

    def send(self, command):
        if self._closed.is_set():
            raise OSError('queue_connection_closed')
        self._pending.put(command)

    def close(self):
        self._closed.set()
        self._pending.put(None)

    def _run(self):
        try:
            while True:
                command = self._pending.get()
                if command is None or self._closed.is_set():
                    break
                self._connection.send(command)
        except (OSError, EOFError):
            pass
        finally:
            self._closed.set()
            self._connection.close()


class PageTaskQueue:
    """Only this process may acknowledge removal of a not-yet-started page."""

    def __init__(self, jobs, sender, commands=None, *, keep_alive=False):
        self.pending = deque(jobs)
        self.seen = {job.id for job in jobs}
        self.sender = sender
        self.commands = commands
        self.keep_alive = keep_alive

    def next_job(self) -> ImageJob | None:
        while True:
            job = self._next_available()
            if job is not None or not self.keep_alive or self.commands is None:
                return job
            try:
                self.commands.poll(.1)
            except (EOFError, OSError):
                return None

    def _next_available(self) -> ImageJob | None:
        if self.commands is not None:
            try:
                while self.commands.poll():
                    command = self.commands.recv()
                    if command.get('action') == 'add':
                        added = []
                        for job in command.get('jobs', []):
                            if isinstance(job, ImageJob) and job.id not in self.seen:
                                self.seen.add(job.id)
                                self.pending.append(job)
                                added.append(job.id)
                        self.sender.send({'event': 'queue_added', 'job_ids': added})
                    elif command.get('action') == 'remove':
                        requested = set(command.get('job_ids', []))
                        removable = requested & {job.id for job in self.pending}
                        self.pending = deque(job for job in self.pending if job.id not in removable)
                        self.sender.send({'event': 'queue_removed', 'job_ids': sorted(removable)})
                        rejected = requested - removable
                        if rejected:
                            self.sender.send({'event': 'queue_remove_rejected', 'job_ids': sorted(rejected)})
                    elif command.get('action') == 'finish':
                        self.keep_alive = False
            except (EOFError, OSError):
                self.commands = None
        return self.pending.popleft() if self.pending else None
