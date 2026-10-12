from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

import main
import core.mic_recorder as mic_recorder_module


class _Settings:
    def __init__(self, values=None, events=None):
        self.values = dict(values or {})
        self.events = events if events is not None else []

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.events.append(('setting', key, value))
        self.values[key] = value

    def save_settings(self):
        self.events.append(('save-settings',))


class _Recorder:
    def __init__(self, events):
        self.events = events
        self.running = True
        self.device_index = 1

    @staticmethod
    def is_available():
        return True

    def is_running(self):
        return self.running

    def start(self, device_index, gain=1.0):
        self.events.append(('recorder-start', device_index, self.running))
        self.running = True
        self.device_index = device_index
        return True

    def set_gain(self, gain):
        self.events.append(('recorder-gain', gain))


class _Combo:
    def __init__(self, data):
        self.data = data

    def currentData(self):
        return self.data

    def count(self):
        return 1


class _Toggle:
    def __init__(self, checked=False):
        self.checked = checked

    def isChecked(self):
        return self.checked

    def blockSignals(self, _blocked):
        pass

    def setChecked(self, checked):
        self.checked = checked


class _Meter:
    def __init__(self, events, recorder):
        self.events = events
        self.recorder = recorder
        self.shared = False

    def stop(self):
        self.events.append(('meter-stop',))
        self.shared = False

    def set_gain(self, gain):
        self.events.append(('meter-gain', gain))

    def start(self, device_index):
        # Model the production meter's recorder-sharing branch. An exclusive
        # fake backend fails if the requested mic is opened while a capture is
        # already active elsewhere.
        self.events.append(('meter-start', device_index, self.recorder.device_index,
                            self.recorder.running))
        if self.recorder.running and self.recorder.device_index == device_index:
            self.shared = True
            return True
        if self.recorder.running:
            raise AssertionError('preview opened a second exclusive capture stream')
        return False


class _Slider:
    def value(self):
        return 75


def _window(monkeypatch, *, data, visible=True, page=2, background=False,
            loopback=False, available=True):
    events = []
    recorder = _Recorder(events)
    settings = _Settings(events=events)
    meter = _Meter(events, recorder)
    window = SimpleNamespace(
        sm=settings,
        settings_manager=settings,
        mic_combo=_Combo(data),
        mic_vol_slider=_Slider(),
        mic_loopback_check=_Toggle(loopback),
        mic_level_meter=meter,
        _loopback_stream=object() if loopback else None,
        isVisible=lambda: visible,
        stack=SimpleNamespace(currentIndex=lambda: page),
        _background_ui_paused=background,
        _stop_loopback=lambda: (events.append(('loopback-stop',)),
                                setattr(window, '_loopback_stream', None)),
        _start_loopback=lambda index: (events.append(('loopback-start', index)),
                                       setattr(window, '_loopback_stream', object())),
        audio_capture_changed=SimpleNamespace(
            emit=lambda restart: events.append(('capture-changed', restart))),
    )
    window._selected_mic_index = (
        lambda: main._SettingsPage._selected_mic_index(window))
    window._save_audio_settings = (
        lambda: main._SettingsPage._save_audio_settings(window))
    class _RecorderFactory:
        @staticmethod
        def is_available():
            return available

        def __new__(cls):
            return recorder

    monkeypatch.setattr(main, 'MicRecorder', _RecorderFactory)
    monkeypatch.setattr('core.mic_recorder.MicRecorder', _RecorderFactory)
    monkeypatch.setattr(main.sys, 'platform', 'linux')
    return window, events, recorder, meter, settings


def test_linux_device_change_restarts_capture_before_visible_meter(monkeypatch):
    window, events, recorder, meter, _settings = _window(
        monkeypatch,
        data={'endpoint_id': None, 'legacy_index': 2, 'display_name': 'USB Mic'},
        loopback=True,
    )

    main._SettingsPage._on_mic_device_changed(window, 1)

    assert events.index(('loopback-stop',)) < events.index(('recorder-start', 2, True))
    assert events.index(('meter-stop',)) < events.index(('recorder-start', 2, True))
    assert events.index(('recorder-start', 2, True)) < next(
        i for i, event in enumerate(events) if event[:2] == ('meter-start', 2)
    )
    assert meter.shared is True
    assert events[-1] == ('loopback-start', 2)
    assert recorder.device_index == 2


@pytest.mark.parametrize('old_index,new_index', [
    (None, 2),  # System Default -> explicit legacy device
    (1, 2),     # one explicit device -> another
    (2, None),  # explicit device -> System Default
])
def test_linux_device_change_restarts_for_default_and_explicit_transitions(
        monkeypatch, old_index, new_index):
    data = ({'endpoint_id': None, 'legacy_index': new_index,
             'display_name': 'System Default' if new_index is None else 'USB Mic'})
    window, events, recorder, _meter, _settings = _window(
        monkeypatch, data=data, visible=False)
    recorder.device_index = old_index

    main._SettingsPage._on_mic_device_changed(window, 0)

    assert ('recorder-start', new_index, True) in events
    assert recorder.device_index == new_index


@pytest.mark.parametrize('visible,page,background,should_start', [
    (True, 2, False, True),
    (False, 2, False, False),
    (True, 1, False, False),
    (True, 2, True, False),
])
def test_linux_device_change_only_binds_meter_when_settings_page_is_active(
        monkeypatch, visible, page, background, should_start):
    window, events, _recorder, meter, _settings = _window(
        monkeypatch,
        data={'endpoint_id': None, 'legacy_index': 4, 'display_name': 'USB Mic'},
        visible=visible, page=page, background=background,
    )

    main._SettingsPage._on_mic_device_changed(window, 0)

    assert any(event[:2] == ('meter-start', 4) for event in events) is should_start
    assert meter.shared is should_start


def test_linux_legacy_selection_name_is_persisted_but_system_default_is_not(
        monkeypatch):
    legacy, _events, _recorder, _meter, settings = _window(
        monkeypatch,
        data={'endpoint_id': None, 'legacy_index': 3, 'display_name': 'USB Mic'},
    )

    main._SettingsPage._save_audio_settings(legacy)

    assert settings.get('mic_device_name') == 'USB Mic'
    assert settings.get('mic_device_id') is None

    default, _events, _recorder, _meter, default_settings = _window(
        monkeypatch,
        data={'endpoint_id': None, 'legacy_index': None,
              'display_name': 'System Default'},
    )
    main._SettingsPage._save_audio_settings(default)
    assert default_settings.get('mic_device_name') is None


def test_linux_saved_legacy_name_binds_only_unique_input_and_tracks_reordered_index(
        monkeypatch, capsys):
    settings = _Settings({'mic_device_name': 'USB Mic'})
    available = [
        {'name': 'Output-only', 'max_input_channels': 0},
        {'name': 'USB Mic', 'max_input_channels': 1},
    ]
    monkeypatch.setattr(main.sys, 'platform', 'linux')
    monkeypatch.setattr(main.MicRecorder, 'is_available', lambda: True)
    monkeypatch.setitem(sys.modules, 'sounddevice',
                        SimpleNamespace(query_devices=lambda: available))
    window = SimpleNamespace(settings_manager=settings)

    assert main.MainWindow._resolved_mic_device_index(window) == 1
    available[:] = [
        {'name': 'USB Mic', 'max_input_channels': 1},
        {'name': 'Output-only', 'max_input_channels': 0},
    ]
    assert main.MainWindow._resolved_mic_device_index(window) == 0

    available[:] = [
        {'name': 'USB Mic', 'max_input_channels': 1},
        {'name': 'USB Mic', 'max_input_channels': 1},
        {'name': 'Output-only', 'max_input_channels': 0},
    ]
    assert main.MainWindow._resolved_mic_device_index(window) is None
    assert 'ambiguous' in capsys.readouterr().out.lower()

    settings.values['mic_device_name'] = 'Missing Mic'
    assert main.MainWindow._resolved_mic_device_index(window) is None
    assert 'not found' in capsys.readouterr().out.lower()

    settings.values['mic_device_name'] = 'Output-only'
    assert main.MainWindow._resolved_mic_device_index(window) is None


def test_linux_device_change_keeps_settings_without_recorder_availability(
        monkeypatch):
    window, events, _recorder, meter, settings = _window(
        monkeypatch,
        data={'endpoint_id': None, 'legacy_index': 5, 'display_name': 'USB Mic'},
        visible=True, available=False,
    )
    _recorder.running = False

    main._SettingsPage._on_mic_device_changed(window, 0)

    assert not any(event[0] == 'recorder-start' for event in events)
    assert settings.get('mic_device_name') == 'USB Mic'
    assert meter.shared is False


class _ExclusiveInputStream:
    """In-memory PortAudio stand-in that rejects a second open per device."""

    def __init__(self, *, device, **_kwargs):
        self.device = device
        self.started = False
        self.closed = False
        self.active = type(self).active
        type(self).attempts.append(device)
        if device in self.active:
            raise RuntimeError('device unavailable: exclusive capture already open')
        self.active.add(device)
        type(self).opens[device] = type(self).opens.get(device, 0) + 1

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        if not self.closed:
            self.active.discard(self.device)
            self.closed = True


_ExclusiveInputStream.active = set()
_ExclusiveInputStream.opens = {}
_ExclusiveInputStream.attempts = []


def _production_audio_harness(monkeypatch, qtbot):
    _ExclusiveInputStream.active = set()
    _ExclusiveInputStream.opens = {}
    _ExclusiveInputStream.attempts = []
    audio_backend = SimpleNamespace(InputStream=_ExclusiveInputStream)
    monkeypatch.setattr(mic_recorder_module, '_sd', audio_backend, raising=False)
    monkeypatch.setattr(mic_recorder_module, '_AVAILABLE', True)
    monkeypatch.setattr(main, '_sd', audio_backend, raising=False)
    monkeypatch.setattr(main, '_SD_AVAILABLE', True)
    monkeypatch.setattr(main.sys, 'platform', 'linux')

    meter = main._MicLevelMeter()
    qtbot.addWidget(meter)
    recorder = main.MicRecorder()
    assert recorder.start(1)

    events = []
    settings = _Settings(events=events)
    window = SimpleNamespace(
        sm=settings,
        settings_manager=settings,
        mic_combo=_Combo({
            'endpoint_id': None, 'legacy_index': 2, 'display_name': 'USB Mic'}),
        mic_vol_slider=_Slider(),
        mic_loopback_check=_Toggle(False),
        mic_level_meter=meter,
        _loopback_stream=None,
        isVisible=lambda: True,
        stack=SimpleNamespace(currentIndex=lambda: 2),
        _background_ui_paused=False,
        audio_capture_changed=SimpleNamespace(emit=lambda *_args: None),
    )
    window._selected_mic_index = (
        lambda: main._SettingsPage._selected_mic_index(window))
    window._save_audio_settings = (
        lambda: main._SettingsPage._save_audio_settings(window))
    return window, recorder, meter


def test_linux_device_change_uses_actual_meter_recorder_sharing_under_exclusive_backend(
        monkeypatch, qtbot):
    original_instance = main.MicRecorder._instance
    monkeypatch.setattr(main.MicRecorder, '_instance', None)
    window, recorder, meter = _production_audio_harness(monkeypatch, qtbot)
    try:
        main._SettingsPage._on_mic_device_changed(window, 1)

        assert recorder.is_running()
        assert recorder._device_index == 2
        assert meter._shared_recorder is recorder
        assert meter._stream is None
        assert _ExclusiveInputStream.opens == {1: 1, 2: 1}
        assert _ExclusiveInputStream.active == {2}
    finally:
        meter.stop()
        recorder.stop()
        main.MicRecorder._instance = original_instance


def test_actual_old_meter_before_recorder_order_fails_exclusive_restart(
        monkeypatch, qtbot):
    original_instance = main.MicRecorder._instance
    monkeypatch.setattr(main.MicRecorder, '_instance', None)
    window, recorder, meter = _production_audio_harness(monkeypatch, qtbot)
    try:
        # Reproduce the old callback ordering: preview opens the newly selected
        # device before save/restart tears down and replaces the old recorder.
        meter.set_gain(window.mic_vol_slider.value() / 100.0)
        assert meter.start(2)
        assert not recorder.start(2, gain=0.75)
        assert recorder.is_running() is False
        assert meter._shared_recorder is None
        assert meter._stream is not None
        assert _ExclusiveInputStream.opens == {1: 1, 2: 1}
        assert _ExclusiveInputStream.attempts == [1, 2, 2]
    finally:
        meter.stop()
        recorder.stop()
        main.MicRecorder._instance = original_instance


def test_windows_device_change_keeps_existing_preview_before_settings_order(
        monkeypatch):
    window, events, _recorder, _meter, _settings = _window(
        monkeypatch,
        data={'endpoint_id': 'native-id', 'legacy_index': None,
              'display_name': 'Native Mic'},
        loopback=True,
    )
    _recorder.device_index = None
    monkeypatch.setattr(main.sys, 'platform', 'win32')

    main._SettingsPage._on_mic_device_changed(window, 0)

    assert events.index(('meter-start', None, None, True)) < next(
        i for i, event in enumerate(events) if event[0] == 'setting'
    )
    assert events.index(('loopback-start', None)) < next(
        i for i, event in enumerate(events) if event[0] == 'setting'
    )
    assert events[-1] == ('capture-changed', True)
