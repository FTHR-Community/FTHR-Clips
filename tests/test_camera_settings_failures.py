from __future__ import annotations

import importlib
import threading
from types import SimpleNamespace

import pytest

from FTHR_UI import main
from FTHR_UI.ui.error_bar import ErrorBar


class _Settings:
    def __init__(self, **values):
        self.values = values
        self.save_count = 0

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value

    def save_settings(self):
        self.save_count += 1


class _CheckBox:
    def __init__(self, checked=False):
        self.checked = checked
        self.signals_blocked = False
        self.mutations = []

    def blockSignals(self, blocked):
        previous = self.signals_blocked
        self.signals_blocked = blocked
        return previous

    def setChecked(self, checked):
        self.checked = bool(checked)
        self.mutations.append((self.checked, threading.get_ident()))


class _ComboBox:
    def __init__(self, device_indices):
        self.device_indices = device_indices

    def itemData(self, combo_index):
        return self.device_indices[combo_index]


class _Stack:
    def currentIndex(self):
        return 0


class _Timer:
    def __init__(self):
        self.active = False
        self.events = []

    def start(self):
        self.active = True
        self.events.append(('start', threading.get_ident()))

    def stop(self):
        self.active = False
        self.events.append(('stop', threading.get_ident()))


class _Preview:
    def __init__(self):
        self.overlay_enabled = None
        self.frame = object()
        self.events = []

    def set_overlay_enabled(self, name, enabled):
        self.overlay_enabled = (name, bool(enabled))
        self.events.append(('enabled', bool(enabled), threading.get_ident()))

    def set_camera_pixmap(self, pixmap):
        self.frame = pixmap
        self.events.append(('frame', pixmap, threading.get_ident()))


class _FakeCameraRecorder:
    available = True
    requests = []
    stop_count = 0

    @classmethod
    def is_available(cls):
        return cls.available

    def start_async(self, device_index, on_result=None):
        type(self).requests.append((device_index, on_result))

    def stop(self):
        type(self).stop_count += 1


@pytest.fixture
def camera_settings_page(qtbot, monkeypatch):
    _FakeCameraRecorder.available = True
    _FakeCameraRecorder.requests = []
    _FakeCameraRecorder.stop_count = 0
    monkeypatch.setattr(main._SettingsPage, '_setup_ui', lambda _self: None)
    monkeypatch.setattr(main._SettingsPage, '_apply_styles', lambda _self: None)
    monkeypatch.setattr(
        main._SettingsPage, '_load_audio_settings', lambda _self: None)
    monkeypatch.setattr(main, 'PresetsManager', lambda: object())
    camera_module = importlib.import_module('core.camera_recorder')
    monkeypatch.setattr(
        camera_module, 'CameraRecorder', _FakeCameraRecorder)

    settings = _Settings(
        camera_enabled=False,
        camera_device_index=0,
        camera_overlay_rect={'x': 0.7, 'y': 0.7, 'w': 0.2, 'h': 0.2},
        camera_position='top-left',
    )
    page = main._SettingsPage(settings)
    page.camera_check = _CheckBox(False)
    page.camera_device_combo = _ComboBox([0, 2, 5])
    page.stack = _Stack()
    page._camera_preview_timer = _Timer()
    page.unified_overlay_editor = _Preview()
    page.camera_unavailable_messages = []
    if hasattr(page, 'camera_unavailable'):
        page.camera_unavailable.connect(
            page.camera_unavailable_messages.append)
    qtbot.addWidget(page)
    return page


def test_camera_toggle_failure_unchecks_disables_clears_and_reports(
        camera_settings_page, qtbot):
    page = camera_settings_page
    ui_thread = threading.get_ident()

    page._on_camera_toggled(True)

    assert len(_FakeCameraRecorder.requests) == 1
    device_index, on_result = _FakeCameraRecorder.requests[0]
    assert device_index == 0
    assert callable(on_result)
    worker = threading.Thread(target=lambda: on_result(False))
    worker.start()
    worker.join()
    qtbot.waitUntil(lambda: bool(page.camera_unavailable_messages))

    assert page.sm.get('camera_enabled') is False
    assert page.camera_check.checked is False
    assert page.camera_unavailable_messages
    assert 'connected' in page.camera_unavailable_messages[-1].lower()
    assert len(page.camera_unavailable_messages[-1]) <= 240
    assert not page._camera_preview_timer.active
    assert page.unified_overlay_editor.overlay_enabled == ('camera', False)
    assert page.unified_overlay_editor.frame.isNull()
    assert {event[-1] for event in page.camera_check.mutations} == {ui_thread}
    assert {event[-1] for event in page.unified_overlay_editor.events} == {ui_thread}
    assert {event[-1] for event in page._camera_preview_timer.events} == {ui_thread}


def test_device_change_failure_keeps_selected_device_but_disables_camera(
        camera_settings_page):
    page = camera_settings_page
    page.sm.set('camera_enabled', True)
    page.camera_check.checked = True
    before = dict(page.sm.values['camera_overlay_rect'])

    page._on_camera_device_changed(1)

    device_index, on_result = _FakeCameraRecorder.requests[-1]
    assert device_index == 2
    assert callable(on_result)
    on_result(False)

    assert page.sm.get('camera_device_index') == 2
    assert page.sm.get('camera_enabled') is False
    assert page.camera_check.checked is False
    assert page.sm.get('camera_overlay_rect') == before
    assert page.sm.get('camera_position') == 'top-left'
    assert page.unified_overlay_editor.overlay_enabled == ('camera', False)
    assert page.unified_overlay_editor.frame.isNull()
    assert not page._camera_preview_timer.active
    assert page.camera_unavailable_messages


def test_stale_open_result_after_disable_is_ignored(camera_settings_page):
    page = camera_settings_page
    page._on_camera_toggled(True)
    _device_index, on_result = _FakeCameraRecorder.requests[-1]
    assert callable(on_result)

    page._on_camera_toggled(False)
    timer_events = list(page._camera_preview_timer.events)
    preview_events = list(page.unified_overlay_editor.events)
    checkbox_mutations = list(page.camera_check.mutations)
    on_result(True)

    assert page.sm.get('camera_enabled') is False
    assert page.camera_unavailable_messages == []
    assert page._camera_preview_timer.events == timer_events
    assert page.unified_overlay_editor.events == preview_events
    assert page.camera_check.mutations == checkbox_mutations


def test_old_device_result_is_ignored_after_newer_device_request(
        camera_settings_page):
    page = camera_settings_page
    page.sm.set('camera_enabled', True)
    page.camera_check.checked = True
    page._on_camera_device_changed(1)
    _old_device, old_result = _FakeCameraRecorder.requests[-1]
    page._on_camera_device_changed(2)
    latest_device, latest_result = _FakeCameraRecorder.requests[-1]
    assert callable(old_result)
    assert callable(latest_result)

    old_result(False)
    assert page.sm.get('camera_enabled') is True
    assert page.camera_unavailable_messages == []
    assert not page._camera_preview_timer.active

    latest_result(True)
    assert latest_device == 5
    assert page.sm.get('camera_device_index') == 5
    assert page.sm.get('camera_enabled') is True
    assert page._camera_preview_timer.active
    assert page.unified_overlay_editor.overlay_enabled == ('camera', True)


def test_camera_unavailable_build_capability_fails_consistently(
        camera_settings_page, monkeypatch):
    page = camera_settings_page
    _FakeCameraRecorder.available = False

    page._on_camera_toggled(True)

    assert _FakeCameraRecorder.requests == []
    assert page.sm.get('camera_enabled') is False
    assert page.camera_check.checked is False
    assert not page._camera_preview_timer.active
    assert page.unified_overlay_editor.overlay_enabled == ('camera', False)
    assert page.camera_unavailable_messages


@pytest.mark.parametrize('result', [False, True])
def test_camera_result_after_settings_hide_is_ignored(
        camera_settings_page, qtbot, result):
    page = camera_settings_page
    page.show()
    page._on_camera_toggled(True)
    _device_index, on_result = _FakeCameraRecorder.requests[-1]
    assert callable(on_result)

    page.hide()
    errors_before = list(page.camera_unavailable_messages)
    preview_before = list(page.unified_overlay_editor.events)
    timer_before = list(page._camera_preview_timer.events)
    on_result(result)

    assert page.camera_unavailable_messages == errors_before
    assert page.unified_overlay_editor.events == preview_before
    assert page._camera_preview_timer.events == timer_before


def test_camera_warning_uses_visible_error_bar_with_settings_action(qtbot):
    error_bar = ErrorBar()
    host = SimpleNamespace(
        error_bar=error_bar,
        settings_manager=_Settings(error_notifications_enabled=True),
        _toggle_settings_page=lambda: None,
    )

    main.MainWindow._show_camera_unavailable(host, 'camera detail ' * 100)

    assert error_bar.isVisible()
    assert error_bar._current['title'] == 'CAMERA UNAVAILABLE'
    assert error_bar._current['level'] == 'warning'
    assert error_bar._current['actions'][0][0] == 'OPEN CAMERA SETTINGS'
    assert len(error_bar._detail.text()) == 240
