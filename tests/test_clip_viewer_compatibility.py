import threading
from types import SimpleNamespace

from PySide6.QtCore import QThreadPool
from PySide6.QtMultimedia import QMediaPlayer

from ui import clip_viewer
from ui.clip_viewer import ClipViewer


def _compatibility_viewer():
    viewer = SimpleNamespace(
        _closing=False,
        _compatibility_checked=False,
        _compatibility_worker=None,
        _compatibility_cancel=threading.Event(),
        _compatibility_generation=1,
        _clip_receivers=[],
        clip_path='original.mp4',
        _playback_path='original.mp4',
        _set_media_source=lambda: None,
    )
    viewer._on_compatibility_source_ready = (
        ClipViewer._on_compatibility_source_ready.__get__(viewer))
    viewer._cancel_compatibility_prepare = (
        ClipViewer._cancel_compatibility_prepare.__get__(viewer))
    return viewer


def _connect_direct(viewer):
    def connect(signal, callback):
        signal.connect(callback)
        receiver = SimpleNamespace(deleteLater=lambda: None)
        viewer._clip_receivers.append(receiver)
        return receiver

    viewer._connect_clip_result = connect


def test_first_source_waits_for_compatibility_worker_before_set_source(
        monkeypatch):
    viewer = _compatibility_viewer()
    _connect_direct(viewer)
    started = []
    sources = []
    viewer._set_media_source = lambda: sources.append(viewer._playback_path)
    monkeypatch.setattr(clip_viewer.sys, 'platform', 'win32')
    monkeypatch.setattr(QThreadPool.globalInstance(), 'start', started.append)

    ClipViewer._prepare_initial_media_source(viewer)
    assert len(started) == 1
    assert sources == []
    worker = started[0]
    worker.signals.ready.emit('compatibility-cache.mp4')
    assert sources == ['compatibility-cache.mp4']
    assert viewer.clip_path == 'original.mp4'
    assert viewer._compatibility_checked


def test_stale_compatibility_result_is_discarded_after_clip_switch_or_close(
        monkeypatch):
    viewer = _compatibility_viewer()
    _connect_direct(viewer)
    started = []
    sources = []
    viewer._set_media_source = lambda: sources.append(viewer._playback_path)
    monkeypatch.setattr(clip_viewer.sys, 'platform', 'win32')
    monkeypatch.setattr(QThreadPool.globalInstance(), 'start', started.append)

    ClipViewer._prepare_initial_media_source(viewer)
    old_worker = started[-1]
    old_token = viewer._compatibility_cancel
    ClipViewer._cancel_compatibility_prepare(viewer)
    viewer._compatibility_generation += 1
    viewer._compatibility_cancel = threading.Event()
    viewer._playback_path = 'second.mp4'
    old_worker.signals.ready.emit('obsolete.mp4')
    assert sources == []
    assert not viewer._compatibility_checked
    assert old_token.is_set()

    ClipViewer._cancel_compatibility_prepare(viewer)
    old_worker.signals.ready.emit('after-close.mp4')
    assert sources == []
    assert viewer._compatibility_cancel.is_set()


def test_cached_full_proxy_skips_compatibility_worker(monkeypatch):
    viewer = _compatibility_viewer()
    viewer._playback_path = 'full-proxy.mp4'
    calls = []
    viewer._set_media_source = lambda: calls.append(viewer._playback_path)
    monkeypatch.setattr(clip_viewer.sys, 'platform', 'win32')

    ClipViewer._prepare_initial_media_source(viewer)
    assert calls == ['full-proxy.mp4']
    assert viewer._compatibility_checked


def test_compatibility_path_can_still_start_optional_gop_proxy(monkeypatch):
    viewer = SimpleNamespace(
        _closing=False,
        clip_path='original.mp4',
        _playback_path='compatibility-cache.mp4',
        _compatibility_cache_active=True,
        _playback_source_worker=None,
        _proxy_checked=False,
        _media_ready=True,
        _play_when_ready=False,
        _proxy_prepare_timer=SimpleNamespace(stop=lambda: None),
        player=SimpleNamespace(
            playbackState=lambda: QMediaPlayer.PlaybackState.PausedState),
        _connect_clip_result=lambda signal, callback: None,
    )
    started = []
    monkeypatch.setattr(QThreadPool.globalInstance(), 'start', started.append)

    ClipViewer._prepare_playback_source(viewer)
    assert len(started) == 1

    viewer._playback_source_worker = None
    viewer._compatibility_cache_active = False
    started.clear()
    ClipViewer._prepare_playback_source(viewer)
    assert started == []
