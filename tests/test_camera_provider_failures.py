from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from FTHR_UI.core import camera_recorder


class _Capture:
    def __init__(self, index, *, opened=True, read=None, release=None):
        self.index = index
        self.opened = opened
        self.read_fn = read
        self.release_fn = release
        self.release_count = 0
        self.read_active = threading.Event()
        self.read_threads = []
        self.read_count = 0

    def isOpened(self):
        return self.opened

    def read(self):
        self.read_active.set()
        self.read_threads.append(threading.get_ident())
        self.read_count += 1
        if self.read_fn:
            return self.read_fn()
        return False, None

    def release(self):
        self.release_count += 1
        if self.release_fn:
            self.release_fn()


class _CV2:
    IMWRITE_JPEG_QUALITY = 1

    def __init__(self, captures, *, encode=None):
        self.captures = iter(captures)
        self.encode = encode or (lambda _ext, _frame, _options: (True, b'jpeg'))
        self.open_indices = []

    def VideoCapture(self, index):
        self.open_indices.append(index)
        capture = next(self.captures)
        assert capture.index == index
        return capture

    def imencode(self, ext, frame, options):
        return self.encode(ext, frame, options)


@pytest.fixture
def recorder(monkeypatch):
    monkeypatch.setattr(camera_recorder, '_AVAILABLE', True)
    original_thread = threading.Thread
    owned_threads = []

    class _TrackedThread(original_thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            owned_threads.append(self)

    monkeypatch.setattr(camera_recorder, 'threading', SimpleNamespace(
        Thread=_TrackedThread,
        Event=threading.Event,
        Lock=threading.Lock,
        current_thread=threading.current_thread,
    ))
    camera_recorder.CameraRecorder._instance = None
    instance = camera_recorder.CameraRecorder()
    instance._test_owned_threads = owned_threads
    yield instance
    instance.stop()
    for thread in owned_threads:
        if thread.ident is not None and thread is not threading.current_thread():
            thread.join(timeout=3)
    assert all(not thread.is_alive() for thread in owned_threads)
    camera_recorder.CameraRecorder._instance = None


def _patch_cv2(monkeypatch, *captures, encode=None):
    fake = _CV2(captures, encode=encode)
    monkeypatch.setattr(camera_recorder, '_cv2', fake)
    return fake


def test_start_async_reports_open_provider_exception_once(recorder, monkeypatch):
    def fail_open(_index):
        raise RuntimeError('provider open failed')

    fake = _CV2([])
    fake.VideoCapture = fail_open
    monkeypatch.setattr(camera_recorder, '_cv2', fake)
    results = []
    done = threading.Event()

    recorder.start_async(3, lambda ok: (results.append(ok), done.set()))

    assert done.wait(2)
    assert results == [False]
    assert not recorder.is_running()


def test_start_async_reports_open_worker_start_failure_once(recorder, monkeypatch):
    monkeypatch.setattr(camera_recorder.threading.Thread, 'start',
                        lambda _self: (_ for _ in ()).throw(RuntimeError('thread failed')))
    results = []

    recorder.start_async(0, results.append)

    assert results == [False]


@pytest.mark.parametrize('failure_point', ['isOpened', 'thread_constructor', 'thread_start'])
def test_failed_start_cleans_capture_and_returns_false(
        recorder, monkeypatch, failure_point):
    cap = _Capture(0)
    _patch_cv2(monkeypatch, cap)
    if failure_point == 'isOpened':
        cap.isOpened = lambda: (_ for _ in ()).throw(RuntimeError('probe failed'))
    elif failure_point == 'thread_constructor':
        original_thread = camera_recorder.threading.Thread

        def fail_capture_constructor(*args, **kwargs):
            if kwargs.get('name') == 'fthr-camera':
                raise RuntimeError('thread constructor failed')
            return original_thread(*args, **kwargs)

        monkeypatch.setattr(camera_recorder.threading, 'Thread', fail_capture_constructor)
    else:
        monkeypatch.setattr(camera_recorder.threading.Thread, 'start',
                            lambda _self: (_ for _ in ()).throw(RuntimeError('thread failed')))

    assert recorder.start(0) is False
    assert cap.release_count == 1
    assert not recorder.is_running()


@pytest.mark.parametrize('failure_point', ['isOpened', 'read', 'imencode'])
def test_capture_worker_failure_clears_state_and_releases_owned_capture(
        recorder, monkeypatch, failure_point):
    frame = object()
    cap = _Capture(0, read=lambda: (True, frame))
    fresh = _Capture(0, read=lambda: (False, None))
    encode = None
    if failure_point == 'isOpened':
        calls = 0

        def fail_worker_probe():
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RuntimeError('worker probe failed')
            return True

        cap.isOpened = fail_worker_probe
    elif failure_point == 'read':
        cap.read_fn = lambda: (_ for _ in ()).throw(RuntimeError('read failed'))
    elif failure_point == 'imencode':
        encode = lambda *_args: (_ for _ in ()).throw(RuntimeError('encode failed'))
    _patch_cv2(monkeypatch, cap, fresh, encode=encode)

    assert recorder.start(0)
    deadline = time.monotonic() + 2
    while recorder.is_running() and time.monotonic() < deadline:
        time.sleep(.005)

    assert not recorder.is_running()
    assert recorder.latest_frame is None
    assert not recorder._chunks
    assert cap.release_count == 1
    assert recorder.start(0) is True
    recorder.stop()
    assert fresh.release_count == 1


def test_closed_capture_is_released_and_recorder_can_restart(recorder, monkeypatch):
    closed = _Capture(0, opened=False)
    fresh = _Capture(0)
    _patch_cv2(monkeypatch, closed, fresh)

    assert recorder.start(0) is False
    assert closed.release_count == 1
    assert not recorder.is_running()
    assert recorder.start(0) is True
    recorder.stop()
    assert fresh.release_count == 1


def test_stop_does_not_release_blocked_old_capture_or_publish_into_new_run(
        recorder, monkeypatch):
    entered = threading.Event()
    unblock = threading.Event()
    old_frame = object()

    def old_read():
        entered.set()
        unblock.wait()
        return True, old_frame

    old_cap = _Capture(0, read=old_read)
    new_frame = object()

    def new_read():
        if new_cap.read_count == 1:
            return True, new_frame
        return False, None

    new_cap = _Capture(1, read=new_read)
    encoded = []
    fake = _patch_cv2(monkeypatch, old_cap, new_cap,
                      encode=lambda _ext, frame, _opts: (encoded.append(frame) is None, b'jpeg'))

    try:
        assert recorder.start(0)
        assert entered.wait(2)
        recorder.stop()
        assert old_cap.release_count == 0
        assert recorder.start(1) is False
        assert fake.open_indices == [0]
        retired_thread = recorder._retired_thread
        assert retired_thread is not None and retired_thread.is_alive()
        unblock.set()
        retired_thread.join(timeout=3)
        assert not retired_thread.is_alive()
        assert old_cap.release_count == 1
        assert old_frame not in encoded
        assert recorder.start(1)
        assert fake.open_indices == [0, 1]
        assert recorder._thread is not None and recorder._thread.is_alive()
        new_worker_id = recorder._thread.ident
        deadline = time.monotonic() + 2
        while new_frame not in encoded and time.monotonic() < deadline:
            time.sleep(.005)
        assert new_frame in encoded
        assert new_cap.read_threads
        assert set(new_cap.read_threads) == {new_worker_id}
        assert recorder.latest_frame is new_frame
        assert recorder.is_running()
    finally:
        unblock.set()
        recorder.stop()


def test_stop_clears_frame_and_chunks_without_stale_worker_repopulation(
        recorder, monkeypatch):
    entered = threading.Event()
    unblock = threading.Event()

    def blocked_read():
        entered.set()
        unblock.wait()
        return True, object()

    cap = _Capture(0, read=blocked_read)
    _patch_cv2(monkeypatch, cap)
    try:
        assert recorder.start(0)
        assert entered.wait(2)
        recorder._latest_frame = object()
        recorder._chunks.append((time.monotonic(), b'old'))
        recorder.stop()
        assert recorder.latest_frame is None
        assert not recorder._chunks
        unblock.set()
        deadline = time.monotonic() + 2
        while cap.release_count == 0 and time.monotonic() < deadline:
            time.sleep(.005)
        assert cap.release_count == 1
        assert recorder.latest_frame is None
        assert not recorder._chunks
    finally:
        unblock.set()
        recorder.stop()


def test_raw_preview_survives_failed_jpeg_encode(recorder, monkeypatch):
    frame = object()
    cap = _Capture(0, read=lambda: (True, frame))
    _patch_cv2(monkeypatch, cap, encode=lambda *_args: (False, None))
    try:
        assert recorder.start(0)
        deadline = time.monotonic() + 2
        while recorder.latest_frame is None and time.monotonic() < deadline:
            time.sleep(.005)
        assert recorder.latest_frame is frame
        assert not recorder._chunks
    finally:
        recorder.stop()


def test_chunk_timestamp_is_sampled_before_jpeg_encoding(recorder, monkeypatch):
    frame = object()
    read_count = 0

    def read_once():
        nonlocal read_count
        read_count += 1
        return (True, frame) if read_count == 1 else (False, None)

    cap = _Capture(0, read=read_once)
    clock = [100.0]

    def encode(_ext, _frame, _options):
        clock[0] = 200.0
        return True, b'jpeg'

    _patch_cv2(monkeypatch, cap, encode=encode)
    monkeypatch.setattr(camera_recorder, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], sleep=time.sleep))
    try:
        assert recorder.start(0)
        deadline = time.monotonic() + 2
        while not recorder._chunks and time.monotonic() < deadline:
            time.sleep(.005)
        assert recorder._chunks
        assert recorder._chunks[0][0] == 100.0
    finally:
        recorder.stop()


def test_stop_during_encoding_cancels_jpeg_publication(recorder, monkeypatch):
    frame = object()
    encoding = threading.Event()
    finish_encoding = threading.Event()

    def encode(*_args):
        encoding.set()
        finish_encoding.wait()
        return True, b'jpeg'

    cap = _Capture(0, read=lambda: (True, frame))
    _patch_cv2(monkeypatch, cap, encode=encode)
    stopper = None
    try:
        assert recorder.start(0)
        assert encoding.wait(2)
        cancel_event = recorder._stop_event
        stopper = threading.Thread(target=recorder.stop)
        stopper.start()
        assert cancel_event.wait(2) or cancel_event.is_set()
        assert recorder.latest_frame is None
        assert not recorder._chunks
        finish_encoding.set()
        stopper.join(timeout=3)
        assert not stopper.is_alive()
        assert not recorder._chunks
        assert recorder.latest_frame is None
    finally:
        finish_encoding.set()
        if stopper and stopper.is_alive():
            stopper.join(timeout=3)
        recorder.stop()


def test_callback_exception_is_logged_and_does_not_change_open_state(
        recorder, monkeypatch, capsys):
    cap = _Capture(0, read=lambda: (False, None))
    _patch_cv2(monkeypatch, cap)
    called = threading.Event()

    def fail_callback(_ok):
        called.set()
        raise RuntimeError('callback failed')

    try:
        recorder.start_async(0, fail_callback)
        assert called.wait(2)
        opener = next(thread for thread in recorder._test_owned_threads
                      if thread.name == 'fthr-camera-open')
        opener.join(timeout=3)
        assert not opener.is_alive()
        deadline = time.monotonic() + 2
        while not recorder.is_running() and time.monotonic() < deadline:
            time.sleep(.005)
        assert recorder.is_running()
        output = capsys.readouterr().out
        assert 'Open-result callback failed: RuntimeError: callback failed' in output
    finally:
        recorder.stop()


def test_worker_and_release_failures_log_bounded_text_and_release_once(
        recorder, monkeypatch, capsys):
    def fail_read():
        raise RuntimeError('r' * 10000)

    def fail_release():
        raise ValueError('s' * 10000)

    cap = _Capture(0, read=fail_read, release=fail_release)
    _patch_cv2(monkeypatch, cap)
    assert recorder.start(0)
    deadline = time.monotonic() + 2
    while recorder.is_running() and time.monotonic() < deadline:
        time.sleep(.005)
    recorder.stop()

    output = capsys.readouterr().out
    assert 'Capture worker failed: RuntimeError:' in output
    assert 'Capture release failed: ValueError:' in output
    assert len(output) < 1400
    assert cap.release_count == 1


def test_worker_exits_and_releases_when_device_closes(recorder, monkeypatch):
    cap = _Capture(0, read=lambda: (False, None))
    calls = 0

    def closes_after_open_probe():
        nonlocal calls
        calls += 1
        return calls == 1

    cap.isOpened = closes_after_open_probe
    _patch_cv2(monkeypatch, cap)
    assert recorder.start(0)
    deadline = time.monotonic() + 2
    while recorder.is_running() and time.monotonic() < deadline:
        time.sleep(.005)

    assert not recorder.is_running()
    assert cap.release_count == 1
    assert recorder.latest_frame is None
