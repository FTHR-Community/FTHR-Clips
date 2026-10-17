"""Linux records the microphone in Python; Windows captures it natively."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == 'win32', reason='Windows captures the microphone natively')


class _Recorder:
    running = False
    started = []
    stops = 0

    def __init__(self):
        pass

    @staticmethod
    def is_available():
        return True

    def is_running(self):
        return _Recorder.running

    def start(self, device_index, gain=1.0):
        _Recorder.started.append((device_index, gain))
        _Recorder.running = True
        return True

    def stop(self):
        _Recorder.stops += 1
        _Recorder.running = False


@pytest.fixture
def recorder(monkeypatch):
    import main
    _Recorder.running = False
    _Recorder.started = []
    _Recorder.stops = 0
    monkeypatch.setattr(main, 'MicRecorder', _Recorder)
    return main


def _host(main, values):
    host = SimpleNamespace(
        settings_manager=SimpleNamespace(get=lambda k, d=None: values.get(k, d)),
        push_error=lambda *a, **k: None,
        _toggle_settings_page=lambda: None,
        _resolved_mic_device_index=lambda: 3,
    )
    host._start_mic_recorder = main.MainWindow._start_mic_recorder.__get__(host)
    return host


def test_audio_enabled_starts_the_python_mic_recorder(recorder):
    host = _host(recorder, {'mic_volume': 150})
    recorder.MainWindow._apply_active_audio_state(
        host, SimpleNamespace(audio_enabled=True))
    assert _Recorder.started == [(3, 1.5)]


def test_audio_disabled_stops_the_recorder(recorder):
    _Recorder.running = True
    host = _host(recorder, {})
    recorder.MainWindow._apply_active_audio_state(
        host, SimpleNamespace(audio_enabled=False))
    assert _Recorder.stops == 1
    assert _Recorder.started == []


def test_saved_clip_is_muxed_with_the_recorded_mic(recorder):
    _Recorder.running = True
    spawned = []
    host = SimpleNamespace(
        settings_manager=SimpleNamespace(get=lambda k, d=None: d),
        _spawn_mux_thread=lambda target, args: spawned.append((target, args)),
        _mic_mux_worker=object(),
        _allow_completed_clip_pipeline=lambda *_a: True,
    )
    recorder.MainWindow._mux_mic_into_clip(
        host, '/tmp/clip.mp4', 30, 12.0, None, None)
    assert spawned and spawned[0][0] is host._mic_mux_worker
