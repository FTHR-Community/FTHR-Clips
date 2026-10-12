from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QComboBox

from core import camera_recorder
from main import _SettingsPage


class _ProbeCapture:
    def __init__(self, index, opened=False, error=None):
        self.index = index
        self.opened = opened
        self.error = error
        self.release_count = 0

    def isOpened(self):
        if self.error:
            raise self.error
        return self.opened

    def release(self):
        self.release_count += 1


class _Settings:
    def __init__(self, selected=0, enabled=True):
        self.values = {
            'camera_device_index': selected,
            'camera_enabled': enabled,
        }
        self.save_count = 0

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value

    def save_settings(self):
        self.save_count += 1


@pytest.fixture
def fake_recorder(monkeypatch):
    camera_recorder.CameraRecorder._instance = None
    instance = camera_recorder.CameraRecorder()
    instance._running = True
    instance._device_index = 4
    instance._cap = object()
    with instance._lock:
        instance._running = True
        instance._device_index = 4
        instance._cap = object()
    yield instance
    instance._running = False
    instance._cap = None
    camera_recorder.CameraRecorder._instance = None


def test_listing_keeps_exclusive_active_camera_without_reopening(
        fake_recorder, monkeypatch):
    probes = []

    def video_capture(index):
        assert index != 4, 'active exclusive camera must not be probed again'
        cap = _ProbeCapture(index, opened=index == 2)
        probes.append(cap)
        return cap

    monkeypatch.setattr(camera_recorder, '_AVAILABLE', True)
    monkeypatch.setattr(camera_recorder.sys, 'platform', 'linux')
    monkeypatch.setattr(camera_recorder, '_cv2', SimpleNamespace(
        VideoCapture=video_capture))

    devices = camera_recorder.CameraRecorder.list_devices()

    assert devices == [
        {'index': 2, 'name': 'Camera 3'},
        {'index': 4, 'name': 'Camera 5'},
    ]
    assert len(probes) == 7
    assert all(cap.release_count == 1 for cap in probes)


def test_inactive_missing_devices_are_not_invented_and_probe_errors_release(
        monkeypatch):
    camera_recorder.CameraRecorder._instance = None
    probes = []

    def video_capture(index):
        cap = _ProbeCapture(
            index, opened=False,
            error=RuntimeError('probe failure') if index == 3 else None)
        probes.append(cap)
        return cap

    monkeypatch.setattr(camera_recorder, '_AVAILABLE', True)
    monkeypatch.setattr(camera_recorder.sys, 'platform', 'linux')
    monkeypatch.setattr(camera_recorder, '_cv2', SimpleNamespace(
        VideoCapture=video_capture))

    assert camera_recorder.CameraRecorder.list_devices() == []
    assert len(probes) == 8
    assert all(cap.release_count == 1 for cap in probes)


@pytest.mark.parametrize('initial_blocked', [False, True])
def test_refresh_preserves_saved_selection_and_suppresses_change_signal(
        qtbot, monkeypatch, initial_blocked):
    combo = QComboBox()
    qtbot.addWidget(combo)
    combo.addItem('Old camera', 1)
    combo.addItem('Saved camera', 5)
    combo.setCurrentIndex(0)
    combo.blockSignals(initial_blocked)
    settings = _Settings(selected=5, enabled=True)
    host = SimpleNamespace(camera_device_combo=combo, sm=settings)
    recorder_starts = []

    class FakeRecorder:
        def __new__(cls):
            return cls

        @classmethod
        def is_available(cls):
            return True

        @classmethod
        def list_devices(cls):
            return [
                {'index': 2, 'name': 'Camera 3'},
                {'index': 5, 'name': 'Saved camera'},
            ]

        @classmethod
        def start_async(cls, index):
            recorder_starts.append(index)

    monkeypatch.setattr(camera_recorder, 'CameraRecorder', FakeRecorder)
    combo.currentIndexChanged.connect(
        lambda index: _SettingsPage._on_camera_device_changed(host, index))

    _SettingsPage._populate_camera_devices(host)

    assert combo.currentData() == 5
    assert combo.signalsBlocked() is initial_blocked
    assert recorder_starts == []
    assert settings.values['camera_device_index'] == 5
    assert settings.save_count == 0


def test_refresh_restores_signal_state_when_scan_raises(qtbot, monkeypatch):
    combo = QComboBox()
    qtbot.addWidget(combo)
    combo.addItem('Existing', 3)
    combo.blockSignals(True)
    host = SimpleNamespace(camera_device_combo=combo, sm=_Settings(selected=3))

    class FailingRecorder:
        @classmethod
        def is_available(cls):
            return True

        @classmethod
        def list_devices(cls):
            raise RuntimeError('enumeration failed')

    monkeypatch.setattr(camera_recorder, 'CameraRecorder', FailingRecorder)

    with pytest.raises(RuntimeError, match='enumeration failed'):
        _SettingsPage._populate_camera_devices(host)

    assert combo.signalsBlocked() is True
