"""Focused lifetime checks for clip workers' GUI-owned result channels."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import QObject, QThreadPool, Slot
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from ui import clip_viewer


class _ReadyReceiver(QObject):
    def __init__(self):
        super().__init__(QApplication.instance())
        self.values = []
        self.destroyed_count = 0

    @Slot(object)
    def receive(self, value):
        self.values.append(value)

    def mark_destroyed(self, *_args):
        self.destroyed_count += 1


def _run_worker(qtbot, worker, *, expect_result=True):
    """Run a real QRunnable and wait for both its queued result and cleanup."""
    receiver = _ReadyReceiver()
    signals = worker.signals
    assert signals.parent() is QApplication.instance()
    signals.ready.connect(receiver.receive)
    signals.destroyed.connect(receiver.mark_destroyed)

    pool = QThreadPool()
    pool.setMaxThreadCount(1)
    pool.start(worker)
    if expect_result:
        qtbot.waitUntil(
            lambda: bool(receiver.values and receiver.destroyed_count),
            timeout=5000,
        )
    else:
        qtbot.waitUntil(lambda: receiver.destroyed_count, timeout=5000)
    pool.waitForDone(1000)
    return receiver, signals


@pytest.mark.parametrize("worker_name", [
    "_PlaybackSourceWorker",
    "_CompatibilitySourceWorker",
])
def test_source_worker_queues_result_before_gui_cleanup(
        monkeypatch, qtbot, worker_name):
    from core import playback_proxy

    if worker_name == "_PlaybackSourceWorker":
        monkeypatch.setattr(
            playback_proxy, "prepare_playback_path",
            lambda path, cancelled: path + ".prepared")
    else:
        monkeypatch.setattr(
            playback_proxy, "prepare_compatibility_path",
            lambda path, cancelled: path + ".compatible")

    worker = getattr(clip_viewer, worker_name)(
        "clip.mp4", threading.Event())
    receiver, _signals = _run_worker(qtbot, worker)

    assert receiver.values == [
        "clip.mp4.prepared" if worker_name == "_PlaybackSourceWorker"
        else "clip.mp4.compatible"
    ]
    assert receiver.destroyed_count == 1


class _Capture:
    released = 0
    seeks = []

    def __init__(self, _path):
        pass

    def set(self, property_id, value):
        self.seeks.append((property_id, value))
        return True

    def read(self):
        return True, np.zeros((3, 5, 3), dtype="uint8")

    def release(self):
        type(self).released += 1


@pytest.mark.parametrize("worker_name", [
    "_InitialFrameDecodeWorker",
    "_PausedSeekFrameDecodeWorker",
])
def test_decode_workers_release_capture_and_keep_queued_frame(
        monkeypatch, qtbot, worker_name):
    _Capture.released = 0
    _Capture.seeks = []
    monkeypatch.setattr(clip_viewer.cv2, "VideoCapture", _Capture)
    cancelled = threading.Event()
    if worker_name == "_InitialFrameDecodeWorker":
        worker = clip_viewer._InitialFrameDecodeWorker(
            "clip.mp4", 17, cancelled)
    else:
        worker = clip_viewer._PausedSeekFrameDecodeWorker(
            "clip.mp4", 17, 23, 4200, cancelled)

    receiver, _signals = _run_worker(qtbot, worker)

    assert len(receiver.values) == 1
    result = receiver.values[0]
    if worker_name == "_InitialFrameDecodeWorker":
        generation, image, error = result
        assert generation == 17
    else:
        source_generation, seek_generation, target_ms, image, error = result
        assert (source_generation, seek_generation, target_ms) == (17, 23, 4200)
        assert _Capture.seeks
    assert image.size().width() == 5
    assert image.size().height() == 3
    assert error is None
    assert _Capture.released == 1
    assert receiver.destroyed_count == 1


def test_metadata_worker_queues_probe_result_and_deletes_signals(
        monkeypatch, qtbot, tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"test")
    monkeypatch.setattr(
        clip_viewer, "probe_video_metadata",
        lambda path, cancelled: SimpleNamespace(
            duration_seconds=2.5,
            width=1920,
            height=1080,
            average_fps=60.0,
            video_bitrate_bps=123,
            total_bitrate_bps=456,
        ),
    )
    from ui import library_thumbnails
    monkeypatch.setattr(library_thumbnails, "_get_cached_thumb_path", lambda path: "thumb.jpg")
    monkeypatch.setattr(
        library_thumbnails, "_get_cached_duration_path",
        lambda path: str(tmp_path / "duration.meta"),
    )
    writes = []
    monkeypatch.setattr(
        library_thumbnails, "_write_cached_duration",
        lambda *args: writes.append(args),
    )

    worker = clip_viewer._ClipMetadataWorker(str(source), threading.Event())
    receiver, _signals = _run_worker(qtbot, worker)

    assert receiver.values == [(2.5, 1920, 1080, 60.0, 123, 456)]
    assert writes
    assert receiver.destroyed_count == 1


@pytest.mark.parametrize("worker_name", [
    "_PlaybackSourceWorker",
    "_CompatibilitySourceWorker",
    "_InitialFrameDecodeWorker",
    "_PausedSeekFrameDecodeWorker",
    "_ClipMetadataWorker",
])
def test_cancelled_clip_workers_still_release_gui_signal_owner(
        monkeypatch, qtbot, tmp_path, worker_name):
    from core import playback_proxy

    monkeypatch.setattr(
        playback_proxy, "prepare_playback_path",
        lambda path, cancelled: path,
    )
    monkeypatch.setattr(
        playback_proxy, "prepare_compatibility_path",
        lambda path, cancelled: path,
    )
    monkeypatch.setattr(
        clip_viewer.cv2, "VideoCapture",
        lambda path: pytest.fail("cancelled decode must not open a capture"),
    )
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"test")
    cancelled = threading.Event()
    cancelled.set()
    if worker_name == "_PlaybackSourceWorker":
        worker = clip_viewer._PlaybackSourceWorker(str(source), cancelled)
    elif worker_name == "_CompatibilitySourceWorker":
        worker = clip_viewer._CompatibilitySourceWorker(str(source), cancelled)
    elif worker_name == "_InitialFrameDecodeWorker":
        worker = clip_viewer._InitialFrameDecodeWorker(str(source), 1, cancelled)
    elif worker_name == "_PausedSeekFrameDecodeWorker":
        worker = clip_viewer._PausedSeekFrameDecodeWorker(
            str(source), 1, 2, 100, cancelled)
    else:
        worker = clip_viewer._ClipMetadataWorker(str(source), cancelled)

    receiver, _signals = _run_worker(qtbot, worker, expect_result=False)

    assert receiver.values == []
    assert receiver.destroyed_count == 1


class _FakeReadySignal:
    def connect(self, _callback):
        pass


def test_initial_decode_pool_is_parented_before_worker_signal_creation(monkeypatch):
    events = []

    class _Pool:
        def start(self, _worker):
            events.append("start")

    class _Worker:
        def __init__(self, *_args):
            events.append("worker")
            self.signals = SimpleNamespace(ready=_FakeReadySignal())

    pool = _Pool()
    monkeypatch.setattr(
        clip_viewer, "_frame_decode_pool",
        lambda: events.append("pool") or pool,
    )
    monkeypatch.setattr(clip_viewer, "_InitialFrameDecodeWorker", _Worker)
    viewer = SimpleNamespace(
        _closing=False,
        _media_ready=True,
        _playback_path="clip.mp4",
        _prepare_cancel=threading.Event(),
        _seek_generation=3,
        _latest_paused_seek_ms=None,
        _native_output_enabled=False,
        _native_has_real_frame=False,
        player=SimpleNamespace(playbackState=lambda: None),
        video_widget=SimpleNamespace(
            _source_generation=7,
            request_initial_frame=lambda: True,
        ),
        _connect_clip_result=lambda signal, callback: signal.connect(callback),
        _on_initial_decoded_frame=lambda *_args, **_kwargs: None,
    )

    clip_viewer.ClipViewer._request_initial_preview_frame(viewer)

    assert events == ["pool", "worker", "start"]


def test_paused_decode_pool_is_parented_before_worker_signal_creation(monkeypatch):
    events = []

    class _Pool:
        def start(self, _worker):
            events.append("start")

    class _Worker:
        def __init__(self, *_args):
            events.append("worker")
            self.signals = SimpleNamespace(ready=_FakeReadySignal())

    pool = _Pool()
    monkeypatch.setattr(
        clip_viewer, "_frame_decode_pool",
        lambda: events.append("pool") or pool,
    )
    monkeypatch.setattr(clip_viewer, "_PausedSeekFrameDecodeWorker", _Worker)
    viewer = SimpleNamespace(
        _closing=False,
        _media_ready=True,
        _seek_generation=3,
        _latest_paused_seek_ms=4200,
        _seek_frame_acknowledged=False,
        _paused_seek_decode_worker=None,
        _pending_paused_seek_decode=None,
        player=SimpleNamespace(playbackState=lambda: None),
        video_widget=SimpleNamespace(_source_generation=7),
        _prepare_cancel=threading.Event(),
        _connect_clip_result=lambda signal, callback: signal.connect(callback),
        _on_paused_seek_frame_decoded=lambda *_args: None,
    )

    clip_viewer.ClipViewer._start_paused_seek_decode(
        viewer, (7, 3, 4200, "clip.mp4"))

    assert events == ["pool", "worker", "start"]


def _assert_direct_worker_error_cleanup(qtbot, worker, error_type):
    receiver = _ReadyReceiver()
    signals = worker.signals
    signals.destroyed.connect(receiver.mark_destroyed)

    with pytest.raises(error_type):
        worker.run()

    qtbot.waitUntil(lambda: receiver.destroyed_count == 1, timeout=5000)
    assert receiver.values == []
    assert not isValid(signals)


@pytest.mark.parametrize("worker_name,error_type", [
    ("_PlaybackSourceWorker", OSError),
    ("_CompatibilitySourceWorker", RuntimeError),
])
def test_source_worker_prepare_error_still_runs_outer_cleanup(
        monkeypatch, qtbot, tmp_path, worker_name, error_type):
    from core import playback_proxy

    def fail_prepare(_path, _cancelled):
        raise error_type("prepare failed")

    helper_name = (
        "prepare_playback_path"
        if worker_name == "_PlaybackSourceWorker"
        else "prepare_compatibility_path"
    )
    monkeypatch.setattr(playback_proxy, helper_name, fail_prepare)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"test")
    worker = getattr(clip_viewer, worker_name)(
        str(source), threading.Event())

    _assert_direct_worker_error_cleanup(qtbot, worker, error_type)


class _ReleaseFailingCapture:
    def __init__(self, _path):
        pass

    def set(self, _property_id, _value):
        return True

    def read(self):
        return True, np.zeros((3, 5, 3), dtype="uint8")

    def release(self):
        raise RuntimeError("capture release failed")


@pytest.mark.parametrize("worker_name", [
    "_InitialFrameDecodeWorker",
    "_PausedSeekFrameDecodeWorker",
])
def test_decode_release_error_still_runs_outer_cleanup(
        monkeypatch, qtbot, tmp_path, worker_name):
    monkeypatch.setattr(
        clip_viewer.cv2, "VideoCapture", _ReleaseFailingCapture)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"test")
    cancelled = threading.Event()
    if worker_name == "_InitialFrameDecodeWorker":
        worker = clip_viewer._InitialFrameDecodeWorker(
            str(source), 17, cancelled)
    else:
        worker = clip_viewer._PausedSeekFrameDecodeWorker(
            str(source), 17, 23, 4200, cancelled)

    _assert_direct_worker_error_cleanup(qtbot, worker, RuntimeError)
