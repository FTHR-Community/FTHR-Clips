from __future__ import annotations

from functools import partial
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from core.capture_settings import CaptureConfig, CaptureConfigTracker
from core.error_recovery import RecoveryTracker


class _Settings:
    def __init__(self, **values):
        self.settings = {
            'capture_mode': 'desktop',
            'target_hwnd': 0,
            'mic_volume': 100,
            'target_window_identity': None,
            'capture_source_authority': 'manual',
        }
        self.settings.update(values)
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


class _Process:
    stdout = object()
    returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = 0


class _StartupCapture:
    def __init__(self, text=''):
        self.text = text

    def attach(self, _stream):
        pass

    def startup_text(self):
        return self.text

    def close(self):
        pass


class _Bridge:
    def __init__(self, initialize_result=False, after_initialize=None):
        self.initialize_result = initialize_result
        self.after_initialize = after_initialize
        self.shutdown_calls = 0
        self._layout = SimpleNamespace(nvenc_active=False)

    def initialize(self):
        if self.after_initialize is not None:
            self.after_initialize()
        return self.initialize_result

    def is_connected(self):
        return False

    def shutdown(self):
        self.shutdown_calls += 1

    def request_engine_shutdown(self):
        return False

    def get_active_codec(self):
        return ''


def _host(main, bridge, *, startup_text='', queue_ui=True):
    config = CaptureConfig(
        fps=60, buffer_seconds=32, width=1920, height=1080,
        bitrate_kbps=25_000, codec='h264', preset=4,
        monitor=r'\\?\display#amd-primary', scaling='stretch',
        audio_enabled=False,
    )
    queued = []
    errors = []
    statuses = []
    summary_button = SimpleNamespace(text='', tooltip='')
    summary_button.setText = lambda value: setattr(
        summary_button, 'text', str(value))
    summary_button.setToolTip = lambda value: setattr(
        summary_button, 'tooltip', str(value))
    # Recovery cases intentionally mutate or raise from this seam.  The
    # adapter below keeps that behavior while supplying start_engine's real
    # immutable request boundary.
    def requested_capture_config():
        return config

    host = SimpleNamespace(
        _engine_gen=0,
        _engine_process_generation=None,
        _diagnostic_engine_start_count=0,
        _diagnostics=None,
        _capture_config=CaptureConfigTracker(),
        _requested_capture_config=requested_capture_config,
        _capture_request_token=0,
        _capture_request_intents={},
        _capture_intent_revision=0,
        _latest_capture_intent_id=None,
        _desired_capture_intent_id=None,
        _latest_capture_intent_state='none',
        _latest_capture_intent_consumed=True,
        _desired_capture_request=None,
        _pending_capture_reason='',
        _source_intent_revision=0,
        _active_game_window=None,
        _engine_profile='clips',
        _shutdown_complete=False,
        _file_operations_ready=True,
        _diagnostic_capture_config=main.MainWindow._diagnostic_capture_config,
        _restart_pending=False,
        _restart_capture_engine=lambda: None,
        _restart_deferred_for_save=False,
        _engine_restart_requested=False,
        _capture_settings_applying=False,
        _camera_runtime_receipt=None,
        _accepted_capture_session=None,
        _accepted_camera_runtime_receipt=None,
        _pending_manual_record_request=None,
        _pending_manual_record_path=None,
        _manual_record_retry_pending=False,
        _background_ui_paused=False,
        _capture_health_snapshot=None,
        _last_capture_health_event_at=main.time.monotonic(),
        _last_native_capture_generation=None,
        _reconnect_counter=0,
        is_capturing=False,
        _capture_health_log=SimpleNamespace(
            info=lambda *_args, **_kwargs: None,
            warning=lambda *_args, **_kwargs: None),
        _advance_recoverable_errors=lambda **_kwargs: None,
        _continue_prepared_manual_recording=lambda: None,
        _settings_page_widget=SimpleNamespace(),
        cap_btn=summary_button,
        _set_status=lambda text, _style: statuses.append(text),
        push_error=lambda *args, **kwargs: errors.append((args, kwargs)),
        engine_path=Path(sys.executable),
        settings_manager=_Settings(),
        _capture_health=SimpleNamespace(
            reset=lambda preserve_recovery_budget: None),
        _close_engine_startup_output=lambda: None,
        _engine_startup_output=None,
        engine_process=None,
        bridge=bridge,
        _recoverable_error_incidents={},
        _error_recovery_tracker=RecoveryTracker(),
        _pending_launch_config=None,
        _pending_launch_generation=None,
        _pending_launch_token=None,
        _pending_launch_request=None,
        _pending_launch_intent_id=None,
        _engine_handshake_generation=None,
        _manual_record_state='idle',
        _ui_ready=False,
        _set_status_history=statuses,
        _errors=errors,
        _queued_callbacks=queued,
        _startup_text=startup_text,
        _fail_manual_recording=lambda *_args, **_kwargs: None,
        _request_camera_apply=lambda *_args, **_kwargs: None,
        _toggle_game_detection=lambda: None,
        _toggle_settings_page=lambda: None,
        _warn_no_engine=lambda **_kwargs: None,
        stop_engine_calls=[],
    )
    host._admission_epoch = 0
    host._admission_policy = None
    host._admission_policy_error = None
    host._admission_transition_latch = None
    host._terminal_response_owner_cache = None
    host._owns_engine_attempt = partial(
        main.MainWindow._owns_engine_attempt, host)
    host._owns_capture_apply = partial(
        main.MainWindow._owns_capture_apply, host)
    host._capture_global_defaults_snapshot = partial(
        main.MainWindow._capture_global_defaults_snapshot, host)
    host._capture_source_snapshot = partial(
        main.MainWindow._capture_source_snapshot, host)
    host._begin_capture_intent = partial(
        main.MainWindow._begin_capture_intent, host)
    host._capture_request_intent_id = partial(
        main.MainWindow._capture_request_intent_id, host)
    host._latest_intent_owns_request = partial(
        main.MainWindow._latest_intent_owns_request, host)
    host._consume_capture_intent = partial(
        main.MainWindow._consume_capture_intent, host)
    host._accepted_profile_status = partial(
        main.MainWindow._accepted_profile_status, host)
    host._capture_quality_summary = partial(
        main.MainWindow._capture_quality_summary, host)
    host._bounded_capture_label = main.MainWindow._bounded_capture_label
    host._refresh_capture_summary = partial(
        main.MainWindow._refresh_capture_summary, host)
    host._publish_capture_profile_status = partial(
        main.MainWindow._publish_capture_profile_status, host)
    host._capture_owner_signature = partial(
        main.MainWindow._capture_owner_signature, host)
    host._accepted_session_owner_is_current = partial(
        main.MainWindow._accepted_session_owner_is_current, host)
    host._rebind_recovered_session = partial(
        main.MainWindow._rebind_recovered_session, host)
    host._capture_resources_busy = partial(
        main.MainWindow._capture_resources_busy, host)
    host._camera_runtime_receipt_for_request = partial(
        main.MainWindow._camera_runtime_receipt_for_request, host)
    host._configure_keyboard_capture_for_request = partial(
        main.MainWindow._configure_keyboard_capture_for_request, host)
    host._bind_same_native_capture_request = partial(
        main.MainWindow._bind_same_native_capture_request, host)
    host._apply_latest_desired_request_if_idle = partial(
        main.MainWindow._apply_latest_desired_request_if_idle, host)
    host._restart_capture_engine = partial(
        main.MainWindow._restart_capture_engine, host)
    host._pump_save_responses = lambda: None
    host._apply_active_audio_state = lambda _config: None
    host._maybe_restart_for_display_mode_change = lambda _status: None
    for name, descriptor in vars(main.MainWindow).items():
        if not name.startswith('_admission_'):
            continue
        helper = getattr(main.MainWindow, name)
        if isinstance(descriptor, staticmethod):
            setattr(host, name, helper)
        elif callable(helper):
            setattr(host, name, partial(helper, host))
    host._cached_terminal_response_owner = partial(
        main.MainWindow._cached_terminal_response_owner, host)
    host._cached_terminal_response_owner_lineage = partial(
        main.MainWindow._cached_terminal_response_owner_lineage, host)
    host._retain_terminal_response_owner_cache = partial(
        main.MainWindow._retain_terminal_response_owner_cache, host)
    host._begin_admission_transition = partial(
        main.MainWindow._begin_admission_transition, host)
    main.MainWindow._reload_admission_policy(
        host, authoritative=True, trigger_transition=False)

    def resolve_capture_request(purpose='clips', *, source_snapshot=None,
                                prepared_request=None):
        from core.capture_session import (
            CameraSnapshot,
            ResolvedCaptureRequest,
            SourceSnapshot,
            build_resolved_capture_request,
        )

        if prepared_request is not None:
            if not isinstance(prepared_request, ResolvedCaptureRequest):
                raise ValueError('prepared capture request has an invalid type')
            return prepared_request
        intent_id = host._begin_capture_intent()
        # Keep the legacy test seam: selected cases inject a configuration
        # failure, or create a newer B apply while resolving A.
        resolved_config = host._requested_capture_config()
        values = host._capture_global_defaults_snapshot()
        if isinstance(source_snapshot, SourceSnapshot):
            source = source_snapshot
        else:
            source_mode = str(values.get('capture_mode', 'desktop') or 'desktop')
            authority = str(values.get(
                'capture_source_authority', 'manual') or 'manual').lower()
            if authority not in {'manual', 'automatic'}:
                authority = 'manual'
            if source_mode != 'window':
                source = SourceSnapshot(
                    source_mode='desktop', authority=authority,
                    display_name='Desktop', clip_group='Desktop',
                    monitor_identity=str(
                        values.get('capture_monitor', '') or ''),
                )
            else:
                identity = values.get('target_window_identity')
                exe_path = (identity.get('exe_path')
                            if isinstance(identity, dict) else None)
                source = SourceSnapshot(
                    source_mode='window', exe_path=exe_path,
                    live_identity=identity if isinstance(identity, dict)
                    else None,
                    authority=authority,
                    display_name=str(
                        values.get('target_window_name', '') or 'Game'),
                    clip_group=str(
                        values.get('target_clip_group', '') or 'Game'),
                    monitor_identity=str(
                        values.get('capture_monitor', '') or ''),
                )
        raw_gain = values.get('mic_volume', 100)
        mic_gain = int(100 if raw_gain is None else raw_gain)
        host._capture_request_token += 1
        request = build_resolved_capture_request(
            request_token=host._capture_request_token,
            purpose=purpose,
            source=source,
            capture_config=resolved_config,
            mic_gain_percent=mic_gain,
            camera=CameraSnapshot(),
            effects={'capture_card_enabled': True},
            source_intent_revision=int(
                getattr(host, '_source_intent_revision', 0)),
        )
        host._capture_request_intents[request.request_token] = intent_id
        host._latest_capture_intent_state = 'valid'
        host._latest_capture_intent_consumed = False
        host._desired_capture_intent_id = intent_id
        host._desired_capture_request = request
        return request

    # Fixture adapter only: all lifecycle ownership checks remain the
    # production MainWindow helpers and prepared requests pass through intact.
    host._resolve_capture_request = resolve_capture_request
    host._capture_requests_equivalent = partial(
        main.MainWindow._capture_requests_equivalent, host)
    host._prepared_source_is_live = partial(
        main.MainWindow._prepared_source_is_live, host)
    host._capture_pending_text = partial(
        main.MainWindow._capture_pending_text, host)
    host._set_capture_apply_state = partial(
        main.MainWindow._set_capture_apply_state, host)
    host._read_engine_startup_output = lambda _capture=None: host._startup_text
    host._schedule_owned_hardware_check = lambda *_args, **_kwargs: None
    host._ui_call = SimpleNamespace(
        emit=(queued.append if queue_ui else lambda callback: callback()))
    host._clear_focus_action = partial(
        main.MainWindow._clear_focus_action, host)
    production_stop = partial(main.MainWindow.stop_engine, host)

    def stop_engine():
        host.stop_engine_calls.append(True)
        return production_stop()

    host.stop_engine = stop_engine
    return host


def _bind_pending_capture_request(host, config):
    """Bind a real request/intent receipt to a manually prepared config."""
    from core.capture_session import (
        CameraSnapshot,
        SourceSnapshot,
        build_resolved_capture_request,
    )

    request_token = int(getattr(host, '_capture_request_token', 0)) + 1
    intent_id = int(getattr(host, '_capture_intent_revision', 0)) + 1
    request = build_resolved_capture_request(
        request_token=request_token,
        purpose='clips',
        source=SourceSnapshot(
            source_mode='desktop', display_name='Desktop',
            clip_group='Desktop'),
        capture_config=config,
        mic_gain_percent=100,
        camera=CameraSnapshot(),
        effects={'capture_card_enabled': True},
    )
    host._capture_request_token = request_token
    host._capture_intent_revision = intent_id
    host._capture_request_intents[request_token] = intent_id
    host._pending_launch_request = request
    host._pending_launch_intent_id = intent_id
    return request


def _run_start(monkeypatch, main, host, *, process=None, popen_error=None,
               popen_error_hook=None):
    process = process or _Process()
    targets = []
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    monkeypatch.setattr(main, 'EngineLogCapture',
                        lambda *_args: _StartupCapture(host._startup_text))
    if popen_error is None:
        monkeypatch.setattr(main.subprocess, 'Popen',
                            lambda *_args, **_kwargs: process)
    else:
        def fail_popen(*_args, **_kwargs):
            if popen_error_hook is not None:
                popen_error_hook()
            raise popen_error
        monkeypatch.setattr(main.subprocess, 'Popen', fail_popen)
    monkeypatch.setattr(
        main.threading, 'Thread',
        lambda *, target, **_kwargs: SimpleNamespace(
            start=lambda: targets.append(target)))
    monkeypatch.setattr(main.time, 'sleep', lambda _seconds: None)
    return main.MainWindow.start_engine(host), targets, process


def test_popen_failure_is_public_005_and_keeps_native_4551_payload(
        monkeypatch):
    import main

    events = []
    monkeypatch.setattr(main, 'emit_event',
                        lambda *args, **kwargs: events.append((args, kwargs)))
    host = _host(main, _Bridge())
    error = OSError(4551, 'blocked by Windows policy')
    error.winerror = 4551

    started, targets, _process = _run_start(
        monkeypatch, main, host, popen_error=error)

    assert started is False
    assert targets == []
    event = next(fields for args, fields in events
                 if args[:2] == ('engine', 'start_failed')
                 and 'native_failure' in fields)
    assert event['compatibility_error'] == 'Error 005'
    assert event['process_generation'] == 1
    assert event['native_capture_generation'] is None
    assert event['native_failure'] == {
        'api_call': 'CreateProcessW',
        'native_error_signed': 4551,
        'native_error_unsigned': 4551,
        'native_error_hex': '0x000011C7',
        'win32_error_decimal': 4551,
        'symbolic_error': 'ERROR_SYSTEM_INTEGRITY_POLICY_VIOLATION',
        'system_message': str(error),
    }
    assert host._errors[0][0][0] == 'ENGINE COULD NOT START'
    assert host._errors[0][1]['diagnostic_context']['process_generation'] == 1


def test_popen_error_after_reentrant_b_keeps_b_owner_without_a_cleanup(
        monkeypatch):
    import main

    host = _host(main, _Bridge())
    process_b = _Process()
    bridge_b = _Bridge()
    config_b = replace(host._requested_capture_config(), encoder='software')
    error = OSError(4551, 'blocked by Windows policy')
    error.winerror = 4551

    def reenter_b_before_error():
        host._engine_gen += 1
        host.engine_process = process_b
        host.bridge = bridge_b
        host._capture_config.request(config_b)
        token_b = host._capture_config.begin_apply()
        host._pending_launch_config = config_b
        host._pending_launch_generation = host._engine_gen
        host._pending_launch_token = token_b
        host._capture_settings_applying = True
        host._restart_pending = True

    started, targets, _process = _run_start(
        monkeypatch, main, host, popen_error=error,
        popen_error_hook=reenter_b_before_error)

    assert started is False
    assert targets == []
    assert host.engine_process is process_b
    assert host.bridge is bridge_b
    assert host._engine_gen == 2
    assert host._capture_config.applying == config_b
    assert host._pending_launch_config is config_b
    assert host._pending_launch_generation == 2
    assert host._pending_launch_token == host._capture_config.applying_token
    assert host._capture_settings_applying is True
    assert host._restart_pending is True
    assert host.stop_engine_calls == []
    assert host._errors == []


@pytest.mark.parametrize(
    ('startup_text', 'expected_title', 'expected_code', 'expected_cause'),
    [
        ('', 'ENGINE NOT RESPONDING', 'Error 004', 'ENGINE_START_FAILED'),
        ('FTHR_STARTUP_ERROR: ENCODER_INIT_FAILED: no supported codec',
         'CAPTURE FAILED', 'Error 001', 'ENCODER_INIT_FAILED'),
    ],
)
def test_queued_handshake_failure_maps_public_code_by_native_evidence(
        monkeypatch, startup_text, expected_title, expected_code,
        expected_cause):
    import main

    events = []
    monkeypatch.setattr(main, 'emit_event',
                        lambda *args, **kwargs: events.append((args, kwargs)))
    host = _host(main, _Bridge(), startup_text=startup_text)
    started, targets, _process = _run_start(monkeypatch, main, host)
    assert started is True

    targets[0]()
    assert len(host._queued_callbacks) == 1
    host._queued_callbacks.pop()()

    assert host._errors[-1][0][0] == expected_title
    context = host._errors[-1][1]['diagnostic_context']
    assert context['technical_cause'] == expected_cause
    assert context['process_generation'] == 1
    assert context['recovery_kind'] == (
        'engine_lineage' if expected_code == 'Error 004' else None)
    event_name = 'handshake_timeout'
    event = next(fields for args, fields in events
                 if args[:2] == ('engine', event_name))
    assert event['compatibility_error'] == expected_code
    assert event['process_generation'] == 1
    assert event['native_capture_generation'] is None


def test_initialize_race_does_not_queue_old_connect_or_shutdown_replacement(
        monkeypatch):
    import main

    host = _host(main, _Bridge())
    replacement = _Bridge()
    process = _Process()
    bridge = _Bridge(
        initialize_result=True,
        after_initialize=lambda: setattr(host, 'bridge', replacement),
    )
    host.bridge = bridge
    started, targets, _ = _run_start(
        monkeypatch, main, host, process=process)
    assert started is True

    targets[0]()

    assert host._queued_callbacks == []
    assert bridge.shutdown_calls == 1
    assert replacement.shutdown_calls == 0
    assert host.bridge is replacement
    assert host.engine_process is process


def test_status_poll_does_not_replace_bridge_during_startup(monkeypatch):
    import main

    bridge = _Bridge(initialize_result=True)
    host = _host(main, bridge)
    host._capture_settings_applying = True
    host._capture_pending_text = lambda: 'APPLYING SETTINGS'
    host._advance_recoverable_errors = lambda **_: None
    started, targets, process = _run_start(monkeypatch, main, host)
    assert started

    # Reproduce the regular status tick before the startup worker connects.
    host._maybe_restart_for_display_mode_change = partial(main.MainWindow._maybe_restart_for_display_mode_change, host)
    main.MainWindow._update_status(host)
    assert host.bridge is bridge
    assert bridge.shutdown_calls == 0
    assert host.engine_process is process

    targets[0]()
    host._queued_callbacks.pop()()
    assert host._engine_handshake_generation is None
    assert host._pending_launch_generation == host._engine_gen
    assert host._pending_launch_config == host._capture_config.requested


@pytest.mark.parametrize('connected', [True, False])
def test_confirmation_timeout_ignores_queued_handshake_without_stopping_engine(
        monkeypatch, connected):
    import main

    bridge = _Bridge(initialize_result=connected)
    host = _host(main, bridge)
    host._capture_settings_applying = True
    started, targets, process = _run_start(monkeypatch, main, host)
    assert started
    targets[0]()
    callback = host._queued_callbacks.pop()
    main.MainWindow._capture_apply_timed_out(host)
    callback()
    assert host.stop_engine_calls == []
    assert host.engine_process is process
    assert host._pending_launch_config is None
    assert host._pending_launch_generation is None
    assert host._engine_handshake_generation is None


@pytest.mark.parametrize('callback_kind', ['connected', 'failed'])
def test_queued_old_generation_callback_cannot_mutate_or_stop_replacement(
        monkeypatch, callback_kind):
    import main

    bridge = _Bridge(initialize_result=(callback_kind == 'connected'))
    host = _host(main, bridge)
    started, targets, _process = _run_start(monkeypatch, main, host)
    assert started is True

    targets[0]()
    assert len(host._queued_callbacks) == 1
    old_callback = host._queued_callbacks.pop()
    replacement_process = _Process()
    replacement_bridge = _Bridge()
    host._engine_gen += 1
    host.engine_process = replacement_process
    host.bridge = replacement_bridge

    old_callback()

    assert host._pending_launch_config is None
    assert host._pending_launch_generation is None
    assert host.stop_engine_calls == []
    assert host.engine_process is replacement_process
    assert host.bridge is replacement_bridge
    assert replacement_bridge.shutdown_calls == 0


def test_same_generation_stale_apply_token_cannot_accept_old_handshake(
        monkeypatch):
    import main

    host = _host(main, _Bridge())
    started, targets, process = _run_start(monkeypatch, main, host)
    assert started is True
    targets[0]()
    old_callback = host._queued_callbacks.pop()

    newer = replace(host._capture_config.requested, codec='hevc')
    host._capture_config.request(newer)
    newer_token = host._capture_config.begin_apply()
    old_callback()

    assert host._capture_config.applying_token == newer_token
    assert host._pending_launch_config is None
    assert host._pending_launch_generation is None
    assert host._pending_launch_token is None
    assert host.stop_engine_calls == []
    assert host.engine_process is process


def test_accepted_receipt_allows_owned_hardware_check_until_new_begin():
    import main

    config = CaptureConfig(
        fps=60, buffer_seconds=32, width=1920, height=1080,
        bitrate_kbps=25_000, codec='h264', preset=4,
        monitor='', scaling='stretch', audio_enabled=False,
        encoder='software')
    replacement = replace(config, encoder='auto')
    process = _Process()
    bridge = SimpleNamespace(
        _layout=SimpleNamespace(nvenc_active=True),
        is_connected=lambda: True,
        get_active_codec=lambda: 'libopenh264',
    )
    host = _host(main, bridge)
    host.engine_process = process
    host._engine_gen = 9
    host.settings_manager = _Settings()
    host._capture_config.request(config)
    token = host._capture_config.begin_apply()
    assert host._capture_config.succeed(token) is True
    host._capture_config.request(replacement)

    main.MainWindow._check_hardware_encoding_status(
        host, expected_generation=9, expected_process=process,
        expected_bridge=bridge, expected_config=config,
        expected_token=token)
    assert host._encoder_type == 'LIBOPENH264'
    assert host._errors == []

    newer_token = host._capture_config.begin_apply()
    before = host._encoder_type
    bridge.get_active_codec = lambda: (_ for _ in ()).throw(
        AssertionError('stale hardware callback inspected codec'))
    main.MainWindow._check_hardware_encoding_status(
        host, expected_generation=9, expected_process=process,
        expected_bridge=bridge, expected_config=config,
        expected_token=token)
    assert host._encoder_type == before
    assert host._errors == []
    assert host._capture_config.applying_token == newer_token


def test_muted_failure_still_emits_bounded_structured_operation_diagnostic(
        monkeypatch):
    import main

    events = []
    monkeypatch.setattr(main, 'emit_event',
                        lambda *args, **kwargs: events.append((args, kwargs)))
    pushed = []
    host = SimpleNamespace(
        error_bar=SimpleNamespace(push=lambda *args, **kwargs: pushed.append(args)),
        settings_manager=SimpleNamespace(
            get=lambda _key, default=None: False),
        _recoverable_error_incidents={},
        _error_recovery_tracker=RecoveryTracker(),
    )

    main.MainWindow.push_error(
        host, 'CLIP WAS NOT SAVED', 'native writer failed',
        diagnostic_context={
            'diagnostic_error': main.DiagnosticError.CLIP_SAVE_FAILED,
            'technical_cause': 'NATIVE_SAVE_FAILURE',
            'technical_subsystem': 'clip_save',
            'operation': 'write_clip',
            'operation_id': 'save-op-42',
            'process_generation': 8,
            'backend_generation': 3,
            'recovery_kind': 'capture_backend',
            'recovery_status': 'terminal_not_recoverable',
        }, key='save:save-op-42')

    assert pushed == []
    args, fields = events[-1]
    assert args[:2] == ('application', 'failure_reported')
    assert fields['public_error_code'] == 'Error 015'
    assert fields['operation_id'] == 'save-op-42'
    assert fields['incident_key'] == 'save:save-op-42'
    assert fields['process_generation'] == 8
    assert fields['backend_generation'] == 3
    assert fields['recovery_status'] == 'terminal_not_recoverable'
    assert host._recoverable_error_incidents == {}


def test_missing_hardware_encoder_is_error_001_and_not_frame_recoverable(
        monkeypatch):
    import main

    events = []
    monkeypatch.setattr(main, 'emit_event',
                        lambda *args, **kwargs: events.append((args, kwargs)))
    pushed = []
    host = SimpleNamespace(
        bridge=SimpleNamespace(
            is_connected=lambda: True,
            get_active_codec=lambda: '',
            _layout=SimpleNamespace(nvenc_active=False)),
        settings_manager=_Settings(),
        error_bar=SimpleNamespace(
            push=lambda *args, **kwargs: pushed.append((args, kwargs))),
        _recoverable_error_incidents={},
        _error_recovery_tracker=RecoveryTracker(),
        _engine_process_generation=12,
        _last_native_capture_generation=4,
        _restart_capture_engine=lambda: None,
    )
    host.push_error = partial(main.MainWindow.push_error, host)

    main.MainWindow._check_hardware_encoding_status(host)

    assert pushed[0][0][0].endswith('CAPTURE FAILED')
    assert 'HARDWARE ENCODING UNAVAILABLE' in pushed[0][0][1]
    assert pushed[0][1]['key'] == 'encoder-init'
    assert pushed[0][1]['dismiss_on_action'] is False
    assert events[-1][1]['technical_cause'] == 'ENCODER_INIT_FAILED'
    assert events[-1][1]['process_generation'] == 12
    assert events[-1][1]['backend_generation'] == 4
    assert events[-1][1]['recovery_status'] == (
        'awaiting_replacement_hardware_and_stable_progress')
    assert host._recoverable_error_incidents['encoder-init'] == {
        'title': 'CAPTURE FAILED',
        'public_error_code': 'Error 001',
        'recovery_kind': 'encoder_init',
        'process_generation': 12,
    }
    assert events[-1][1]['public_error_code'] == 'Error 001'


def test_encoder_init_recovery_requires_new_hardware_and_stable_healthy_polls(
        monkeypatch):
    import main

    events = []
    monkeypatch.setattr(main, 'emit_event',
                        lambda *args, **kwargs: events.append((args, kwargs)))
    now = [0.0]

    class _Clock:
        def __call__(self):
            return now[0]

    removed = []
    host = SimpleNamespace(
        _recoverable_error_incidents={
            'encoder-init': {
                'title': 'CAPTURE FAILED',
                'public_error_code': 'Error 001',
                'recovery_kind': 'encoder_init',
                'process_generation': 12,
            },
        },
        _error_recovery_tracker=RecoveryTracker(clock=_Clock()),
        _engine_process_generation=12,
        error_bar=SimpleNamespace(remove_by_key=removed.append),
    )
    advance = partial(main.MainWindow._advance_recoverable_errors, host)

    # The failed launch's own generation cannot auto-recover itself.
    advance(connected=True, frame_count=1,
            health_state=main.CaptureHealthState.HEALTHY,
            backend_generation=7, hardware_active=True)
    assert removed == []

    # On a replacement process, frames alone (including a missing hardware
    # flag) are insufficient. Content-suspect and disconnected polls reset too.
    host._engine_process_generation = 13
    now[0] = 1.0
    advance(connected=True, frame_count=2,
            health_state=main.CaptureHealthState.HEALTHY,
            backend_generation=7, hardware_active=False)
    now[0] = 2.0
    advance(connected=True, frame_count=3,
            health_state=main.CaptureHealthState.HEALTHY,
            backend_generation=7, hardware_active=False)
    now[0] = 3.1
    advance(connected=True, frame_count=4,
            health_state=main.CaptureHealthState.HEALTHY,
            backend_generation=7, hardware_active=False)
    now[0] = 4.1
    advance(connected=True, frame_count=5,
            health_state=main.CaptureHealthState.HEALTHY,
            backend_generation=7, hardware_active=False)
    now[0] = 5.0
    advance(connected=True, frame_count=6,
            health_state=main.CaptureHealthState.HEALTHY,
            backend_generation=7)
    now[0] = 6.0
    advance(connected=True, frame_count=4,
            health_state=main.CaptureHealthState.CONTENT_SUSPECT,
            backend_generation=7, hardware_active=True)
    now[0] = 6.5
    advance(connected=False, frame_count=0,
            health_state=main.CaptureHealthState.STOPPED,
            backend_generation=7, hardware_active=True)
    assert removed == []

    # Hardware evidence and HEALTHY progress must persist in one backend
    # generation. A generation change and then a counter decrease reset it.
    now[0] = 7.0
    advance(connected=True, frame_count=1,
            health_state=main.CaptureHealthState.HEALTHY,
            backend_generation=7, hardware_active=True)
    now[0] = 8.0
    advance(connected=True, frame_count=2,
            health_state=main.CaptureHealthState.HEALTHY,
            backend_generation=7, hardware_active=True)
    now[0] = 8.5
    advance(connected=True, frame_count=3,
            health_state=main.CaptureHealthState.HEALTHY,
            backend_generation=8, hardware_active=True)
    now[0] = 9.5
    advance(connected=True, frame_count=4,
            health_state=main.CaptureHealthState.HEALTHY,
            backend_generation=8, hardware_active=True)
    now[0] = 10.5
    advance(connected=True, frame_count=5,
            health_state=main.CaptureHealthState.HEALTHY,
            backend_generation=8, hardware_active=True)
    now[0] = 11.5
    advance(connected=True, frame_count=2,
            health_state=main.CaptureHealthState.HEALTHY,
            backend_generation=8, hardware_active=True)
    now[0] = 12.5
    advance(connected=True, frame_count=3,
            health_state=main.CaptureHealthState.HEALTHY,
            backend_generation=8, hardware_active=True)
    now[0] = 13.5
    advance(connected=True, frame_count=4,
            health_state=main.CaptureHealthState.HEALTHY,
            backend_generation=8, hardware_active=True)
    now[0] = 14.6
    advance(connected=True, frame_count=5,
            health_state=main.CaptureHealthState.HEALTHY,
            backend_generation=8, hardware_active=True)
    assert removed == []
    now[0] = 15.6
    advance(connected=True, frame_count=6,
            health_state=main.CaptureHealthState.HEALTHY,
            backend_generation=8, hardware_active=True)

    assert removed == ['encoder-init']
    assert 'encoder-init' not in host._recoverable_error_incidents
    recovered = next(fields for args, fields in events
                     if args[:2] == ('application', 'failure_recovered'))
    assert recovered['compatibility_error'] == 'Error 001'
    assert recovered['process_generation'] == 13


@pytest.mark.parametrize(
    ('hardware_status', 'codec', 'expected'),
    [({'nvenc_active': True}, 'amf_h264', True), ({}, 'nvenc_h264', False)],
)
def test_update_status_passes_native_hardware_flag_to_recovery(
        hardware_status, codec, expected):
    import main

    observed = []
    status = {
        'connected': True,
        'frames_captured': 10,
        'capture_generation': 4,
        'capture_health_flags': 0,
        **hardware_status,
    }
    snapshot = SimpleNamespace(
        state=main.CaptureHealthState.HEALTHY,
        changed=False,
        request_recovery=False,
    )
    bridge = SimpleNamespace(
        is_connected=lambda: True,
        get_status=lambda: status,
        get_active_codec=lambda: codec,
        get_active_preset=lambda: 4,
    )
    host = SimpleNamespace(
        engine_process=None,
        bridge=bridge,
        settings_manager=_Settings(),
        _pump_save_responses=lambda: None,
        is_capturing=True,
        _capture_health=SimpleNamespace(observe=lambda **_kwargs: snapshot),
        _advance_recoverable_errors=lambda **kwargs: observed.append(kwargs),
        _last_capture_health_event_at=__import__('time').monotonic(),
        _ui_ready=False,
        _background_ui_paused=False,
        _engine_process_generation=13,
        _pending_launch_config=None,
        _pending_launch_generation=None,
        _capture_settings_applying=False,
        _pending_launch_request=None,
        _pending_launch_token=None,
        _pending_launch_intent_id=None,
        _accepted_capture_session=None,
        _accepted_camera_runtime_receipt=None,
        _capture_request_intents={},
        _desired_capture_request=None,
        _desired_capture_intent_id=None,
        _latest_capture_intent_state='none',
        _latest_capture_intent_consumed=True,
        _restart_pending=False,
        _shutdown_requested=False,
        _shutdown_complete=False,
        _set_status=lambda *_args: None,
        _diagnostics=None,
        _diagnostic_engine_start_count=1,
    )

    host._capture_owner_signature = partial(
        main.MainWindow._capture_owner_signature, host)
    host._retain_terminal_response_owner_cache = partial(
        main.MainWindow._retain_terminal_response_owner_cache, host)
    host._capture_resources_busy = partial(
        main.MainWindow._capture_resources_busy, host)
    host._apply_latest_desired_request_if_idle = partial(
        main.MainWindow._apply_latest_desired_request_if_idle, host)
    host._maybe_restart_for_display_mode_change = partial(main.MainWindow._maybe_restart_for_display_mode_change, host)
    main.MainWindow._reload_admission_policy(
        host, authoritative=True, trigger_transition=False)
    main.MainWindow._update_status(host)

    assert observed[0]['hardware_active'] is expected


@pytest.mark.parametrize(
    ('frames', 'hardware', 'generation', 'completed'),
    [(10, True, 12, True), (0, True, 12, True),
     (10, False, 12, False), (10, True, 11, False)],
)
def test_apply_can_complete_while_focus_paused(frames, hardware, generation, completed):
    import main
    from core.capture_health import CaptureHealthFlag, CaptureHealthMonitor
    from core.capture_settings import ApplyStatus

    bridge = SimpleNamespace(
        is_connected=lambda: True,
        get_status=lambda: {
            'connected': True, 'frames_captured': frames,
            'capture_generation': 4, 'nvenc_active': hardware,
            'capture_health_flags': int(CaptureHealthFlag.PAUSED | CaptureHealthFlag.ACTIVE),
        },
        get_active_codec=lambda: 'amf_h264',
    )
    host = _host(main, bridge)
    host._capture_config.request(host._requested_capture_config())
    apply_token = host._capture_config.begin_apply()
    host._engine_gen = 12
    host._pending_launch_config = host._capture_config.requested
    host._pending_launch_generation = generation
    host._pending_launch_token = apply_token
    _bind_pending_capture_request(host, host._pending_launch_config)
    host._capture_settings_applying = True
    host._capture_pending_text = lambda: 'APPLYING SETTINGS'
    host._set_capture_apply_state = lambda state: setattr(
        host, '_capture_settings_applying', state)
    host._capture_health = CaptureHealthMonitor()
    host._pump_save_responses = lambda: None
    host._advance_recoverable_errors = lambda **_: None
    host._apply_active_audio_state = lambda _: None
    host._last_capture_health_event_at = main.time.monotonic()
    host._background_ui_paused = False
    host._capture_health_log = SimpleNamespace(info=lambda *_: None)
    host.is_capturing = True
    host._maybe_restart_for_display_mode_change = partial(main.MainWindow._maybe_restart_for_display_mode_change, host)
    main.MainWindow._update_status(host)
    assert host._capture_settings_applying is not completed
    assert host._capture_config.status is (
        ApplyStatus.ACTIVE if completed else ApplyStatus.APPLYING)


def test_reentrant_audio_apply_keeps_new_attempt_and_skips_old_manual_continuation():
    import main
    from core.capture_health import CaptureHealthFlag, CaptureHealthMonitor
    from core.capture_settings import ApplyStatus

    bridge = SimpleNamespace(
        is_connected=lambda: True,
        get_status=lambda: {
            'connected': True,
            'frames_captured': 10,
            'capture_generation': 4,
            'nvenc_active': True,
            'capture_health_flags': int(
                CaptureHealthFlag.PAUSED | CaptureHealthFlag.ACTIVE),
        },
        get_active_codec=lambda: 'amf_h264',
    )
    host = _host(main, bridge)
    config_a = host._requested_capture_config()
    host._capture_config.request(config_a)
    token_a = host._capture_config.begin_apply()
    host._engine_gen = 12
    host._pending_launch_config = config_a
    host._pending_launch_generation = 12
    host._pending_launch_token = token_a
    _bind_pending_capture_request(host, config_a)
    host._capture_settings_applying = True
    host._capture_pending_text = lambda: 'APPLYING SETTINGS'
    ui_states = []
    host._set_capture_apply_state = lambda state: ui_states.append(state)
    host._capture_health = CaptureHealthMonitor()
    host._pump_save_responses = lambda: None
    host._advance_recoverable_errors = lambda **_: None
    host._background_ui_paused = False
    host._capture_health_log = SimpleNamespace(info=lambda *_: None)
    host.is_capturing = True
    host._manual_record_state = 'preparing'
    continuations = []
    host._continue_prepared_manual_recording = (
        lambda: continuations.append('stale-A'))

    def start_new_apply(_config):
        config_b = replace(config_a, encoder='software')
        host._capture_config.request(config_b)
        token_b = host._capture_config.begin_apply()
        host._pending_launch_config = config_b
        host._pending_launch_generation = 12
        host._pending_launch_token = token_b
        _bind_pending_capture_request(host, config_b)
        host._capture_settings_applying = True

    host._apply_active_audio_state = start_new_apply
    host._maybe_restart_for_display_mode_change = partial(
        main.MainWindow._maybe_restart_for_display_mode_change, host)
    main.MainWindow._update_status(host)

    assert host._capture_config.status is ApplyStatus.APPLYING
    assert host._capture_config.applying == replace(config_a, encoder='software')
    assert host._capture_config.applying_token != token_a
    assert host._pending_launch_config.encoder == 'software'
    assert host._pending_launch_token == host._capture_config.applying_token
    assert host._capture_settings_applying is True
    assert ui_states == []
    assert continuations == []


def test_manual_health_continuation_uses_accepted_receipt_and_path_owner(
        monkeypatch):
    import main
    from core.capture_health import CaptureHealthFlag, CaptureHealthMonitor

    callbacks = []
    monkeypatch.setattr(
        main.QTimer, 'singleShot', lambda _delay, callback: callbacks.append(
            callback))
    bridge = SimpleNamespace(
        is_connected=lambda: True,
        get_status=lambda: {
            'connected': True,
            'frames_captured': 10,
            'capture_generation': 4,
            'nvenc_active': True,
            'capture_health_flags': int(
                CaptureHealthFlag.PAUSED | CaptureHealthFlag.ACTIVE),
        },
        get_active_codec=lambda: 'amf_h264',
    )
    host = _host(main, bridge)
    config_a = host._requested_capture_config()
    config_b = replace(config_a, encoder='software')
    host._capture_config.request(config_a)
    token_a = host._capture_config.begin_apply()
    host._capture_config.request(config_b)
    host._engine_gen = 12
    host._pending_launch_config = config_a
    host._pending_launch_generation = 12
    host._pending_launch_token = token_a
    _bind_pending_capture_request(host, config_a)
    host._capture_settings_applying = True
    host._capture_pending_text = lambda: 'APPLYING SETTINGS'
    host._set_capture_apply_state = lambda state: setattr(
        host, '_capture_settings_applying', state)
    host._capture_health = CaptureHealthMonitor()
    host._pump_save_responses = lambda: None
    host._advance_recoverable_errors = lambda **_: None
    host._apply_active_audio_state = lambda _: None
    host._last_capture_health_event_at = main.time.monotonic()
    host._background_ui_paused = False
    host._capture_health_log = SimpleNamespace(info=lambda *_: None)
    host.is_capturing = True
    host._manual_record_state = 'preparing'
    host._pending_manual_record_path = Path('capture-a.mp4')
    continuations = []
    host._continue_prepared_manual_recording = (
        lambda: continuations.append('A'))
    host._maybe_restart_for_display_mode_change = partial(
        main.MainWindow._maybe_restart_for_display_mode_change, host)

    main.MainWindow._update_status(host)
    assert len(callbacks) == 1
    callback = callbacks.pop()
    callback()
    assert continuations == ['A']

    # A request-only B leaves the accepted A receipt valid, but beginning B
    # invalidates it before another queued callback can run.
    host._capture_config.request(config_b)
    token_b = host._capture_config.begin_apply()
    host._pending_manual_record_path = Path('capture-b.mp4')
    callback()
    assert token_b == host._capture_config.applying_token
    assert continuations == ['A']


@pytest.mark.parametrize(
    'mutation', [
        'request-only', 'path', 'begin', 'cancel', 'failure',
        'late-timeout', 'shutdown',
    ])
def test_manual_queued_receipt_isolated_mutations(
        monkeypatch, mutation):
    import main
    from core.capture_health import CaptureHealthMonitor

    callbacks = []
    monkeypatch.setattr(
        main.QTimer, 'singleShot', lambda _delay, callback: callbacks.append(
            callback))
    bridge = SimpleNamespace(
        is_connected=lambda: True,
        get_status=lambda: {
            'connected': True,
            'frames_captured': 10,
            'capture_generation': 4,
            'nvenc_active': True,
            'capture_health_flags': 0,
        },
        get_active_codec=lambda: 'amf_h264',
    )
    host = _host(main, bridge)
    config_a = host._requested_capture_config()
    config_b = replace(config_a, encoder='software')
    host._capture_config.request(config_a)
    token_a = host._capture_config.begin_apply()
    host._capture_config.request(config_b)
    host._engine_gen = 12
    host._pending_launch_config = config_a
    host._pending_launch_generation = 12
    host._pending_launch_token = token_a
    _bind_pending_capture_request(host, config_a)
    host._capture_settings_applying = True
    host._capture_pending_text = lambda: 'APPLYING SETTINGS'
    host._set_capture_apply_state = lambda state: setattr(
        host, '_capture_settings_applying', state)
    host._capture_health = CaptureHealthMonitor()
    host._pump_save_responses = lambda: None
    host._advance_recoverable_errors = lambda **_: None
    host._apply_active_audio_state = lambda _: None
    host._last_capture_health_event_at = main.time.monotonic()
    host._background_ui_paused = False
    host._capture_health_log = SimpleNamespace(info=lambda *_: None)
    host.is_capturing = True
    host._manual_record_state = 'preparing'
    host._pending_manual_record_path = Path('capture-a.mp4')
    continuations = []
    host._continue_prepared_manual_recording = (
        lambda: continuations.append('A'))
    host._maybe_restart_for_display_mode_change = partial(
        main.MainWindow._maybe_restart_for_display_mode_change, host)

    main.MainWindow._update_status(host)
    assert len(callbacks) == 1
    callback = callbacks.pop()
    if mutation == 'request-only':
        pass
    elif mutation == 'path':
        host._pending_manual_record_path = Path('capture-b.mp4')
    elif mutation == 'begin':
        host._capture_config.begin_apply()
    elif mutation == 'cancel':
        host._capture_config.cancel_apply()
    elif mutation == 'failure':
        host._capture_config.fail('capture apply timed out')
    elif mutation == 'late-timeout':
        main.MainWindow._capture_apply_timed_out(host)
    elif mutation == 'shutdown':
        host._shutdown_requested = True
    callback()

    assert continuations == (
        ['A'] if mutation in {'request-only', 'late-timeout'} else [])


@pytest.mark.parametrize(
    'mutation', ['current', 'accepted', 'cancel', 'begin', 'timeout', 'shutdown'])
def test_connected_mic_callback_uses_real_receipt_admission(
        monkeypatch, mutation):
    import main

    mic_calls = []
    callbacks = []
    monkeypatch.setattr(
        main.QTimer, 'singleShot', lambda _delay, callback: callbacks.append(
            callback))
    bridge = _Bridge(initialize_result=True)
    host = _host(main, bridge)
    host._ui_ready = True
    host._settings_page_widget = SimpleNamespace(
        _populate_mic_devices=lambda: mic_calls.append('populate'))
    started, targets, _process = _run_start(monkeypatch, main, host)
    assert started is True
    targets[0]()
    assert len(host._queued_callbacks) == 1
    host._queued_callbacks.pop()()
    assert len(callbacks) == 1
    token = host._capture_config.applying_token
    config = host._capture_config.applying
    assert token is not None and config is not None
    if mutation == 'current':
        host._capture_config.request(replace(config, encoder='software'))
    elif mutation == 'accepted':
        assert host._capture_config.succeed(token) is True
        host._capture_config.request(replace(config, encoder='software'))
    elif mutation == 'cancel':
        host._capture_config.cancel_apply()
    elif mutation == 'begin':
        host._capture_config.request(replace(config, encoder='software'))
        host._capture_config.begin_apply()
    elif mutation == 'timeout':
        host._capture_settings_applying = True
        main.MainWindow._capture_apply_timed_out(host)
    elif mutation == 'shutdown':
        host._shutdown_requested = True
    callbacks.pop()()

    assert mic_calls == (['populate'] if mutation in {'current', 'accepted'} else [])


def test_preflight_monitor_failure_finishes_owned_manual_restart(
        monkeypatch):
    import main
    from core.windows_monitor import MonitorSelectionResult, MonitorSelectionState

    host = _host(main, _Bridge())
    host._restart_pending = True
    host._capture_settings_applying = True
    host._engine_gen = 7
    host._set_capture_apply_state = lambda state: setattr(
        host, '_capture_settings_applying', state)
    host._manual_record_state = 'preparing'
    host._pending_manual_record_path = Path('capture.mp4')
    manual_failures = []
    host._fail_manual_recording = lambda detail: manual_failures.append(detail)
    selection = MonitorSelectionResult(
        state=MonitorSelectionState.STALE,
        code='MONITOR_STALE', reason='saved monitor is unavailable',
        previous_state=MonitorSelectionState.RESOLVED,
        previous_path='\\\\?\\display#missing')
    host._requested_capture_config = lambda: (_ for _ in ()).throw(
        main.MonitorSelectionError(selection))
    monkeypatch.setattr(main, 'CaptureBridge', lambda: host.bridge)
    popen_calls = []
    monkeypatch.setattr(
        main.subprocess, 'Popen',
        lambda *args, **kwargs: popen_calls.append((args, kwargs)))
    host.start_engine = partial(main.MainWindow.start_engine, host)
    host._finish_restart = partial(main.MainWindow._finish_restart, host)

    assert host._finish_restart(7) is False
    assert host._capture_config.status is main.ApplyStatus.FAILED
    assert host._capture_config.error.startswith('saved monitor is unavailable')
    assert manual_failures == ['The recording quality profile could not be started.']
    assert host._capture_settings_applying is False
    assert popen_calls == []


def test_preflight_saved_window_failure_finishes_owned_manual_restart(
        monkeypatch):
    import main

    host = _host(main, _Bridge())
    config = host._requested_capture_config()
    host._requested_capture_config = lambda: config
    host.settings_manager = _Settings(
        capture_mode='window',
        target_hwnd=42,
        target_window_identity={
            'hwnd': 42, 'pid': 1234, 'process_created': 1.0,
            'exe_path': r'C:\Games\saved.exe', 'class_name': 'GameWindow',
        },
    )
    monkeypatch.setattr(main, 'get_window_info', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, 'CaptureBridge', lambda: host.bridge)
    popen_calls = []
    monkeypatch.setattr(
        main.subprocess, 'Popen',
        lambda *args, **kwargs: popen_calls.append((args, kwargs)))
    host._restart_pending = True
    host._capture_settings_applying = True
    host._engine_gen = 7
    host._set_capture_apply_state = lambda state: setattr(
        host, '_capture_settings_applying', state)
    host._manual_record_state = 'preparing'
    host._pending_manual_record_path = Path('capture-window.mp4')
    manual_failures = []
    host._fail_manual_recording = lambda detail: manual_failures.append(detail)
    host.start_engine = partial(main.MainWindow.start_engine, host)
    host._finish_restart = partial(main.MainWindow._finish_restart, host)

    assert host._finish_restart(7) is False
    assert host._capture_config.status is main.ApplyStatus.FAILED
    assert 'selected window' in host._capture_config.error
    assert manual_failures == ['The recording quality profile could not be started.']
    assert host._capture_settings_applying is False
    assert popen_calls == []


def test_preflight_failure_rejects_reentrant_new_apply_without_old_cleanup(
        monkeypatch):
    import main
    from core.windows_monitor import MonitorSelectionResult, MonitorSelectionState

    host = _host(main, _Bridge())
    host._restart_pending = True
    host._capture_settings_applying = True
    host._engine_gen = 7
    host._manual_record_state = 'preparing'
    host._pending_manual_record_path = Path('capture-a.mp4')
    config_b = replace(host._requested_capture_config(), encoder='software')
    manual_failures = []
    host._fail_manual_recording = lambda detail: manual_failures.append(detail)
    selection = MonitorSelectionResult(
        state=MonitorSelectionState.STALE,
        code='MONITOR_STALE', reason='saved monitor is unavailable',
        previous_state=MonitorSelectionState.RESOLVED,
        previous_path='\\\\?\\display#missing')
    host._requested_capture_config = lambda: (_ for _ in ()).throw(
        main.MonitorSelectionError(selection))
    monkeypatch.setattr(main, 'CaptureBridge', lambda: host.bridge)
    host.start_engine = partial(main.MainWindow.start_engine, host)
    replaced = []

    def apply_state(state):
        host._capture_settings_applying = state
        if not state and not replaced:
            replaced.append(True)
            host._capture_config.request(config_b)
            token_b = host._capture_config.begin_apply()
            host._capture_settings_applying = True
            host._pending_launch_config = config_b
            host._pending_launch_generation = host._engine_gen
            host._pending_launch_token = token_b
            host._restart_pending = True

    host._set_capture_apply_state = apply_state
    host._finish_restart = partial(main.MainWindow._finish_restart, host)

    assert host._finish_restart(7) is False
    assert host._capture_config.applying == config_b
    assert host._capture_settings_applying is True
    assert host._restart_pending is True
    assert manual_failures == []
    assert host._errors == []


def test_popen_failure_stop_cannot_clear_reentrant_new_apply(
        monkeypatch):
    import main

    host = _host(main, _Bridge())
    config_b = replace(host._requested_capture_config(), encoder='software')

    def stop_with_new_apply():
        host.stop_engine_calls.append(True)
        host._engine_gen += 1
        host._capture_config.request(config_b)
        token_b = host._capture_config.begin_apply()
        host._pending_launch_config = config_b
        host._pending_launch_generation = host._engine_gen
        host._pending_launch_token = token_b
        host._capture_settings_applying = True
        host._restart_pending = True

    host.stop_engine = stop_with_new_apply
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    error = OSError(4551, 'blocked by Windows policy')
    error.winerror = 4551

    started, targets, _process = _run_start(
        monkeypatch, main, host, popen_error=error)

    assert started is False
    assert targets == []
    assert host._capture_config.applying == config_b
    assert host._capture_settings_applying is True
    assert host._restart_pending is True
    assert host._errors == []


@pytest.mark.parametrize('mutation', ['process', 'bridge', 'generation'])
def test_restart_wrapper_rejects_replaced_owner_at_cleanup(
        monkeypatch, mutation):
    import main
    from core.capture_settings import ApplyStatus

    process_a = _Process()
    process_b = _Process()
    bridge_a = _Bridge()
    bridge_b = _Bridge()
    host = _host(main, bridge_a)
    host.engine_process = process_a
    host._engine_gen = 12
    host._restart_pending = True
    host._capture_settings_applying = True
    host._capture_config.status = ApplyStatus.FAILED
    host._capture_config.error = 'launch failed'
    tracker_revision = host._capture_config.transition_revision
    tracker_snapshot = (
        host._capture_config.status,
        host._capture_config.error,
        host._capture_config.transition_revision)
    host._manual_record_state = 'preparing'
    host._pending_manual_record_path = Path('capture-a.mp4')
    manual_failures = []
    host._fail_manual_recording = lambda detail: manual_failures.append(detail)
    monkeypatch.setattr(main, 'CaptureBridge', lambda: bridge_a)

    def fake_start_engine(*, _launch_receipt, _prepared_request=None):
        _launch_receipt.update({
            'generation': 12,
            'config': None,
            'token': None,
            'terminal': {
                'generation': 12,
                'process': process_a,
                'bridge': bridge_a,
                'tracker': host._capture_config,
                'token': None,
                'error': 'launch failed',
                'revision': tracker_revision,
            },
        })
        return False

    host.start_engine = fake_start_engine

    def clear_apply_state(state):
        host._capture_settings_applying = state
        if not state:
            if mutation == 'process':
                host.engine_process = process_b
            elif mutation == 'bridge':
                host.bridge = bridge_b
            else:
                host._engine_gen += 1

    host._set_capture_apply_state = clear_apply_state
    host._finish_restart = partial(main.MainWindow._finish_restart, host)

    assert host._finish_restart(12) is False
    assert host.engine_process is (process_b if mutation == 'process' else process_a)
    assert host.bridge is (bridge_b if mutation == 'bridge' else bridge_a)
    assert host._engine_gen == (13 if mutation == 'generation' else 12)
    assert (
        host._capture_config.status,
        host._capture_config.error,
        host._capture_config.transition_revision) == tracker_snapshot
    assert manual_failures == []


def _health_failure_host(main, *, snapshot_changed, manual_state='preparing'):
    from core.capture_health import CaptureHealthSnapshot, CaptureHealthState

    config_a = CaptureConfig(
        fps=60, buffer_seconds=32, width=1920, height=1080,
        bitrate_kbps=25_000, codec='h264', preset=4,
        monitor='', scaling='stretch', audio_enabled=False)
    config_b = replace(config_a, encoder='software')
    process = _Process()
    bridge = SimpleNamespace(
        is_connected=lambda: True,
        get_status=lambda: {
            'connected': True, 'frames_captured': 10,
            'capture_generation': 4, 'nvenc_active': True,
            'capture_health_flags': 0,
        },
        get_active_codec=lambda: '',
    )
    host = _host(main, bridge)
    host.engine_process = process
    host._engine_gen = 12
    host._engine_process_generation = 12
    host._capture_config.request(config_a)
    token_a = host._capture_config.begin_apply()
    host._pending_launch_config = config_a
    host._pending_launch_generation = 12
    host._pending_launch_token = token_a
    _bind_pending_capture_request(host, config_a)
    host._capture_settings_applying = True
    snapshot = CaptureHealthSnapshot(
        state=CaptureHealthState.FAILED,
        previous_state=CaptureHealthState.HEALTHY,
        reason='capture stalled', changed=snapshot_changed,
        request_recovery=False, seconds_since_progress=8.0,
        fresh_buffer_seconds=0.0)
    host._capture_health = SimpleNamespace(
        observe=lambda **_kwargs: snapshot)
    host._capture_health_snapshot = snapshot
    host._pump_save_responses = lambda: None
    host._advance_recoverable_errors = lambda **_: None
    host._maybe_restart_for_display_mode_change = lambda _status: None
    host._background_ui_paused = False
    host._ui_ready = False
    host.is_capturing = True
    host._capture_pending_text = lambda: 'APPLYING SETTINGS'
    host._last_capture_health_event_at = main.time.monotonic()
    health_logs = []
    host._capture_health_log = SimpleNamespace(
        info=lambda *args: health_logs.append(('info', args)),
        warning=lambda *args: health_logs.append(('warning', args)))
    host._manual_record_state = manual_state
    host._pending_manual_record_path = (
        Path('capture-a.mp4') if manual_state == 'preparing' else None)
    manual_failures = []
    host._fail_manual_recording = lambda detail: manual_failures.append(detail)
    return host, config_a, config_b, manual_failures, health_logs


@pytest.mark.parametrize('snapshot_changed', [False, True])
def test_health_failure_ui_reentrancy_preserves_new_preparation(
        monkeypatch, snapshot_changed):
    import main
    from core.capture_settings import ApplyStatus

    host, _config_a, config_b, manual_failures, health_logs = (
        _health_failure_host(main, snapshot_changed=snapshot_changed))
    events = []
    monkeypatch.setattr(
        main, 'emit_event', lambda *args, **kwargs: events.append((args, kwargs)))
    ui_states = []

    def apply_state(state):
        ui_states.append(state)
        host._capture_settings_applying = state
        if not state:
            host._capture_config.request(config_b)
            token_b = host._capture_config.begin_apply()
            host._pending_launch_config = config_b
            host._pending_launch_generation = host._engine_gen
            host._pending_launch_token = token_b
            _bind_pending_capture_request(host, config_b)
            host._manual_record_state = 'preparing'
            host._pending_manual_record_path = Path('capture-b.mp4')
            host._capture_settings_applying = True

    host._set_capture_apply_state = apply_state
    main.MainWindow._update_status(host)

    assert host._capture_config.status is ApplyStatus.APPLYING
    assert host._capture_config.applying == config_b
    assert host._pending_launch_config is config_b
    assert host._pending_launch_token == host._capture_config.applying_token
    assert host._pending_manual_record_path == Path('capture-b.mp4')
    assert host._capture_settings_applying is True
    assert ui_states == [False]
    assert manual_failures == []
    assert host._set_status_history == []
    assert host._errors == []
    assert events == []
    assert health_logs == []


def test_health_failure_manual_hook_reentrancy_preserves_new_apply(monkeypatch):
    import main
    from core.capture_settings import ApplyStatus

    host, _config_a, config_b, manual_failures, health_logs = (
        _health_failure_host(main, snapshot_changed=True))
    events = []
    monkeypatch.setattr(
        main, 'emit_event', lambda *args, **kwargs: events.append((args, kwargs)))
    host._set_capture_apply_state = lambda state: setattr(
        host, '_capture_settings_applying', state)

    def fail_manual(_detail):
        manual_failures.append('A')
        host._capture_config.request(config_b)
        token_b = host._capture_config.begin_apply()
        host._pending_launch_config = config_b
        host._pending_launch_generation = host._engine_gen
        host._pending_launch_token = token_b
        _bind_pending_capture_request(host, config_b)
        host._manual_record_state = 'preparing'
        host._pending_manual_record_path = Path('capture-b.mp4')
        host._capture_settings_applying = True

    host._fail_manual_recording = fail_manual
    main.MainWindow._update_status(host)

    assert manual_failures == ['A']
    assert host._capture_config.status is ApplyStatus.APPLYING
    assert host._capture_config.applying == config_b
    assert host._pending_launch_config is config_b
    assert host._pending_launch_token == host._capture_config.applying_token
    assert host._pending_manual_record_path == Path('capture-b.mp4')
    assert host._capture_settings_applying is True
    assert host._set_status_history == []
    assert host._errors == []
    assert events == []
    assert health_logs == []


@pytest.mark.parametrize('manual_state', ['idle', 'preparing'])
def test_health_failure_owned_tail_keeps_normal_failure_reporting(manual_state):
    import main
    from core.capture_settings import ApplyStatus

    host, _config_a, _config_b, manual_failures, health_logs = (
        _health_failure_host(main, snapshot_changed=True,
                             manual_state=manual_state))
    if manual_state == 'preparing':
        def fail_manual(detail):
            manual_failures.append(detail)
            host._manual_record_state = 'idle'
            host._pending_manual_record_path = None

        host._fail_manual_recording = fail_manual
    host._set_capture_apply_state = lambda state: setattr(
        host, '_capture_settings_applying', state)
    main.MainWindow._update_status(host)

    assert host._capture_config.status is ApplyStatus.FAILED
    assert host._capture_config.error == 'capture stalled'
    assert host._capture_settings_applying is False
    assert host._set_status_history == ['CAPTURE FAILED']
    assert len(host._errors) == 1
    assert manual_failures == (
        ['The recording quality profile did not become healthy.']
        if manual_state == 'preparing' else [])
    assert len(health_logs) == 1


def test_backend_failure_becomes_visible_after_recovery_is_no_longer_pending():
    import main

    host, *_ = _health_failure_host(
        main, snapshot_changed=False, manual_state='idle')
    host._pending_launch_config = None
    host._capture_settings_applying = False
    host._display_mode_restart_pending = True
    host._display_mode_restart_pending_generation = host._engine_gen
    get_status = host.bridge.get_status
    host.bridge.get_status = lambda: dict(
        get_status(), capture_health_flags=int(main.CaptureHealthFlag.BACKEND_FAILED))

    main.MainWindow._update_status(host)
    assert host._errors == []
    assert host._set_status_history == ['RECOVERING CAPTURE']

    # The health snapshot has not changed. A cancelled/exhausted recovery must
    # still expose the failure, exactly once, on the next heartbeat.
    host._display_mode_restart_pending = False
    main.MainWindow._update_status(host)
    main.MainWindow._update_status(host)
    assert len(host._errors) == 1
    assert host._errors[0][0][0] == 'CAPTURE FAILED'
    assert host._set_status_history[-1] == 'CAPTURE FAILED'


@pytest.mark.parametrize('owner_loss', ['shutdown', 'engine'])
def test_health_failure_owner_loss_skips_old_tail(monkeypatch, owner_loss):
    import main

    host, _config_a, _config_b, _manual_failures, health_logs = (
        _health_failure_host(main, snapshot_changed=True, manual_state='idle'))
    events = []
    monkeypatch.setattr(
        main, 'emit_event', lambda *args, **kwargs: events.append((args, kwargs)))

    def clear_apply_state(state):
        host._capture_settings_applying = state
        if not state:
            if owner_loss == 'shutdown':
                host._shutdown_requested = True
            else:
                host._engine_gen += 1

    host._set_capture_apply_state = clear_apply_state
    main.MainWindow._update_status(host)

    assert host._capture_config.status is main.ApplyStatus.FAILED
    assert host._set_status_history == []
    assert host._errors == []
    assert events == []
    assert health_logs == []


@pytest.mark.parametrize(
    'branch', ['x11-missing', 'x11-resolve', 'win-argument'])
@pytest.mark.parametrize('action', ['begin-b', 'shutdown'])
def test_terminal_failure_ui_hooks_stop_stale_branch_before_old_effects(
        monkeypatch, branch, action):
    import main
    from core.windows_monitor import MonitorSelectionResult, MonitorSelectionState

    host = _host(main, _Bridge())
    config_a = host._requested_capture_config()
    config_b = replace(config_a, encoder='software')
    host._requested_capture_config = lambda: config_a
    host._manual_record_state = 'preparing'
    host._pending_manual_record_path = Path('capture-a.mp4')
    manual_failures = []
    host._fail_manual_recording = lambda detail: manual_failures.append(detail)
    statuses = []
    host._set_status = lambda text, _style: statuses.append(text)
    events = []
    monkeypatch.setattr(
        main, 'emit_event', lambda *args, **kwargs: events.append((args, kwargs)))
    popen_calls = []
    monkeypatch.setattr(
        main.subprocess, 'Popen',
        lambda *args, **kwargs: popen_calls.append((args, kwargs)))

    if branch.startswith('x11'):
        monkeypatch.setattr(main.sys, 'platform', 'linux')
        monkeypatch.setattr(main, 'is_native_x11_session', lambda: True)
        if branch == 'x11-missing':
            monkeypatch.setattr(main.linux_tools, 'path', lambda _name: None)
        else:
            monkeypatch.setattr(main.linux_tools, 'path', lambda _name: 'xrandr')
            monkeypatch.setattr(
                main, 'resolve_x11_capture_target',
                lambda *_args: (_ for _ in ()).throw(
                    main.X11MonitorError('RandR resolution failed')))
    else:
        monkeypatch.setattr(main.sys, 'platform', 'win32')
        selection = MonitorSelectionResult(
            state=MonitorSelectionState.STALE,
            code='MONITOR_STALE', reason='monitor argument stale',
            previous_state=MonitorSelectionState.RESOLVED,
            previous_path='\\\\?\\display#missing')
        monkeypatch.setattr(
            main, 'validate_engine_monitor_argument',
            lambda _value: (_ for _ in ()).throw(
                main.MonitorSelectionError(selection)))

    def apply_state(state):
        host._capture_settings_applying = state
        if not state:
            if action == 'begin-b':
                host._capture_config.request(config_b)
                token_b = host._capture_config.begin_apply()
                host._pending_launch_config = config_b
                host._pending_launch_generation = host._engine_gen
                host._pending_launch_token = token_b
                host._capture_settings_applying = True
                host._restart_pending = True
            else:
                host._shutdown_requested = True

    host._set_capture_apply_state = apply_state
    started = main.MainWindow.start_engine(host)

    assert started is False
    assert popen_calls == []
    assert statuses == []
    assert manual_failures == []
    assert not [fields for args, fields in events
                if args[:2] == ('engine', 'start_failed')]
    if action == 'begin-b':
        assert host._capture_config.applying == config_b
        assert host._capture_settings_applying is True
        assert host._restart_pending is True
        assert host._pending_launch_config is config_b
        assert host._pending_launch_token == host._capture_config.applying_token
    else:
        assert host._shutdown_requested is True
        assert host._capture_config.status is main.ApplyStatus.FAILED


def test_reentrant_diagnostic_failure_cannot_stop_new_apply_attempt(
        monkeypatch):
    import main
    from core.capture_settings import ApplyStatus

    host = _host(main, _Bridge())
    config_a = host._requested_capture_config()
    config_b = replace(config_a, codec='hevc')

    def diagnostics(_config):
        host._capture_config.request(config_b)
        token_b = host._capture_config.begin_apply()
        host._pending_launch_config = config_b
        host._pending_launch_generation = host._engine_gen
        host._pending_launch_token = token_b
        host._capture_settings_applying = True
        raise RuntimeError('diagnostic callback failed after B began')

    host._diagnostic_capture_config = diagnostics
    monkeypatch.setattr(main.sys, 'platform', 'win32')

    assert main.MainWindow.start_engine(host) is False
    assert host._capture_config.status is ApplyStatus.APPLYING
    assert host._capture_config.applying == config_b
    assert host._pending_launch_config == config_b
    assert host._pending_launch_token == host._capture_config.applying_token
    assert host._capture_settings_applying is True
    assert host.stop_engine_calls == []
    assert host._errors == []


def test_current_setup_failure_records_failed_before_cleanup(monkeypatch):
    import main
    from core.capture_settings import ApplyStatus

    host = _host(main, _Bridge())
    host._diagnostic_capture_config = lambda _config: (
        (_ for _ in ()).throw(RuntimeError('setup failed before Popen')))
    monkeypatch.setattr(main.sys, 'platform', 'win32')

    assert main.MainWindow.start_engine(host) is False
    assert host._capture_config.status is ApplyStatus.FAILED
    assert host._capture_config.error == 'setup failed before Popen'
    assert host.stop_engine_calls == [True]
    assert host._errors


@pytest.mark.parametrize(
    ('title', 'key', 'technical_cause', 'process_generation',
     'recovery_status', 'expected', 'expected_status'),
    [
        ('CAPTURE FAILED', 'encoder-init', 'ENCODER_INIT_FAILED', 12,
         None, True, 'awaiting_replacement_hardware_and_stable_progress'),
        ('CAPTURE FAILED', 'encoder-init', 'ENCODER_INIT_FAILED', 12,
         'manual_override', True, 'manual_override'),
        ('CAPTURE FAILED', 'other-key', 'ENCODER_INIT_FAILED', 12,
         None, False, 'not_recoverable'),
        ('CAPTURE FAILED', 'encoder-init', 'OTHER_FAILURE', 12,
         None, False, 'not_recoverable'),
        ('ENGINE NOT RESPONDING', 'encoder-init',
         'ENCODER_INIT_FAILED', 12, None, False, 'not_recoverable'),
        ('CAPTURE FAILED', 'encoder-init', 'ENCODER_INIT_FAILED', None,
         None, False, 'not_recoverable'),
    ],
)
def test_encoder_init_recovery_registration_requires_exact_incident_identity(
        monkeypatch, title, key, technical_cause, process_generation,
        recovery_status, expected, expected_status):
    import main

    events = []
    monkeypatch.setattr(main, 'emit_event',
                        lambda *args, **kwargs: events.append((args, kwargs)))
    host = SimpleNamespace(
        settings_manager=_Settings(),
        error_bar=SimpleNamespace(push=lambda *_args, **_kwargs: None),
        _recoverable_error_incidents={},
        _error_recovery_tracker=RecoveryTracker(),
    )

    main.MainWindow.push_error(
        host, title, 'failure', key=key,
        diagnostic_context={
            'technical_cause': technical_cause,
            'technical_subsystem': 'encoder',
            'process_generation': process_generation,
            'recovery_kind': 'encoder_init',
            'recovery_status': recovery_status,
        })

    assert ('encoder-init' in host._recoverable_error_incidents) is expected
    assert events[-1][1]['recovery_status'] == expected_status


def test_recoverable_error_removal_waits_for_stable_progress_and_resets(
        monkeypatch):
    import main

    events = []
    monkeypatch.setattr(main, 'emit_event',
                        lambda *args, **kwargs: events.append((args, kwargs)))
    now = [0.0]

    class _Clock:
        def __call__(self):
            return now[0]

    removed = []
    host = SimpleNamespace(
        _recoverable_error_incidents={
            'capture-backend': {
                'title': 'CAPTURE FAILED',
                'public_error_code': 'Error 001',
                'recovery_kind': 'capture_backend',
                'process_generation': 7,
            },
            'engine-launch': {
                'title': 'ENGINE NOT RESPONDING',
                'public_error_code': 'Error 004',
                'recovery_kind': 'engine_lineage',
                'process_generation': 8,
            },
        },
        _error_recovery_tracker=RecoveryTracker(clock=_Clock()),
        _engine_process_generation=8,
        error_bar=SimpleNamespace(remove_by_key=removed.append),
    )
    advance = partial(main.MainWindow._advance_recoverable_errors, host)

    advance(connected=True, frame_count=1,
            health_state=main.CaptureHealthState.HEALTHY,
            backend_generation=2)
    now[0] = 1.0
    advance(connected=True, frame_count=2,
            health_state=main.CaptureHealthState.HEALTHY,
            backend_generation=2)
    now[0] = 1.5
    advance(connected=False, frame_count=0,
            health_state=main.CaptureHealthState.STOPPED,
            backend_generation=2)
    now[0] = 2.0
    advance(connected=True, frame_count=3,
            health_state=main.CaptureHealthState.HEALTHY,
            backend_generation=2)
    now[0] = 4.0
    advance(connected=True, frame_count=4,
            health_state=main.CaptureHealthState.HEALTHY,
            backend_generation=2)
    now[0] = 5.0
    advance(connected=True, frame_count=5,
            health_state=main.CaptureHealthState.HEALTHY,
            backend_generation=2)
    now[0] = 5.1
    advance(connected=True, frame_count=6,
            health_state=main.CaptureHealthState.HEALTHY,
            backend_generation=2)
    now[0] = 7.1
    advance(connected=True, frame_count=7,
            health_state=main.CaptureHealthState.HEALTHY,
            backend_generation=2)

    assert removed == ['capture-backend']
    assert 'engine-launch' in host._recoverable_error_incidents
    assert 'capture-backend' not in host._recoverable_error_incidents
    recovered = next(fields for args, fields in events
                     if args[:2] == ('application', 'failure_recovered'))
    assert recovered['compatibility_error'] == 'Error 001'


@pytest.mark.parametrize('needs_reconfigure', [False, True])
def test_display_mode_recovery_is_owned_bounded_and_waits_for_recording(
        monkeypatch, needs_reconfigure):
    import main
    from core.capture_health import CaptureHealthFlag
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    callbacks = []
    monkeypatch.setattr(main.QTimer, 'singleShot', lambda delay, callback: callbacks.append(callback))
    flags = CaptureHealthFlag.BACKEND_FAILED
    if needs_reconfigure:
        flags |= CaptureHealthFlag.RECONFIGURE_REQUIRED
    status = {'connected': True, 'capture_health_flags': int(flags)}
    host = _host(main, SimpleNamespace(get_status=lambda: status))
    host.engine_process = object()
    restarted = []
    host._restart_capture_engine = lambda: restarted.append(True)
    recover = partial(main.MainWindow._maybe_restart_for_display_mode_change, host)
    host._manual_record_state = 'recording'
    recover(status)
    assert not callbacks
    host._manual_record_state = 'idle'
    recover(status); recover(status)
    assert len(callbacks) == 1
    callbacks.pop()()
    assert restarted == [True]
    assert len(host._display_mode_restart_times) == 1
    recover(status)
    assert callbacks == []  # The same engine generation cannot queue twice.
    host._engine_gen += 1
    recover(status)
    host.bridge = object()  # A queued callback must not restart a newer owner.
    callbacks.pop()()
    assert restarted == [True]
    assert len(host._display_mode_restart_times) == 1  # stale callback not charged
    host.bridge = SimpleNamespace(get_status=lambda: status)
    host._engine_gen += 1
    recover(status)
    assert len(callbacks) == 1
    callbacks.pop()()
    assert restarted == [True, True]
    host._engine_gen += 1
    recover(status)
    assert len(callbacks) == 1
    callbacks.pop()()
    assert restarted == [True, True, True]
    assert len(host._display_mode_restart_times) == 3
    host._engine_gen += 1
    recover(status)
    assert not callbacks  # No fourth callback after three actual restarts.


@pytest.mark.parametrize('cancel', ['stop', 'shutdown'])
def test_queued_backend_recovery_cannot_undo_manual_stop(monkeypatch, cancel):
    import main

    monkeypatch.setattr(main.sys, 'platform', 'win32')
    callbacks, restarted = [], []
    monkeypatch.setattr(main.QTimer, 'singleShot',
                        lambda _delay, callback: callbacks.append(callback))
    status = {'connected': True, 'capture_health_flags': int(
        main.CaptureHealthFlag.BACKEND_FAILED)}
    host = _host(main, SimpleNamespace(get_status=lambda: status,
                                      shutdown=lambda: None))
    host.engine_process = SimpleNamespace(wait=lambda **_kw: 0, returncode=0)
    host._restart_capture_engine = lambda: restarted.append(True)
    main.MainWindow._maybe_restart_for_display_mode_change(host, status)
    assert len(callbacks) == 1
    if cancel == 'stop':
        main.MainWindow.stop_engine(host)
    else:
        host._shutdown_requested = True
    callbacks.pop()()
    assert restarted == []
    assert not getattr(host, '_display_mode_restart_times', [])


def test_display_mode_budget_resets_from_prior_sustained_health_before_callback(
        monkeypatch):
    import main
    from core.capture_health import CaptureHealthFlag

    monkeypatch.setattr(main.sys, 'platform', 'win32')
    monkeypatch.setattr(main.time, 'monotonic', lambda: 100.0)
    callbacks = []
    monkeypatch.setattr(main.QTimer, 'singleShot',
                        lambda _delay, callback: callbacks.append(callback))
    flags = int(CaptureHealthFlag.BACKEND_FAILED |
                CaptureHealthFlag.RECONFIGURE_REQUIRED)
    bridge = SimpleNamespace(get_status=lambda: {'capture_health_flags': flags})
    host = _host(main, bridge)
    host.engine_process = object()
    host.settings_manager = _Settings()
    host._source_authority_revision = 0
    host._capture_settings_applying = False
    host._display_mode_restart_times = [10.0, 20.0, 30.0]
    host._capture_health._healthy_since = 60.0
    host._capture_health._fresh_duration_s = 30.0
    host._capture_health_snapshot = SimpleNamespace(
        state=main.CaptureHealthState.HEALTHY)
    restarted = []
    host._restart_capture_engine = lambda: restarted.append(True)

    main.MainWindow._maybe_restart_for_display_mode_change(host, {
        'connected': True, 'capture_health_flags': flags})

    assert host._display_mode_restart_times == []
    assert len(callbacks) == 1
    # The next health poll has already observed the failure by callback time;
    # reset was correctly decided from the previous healthy snapshot.
    host._capture_health_snapshot.state = main.CaptureHealthState.FAILED
    host._capture_health._healthy_since = None
    host._capture_health._fresh_duration_s = 0.0
    callbacks.pop()()
    assert restarted == [True]
    assert len(host._display_mode_restart_times) == 1


@pytest.mark.parametrize('state, fresh_duration', [
    ('initializing', 0.0), ('healthy', 0.0), ('healthy', 29.999),
])
def test_display_mode_budget_does_not_reset_before_30s_frame_progress(
        monkeypatch, state, fresh_duration):
    import main
    from core.capture_health import CaptureHealthFlag

    monkeypatch.setattr(main.sys, 'platform', 'win32')
    monkeypatch.setattr(main.time, 'monotonic', lambda: 100.0)
    callbacks = []
    monkeypatch.setattr(main.QTimer, 'singleShot',
                        lambda _delay, callback: callbacks.append(callback))
    flags = int(CaptureHealthFlag.RECONFIGURE_REQUIRED)
    bridge = SimpleNamespace(get_status=lambda: {'capture_health_flags': flags})
    host = _host(main, bridge)
    host.engine_process = object()
    host.settings_manager = _Settings()
    host._source_authority_revision = 0
    host._capture_settings_applying = False
    host._display_mode_restart_times = [10.0, 20.0, 30.0]
    host._capture_health._healthy_since = 60.0
    host._capture_health._fresh_duration_s = fresh_duration
    host._capture_health_snapshot = SimpleNamespace(
        state=main.CaptureHealthState(state))

    main.MainWindow._maybe_restart_for_display_mode_change(host, {
        'connected': True, 'capture_health_flags': flags})

    assert callbacks == []
    assert host._display_mode_restart_times == [10.0, 20.0, 30.0]

def test_exited_startup_heartbeat_does_not_revoke_worker_failure_receipt(monkeypatch):
    import main
    host = _host(main, _Bridge(), startup_text=(
        'FTHR_STARTUP_ERROR: CAPTURE_TARGET_UNAVAILABLE: selected window is minimized'))
    host._capture_settings_applying = True
    host._restart_pending = True
    started, targets, process = _run_start(monkeypatch, main, host)
    assert started
    process.returncode = 1
    revocations = []
    host._begin_admission_transition = lambda **kw: revocations.append(kw)
    # Tick before the worker polls, then while its failure callback is queued.
    main.MainWindow._update_status(host)
    assert not revocations
    targets[0]()
    assert len(host._queued_callbacks) == 1
    main.MainWindow._update_status(host)
    assert not revocations
    host._queued_callbacks.pop()()
    assert host.engine_process is None
    assert host._pending_launch_request is None
    assert host._capture_settings_applying is False
    assert host._restart_pending is False
    assert host._engine_handshake_generation is None
    assert host._set_status_history[-1] == 'DISCONNECTED'
    main.MainWindow._update_status(host)
    assert host._set_status_history[-1] == 'DISCONNECTED'
    assert host._errors[-1][1]['diagnostic_context']['technical_cause'] == 'CAPTURE_TARGET_UNAVAILABLE'
    # Explicit Desktop recovery gets a new receipt, not a stale busy/apply gate.
    host.settings_manager.set('capture_mode', 'desktop')
    host.bridge = _Bridge(initialize_result=True)
    started, targets, replacement = _run_start(monkeypatch, main, host)
    assert started
    assert host.engine_process is replacement
    assert host._pending_launch_request.source.source_mode == 'desktop'
    targets[0]()
    host._queued_callbacks.pop()()
    assert host._engine_handshake_generation is None

@pytest.mark.parametrize('next_action', ['restore', 'select-desktop', 'shutdown'])
def test_minimized_window_handoff_retains_engine_and_retries_only_current_intent(monkeypatch, next_action):
    import main
    from core.capture_session import SourceSnapshot
    callbacks = []
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    monkeypatch.setattr(main.QTimer, 'singleShot', lambda delay, cb: callbacks.append(cb))
    ready = [False]
    monkeypatch.setattr(main, 'is_capture_window_renderable', lambda hwnd: ready[0])
    host = _host(main, _Bridge())
    process = _Process()
    host.engine_process = process
    host._prepared_source_is_live = lambda request: True
    host._publish_capture_profile_status = lambda **kw: None
    host._queue_latest_desired_request = partial(main.MainWindow._queue_latest_desired_request, host)
    source = SourceSnapshot(source_mode='window', exe_path=r'C:\Games\Minecraft.exe',
        live_identity={'hwnd': 42, 'pid': 3, 'exe_path': r'C:\Games\Minecraft.exe',
                       'class_name': 'GLFW30', 'process_created': 1.0})
    request = host._resolve_capture_request(source_snapshot=source)
    retries = []
    host._apply_latest_desired_request_if_idle = lambda: retries.append(host._desired_capture_request)
    assert main.MainWindow._defer_unready_capture_window(host, request)
    assert host.engine_process is process
    assert host.stop_engine_calls == []
    assert host._capture_settings_applying is False
    assert host._restart_pending is False
    assert host._latest_capture_intent_state == 'queued'
    assert len(callbacks) == 1
    assert main.MainWindow._defer_unready_capture_window(host, request)
    assert len(callbacks) == 1
    if next_action == 'restore':
        ready[0] = True
    elif next_action == 'select-desktop':
        host._desired_capture_request = host._resolve_capture_request()
    else:
        host._shutdown_requested = True
    callbacks.pop()()
    assert retries == ([request] if next_action == 'restore' else [])
    if next_action == 'restore':
        assert not main.MainWindow._defer_unready_capture_window(host, request)
