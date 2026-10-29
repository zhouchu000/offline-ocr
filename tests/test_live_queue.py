from collections import deque

from offline_word.domain import ImageJob
from offline_word.task_queue import PageTaskQueue


class Commands:
    def __init__(self):
        self.messages = deque()

    def poll(self):
        return bool(self.messages)

    def recv(self):
        return self.messages.popleft()


class Sender:
    def __init__(self):
        self.events = []

    def send(self, message):
        self.events.append(message)


def test_live_queue_add_remove_and_start_race_are_acknowledged_at_page_boundary():
    a, b, c, d = [ImageJob(name, f'{name}.png') for name in 'abcd']
    commands, sender = Commands(), Sender()
    queue = PageTaskQueue([a, b], sender, commands)
    assert queue.next_job() is a
    # a is already in flight. An old GUI snapshot might still call it pending.
    commands.messages.extend([
        {'action': 'add', 'jobs': [c, d, c]},
        {'action': 'remove', 'job_ids': ['a', 'b', 'd']},
    ])
    assert queue.next_job() is c
    assert queue.next_job() is None
    assert sender.events == [
        {'event': 'queue_added', 'job_ids': ['c', 'd']},
        {'event': 'queue_removed', 'job_ids': ['b', 'd']},
        {'event': 'queue_remove_rejected', 'job_ids': ['a']},
    ]


def test_added_job_after_last_page_is_drained_before_worker_exits():
    commands, sender = Commands(), Sender()
    queue = PageTaskQueue([ImageJob('a', 'a.png')], sender, commands)
    assert queue.next_job().id == 'a'
    commands.messages.append({'action': 'add', 'jobs': [ImageJob('b', 'b.png')]})
    assert queue.next_job().id == 'b'
    assert queue.next_job() is None


def test_command_pipe_backpressure_cannot_block_the_gui():
    import multiprocessing
    import time
    from offline_word.task_queue import CommandMailbox
    receiver, sender = multiprocessing.get_context('spawn').Pipe(duplex=False)
    mailbox = CommandMailbox(sender)
    payload = {'action': 'add', 'body': 'x' * 2_000_000}
    started = time.monotonic()
    mailbox.send(payload)
    mailbox.send({'action': 'remove', 'job_ids': ['later']})
    assert time.monotonic() - started < .5
    assert receiver.poll(2)
    assert receiver.recv() == payload
    assert receiver.poll(2)
    assert receiver.recv()['action'] == 'remove'
    mailbox.close()
    receiver.close()
    mailbox._thread.join(2)
    assert not mailbox._thread.is_alive()


def test_writer_handles_closed_child_reader_and_rejects_later_commands():
    import multiprocessing
    import pytest
    from offline_word.task_queue import CommandMailbox
    receiver, sender = multiprocessing.get_context('spawn').Pipe(duplex=False)
    mailbox = CommandMailbox(sender)
    receiver.close()
    mailbox.send({'action': 'add', 'jobs': []})
    mailbox._thread.join(2)
    assert not mailbox._thread.is_alive()
    with pytest.raises(OSError):
        mailbox.send({'action': 'remove'})
