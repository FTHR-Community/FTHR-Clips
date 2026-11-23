"""Windows profile and capture-session boundary checks."""

from __future__ import annotations

from dataclasses import replace
from functools import partial
from types import MappingProxyType, SimpleNamespace

import pytest

_PROFILE_ID = '12345678-1234-4234-8234-1234567890ab'
_WINDOW = {
    'hwnd': 41,
    'pid': 9001,
    'exe_path': r'C:\Games\Alpha\Game.exe',
    'class_name': 'GameWindow',
    'process_created': 123,
    'display_name': 'Alpha - Match',
    'title': 'Alpha - Match',
}


def _saved_identity(window):
    from core.source_authority import window_identity

    identity = window_identity(window)
    assert identity is not None
    return identity


_ALPHA_IDENTITY = _saved_identity(_WINDOW)
_BETA_WINDOW = dict(
    _WINDOW, exe_path=r'C:\Games\Beta\Game.exe',
    display_name='Beta - Match', title='Beta - Match',
)
_BETA_IDENTITY = _saved_identity(_BETA_WINDOW)


class _Settings:
    def __init__(self, catalog, **overrides):
        self.settings = {
            'capture_mode': 'window',
            'target_hwnd': _WINDOW['hwnd'],
            'target_window_name': _WINDOW['display_name'],
            'target_clip_group': 'Alpha',
            'target_window_identity': dict(_ALPHA_IDENTITY),
            'capture_source_authority': 'manual',
            'capture_monitor': '',
            'codec_pref': 'auto',
            'encoder_pref': 'auto',
            'encoder_preset': 4,
            'scaling_mode': 'stretch',
            'clip_length': 30,
            'framerate': 60,
            'resolution': 'source',
            'bitrate_level': 'medium',
            'custom_bitrate_kbps': 25_000,
            'recording_framerate': 60,
            'recording_resolution': 'source',
            'recording_bitrate_level': 'medium',
            'recording_custom_bitrate_kbps': 25_000,
            'audio_capture_enabled': True,
            'audio_capture_mode': 'combined',
            'audio_source_mode': None,
            'audio_device_tracks': [],
            'per_app_audio_tracks': None,
            'mic_device_id': None,
            'playback_device_id': None,
            'mic_downmix_mono': False,
            'mic_volume': 100,
            'camera_enabled': False,
            'camera_device_index': 0,
            'camera_overlay_rect': {'x': 0.7, 'y': 0.7, 'w': 0.2, 'h': 0.2},
            'capture_card_enabled': True,
            'image_overlay_enabled': False,
            'image_overlay_path': '',
            'image_overlay_opacity': 100,
            'image_overlay_fit': 'fit',
            'image_overlay_rect': {'x': 0.0, 'y': 0.0, 'w': 1.0, 'h': 1.0},
            'keyboard_overlay_enabled': False,
            'keyboard_overlay_rect': {'x': 0.3, 'y': 0.7, 'w': 0.4, 'h': 0.25},
            'game_detection_custom_games': [],
            'upload_account': {'username': 'must-not-enter-capture'},
        }
        self.settings.update(overrides)
        self.values = self.settings
        self.catalog = catalog
        self.save_count = 0

    def get(self, key, default=None):
        return self.settings.get(key, default)

    def set(self, key, value):
        self.settings[key] = value

    def save_settings(self):
        self.save_count += 1
        return True

    def read_capture_admission_policy(self):
        from core.settings_manager import SettingsManager

        return SettingsManager.read_capture_admission_policy(self)

    def read_game_capture_profiles(self):
        if isinstance(self.catalog, BaseException):
            raise self.catalog
        return self.catalog


class _Button:
    def __init__(self):
        self.enabled = None
        self.text = ''

    def setEnabled(self, enabled):
        self.enabled = bool(enabled)

    def setText(self, text):
        self.text = str(text)

    def setToolTip(self, _text):
        pass


class _Timer:
    def __init__(self):
        self.starts = 0
        self.stops = 0

    def start(self):
        self.starts += 1

    def stop(self):
        self.stops += 1


class _Process:
    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode


class _Bridge:
    def __init__(self, *, responses=(), capture_generation=11):
        self.save_calls = []
        self.recording_calls = []
        self.responses = list(responses)
        self.capture_generation = capture_generation
        self._terminal_response_owners = {}

    def is_connected(self):
        return True

    def save_clip(self, path, duration):
        self.save_calls.append((str(path), int(duration)))
        return True

    def start_manual_recording(self, path):
        self.recording_calls.append(str(path))
        return True

    def consume_manual_recording_response(self):
        if self.responses:
            return self.responses.pop(0)
        return None

    def peek_manual_recording_response(self):
        return self.responses[0] if self.responses else None

    def get_status(self):
        return {
            'connected': True,
            'capture_generation': self.capture_generation,
            'is_recording': True,
            'capture_health_flags': 0,
        }

    def retain_terminal_response_owner(self, *, expected_capture_generation):
        if expected_capture_generation != self.capture_generation:
            return None
        return self._terminal_response_owners.setdefault(
            expected_capture_generation, object())

    def release_terminal_response_owner(self, owner=None):
        current = self._terminal_response_owners.get(self.capture_generation)
        if owner is None or owner is not current:
            return False
        del self._terminal_response_owners[self.capture_generation]
        return True


def _bind(host, main, *names):
    """Attach production MainWindow helpers to a narrow host double."""
    for name in names:
        descriptor = vars(main.MainWindow).get(name)
        helper = getattr(main.MainWindow, name)
        if isinstance(descriptor, staticmethod):
            setattr(host, name, helper)
        else:
            setattr(host, name, partial(helper, host))
    return host


def _bind_real_admission_helpers(host, main):
    for name, descriptor in vars(main.MainWindow).items():
        if not name.startswith('_admission_'):
            continue
        helper = getattr(main.MainWindow, name)
        if isinstance(descriptor, staticmethod):
            setattr(host, name, helper)
        elif callable(helper):
            setattr(host, name, partial(helper, host))
    return host


def _accepted_tracker(request):
    from core.capture_settings import CaptureConfigTracker

    tracker = CaptureConfigTracker(active=request.capture_config)
    # Exercise the production tracker transitions so accepted_token is a real
    # token, rather than assigning a sentinel that bypasses ownership checks.
    for _ in range(3):
        tracker.request(request.capture_config)
        token = tracker.begin_apply()
        assert tracker.succeed(token)
    assert tracker.accepted_token == 3
    return tracker


def _catalog(overrides=None, *, exe_path=None, name='Alpha'):
    from core.game_profiles import create_profile, empty_catalog

    return create_profile(
        empty_catalog(), name, exe_path or _WINDOW['exe_path'],
        overrides=overrides or {}, profile_id=_PROFILE_ID,
    )


def _request(token, profile_name='A', *, source=None, config=None,
             mic_gain=100, effects=None, camera=None, purpose='clips'):
    from core.capture_session import CameraSnapshot, SourceSnapshot
    from core.capture_session import build_resolved_capture_request
    from core.capture_settings import CaptureConfig

    if source is None:
        source = SourceSnapshot(source_mode='desktop', clip_group='Desktop')
    if config is None:
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
        request_token=token, purpose=purpose, source=source,
        capture_config=config, mic_gain_percent=mic_gain,
        camera=camera or CameraSnapshot(),
        effects=effects if effects is not None else {'capture_card_enabled': True},
        profile_id=_PROFILE_ID, profile_revision=1,
        profile_name=profile_name, profile_document_revision=1,
    )


def _session(request, *, process=7, native=11, token=3):
    from core.capture_session import bind_capture_session

    return bind_capture_session(
        request, process_generation=process,
        native_capture_generation=native, accepted_config_token=token,
    )


def _resolve_host(main, catalog, *, window=None, **settings):
    values = dict(settings)
    manager = _Settings(catalog, **values)
    host = SimpleNamespace(
        settings_manager=manager,
        _capture_request_token=0,
        _source_intent_revision=4,
        _active_game_window=None,
        _engine_profile='clips',
        _capture_request_intents={},
        _capture_intent_revision=0, _latest_capture_intent_id=None,
        _desired_capture_intent_id=None,
        _shutdown_requested=False,
        _latest_capture_intent_state='none',
        _latest_capture_intent_consumed=True,
        statuses=[], errors=[],
    )
    live_window = dict(window or _WINDOW)
    host._set_status = lambda text, _style: host.statuses.append(text)
    host.push_error = lambda *args, **kwargs: host.errors.append((args, kwargs))
    host._settings_page_widget = SimpleNamespace(
        set_profile_capture_status=lambda *_args: None)
    _bind(
        host, main,
        '_capture_global_defaults_snapshot', '_capture_source_snapshot',
        '_begin_capture_intent', '_capture_request_intent_id',
        '_latest_intent_owns_request', '_consume_capture_intent',
        '_resolve_capture_request', '_prepare_next_capture_request',
        '_accepted_profile_status', '_publish_capture_profile_status',
        '_capture_requests_equivalent', '_capture_resources_busy',
        '_queue_latest_desired_request', '_apply_latest_desired_request_if_idle',
        '_refresh_capture_summary',
    )
    _bind_real_admission_helpers(host, main)
    return host, manager, live_window


def _patch_resolver(monkeypatch, main, live_window):
    from core.windows_monitor import MonitorChoice

    monkeypatch.setattr(main.sys, 'platform', 'win32')
    monkeypatch.setattr(main, 'get_window_info',
                        lambda _hwnd, own_pid=None: dict(live_window))
    # Keep monitor discovery behind a deterministic native boundary while the
    # real resolver validates the persisted path and primary choice.
    monkeypatch.setattr(main, 'enumerate_windows_monitors', lambda: [
        MonitorChoice('\\\\.\\DISPLAY1', 'FTHR primary',
                      r'\\?\display#fthr-primary', True),
    ])
    monkeypatch.setattr(main, 'third_party_keyboard_settings', lambda _sm: {})
    monkeypatch.setattr(main, 'image_overlay_layers', lambda _sm: [])


def _owned_host(main, request, *, settings_manager=None):
    """Create an accepted A owner using the real tracker/session contracts."""
    session = _session(request)
    settings_manager = settings_manager or _Settings(None)
    tracker = _accepted_tracker(request)
    bridge = _Bridge()
    process = _Process()
    host = SimpleNamespace(
        settings_manager=settings_manager,
        _accepted_capture_session=session,
        _accepted_camera_runtime_receipt=None,
        _capture_request_token=2, _capture_request_intents={},
        _capture_intent_revision=0, _latest_capture_intent_id=None,
        _desired_capture_intent_id=None,
        _source_intent_revision=4, _active_game_window=None,
        _engine_profile='clips',
        _capture_config=tracker,
        engine_process=process, bridge=bridge,
        _engine_process_generation=session.process_generation,
        _engine_gen=session.process_generation,
        _shutdown_requested=False, _shutdown_complete=False,
        _file_operations_ready=True, _manual_record_state='idle',
        _manual_record_finalize_thread=None, _mux_threads=(),
        _restart_pending=False, _capture_settings_applying=False,
        _desired_capture_request=None, _pending_capture_reason='',
        _latest_capture_intent_state='none',
        _latest_capture_intent_consumed=True,
        statuses=[], errors=[],
    )
    host._set_status = lambda text, _style: host.statuses.append(text)
    host.push_error = lambda *args, **kwargs: host.errors.append((args, kwargs))
    host._set_capture_apply_state = lambda value: setattr(
        host, '_capture_settings_applying', bool(value))
    host._settings_page_widget = SimpleNamespace(
        set_profile_capture_status=lambda *_args: None)
    _bind(
        host, main,
        '_accepted_session_owner_is_current', '_prepared_source_is_live',
        '_manual_prepare_owns_capture',
        '_capture_requests_equivalent', '_capture_request_intent_id',
        '_accepted_profile_status', '_publish_capture_profile_status',
        '_capture_resources_busy', '_queue_latest_desired_request',
        '_apply_latest_desired_request_if_idle', '_restart_capture_engine',
        '_show_save_busy_feedback', '_capture_global_defaults_snapshot',
        '_capture_source_snapshot', '_begin_capture_intent',
        '_latest_intent_owns_request', '_consume_capture_intent',
        '_resolve_capture_request', '_prepare_next_capture_request',
        '_refresh_capture_summary', '_configure_keyboard_capture_for_request',
        '_camera_runtime_receipt_for_request', '_capture_owner_snapshot',
        '_capture_owner_snapshot_is_current',
        '_retain_terminal_response_owner_cache',
    )
    _bind_real_admission_helpers(host, main)
    return host, session, process, bridge, tracker


def test_real_main_resolves_exact_exe_and_keeps_same_basename_separate(
        monkeypatch):
    import main

    catalog = _catalog(name='Alpha')
    host, _manager, live = _resolve_host(main, catalog)
    _patch_resolver(monkeypatch, main, live)

    request = main.MainWindow._resolve_capture_request(host)
    assert request.profile_name == 'Alpha'
    assert request.source.exe_path == r'c:\games\alpha\game.exe'

    other = dict(_BETA_WINDOW)
    host, _manager, live = _resolve_host(
        main, catalog, window=other,
        target_window_name=other['display_name'],
        target_clip_group='Beta',
        target_window_identity=_BETA_IDENTITY,
    )
    _patch_resolver(monkeypatch, main, live)
    request = main.MainWindow._resolve_capture_request(host)
    assert request.profile_name is None
    assert request.source.exe_path == r'c:\games\beta\game.exe'
    assert request.source.live_identity == _BETA_IDENTITY

    # A live same-basename candidate must not be accepted under Alpha's
    # persisted identity; this is a separate fail-closed authority case.
    host, _manager, live = _resolve_host(main, catalog, window=other)
    _patch_resolver(monkeypatch, main, live)
    try:
        main.MainWindow._resolve_capture_request(host)
    except ValueError:
        pass
    else:
        raise AssertionError('swapped live window identity was accepted')


def test_real_main_profile_resolution_preserves_inherit_false_zero_and_gain(
        monkeypatch):
    import main

    catalog = _catalog({
        'clips': {'clip_length': 21, 'framerate': 144},
        'audio': {
            'audio_capture_enabled': False,
            'mic_volume': 0,
            'mic_device_id': 'profile-mic',
        },
        'camera': {'camera_enabled': False},
    })
    host, manager, live = _resolve_host(
        main, catalog, audio_capture_enabled=True, mic_volume=88,
        mic_device_id='global-mic', credentials={'token': 'secret'},
    )
    _patch_resolver(monkeypatch, main, live)
    request = main.MainWindow._resolve_capture_request(host)

    assert request.profile_name == 'Alpha'
    assert request.capture_config.normal_clip_seconds == 21
    assert request.capture_config.fps == 144
    assert request.capture_config.audio_enabled is False
    assert request.mic_gain_percent == 0
    assert request.capture_config.microphone_endpoint_id == 'profile-mic'
    assert 'credentials' not in request.to_dict()
    assert manager.get('credentials')['token'] == 'secret'


def test_future_catalog_rejects_new_request_before_stop_and_keeps_A_session(
        monkeypatch):
    import main

    from core.game_profiles import ProfileDataError

    from core.capture_session import SourceSnapshot
    request_a = _request(
        1, 'A', source=SourceSnapshot(
            source_mode='window', exe_path=_WINDOW['exe_path'],
            live_identity=_ALPHA_IDENTITY, display_name='Alpha - Match',
            clip_group='Alpha'))
    request_b = _request(2, 'B')
    host, accepted, process, bridge, tracker = _owned_host(
        main, request_a)
    manager = _Settings(ProfileDataError('future catalog'))
    host.settings_manager = manager
    host._desired_capture_request = request_b
    host._capture_request_intents = {2: 20}
    host.stop_calls = []
    host.restart_calls = []
    host.stop_engine = lambda: host.stop_calls.append(True)
    host._restart_capture_engine = partial(
        main.MainWindow._restart_capture_engine, host)
    _patch_resolver(monkeypatch, main, dict(_WINDOW))

    # The real prepare/resolve path rejects newest C before any stop.  It must
    # consume queued B while preserving accepted A's owner proof.
    main.MainWindow._restart_capture_engine(host)

    assert host.stop_calls == []
    assert host._accepted_capture_session is accepted
    assert host.engine_process is process
    assert host.bridge is bridge
    assert tracker.accepted_token == accepted.accepted_config_token == 3
    assert host._desired_capture_request is None
    assert host._latest_capture_intent_state == 'invalid'
    assert len(host.errors) == 1

    # Repeated idle/heartbeat flushes cannot resurrect stale B or stop A.
    host._save_state = SimpleNamespace(is_busy=lambda: False)
    for _ in range(3):
        assert host._apply_latest_desired_request_if_idle() is False
    assert host.stop_calls == []
    assert process.poll() is None
    assert bridge.get_status()['capture_generation'] == 11


@pytest.mark.parametrize('flags', [1 << 2, 1 << 5, (1 << 2) | (1 << 5)])
def test_failed_backend_restarts_identical_accepted_settings(monkeypatch, flags):
    """SOURCE geometry can change while the immutable request stays equal."""
    import main

    request = _request(1)
    host, accepted, process, bridge, _tracker = _owned_host(main, request)
    _patch_resolver(monkeypatch, main, dict(_WINDOW))
    callbacks, stops, launches = [], [], []
    monkeypatch.setattr(main.QTimer, 'singleShot',
                        lambda delay, callback: callbacks.append((delay, callback)))
    original_status = bridge.get_status
    bridge.get_status = lambda: dict(original_status(), capture_health_flags=flags,
                                     is_recording=False)
    host._prepare_next_capture_request = lambda _purpose: request
    host._capture_pending_text = lambda: 'RECOVERING CAPTURE'
    host._owns_engine_attempt = partial(main.MainWindow._owns_engine_attempt, host)
    host._finish_restart = lambda generation, **kw: launches.append((generation, kw))

    def stop():
        stops.append(process)
        host._engine_gen += 1
        host._accepted_capture_session = None

    host.stop_engine = stop
    main.MainWindow._maybe_restart_for_display_mode_change(host, bridge.get_status())
    assert callbacks[0][0] == 750
    callbacks.pop(0)[1]()
    assert stops == [process]
    assert host._restart_pending
    assert host._accepted_capture_session is None
    assert callbacks[0][0] == 1000
    callbacks.pop(0)[1]()
    assert launches == [(accepted.process_generation + 1, {'prepared_request': request})]
    assert len(host._display_mode_restart_times) == 1


@pytest.mark.parametrize('waiting_for_post_roll', [False, True])
def test_busy_profile_changes_coalesce_latest_B_or_C_without_native_restart(
        monkeypatch, waiting_for_post_roll):
    import main

    request_a = _request(1, 'A')
    catalog_b = _catalog(name='Beta')
    catalog_c = _catalog({'clips': {'clip_length': 21}}, name='Gamma')
    host, _session_a, _process, _bridge, _tracker = _owned_host(
        main, request_a)
    manager = _Settings(catalog_b)
    host.settings_manager = manager
    manager.catalogs = [catalog_b, catalog_c]
    manager.read_game_capture_profiles = lambda: manager.catalogs.pop(0)
    host._pending_post_roll = object() if waiting_for_post_roll else None
    host._save_state = SimpleNamespace(is_busy=lambda: not waiting_for_post_roll)
    host._settings_page_widget = SimpleNamespace(
        set_profile_capture_status=lambda *_args: None)
    host.stop_calls = []
    host.stop_engine = lambda: host.stop_calls.append(True)
    host._set_status = lambda text, _style: host.statuses.append(text)
    _patch_resolver(monkeypatch, main, dict(_WINDOW))

    main.MainWindow._restart_capture_engine(host)
    main.MainWindow._restart_capture_engine(host)
    assert host._desired_capture_request is not None
    assert host._desired_capture_request.profile_name == 'Gamma'
    assert host.stop_calls == []
    assert host.statuses == ['CAPTURE CHANGE PENDING', 'CAPTURE CHANGE PENDING']
    assert host._apply_latest_desired_request_if_idle() is False

    # Once the real owner releases, idle flush applies exactly the newest C;
    # queued B is never handed to stop/start.
    host._pending_post_roll = None
    host._save_state.is_busy = lambda: False
    apply_calls = []
    host._restart_capture_engine = lambda **kwargs: (
        apply_calls.append(kwargs.get('_prepared_request')) or False)
    assert host._apply_latest_desired_request_if_idle() is False
    assert apply_calls == [host._desired_capture_request]
    assert apply_calls[0].profile_name == 'Gamma'


def test_profile_status_keeps_accepted_A_visible_while_B_is_pending():
    import main

    request_a = _request(1, 'A')
    request_b = _request(2, 'B')
    received = []
    host = SimpleNamespace(
        _accepted_capture_session=_session(request_a),
        _desired_capture_request=request_b,
        _settings_page_widget=SimpleNamespace(
            set_profile_capture_status=lambda active, pending, message: received.append(
                (active, pending, message)),
        ),
    )
    host._refresh_capture_summary = lambda: None
    _bind(host, main, '_accepted_profile_status',
          '_publish_capture_profile_status')

    main.MainWindow._publish_capture_profile_status(
        host, pending=True, message='queued while saving')

    assert received == [(
        {'name': 'A', 'exe_path': ''},
        {'name': 'B', 'exe_path': ''},
        'queued while saving',
    )]


def test_invalid_latest_profile_consumes_pending_B_once_without_heartbeat_retry(
        monkeypatch):
    import main

    host, accepted, process, bridge, tracker = _owned_host(
        main, _request(1, 'A'))
    manager = _Settings(_catalog(name='B'))
    manager.catalogs = [manager.catalog, __import__(
        'core.game_profiles', fromlist=['ProfileDataError']).ProfileDataError(
            'invalid newest catalog')]
    manager.read_game_capture_profiles = lambda: (
        manager.catalogs.pop(0) if manager.catalogs else
        manager.catalog)
    host.settings_manager = manager
    host._save_state = SimpleNamespace(is_busy=lambda: True)
    host.stop_calls = []
    host.stop_engine = lambda: host.stop_calls.append(True)
    _patch_resolver(monkeypatch, main, dict(_WINDOW))
    main.MainWindow._restart_capture_engine(host)
    request_b = host._desired_capture_request
    assert request_b is not None
    assert request_b.profile_name == 'B'

    main.MainWindow._restart_capture_engine(host)
    assert host._desired_capture_request is None
    assert host._latest_capture_intent_state == 'invalid'

    # Release the owner and exercise the real idle flush repeatedly.  Invalid
    # C is terminal and cannot restart stale B or emit retry spam.
    host._save_state.is_busy = lambda: False
    production_restart = host._restart_capture_engine
    restart_calls = []

    def restart_spy(**kwargs):
        restart_calls.append(kwargs)
        return production_restart(**kwargs)

    host._restart_capture_engine = restart_spy
    for _ in range(3):
        assert host._apply_latest_desired_request_if_idle() is False
    assert host.stop_calls == []
    assert restart_calls == []
    assert host._accepted_capture_session is accepted
    assert host.engine_process is process
    assert host.bridge is bridge
    assert tracker.accepted_token == 3


def _save_host(main, session, tmp_path):
    from core.capture_health import CaptureHealthSnapshot, CaptureHealthState
    from core.save_state import SaveStateMachine

    bridge = _Bridge()
    process = _Process()
    tracker = _accepted_tracker(session.request)
    card = SimpleNamespace(show_error=lambda: None,
                           show_captured=lambda *_args: None,
                           show_clip=lambda *_args: None)
    host = SimpleNamespace(
        _manual_record_state='idle', _accepted_capture_session=session,
        _capture_config=tracker, engine_process=process, bridge=bridge,
        _engine_process_generation=session.process_generation,
        _engine_gen=session.process_generation,
        _shutdown_requested=False, _shutdown_complete=False,
        _accepted_camera_runtime_receipt={'generation': 11, 'device_index': 2},
        _capture_health_snapshot=CaptureHealthSnapshot(
            state=CaptureHealthState.HEALTHY,
            previous_state=CaptureHealthState.HEALTHY,
            reason='test', changed=False, request_recovery=False,
            seconds_since_progress=0.0, fresh_buffer_seconds=60.0,
        ),
        _save_state=SaveStateMachine(), capture_card=card,
        _save_poll_timer=_Timer(), _last_save_request=0.0,
        _save_diagnostic_started_at=0.0, _clip_diagnostic_started_by_path={},
        _capture_health_log=SimpleNamespace(warning=lambda *_args: None),
        settings_manager=_Settings(None, capture_mode='desktop'),
        _storage_admission=lambda _folder: (True, ''),
        _pump_save_responses=lambda: None,
        _set_status=lambda *_args: None,
        _update_status=lambda: None,
        push_error=lambda *_args, **_kwargs: None,
        _capture_settings_applying=False,
    )
    host._accepted_camera_runtime_receipt = None
    _bind(host, main, '_accepted_session_owner_is_current',
          '_prepared_source_is_live', '_show_save_busy_feedback',
          '_camera_runtime_receipt_for_request',
          '_accepted_session_owner_pure_is_current', '_capture_owner_snapshot',
          '_capture_owner_snapshot_is_current',
          '_retain_terminal_response_owner_cache')
    _bind_real_admission_helpers(host, main)
    return host, bridge


def test_save_admission_carries_immutable_A_session_and_effects_mappingproxy(
        monkeypatch, tmp_path):
    import main

    request = _request(
        1, 'A',
        effects=MappingProxyType({'capture_card_enabled': True,
                                  'watermark_enabled': False}),
    )
    session = _session(request)
    host, bridge = _save_host(main, session, tmp_path)
    monkeypatch.setattr(main, 'clips_directory_from', lambda _sm: tmp_path)
    monkeypatch.setattr(main, 'emit_event', lambda *_args, **_kwargs: None)

    main.MainWindow._save_clip(host, 30)

    operation = host._save_state.current
    assert operation is not None
    assert bridge.save_calls
    assert operation.context['session_snapshot'] is session
    assert operation.context['effects'] == request.to_dict()['effects']
    assert 'credentials' not in operation.context
    assert 'credentials' not in operation.context['effects']
    operation_effects = operation.context['effects']
    operation_effects['capture_card_enabled'] = False
    assert operation_effects['capture_card_enabled'] is False
    assert request.effects['capture_card_enabled'] is True
    assert operation.context['session_snapshot'].request.profile_name == 'A'


def test_save_capture_error_does_not_send_orphan_native_command(monkeypatch, tmp_path):
    import main

    host, bridge = _save_host(main, _session(_request(1, 'A')), tmp_path)
    host._accepted_capture_session = None
    host.push_error = lambda *_args, **_kwargs: None
    monkeypatch.setattr(main.QTimer, 'singleShot', lambda *_args: None)
    monkeypatch.setattr(main, 'clips_directory_from', lambda _sm: tmp_path)
    monkeypatch.setattr(main, 'emit_event', lambda *_args, **_kwargs: None)

    main.MainWindow._save_clip(host, 30)

    assert bridge.save_calls == []
    assert host._save_state.current is None


def test_post_roll_keeps_first_press_start_when_repeated_and_dispatched_late(
        monkeypatch, tmp_path):
    import main

    request = _request(1, 'A')
    request = replace(
        request,
        capture_config=replace(request.capture_config, buffer_seconds=42,
                               post_roll_seconds=5),
    )
    session = _session(request)
    host, bridge = _save_host(main, session, tmp_path)
    _bind(host, main, '_save_clip', '_arm_post_roll_timer',
          '_extend_pending_post_roll', '_cancel_pending_post_roll')
    timer_calls = []
    host._post_roll_timer = SimpleNamespace(start=timer_calls.append)
    bridge.supports_clip_boundary = lambda: True
    bridge.save_clip = lambda *args, **kwargs: bridge.save_calls.append((args, kwargs)) or True
    clock_ns = 100_000_000_000
    monkeypatch.setattr(main.time, 'monotonic_ns', lambda: clock_ns)
    monkeypatch.setattr(main, 'clips_directory_from', lambda _sm: tmp_path)
    monkeypatch.setattr(main, 'emit_event', lambda *_args, **_kwargs: None)

    host._save_clip(30, pressed_at_ns=99_100_000_000)
    assert not bridge.save_calls
    assert timer_calls == [4100]
    first = host._pending_post_roll
    assert first.end_ns == 104_100_000_000

    clock_ns = 103_300_000_000
    host._save_clip(60, pressed_at_ns=103_200_000_000)
    assert host._pending_post_roll is first
    assert first.duration_seconds == 40
    assert first.end_ns == 109_100_000_000
    assert timer_calls[-1] == 5800
    assert not bridge.save_calls

    clock_ns = 109_700_000_000
    main.MainWindow._finish_pending_post_roll(host)
    assert host._pending_post_roll is None
    assert len(bridge.save_calls) == 1
    args, kwargs = bridge.save_calls[0]
    assert args[1] == 40
    assert kwargs == {'end_monotonic_ns': 109_100_000_000}
    assert host._save_state.current.context['mic_end_time'] == 109.1
    assert host._save_state.current.context['session_snapshot'] is session


def test_pending_post_roll_reports_capture_owner_loss(monkeypatch):
    import main

    owner = object()
    pending = main._PendingPostRoll(owner, 100_000_000_000, 30, 5, 35, 40)
    errors = []
    timer = _Timer()
    host = SimpleNamespace(
        _pending_post_roll=pending, _post_roll_timer=timer,
        _accepted_session_owner_is_current=lambda session: False,
        capture_card=SimpleNamespace(show_error=lambda: None),
        push_error=lambda *args, **_kwargs: errors.append(args),
    )
    _bind(host, main, '_cancel_pending_post_roll')
    monkeypatch.setattr(main.time, 'monotonic_ns', lambda: pending.end_ns)
    monkeypatch.setattr(main, 'emit_event', lambda *_args, **_kwargs: None)

    main.MainWindow._finish_pending_post_roll(host)

    assert host._pending_post_roll is None
    assert timer.stops == 1
    assert errors == [('CLIP NOT SAVED',
                       'Capture changed before the additional seconds were ready.')]


def test_normal_hotkey_uses_accepted_profile_duration_after_global_edits():
    import main

    calls = []
    request_a = _request(1, 'A')
    request_a = replace(
        request_a,
        capture_config=replace(request_a.capture_config, normal_clip_seconds=60),
    )
    request_b = replace(
        request_a,
        request_token=2,
        capture_config=replace(request_a.capture_config, normal_clip_seconds=90),
    )
    tracker = _accepted_tracker(request_a)
    host = SimpleNamespace(
        _accepted_capture_session=_session(request_a),
        _capture_config=tracker, _desired_capture_request=request_b,
        clip_duration=30,
        _save_clip=lambda duration: calls.append(duration),
    )

    main.MainWindow._on_hotkey_save_clip(host)
    # B90 is pending while accepted A remains the live owner; global edits
    # must not change the hotkey's retained A60 duration.
    main.MainWindow._on_hotkey_save_clip(host)
    host._accepted_capture_session = _session(request_b)
    host._capture_config = _accepted_tracker(request_b)
    host._desired_capture_request = None
    main.MainWindow._on_hotkey_save_clip(host)

    assert calls == [60, 60, 90]


def test_accepted_request_duration_is_forwarded_by_normal_hotkey_action():
    import main

    request = replace(
        _request(1, 'A'),
        capture_config=replace(_request(1, 'A').capture_config,
                               normal_clip_seconds=47),
    )
    calls = []
    host = SimpleNamespace(
        _accepted_capture_session=_session(request),
        _capture_config=_accepted_tracker(request),
        _save_clip=lambda duration: calls.append(duration),
    )

    main.MainWindow._on_hotkey_save_clip(host)

    assert calls == [47]


def test_unready_or_failed_replay_camera_does_not_block_manual_native_start(
        monkeypatch, tmp_path):
    """Optional replay-camera health cannot gate a native manual command."""
    import main
    from core.capture_session import CameraSnapshot

    monkeypatch.setattr(main, 'set_theme_style', lambda *_args: None)
    for suffix, receipt in (
            ('failed', None),
            ('warming', {
                'generation': 31, 'device_index': 2, 'ready': False,
            })):
        request = _request(
            1, f'A-{suffix}',
            camera=CameraSnapshot(enabled=True, device_index=2),
        )
        host, session, _process, bridge, _tracker = _owned_host(
            main, request)
        host._accepted_camera_runtime_receipt = receipt
        host._manual_record_state = 'preparing'
        host._pending_manual_record_request = request
        host._pending_manual_record_path = tmp_path / f'{suffix}.mp4'
        host._manual_record_timer = _Timer()
        host.record_btn = _Button()
        host._manual_record_button_qss = lambda _active: ''
        host._manual_stop_pending = False
        host._manual_stop_deadline = 0.0
        host._manual_stop_engine = None
        host._manual_stop_bridge = None
        host._manual_stop_engine_generation = None
        host._manual_stop_path = None
        host._set_status = lambda *_args: None
        _bind(host, main, '_clear_manual_stop_request')

        main.MainWindow._submit_manual_recording_start(host)

        assert bridge.recording_calls == [str(tmp_path / f'{suffix}.mp4')]
        assert host._manual_record_state == 'starting'
        assert host._manual_record_request is request
        assert host._manual_record_session is session
        assert host._accepted_camera_runtime_receipt is receipt


def test_owned_manual_prepare_with_live_mux_queues_without_stop_or_provider_change(
        tmp_path):
    """An unrelated live mux owner blocks preparation and preserves A."""
    import main

    class _LiveMux:
        def is_alive(self):
            return True

    class _KeyboardCapture:
        def __init__(self):
            self.configure_calls = []

        def configure(self, config):
            self.configure_calls.append(config)

    request_a = _request(1, 'A')
    host, _session_a, _process, _bridge, _tracker = _owned_host(
        main, request_a)
    host._manual_record_state = 'preparing'
    host._pending_manual_record_request = request_a
    host._pending_manual_record_path = tmp_path / 'a.mp4'
    host._mux_threads = (_LiveMux(),)
    host._capture_request_intents[request_a.request_token] = 10
    host._capture_intent_revision = 9
    host._latest_capture_intent_id = 10
    provider = _KeyboardCapture()
    host._keyboard_overlay_capture = provider
    stop_calls = []
    host.stop_engine = lambda: stop_calls.append('stop')

    assert main.MainWindow._restart_capture_engine(
        host, allow_recording_prepare=True, _prepared_request=request_a,
    ) is False

    assert stop_calls == []
    assert provider.configure_calls == []
    assert host._manual_record_state == 'preparing'
    assert host._pending_manual_record_request is request_a
    assert host._desired_capture_request is request_a
    assert host._latest_capture_intent_state == 'queued'
    assert host._latest_capture_intent_consumed is False


def test_settings_keyboard_edit_waits_for_idle_coordinator_and_rebinds_B(
        qtbot, monkeypatch):
    """Settings persists B while Main owns A and configures B once at idle."""
    import main

    class _Settings:
        def __init__(self):
            self.values = {'third_party_keyboard': {}}
            self.saves = 0

        def get(self, key, default=None):
            return self.values.get(key, default)

        def set(self, key, value):
            self.values[key] = value

        def save_settings(self):
            self.saves += 1
            return True

    class _Timer:
        def __init__(self):
            self.starts = 0

        def start(self):
            self.starts += 1

    class _KeyboardCapture:
        def __init__(self):
            self.configure_calls = []

        def configure(self, config):
            self.configure_calls.append(config)

    settings = _Settings()
    keyboard_b = {
        'enabled': True, 'hwnd': 51, 'window_name': 'Keyboard B',
        'color': '#00ff00', 'intensity': 62,
        'rect': {'x': 0.2, 'y': 0.3, 'w': 0.5, 'h': 0.4},
    }
    monkeypatch.setattr(main._SettingsPage, '_setup_ui', lambda _self: None)
    monkeypatch.setattr(main._SettingsPage, '_apply_styles', lambda _self: None)
    monkeypatch.setattr(main, 'PresetsManager', lambda: object())
    page = main._SettingsPage(settings)
    qtbot.addWidget(page)
    page._capture_effects_timer = _Timer()
    provider = _KeyboardCapture()
    page._keyboard_capture = provider

    page._save_keyboard_config(keyboard_b)

    assert settings.saves == 1
    assert page._capture_effects_timer.starts == 1
    assert provider.configure_calls == []

    request_a = _request(
        1, 'A',
        effects={'capture_card_enabled': True,
                 'third_party_keyboard': {'enabled': False}},
    )
    request_b = _request(
        2, 'B',
        effects={'capture_card_enabled': True,
                 'third_party_keyboard': keyboard_b},
    )
    host, _session_a, _process, _bridge, _tracker = _owned_host(
        main, request_a, settings_manager=settings)
    host._keyboard_overlay_capture = provider
    host._mux_threads = (SimpleNamespace(is_alive=lambda: True),)
    host._capture_request_intents[request_b.request_token] = 17
    host._capture_intent_revision = 16
    host._latest_capture_intent_id = 17
    host._queue_latest_desired_request(
        request_b, reason='Keyboard edit waits for A to release')
    assert host._apply_latest_desired_request_if_idle() is False
    assert provider.configure_calls == []

    host._mux_threads = ()
    stop_calls = []
    host.stop_engine = lambda: stop_calls.append('stop')
    assert host._apply_latest_desired_request_if_idle() is False

    assert provider.configure_calls == [keyboard_b]
    assert host._accepted_capture_session.request is request_b
    assert host._desired_capture_request is None
    assert stop_calls == []


def test_on_clip_written_refuses_missing_session_before_post_route(
        monkeypatch, tmp_path):
    import main

    clip = tmp_path / 'a.mp4'
    clip.write_bytes(b'clip')
    failures = []
    routes = []
    host = SimpleNamespace(
        _clip_readiness=SimpleNamespace(
            finalization_failed=lambda *args, **kwargs: failures.append((args, kwargs)),
            can_access=lambda _path: False,
        ),
        _record_finalization_warning=lambda path, message: failures.append(
            ((path, message), {})),
        _finalize_clip=lambda *args, **kwargs: routes.append((args, kwargs)),
        _register_saved_clip_duration_report=lambda *_args: None,
        _set_status=lambda *_args: None,
        push_error=lambda *_args, **_kwargs: None,
    )
    op = SimpleNamespace(op_id=1, output_path=str(clip), duration_seconds=30,
                         requested_at=0.0, context={})
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    monkeypatch.setattr(main, 'is_completed_video_path', lambda _path: True)
    monkeypatch.setattr(main, 'emit_event', lambda *_args, **_kwargs: None)

    main.MainWindow._on_clip_written(host, op)

    assert routes == []
    assert any('accepted capture session' in str(item).lower()
               for item in failures)


def test_on_clip_written_post_route_uses_A_job_when_settings_are_B(
        monkeypatch, tmp_path):
    import main

    request_a = _request(
        1, 'A', mic_gain=0,
        camera=__import__('core.capture_session', fromlist=['CameraSnapshot'])
        .CameraSnapshot(enabled=True, device_index=2),
        effects=MappingProxyType({
            'capture_card_enabled': True, 'image_overlay_enabled': True,
            'image_overlay_path': 'A.png', 'image_overlay_opacity': 80,
            'image_overlay_fit': 'fit',
            'image_overlay_rect': {'x': 0.0, 'y': 0.0, 'w': 1.0, 'h': 1.0},
            'keyboard_overlay_enabled': True,
            'keyboard_overlay_rect': {'x': 0.3, 'y': 0.7, 'w': 0.4, 'h': 0.25},
             'third_party_keyboard': {
                 'enabled': True, 'hwnd': 0, 'window_name': 'Keyboard',
                 'color': '#00ff00', 'intensity': 58,
                 'rect': {'x': 0.3, 'y': 0.7, 'w': 0.4, 'h': 0.25},
             },
            'watermark_enabled': True,
        }),
    )
    session_a = _session(request_a)
    clip = tmp_path / 'a.mp4'
    clip.write_bytes(b'clip')
    worker_calls = []
    selector_calls = []
    host = SimpleNamespace(
        _clip_readiness=SimpleNamespace(
            finalization_failed=lambda *_args, **_kwargs: None,
            can_access=lambda _path: False,
        ),
        _register_saved_clip_duration_report=lambda *_args: None,
        _finalize_clip=None,
        upload_manager=SimpleNamespace(
            notify_clip_saved=lambda *_args, **_kwargs: SimpleNamespace(set=lambda: None)),
        settings_manager=_Settings(None, audio_capture_enabled=False,
                                   mic_volume=200),
        clip_metadata_manager=None,
        _set_status=lambda *_args: None,
        push_error=lambda *_args, **_kwargs: None,
        _ui_call=SimpleNamespace(emit=lambda callback: callback()),
    )
    op = SimpleNamespace(
        op_id=1, output_path=str(clip), duration_seconds=30, requested_at=0.0,
        context={
            'session_snapshot': session_a,
            'camera_runtime_receipt': None,
            'effects': request_a.to_dict()['effects'],
            'mic_end_time': 1.0,
            'crop_profile': None,
        },
    )
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    monkeypatch.setattr(main, 'is_completed_video_path', lambda _path: True)
    selector = main.select_post_route

    def select_wrapper(**kwargs):
        selector_calls.append(kwargs)
        return selector(**kwargs)

    monkeypatch.setattr(main, 'select_post_route', select_wrapper)
    host._allow_completed_clip_pipeline = (
        main.MainWindow._allow_completed_clip_pipeline)
    host._spawn_mux_thread = lambda target, args, **kwargs: (
        worker_calls.append((target, args, kwargs)) or None)
    host._finalize_clip = partial(main.MainWindow._finalize_clip, host)
    host._mux_mic_into_clip = partial(main.MainWindow._mux_mic_into_clip, host)
    host._combine_native_audio_worker = partial(
        main.MainWindow._combine_native_audio_worker, host)
    host._finalize_clip_worker = partial(
        main.MainWindow._finalize_clip_worker, host)
    monkeypatch.setattr(main, 'emit_event', lambda *_args, **_kwargs: None)

    main.MainWindow._on_clip_written(host, op)

    assert selector_calls and selector_calls[0]['audio_on'] is True
    assert selector_calls[0]['camera'] is True
    assert selector_calls[0]['keyboard'] is True
    assert selector_calls[0]['watermark'] is True
    assert len(worker_calls) == 1
    assert worker_calls[0][0] in {
        host._combine_native_audio_worker, host._finalize_clip_worker,
    }
    assert worker_calls[0][2]['capture_job']
    job = worker_calls[0][2]['capture_job']
    assert job['session_snapshot'] is session_a
    assert job['session_snapshot'].request.profile_name == 'A'
    assert job['session_snapshot'].request.camera.device_index == 2
    assert job['mic_gain_percent'] == 0
    assert job['effects']['image_overlay_path'] == 'A.png'
    assert job['effects']['third_party_keyboard']['enabled'] is True
    clip_stat = clip.stat()
    for key, attribute in (
            ('st_dev', 'st_dev'), ('st_ino', 'st_ino'),
            ('size', 'st_size'), ('mtime_ns', 'st_mtime_ns')):
        assert job['initial_identity'][key] == getattr(clip_stat, attribute)
        assert job['owned_identity'][key] == getattr(clip_stat, attribute)


def test_manual_preparation_compares_full_request_and_keeps_A_through_ack(
        monkeypatch, tmp_path):
    import main

    from core.capture_session import SourceSnapshot
    config = _request(1, 'A').capture_config
    source_a = SourceSnapshot(
        source_mode='window', exe_path=_WINDOW['exe_path'],
        live_identity=_ALPHA_IDENTITY, display_name='Alpha - Match',
        clip_group='Alpha', authority='manual')
    request_a = _request(1, 'A', source=source_a, config=config,
                         mic_gain=100, purpose='clips')
    request_recording = _request(
        2, 'A recording', source=source_a, config=config, mic_gain=100,
        purpose='recording')
    source_b = SourceSnapshot(
        source_mode='window', exe_path=_BETA_WINDOW['exe_path'],
        live_identity=_BETA_IDENTITY, display_name='Beta - Match',
        clip_group='Beta', authority='manual')
    config_b = replace(config, audio_enabled=False,
                       microphone_endpoint_id='mic-b')
    request_b = _request(3, 'B', source=source_b, config=config_b, mic_gain=20,
                         purpose='recording')
    host, session_a, process, bridge, _tracker = _owned_host(
        main, request_a)
    host._manual_record_state = 'preparing'
    host._pending_manual_record_request = request_b
    host._restart_pending = False
    host.restart_calls = []
    host._pending_manual_record_path = tmp_path / 'recording-b.mp4'
    host._manual_record_request = None
    host._manual_record_timer = _Timer()
    host.record_btn = _Button()
    host._manual_stop_pending = False
    host._manual_stop_deadline = 0.0
    host._manual_stop_engine = None
    host._manual_stop_bridge = None
    host._manual_stop_engine_generation = None
    host._manual_stop_path = None
    host._set_status = lambda *_args: None
    host._refresh_capture_summary = lambda: None
    _bind(host, main, '_camera_runtime_receipt_for_request')
    _patch_resolver(monkeypatch, main, dict(_WINDOW))
    _bind(host, main, '_continue_prepared_manual_recording',
          '_submit_manual_recording_start', '_clear_manual_stop_request',
          '_rebind_equivalent_capture_request',
          '_capture_native_inputs_equivalent')
    host._restart_capture_engine = lambda **kwargs: host.restart_calls.append(kwargs)

    main.MainWindow._continue_prepared_manual_recording(host)

    assert len(host.restart_calls) == 1
    assert host.restart_calls[0]['_prepared_request'] is request_b
    assert host.restart_calls[0]['allow_recording_prepare'] is True

    # Recording defaults are semantically equal to the accepted clip request;
    # request token and purpose metadata alone must not force a restart.
    host.restart_calls.clear()
    host._pending_manual_record_request = request_recording
    host._pending_manual_record_path = tmp_path / 'recording-a.mp4'
    host._accepted_capture_session = _session(request_a)
    host._manual_record_state = 'preparing'
    host._set_status = lambda *_args: None
    monkeypatch.setattr(main, 'set_theme_style', lambda *_args: None)
    _bind(host, main, '_restart_capture_engine')
    host.stop_engine = lambda: host.restart_calls.append('native_stop')
    host._set_capture_apply_state = lambda _applying: None

    # The first continuation rebinds the equivalent request through the real
    # coordinator; the next status continuation admits the native command.
    main.MainWindow._continue_prepared_manual_recording(host)
    main.MainWindow._continue_prepared_manual_recording(host)
    assert bridge.recording_calls == [str(tmp_path / 'recording-a.mp4')]
    assert host.restart_calls == []
    assert host._manual_record_request is request_recording
    rebound = host._accepted_capture_session
    assert rebound.request is request_recording
    assert rebound.process_generation == session_a.process_generation
    assert rebound.native_capture_generation == session_a.native_capture_generation
    assert rebound.accepted_config_token == session_a.accepted_config_token
    assert host.engine_process is process
    host._accepted_capture_session = _session(request_b)
    assert host._manual_record_request is request_recording


def test_manual_ack_and_finalize_boundary_keep_original_request_after_edits(
        monkeypatch, tmp_path):
    import main

    request_a = _request(1, 'A')
    request_b = _request(2, 'B', mic_gain=0)
    finalized = []
    host, _session_a, _process, bridge, _tracker = _owned_host(
        main, request_a)
    bridge.responses = [('started', '')]
    host._manual_record_state = 'starting'
    host._manual_record_request = request_a
    host._manual_record_requested_at = 0.0
    host._manual_record_started_at = 0.0
    host._manual_stop_pending = False
    host._manual_record_path = tmp_path / 'a.mp4'
    host.record_btn = _Button()
    host._manual_record_timer = _Timer()
    host._set_status = lambda *_args: None
    host._manual_stop_deadline = 0.0
    host._manual_stop_engine = None
    host._manual_stop_bridge = None
    host._manual_stop_engine_generation = None
    host._manual_stop_path = None
    _patch_resolver(monkeypatch, main, dict(_WINDOW))
    monkeypatch.setattr(main, 'set_theme_style', lambda *_args: None)
    _bind(host, main, '_pump_manual_recording_responses',
          '_mark_manual_recording_started', '_clear_manual_stop_request',
          '_format_recording_elapsed')

    main.MainWindow._pump_manual_recording_responses(host)
    assert host._manual_record_request is request_a
    host._manual_record_state = 'stopping'
    host.bridge.responses = [('stopped', '')]
    host._finalize_manual_recording_file = (
        lambda **_kwargs: finalized.append(host._manual_record_request))
    host._accepted_capture_session = _session(request_b)

    main.MainWindow._pump_manual_recording_responses(host)

    assert host._manual_record_state == 'stopping'
    assert finalized == [request_a]
    assert host._manual_record_request is request_a


def test_manual_observer_reentry_keeps_A_owner_until_publication_tail(
        monkeypatch, tmp_path):
    """A publication callback cannot release A or start a second manual job."""
    import main
    from core.save_state import SaveStateMachine

    request_a = _request(1, 'A')
    request_b = _request(
        2, 'B',
        effects={'capture_card_enabled': False},
    )
    host, session_a, _process, bridge, _tracker = _owned_host(
        main, request_a)
    recording = tmp_path / 'a.mp4'
    recording.write_bytes(b'manual-A')
    host._manual_record_state = 'finalizing'
    host._manual_record_path = recording
    host._manual_record_request = request_a
    host._manual_record_session = session_a
    host._manual_record_timer = _Timer()
    host._engine_profile = 'clips'
    host._set_status = lambda *_args: None
    host._save_state = SaveStateMachine()
    heartbeat_calls = []
    host._update_status = lambda: heartbeat_calls.append('heartbeat')
    host.capture_card = SimpleNamespace(
        show_recording_saved=lambda *_args, **_kwargs: None)
    host._capture_request_intents[request_b.request_token] = 9
    host._capture_intent_revision = 8
    host._latest_capture_intent_id = 9
    observed = []

    def observe(recording_path, identity, *, publish_ui):
        observed.append((
            recording_path, identity,
            host._manual_record_state,
            host._manual_record_session is session_a,
            host._manual_record_request is request_a,
            publish_ui,
        ))
        main.MainWindow._start_manual_recording(host)
        assert host._manual_record_state == 'finalizing'
        assert host._manual_record_path is recording
        assert host._manual_record_session is session_a
        assert host._manual_record_request is request_a
        assert host._accepted_capture_session is session_a
        assert bridge.recording_calls == []
        host._queue_latest_desired_request(
            request_b, reason='observer reentrant profile intent')
        assert host._apply_latest_desired_request_if_idle() is False

    host.library_storage_controller = SimpleNamespace(
        observe_completed_clip=observe)
    _bind(host, main, '_restore_clip_capture_profile', '_start_manual_recording')
    monkeypatch.setattr(main, 'set_theme_style', lambda *_args: None)
    timer_calls = []
    monkeypatch.setattr(
        main.QTimer, 'singleShot',
        lambda delay, callback: timer_calls.append((delay, callback)),
    )

    expected_identity = main.media_identity(recording)
    main.MainWindow._complete_manual_recording_file(
        host, recording, publish_ui=True, duration_seconds=4,
        expected_identity=expected_identity, session_snapshot=session_a,
    )

    assert observed and observed[0][2:] == ('finalizing', True, True, True)
    assert host._manual_record_state == 'idle'
    assert host._manual_record_path is None
    assert host._manual_record_session is None
    assert host._manual_record_request is None
    assert host._accepted_capture_session.request is request_b
    assert host._desired_capture_request is None
    assert bridge.recording_calls == []
    assert heartbeat_calls == []
    assert timer_calls and timer_calls[-1][0] == 2500


@pytest.mark.parametrize('broken_owner', ('native-generation', 'process-ended'))
def test_identity_valid_manual_A_publishes_without_live_native_owner_proof(
        broken_owner, monkeypatch, tmp_path):
    """Completed A publication is file/session-bound after native teardown."""
    import main

    request_a = _request(1, 'A')
    host, session_a, process, bridge, _tracker = _owned_host(main, request_a)
    recording = tmp_path / f'{broken_owner}.mp4'
    base_bytes = b'identity-valid manual A bytes'
    recording.write_bytes(base_bytes)
    host._manual_record_state = 'finalizing'
    host._manual_record_path = recording
    host._manual_record_request = request_a
    host._manual_record_session = session_a
    host._manual_record_timer = _Timer()
    host._engine_profile = 'clips'
    host._set_status = lambda *_args: None
    host._update_status = lambda: None
    host.capture_card = SimpleNamespace(
        show_recording_saved=lambda *_args, **_kwargs: None)
    if broken_owner == 'native-generation':
        bridge.capture_generation = session_a.native_capture_generation + 1
    else:
        process.returncode = 1

    observed = []

    def observe(recording_path, identity, *, publish_ui):
        observed.append({
            'path': recording_path,
            'identity': identity,
            'publish_ui': publish_ui,
            'session': host._manual_record_session,
            'request': host._manual_record_request,
            'state': host._manual_record_state,
        })

    host.library_storage_controller = SimpleNamespace(
        observe_completed_clip=observe)
    _bind(host, main, '_restore_clip_capture_profile')
    timer_calls = []
    monkeypatch.setattr(
        main.QTimer, 'singleShot',
        lambda delay, callback: timer_calls.append((delay, callback)),
    )

    expected_identity = main.media_identity(recording)
    main.MainWindow._complete_manual_recording_file(
        host, recording, publish_ui=True, duration_seconds=7,
        expected_identity=expected_identity, session_snapshot=session_a,
    )

    assert len(observed) == 1
    assert observed[0]['path'] == recording
    assert observed[0]['identity'] == expected_identity
    assert observed[0]['publish_ui'] is True
    assert observed[0]['session'] is session_a
    assert observed[0]['request'] is request_a
    assert observed[0]['state'] == 'finalizing'
    assert host._accepted_capture_session is session_a
    assert recording.read_bytes() == base_bytes
    assert host._manual_record_path is None
    assert host._manual_record_session is None
    assert timer_calls and timer_calls[-1][0] == 2500


def test_prepared_source_liveness_guard_rejects_replaced_window(
        monkeypatch):
    import main

    from core.capture_session import SourceSnapshot

    request = _request(
        1, 'A', source=SourceSnapshot(
            source_mode='window', exe_path=_WINDOW['exe_path'],
            live_identity=_ALPHA_IDENTITY, display_name='Alpha - Match',
            clip_group='Alpha'))
    host = SimpleNamespace(_shutdown_requested=False)
    _bind(host, main, '_prepared_source_is_live')
    _bind(host, main, '_admission_positive_int')
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    monkeypatch.setattr(main, 'get_window_info',
                        lambda _hwnd, own_pid=None: dict(_BETA_WINDOW))

    assert host._prepared_source_is_live(request) is False


def test_healthy_recovery_rebinds_original_A_with_new_native_generation(
        monkeypatch):
    import main

    from core.capture_session import SourceSnapshot
    request_a = _request(
        1, 'A', source=SourceSnapshot(
            source_mode='window', exe_path=_WINDOW['exe_path'],
            live_identity=_ALPHA_IDENTITY, display_name='Alpha - Match',
            clip_group='Alpha'))
    host, session_a, _process, bridge, tracker = _owned_host(main, request_a)
    host._refresh_capture_summary = lambda: None
    _bind(host, main, '_camera_runtime_receipt_for_request')
    host._rebind_recovered_session = partial(
        main.MainWindow._rebind_recovered_session, host)
    bridge.capture_generation = 12
    status = bridge.get_status()
    _patch_resolver(monkeypatch, main, dict(_WINDOW))

    assert host._rebind_recovered_session(status) is True
    recovered = host._accepted_capture_session
    assert recovered.request is request_a
    assert recovered.native_capture_generation == 12
    assert recovered.process_generation == session_a.process_generation
    assert recovered.accepted_config_token == 3
    assert tracker.accepted_token == 3

    # A queued B and a replaced live source cannot be adopted by the recovery
    # proof; the newly observed native generation still belongs to A only.
    host._desired_capture_request = _request(2, 'B')
    monkeypatch.setattr(main, 'get_window_info',
                        lambda _hwnd, own_pid=None: dict(_BETA_WINDOW))
    assert host._rebind_recovered_session(status) is False
    assert host._accepted_capture_session.request is request_a
    assert host._desired_capture_request.profile_name == 'B'


def test_profile_status_explicitly_distinguishes_accepted_none_from_global_defaults():
    import main

    received = []
    host = SimpleNamespace(
        _accepted_capture_session=None,
        _desired_capture_request=None,
        _settings_page_widget=SimpleNamespace(
            set_profile_capture_status=lambda *args: received.append(args)),
    )
    _bind(host, main, '_refresh_capture_summary')
    _bind(host, main, '_accepted_profile_status',
          '_publish_capture_profile_status')
    host._publish_capture_profile_status(pending=False, message='')
    assert received[-1] == (None, None, '')

    host._desired_capture_request = replace(
        _request(1, 'A'), profile_id=None, profile_revision=0,
        profile_name=None, profile_document_revision=0)
    host._publish_capture_profile_status(pending=True, message='global')
    assert received[-1][0] is None
    assert received[-1][1] == {'name': 'Global defaults', 'exe_path': ''}


def test_start_engine_source_documents_zero_microphone_gain_abi():
    """Keep this source-level ABI note separate from launch-boundary tests."""
    import inspect
    import main

    source = inspect.getsource(main.MainWindow.start_engine)
    assert 'microphone_gain_arg = str(request.mic_gain_percent)' in source
    assert 'microphone_id_arg = launch_config.microphone_endpoint_id' in source
    assert 'microphone_id_arg, microphone_gain_arg' in source
