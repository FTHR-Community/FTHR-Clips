from __future__ import annotations

import threading
import time
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'FTHR_UI'))
from core import camera_recorder


class _Capture:
    def __init__(self, index, *, opened=True, read=None, release_error=None,
                 opened_error=None, release_result=None, on_release=None):
        self.index = index
        self.opened = opened
        self.read_fn = read
        self.release_error = release_error
        self.opened_error = opened_error
        self.release_result = release_result
        self.on_release = on_release
        self.release_count = 0
        self.read_entered = threading.Event()

    def isOpened(self):
        if self.opened_error is not None:
            raise self.opened_error
        return self.opened

    def read(self):
        self.read_entered.set()
        if self.read_fn is not None:
            return self.read_fn()
        return False, None

    def release(self):
        self.release_count += 1
        if self.on_release is not None:
            self.on_release()
        if self.release_error is not None:
            raise self.release_error
        return self.release_result


class _Cv2:
    IMWRITE_JPEG_QUALITY = 1

    def __init__(self, captures):
        self.captures = list(captures)
        self.open_indices = []

    def VideoCapture(self, index):
        self.open_indices.append(index)
        capture = self.captures.pop(0)
        assert capture.index == index
        return capture

    def imencode(self, _ext, _frame, _options):
        return True, b'jpeg'


@pytest.fixture
def recorder(monkeypatch):
    monkeypatch.setattr(camera_recorder, '_AVAILABLE', True)
    previous = camera_recorder.CameraRecorder._instance
    camera_recorder.CameraRecorder._instance = None
    recorder = camera_recorder.CameraRecorder()
    try:
        yield recorder
    finally:
        recorder.stop()
        deadline = time.monotonic() + 2
        while (any(thread.is_alive() for thread in threading.enumerate()
                   if thread.name.startswith('fthr-camera'))
               and time.monotonic() < deadline):
            time.sleep(0.005)
        assert not any(thread.is_alive() for thread in threading.enumerate()
                       if thread.name.startswith('fthr-camera'))
        camera_recorder.CameraRecorder._instance = previous


def _wait(event, timeout=2.0):
    assert event.wait(timeout), 'camera operation did not finish in time'


def _wait_for_natural_retired(recorder, request):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with recorder._lock:
            context = recorder._retired_context
            if (context is not None
                    and context.get('request_token') is request
                    and context.get('natural_exit') is True
                    and context.get('thread') is not None):
                return context
        time.sleep(0.001)
    pytest.fail('owned natural completion was not retained')


def test_owned_open_can_be_cancelled_while_queued_without_opening(
        recorder, monkeypatch):
    capture = _Capture(0)
    fake = _Cv2([capture])
    monkeypatch.setattr(camera_recorder, '_cv2', fake)
    owner = object()
    request = object()
    open_result = []
    stop_result = []
    open_done = threading.Event()
    stop_done = threading.Event()
    recorder._op_lock.acquire()
    try:
        recorder.start_async(
            0, lambda ok: (open_result.append(ok), open_done.set()),
            owner_token=owner, request_token=request)
        deadline = time.monotonic() + 2
        while recorder._opening_context is None and time.monotonic() < deadline:
            time.sleep(0.001)
        assert recorder._opening_context is not None
        assert recorder.request_stop_async(
            owner_token=owner, request_token=request,
            expected_device_index=0,
            on_result=lambda ok: (stop_result.append(ok), stop_done.set()))
    finally:
        recorder._op_lock.release()
    _wait(open_done)
    _wait(stop_done)
    assert open_result == [False]
    assert stop_result == [True]
    assert fake.open_indices == []
    assert capture.release_count == 0
    assert not recorder.is_running()


def test_legacy_start_async_keeps_start_hook(monkeypatch, recorder):
    calls = []
    done = threading.Event()

    def fake_start(index):
        calls.append(index)
        return True

    monkeypatch.setattr(recorder, 'start', fake_start)
    recorder.start_async(7, lambda ok: done.set())
    _wait(done)
    assert calls == [7]


def test_foreign_opening_stop_request_is_a_noop(recorder, monkeypatch):
    entered = threading.Event()
    unblock = threading.Event()

    def open_capture(index):
        entered.set()
        unblock.wait(2)
        return capture

    capture = _Capture(0)
    fake = _Cv2([])
    fake.VideoCapture = open_capture
    monkeypatch.setattr(camera_recorder, '_cv2', fake)
    owner = object()
    foreign = object()
    request = object()
    opened = threading.Event()
    recorder.start_async(
        0, lambda ok: opened.set(), owner_token=owner,
        request_token=request)
    _wait(entered)
    assert not recorder.request_stop_async(
        owner_token=foreign, request_token=request,
        expected_device_index=0)
    assert recorder._opening_context is not None
    assert not recorder._opening_context['cancel_event'].is_set()
    unblock.set()
    _wait(opened)
    assert recorder.is_running()
    assert recorder.request_stop_async(owner_token=owner, request_token=request)


def test_same_owner_replaces_blocked_open_with_latest_request(
        recorder, monkeypatch):
    first_entered = threading.Event()
    first_unblock = threading.Event()
    captures = [_Capture(0), _Capture(0)]

    def open_capture(index):
        if len(fake.open_indices) == 0:
            first_entered.set()
            first_unblock.wait(2)
        fake.open_indices.append(index)
        return captures[len(fake.open_indices) - 1]

    fake = _Cv2([])
    fake.VideoCapture = open_capture
    monkeypatch.setattr(camera_recorder, '_cv2', fake)
    owner = object()
    first_request = object()
    second_request = object()
    first_result = []
    second_result = []
    first_done = threading.Event()
    second_done = threading.Event()
    recorder.start_async(
        0, lambda ok: (first_result.append(ok), first_done.set()),
        owner_token=owner, request_token=first_request)
    _wait(first_entered)
    recorder.start_async(
        0, lambda ok: (second_result.append(ok), second_done.set()),
        owner_token=owner, request_token=second_request)
    first_unblock.set()
    _wait(first_done)
    _wait(second_done)
    assert first_result == [False]
    assert second_result == [True]
    assert fake.open_indices == [0, 0]
    assert recorder._run_context['request_token'] is second_request
    assert recorder.request_stop_async(
        owner_token=owner, request_token=second_request)


def test_superseded_open_release_failure_blocks_successor(
        recorder, monkeypatch):
    entered = threading.Event()
    unblock = threading.Event()

    capture = _Capture(
        0, release_error=RuntimeError('camera release failed'))

    def open_capture(index):
        entered.set()
        unblock.wait(2)
        fake.open_indices.append(index)
        return capture

    fake = _Cv2([])
    fake.VideoCapture = open_capture
    monkeypatch.setattr(camera_recorder, '_cv2', fake)
    owner = object()
    first_request = object()
    second_request = object()
    first_result = []
    second_result = []
    first_done = threading.Event()
    second_done = threading.Event()
    recorder.start_async(
        0, lambda ok: (first_result.append(ok), first_done.set()),
        owner_token=owner, request_token=first_request)
    _wait(entered)
    recorder.start_async(
        0, lambda ok: (second_result.append(ok), second_done.set()),
        owner_token=owner, request_token=second_request)
    unblock.set()
    _wait(first_done)
    _wait(second_done)
    assert first_result == [False]
    assert second_result == [False]
    assert fake.open_indices == [0]
    assert capture.release_count == 1
    assert any(context.get('cleanup_ok') is False
               for context in recorder._pending_cleanup.values())
    assert any(context.get('phase') == 'cleanup_failed'
               for context in recorder._pending_cleanup.values())


def test_pre_registered_successor_waits_for_active_cleanup(
        recorder, monkeypatch):
    entered = threading.Event()
    unblock = threading.Event()

    def blocked_read():
        entered.set()
        unblock.wait(2)
        return False, None

    first = _Capture(0, read=blocked_read)
    second = _Capture(1)
    fake = _Cv2([first, second])
    monkeypatch.setattr(camera_recorder, '_cv2', fake)
    owner = object()
    first_request = object()
    second_request = object()
    first_done = threading.Event()
    second_done = threading.Event()
    second_result = []

    initial_empty_wait = threading.Event()
    nonempty_wait_entered = threading.Event()
    original_wait = recorder._wait_for_predecessors
    before_stop = threading.Event()
    allow_stop = threading.Event()

    def observed_wait(context):
        if context.get('request_token') is second_request:
            if context.get('predecessors'):
                nonempty_wait_entered.set()
                return original_wait(context)
            result = original_wait(context)
            if result:
                initial_empty_wait.set()
            return result
        return original_wait(context)

    original_stop = recorder._stop_locked

    def paused_stop(*args, **kwargs):
        before_stop.set()
        assert allow_stop.wait(2)
        return original_stop(*args, **kwargs)

    monkeypatch.setattr(recorder, '_wait_for_predecessors', observed_wait)
    recorder.start_async(
        0, lambda ok: first_done.set(), owner_token=owner,
        request_token=first_request)
    _wait(first_done)
    _wait(entered)
    monkeypatch.setattr(recorder, '_stop_locked', paused_stop)

    recorder.start_async(
        1, lambda ok: (second_result.append(ok), second_done.set()),
        owner_token=owner, request_token=second_request)
    _wait(initial_empty_wait)
    _wait(before_stop)
    with recorder._lock:
        assert recorder._opening_context is not None
        assert recorder._opening_context['request_token'] is second_request
    stopped = threading.Event()
    stop_result = []
    assert recorder.request_stop_async(
        owner_token=owner, request_token=first_request,
        on_result=lambda ok: (stop_result.append(ok), stopped.set()))
    allow_stop.set()

    assert not stopped.wait(0.05)
    assert not second_done.wait(0.05)
    assert fake.open_indices == [0]
    _wait(nonempty_wait_entered)
    assert recorder._op_lock.acquire(timeout=0.25)
    recorder._op_lock.release()
    unblock.set()
    _wait(stopped)
    assert stop_result == [True]
    _wait(second_done)
    assert second_result == [True]
    assert fake.open_indices == [0, 1]
    assert first.release_count == 1
    assert recorder.request_stop_async(
        owner_token=owner, request_token=second_request)


def test_replacement_links_active_predecessor_before_bounded_stop(
        recorder, monkeypatch):
    entered = threading.Event()
    unblock = threading.Event()
    before_stop = threading.Event()
    allow_stop = threading.Event()

    def blocked_read():
        entered.set()
        unblock.wait(5)
        return False, None

    first = _Capture(0, read=blocked_read)
    second = _Capture(1)
    fake = _Cv2([first, second])
    monkeypatch.setattr(camera_recorder, '_cv2', fake)
    owner = object()
    first_request = object()
    second_request = object()
    first_done = threading.Event()
    second_done = threading.Event()
    second_result = []
    recorder.start_async(
        0, lambda ok: first_done.set(), owner_token=owner,
        request_token=first_request)
    _wait(first_done)
    _wait(entered)

    original_stop = recorder._stop_locked

    def paused_stop(*args, **kwargs):
        before_stop.set()
        assert allow_stop.wait(2)
        return original_stop(*args, **kwargs)

    monkeypatch.setattr(recorder, '_stop_locked', paused_stop)
    recorder.start_async(
        1, lambda ok: (second_result.append(ok), second_done.set()),
        owner_token=owner, request_token=second_request)
    _wait(before_stop)
    assert fake.open_indices == [0]
    allow_stop.set()
    assert not second_done.wait(0.1)
    assert fake.open_indices == [0]
    unblock.set()
    _wait(second_done)
    assert second_result == [True]
    assert fake.open_indices == [0, 1]
    assert first.release_count == 1
    assert recorder.request_stop_async(
        owner_token=owner, request_token=second_request)


def test_retired_context_requires_exact_old_receipt(
        recorder, monkeypatch):
    entered = threading.Event()
    unblock = threading.Event()

    def blocked_read():
        entered.set()
        unblock.wait(5)
        return False, None

    capture = _Capture(0, read=blocked_read)
    fake = _Cv2([capture])
    monkeypatch.setattr(camera_recorder, '_cv2', fake)
    owner = object()
    request = object()
    opened = threading.Event()
    recorder.start_async(
        0, lambda ok: opened.set(), owner_token=owner,
        request_token=request)
    _wait(opened)
    _wait(entered)
    old = recorder.runtime_snapshot()

    recorder.stop()
    advanced = recorder.runtime_snapshot()
    assert advanced.generation == old.generation + 1
    assert recorder._retired_context is not None
    assert not recorder.is_running()

    assert not recorder.request_stop_async(
        owner_token=object(), request_token=request,
        expected_generation=old.generation, expected_device_index=0)
    assert not recorder.request_stop_async(
        owner_token=owner, request_token=request,
        expected_generation=advanced.generation, expected_device_index=0)
    assert capture.release_count == 0
    assert fake.open_indices == [0]
    assert recorder.runtime_snapshot().generation == advanced.generation

    result = []
    done = threading.Event()
    assert recorder.request_stop_async(
        owner_token=owner, request_token=request,
        expected_generation=old.generation, expected_device_index=0,
        on_result=lambda ok: (result.append(ok), done.set()))
    assert not done.wait(0.05)
    assert recorder.runtime_snapshot().generation == advanced.generation
    unblock.set()
    _wait(done)
    assert result == [True]
    assert capture.release_count == 1


@pytest.mark.parametrize('release_error, expected', [
    (None, True),
    (RuntimeError('inherited release failed'), False),
])
def test_cancelled_opening_cleanup_waits_for_inherited_predecessor(
        recorder, monkeypatch, release_error, expected):
    entered = threading.Event()
    unblock = threading.Event()

    def blocked_read():
        entered.set()
        unblock.wait(5)
        return False, None

    capture = _Capture(0, read=blocked_read, release_error=release_error)
    fake = _Cv2([capture])
    monkeypatch.setattr(camera_recorder, '_cv2', fake)
    owner = object()
    first_request = object()
    second_request = object()
    first_result = []
    second_open_result = []
    second_cleanup_result = []
    second_cleanup_after_first = []
    first_done = threading.Event()
    second_open_done = threading.Event()
    second_cleanup_done = threading.Event()
    recorder.start_async(
        0, lambda ok: (first_result.append(ok), first_done.set()),
        owner_token=owner, request_token=first_request)
    _wait(first_done)
    _wait(entered)

    recorder._op_lock.acquire()
    try:
        recorder.start_async(
            1, lambda ok: (second_open_result.append(ok),
                           second_open_done.set()),
            owner_token=owner, request_token=second_request)
        deadline = time.monotonic() + 2
        while (recorder._opening_context is None
               and time.monotonic() < deadline):
            time.sleep(0.001)
        assert recorder._opening_context is not None
        with recorder._lock:
            first_context = recorder._run_context
        assert first_context is not None
        first_stopped = threading.Event()
        assert recorder.request_stop_async(
            owner_token=owner, request_token=first_request,
            on_result=lambda ok: (first_result.append(ok),
                                  first_stopped.set()))
        assert recorder.request_stop_async(
            owner_token=owner, request_token=second_request,
            on_result=lambda ok: (second_cleanup_result.append(ok),
                                  second_cleanup_after_first.append(
                                      (first_context['cleanup_done'].is_set(),
                                       first_context.get('cleanup_ok'),
                                       not first_context['thread'].is_alive())),
                                  second_cleanup_done.set()))
    finally:
        recorder._op_lock.release()

    assert not second_cleanup_done.wait(0.1)
    assert fake.open_indices == [0]
    unblock.set()
    _wait(first_stopped)
    _wait(second_open_done)
    assert second_open_result == [False]
    _wait(second_cleanup_done)
    assert first_result == [True, expected]
    assert second_cleanup_result == [expected]
    assert second_cleanup_after_first == [(True, expected, True)]
    assert capture.release_count == 1


@pytest.mark.parametrize('release_error, release_result, expected', [
    (None, None, True),
    (RuntimeError('natural release failed'), None, False),
    (None, False, False),
])
def test_natural_owned_completion_retains_exact_receipt(
        recorder, monkeypatch, release_error, release_result, expected):
    released = threading.Event()

    def natural_read():
        capture.opened = False
        return False, None

    capture = _Capture(
        0,
        read=natural_read,
        release_error=release_error,
        release_result=release_result,
        on_release=released.set,
    )
    fake = _Cv2([capture])
    monkeypatch.setattr(camera_recorder, '_cv2', fake)
    owner = object()
    request = object()
    opened = threading.Event()
    open_result = []
    recorder.start_async(
        0, lambda ok: (open_result.append(ok), opened.set()),
        owner_token=owner, request_token=request)
    _wait(opened)
    assert open_result == [True]
    _wait(released)

    retired = _wait_for_natural_retired(recorder, request)
    assert retired is not None
    assert retired.get('natural_exit') is True
    assert retired.get('thread') is not None
    generation = retired['generation']
    if expected:
        assert not retired['cleanup_done'].is_set()
    else:
        assert retired['cleanup_done'].is_set()
        assert retired.get('cleanup_ok') is False
    assert fake.open_indices == [0]
    assert capture.release_count == 1

    foreign_result = []
    assert not recorder.request_stop_async(
        owner_token=object(), request_token=request,
        expected_generation=generation, expected_device_index=0,
        on_result=foreign_result.append)
    assert foreign_result == []
    assert capture.release_count == 1

    result = []
    done = threading.Event()
    joined_before_callback = []
    assert recorder.request_stop_async(
        owner_token=owner, request_token=request,
        expected_generation=generation, expected_device_index=0,
        on_result=lambda ok: (result.append(ok),
                              joined_before_callback.append(
                                  not retired['thread'].is_alive()),
                              done.set()))
    _wait(done)
    assert result == [expected]
    if expected:
        assert joined_before_callback == [True]
    assert capture.release_count == 1
    assert not any(thread.is_alive() for thread in recorder._cleanup_threads.values())

    repeat_result = []
    assert not recorder.request_stop_async(
        owner_token=owner, request_token=request,
        expected_generation=generation, expected_device_index=0,
        on_result=repeat_result.append)
    assert repeat_result == []
    assert capture.release_count == 1


def test_next_owned_start_reaps_natural_completion(
        recorder, monkeypatch):
    first_released = threading.Event()

    def natural_read():
        first.opened = False
        return False, None

    first = _Capture(0, read=natural_read, on_release=first_released.set)
    second = _Capture(0)
    fake = _Cv2([first, second])
    monkeypatch.setattr(camera_recorder, '_cv2', fake)
    owner = object()
    first_request = object()
    second_request = object()
    first_done = threading.Event()
    second_done = threading.Event()
    recorder.start_async(
        0, lambda ok: first_done.set(), owner_token=owner,
        request_token=first_request)
    _wait(first_done)
    _wait(first_released)
    _wait_for_natural_retired(recorder, first_request)

    recorder.start_async(
        0, lambda ok: second_done.set(), owner_token=owner,
        request_token=second_request)
    _wait(second_done)
    assert fake.open_indices == [0, 0]
    assert first.release_count == 1
    assert recorder.request_stop_async(
        owner_token=owner, request_token=second_request)


@pytest.mark.parametrize(
    'opened, opened_error, release_error, release_result', [
    (False, None, RuntimeError('release failed'), None),
    (True, RuntimeError('isOpened failed'), RuntimeError('release failed'),
     None),
    (False, None, None, False),
])
def test_open_failure_with_release_failure_blocks_followup(
        recorder, monkeypatch, opened, opened_error, release_error,
        release_result):
    capture = _Capture(
        0,
        opened=opened,
        opened_error=opened_error,
        release_error=release_error,
        release_result=release_result,
    )
    fake = _Cv2([capture])
    monkeypatch.setattr(camera_recorder, '_cv2', fake)
    owner = object()
    first_request = object()
    first_result = []
    first_done = threading.Event()
    recorder.start_async(
        0, lambda ok: (first_result.append(ok), first_done.set()),
        owner_token=owner, request_token=first_request)
    _wait(first_done)
    assert first_result == [False]
    assert fake.open_indices == [0]
    assert capture.release_count == 1
    retained = list(recorder._pending_cleanup.values())
    assert any(context.get('cleanup_ok') is False
               and context.get('blocks_successors')
               and context.get('release_error') is not None
               for context in retained)

    second_result = []
    second_done = threading.Event()
    recorder.start_async(
        0, lambda ok: (second_result.append(ok), second_done.set()),
        owner_token=owner, request_token=object())
    _wait(second_done)
    assert second_result == [False]
    assert fake.open_indices == [0]
    assert capture.release_count == 1


def test_rapid_same_owner_chain_opens_only_latest_after_cleanup(
        recorder, monkeypatch):
    first_entered = threading.Event()
    first_unblock = threading.Event()
    first_released = threading.Event()
    second_open_seen = threading.Event()
    captures = [
        _Capture(0, on_release=first_released.set),
        _Capture(0),
    ]

    def open_capture(index):
        call_number = len(fake.open_indices)
        fake.open_indices.append(index)
        if call_number == 0:
            first_entered.set()
            first_unblock.wait(2)
        else:
            if first_released.is_set():
                second_open_seen.set()
        return captures[call_number]

    fake = _Cv2([])
    fake.VideoCapture = open_capture
    monkeypatch.setattr(camera_recorder, '_cv2', fake)
    owner = object()
    requests = [object(), object(), object()]
    results = [[] for _ in requests]
    done = [threading.Event() for _ in requests]
    recorder.start_async(
        0, lambda ok: (results[0].append(ok), done[0].set()),
        owner_token=owner, request_token=requests[0])
    _wait(first_entered)
    recorder.start_async(
        0, lambda ok: (results[1].append(ok), done[1].set()),
        owner_token=owner, request_token=requests[1])
    recorder.start_async(
        0, lambda ok: (results[2].append(ok), done[2].set()),
        owner_token=owner, request_token=requests[2])
    first_unblock.set()
    for event in done:
        _wait(event)
    _wait(second_open_seen)
    assert results == [[False], [False], [True]]
    assert fake.open_indices == [0, 0]
    assert first_released.is_set()
    assert recorder.request_stop_async(
        owner_token=owner, request_token=requests[2])


def test_owned_open_cancellation_releases_blocked_backend_once(
        recorder, monkeypatch):
    entered = threading.Event()
    unblock = threading.Event()

    def open_capture(index):
        entered.set()
        unblock.wait(2)
        return capture

    capture = _Capture(0)
    fake = _Cv2([])
    fake.VideoCapture = open_capture
    monkeypatch.setattr(camera_recorder, '_cv2', fake)
    owner = object()
    request = object()
    result = []
    done = threading.Event()
    recorder.start_async(
        0, lambda ok: (result.append(ok), done.set()),
        owner_token=owner, request_token=request)
    _wait(entered)
    started = time.monotonic()
    assert recorder.request_stop_async(
        owner_token=owner, request_token=request,
        expected_device_index=0)
    assert time.monotonic() - started < 0.25
    unblock.set()
    _wait(done)
    assert result == [False]
    assert capture.release_count == 1
    assert not recorder.is_running()
    assert recorder._thread is None


def test_owned_stop_invalidates_blocked_read_before_late_publication(
        recorder, monkeypatch):
    entered = threading.Event()
    unblock = threading.Event()

    def blocked_read():
        entered.set()
        unblock.wait(2)
        return True, object()

    capture = _Capture(0, read=blocked_read)
    monkeypatch.setattr(camera_recorder, '_cv2', _Cv2([capture]))
    owner = object()
    request = object()
    started = threading.Event()
    recorder.start_async(
        0, lambda ok: started.set(),
        owner_token=owner, request_token=request)
    _wait(started)
    _wait(entered)
    snapshot = recorder.runtime_snapshot()
    result = []
    done = threading.Event()
    assert recorder.request_stop_async(
        owner_token=owner, request_token=request,
        expected_generation=snapshot.generation,
        expected_device_index=0,
        on_result=lambda ok: (result.append(ok), done.set()))
    assert not recorder.is_running()
    assert recorder.latest_frame is None
    assert not recorder._chunks
    assert not done.wait(0.05)
    unblock.set()
    _wait(done)
    assert result == [True]
    assert capture.release_count == 1
    assert not recorder.is_running()


def test_owned_stop_during_blocked_encode_drops_late_chunk(
        recorder, monkeypatch):
    encoding = threading.Event()
    finish = threading.Event()
    frame = object()

    capture = _Capture(0, read=lambda: (True, frame))
    fake = _Cv2([capture])

    def blocked_encode(_ext, _frame, _options):
        encoding.set()
        finish.wait(2)
        return True, b'late-jpeg'

    fake.imencode = blocked_encode
    monkeypatch.setattr(camera_recorder, '_cv2', fake)
    owner = object()
    request = object()
    opened = threading.Event()
    recorder.start_async(
        0, lambda ok: opened.set(), owner_token=owner,
        request_token=request)
    _wait(opened)
    _wait(encoding)
    snapshot = recorder.runtime_snapshot()
    stopped = threading.Event()
    assert recorder.request_stop_async(
        owner_token=owner, request_token=request,
        expected_generation=snapshot.generation,
        on_result=lambda ok: stopped.set())
    assert recorder.latest_frame is None
    assert not recorder._chunks
    assert not stopped.wait(0.05)
    finish.set()
    _wait(stopped)
    assert capture.release_count == 1
    assert not recorder._chunks


def test_pending_cleanup_blocks_successor_and_stale_stop_cannot_touch_it(
        recorder, monkeypatch):
    entered = threading.Event()
    unblock = threading.Event()

    def blocked_read():
        entered.set()
        unblock.wait(2)
        return False, None

    first = _Capture(0, read=blocked_read)
    second = _Capture(1, read=lambda: (False, None))
    fake = _Cv2([first, second])
    monkeypatch.setattr(camera_recorder, '_cv2', fake)
    owner = object()
    first_request = object()
    second_request = object()
    opened = threading.Event()
    recorder.start_async(
        0, lambda ok: opened.set(), owner_token=owner,
        request_token=first_request)
    _wait(opened)
    _wait(entered)
    snapshot = recorder.runtime_snapshot()
    stopped = threading.Event()
    assert recorder.request_stop_async(
        owner_token=owner, request_token=first_request,
        expected_generation=snapshot.generation,
        on_result=lambda ok: stopped.set())
    assert not recorder.start(1, owner_token=owner,
                              request_token=second_request)
    unblock.set()
    _wait(stopped)
    second_opened = threading.Event()
    recorder.start_async(
        1, lambda ok: second_opened.set(), owner_token=owner,
        request_token=second_request)
    _wait(second_opened)
    assert fake.open_indices == [0, 1]
    assert not recorder.request_stop_async(
        owner_token=owner, request_token=first_request)
    assert recorder.request_stop_async(
        owner_token=owner, request_token=second_request)


def test_stop_rejects_bool_and_float_expected_receipts(
        recorder, monkeypatch):
    capture = _Capture(0)
    monkeypatch.setattr(camera_recorder, '_cv2', _Cv2([capture]))
    owner = object()
    request = object()
    opened = threading.Event()
    recorder.start_async(
        0, lambda ok: opened.set(), owner_token=owner,
        request_token=request)
    _wait(opened)
    snapshot = recorder.runtime_snapshot()
    assert not recorder.request_stop_async(
        owner_token=owner, request_token=request,
        expected_generation=True)
    assert not recorder.request_stop_async(
        owner_token=owner, request_token=request,
        expected_device_index=float(snapshot.device_index))
    assert recorder.is_running()
    assert recorder.request_stop_async(owner_token=owner, request_token=request)


def test_same_device_owned_reuse_keeps_generation_and_adopts_request(
        recorder, monkeypatch):
    capture = _Capture(0)
    fake = _Cv2([capture])
    monkeypatch.setattr(camera_recorder, '_cv2', fake)
    owner = object()
    first = object()
    second = object()
    first_done = threading.Event()
    recorder.start_async(
        0, lambda ok: first_done.set(), owner_token=owner,
        request_token=first)
    _wait(first_done)
    before = recorder.runtime_snapshot()
    second_done = threading.Event()
    recorder.start_async(
        0, lambda ok: second_done.set(), owner_token=owner,
        request_token=second)
    _wait(second_done)
    after = recorder.runtime_snapshot()
    assert fake.open_indices == [0]
    assert after.generation == before.generation
    assert recorder._run_context['request_token'] is second
    assert not recorder.request_stop_async(
        owner_token=owner, request_token=first)
    assert recorder.request_stop_async(owner_token=owner, request_token=second)


@pytest.mark.parametrize('field, value', [
    ('owner_token', object()),
    ('request_token', object()),
    ('expected_generation', 999999),
    ('expected_device_index', 99),
])
def test_stale_owned_stop_never_touches_current_run(
        recorder, monkeypatch, field, value):
    capture = _Capture(0)
    monkeypatch.setattr(camera_recorder, '_cv2', _Cv2([capture]))
    owner = object()
    request = object()
    opened = threading.Event()
    recorder.start_async(
        0, lambda ok: opened.set(), owner_token=owner,
        request_token=request)
    _wait(opened)
    kwargs = {'owner_token': owner, 'request_token': request}
    if field == 'owner_token':
        kwargs[field] = value
    elif field == 'request_token':
        kwargs[field] = value
    else:
        kwargs[field] = value
    assert not recorder.request_stop_async(**kwargs)
    assert recorder.is_running()
    assert recorder.request_stop_async(owner_token=owner, request_token=request)


def test_repeated_owned_stop_queues_one_cleanup_and_one_callback(
        recorder, monkeypatch):
    entered = threading.Event()
    unblock = threading.Event()

    def blocked_read():
        entered.set()
        unblock.wait(2)
        return False, None

    capture = _Capture(0, read=blocked_read)
    monkeypatch.setattr(camera_recorder, '_cv2', _Cv2([capture]))
    owner = object()
    request = object()
    opened = threading.Event()
    recorder.start_async(
        0, lambda ok: opened.set(), owner_token=owner,
        request_token=request)
    _wait(opened)
    _wait(entered)
    callbacks = []
    done = threading.Event()
    assert recorder.request_stop_async(
        owner_token=owner, request_token=request,
        on_result=lambda ok: (callbacks.append(ok), done.set()))
    assert not recorder.request_stop_async(
        owner_token=owner, request_token=request,
        on_result=lambda ok: callbacks.append(ok))
    assert len(recorder._cleanup_threads) == 1
    unblock.set()
    _wait(done)
    assert callbacks == [True]
    assert capture.release_count == 1
    assert not recorder._cleanup_threads


def test_owned_cleanup_complete_is_identity_bound_and_readonly(
        recorder, monkeypatch):
    assert recorder.owned_cleanup_complete(owner_token=object())
    assert not recorder.owned_cleanup_complete(owner_token=None)

    class _Poison:
        def __eq__(self, _other):
            raise AssertionError('owner tokens must use identity')

    capture = _Capture(0, read=lambda: (False, None))
    monkeypatch.setattr(camera_recorder, '_cv2', _Cv2([capture]))
    foreign_owner = object()
    owner = object()
    opened = threading.Event()
    recorder.start_async(
        0, lambda ok: opened.set(), owner_token=foreign_owner,
        request_token=object())
    _wait(opened)

    assert recorder.is_running()
    assert recorder.owned_cleanup_complete(owner_token=owner)
    assert recorder.owned_cleanup_complete(owner_token=_Poison())
    assert not recorder.owned_cleanup_complete(owner_token=None)
    assert recorder.is_running()

    stopped = threading.Event()
    assert recorder.request_stop_async(
        owner_token=foreign_owner,
        on_result=lambda ok: stopped.set())
    _wait(stopped)


@pytest.mark.parametrize('release_error, release_result, expected', [
    (None, None, True),
    (RuntimeError('failed release'), None, False),
    (None, False, False),
])
def test_owned_cleanup_complete_reports_clean_failed_open(
        recorder, monkeypatch, release_error, release_result, expected):
    capture = _Capture(
        0,
        opened=False,
        release_error=release_error,
        release_result=release_result,
    )
    monkeypatch.setattr(camera_recorder, '_cv2', _Cv2([capture]))
    owner = object()
    result = []
    done = threading.Event()
    recorder.start_async(
        0, lambda ok: (result.append(ok), done.set()), owner_token=owner,
        request_token=object())
    _wait(done)

    assert result == [False]
    assert recorder.owned_cleanup_complete(owner_token=owner) is expected
    assert capture.release_count == 1


def test_owned_cleanup_complete_waits_for_queued_open_cleanup(
        recorder, monkeypatch):
    monkeypatch.setattr(camera_recorder, '_cv2', _Cv2([]))
    owner = object()
    request = object()
    open_done = threading.Event()
    cleanup_done = threading.Event()

    recorder._op_lock.acquire()
    try:
        recorder.start_async(
            0, lambda ok: open_done.set(), owner_token=owner,
            request_token=request)
        deadline = time.monotonic() + 2
        while (recorder._opening_context is None
               and time.monotonic() < deadline):
            time.sleep(0.001)
        assert recorder._opening_context is not None
        assert not recorder.owned_cleanup_complete(owner_token=owner)
        assert recorder.request_stop_async(
            owner_token=owner, request_token=request,
            on_result=lambda ok: cleanup_done.set())
        assert not recorder.owned_cleanup_complete(owner_token=owner)
    finally:
        recorder._op_lock.release()

    _wait(open_done)
    _wait(cleanup_done)
    assert recorder.owned_cleanup_complete(owner_token=owner)


def test_owned_cleanup_complete_keeps_active_predecessor_visible(
        recorder, monkeypatch):
    first = _Capture(0, read=lambda: (False, None))
    monkeypatch.setattr(camera_recorder, '_cv2', _Cv2([first]))
    owner = object()
    first_request = object()
    second_request = object()
    first_opened = threading.Event()
    second_opened = threading.Event()
    second_cleaned = threading.Event()

    recorder.start_async(
        0, lambda ok: first_opened.set(), owner_token=owner,
        request_token=first_request)
    _wait(first_opened)
    recorder._op_lock.acquire()
    try:
        recorder.start_async(
            1, lambda ok: second_opened.set(), owner_token=owner,
            request_token=second_request)
        deadline = time.monotonic() + 2
        while (recorder._opening_context is None
               and time.monotonic() < deadline):
            time.sleep(0.001)
        assert recorder._opening_context is not None
        assert recorder.request_stop_async(
            owner_token=owner, request_token=second_request,
            on_result=lambda ok: second_cleaned.set())
        assert not recorder.owned_cleanup_complete(owner_token=owner)
    finally:
        recorder._op_lock.release()

    _wait(second_opened)
    _wait(second_cleaned)
    assert recorder.is_running()
    assert recorder.owned_cleanup_complete(owner_token=owner) is False
    assert recorder.request_stop_async(
        owner_token=owner, request_token=first_request)
    deadline = time.monotonic() + 2
    while (recorder.owned_cleanup_complete(owner_token=owner) is False
           and time.monotonic() < deadline):
        time.sleep(0.001)
    assert recorder.owned_cleanup_complete(owner_token=owner)
    assert first.release_count == 1


def test_owned_cleanup_complete_stays_false_until_adopted_run_stops(
        recorder, monkeypatch):
    capture = _Capture(0, read=lambda: (False, None))
    fake = _Cv2([capture])
    monkeypatch.setattr(camera_recorder, '_cv2', fake)
    owner = object()
    first_request = object()
    second_request = object()
    first_opened = threading.Event()
    second_opened = threading.Event()
    recorder.start_async(
        0, lambda ok: first_opened.set(), owner_token=owner,
        request_token=first_request)
    _wait(first_opened)
    recorder.start_async(
        0, lambda ok: second_opened.set(), owner_token=owner,
        request_token=second_request)
    _wait(second_opened)

    assert fake.open_indices == [0]
    assert not recorder.owned_cleanup_complete(owner_token=owner)
    stopped = threading.Event()
    assert recorder.request_stop_async(
        owner_token=owner, request_token=second_request,
        on_result=lambda ok: stopped.set())
    _wait(stopped)
    assert recorder.owned_cleanup_complete(owner_token=owner)
    assert capture.release_count == 1


def test_owned_cleanup_complete_requires_external_natural_retirement_cleanup(
        recorder, monkeypatch):
    released = threading.Event()

    def natural_read():
        capture.opened = False
        return False, None

    capture = _Capture(0, read=natural_read, on_release=released.set)
    monkeypatch.setattr(camera_recorder, '_cv2', _Cv2([capture]))
    owner = object()
    request = object()
    opened = threading.Event()
    recorder.start_async(
        0, lambda ok: opened.set(), owner_token=owner,
        request_token=request)
    _wait(opened)
    _wait(released)
    retired = _wait_for_natural_retired(recorder, request)

    assert retired['cleanup_done'].is_set() is False
    assert not recorder.owned_cleanup_complete(owner_token=owner)
    stopped = threading.Event()
    assert recorder.request_stop_async(
        owner_token=owner, request_token=request,
        on_result=lambda ok: stopped.set())
    _wait(stopped)
    assert recorder.owned_cleanup_complete(owner_token=owner)
    assert capture.release_count == 1


@pytest.mark.parametrize('release_error, expected', [
    (None, True),
    (RuntimeError('blocked release failed'), False),
])
def test_thread_start_failure_retains_context_until_worker_cleanup(
        recorder, monkeypatch, release_error, expected):
    read_entered = threading.Event()
    read_unblock = threading.Event()
    release_entered = threading.Event()
    release_unblock = threading.Event()
    capture = _Capture(0)

    def blocked_read():
        read_entered.set()
        read_unblock.wait(2)
        return False, None

    def blocked_release():
        capture.release_count += 1
        release_entered.set()
        release_unblock.wait(2)
        if release_error is not None:
            raise release_error
        return None

    capture.read_fn = blocked_read
    capture.release = blocked_release
    original_thread = camera_recorder.threading.Thread
    worker_threads = []

    class _StartFailureThread:
        def __init__(self, *args, **kwargs):
            self._thread = original_thread(*args, **kwargs)
            worker_threads.append(self)

        def start(self):
            self._thread.start()
            assert read_entered.wait(2)
            raise RuntimeError('synthetic capture thread start failure')

        def join(self, timeout=None):
            if timeout is None:
                return self._thread.join()
            return self._thread.join(timeout)

        def is_alive(self):
            return self._thread.is_alive()

    def thread_factory(*args, **kwargs):
        if kwargs.get('name') == 'fthr-camera':
            return _StartFailureThread(*args, **kwargs)
        return original_thread(*args, **kwargs)

    monkeypatch.setattr(camera_recorder.threading, 'Thread', thread_factory)
    monkeypatch.setattr(camera_recorder, '_cv2', _Cv2([capture]))
    owner = object()
    opened = threading.Event()
    recorder.start_async(
        0, lambda ok: opened.set(), owner_token=owner,
        request_token=object())
    _wait(opened)
    assert worker_threads
    assert not recorder.owned_cleanup_complete(owner_token=owner)
    with recorder._lock:
        failed_context = next(
            context for context in recorder._pending_cleanup.values()
            if context.get('owner_token') is owner)
    cleanup_done = failed_context['cleanup_done']

    read_unblock.set()
    _wait(release_entered)
    assert not recorder.owned_cleanup_complete(owner_token=owner)
    release_unblock.set()
    _wait(cleanup_done)
    worker_threads[0].join(timeout=2)
    assert failed_context.get('cleanup_ok') is expected
    assert recorder.owned_cleanup_complete(owner_token=owner) is expected
    assert not worker_threads[0].is_alive()
    assert capture.release_count == 1


def test_thread_start_failure_without_liveness_proof_stays_pending(
        recorder, monkeypatch):
    capture = _Capture(0)
    original_thread = camera_recorder.threading.Thread

    class _UnknownThread:
        def __init__(self, *args, **kwargs):
            self._thread = original_thread(*args, **kwargs)

        def start(self):
            raise RuntimeError('synthetic unproved start failure')

        def join(self, timeout=None):
            raise RuntimeError('worker start state is unknown')

    def thread_factory(*args, **kwargs):
        if kwargs.get('name') == 'fthr-camera':
            return _UnknownThread(*args, **kwargs)
        return original_thread(*args, **kwargs)

    monkeypatch.setattr(camera_recorder.threading, 'Thread', thread_factory)
    monkeypatch.setattr(camera_recorder, '_cv2', _Cv2([capture]))
    owner = object()
    opened = threading.Event()
    recorder.start_async(
        0, lambda ok: opened.set(), owner_token=owner,
        request_token=object())
    _wait(opened)

    classified = None
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with recorder._lock:
            for context in recorder._pending_cleanup.values():
                if (context.get('owner_token') is owner
                        and context.get('cleanup_done').is_set()
                        and context.get('phase') == 'cleanup_failed'):
                    classified = context
                    break
        if classified is not None:
            break
        time.sleep(0.001)
    assert classified is not None
    assert classified.get('cleanup_ok') is False
    assert not recorder.owned_cleanup_complete(owner_token=owner)
    assert capture.release_count == 0
