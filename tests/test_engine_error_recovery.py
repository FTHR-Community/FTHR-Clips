from __future__ import annotations

from functools import partial
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from core.capture_settings import CaptureConfig, CaptureConfigTracker
from core.error_recovery import RecoveryTracker


class _Settings:
    def get(self, key, default=None):
        return {
            'capture_mode': 'desktop',
            'target_hwnd': 0,
            'mic_volume': 100,
        }.get(key, default)


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
    host = SimpleNamespace(
        _engine_gen=0,
        _engine_process_generation=None,
        _diagnostic_engine_start_count=0,
        _diagnostics=None,
        _capture_config=CaptureConfigTracker(),
        _requested_capture_config=lambda: config,
        _diagnostic_capture_config=main.MainWindow._diagnostic_capture_config,
        _restart_pending=False,
        _restart_capture_engine=lambda: None,
        _set_capture_apply_state=lambda _applying: None,
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
        _manual_record_state='idle',
        _ui_ready=False,
        _set_status_history=statuses,
        _errors=errors,
        _queued_callbacks=queued,
        _startup_text=startup_text,
        stop_engine_calls=[],
    )
    host._owns_engine_attempt = partial(
        main.MainWindow._owns_engine_attempt, host)
    host._read_engine_startup_output = lambda _capture=None: host._startup_text
    host._schedule_owned_hardware_check = lambda *_args: None
    host._ui_call = SimpleNamespace(
        emit=(queued.append if queue_ui else lambda callback: callback()))
    production_stop = partial(main.MainWindow.stop_engine, host)

    def stop_engine():
        host.stop_engine_calls.append(True)
        return production_stop()

    host.stop_engine = stop_engine
    return host


def _run_start(monkeypatch, main, host, *, process=None, popen_error=None):
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
        _set_status=lambda *_args: None,
        _diagnostics=None,
        _diagnostic_engine_start_count=1,
    )

    host._maybe_restart_for_display_mode_change = partial(main.MainWindow._maybe_restart_for_display_mode_change, host)
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
    host._capture_config.begin_apply()
    host._engine_gen = 12
    host._pending_launch_config = host._capture_config.requested
    host._pending_launch_generation = generation
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


def test_display_mode_recovery_is_owned_bounded_and_waits_for_recording(monkeypatch):
    import main
    from core.capture_health import CaptureHealthFlag
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    callbacks = []
    monkeypatch.setattr(main.QTimer, 'singleShot', lambda delay, callback: callbacks.append(callback))
    status = {'connected': True, 'capture_health_flags': int(CaptureHealthFlag.BACKEND_FAILED | CaptureHealthFlag.RECONFIGURE_REQUIRED)}
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
    host._engine_gen += 1
    recover(status)
    host.bridge = object()  # A queued callback must not restart a newer owner.
    callbacks.pop()()
    assert restarted == [True]
    host.bridge = SimpleNamespace(get_status=lambda: status)
    host._engine_gen += 1
    recover(status)
    assert len(callbacks) == 1
    host._engine_gen += 1
    recover(status)
    assert len(callbacks) == 1  # At most three scheduled attempts in thirty seconds.
