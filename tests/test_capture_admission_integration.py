"""Main admission boundary tests.

These tests exercise the Main adapter with a plain Settings.get provider as
well as the real dedicated settings API.  They intentionally do not replace
the admission guard with a permissive test double.
"""

from dataclasses import replace
from types import SimpleNamespace
import threading

import pytest


def _request(main, *, exe=r"C:\Games\allowed.exe", hwnd=4242,
             pid=321, process_created=9876, mode='window', camera=None):
    from core.capture_session import SourceSnapshot, ResolvedCaptureRequest
    from core.capture_session import CameraSnapshot
    from core.capture_settings import CaptureConfig
    identity = {
        'hwnd': hwnd, 'pid': pid, 'exe_path': exe,
        'class_name': 'GameWindow', 'process_created': process_created,
    }
    source = SourceSnapshot(
        source_mode=mode,
        exe_path=exe if mode == 'window' else None,
        live_identity=identity if mode == 'window' else None,
        display_name='Game' if mode == 'window' else 'Desktop',
    )
    config = CaptureConfig(
        fps=60, buffer_seconds=30, width=1920, height=1080,
        bitrate_kbps=25_000, codec='h264', preset=4,
        monitor=r'\\?\display#primary', scaling='stretch',
        audio_enabled=False,
    )
    return ResolvedCaptureRequest(
        request_token=1, purpose='clips', source=source,
        capture_config=config,
        camera=CameraSnapshot() if camera is None else camera)


class _PlainSettings:
    def __init__(self, raw=...):
        self.settings = {}
        if raw is not ...:
            self.settings['capture_admission_policy'] = raw

    def get(self, key, default=None):
        return self.settings.get(key, default)


class _DictOnlySettings:
    def __init__(self, raw=...):
        self.settings = {}
        if raw is not ...:
            self.settings['capture_admission_policy'] = raw


class _ThrowingSettings:
    def get(self, _key, _default=None):
        raise OSError('settings read failed')


def _host(settings):
    import main

    host = SimpleNamespace(
        settings_manager=settings,
        _admission_policy=None,
        _admission_policy_error=None,
        _admission_policy_raw_owner=object(),
        _admission_epoch=0,
        _accepted_capture_session=None,
        _latest_capture_intent_state='none',
        _latest_capture_intent_consumed=True,
        _pending_capture_reason='',
        _set_status=lambda *_args: None,
        push_error=lambda *_args, **_kwargs: None,
    )
    for name, descriptor in main.MainWindow.__dict__.items():
        if not (name.startswith('_admission_')
                or name in {'_reload_admission_policy',
                            '_capture_owner_snapshot',
                            '_capture_owner_snapshot_is_current',
                            '_owns_capture_apply'}):
            continue
        if isinstance(descriptor, staticmethod):
            setattr(host, name, descriptor.__func__)
        elif callable(descriptor):
            setattr(host, name, descriptor.__get__(host, main.MainWindow))
    return host


def test_plain_get_missing_policy_keeps_legacy_default_off(monkeypatch):
    import main

    monkeypatch.setattr(main.sys, 'platform', 'win32')
    host = _host(_PlainSettings())
    request = _request(main, mode='desktop')
    decision = host._admission_decision_for_request(request)
    assert decision.allowed is True
    assert decision.code == 'policy-disabled'


def test_plain_get_enabled_policy_requires_exact_live_executable(monkeypatch):
    import main
    exe = r'C:\Games\allowed.exe'
    candidate = SimpleNamespace(
        hwnd=4242, pid=321, exe_path=exe, class_name='GameWindow',
        process_created=9876)
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    monkeypatch.setattr(main, 'get_window_info', lambda *_a, **_k: candidate)
    raw = {
        'version': 1, 'revision': 1, 'enabled': True,
        'executable_paths': [exe],
    }
    host = _host(_PlainSettings(raw))
    request = _request(main, exe=exe)
    assert host._admission_decision_for_request(request).allowed is True

    mismatch = _request(main, exe=r'C:\Games\other.exe')
    denied = host._admission_decision_for_request(mismatch)
    assert denied.allowed is False
    assert denied.code in {'source-not-approved', 'invalid-executable',
                           'identity-incomplete'}


def test_settings_dict_fallback_uses_the_actual_raw_policy(monkeypatch):
    import main

    monkeypatch.setattr(main.sys, 'platform', 'win32')
    raw = {
        'version': 1, 'revision': 1, 'enabled': True,
        'executable_paths': [r'C:\Games\allowed.exe'],
    }
    host = _host(_DictOnlySettings(raw))
    request = _request(main, exe=r'C:\Games\allowed.exe')
    candidate = SimpleNamespace(
        hwnd=4242, pid=321, exe_path=r'C:\Games\allowed.exe',
        class_name='GameWindow', process_created=9876)
    monkeypatch.setattr(main, 'get_window_info', lambda *_a, **_k: candidate)

    decision = host._admission_decision_for_request(request)

    assert decision.allowed is True
    assert decision.code == 'admitted'


@pytest.mark.parametrize('raw', [
    None,
    {'version': 2, 'revision': 0, 'enabled': True, 'executable_paths': []},
    {'version': 1, 'revision': 0, 'enabled': True,
     'executable_paths': [r'C:\Games\allowed.exe'], 'future': True},
])
def test_malformed_raw_policy_is_unavailable_and_fails_closed(raw):
    import main

    host = _host(_PlainSettings(raw))
    request = _request(main, mode='desktop')
    assert host._admission_decision_for_request(request) is None
    # The unavailable policy must stop the side-effect boundary before a
    # provider/window lookup can happen.  This is the same guard used by
    # launch, resume, and camera/provider mutation paths.
    looked_up = []
    host._admission_live_identity = lambda source: looked_up.append(source) or (True, {})
    assert host._admission_guard_request(request) is False
    assert looked_up == []


def test_unsupported_settings_provider_does_not_become_default_off():
    import main

    host = SimpleNamespace(settings_manager=object(),
                           _admission_policy_raw_owner=object())
    host._admission_raw_owner = main.MainWindow._admission_raw_owner.__get__(host)
    host._reload_admission_policy = main.MainWindow._reload_admission_policy.__get__(host)
    host._admission_policy = None
    host._admission_policy_error = None
    assert host._reload_admission_policy(authoritative=True) is None


def test_throwing_generic_settings_provider_is_unavailable(monkeypatch):
    import main

    monkeypatch.setattr(main.sys, 'platform', 'win32')
    host = _host(_ThrowingSettings())
    request = _request(main, mode='desktop')
    looked_up = []
    host._admission_live_identity = (
        lambda source: looked_up.append(source) or (True, {}))

    assert host._admission_decision_for_request(request) is None
    assert host._admission_guard_request(request) is False
    assert looked_up == []


def test_start_engine_without_supported_settings_authority_denies_before_popen(
        monkeypatch):
    import main

    called = []
    monkeypatch.setattr(main.subprocess, 'Popen',
                        lambda *_args, **_kwargs: called.append(True))
    denied = []
    host = _host(object())
    host._file_operations_ready = True
    host._admission_deny = (
        lambda decision, **_kwargs: denied.append(decision) or False)
    host.start_engine = main.MainWindow.start_engine.__get__(host)

    assert host.start_engine() is False
    assert denied == [None]
    assert called == []


def test_current_session_guard_stops_resume_while_transition_is_active():
    import main

    host = _host(_PlainSettings())
    host._admission_transition_latch = object()
    host._admission_transition_active = (
        main.MainWindow._admission_transition_active.__get__(host))
    host._accepted_capture_session = SimpleNamespace(
        request=object())

    def unexpected_guard(_request):
        raise AssertionError('guard must not run')

    host._admission_guard_request = unexpected_guard
    host._admission_guard_current_session = (
        main.MainWindow._admission_guard_current_session.__get__(host))

    assert host._admission_guard_current_session() is False


def test_pending_launch_reauthorizes_after_an_admission_epoch_change():
    import main

    request = object()
    host = _host(_PlainSettings())
    host._pending_launch_request = request
    host._pending_launch_admission_epoch = 3
    host._admission_epoch = 4
    calls = []

    def decision(candidate, *, authoritative=True):
        calls.append((candidate, authoritative))
        return SimpleNamespace(allowed=True)

    host._admission_decision_for_request = decision
    host._admission_reauthorize_pending_launch = (
        main.MainWindow._admission_reauthorize_pending_launch.__get__(host))

    assert host._admission_reauthorize_pending_launch() is True
    assert host._pending_launch_admission_epoch == 4
    assert calls == [(request, True)]


def test_live_lookup_policy_edit_never_blesses_old_decision(monkeypatch):
    import main

    monkeypatch.setattr(main.sys, 'platform', 'win32')
    exe = r'C:\Games\allowed.exe'
    candidate = SimpleNamespace(
        hwnd=4242, pid=321, exe_path=exe, class_name='GameWindow',
        process_created=9876)
    raw = {
        'version': 1, 'revision': 1, 'enabled': True,
        'executable_paths': [exe],
    }
    host = _host(_PlainSettings(raw))
    host._reload_admission_policy(authoritative=True)
    request = _request(main, exe=exe)
    initial_epoch = host._admission_epoch
    calls = []

    def lookup(*_args, **_kwargs):
        calls.append(True)
        if len(calls) == 1:
            raw['revision'] = 2
            raw['executable_paths'] = [r'C:\Games\other.exe']
        return candidate

    monkeypatch.setattr(main, 'get_window_info', lookup)
    decision = host._admission_decision_for_request(request)

    assert decision.allowed is False
    assert decision.policy_revision == 2
    assert host._admission_epoch > initial_epoch
    assert len(calls) >= 2


def test_live_lookup_same_revision_edit_retries_once_with_new_policy(monkeypatch):
    import main

    monkeypatch.setattr(main.sys, 'platform', 'win32')
    exe = r'C:\Games\allowed.exe'
    candidate = SimpleNamespace(
        hwnd=4242, pid=321, exe_path=exe, class_name='GameWindow',
        process_created=9876)
    raw = {
        'version': 1, 'revision': 1, 'enabled': True,
        'executable_paths': [exe],
    }
    host = _host(_PlainSettings(raw))
    host._reload_admission_policy(authoritative=True)
    request = _request(main, exe=exe)
    initial_epoch = host._admission_epoch
    calls = []

    def lookup(*_args, **_kwargs):
        calls.append(True)
        if len(calls) == 1:
            raw['executable_paths'].append(r'C:\Games\other.exe')
        return candidate

    monkeypatch.setattr(main, 'get_window_info', lookup)
    decision = host._admission_decision_for_request(request)

    assert decision.allowed is True
    assert decision.policy_revision == 1
    assert host._admission_epoch > initial_epoch
    # The first proof is invalidated; exactly one fresh lookup may retry it.
    assert len(calls) == 2


def test_live_lookup_that_never_stabilizes_is_denied_after_one_retry(monkeypatch):
    import main

    monkeypatch.setattr(main.sys, 'platform', 'win32')
    exe = r'C:\Games\allowed.exe'
    candidate = SimpleNamespace(
        hwnd=4242, pid=321, exe_path=exe, class_name='GameWindow',
        process_created=9876)
    raw = {
        'version': 1, 'revision': 1, 'enabled': True,
        'executable_paths': [exe],
    }
    host = _host(_PlainSettings(raw))
    request = _request(main, exe=exe)

    def lookup(*_args, **_kwargs):
        raw['revision'] += 1
        return candidate

    monkeypatch.setattr(main, 'get_window_info', lookup)
    assert host._admission_decision_for_request(request) is None
    assert host._admission_epoch >= 2


@pytest.mark.parametrize('raw_hwnd', [True, '4242', 4242.0])
def test_enabled_capture_window_rejects_non_exact_hwnd_before_lookup(
        monkeypatch, raw_hwnd):
    import main

    monkeypatch.setattr(main.sys, 'platform', 'win32')
    raw = {
        'version': 1, 'revision': 1, 'enabled': True,
        'executable_paths': [r'C:\Games\allowed.exe'],
    }
    host = _host(_PlainSettings(raw))
    looked_up = []
    monkeypatch.setattr(
        main, 'get_window_info',
        lambda *args, **kwargs: looked_up.append((args, kwargs)))
    decision = host._admission_decision_for_settings({
        'hwnd': raw_hwnd, 'pid': 321, 'exe_path': r'C:\Games\allowed.exe',
        'class_name': 'GameWindow', 'process_created': 9876,
    })

    assert decision.allowed is False
    assert decision.code == 'identity-incomplete'
    assert looked_up == []


def test_disabled_capture_window_keeps_legacy_non_exact_hwnd_compatibility():
    host = _host(_PlainSettings())
    decision = host._admission_decision_for_settings({
        'hwnd': True, 'pid': 321, 'exe_path': '',
        'class_name': 'GameWindow', 'process_created': 9876,
    })

    assert decision.allowed is True
    assert decision.code == 'policy-disabled'


def test_disabled_capture_window_does_not_refresh_identity(monkeypatch):
    import main

    monkeypatch.setattr(main.sys, 'platform', 'win32')
    host = _host(_PlainSettings())
    looked_up = []
    monkeypatch.setattr(
        main, 'get_window_info',
        lambda *args, **kwargs: looked_up.append((args, kwargs)))
    decision = host._admission_decision_for_settings({
        'hwnd': 4242, 'pid': 321, 'exe_path': r'C:\Games\allowed.exe',
        'class_name': 'GameWindow', 'process_created': 9876,
    })

    assert decision.allowed is True
    assert looked_up == []


def test_policy_enabled_during_disabled_settings_read_is_rechecked(monkeypatch):
    import main

    monkeypatch.setattr(main.sys, 'platform', 'win32')
    exe = r'C:\Games\allowed.exe'
    identity = {
        'hwnd': 4242, 'pid': 321, 'exe_path': exe,
        'class_name': 'GameWindow', 'process_created': 9876,
    }

    class FlipSettings(_PlainSettings):
        def __init__(self):
            super().__init__()
            self.settings.update({
                'capture_mode': 'window',
                'target_window_identity': identity,
            })
            self.flipped = False

        def get(self, key, default=None):
            value = super().get(key, default)
            if key == 'capture_mode' and not self.flipped:
                self.flipped = True
                self.settings['capture_admission_policy'] = {
                    'version': 1, 'revision': 2, 'enabled': True,
                    'executable_paths': [r'C:\Games\other.exe'],
                }
            return value

    candidate = SimpleNamespace(**identity)
    looked_up = []
    monkeypatch.setattr(
        main, 'get_window_info',
        lambda *args, **kwargs: looked_up.append((args, kwargs)) or candidate)
    host = _host(FlipSettings())

    decision = host._admission_decision_for_settings()

    assert decision.allowed is False
    assert decision.policy_revision == 2
    assert host._admission_epoch >= 2
    assert looked_up


def test_dedicated_policy_reader_runtime_error_is_unavailable():
    import main

    class RuntimeReader(_PlainSettings):
        def read_capture_admission_policy(self):
            raise RuntimeError('reader unavailable')

    host = _host(RuntimeReader())
    request = _request(main, mode='desktop')

    assert host._admission_decision_for_request(request) is None
    assert 'capture_admission_policy' not in host.settings_manager.settings


@pytest.mark.parametrize('field,value', [
    ('pid', 999), ('process_created', 111), ('class_name', 'OtherWindow'),
])
def test_enabled_capture_window_rejects_stale_original_identity(
        monkeypatch, field, value):
    import main

    monkeypatch.setattr(main.sys, 'platform', 'win32')
    exe = r'C:\Games\allowed.exe'
    raw = {
        'version': 1, 'revision': 1, 'enabled': True,
        'executable_paths': [exe],
    }
    supplied = {
        'hwnd': 4242, 'pid': 321, 'exe_path': exe,
        'class_name': 'GameWindow', 'process_created': 9876,
    }
    live = dict(supplied)
    live[field] = value
    host = _host(_PlainSettings(raw))
    looked_up = []
    monkeypatch.setattr(
        main, 'get_window_info',
        lambda *args, **kwargs: looked_up.append((args, kwargs))
        or SimpleNamespace(**live))

    decision = host._admission_decision_for_settings(supplied)

    assert decision.allowed is False
    assert looked_up


def test_restart_owner_loss_preserves_successor_apply_state(monkeypatch):
    import main

    class Timer:
        def __init__(self, starts=0):
            self.start_calls = starts
            self.stop_calls = 0

        def start(self):
            self.start_calls += 1

        def stop(self):
            self.stop_calls += 1

    original = _request(main, mode='desktop')
    request = replace(
        original,
        capture_config=replace(original.capture_config, bitrate_kbps=30_000),
    )
    host = _host(_PlainSettings())
    process_a = SimpleNamespace(poll=lambda: None)
    bridge_a = SimpleNamespace()
    tracker_a = SimpleNamespace(
        active=original.capture_config, accepted_token=7,
        applying=None, applying_token=None, transition_revision=1)
    session_a = SimpleNamespace(
        request=original, process_generation=1,
        native_capture_generation=1, accepted_config_token=7)
    host._accepted_capture_session = session_a
    host._capture_config = tracker_a
    host.engine_process = process_a
    host.bridge = bridge_a
    host._engine_bridge_owner = bridge_a
    host._engine_gen = 1
    host._engine_process_generation = 1
    host._file_operations_ready = True
    host._engine_profile = 'clips'
    host._prepare_next_capture_request = lambda *_args: request
    host._capture_resources_busy = lambda **_kwargs: False
    host._manual_record_state = 'idle'
    host._save_state = SimpleNamespace(is_busy=lambda: False)
    host._set_status = lambda *_args, **_kwargs: None
    host._capture_pending_text = (
        main.MainWindow._capture_pending_text.__get__(host))
    host._set_capture_apply_state = (
        main.MainWindow._set_capture_apply_state.__get__(host))
    host._capture_apply_timer = Timer()
    host._restart_pending = False
    host._capture_settings_applying = False
    host._engine_restart_requested = False
    stop_calls = []
    provider_calls = []
    host.stop_engine = lambda: stop_calls.append(True)
    host._request_camera_apply = lambda *_args, **_kwargs: provider_calls.append(True)
    real_proof = host._admission_stable_proof
    b_timer = Timer(starts=4)
    process_b = SimpleNamespace(poll=lambda: None)
    bridge_b = SimpleNamespace()
    tracker_b = SimpleNamespace(
        active=request.capture_config, accepted_token=8,
        applying=request.capture_config, applying_token=9,
        transition_revision=4)
    session_b = SimpleNamespace(
        request=request, process_generation=2,
        native_capture_generation=2, accepted_config_token=8)

    def reentrant_proof(candidate):
        proof = real_proof(candidate)
        host._accepted_capture_session = session_b
        host._capture_config = tracker_b
        host.engine_process = process_b
        host.bridge = bridge_b
        host._engine_bridge_owner = bridge_b
        host._engine_gen = 2
        host._engine_process_generation = 2
        host._capture_apply_timer = b_timer
        host._restart_pending = True
        host._capture_settings_applying = True
        host._engine_restart_requested = True
        return proof

    host._admission_stable_proof = reentrant_proof

    assert main.MainWindow._restart_capture_engine(host) is False
    assert stop_calls == []
    assert provider_calls == []
    assert host._accepted_capture_session is session_b
    assert host._capture_config is tracker_b
    assert host.engine_process is process_b
    assert host.bridge is bridge_b
    assert host._restart_pending is True
    assert host._capture_settings_applying is True
    assert host._engine_restart_requested is True
    assert host._capture_apply_timer is b_timer
    assert b_timer.start_calls == 4
    assert b_timer.stop_calls == 0


def test_revoke_owner_loss_preserves_successor_without_binding(monkeypatch):
    import main

    request = _request(main, mode='desktop')
    host = _host(_PlainSettings())
    bridge_a = SimpleNamespace()
    session_a = SimpleNamespace(
        request=request, process_generation=1,
        native_capture_generation=1, accepted_config_token=7)
    host._accepted_capture_session = session_a
    host._capture_config = SimpleNamespace(
        active=request.capture_config, accepted_token=7,
        applying=None, applying_token=None, transition_revision=1)
    host.engine_process = SimpleNamespace(poll=lambda: None)
    host.bridge = bridge_a
    host._engine_bridge_owner = bridge_a
    host._engine_gen = 1
    host._engine_process_generation = 1
    host._admission_transition_latch = None
    b_session = SimpleNamespace(
        request=request, process_generation=2,
        native_capture_generation=2, accepted_config_token=8)
    b_process = SimpleNamespace()
    b_bridge = SimpleNamespace()
    b_tracker = SimpleNamespace(
        active=request.capture_config, accepted_token=8,
        applying=request.capture_config, applying_token=9,
        transition_revision=4)
    host._restart_pending = True
    host._capture_settings_applying = True
    host._engine_restart_requested = True
    host._capture_apply_timer = SimpleNamespace(stop_calls=0)
    bind_calls = []
    monkeypatch.setattr(
        main, 'bind_capture_session',
        lambda *args, **kwargs: bind_calls.append((args, kwargs)))

    def replace_owner(_request, _policy):
        host._accepted_capture_session = b_session
        host._capture_config = b_tracker
        host.engine_process = b_process
        host.bridge = b_bridge
        host._engine_bridge_owner = b_bridge
        host._engine_gen = 2
        host._engine_process_generation = 2
        return SimpleNamespace(allowed=True, policy_revision=2)

    host._admission_decision_for_policy = replace_owner
    host._admission_revoke_if_needed(
        SimpleNamespace(revision=1), SimpleNamespace(revision=2))

    assert bind_calls == []
    assert host._accepted_capture_session is b_session
    assert host._capture_config is b_tracker
    assert host.engine_process is b_process
    assert host.bridge is b_bridge
    assert host._restart_pending is True
    assert host._capture_settings_applying is True
    assert host._engine_restart_requested is True


def test_start_engine_reentrant_latch_during_log_setup_has_no_native_effect(
        monkeypatch):
    import main

    host = _host(_PlainSettings())
    host._admission_transition_active = (
        main.MainWindow._admission_transition_active.__get__(host))
    host.start_engine = main.MainWindow.start_engine.__get__(host)
    host._file_operations_ready = True
    host._engine_gen = 0
    host._diagnostic_engine_start_count = 0
    host._diagnostics = None
    host._close_engine_startup_output = lambda: None
    host._admission_transition_latch = None
    tracker = SimpleNamespace(begin_apply=lambda: (_ for _ in ()).throw(
        AssertionError('tracker must not be touched')))
    host._capture_config = tracker
    popen_calls = []
    monkeypatch.setattr(main.subprocess, 'Popen',
                        lambda *args, **kwargs: popen_calls.append(args))

    class ReentrantLog:
        def __init__(self, *_args, **_kwargs):
            host._admission_transition_latch = object()

    monkeypatch.setattr(main, 'EngineLogCapture', ReentrantLog)
    request = _request(main, mode='desktop')

    assert host.start_engine(_prepared_request=request) is False
    assert popen_calls == []
    assert host._engine_gen == 0


def test_start_engine_final_source_proof_latch_has_no_native_effect(monkeypatch):
    import main
    from pathlib import Path

    request = _request(main, mode='desktop')
    host = _host(_PlainSettings())
    host._admission_transition_active = (
        main.MainWindow._admission_transition_active.__get__(host))
    host.start_engine = main.MainWindow.start_engine.__get__(host)
    host._file_operations_ready = True
    host._engine_gen = 0
    host._diagnostic_engine_start_count = 0
    host._diagnostics = None
    host._close_engine_startup_output = lambda: None
    host._admission_transition_latch = None
    host._resolve_capture_request = lambda *_a, **_k: request
    host._capture_request_intent_id = lambda _request: 'test-intent'
    host._diagnostic_capture_config = lambda _config: {}
    host._capture_health = SimpleNamespace(reset=lambda **_k: None)

    class Tracker:
        transition_revision = 0
        applying = None
        applying_token = None
        active = None
        accepted_token = None

        def request(self, _config):
            return None

        def begin_apply(self):
            self.applying = request.capture_config
            self.applying_token = 1
            self.transition_revision += 1
            return self.applying_token

    host._capture_config = Tracker()
    host.engine_path = Path(__file__)
    host._set_capture_apply_state = lambda *_a, **_k: None
    host._set_status = lambda *_a, **_k: None
    host._restart_pending = False
    source_calls = []

    def final_source_hook(_request):
        source_calls.append(True)
        host._admission_transition_latch = object()
        return True

    host._prepared_source_is_live = final_source_hook
    popen_calls = []
    monkeypatch.setattr(main.subprocess, 'Popen',
                        lambda *args, **kwargs: popen_calls.append(args))
    monkeypatch.setattr(main, 'emit_event', lambda *_a, **_k: None)

    class LogCapture:
        def __init__(self, *_args, **_kwargs):
            pass

        def attach(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(main, 'EngineLogCapture', LogCapture)

    assert host.start_engine(_prepared_request=request) is False
    assert source_calls == [True]
    assert popen_calls == []
    assert host._engine_gen == 1


def test_highlight_reentrant_latch_is_checked_before_native_command():
    import main

    request = _request(main, mode='desktop')
    host = _host(_PlainSettings())
    host._admission_transition_active = (
        main.MainWindow._admission_transition_active.__get__(host))
    host._admission_pure_proof_current = (
        main.MainWindow._admission_pure_proof_current.__get__(host))
    host._accepted_session_owner_pure_is_current = (
        main.MainWindow._accepted_session_owner_pure_is_current.__get__(host))
    host._capture_config = SimpleNamespace(
        accepted_token=7, active=request.capture_config)
    host.engine_process = SimpleNamespace(poll=lambda: None)
    host._engine_gen = 1
    host._engine_process_generation = 1
    host._accepted_capture_session = SimpleNamespace(
        request=request, accepted_config_token=7,
        process_generation=1, native_capture_generation=1)
    host._accepted_session_owner_is_current = lambda *_args, **_kwargs: True

    class Bridge:
        def __init__(self):
            self.mark_calls = 0

        def mark_highlight(self, _marker):
            self.mark_calls += 1
            return True

    bridge = Bridge()
    host.bridge = bridge

    real_proof = main.MainWindow._admission_stable_proof.__get__(host)

    def reentrant_proof(candidate):
        proof = real_proof(candidate)
        host._admission_transition_latch = object()
        return proof

    host._admission_stable_proof = reentrant_proof
    host._on_hotkey_mark_highlight = (
        main.MainWindow._on_hotkey_mark_highlight.__get__(host))

    host._on_hotkey_mark_highlight()

    assert bridge.mark_calls == 0


def test_manual_start_reentrant_response_cannot_reach_native_command(tmp_path):
    import main

    request = _request(main, mode='desktop')
    session = SimpleNamespace(
        request=request, accepted_config_token=7, process_generation=1,
        native_capture_generation=1)
    host = _host(_PlainSettings())
    host._admission_transition_active = (
        main.MainWindow._admission_transition_active.__get__(host))
    host._accepted_capture_session = session
    host._capture_config = SimpleNamespace(
        accepted_token=7, active=request.capture_config)
    host.engine_process = SimpleNamespace(poll=lambda: None)
    host.bridge = SimpleNamespace()
    host._engine_gen = 1
    host._engine_process_generation = 1
    host._accepted_session_owner_is_current = lambda *_a, **_k: True
    host._capture_resources_busy = lambda **_k: False
    host._pending_manual_record_path = tmp_path / 'recording.mp4'
    host._pending_manual_record_request = request
    host._manual_record_retry_pending = False
    host._manual_record_state = 'idle'
    host._clear_manual_stop_request = lambda: None
    host._fail_manual_recording = lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError('manual start should be rejected at the admission gate'))
    native_calls = []

    def consume_response():
        host._admission_transition_latch = object()

    host.bridge.consume_manual_recording_response = consume_response
    host.bridge.start_manual_recording = (
        lambda *_a, **_k: native_calls.append(True) or True)

    main.MainWindow._submit_manual_recording_start(host)

    assert native_calls == []
    assert host._manual_record_state == 'idle'


def test_save_reentrant_response_cannot_reach_native_command(tmp_path, monkeypatch):
    import main

    request = _request(main, mode='desktop')
    session = SimpleNamespace(
        request=request, accepted_config_token=7, process_generation=1,
        native_capture_generation=1)
    host = _host(_PlainSettings())
    host._admission_transition_active = (
        main.MainWindow._admission_transition_active.__get__(host))
    host._accepted_capture_session = session
    host._accepted_session_owner_is_current = lambda *_a, **_k: True
    host._capture_health_snapshot = object()
    host._manual_record_state = 'idle'
    host._last_save_request = 0.0
    host._save_diagnostic_started_at = 0.0
    host._clip_diagnostic_started_by_path = {}
    host._storage_admission = lambda *_a, **_k: (True, '')
    host._camera_runtime_receipt_for_request = lambda _request: None
    host._show_save_busy_feedback = lambda: None
    host._set_status = lambda *_a, **_k: None
    host.push_error = lambda *_a, **_k: None
    host.capture_card = SimpleNamespace(
        show_error=lambda: None, show_clip=lambda *_a, **_k: None)
    monkeypatch.setattr(main, 'clips_directory_from', lambda _settings: tmp_path)
    monkeypatch.setattr(
        main, 'evaluate_save_admission',
        lambda _snapshot, duration: SimpleNamespace(
            allowed=True, duration_seconds=duration, reason=''))
    monkeypatch.setattr(main, 'emit_event', lambda *_a, **_k: None)

    class SaveState:
        def is_busy(self):
            return False

    host._save_state = SaveState()
    host._pump_save_responses = lambda: setattr(
        host, '_admission_transition_latch', object())
    native_calls = []
    host.bridge = SimpleNamespace(
        is_connected=lambda: True,
        save_clip=lambda *_a, **_k: native_calls.append(True) or True)

    main.MainWindow._save_clip(host, duration_seconds=5)

    assert native_calls == []
    assert host._admission_transition_latch is not None


def test_focus_resume_reentrant_admission_latch_cannot_reach_native_command():
    import main

    request = _request(main, mode='desktop')
    session = SimpleNamespace(
        request=request, accepted_config_token=7, process_generation=1,
        native_capture_generation=1)
    host = _host(_PlainSettings())
    host._admission_transition_active = (
        main.MainWindow._admission_transition_active.__get__(host))
    host._clear_focus_action = (
        main.MainWindow._clear_focus_action.__get__(host))
    host._accepted_capture_session = session
    host._capture_config = SimpleNamespace(
        accepted_token=7, active=request.capture_config)
    engine = SimpleNamespace(poll=lambda: None)
    resume_calls = []
    bridge = SimpleNamespace(
        is_connected=lambda: True,
        resume_recording=lambda: resume_calls.append(True) or True)
    host.engine_process = engine
    host.bridge = bridge
    host._engine_process_generation = 1
    host._engine_gen = 1
    host._accepted_session_owner_is_current = lambda *_a, **_k: True
    host._focus_action = 'resume'
    host._focus_action_generation = 1
    host._focus_action_deadline = main.time.monotonic() + 5.0
    host._focus_action_engine = engine
    host._focus_action_bridge = bridge
    host._focus_action_engine_generation = 1
    host._focus_action_timer = SimpleNamespace(
        stop=lambda: None, start=lambda: None, isActive=lambda: False)

    stable_proof = main.MainWindow._admission_stable_proof.__get__(host)

    def reentrant_proof(candidate):
        proof = stable_proof(candidate)
        host._admission_transition_latch = object()
        return proof

    host._admission_stable_proof = reentrant_proof

    main.MainWindow._pump_focus_action(host)

    assert resume_calls == []


def test_metadata_bind_rejects_in_place_policy_edit_before_replacing_owner(
        monkeypatch):
    import main

    monkeypatch.setattr(main.sys, 'platform', 'win32')
    exe = r'C:\Games\allowed.exe'
    raw = {
        'version': 1, 'revision': 1, 'enabled': True,
        'executable_paths': [exe],
    }
    host = _host(_PlainSettings(raw))
    active_request = _request(main, exe=exe)
    next_request = _request(main, exe=exe)
    active = SimpleNamespace(
        request=active_request, process_generation=1,
        native_capture_generation=1, accepted_config_token=7,
        manual_consent_nonce=None)
    host._accepted_capture_session = active
    host._capture_config = SimpleNamespace(
        active=active_request.capture_config, accepted_token=7,
        applying=None, applying_token=None, transition_revision=1)
    host.engine_process = SimpleNamespace(poll=lambda: None)
    host.bridge = SimpleNamespace(
        is_connected=lambda: True,
        get_status=lambda: {'connected': True, 'capture_generation': 1})
    host._engine_bridge_owner = host.bridge
    host._engine_gen = 1
    host._engine_process_generation = 1
    host._admission_transition_active = (
        main.MainWindow._admission_transition_active.__get__(host))
    host._configure_keyboard_capture_for_request = lambda _request: None
    host._camera_runtime_receipt_for_request = lambda _request: None
    host._consume_capture_intent = lambda *_a, **_k: None
    host._publish_capture_profile_status = lambda *_a, **_k: None
    bound = []
    monkeypatch.setattr(
        main, 'bind_capture_session',
        lambda request, **kwargs: bound.append((request, kwargs)) or request)
    candidate = SimpleNamespace(
        hwnd=4242, pid=321, exe_path=exe, class_name='GameWindow',
        process_created=9876)
    lookups = []

    def lookup(*_args, **_kwargs):
        lookups.append(True)
        if len(lookups) == 1:
            raw['executable_paths'] = [r'C:\Games\other.exe']
        return candidate

    monkeypatch.setattr(main, 'get_window_info', lookup)

    assert main.MainWindow._rebind_equivalent_capture_request(
        host, next_request, active) is False
    assert bound == []
    assert host._accepted_capture_session is active
    assert len(lookups) >= 2


def test_real_settings_page_keeps_allowed_games_lazy_until_explicit_navigation(
        qtbot, monkeypatch):
    import main
    from PySide6.QtWidgets import QWidget

    from core.capture_admission import empty_policy

    class _IsolatedSettings:
        def __init__(self):
            self.settings = {}
            self._navigation_bindings = None

        def get(self, key, default=None):
            return self.settings.get(key, default)

        def set(self, key, value):
            self.settings[key] = value

        def save_settings(self):
            return True

        def read_capture_admission_policy(self):
            return empty_policy()

    # Keep unrelated controls inert while exercising the real embedded route.
    for builder in (
            '_make_general_page', '_make_clip_page', '_make_audio_page',
            '_make_visuals_page', '_make_customize_page',
            '_make_performance_page', '_make_shortcuts_page',
            '_make_profiles_page'):
        monkeypatch.setattr(main._SettingsPage, builder,
                            lambda _self: QWidget())
    def clip_shell(owner):
        from PySide6.QtWidgets import QScrollArea, QVBoxLayout
        from ui.customize_page import _AccordionSection
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        sections = QVBoxLayout(content)
        owner._clip_embedded_sections = {
            7: _AccordionSection('Game profiles'),
            8: _AccordionSection('Allowed games'),
        }
        for section in owner._clip_embedded_sections.values():
            sections.addWidget(section)
        scroll.setWidget(content)
        layout.addWidget(scroll)
        return wrapper
    monkeypatch.setattr(main._SettingsPage, '_make_clip_page', clip_shell)
    monkeypatch.setattr(main._SettingsPage, '_load_audio_settings',
                        lambda _self: None)
    monkeypatch.setattr(main._SettingsPage, '_sync_encoder_controls',
                        lambda _self, **_kwargs: None)
    monkeypatch.setattr(main._SettingsPage, '_start_encoder_probe',
                        lambda _self: None)
    monkeypatch.setattr(main._SettingsPage, '_apply_styles', lambda _self: None)
    monkeypatch.setattr(main, 'PresetsManager', lambda: object())
    page = main._SettingsPage(_IsolatedSettings())
    qtbot.addWidget(page)

    old_titles = [
        'General', 'Clip', 'Audio', 'Visuals', 'Customize', 'Performance',
        'Shortcuts', 'Profiles',
    ]
    assert page._cat_titles[:8] == old_titles
    assert 8 not in page._loaded_categories
    page.set_capture_admission_status('Capture admission is disabled.')
    cached_windows = [{
        'exe_path': r'C:\Games\Cached.exe', 'display_name': 'Cached game',
    }]
    page.set_capture_admission_windows(cached_windows)
    assert 8 not in page._loaded_categories
    assert 'capture_admission_widget' not in page.__dict__

    page.navigate_category_to(8)
    page._apply_pending_category()
    assert 8 in page._loaded_categories
    assert 'capture_admission_widget' in page.__dict__
    widget = page.capture_admission_widget
    assert widget._capture_status_message == 'Capture admission is disabled.'
    assert widget._available_windows == cached_windows

    # Direct and repeated index-8 navigation keeps the same lazy widget.
    page.navigate_category_to(8)
    page._apply_pending_category()
    assert page.capture_admission_widget is widget
    for index in range(8):
        page.navigate_category_to(index)
        page._apply_pending_category()
        assert page.stack.currentIndex() == (1 if index == 7 else index)
    assert page._cat_titles[7] == 'Profiles'


def test_popup_policy_label_and_authoritative_reload_update_passively(
        qtbot, monkeypatch):
    import main
    from core.capture_admission import AdmissionDataError, AdmissionPolicy

    class _PopupSettings:
        def get(self, _key, default=None):
            return default

    class _Hotkeys:
        hotkeys = {
            'confirm_game_detection': 'F8',
            'dismiss_game_detection': 'F7',
        }

    # The popup's normal deferred refresh enumerates running windows.  This
    # test covers the passive policy label, so keep that host boundary inert.
    monkeypatch.setattr(main.GameDetectionPopup, '_refresh_running_windows',
                        lambda _self: None)
    popup = main.GameDetectionPopup(_PopupSettings(), _Hotkeys())
    qtbot.addWidget(popup)

    popup.set_capture_admission_policy_summary(
        'Capture admission disabled.')
    assert popup.capture_admission_policy_label.text() == (
        'Capture admission disabled.')
    popup.set_capture_admission_policy_summary(
        'Approved Windows executables only.')
    assert popup.capture_admission_policy_label.text() == (
        'Approved Windows executables only.')
    popup.set_capture_admission_policy_summary(
        'Capture admission unavailable.')
    assert popup.capture_admission_policy_label.text() == (
        'Capture admission unavailable.')

    class _PolicySettings:
        def __init__(self):
            self.settings = {'capture_admission_policy': object()}
            self.policy = AdmissionPolicy()
            self.failure = None

        def read_capture_admission_policy(self):
            if self.failure is not None:
                raise self.failure
            return self.policy

        def get(self, key, default=None):
            return self.settings.get(key, default)

    settings = _PolicySettings()
    page_updates = []
    host = SimpleNamespace(
        settings_manager=settings,
        _admission_policy=None,
        _admission_policy_error=None,
        _admission_policy_raw_owner=object(),
        _admission_epoch=0,
        _accepted_capture_session=None,
        game_detection_popup=popup,
        _settings_page_widget=SimpleNamespace(
            set_capture_admission_status=page_updates.append),
    )
    host._reload_admission_policy = (
        main.MainWindow._reload_admission_policy.__get__(host))
    host._publish_capture_admission_policy_summary = (
        main.MainWindow._publish_capture_admission_policy_summary.__get__(host))
    host._capture_admission_policy_summary = (
        main.MainWindow._capture_admission_policy_summary)

    host._reload_admission_policy(
        authoritative=True, trigger_transition=False)
    assert popup.capture_admission_policy_label.text() == (
        'Capture admission disabled.')
    assert page_updates[-1] == 'Capture admission disabled.'

    settings.settings['capture_admission_policy'] = object()
    settings.policy = AdmissionPolicy(enabled=True)
    host._reload_admission_policy(
        authoritative=True, trigger_transition=False)
    assert popup.capture_admission_policy_label.text() == (
        'Approved Windows executables only.')
    assert page_updates[-1] == 'Approved Windows executables only.'

    settings.settings['capture_admission_policy'] = object()
    settings.failure = AdmissionDataError('future policy version')
    host._reload_admission_policy(
        authoritative=True, trigger_transition=False)
    assert popup.capture_admission_policy_label.text() == (
        'Capture admission unavailable.')
    assert page_updates[-1] == 'Capture admission unavailable.'

    # An unchanged authoritative read does not republish or build UI work.
    update_count = len(page_updates)
    host._reload_admission_policy(
        authoritative=True, trigger_transition=False)
    assert len(page_updates) == update_count


class _OwnedCameraProvider:
    """Small stateful provider used to exercise Main's owner boundaries."""

    @classmethod
    def is_available(cls):
        return True

    def __init__(self):
        self.starts = []
        self.stops = []
        self.opening = {}
        self.running = {}
        self.generation = 0
        self.sync_stop_result = None

    def start_async(self, device_index, on_result=None, *, owner_token=None,
                    request_token=None):
        assert owner_token is not None
        self.starts.append({
            'device_index': device_index, 'owner_token': owner_token,
            'request_token': request_token, 'callback': on_result,
        })
        self.opening[request_token] = self.starts[-1]

    def emit_start(self, request_token, ok=True):
        opening = self.opening.pop(request_token)
        if ok:
            self.generation += 1
            self.running[request_token] = {
                'owner_token': opening['owner_token'],
                'device_index': opening['device_index'],
                'generation': self.generation,
            }
        opening['callback'](ok)

    def runtime_snapshot(self):
        if not self.running:
            from core.camera_recorder import CameraRuntimeSnapshot
            return CameraRuntimeSnapshot(0, self.generation, False, False)
        context = next(iter(self.running.values()))
        from core.camera_recorder import CameraRuntimeSnapshot
        return CameraRuntimeSnapshot(
            context['device_index'], context['generation'], True, True)

    def request_stop_async(self, *, expected_generation=None,
                           expected_device_index=None, owner_token=None,
                           request_token=None, on_result=None):
        target = self.opening.get(request_token) or self.running.get(
            request_token)
        self.stops.append({
            'owner_token': owner_token, 'request_token': request_token,
            'generation': expected_generation,
            'device_index': expected_device_index, 'callback': on_result,
        })
        if target is None or target['owner_token'] is not owner_token:
            return False
        self.stops[-1]['target'] = target
        if self.sync_stop_result is not None:
            result = bool(self.sync_stop_result)
            self.stops[-1]['emitted'] = True
            if result:
                self.opening.pop(request_token, None)
                self.running.pop(request_token, None)
            on_result(result)
            return result
        return True

    def emit_stop(self, request_token, ok=True):
        receipt = next(value for value in reversed(self.stops)
                       if value['request_token'] is request_token
                       and 'target' in value
                       and not value.get('emitted'))
        receipt['emitted'] = True
        if ok:
            self.opening.pop(request_token, None)
            self.running.pop(request_token, None)
        receipt['callback'](ok)

    def owned_cleanup_complete(self, *, owner_token):
        return not any(
            context['owner_token'] is owner_token
            for context in (*self.opening.values(), *self.running.values()))


class _AvailabilityCameraProvider(_OwnedCameraProvider):
    """Stateful provider whose availability hook can re-enter Main."""

    available = True
    hook = None

    @classmethod
    def is_available(cls):
        if cls.hook is not None:
            cls.hook()
        return cls.available


class _QueuedDispatcher:
    def __init__(self):
        self.callbacks = []

    def emit(self, callback):
        self.callbacks.append(callback)

    def drain(self):
        callbacks, self.callbacks = self.callbacks, []
        for callback in callbacks:
            callback()


def _camera_host(provider):
    import main

    host = _host(_PlainSettings())
    host._camera_owner_token = object()
    host._camera_provider = provider
    host._camera_pending_operation = None
    host._camera_retired_operations = []
    host._camera_active_physical = None
    host._camera_retired_physical = []
    host._camera_stop_receipts = []
    host._camera_disabled_request = None
    host._camera_apply_generation = 0
    host._camera_pending_request = None
    host._camera_runtime_receipt = None
    host._accepted_camera_runtime_receipt = None
    host._shutdown_requested = False
    host._shutdown_complete = False
    host._admission_transition_latch = None
    host._manual_prepare_owns_capture = lambda **_kwargs: False
    host._capture_resources_busy = lambda **_kwargs: False
    host._publish_capture_profile_status = lambda **_kwargs: None
    host._show_camera_unavailable = lambda _detail: None
    host._set_status = lambda *_args: None
    host._settings_page_widget = SimpleNamespace()
    host._ui_call = _QueuedDispatcher()
    for name in (
            '_camera_make_operation', '_camera_original_engine_is_current',
            '_camera_stable_request_is_current', '_camera_request_is_owned',
            '_camera_operation_boundary_current',
            '_camera_pre_start_boundary_current',
            '_camera_runtime_receipt_for_request',
            '_camera_record_operation_result', '_camera_stop_receipt_for',
            '_camera_submit_owned_stop',
            '_camera_cleanup_complete', '_camera_prune_completed_stops',
            '_camera_maybe_release_provider', '_camera_cancel_owned_contexts',
            '_camera_finish_disabled', '_camera_maybe_finish_disable',
            '_camera_queue_stop_continuation',
            '_request_camera_apply', '_on_camera_apply_result'):
        setattr(host, name, main.MainWindow.__dict__[name].__get__(
            host, main.MainWindow))
    return host


def test_owned_camera_denied_start_has_zero_provider_effects():
    from core.capture_session import CameraSnapshot

    provider = _OwnedCameraProvider()
    host = _camera_host(provider)
    request = _request(
        __import__('main'), mode='desktop',
        camera=CameraSnapshot(enabled=True, device_index=2))
    host._desired_capture_request = request
    host.settings_manager.settings['capture_admission_policy'] = {
        'version': 1, 'revision': 1, 'enabled': True,
        'executable_paths': [],
    }

    assert host._request_camera_apply(
        request.camera, origin='settings',
        request_token=request.request_token, capture_request=request) is False
    assert provider.starts == []
    assert provider.stops == []


def test_enabled_camera_missing_owner_or_request_has_zero_provider_effects():
    from core.capture_session import CameraSnapshot

    main = __import__('main')
    request = _request(
        main, mode='desktop',
        camera=CameraSnapshot(enabled=True, device_index=2))

    provider = _OwnedCameraProvider()
    host = _camera_host(provider)
    host._desired_capture_request = request
    host._camera_owner_token = None
    before_generation = host._camera_apply_generation
    assert host._request_camera_apply(
        request.camera, request_token=request.request_token,
        capture_request=request) is False
    assert provider.starts == []
    assert provider.stops == []
    assert host._camera_apply_generation == before_generation

    provider = _OwnedCameraProvider()
    host = _camera_host(provider)
    host._desired_capture_request = request
    assert host._request_camera_apply(
        request.camera, request_token=request.request_token) is False
    assert provider.starts == []
    assert provider.stops == []
    assert host._camera_apply_generation == before_generation


def test_camera_result_without_operation_has_zero_publish_effects():
    from core.capture_session import CameraSnapshot
    from core.camera_recorder import CameraRuntimeSnapshot

    main = __import__('main')
    provider = _OwnedCameraProvider()
    host = _camera_host(provider)
    request = _request(
        main, mode='desktop',
        camera=CameraSnapshot(enabled=True, device_index=2))
    shown = []
    published = []
    host._show_camera_unavailable = shown.append
    host._publish_capture_profile_status = (
        lambda **kwargs: published.append(kwargs))
    host._on_camera_apply_result(
        1, request.camera, True,
        CameraRuntimeSnapshot(2, 1, True, True), request.request_token,
        operation=None)
    assert provider.starts == []
    assert provider.stops == []
    assert host._camera_runtime_receipt is None
    assert shown == []
    assert published == []


def test_owned_camera_start_keeps_tokens_private_and_records_event_before_ui():
    from core.capture_session import CameraSnapshot

    provider = _OwnedCameraProvider()
    host = _camera_host(provider)
    request = _request(
        __import__('main'), mode='desktop',
        camera=CameraSnapshot(enabled=True, device_index=2))
    host._desired_capture_request = request
    assert host._request_camera_apply(
        CameraSnapshot(enabled=True, device_index=2),
        request_token=request.request_token, capture_request=request)
    start = provider.starts[-1]
    assert start['owner_token'] is host._camera_owner_token
    assert start['request_token'] is host._camera_pending_operation['nonce']
    provider.emit_start(start['request_token'])
    assert host._camera_pending_operation['event'].is_set()
    assert host._camera_runtime_receipt is None
    host._ui_call.drain()
    receipt = host._camera_runtime_receipt
    assert set(receipt) == {
        'device_index', 'generation', 'ready', 'origin', 'request_token'}
    assert receipt['request_token'] == request.request_token
    assert host._camera_active_physical['nonce'] is start['request_token']


def test_stale_camera_callback_cancels_only_A_and_preserves_B():
    from core.capture_session import CameraSnapshot

    provider = _OwnedCameraProvider()
    host = _camera_host(provider)
    main = __import__('main')
    request_a = _request(
        main, mode='desktop',
        camera=CameraSnapshot(enabled=True, device_index=2))
    request_b = replace(
        request_a, request_token=2,
        camera=CameraSnapshot(enabled=True, device_index=5))
    host._desired_capture_request = request_a
    assert host._request_camera_apply(
        request_a.camera, request_token=1, capture_request=request_a)
    start_a = provider.starts[-1]
    host._desired_capture_request = request_b
    assert host._request_camera_apply(
        request_b.camera, request_token=2, capture_request=request_b)
    start_b = provider.starts[-1]
    provider.emit_start(start_b['request_token'])
    host._ui_call.drain()
    provider.emit_start(start_a['request_token'])
    host._ui_call.drain()
    assert host._camera_active_physical['nonce'] is start_b['request_token']
    assert all(value['request_token'] is not start_b['request_token']
               for value in provider.stops)


def test_camera_latch_cancels_pending_open_without_publishing_success():
    from core.capture_session import CameraSnapshot

    provider = _OwnedCameraProvider()
    host = _camera_host(provider)
    main = __import__('main')
    request = _request(
        main, mode='desktop',
        camera=CameraSnapshot(enabled=True, device_index=2))
    host._desired_capture_request = request
    assert host._request_camera_apply(
        CameraSnapshot(enabled=True, device_index=2),
        request_token=request.request_token, capture_request=request)
    operation = host._camera_pending_operation
    host._admission_transition_latch = object()
    provider.emit_start(operation['nonce'])
    host._ui_call.drain()
    assert host._camera_runtime_receipt is None
    assert provider.stops[-1]['request_token'] is operation['nonce']


def test_still_allowed_policy_revision_reauthorizes_current_camera_request():
    from core.capture_session import CameraSnapshot

    provider = _OwnedCameraProvider()
    host = _camera_host(provider)
    main = __import__('main')
    request = _request(
        main, mode='desktop',
        camera=CameraSnapshot(enabled=True, device_index=2))
    host._desired_capture_request = request
    assert host._request_camera_apply(
        CameraSnapshot(enabled=True, device_index=2),
        request_token=request.request_token, capture_request=request)
    host.settings_manager.settings['capture_admission_policy'] = {
        'version': 1, 'revision': 2, 'enabled': False,
        'executable_paths': [],
    }
    provider.emit_start(provider.starts[-1]['request_token'])
    host._ui_call.drain()
    assert host._camera_runtime_receipt['request_token'] == request.request_token


def test_disable_stops_owned_active_camera_even_when_new_admission_is_denied():
    from core.capture_session import CameraSnapshot

    provider = _OwnedCameraProvider()
    host = _camera_host(provider)
    main = __import__('main')
    request = _request(
        main, mode='desktop',
        camera=CameraSnapshot(enabled=True, device_index=2))
    host._desired_capture_request = request
    assert host._request_camera_apply(
        CameraSnapshot(enabled=True, device_index=2),
        request_token=request.request_token, capture_request=request)
    provider.emit_start(provider.starts[-1]['request_token'])
    host._ui_call.drain()
    host._admission_decision_for_settings = (
        lambda: SimpleNamespace(allowed=False))
    assert host._request_camera_apply(
        CameraSnapshot(enabled=False, device_index=2), origin='settings')
    stop = provider.stops[-1]
    assert stop['request_token'] is host._camera_active_physical['nonce']
    provider.emit_stop(stop['request_token'])
    host._ui_call.drain()
    assert host._camera_runtime_receipt is None
    assert host._camera_provider is None


def test_pending_and_active_camera_stops_are_independent_and_false_is_not_clean():
    from core.capture_session import CameraSnapshot

    provider = _OwnedCameraProvider()
    host = _camera_host(provider)
    main = __import__('main')
    request_a = _request(
        main, mode='desktop',
        camera=CameraSnapshot(enabled=True, device_index=2))
    request_b = replace(
        request_a, request_token=2,
        camera=CameraSnapshot(enabled=True, device_index=7))
    host._desired_capture_request = request_a
    assert host._request_camera_apply(
        request_a.camera, request_token=1, capture_request=request_a)
    provider.emit_start(provider.starts[-1]['request_token'])
    host._ui_call.drain()
    retained_a_nonce = host._camera_active_physical['nonce']
    host._desired_capture_request = request_b
    assert host._request_camera_apply(
        request_b.camera, request_token=2, capture_request=request_b)
    pending = host._camera_pending_operation
    assert host._camera_cancel_owned_contexts() is False
    assert {id(value['request_token']) for value in provider.stops} >= {
        id(retained_a_nonce), id(pending['nonce'])}
    first = provider.stops[-1]
    provider.emit_stop(first['request_token'], ok=False)
    assert host._camera_cleanup_complete() is False


def test_metadata_only_accepted_request_preserves_equivalent_camera_warmup():
    """A pre-Popen camera A may finish after equivalent metadata B binds."""
    import main
    from core.capture_session import CameraSnapshot

    provider = _OwnedCameraProvider()
    host = _camera_host(provider)
    request_a = _request(
        main, mode='desktop',
        camera=CameraSnapshot(enabled=True, device_index=2))
    request_b = replace(
        request_a, request_token=2,
        profile_id='12345678-1234-5678-1234-567812345678',
        profile_name='metadata-only-B',
        profile_revision=4)
    tracker = SimpleNamespace(
        active=None, accepted_token=None, applying=None,
        applying_token=None, transition_revision=1)
    host._capture_config = tracker
    host._engine_gen = 1
    host.engine_process = None
    host.bridge = None
    host._engine_bridge_owner = None
    host._engine_process_generation = None
    host._accepted_capture_session = None
    host._pending_launch_request = request_a
    host._desired_capture_request = request_a
    assert host._request_camera_apply(
        request_a.camera, request_token=request_a.request_token,
        capture_request=request_a)

    # The real launch then applies Tracker/Popen ownership and accepts A.
    tracker.active = request_a.capture_config
    tracker.accepted_token = 7
    process = object()
    bridge = object()
    host.engine_process = process
    host.bridge = bridge
    host._engine_bridge_owner = object()
    host._engine_process_generation = 1
    host._accepted_capture_session = SimpleNamespace(
        request=request_a, process_generation=1,
        native_capture_generation=9, accepted_config_token=7)
    host._pending_launch_request = None

    # Handshake consumed A before the metadata-only B session was accepted.
    host._accepted_capture_session = SimpleNamespace(
        request=request_b, process_generation=1,
        native_capture_generation=9, accepted_config_token=7)
    host._desired_capture_request = request_b
    provider.emit_start(provider.starts[-1]['request_token'])
    host._ui_call.drain()

    assert host._camera_active_physical['request'] is request_a
    assert provider.stops == []
    assert host._camera_runtime_receipt_for_request(request_b) is not None


def test_prelaunch_camera_rejects_later_engine_generation():
    import main
    from core.capture_session import CameraSnapshot

    provider = _OwnedCameraProvider()
    host = _camera_host(provider)
    request = _request(
        main, mode='desktop',
        camera=CameraSnapshot(enabled=True, device_index=2))
    host._engine_gen = 1
    host._pending_launch_request = request
    host._desired_capture_request = request
    assert host._request_camera_apply(
        request.camera, request_token=request.request_token,
        capture_request=request)
    operation = host._camera_pending_operation
    host._engine_gen = 2
    provider.emit_start(operation['nonce'])
    host._ui_call.drain()
    assert host._camera_runtime_receipt is None
    assert provider.stops[-1]['request_token'] is operation['nonce']


def test_busy_hook_replacing_native_owner_cannot_start_desired_A():
    import main
    from core.capture_session import CameraSnapshot

    provider = _OwnedCameraProvider()
    host = _camera_host(provider)
    request = _request(
        main, mode='desktop',
        camera=CameraSnapshot(enabled=True, device_index=2))
    tracker = SimpleNamespace(
        active=request.capture_config, accepted_token=7, applying=None,
        applying_token=None, transition_revision=1)
    process_a = object()
    bridge_a = object()
    host._capture_config = tracker
    host.engine_process = process_a
    host.bridge = bridge_a
    host._engine_bridge_owner = bridge_a
    host._engine_gen = 1
    host._engine_process_generation = 1
    host._accepted_capture_session = SimpleNamespace(
        request=request, process_generation=1,
        native_capture_generation=9, accepted_config_token=7)
    host._desired_capture_request = request

    replacement = object()

    def busy_hook(**_kwargs):
        host.engine_process = replacement
        return False

    host._capture_resources_busy = busy_hook
    assert host._request_camera_apply(
        request.camera, request_token=request.request_token,
        capture_request=request) is False
    assert provider.starts == []
    assert host.engine_process is replacement


def test_availability_hook_replacing_native_owner_cannot_start_desired_A():
    import main
    from core.capture_session import CameraSnapshot

    provider_type = _AvailabilityCameraProvider
    provider_type.available = True
    provider = provider_type()
    host = _camera_host(provider)
    request = _request(
        main, mode='desktop',
        camera=CameraSnapshot(enabled=True, device_index=2))
    tracker = SimpleNamespace(
        active=request.capture_config, accepted_token=7, applying=None,
        applying_token=None, transition_revision=1)
    process_a = object()
    bridge_a = object()
    host._capture_config = tracker
    host.engine_process = process_a
    host.bridge = bridge_a
    host._engine_bridge_owner = bridge_a
    host._engine_gen = 1
    host._engine_process_generation = 1
    host._accepted_capture_session = SimpleNamespace(
        request=request, process_generation=1,
        native_capture_generation=9, accepted_config_token=7)
    host._desired_capture_request = request
    replacement = object()

    def availability_hook():
        host.engine_process = replacement

    provider_type.hook = availability_hook
    try:
        assert host._request_camera_apply(
            request.camera, request_token=request.request_token,
            capture_request=request) is False
    finally:
        provider_type.hook = None
        provider_type.available = True
    assert provider.starts == []
    assert host.engine_process is replacement


def test_availability_false_hook_B_cannot_publish_A_error():
    import main
    from core.capture_session import CameraSnapshot

    provider_type = _AvailabilityCameraProvider
    provider_type.available = False
    provider_type.hook = None
    provider = provider_type()
    host = _camera_host(provider)
    request_a = _request(
        main, mode='desktop',
        camera=CameraSnapshot(enabled=True, device_index=2))
    request_b = replace(
        request_a, request_token=2,
        camera=CameraSnapshot(enabled=True, device_index=5))
    host._desired_capture_request = request_a
    shown = []
    host._show_camera_unavailable = shown.append

    def availability_hook():
        host._desired_capture_request = request_b

    provider_type.hook = availability_hook
    try:
        assert host._request_camera_apply(
            request_a.camera, request_token=request_a.request_token,
            capture_request=request_a) is False
    finally:
        provider_type.hook = None
        provider_type.available = True
    assert provider.starts == []
    assert shown == []
    assert host._desired_capture_request is request_b


def test_availability_false_page_callback_B_preserves_successor_ui():
    import main
    from core.capture_session import CameraSnapshot

    provider_type = _AvailabilityCameraProvider
    provider_type.available = False
    provider_type.hook = None
    provider = provider_type()
    host = _camera_host(provider)
    request_a = _request(
        main, mode='desktop',
        camera=CameraSnapshot(enabled=True, device_index=2))
    request_b = replace(
        request_a, request_token=2,
        camera=CameraSnapshot(enabled=True, device_index=5))
    host._desired_capture_request = request_a
    successor_receipt = object()
    shown = []
    host._show_camera_unavailable = shown.append
    host._settings_page_widget._on_main_camera_apply = (
        lambda _camera, _ok, _detail: (
            setattr(host, '_desired_capture_request', request_b),
            setattr(host, '_camera_apply_generation', 1),
            setattr(host, '_camera_runtime_receipt', successor_receipt)))
    try:
        assert host._request_camera_apply(
            request_a.camera, request_token=request_a.request_token,
            capture_request=request_a) is False
    finally:
        provider_type.hook = None
        provider_type.available = True
    assert provider.starts == []
    assert shown == []
    assert host._desired_capture_request is request_b
    assert host._camera_runtime_receipt is successor_receipt


def test_final_proof_reentrant_B_same_owner_cannot_start_A():
    import main
    from core.capture_session import CameraSnapshot

    provider = _OwnedCameraProvider()
    host = _camera_host(provider)
    request_a = _request(
        main, mode='desktop',
        camera=CameraSnapshot(enabled=True, device_index=2))
    request_b = replace(
        request_a, request_token=2,
        profile_id='12345678-1234-5678-1234-567812345678',
        profile_name='metadata-B',
        profile_revision=4)
    tracker = SimpleNamespace(
        active=request_a.capture_config, accepted_token=7, applying=None,
        applying_token=None, transition_revision=1)
    bridge = object()
    host._capture_config = tracker
    host.engine_process = object()
    host.bridge = bridge
    host._engine_bridge_owner = bridge
    host._engine_gen = 1
    host._engine_process_generation = 1
    host._accepted_capture_session = SimpleNamespace(
        request=request_a, process_generation=1,
        native_capture_generation=9, accepted_config_token=7)
    host._desired_capture_request = request_a
    original_proof = host._admission_stable_proof
    proof_calls_for_a = 0

    def proof(request):
        nonlocal proof_calls_for_a
        result = original_proof(request)
        if request is request_a:
            proof_calls_for_a += 1
            if proof_calls_for_a == 2:
                host._desired_capture_request = request_b
                assert host._request_camera_apply(
                    request_b.camera, request_token=request_b.request_token,
                    capture_request=request_b)
        return result

    host._admission_stable_proof = proof
    assert host._request_camera_apply(
        request_a.camera, request_token=request_a.request_token,
        capture_request=request_a) is False
    assert proof_calls_for_a == 2
    assert len(provider.starts) == 1
    start_b = provider.starts[0]
    provider.emit_start(start_b['request_token'])
    host._ui_call.drain()
    assert host._camera_runtime_receipt['request_token'] == request_b.request_token
    assert host._camera_apply_generation == 1
    assert host._camera_active_physical['request'] is request_b


def test_camera_worker_records_before_queued_dispatch_and_never_falls_back():
    import main
    from core.capture_session import CameraSnapshot

    provider = _OwnedCameraProvider()
    host = _camera_host(provider)
    request = _request(
        main, mode='desktop',
        camera=CameraSnapshot(enabled=True, device_index=2))
    host._desired_capture_request = request
    assert host._request_camera_apply(
        request.camera, request_token=request.request_token,
        capture_request=request)
    operation = host._camera_pending_operation
    worker = threading.Thread(
        target=provider.emit_start,
        args=(operation['nonce'],), name='camera-callback')
    worker.start()
    worker.join()
    assert operation['event'].is_set()
    assert operation['result'] is True
    assert host._camera_runtime_receipt is None
    assert host._camera_pending_operation is operation
    host._ui_call.drain()
    assert host._camera_runtime_receipt['device_index'] == 2

    # With the Qt dispatcher gone, the worker records evidence and stops.  It
    # must never execute the Main continuation on its own thread.
    provider_b = _OwnedCameraProvider()
    host_b = _camera_host(provider_b)
    request_b = replace(request, request_token=3)
    host_b._desired_capture_request = request_b
    host_b._ui_call = None
    assert host_b._request_camera_apply(
        request_b.camera, request_token=request_b.request_token,
        capture_request=request_b)
    operation_b = host_b._camera_pending_operation
    worker = threading.Thread(
        target=provider_b.emit_start,
        args=(operation_b['nonce'],), name='camera-callback-no-dispatcher')
    worker.start()
    worker.join()
    assert operation_b['event'].is_set()
    assert host_b._camera_runtime_receipt is None
    assert host_b._camera_pending_operation is operation_b


def test_success_page_callback_B_cannot_publish_A_trailing_status():
    import main
    from core.capture_session import CameraSnapshot

    provider = _OwnedCameraProvider()
    host = _camera_host(provider)
    request_a = _request(
        main, mode='desktop',
        camera=CameraSnapshot(enabled=True, device_index=2))
    request_b = replace(request_a, request_token=2)
    host._desired_capture_request = request_a
    status = []
    successor_receipt = object()
    host._publish_capture_profile_status = (
        lambda **kwargs: status.append(kwargs))
    host._settings_page_widget._on_main_camera_apply = (
        lambda _camera, _ok, _detail: (
            setattr(host, '_desired_capture_request', request_b),
            setattr(host, '_camera_apply_generation',
                    host._camera_apply_generation + 1),
            setattr(host, '_camera_runtime_receipt', successor_receipt)))
    assert host._request_camera_apply(
        request_a.camera, request_token=request_a.request_token,
        capture_request=request_a)
    provider.emit_start(provider.starts[-1]['request_token'])
    host._ui_call.drain()
    assert status == []
    assert host._desired_capture_request is request_b
    assert host._camera_runtime_receipt is successor_receipt


def test_failure_page_callback_B_cannot_publish_A_trailing_warning():
    import main
    from core.capture_session import CameraSnapshot

    provider = _OwnedCameraProvider()
    host = _camera_host(provider)
    request_a = _request(
        main, mode='desktop',
        camera=CameraSnapshot(enabled=True, device_index=2))
    request_b = replace(request_a, request_token=2)
    host._desired_capture_request = request_a
    shown = []
    successor_receipt = object()
    host._show_camera_unavailable = shown.append
    host._settings_page_widget._on_main_camera_apply = (
        lambda _camera, _ok, _detail: (
            setattr(host, '_desired_capture_request', request_b),
            setattr(host, '_camera_apply_generation',
                    host._camera_apply_generation + 1),
            setattr(host, '_camera_runtime_receipt', successor_receipt)))
    assert host._request_camera_apply(
        request_a.camera, request_token=request_a.request_token,
        capture_request=request_a)
    provider.emit_start(provider.starts[-1]['request_token'], ok=False)
    host._ui_call.drain()
    assert shown == []
    assert host._desired_capture_request is request_b
    assert host._camera_runtime_receipt is successor_receipt


def test_camera_sync_false_stop_is_recorded_before_request_returns():
    import main
    from core.capture_session import CameraSnapshot

    provider = _OwnedCameraProvider()
    host = _camera_host(provider)
    request = _request(
        main, mode='desktop',
        camera=CameraSnapshot(enabled=True, device_index=2))
    host._desired_capture_request = request
    assert host._request_camera_apply(
        request.camera, request_token=request.request_token,
        capture_request=request)
    provider.emit_start(provider.starts[-1]['request_token'])
    host._ui_call.drain()
    provider.sync_stop_result = False
    assert host._camera_cancel_owned_contexts() is False
    receipt = host._camera_stop_receipts[-1]
    assert receipt['callback_recorded'] is True
    assert receipt['result'] is False
    assert receipt['submitted'] is False
    assert host._camera_cleanup_complete() is False


def test_camera_reentrant_proof_preserves_successor_B_for_both_outcomes():
    import main
    from core.capture_session import CameraSnapshot

    for proof_result in (True, False):
        provider = _OwnedCameraProvider()
        host = _camera_host(provider)
        request_a = _request(
            main, mode='desktop',
            camera=CameraSnapshot(enabled=True, device_index=2))
        request_b = replace(
            request_a, request_token=2,
            camera=CameraSnapshot(enabled=True, device_index=5))
        host._desired_capture_request = request_a
        assert host._request_camera_apply(
            request_a.camera, request_token=1, capture_request=request_a)
        original_proof = host._admission_stable_proof
        entered = False

        def proof(request, *, request_a=request_a, request_b=request_b,
                  proof_result=proof_result, host=host,
                  original_proof=original_proof):
            nonlocal entered
            if request is request_a and not entered:
                entered = True
                host._desired_capture_request = request_b
                assert host._request_camera_apply(
                    request_b.camera, request_token=2,
                    capture_request=request_b)
                if not proof_result:
                    return None
            return original_proof(request)

        host._admission_stable_proof = proof
        provider.emit_start(provider.starts[0]['request_token'])
        host._ui_call.drain()
        assert host._camera_pending_operation is not None
        assert host._camera_pending_operation['capture_request'] is request_b
        assert host._camera_pending_request is request_b.camera
        assert provider.starts[-1]['request_token'] is not provider.starts[0]['request_token']


def test_disabled_without_owned_provider_has_no_probe_or_provider_effect():
    from core.capture_session import CameraSnapshot

    provider = _OwnedCameraProvider()
    host = _camera_host(provider)
    host._camera_provider = None
    assert host._request_camera_apply(
        CameraSnapshot(enabled=False, device_index=0), origin='settings')
    assert provider.starts == []
    assert provider.stops == []


def test_same_device_owned_start_adopts_without_prophylactic_A_stop():
    import main
    from core.capture_session import CameraSnapshot

    provider = _OwnedCameraProvider()
    host = _camera_host(provider)
    request_a = _request(
        main, mode='desktop',
        camera=CameraSnapshot(enabled=True, device_index=2))
    request_b = replace(request_a, request_token=2)
    host._desired_capture_request = request_a
    assert host._request_camera_apply(
        request_a.camera, request_token=1, capture_request=request_a)
    provider.emit_start(provider.starts[-1]['request_token'])
    host._ui_call.drain()
    host._desired_capture_request = request_b
    assert host._request_camera_apply(
        request_b.camera, request_token=2, capture_request=request_b)
    assert provider.stops == []


def test_stale_nonce_stop_cannot_claim_cleanup_or_touch_active_owner():
    import main
    from core.capture_session import CameraSnapshot

    provider = _OwnedCameraProvider()
    host = _camera_host(provider)
    request = _request(
        main, mode='desktop',
        camera=CameraSnapshot(enabled=True, device_index=2))
    host._desired_capture_request = request
    assert host._request_camera_apply(
        request.camera, request_token=request.request_token,
        capture_request=request)
    provider.emit_start(provider.starts[-1]['request_token'])
    host._ui_call.drain()
    stale = dict(host._camera_active_physical)
    stale['nonce'] = object()
    receipt = host._camera_submit_owned_stop(stale)
    assert receipt['submitted'] is False
    assert host._camera_cleanup_complete() is False
    assert host._camera_active_physical['nonce'] is not stale['nonce']
