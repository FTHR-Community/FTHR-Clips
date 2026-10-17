"""A slow camera driver must not hold up the first Visuals visit."""

import threading
from pathlib import Path

from PySide6.QtCore import QTimer

from core.camera_recorder import CameraRecorder
from core.settings_manager import SettingsManager
from main import _SettingsPage


def test_first_visuals_visit_and_tab_switch_remain_responsive(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(_SettingsPage, '_start_encoder_probe', lambda *_: None)
    entered, release = threading.Event(), threading.Event()
    scans = []
    ui_thread = threading.get_ident()

    def scan():
        scans.append(threading.get_ident())
        entered.set()
        release.wait(5)
        return [{'name': 'Saved camera', 'index': 3}, {'name': 'Other camera', 'index': 0}]

    monkeypatch.setattr(CameraRecorder, 'is_available', lambda: True)
    monkeypatch.setattr(CameraRecorder, 'list_devices', scan)
    settings = SettingsManager()
    settings.set('camera_device_index', 3)
    page = _SettingsPage(settings)
    qtbot.addWidget(page)
    try:
        page._ensure_category(3)
        qtbot.waitUntil(entered.is_set)
        assert page.camera_device_combo.currentText() == 'Scanning cameras…'
        assert not page.camera_device_combo.isEnabled()
        page._populate_camera_devices()  # Repeated refresh shares the active scan.
        assert len(scans) == 1 and scans[0] != ui_thread
        ticks = []
        QTimer.singleShot(0, lambda: ticks.append(True))
        page._on_category_changed(6)
        qtbot.waitUntil(lambda: page.stack.currentIndex() == 6 and bool(ticks))
        assert not release.is_set()
        assert settings.get('camera_device_index') == 3
    finally:
        release.set()
        page._camera_device_scan_thread.join(5)
    qtbot.waitUntil(lambda: page.camera_device_combo.isEnabled())
    assert page.camera_device_combo.currentData() == 3
    assert settings.get('camera_device_index') == 3


def test_failed_camera_scan_can_be_refreshed(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(_SettingsPage, '_start_encoder_probe', lambda *_: None)
    monkeypatch.setattr(CameraRecorder, 'is_available', lambda: True)

    def fail():
        raise RuntimeError('Device scan failed')

    monkeypatch.setattr(CameraRecorder, 'list_devices', fail)
    page = _SettingsPage(SettingsManager())
    qtbot.addWidget(page)
    page._ensure_category(3)
    qtbot.waitUntil(lambda: 'failed' in page.camera_device_combo.currentText())
    page._camera_device_scan_thread.join(5)
    monkeypatch.setattr(CameraRecorder, 'list_devices', lambda: [{'name': 'Camera', 'index': 0}])
    page._populate_camera_devices()
    qtbot.waitUntil(lambda: page.camera_device_combo.isEnabled())
    page._camera_device_scan_thread.join(5)
    assert page.camera_device_combo.currentText() == 'Camera'
