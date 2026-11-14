"""Generation-bound camera and lazy Profiles integration boundaries."""

from __future__ import annotations

from collections import deque
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest


class _AdmissionSettings:
    """Small data-backed manager with the production admission reader."""

    def __init__(self, **values):
        self.settings = dict(values)
        self.values = self.settings

    def get(self, key, default=None):
        return self.settings.get(key, default)

    def set(self, key, value):
        self.settings[key] = value

    def update(self, values):
        self.settings.update(values)

    def save_settings(self):
        return True

    def read_capture_admission_policy(self):
        from core.settings_manager import SettingsManager

        return SettingsManager.read_capture_admission_policy(self)


def _bind_real_main_helpers(host, main):
    """Attach production admission/camera helpers to a narrow host."""
    for name, descriptor in vars(main.MainWindow).items():
        if not (name.startswith('_admission_')
                or name.startswith('_camera_')):
            continue
        helper = getattr(main.MainWindow, name)
        if isinstance(descriptor, staticmethod):
            setattr(host, name, helper)
        elif callable(helper):
            setattr(host, name, partial(helper, host))
    for name in (
            '_capture_owner_snapshot', '_capture_owner_snapshot_is_current'):
        helper = getattr(main.MainWindow, name)
        setattr(host, name, partial(helper, host))
    return host


def _request(token=1, *, camera_enabled=True, device_index=2,
             profile_name='A'):
    from core.capture_session import CameraSnapshot, SourceSnapshot
    from core.capture_session import build_resolved_capture_request
    from core.capture_settings import CaptureConfig

    config = CaptureConfig(
        fps=60, buffer_seconds=32, width=1920, height=1080,
        bitrate_kbps=25_000, codec='auto', preset=4, monitor='',
        scaling='stretch', audio_enabled=True, encoder='auto',
        multiband_enabled=False, separate_audio_enabled=False,
        microphone_endpoint_id='', playback_endpoint_id='',
        microphone_mono=False, audio_device_tracks='',
        application_audio_tracks_enabled=False, normal_clip_seconds=30,
    )
    return build_resolved_capture_request(
        request_token=token, purpose='clips',
        source=SourceSnapshot(source_mode='desktop', clip_group='Desktop'),
        capture_config=config,
        camera=CameraSnapshot(enabled=camera_enabled, device_index=device_index),
        effects={'capture_card_enabled': True},
        profile_id='12345678-1234-4234-8234-1234567890ab',
        profile_revision=1, profile_name=profile_name,
        profile_document_revision=1,
    )


def _session(request):
    from core.capture_session import bind_capture_session

    return bind_capture_session(
        request, process_generation=7, native_capture_generation=11,
        accepted_config_token=3,
    )


def _bind_main(host, main, *names):
    for name in names:
        descriptor = vars(main.MainWindow).get(name)
        helper = getattr(main.MainWindow, name)
        if isinstance(descriptor, staticmethod):
            setattr(host, name, helper)
        else:
            setattr(host, name, partial(helper, host))
    return host


def test_camera_provider_snapshot_is_frozen_and_extract_rejects_stale_ring(
        monkeypatch):
    from core import camera_recorder

    monkeypatch.setattr(camera_recorder.CameraRecorder, '_instance', None)
    recorder = camera_recorder.CameraRecorder()
    recorder._running = True
    recorder._device_index = 2
    recorder._generation = 9
    recorder._latest_frame = object()
    recorder._chunks = deque([(100.0, b'jpeg')])
    monkeypatch.setattr(
        camera_recorder, '_cv2',
        SimpleNamespace(IMREAD_COLOR=1,
                        imdecode=lambda _buf, _mode: 'frame'),
    )

    snapshot = recorder.runtime_snapshot()
    assert snapshot.device_index == 2
    assert snapshot.generation == 9
    assert snapshot.running is True
    assert snapshot.ready is True
    with pytest.raises(AttributeError):
        snapshot.ready = False
    try:
        assert recorder.extract_segment(
            101.0, 5.0, expected_generation=8,
            expected_device_index=2) is None
        assert recorder.extract_segment(
            101.0, 5.0, expected_generation=9,
            expected_device_index=3) is None
        assert recorder.extract_segment(
            101.0, 5.0, expected_generation=9,
            expected_device_index=2) == ['frame']
    finally:
        recorder.stop()


def test_camera_write_segment_rejects_wrong_generation_without_opening_writer(
        monkeypatch, tmp_path):
    from core import camera_recorder

    monkeypatch.setattr(camera_recorder.CameraRecorder, '_instance', None)
    recorder = camera_recorder.CameraRecorder()
    recorder._running = True
    recorder._device_index = 2
    recorder._generation = 9
    recorder._latest_frame = object()
    recorder._chunks = deque([(100.0, b'jpeg')])
    writer_opened = []

    class _CV2:
        IMREAD_COLOR = 1

        def imdecode(self, _buf, _mode):
            return 'frame'

        def VideoWriter(self, *_args):
            writer_opened.append(True)
            raise AssertionError('stale receipt reached writer')

    monkeypatch.setattr(camera_recorder, '_AVAILABLE', True)
    monkeypatch.setattr(camera_recorder, '_cv2', _CV2())

    try:
        assert recorder.write_segment(
            str(tmp_path / 'camera.mp4'), 101.0, 5.0,
            expected_generation=8, expected_device_index=2,
        ) is False
        assert writer_opened == []
    finally:
        recorder.stop()


def test_main_camera_apply_keeps_warmup_receipt_until_provider_is_ready(
        monkeypatch):
    import main

    from core.camera_recorder import CameraRuntimeSnapshot
    from core.capture_session import CameraSnapshot

    class _WarmupProvider:
        def __init__(self):
            self.callback = None
            self.owner_token = None
            self.request_token = None
            self.ready = False
            self.stopped = False

        @classmethod
        def is_available(cls):
            return True

        def start_async(self, _device_index, on_result=None, *,
                        owner_token=None, request_token=None):
            self.callback = on_result
            self.owner_token = owner_token
            self.request_token = request_token

        def request_stop_async(self, *, expected_generation=None,
                               expected_device_index=None,
                               owner_token=None, request_token=None,
                               on_result=None):
            if (owner_token is not self.owner_token
                    or request_token is not self.request_token
                    or expected_device_index not in (None, 2)
                    or (expected_generation is not None
                        and expected_generation != 20)):
                return False
            self.stopped = True
            if on_result is not None:
                on_result(True)
            return True

        def owned_cleanup_complete(self, *, owner_token):
            return owner_token is self.owner_token and self.stopped

        def runtime_snapshot(self):
            return CameraRuntimeSnapshot(2, 20, True, self.ready)

    camera = CameraSnapshot(enabled=True, device_index=2)
    request = _request(camera_enabled=True, device_index=2)
    host = SimpleNamespace(
        settings_manager=_AdmissionSettings(),
        _camera_apply_generation=0, _camera_pending_request=None,
        _camera_runtime_receipt=None, _accepted_capture_session=None,
        _accepted_camera_runtime_receipt=None, _shutdown_requested=False,
        _shutdown_complete=False, _camera_owner_token=object(),
        _camera_provider=None, _camera_pending_operation=None,
        _camera_retired_operations=[], _camera_active_physical=None,
        _camera_retired_physical=[], _camera_disabled_request=None,
        _camera_disabled_generation=None, _camera_stop_receipts=[],
        _engine_gen=0, _engine_process_generation=None,
        _pending_launch_request=None, _pending_manual_record_request=None,
        _desired_capture_request=request, _admission_epoch=0,
        _admission_policy=None, _admission_policy_error=None,
        _manual_record_state='idle',
        _save_state=SimpleNamespace(is_busy=lambda: False),
        _manual_record_finalize_thread=None, _mux_threads=(),
        _ui_call=SimpleNamespace(emit=lambda callback: callback()),
        status=[], _set_status=lambda text, _style: host.status.append(text),
        _settings_page_widget=SimpleNamespace(),
        _publish_capture_profile_status=None,
        _show_camera_unavailable=lambda _detail: pytest.fail(
            'warmup must not report camera unavailable'),
    )
    host._settings_page_widget.set_profile_capture_status = (
        lambda *_args: None)
    host._refresh_capture_summary = lambda: None
    monkeypatch.setattr('core.camera_recorder.CameraRecorder', _WarmupProvider)
    _bind_real_main_helpers(host, main)
    _bind_main(host, main, '_accepted_profile_status',
               '_publish_capture_profile_status', '_on_camera_apply_result',
               '_capture_resources_busy', '_request_camera_apply')

    assert host._request_camera_apply(
        camera, origin='settings', request_token=request.request_token,
        capture_request=request) is True
    provider = host._camera_provider
    assert isinstance(provider, _WarmupProvider)
    assert callable(provider.callback)
    provider.callback(True)

    assert host._camera_runtime_receipt['generation'] == 20
    assert host._camera_runtime_receipt['ready'] is False
    assert host.status == ['CAMERA WARMING UP']

    provider.ready = True
    fresh = main.MainWindow._camera_runtime_receipt_for_request(host, request)
    assert fresh == {
        'device_index': 2,
        'generation': 20,
        'ready': True,
        'origin': 'settings',
        'request_token': 1,
    }
    assert host._camera_runtime_receipt['ready'] is False


def test_stale_camera_callback_after_B_generation_cannot_change_status_or_device(
        monkeypatch):
    import main

    from core.capture_session import CameraSnapshot

    class _Provider:
        def __init__(self):
            self.callbacks = {}
            self.owned_provider_ledger = {}
            self.stop_calls = []

        @classmethod
        def is_available(cls):
            return True

        def start_async(self, device_index, on_result=None, *,
                        owner_token=None, request_token=None):
            device_index = int(device_index)
            self.callbacks[device_index] = on_result
            self.owned_provider_ledger[id(request_token)] = {
                'owner_token': owner_token,
                'request_token': request_token,
                'device_index': device_index,
                'generation': 54 if device_index == 2 else 55,
                'active': True,
            }

        def request_stop_async(self, *, expected_generation=None,
                               expected_device_index=None,
                               owner_token=None, request_token=None,
                               on_result=None):
            entry = self.owned_provider_ledger.get(id(request_token))
            if (entry is None
                    or entry['owner_token'] is not owner_token
                    or entry['request_token'] is not request_token
                    or not entry['active']):
                return False
            if (expected_device_index is not None
                    and expected_device_index != entry['device_index']
                    or (expected_generation is not None
                        and expected_generation != entry['generation'])):
                return False
            entry['active'] = False
            self.stop_calls.append(entry['device_index'])
            if on_result is not None:
                on_result(True)
            return True

        def owned_cleanup_complete(self, *, owner_token):
            return not any(
                entry['active']
                and entry['owner_token'] is owner_token
                for entry in self.owned_provider_ledger.values())

    camera_a = CameraSnapshot(enabled=True, device_index=2)
    camera_b = CameraSnapshot(enabled=True, device_index=5)
    request_a = _request(1, camera_enabled=True, device_index=2,
                         profile_name='A')
    request_b = _request(2, camera_enabled=True, device_index=5,
                         profile_name='B')
    host = SimpleNamespace(
        settings_manager=_AdmissionSettings(),
        _camera_apply_generation=0, _camera_pending_request=None,
        _camera_runtime_receipt=None,
        _accepted_capture_session=None, _accepted_camera_runtime_receipt=None,
        _shutdown_requested=False, _shutdown_complete=False,
        _camera_owner_token=object(), _camera_provider=None,
        _camera_pending_operation=None, _camera_retired_operations=[],
        _camera_active_physical=None, _camera_retired_physical=[],
        _camera_disabled_request=None, _camera_disabled_generation=None,
        _camera_stop_receipts=[], _engine_gen=0,
        _engine_process_generation=None, _pending_launch_request=None,
        _pending_manual_record_request=None,
        _desired_capture_request=request_a, _admission_epoch=0,
        _admission_policy=None, _admission_policy_error=None,
        _manual_record_state='idle',
        _save_state=SimpleNamespace(is_busy=lambda: False),
        _manual_record_finalize_thread=None, _mux_threads=(),
        _ui_call=SimpleNamespace(emit=lambda callback: callback()),
        status=[],
        _set_status=lambda text, _style: host.status.append(text),
        _settings_page_widget=SimpleNamespace(),
        _publish_capture_profile_status=None,
        _show_camera_unavailable=lambda detail: host.status.append(detail),
    )
    host._settings_page_widget.set_profile_capture_status = (
        lambda *_args: None)
    host._refresh_capture_summary = lambda: None
    monkeypatch.setattr('core.camera_recorder.CameraRecorder', _Provider)
    _bind_real_main_helpers(host, main)
    _bind_main(host, main, '_accepted_profile_status',
               '_publish_capture_profile_status', '_on_camera_apply_result',
               '_capture_resources_busy', '_request_camera_apply')

    assert host._request_camera_apply(
        camera_a, origin='settings', request_token=request_a.request_token,
        capture_request=request_a) is True
    provider = host._camera_provider
    host._desired_capture_request = request_b
    assert host._request_camera_apply(
        camera_b, origin='settings', request_token=request_b.request_token,
        capture_request=request_b) is True

    # B owns generation 2 and its provider is still completing asynchronously.
    # Keep the known B receipt while A's callback is delivered late.
    host._camera_runtime_receipt = {
        'generation': 55, 'device_index': 5,
    }
    before = dict(host._camera_runtime_receipt)
    provider.callbacks[2](False)

    assert host._camera_runtime_receipt == before
    assert host._camera_pending_request is camera_b
    assert host.status == []


def test_main_camera_route_uses_fake_provider_and_detached_result_boundary(
        monkeypatch):
    import main

    from core.camera_recorder import CameraRuntimeSnapshot
    from core.capture_session import CameraSnapshot

    calls = []
    owned_provider_ledger = {}

    class _Provider:
        @classmethod
        def is_available(cls):
            return True

        def start_async(self, device_index, on_result=None, *,
                        owner_token=None, request_token=None):
            assert owner_token is not None
            assert request_token is not None
            entry = {
                'owner_token': owner_token,
                'request_token': request_token,
                'device_index': int(device_index),
                'generation': 31,
                'active': True,
            }
            owned_provider_ledger[id(request_token)] = entry
            calls.append(('start', device_index))
            on_result(True)

        def request_stop_async(self, *, expected_generation=None,
                               expected_device_index=None,
                               owner_token=None, request_token=None,
                               on_result=None):
            entry = owned_provider_ledger.get(id(request_token))
            if (entry is None or entry['owner_token'] is not owner_token
                    or entry['request_token'] is not request_token
                    or (expected_generation is not None
                        and expected_generation != entry['generation'])
                    or expected_device_index != entry['device_index']
                    or not entry['active']):
                return False
            entry['active'] = False
            if on_result is not None:
                on_result(True)
            return True

        def owned_cleanup_complete(self, *, owner_token):
            return not any(
                entry['active'] and entry['owner_token'] is owner_token
                for entry in owned_provider_ledger.values())

        def runtime_snapshot(self):
            return CameraRuntimeSnapshot(2, 31, True, True)

    monkeypatch.setattr('core.camera_recorder.CameraRecorder', _Provider)
    camera = CameraSnapshot(enabled=True, device_index=2)
    host = SimpleNamespace(
        settings_manager=_AdmissionSettings(),
        _camera_apply_generation=0, _camera_pending_request=None,
        _camera_runtime_receipt=None, _accepted_capture_session=None,
        _accepted_camera_runtime_receipt=None, _shutdown_requested=False,
        _shutdown_complete=False, _camera_owner_token=object(),
        _camera_provider=None, _camera_pending_operation=None,
        _camera_retired_operations=[], _camera_active_physical=None,
        _camera_retired_physical=[], _camera_disabled_request=None,
        _camera_disabled_generation=None, _camera_stop_receipts=[],
        _engine_gen=0, _engine_process_generation=None,
        _pending_launch_request=None, _pending_manual_record_request=None,
        _desired_capture_request=None, _admission_epoch=0,
        _admission_policy=None, _admission_policy_error=None,
        _manual_record_state='idle',
        _save_state=SimpleNamespace(is_busy=lambda: False),
        _manual_record_finalize_thread=None, _mux_threads=(),
        _settings_page_widget=SimpleNamespace(), status=[],
        _ui_call=SimpleNamespace(emit=lambda callback: callback()),
        _set_status=lambda text, _style: host.status.append(text),
        _publish_capture_profile_status=None,
        _show_camera_unavailable=lambda detail: host.status.append(detail),
    )
    host._settings_page_widget.set_profile_capture_status = (
        lambda *_args: None)
    host._refresh_capture_summary = lambda: None
    request = _request(7, camera_enabled=True, device_index=2)
    host._desired_capture_request = request
    _bind_real_main_helpers(host, main)
    _bind_main(host, main, '_accepted_profile_status',
               '_publish_capture_profile_status', '_on_camera_apply_result',
               '_capture_resources_busy', '_request_camera_apply')

    assert host._request_camera_apply(
        camera, origin='profile', request_token=7,
        capture_request=request) is True
    assert calls == [('start', 2)]
    assert host._camera_runtime_receipt == {
        'device_index': 2, 'generation': 31, 'ready': True,
        'origin': 'profile', 'request_token': 7,
    }


def test_camera_change_while_save_busy_is_queued_without_switching_A():
    import main

    request_a = _request(1, profile_name='A')
    request_b = _request(2, profile_name='B', device_index=5)
    request_c = _request(3, profile_name='C', device_index=7)
    host = SimpleNamespace(
        settings_manager=_AdmissionSettings(),
        _file_operations_ready=True, _manual_record_state='idle',
        _shutdown_requested=False, _restart_pending=False,
        _capture_settings_applying=False, _desired_capture_request=request_a,
        _pending_capture_reason='', statuses=[],
        _capture_request_intents={2: 101, 3: 102},
        _latest_capture_intent_id=102,
        _accepted_capture_session=_session(request_a),
        _save_state=SimpleNamespace(is_busy=lambda: True),
        _set_capture_apply_state=lambda _value: None,
        _set_status=lambda text, _style: host.statuses.append(text),
        _settings_page_widget=SimpleNamespace(
            set_profile_capture_status=lambda *_args: None),
        stop_engine=lambda: pytest.fail('busy camera change stopped A'),
    )
    host._admission_epoch = 0
    host._admission_policy = None
    host._admission_policy_error = None
    host._refresh_capture_summary = lambda: None
    _bind_real_main_helpers(host, main)
    _bind_main(host, main, '_accepted_profile_status',
               '_publish_capture_profile_status', '_capture_resources_busy',
               '_capture_request_intent_id', '_queue_latest_desired_request',
               '_restart_capture_engine')

    host._restart_capture_engine(_prepared_request=request_b)
    host._restart_capture_engine(_prepared_request=request_c)
    assert host._desired_capture_request is request_c
    assert host.statuses == ['CAPTURE CHANGE PENDING'] * 2


def test_camera_overlay_uses_session_request_and_job_receipt_before_globals(
        monkeypatch, tmp_path):
    import main
    from core import camera_recorder

    request = _request(1, camera_enabled=True, device_index=2)
    session = _session(request)
    job = {
        'session_snapshot': session,
        'camera_runtime_receipt': {'generation': 9, 'device_index': 2},
    }
    calls = []

    class _Provider:
        @classmethod
        def is_available(cls):
            return True

        def runtime_snapshot(self):
            return SimpleNamespace(device_index=2, generation=9,
                                   running=True, ready=True)

        def write_segment(self, path, end_time, duration, fps, *,
                          expected_generation=None, expected_device_index=None):
            calls.append((path, end_time, duration, fps,
                          expected_generation, expected_device_index))
            Path(path).write_bytes(b'camera')
            return True

    monkeypatch.setattr(camera_recorder, 'CameraRecorder', _Provider)
    ffmpeg_calls = []
    monkeypatch.setattr(
        main.subprocess, 'run',
        lambda args, **_kwargs: (ffmpeg_calls.append(args)
                                  or SimpleNamespace(returncode=1, stderr=b'no')),
    )
    warnings = []
    base = tmp_path / 'base.mp4'
    base_bytes = b'owned base clip'
    base.write_bytes(base_bytes)
    settings_writes = []

    def settings_set(*args):
        settings_writes.append(('set', args))

    def settings_save():
        settings_writes.append(('save', ()))
        return True

    host = SimpleNamespace(
        settings_manager=SimpleNamespace(
            get=lambda key, default=None: {
                'camera_enabled': False,
                'camera_position': 'top-left',
                'camera_size': 'small',
            }.get(key, default)),
        _settings_set=settings_set,
        _settings_save=settings_save,
        _clip_dimensions=lambda *_args: (1920, 1080),
        _record_finalization_warning=lambda path, message: warnings.append(
            (path, message)),
        _replace_finalization_output=lambda *_args: None,
        _rebind_manifest_after_owned_replace=lambda *_args: None,
        _ui_call=SimpleNamespace(emit=lambda callback: callback()),
        push_error=lambda *_args, **_kwargs: None,
    )
    host.settings_manager.set = settings_set
    host.settings_manager.save_settings = settings_save
    had_previous = hasattr(main._FINALIZATION_CONTEXT, 'capture_job')
    previous_job = getattr(main._FINALIZATION_CONTEXT, 'capture_job', None)
    try:
        main._FINALIZATION_CONTEXT.capture_job = job
        assert main._FINALIZATION_CONTEXT.capture_job is job
        main.MainWindow._apply_camera_overlay(
            host, str(base), 'ffmpeg', 100.0, 30,
        )
    finally:
        if had_previous:
            main._FINALIZATION_CONTEXT.capture_job = previous_job
        elif hasattr(main._FINALIZATION_CONTEXT, 'capture_job'):
            del main._FINALIZATION_CONTEXT.capture_job

    assert calls and calls[0][4:] == (9, 2)
    assert ffmpeg_calls
    assert warnings
    assert base.read_bytes() == base_bytes
    assert settings_writes == []
    assert (hasattr(main._FINALIZATION_CONTEXT, 'capture_job')
            and main._FINALIZATION_CONTEXT.capture_job is previous_job) or (
                not had_previous
                and not hasattr(main._FINALIZATION_CONTEXT, 'capture_job'))


def test_unavailable_camera_keeps_base_clip_and_saved_preference(
        monkeypatch, tmp_path):
    import main
    from core import camera_recorder

    request = _request(1, camera_enabled=True, device_index=5)
    job = {
        'session_snapshot': _session(request),
        'camera_runtime_receipt': {'generation': 9, 'device_index': 5},
    }
    warnings = []
    base = tmp_path / 'base.mp4'
    base_bytes = b'base remains byte-for-byte unchanged'
    base.write_bytes(base_bytes)
    settings_writes = []

    def settings_set(*args):
        settings_writes.append(('set', args))

    def settings_save():
        settings_writes.append(('save', ()))
        return True

    class _Unavailable:
        @classmethod
        def is_available(cls):
            return False

        def runtime_snapshot(self):
            return SimpleNamespace(device_index=5, generation=9,
                                   running=False, ready=False)

    monkeypatch.setattr(camera_recorder, 'CameraRecorder', _Unavailable)
    host = SimpleNamespace(
        settings_manager=SimpleNamespace(
            get=lambda key, default=None: {
                'camera_enabled': True, 'camera_device_index': 5,
            }.get(key, default)),
        _record_finalization_warning=lambda path, message: warnings.append(
            (path, message)),
    )
    host.settings_manager.set = settings_set
    host.settings_manager.save_settings = settings_save
    had_previous = hasattr(main._FINALIZATION_CONTEXT, 'capture_job')
    previous_job = getattr(main._FINALIZATION_CONTEXT, 'capture_job', None)
    try:
        main._FINALIZATION_CONTEXT.capture_job = job
        assert main._FINALIZATION_CONTEXT.capture_job is job
        main.MainWindow._apply_camera_overlay(
            host, str(base), 'ffmpeg', 100.0, 30,
        )
    finally:
        if had_previous:
            main._FINALIZATION_CONTEXT.capture_job = previous_job
        elif hasattr(main._FINALIZATION_CONTEXT, 'capture_job'):
            del main._FINALIZATION_CONTEXT.capture_job

    assert warnings
    assert 'base clip' in warnings[-1][1].lower() or 'unavailable' in warnings[-1][1].lower()
    assert base.read_bytes() == base_bytes
    assert settings_writes == []
    assert (hasattr(main._FINALIZATION_CONTEXT, 'capture_job')
            and main._FINALIZATION_CONTEXT.capture_job is previous_job) or (
                not had_previous
                and not hasattr(main._FINALIZATION_CONTEXT, 'capture_job'))


def test_settings_profiles_is_lazy_index_7_and_old_category_indices_remain(
        qtbot, monkeypatch):
    import main
    from PySide6.QtWidgets import QWidget

    class _Settings:
        def get(self, _key, default=None):
            return default

    built = []

    def page_builder(index):
        def build(self):
            built.append(index)
            return QWidget()
        return build

    build_clip_page = main._SettingsPage._make_clip_page

    def track_clip_page(self):
        built.append(1)
        return build_clip_page(self)

    for index, name in enumerate((
            '_make_general_page', '_make_clip_page', '_make_audio_page',
            '_make_visuals_page', '_make_customize_page',
            '_make_performance_page', '_make_shortcuts_page')):
        monkeypatch.setattr(
            main._SettingsPage, name,
            track_clip_page if index == 1 else page_builder(index))
    monkeypatch.setattr(main._SettingsPage, '_make_profiles_page',
                        page_builder(7))
    monkeypatch.setattr(main._SettingsPage, '_make_capture_admission_page',
                        page_builder(8))
    monkeypatch.setattr(main._SettingsPage, '_apply_styles', lambda _self: None)
    monkeypatch.setattr(main, 'PresetsManager', lambda: object())
    page = main._SettingsPage(_Settings())
    qtbot.addWidget(page)

    assert page._cat_titles[:7] == [
        'General', 'Clip', 'Audio', 'Visuals',
        'Customize', 'Performance', 'Shortcuts',
    ]
    assert page._cat_titles[7] == 'Profiles'
    assert built == [0]
    page._ensure_category(7)
    assert built == [0, 1, 7]
    assert page._cat_titles[8] == 'Allowed games'
    page._ensure_category(8)
    assert built == [0, 1, 7, 8]
    assert page._cat_titles[9] == 'Plugins'
    assert page._cat_titles[10] == 'Storage'
    assert page.stack.count() == len(page._cat_titles)


def test_real_profiles_factory_builds_index_7_without_starting_inventory_providers(
        qtbot, monkeypatch, tmp_path):
    import main
    from PySide6.QtWidgets import QWidget
    from core.game_profiles import empty_catalog, STORAGE_KEY

    settings = main.SettingsManager.__new__(main.SettingsManager)
    settings.config_file = tmp_path / 'settings.json'
    settings.settings = {STORAGE_KEY: empty_catalog().to_dict()}
    settings._navigation_bindings_migrated = False
    settings._retired_settings_removed = False

    presets = main.PresetsManager.__new__(main.PresetsManager)
    presets._path = tmp_path / 'presets.json'

    monkeypatch.setattr(main.QTimer, 'singleShot', lambda *_args: None)
    monkeypatch.setattr(
        main._SettingsPage, '_populate_profile_mic_devices', lambda _self: None)
    monkeypatch.setattr(
        main._SettingsPage, '_populate_playback_devices', lambda _self: None)
    monkeypatch.setattr(
        main._SettingsPage, '_populate_camera_devices', lambda _self: None)
    monkeypatch.setattr(main, 'PresetsManager', lambda: presets)
    monkeypatch.setattr(main._SettingsPage, '_make_audio_page',
                        lambda _self: pytest.fail('Audio page was built'))
    monkeypatch.setattr(main._SettingsPage, '_make_visuals_page',
                        lambda _self: pytest.fail('Visuals page was built'))
    page = main._SettingsPage(settings)
    qtbot.addWidget(page)
    page._ensure_category(7)

    assert page._cat_titles[7] == 'Profiles'
    assert page.profiles_widget is not None
    assert isinstance(page.profiles_widget, QWidget)
    assert 7 in page._loaded_categories
    assert 2 not in page._loaded_categories
    assert 3 not in page._loaded_categories


def test_profiles_inventory_forwarding_works_without_audio_visuals_pages(
        qtbot, monkeypatch):
    import main

    class _Settings:
        def get(self, _key, default=None):
            return default

    monkeypatch.setattr(main._SettingsPage, '_setup_ui', lambda _self: None)
    monkeypatch.setattr(main._SettingsPage, '_apply_styles', lambda _self: None)
    monkeypatch.setattr(main, 'PresetsManager', lambda: object())
    page = main._SettingsPage(_Settings())
    qtbot.addWidget(page)
    received = []
    page._profile_microphones = [{
        'endpoint_id': 'saved-mic', 'display_name': 'Saved microphone',
        'is_default': False, 'available': False,
    }]
    page._profile_playback_devices = []
    page.profiles_widget = SimpleNamespace(
        set_audio_devices=lambda microphones, playback: received.append(
            (microphones, playback)),
    )

    page._publish_profile_audio_inventory()

    assert received == [([
        {
            'endpoint_id': 'saved-mic', 'display_name': 'Saved microphone',
            'is_default': False, 'available': False,
        },
    ], [])]


def test_stale_camera_inventory_result_does_not_replace_current_cache(
        qtbot, monkeypatch):
    import main
    from PySide6.QtWidgets import QWidget

    class _Settings:
        def get(self, _key, default=None):
            return default

    monkeypatch.setattr(main._SettingsPage, '_setup_ui', lambda _self: None)
    monkeypatch.setattr(main._SettingsPage, '_apply_styles', lambda _self: None)
    monkeypatch.setattr(main, 'PresetsManager', lambda: object())
    owner = QWidget()
    owner._on_camera_inventory_ready = lambda *_args: None
    qtbot.addWidget(owner)
    page = main._SettingsPage(_Settings(), parent=owner)
    qtbot.addWidget(page)
    page._camera_inventory_generation = 2
    page._camera_inventory_cache = [{'index': 2, 'display_name': 'Current'}]

    page._on_camera_inventory_ready(
        [{'index': 1, 'display_name': 'Old'}], '', 1)
    assert page._camera_inventory_cache == [{'index': 2, 'display_name': 'Current'}]
    page._on_camera_inventory_ready(
        [{'index': 5, 'display_name': 'Current 2'}], '', 2)
    assert page._camera_inventory_cache == [
        {'index': 5, 'display_name': 'Current 2'},
    ]


def test_stale_playback_inventory_result_is_dropped_before_profile_forwarding(
        qtbot, monkeypatch):
    import main

    class _Settings:
        def get(self, _key, default=None):
            return default

    monkeypatch.setattr(main._SettingsPage, '_setup_ui', lambda _self: None)
    monkeypatch.setattr(main._SettingsPage, '_apply_styles', lambda _self: None)
    monkeypatch.setattr(main, 'PresetsManager', lambda: object())
    page = main._SettingsPage(_Settings())
    qtbot.addWidget(page)
    page._profile_playback_inventory_generation = 2
    applied = []
    page._apply_playback_devices = lambda endpoints, successful: applied.append(
        (endpoints, successful))

    page._on_profile_playback_inventory_ready(['old'], True, 1)
    page._on_profile_playback_inventory_ready(['current'], True, 2)

    assert applied == [(['current'], True)]


def test_profile_audio_cache_keeps_mic_and_playback_results_in_both_orders(
        qtbot, monkeypatch):
    import main
    from core.windows_microphone_devices import MicrophoneDiscoveryResult
    from core.windows_microphone_devices import WindowsMicrophoneEndpoint

    class _Settings:
        def get(self, _key, default=None):
            return default

    monkeypatch.setattr(main._SettingsPage, '_setup_ui', lambda _self: None)
    monkeypatch.setattr(main._SettingsPage, '_apply_styles', lambda _self: None)
    monkeypatch.setattr(main, 'PresetsManager', lambda: object())
    def new_page():
        page = main._SettingsPage(_Settings())
        qtbot.addWidget(page)
        page._profile_mic_inventory_generation = 2
        page._profile_playback_inventory_generation = 2
        forwarded = []
        page.profiles_widget = SimpleNamespace(
            set_audio_devices=lambda microphones, playback: forwarded.append(
                (microphones, playback)),
        )
        return page, forwarded

    page, forwarded = new_page()
    endpoint = WindowsMicrophoneEndpoint(
        'mic-a', 'Microphone A', 1, False)
    mic_result = MicrophoneDiscoveryResult(
        native_endpoints=(endpoint,), legacy_indices={},
        native_error=None, legacy_error=None, generation=2)

    # Playback first, then microphone; stale microphone is ignored.
    page._on_profile_mic_inventory_ready(mic_result, None, 1)
    page._on_profile_playback_inventory_ready(['old'], True, 1)
    page._on_profile_playback_inventory_ready(
        [SimpleNamespace(endpoint_id='play-a', display_name='Playback A',
                         is_default=True, is_active=True)], True, 2)
    page._on_profile_mic_inventory_ready(mic_result, None, 2)
    assert forwarded[-1][0][0]['endpoint_id'] == 'mic-a'
    assert forwarded[-1][1][0]['endpoint_id'] == 'play-a'

    # Reverse delivery keeps the cached microphone while the stale playback
    # result is dropped, without materializing Audio or Visuals categories.
    page, forwarded = new_page()
    page._profile_mic_inventory_generation = 4
    page._profile_playback_inventory_generation = 4
    mic_result_b = MicrophoneDiscoveryResult(
        native_endpoints=(WindowsMicrophoneEndpoint(
            'mic-b', 'Microphone B', 1, False),),
        legacy_indices={}, native_error=None, legacy_error=None, generation=4)
    page._on_profile_mic_inventory_ready(mic_result_b, None, 4)
    page._on_profile_playback_inventory_ready(['stale'], True, 3)
    assert forwarded[-1][0][0]['endpoint_id'] == 'mic-b'
    assert forwarded[-1][1] == []
    page._on_profile_playback_inventory_ready(
        [SimpleNamespace(endpoint_id='play-b', display_name='Playback B',
                         is_default=False, is_active=True)], True, 4)
    assert forwarded[-1][0][0]['endpoint_id'] == 'mic-b'
    assert forwarded[-1][1][0]['endpoint_id'] == 'play-b'


def test_profiles_page_drops_stale_callbacks_before_publication_both_orders(
        qtbot, monkeypatch):
    """Requested generation guards preserve the other detached cache."""
    import main
    from core.windows_microphone_devices import MicrophoneDiscoveryResult
    from core.windows_microphone_devices import WindowsMicrophoneEndpoint

    class _Settings:
        def get(self, _key, default=None):
            return default

    monkeypatch.setattr(main._SettingsPage, '_setup_ui', lambda _self: None)
    monkeypatch.setattr(main._SettingsPage, '_apply_styles', lambda _self: None)
    monkeypatch.setattr(main, 'PresetsManager', lambda: object())

    def new_page():
        page = main._SettingsPage(_Settings())
        qtbot.addWidget(page)
        page._profile_mic_inventory_generation = 2
        page._profile_playback_inventory_generation = 2
        page._profile_microphones = [{
            'endpoint_id': 'mic-a', 'display_name': 'Microphone A',
            'is_default': True, 'available': True,
        }]
        page._profile_playback_devices = [{
            'endpoint_id': 'play-a', 'display_name': 'Playback A',
            'is_default': True, 'available': True,
        }]
        published = []
        page.profiles_widget = SimpleNamespace(
            set_audio_devices=lambda microphones, playback: published.append(
                (microphones, playback)),
        )
        return page, published

    mic_result = MicrophoneDiscoveryResult(
        native_endpoints=(WindowsMicrophoneEndpoint(
            'mic-b', 'Microphone B', 1, False),),
        legacy_indices={}, native_error=None, legacy_error=None, generation=2)
    play_result = [SimpleNamespace(
        endpoint_id='play-b', display_name='Playback B',
        is_default=False, is_active=True)]

    page, published = new_page()
    page._on_profile_mic_inventory_ready(mic_result, None, 1)
    assert published == []
    assert page._profile_microphones[0]['endpoint_id'] == 'mic-a'
    page._on_profile_playback_inventory_ready(play_result, True, 2)
    assert published[-1][0][0]['endpoint_id'] == 'mic-a'
    assert published[-1][1][0]['endpoint_id'] == 'play-b'
    page._on_profile_mic_inventory_ready(mic_result, None, 2)
    assert published[-1][0][0]['endpoint_id'] == 'mic-b'
    assert published[-1][1][0]['endpoint_id'] == 'play-b'

    page, published = new_page()
    page._on_profile_playback_inventory_ready(['stale'], True, 1)
    assert published == []
    assert page._profile_playback_devices[0]['endpoint_id'] == 'play-a'
    page._on_profile_mic_inventory_ready(mic_result, None, 2)
    assert published[-1][0][0]['endpoint_id'] == 'mic-b'
    assert published[-1][1][0]['endpoint_id'] == 'play-a'
    page._on_profile_playback_inventory_ready(play_result, True, 2)
    assert published[-1][0][0]['endpoint_id'] == 'mic-b'
    assert published[-1][1][0]['endpoint_id'] == 'play-b'
