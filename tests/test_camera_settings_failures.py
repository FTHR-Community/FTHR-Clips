from __future__ import annotations

import importlib
import threading
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QLineEdit, QWidget

from FTHR_UI import main
from FTHR_UI.ui.error_bar import ErrorBar


class _Settings:
    def __init__(self, **values):
        self.settings = dict(values)
        self.values = self.settings
        self.save_count = 0

    def get(self, key, default=None):
        return self.settings.get(key, default)

    def set(self, key, value):
        self.settings[key] = value

    def save_settings(self):
        self.save_count += 1

    def read_capture_admission_policy(self):
        from core.settings_manager import SettingsManager

        return SettingsManager.read_capture_admission_policy(self)


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


class _UiDispatcher(QObject):
    callback_ready = Signal(object)

    def __init__(self, parent):
        super().__init__(parent)
        self.callback_ready.connect(self._run)

    def _run(self, callback):
        callback()

    def emit(self, callback):
        self.callback_ready.emit(callback)


class _FakeCameraRecorder:
    available = True
    requests = []
    stop_count = 0
    last_device = 0
    generation = 0
    owned_provider_ledger = {}

    @classmethod
    def is_available(cls):
        return cls.available

    def start_async(self, device_index, on_result=None, *, owner_token=None,
                    request_token=None):
        type(self).last_device = int(device_index)
        type(self).generation += 1
        type(self).owned_provider_ledger[id(request_token)] = {
            'owner_token': owner_token,
            'request_token': request_token,
            'device_index': int(device_index),
            'generation': type(self).generation,
            'active': True,
        }
        type(self).requests.append((device_index, on_result))

    def stop(self):
        type(self).stop_count += 1

    def request_stop_async(self, *, expected_generation=None,
                           expected_device_index=None, owner_token=None,
                           request_token=None, on_result=None):
        entry = type(self).owned_provider_ledger.get(id(request_token))
        if (entry is None or entry['owner_token'] is not owner_token
                or entry['request_token'] is not request_token
                or (expected_generation is not None
                    and expected_generation != entry['generation'])
                or expected_device_index != entry['device_index']
                or not entry['active']):
            return False
        entry['active'] = False
        type(self).stop_count += 1
        if on_result is not None:
            on_result(True)
        return True

    def owned_cleanup_complete(self, *, owner_token):
        return not any(
            entry['active'] and entry['owner_token'] is owner_token
            for entry in type(self).owned_provider_ledger.values())

    def runtime_snapshot(self):
        return SimpleNamespace(
            running=True, ready=True,
            device_index=type(self).last_device,
            generation=type(self).generation or 1)


@pytest.fixture
def camera_settings_page(qtbot, monkeypatch):
    _FakeCameraRecorder.available = True
    _FakeCameraRecorder.requests = []
    _FakeCameraRecorder.stop_count = 0
    _FakeCameraRecorder.last_device = 0
    _FakeCameraRecorder.generation = 0
    _FakeCameraRecorder.owned_provider_ledger = {}
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
    owner = QWidget()
    qtbot.addWidget(owner)
    page = main._SettingsPage(settings, parent=owner)
    page.settings_search = QLineEdit(page)
    page.camera_check = _CheckBox(False)
    page.camera_device_combo = _ComboBox([0, 2, 5])
    page.stack = _Stack()
    page._camera_preview_timer = _Timer()
    page.unified_overlay_editor = _Preview()
    page.camera_unavailable_messages = []
    if hasattr(page, 'camera_unavailable'):
        page.camera_unavailable.connect(
            page.camera_unavailable_messages.append)
    owner._settings_page_widget = page
    owner.settings_manager = page.sm
    owner.camera_unavailable_messages = []
    owner._manual_record_state = 'idle'
    owner._mux_threads = ()
    owner._save_state = SimpleNamespace(is_busy=lambda: False)
    owner._shutdown_requested = False
    owner._shutdown_complete = False
    owner._camera_apply_generation = 0
    owner._camera_pending_request = None
    owner._camera_runtime_receipt = None
    owner._camera_owner_token = object()
    owner._camera_provider = None
    owner._camera_pending_operation = None
    owner._camera_retired_operations = []
    owner._camera_active_physical = None
    owner._camera_retired_physical = []
    owner._camera_disabled_request = None
    owner._camera_disabled_generation = None
    owner._camera_stop_receipts = []
    owner._engine_gen = 0
    owner._engine_process_generation = None
    owner._pending_launch_request = None
    owner._pending_manual_record_request = None
    owner._desired_capture_request = None
    owner._admission_epoch = 0
    owner._admission_policy = None
    owner._admission_policy_error = None
    owner._ui_call = _UiDispatcher(owner)
    owner._set_status = lambda *_args, **_kwargs: None
    owner._publish_capture_profile_status = lambda *_args, **_kwargs: None
    owner._show_camera_unavailable = (
        lambda detail: owner.camera_unavailable_messages.append(str(detail)))
    owner._on_camera_apply_result = (
        main.MainWindow._on_camera_apply_result.__get__(owner))
    owner._manual_prepare_owns_capture = (
        main.MainWindow._manual_prepare_owns_capture.__get__(owner))
    owner._capture_resources_busy = (
        main.MainWindow._capture_resources_busy.__get__(owner))
    for name, descriptor in vars(main.MainWindow).items():
        if not (name.startswith('_admission_')
                or name.startswith('_camera_')):
            continue
        helper = getattr(main.MainWindow, name)
        if isinstance(descriptor, staticmethod):
            setattr(owner, name, helper)
        elif callable(helper):
            setattr(owner, name, helper.__get__(owner))
    owner._capture_owner_snapshot = (
        main.MainWindow._capture_owner_snapshot.__get__(owner))
    owner._capture_owner_snapshot_is_current = (
        main.MainWindow._capture_owner_snapshot_is_current.__get__(owner))

    def route(payload):
        from core.capture_session import SourceSnapshot
        from core.capture_session import build_resolved_capture_request
        from core.capture_settings import CaptureConfig

        camera = main.CameraSnapshot(
            enabled=bool(payload.get('enabled', False)),
            device_index=int(payload.get('device_index', 0) or 0),
            overlay_rect=payload.get(
                'overlay_rect', main.DEFAULT_OVERLAY_RECT),
        )
        config = CaptureConfig(
            fps=60, buffer_seconds=32, width=1920, height=1080,
            bitrate_kbps=25_000, codec='auto', preset=4, monitor='',
            scaling='stretch', audio_enabled=True, encoder='auto',
            normal_clip_seconds=30,
        )
        owner._capture_request_token = int(
            getattr(owner, '_capture_request_token', 0)) + 1
        request = build_resolved_capture_request(
            request_token=owner._capture_request_token, purpose='clips',
            source=SourceSnapshot(source_mode='desktop', clip_group='Desktop'),
            capture_config=config, camera=camera,
            effects={'capture_card_enabled': True},
        )
        owner._desired_capture_request = request
        main.MainWindow._request_camera_apply(
            owner, camera, origin='settings',
            request_token=request.request_token, capture_request=request)

    page.camera_settings_requested.connect(route)
    owner.show()
    page.show()
    qtbot.waitUntil(page.isVisible, timeout=1000)
    return page


def test_camera_toggle_failure_preserves_intent_and_reports(
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

    assert page.sm.get('camera_enabled') is True
    assert page.camera_check.checked is True
    assert page.camera_unavailable_messages
    assert page.parentWidget().camera_unavailable_messages
    assert 'connected' in page.camera_unavailable_messages[-1].lower()
    assert len(page.camera_unavailable_messages[-1]) <= 240
    assert not page._camera_preview_timer.active
    assert page.unified_overlay_editor.overlay_enabled == ('camera', False)
    assert page.unified_overlay_editor.frame.isNull()
    assert {event[-1] for event in page.camera_check.mutations} == {ui_thread}
    assert {event[-1] for event in page.unified_overlay_editor.events} == {ui_thread}
    assert {event[-1] for event in page._camera_preview_timer.events} == {ui_thread}


def test_device_change_failure_keeps_selected_device_and_intent(
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
    assert page.sm.get('camera_enabled') is True
    assert page.camera_check.checked is True
    assert page.sm.get('camera_overlay_rect') == before
    assert page.sm.get('camera_position') == 'top-left'
    assert page.unified_overlay_editor.overlay_enabled == ('camera', False)
    assert page.unified_overlay_editor.frame.isNull()
    assert not page._camera_preview_timer.active
    assert page.camera_unavailable_messages
    assert page.parentWidget().camera_unavailable_messages


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
    assert page.parentWidget().camera_unavailable_messages == []
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
    assert page.sm.get('camera_enabled') is True
    assert page.camera_check.checked is True
    assert not page._camera_preview_timer.active
    assert page.unified_overlay_editor.overlay_enabled == ('camera', False)
    assert page.parentWidget().camera_unavailable_messages


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

    # Main keeps the warning available even after the settings page is hidden;
    # the hidden page itself must not mutate preview or timer state.
    assert page.camera_unavailable_messages == errors_before
    assert page.unified_overlay_editor.events == preview_before
    assert page._camera_preview_timer.events == timer_before
    if result is False:
        assert page.parentWidget().camera_unavailable_messages


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
