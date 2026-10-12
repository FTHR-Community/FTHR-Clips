from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from core import camera_recorder


class _Capture:
    def __init__(self, index, *, opened=True, read=None, release=None):
        self.index = index
        self.opened = opened
        self.read_fn = read or (lambda: (False, None))
        self.release_fn = release
        self.read_count = 0
        self.read_threads = []
        self.release_count = 0

    def isOpened(self):
        return self.opened

    def read(self):
        self.read_count += 1
        self.read_threads.append(threading.get_ident())
        return self.read_fn()

    def release(self):
        self.release_count += 1
        if self.release_fn:
            self.release_fn()


class _CV2:
    IMWRITE_JPEG_QUALITY = 1

    def __init__(self, captures, encode=None):
        self.captures = iter(captures)
        self.opened_indices = []
        self.encode = encode or (lambda *_args: (True, b'jpeg'))

    def VideoCapture(self, index):
        self.opened_indices.append(index)
        cap = next(self.captures)
        assert cap.index == index
        return cap

    def imencode(self, *args):
        return self.encode(*args)


@pytest.fixture
def recorder(monkeypatch):
    real_thread = threading.Thread
    owned_threads = []

    class TrackedThread(real_thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            owned_threads.append(self)

    monkeypatch.setattr(camera_recorder, '_AVAILABLE', True)
    monkeypatch.setattr(camera_recorder, 'threading', SimpleNamespace(
        Thread=TrackedThread,
        Event=threading.Event,
        Lock=threading.Lock,
        current_thread=threading.current_thread,
    ))
    camera_recorder.CameraRecorder._instance = None
    instance = camera_recorder.CameraRecorder()
    instance._test_owned_threads = owned_threads
    yield instance
    thread = instance._thread
    if thread is None or thread.ident is not None:
        instance.stop()
    elif instance._cap is not None:
        instance._cap.release()
    for thread in owned_threads:
        if thread.ident is not None and thread is not threading.current_thread():
            thread.join(timeout=3)
    assert all(not thread.is_alive() for thread in owned_threads)
    camera_recorder.CameraRecorder._instance = None


def _patch_cv2(monkeypatch, *captures, encode=None):
    fake = _CV2(captures, encode=encode)
    monkeypatch.setattr(camera_recorder, '_cv2', fake)
    return fake


def _wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.005)
    return bool(predicate())


def test_async_open_exception_reports_one_false_result(recorder, monkeypatch):
    fake = _CV2([])
    fake.VideoCapture = lambda _index: (_ for _ in ()).throw(
        RuntimeError('provider open failed'))
    monkeypatch.setattr(camera_recorder, '_cv2', fake)
    results = []
    done = threading.Event()

    recorder.start_async(2, lambda result: (results.append(result), done.set()))

    assert done.wait(2)
    opener = next(t for t in recorder._test_owned_threads
                  if t.name == 'fthr-camera-open')
    opener.join(timeout=3)
    assert not opener.is_alive()
    assert results == [False]
    assert not recorder.is_running()


def test_async_open_thread_start_failure_reports_false_once(recorder, monkeypatch):
    monkeypatch.setattr(camera_recorder.threading.Thread, 'start',
                        lambda _self: (_ for _ in ()).throw(
                            RuntimeError('open worker could not start')))
    results = []

    recorder.start_async(0, results.append)

    assert results == [False]


def test_callback_exception_is_logged_without_poisoning_open_state(
        recorder, monkeypatch, capsys):
    cap = _Capture(0)
    _patch_cv2(monkeypatch, cap)
    callback_called = threading.Event()

    def fail_callback(_result):
        callback_called.set()
        raise RuntimeError('callback failure')

    try:
        recorder.start_async(0, fail_callback)
        assert callback_called.wait(2)
        opener = next(t for t in recorder._test_owned_threads
                      if t.name == 'fthr-camera-open')
        opener.join(timeout=3)
        assert not opener.is_alive()
        assert recorder.is_running()
        assert 'Open-result callback failed: RuntimeError: callback failure' in capsys.readouterr().out
    finally:
        recorder.stop()


@pytest.mark.parametrize('failure', ['is_opened', 'read', 'encode'])
def test_provider_failure_releases_capture_and_allows_fresh_restart(
        recorder, monkeypatch, failure):
    frame = object()
    cap = _Capture(0, read=lambda: (True, frame))
    fresh = _Capture(0)
    encode = None
    if failure == 'is_opened':
        calls = 0

        def is_opened():
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RuntimeError('provider health failed')
            return True

        cap.isOpened = is_opened
    elif failure == 'read':
        cap.read_fn = lambda: (_ for _ in ()).throw(RuntimeError('read failed'))
    else:
        encode = lambda *_args: (_ for _ in ()).throw(RuntimeError('encode failed'))
    _patch_cv2(monkeypatch, cap, fresh, encode=encode)

    assert recorder.start(0)
    assert _wait_until(lambda: not recorder.is_running())
    assert recorder.latest_frame is None
    assert not recorder._chunks
    assert cap.release_count == 1
    assert recorder.start(0)
    recorder.stop()
    assert fresh.release_count == 1


@pytest.mark.parametrize('failure', ['is_opened', 'thread_constructor', 'thread_start'])
def test_start_failure_releases_capture_and_returns_false(
        recorder, monkeypatch, failure):
    cap = _Capture(0)
    _patch_cv2(monkeypatch, cap)
    if failure == 'is_opened':
        cap.isOpened = lambda: (_ for _ in ()).throw(RuntimeError('probe failed'))
    elif failure == 'thread_constructor':
        real_thread = camera_recorder.threading.Thread

        def fail_constructor(*args, **kwargs):
            if kwargs.get('name') == 'fthr-camera':
                raise RuntimeError('thread constructor failed')
            return real_thread(*args, **kwargs)

        monkeypatch.setattr(camera_recorder.threading, 'Thread', fail_constructor)
    else:
        monkeypatch.setattr(camera_recorder.threading.Thread, 'start',
                            lambda _self: (_ for _ in ()).throw(
                                RuntimeError('thread start failed')))

    assert recorder.start(0) is False
    assert cap.release_count == 1
    assert not recorder.is_running()


def test_stop_waits_out_old_blocked_read_before_opening_new_device(
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
    new_cap = _Capture(1, read=lambda: (
        (True, new_frame) if new_cap.read_count == 1 else (False, None)))
    encoded = []
    fake = _patch_cv2(monkeypatch, old_cap, new_cap,
                      encode=lambda _ext, frame, _opts: (
                          encoded.append(frame) is None, b'jpeg'))
    try:
        assert recorder.start(0)
        assert entered.wait(2)
        recorder.stop()
        assert old_cap.release_count == 0
        assert recorder.start(1) is False
        assert fake.opened_indices == [0]
        old_thread = recorder._retired_thread
        assert old_thread is not None and old_thread.is_alive()
        unblock.set()
        old_thread.join(timeout=3)
        assert not old_thread.is_alive()
        assert old_cap.release_count == 1
        assert old_frame not in encoded

        assert recorder.start(1)
        assert fake.opened_indices == [0, 1]
        new_thread_id = recorder._thread.ident
        assert _wait_until(lambda: new_frame in encoded)
        assert set(new_cap.read_threads) == {new_thread_id}
        assert recorder.latest_frame is new_frame
        assert recorder.is_running()
    finally:
        unblock.set()
        recorder.stop()


def test_closed_device_exit_releases_capture(recorder, monkeypatch):
    cap = _Capture(0)
    calls = 0

    def closes_after_open():
        nonlocal calls
        calls += 1
        return calls == 1

    cap.isOpened = closes_after_open
    _patch_cv2(monkeypatch, cap)
    assert recorder.start(0)
    assert _wait_until(lambda: not recorder.is_running())
    assert cap.release_count == 1


def test_chunk_timestamp_is_recorded_before_encoding(recorder, monkeypatch):
    frame = object()
    read_count = 0
    clock = [100.0]

    def read_once():
        nonlocal read_count
        read_count += 1
        return (True, frame) if read_count == 1 else (False, None)

    def encode(*_args):
        clock[0] = 200.0
        return True, b'jpeg'

    _patch_cv2(monkeypatch, _Capture(0, read=read_once), encode=encode)
    monkeypatch.setattr(camera_recorder, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], sleep=time.sleep))
    try:
        assert recorder.start(0)
        assert _wait_until(lambda: bool(recorder._chunks))
        assert recorder._chunks[0][0] == 100.0
    finally:
        recorder.stop()


def test_false_jpeg_keeps_raw_preview_without_chunk(recorder, monkeypatch):
    frame = object()
    count = 0

    def read_once():
        nonlocal count
        count += 1
        return (True, frame) if count == 1 else (False, None)

    _patch_cv2(monkeypatch, _Capture(0, read=read_once),
               encode=lambda *_args: (False, None))
    try:
        assert recorder.start(0)
        assert _wait_until(lambda: recorder.latest_frame is frame)
        assert not recorder._chunks
    finally:
        recorder.stop()


def test_stop_cancels_encoding_before_jpeg_publish(recorder, monkeypatch):
    frame = object()
    encoding = threading.Event()
    finish_encode = threading.Event()

    def encode(*_args):
        encoding.set()
        finish_encode.wait()
        return True, b'jpeg'

    cap = _Capture(0, read=lambda: (True, frame))
    _patch_cv2(monkeypatch, cap, encode=encode)
    stopper = None
    try:
        assert recorder.start(0)
        assert encoding.wait(2)
        stop_event = recorder._stop_event
        stopper = threading.Thread(target=recorder.stop)
        stopper.start()
        assert stop_event.wait(2)
        assert recorder.latest_frame is None
        assert not recorder._chunks
        finish_encode.set()
        stopper.join(timeout=3)
        assert not stopper.is_alive()
        assert not recorder._chunks
    finally:
        finish_encode.set()
        if stopper and stopper.is_alive():
            stopper.join(timeout=3)
        recorder.stop()
