"""Main-owned admission revocation and retained-owner regressions."""

from __future__ import annotations

import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest


class _Timer:
    def __init__(self):
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


class _Process:
    def __init__(self, code=None):
        self.code = code
        self.returncode = code
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self):
        return self.code

    def terminate(self):
        self.terminate_calls += 1

    def kill(self):
        self.kill_calls += 1

    def wait(self, timeout=None):
        self.returncode = self.code
        return self.returncode


class _PollErrorProcess(_Process):
    def poll(self):
        raise RuntimeError('poll unavailable')


class _Bridge:
    """Stateful bridge fake with the same owner/release contract as Core."""

    def __init__(self, process=None, snapshot=None, *, shutdown_accept=True):
        self.process = process
        self.snapshot = snapshot
        self.shutdown_accept = shutdown_accept
        self.shutdown_calls = 0
        self.request_calls = 0
        self.events = []
        self.owner = object()
        self.capture_generation = 42
        self.owners_by_generation = {self.capture_generation: self.owner}
        self.remapped = False
        self.physically_absent = False

    def request_engine_shutdown(self):
        self.request_calls += 1
        self.events.append('request')
        return self.shutdown_accept

    def get_status(self):
        return {'capture_generation': self.capture_generation,
                'connected': True}

    def retain_terminal_response_owner(self, *, expected_capture_generation):
        self.events.append(('retain', expected_capture_generation))
        if (self.remapped or self.process is None
                or self.process.poll() is not None):
            return None
        if expected_capture_generation != self.capture_generation:
            return None
        self.owners_by_generation.setdefault(
            expected_capture_generation, self.owner)
        return self.owners_by_generation[expected_capture_generation]

    def read_terminal_response_snapshot(self, owner, *, process_exited):
        self.events.append(('read', process_exited))
        if self.remapped:
            return None
        current_owner = self.owners_by_generation.get(self.capture_generation)
        if owner is not current_owner or not process_exited:
            raise RuntimeError('foreign or live terminal read')
        return self.snapshot

    def release_terminal_response_owner(self, owner=None):
        self.events.append(('release', owner))
        if owner is None:
            return self.physically_absent
        current_owner = self.owners_by_generation.get(self.capture_generation)
        return owner is current_owner and not self.remapped

    def shutdown(self):
        self.events.append('shutdown')
        self.shutdown_calls += 1


class _LifecycleLog:
    def __init__(self):
        self.messages = []

    def info(self, message, *args):
        self.messages.append(('info', message, args))

    def error(self, message, *args):
        self.messages.append(('error', message, args))


class _CameraProvider:
    def __init__(self, *, result=True, worker=False, on_submit=None):
        self.result = result
        self.worker = worker
        self.on_submit = on_submit
        self.calls = []
        self.cleaned = set()

    def request_stop_async(self, **kwargs):
        self.calls.append(kwargs)
        if self.on_submit is not None:
            self.on_submit()

        def complete():
            if self.result:
                self.cleaned.add(kwargs['owner_token'])
            kwargs['on_result'](self.result)

        if self.worker:
            thread = threading.Thread(target=complete)
            thread.start()
            thread.join()
        else:
            complete()
        return True

    def owned_cleanup_complete(self, *, owner_token):
        return owner_token in self.cleaned


def _host(process=None, bridge=None):
    """Bind the real Main admission/camera helpers to a small host object."""
    import main

    bridge = bridge or _Bridge(process)
    if process is not None:
        bridge.process = process
    timer = _Timer()
    host = SimpleNamespace(
        engine_process=process,
        bridge=bridge,
        _engine_bridge_owner=bridge if process is not None else None,
        _engine_process_generation=8 if process is not None else None,
        _terminal_response_owner_cache=None,
        _retained_admission_teardown_receipt=None,
        _admission_transition_latch=None,
        _admission_transition_timer=timer,
        _capture_intent_revision=4,
        _latest_capture_intent_consumed=False,
        _pending_capture_reason='',
        _accepted_capture_session=None,
        _accepted_camera_runtime_receipt=None,
        _set_status=lambda *_args: None,
        _shutdown_requested=False,
        _shutdown_complete=False,
        _shutdown_pending_exit=False,
        _shutdown_forced=False,
        _shutdown_uncertain_owner=False,
        _shutdown_timer_started=None,
        _tray_icon=None,
        _active_clip_viewer=None,
        _parked_editor_shell=None,
        _lifecycle_log=_LifecycleLog(),
        _camera_provider=None,
        _camera_owner_token=None,
        _camera_runtime_receipt=None,
        _camera_pending_operation=None,
        _camera_active_physical=None,
        _camera_retired_operations=[],
        _camera_retired_physical=[],
        _camera_stop_receipts=[],
        _camera_disabled_request=None,
        _camera_disabled_generation=None,
        _camera_apply_generation=0,
        _camera_pending_request=None,
        _manual_record_state='idle',
        _manual_record_path=None,
        _manual_record_request=None,
        _manual_record_session=None,
        _manual_record_process_generation=None,
        _manual_record_native_generation=None,
        _pending_manual_record_path=None,
        _pending_manual_record_request=None,
        _pending_manual_record_session=None,
        _pending_manual_record_process_generation=None,
        _pending_manual_record_native_generation=None,
        _manual_record_retry_pending=False,
        _manual_record_timer=timer,
        _save_state=None,
        _ui_call=None,
        hide=lambda: None,
        push_error=lambda *_args, **_kwargs: None,
        _shutdown_mark=lambda *_args: None,
    )
    names = (
        '_admission_transition_active', '_begin_admission_transition',
        '_cached_terminal_response_owner',
        '_cached_terminal_response_owner_lineage',
        '_retain_terminal_response_owner_cache',
        '_release_terminal_response_owner_cache',
        '_invalidate_terminal_response_owner_cache',
        '_admission_original_latch_is_current',
        '_admission_receipt_matches_current',
        '_admission_has_transition_obligation',
        '_log_forced_admission_facts', '_forced_non_qt_shutdown_tail',
        '_publish_engine_crash_recovery',
        '_update_status',
        '_pump_admission_transition', '_force_admission_transition_for_exit',
        '_admission_handle_terminal_snapshot', '_defer_full_exit_for_admission',
        'stop_engine', '_close_engine_startup_output',
        '_camera_retained_group', '_camera_stop_receipt_for',
        '_camera_submit_owned_stop', '_camera_cleanup_complete',
        '_camera_release_retained_group', '_camera_queue_stop_continuation',
        '_camera_maybe_finish_disable', '_camera_maybe_release_provider',
        '_camera_prune_completed_stops',
        '_camera_cancel_owned_contexts', 'request_full_exit',
        '_perform_full_shutdown',
        '_capture_admission_policy_summary', 'set_capture_admission_status',
        '_set_capture_admission_windows_cache',
    )
    for name in names:
        descriptor = main.MainWindow.__dict__.get(name)
        method = getattr(main.MainWindow, name)
        if isinstance(descriptor, staticmethod):
            setattr(host, name, method)
        else:
            setattr(host, name, method.__get__(host))
    return host


def _session_snapshot():
    from core.capture_session import (
        CaptureSessionSnapshot,
        ResolvedCaptureRequest,
        SourceSnapshot,
    )
    from core.capture_settings import CaptureConfig

    config = CaptureConfig(
        fps=60, buffer_seconds=32, width=1920, height=1080,
        bitrate_kbps=8000, codec='h264', preset=4, monitor='primary',
        scaling='fit', audio_enabled=True,
    )
    request = ResolvedCaptureRequest(
        request_token=1, purpose='clips',
        source=SourceSnapshot('desktop', display_name='Desktop'),
        capture_config=config,
        profile_id='12345678-1234-5678-1234-567812345678',
        profile_revision=1, profile_name='QA profile',
        profile_document_revision=1,
    )
    return CaptureSessionSnapshot(
        request=request, process_generation=8, native_capture_generation=42,
        accepted_config_token=1, allowlist_revision=3,
    )


def test_no_process_has_positive_absence_without_bridge_shutdown():
    bridge = _Bridge()
    bridge.physically_absent = True
    host = _host(None, bridge)

    assert host._begin_admission_transition(reason='revoked') is True
    host._pump_admission_transition()

    assert bridge.request_calls == 0
    assert bridge.shutdown_calls == 0
    assert ('release', None) in bridge.events
    assert host._admission_transition_latch is None


def test_camera_only_cold_bridge_uses_readonly_absence_and_owned_cleanup():
    bridge = _Bridge()
    bridge.physically_absent = True
    provider = _CameraProvider()
    owner = object()
    provider.cleaned.add(owner)
    host = _host(None, bridge)
    host._camera_provider = provider
    host._camera_owner_token = owner

    host._begin_admission_transition(reason='camera disabled')
    host._pump_admission_transition()

    assert ('release', None) in bridge.events
    assert host._camera_provider is None
    assert host._admission_transition_latch is None


def test_foreign_process_mapping_is_left_untouched():
    process = _Process()
    foreign = _Bridge(process)
    owner = _Bridge(process)
    host = _host(process, foreign)
    host._engine_bridge_owner = owner

    host._begin_admission_transition(reason='revoked')
    host._pump_admission_transition()

    assert foreign.request_calls == 0
    assert not any(event[0] == 'release' for event in foreign.events)
    assert host._admission_transition_latch is not None
    latch = host._admission_transition_latch
    assert latch['source_process'] is process
    assert latch['process'] is None
    assert latch['native_absence_proven'] is False
    assert latch['native_exit_proven'] is False

    latch['started_at'] -= 10.0
    host._pump_admission_transition()
    host._force_admission_transition_for_exit()

    assert host._admission_transition_latch is latch
    assert host._shutdown_uncertain_owner is True
    assert process.terminate_calls == process.kill_calls == 0
    assert foreign.events == owner.events == []
    assert foreign.shutdown_calls == owner.shutdown_calls == 0


def test_live_owner_is_retained_before_shutdown_and_released_after_read():
    process = _Process()
    bridge = _Bridge(process, snapshot=None)
    host = _host(process, bridge)

    assert host._begin_admission_transition(reason='revoked') is True
    assert bridge.events.index(('retain', 42)) < bridge.events.index('request')
    process.code = 0
    host._pump_admission_transition()

    read_index = next(i for i, event in enumerate(bridge.events)
                      if event == ('read', True))
    release_index = next(i for i, event in enumerate(bridge.events)
                         if event[0] == 'release')
    assert read_index < release_index
    assert bridge.shutdown_calls == 0
    assert host._admission_transition_latch is None


def test_completed_a_receipt_protects_absent_a_and_rejects_b_resources():
    process = _Process()
    bridge = _Bridge(process, snapshot=None)
    host = _host(process, bridge)
    provider_a = _CameraProvider()
    owner_a = object()
    target_a = {
        'provider': provider_a, 'owner_token': owner_a, 'nonce': object(),
        'provider_generation': 4, 'device_index': 1,
    }
    host._camera_provider = provider_a
    host._camera_owner_token = owner_a
    host._camera_active_physical = target_a

    host._begin_admission_transition(reason='completed A receipt')
    process.code = 0
    host._pump_admission_transition()

    receipt = host._retained_admission_teardown_receipt
    assert receipt['native_exit_proven'] is True
    assert receipt['terminal_released'] is True
    assert receipt['camera_cleanup_proven'] is True
    assert host.engine_process is None
    assert host._camera_provider is None
    assert host._camera_active_physical is None
    assert host._admission_receipt_matches_current(receipt, 'native')
    assert host._admission_receipt_matches_current(receipt, 'camera')

    host.engine_process = _Process()
    host.bridge = _Bridge(host.engine_process)
    host._engine_bridge_owner = host.bridge
    host._engine_process_generation = 9
    assert not host._admission_receipt_matches_current(receipt, 'native')

    provider_b = _CameraProvider()
    owner_b = object()
    target_b = {
        'provider': provider_b, 'owner_token': owner_b, 'nonce': object(),
        'provider_generation': 5, 'device_index': 2,
    }
    host._camera_provider = provider_b
    host._camera_owner_token = owner_b
    host._camera_active_physical = target_b
    assert not host._admission_receipt_matches_current(receipt, 'camera')


def test_preheld_mapping_cache_handles_exit_without_a_second_retain():
    process = _Process()
    bridge = _Bridge(process, snapshot=None)
    host = _host(process, bridge)
    host._accepted_capture_session = SimpleNamespace(
        process_generation=8, native_capture_generation=42)
    assert host._retain_terminal_response_owner_cache(
        process, bridge, 8, 42) is bridge.owner
    process.code = 0

    host._begin_admission_transition(reason='revoked')
    host._pump_admission_transition()

    assert bridge.events.count(('retain', 42)) == 1
    assert ('read', True) in bridge.events
    assert host._terminal_response_owner_cache is None
    assert host._admission_transition_latch is None


def test_preaccepted_exited_owner_uses_only_previously_positive_cache():
    process = _Process()
    bridge = _Bridge(process, snapshot=None)
    host = _host(process, bridge)
    owner = host._retain_terminal_response_owner_cache(
        process, bridge, 8, 42)
    assert owner is bridge.owner
    process.code = 0
    host._begin_admission_transition(reason='revoked after pre-Popen')

    latch = host._admission_transition_latch
    assert latch['native_generation'] == 42
    assert latch['terminal_owner'] is owner
    assert bridge.events.count(('retain', 42)) == 1
    host._pump_admission_transition()
    assert host._admission_transition_latch is None


def test_reentrant_process_poll_cannot_publish_a_over_successor_b():
    process = _Process()
    bridge = _Bridge(process)
    host = _host(process, bridge)
    successor_process = _Process()
    successor_bridge = _Bridge(successor_process)
    successor = {'reason': 'B'}

    def install_b():
        host.engine_process = successor_process
        host.bridge = successor_bridge
        host._engine_bridge_owner = successor_bridge
        host._engine_process_generation = 9
        host._admission_transition_latch = successor

    original_poll = process.poll

    def reentrant_poll():
        result = original_poll()
        install_b()
        return result

    process.poll = reentrant_poll
    host._begin_admission_transition(reason='process poll reentry')

    assert host._admission_transition_latch is successor
    assert host.engine_process is successor_process
    assert host.bridge is successor_bridge


def test_defaultoff_session_and_weak_cache_keep_existing_error006_route():
    from core.capture_admission import AdmissionPolicy

    process = _Process(code=0)
    bridge = _Bridge(process)
    host = _host(process, bridge)
    session = _session_snapshot()
    host._accepted_capture_session = session
    host._reload_admission_policy = lambda **_kwargs: AdmissionPolicy(
        enabled=False)
    host._capture_config = SimpleNamespace(deactivate=lambda *_args: None)
    host._capture_health = SimpleNamespace(observe=lambda **_kwargs: None)
    host._clear_focus_action = lambda: None
    host._set_capture_apply_state = lambda *_args: None
    host._restart_capture_engine = lambda: None
    status = []
    errors = []
    host._set_status = lambda value, *_args: status.append(value)
    host.push_error = lambda *args, **_kwargs: errors.append(args)
    host._retain_terminal_response_owner_cache(process, bridge, 8, 42)

    host._update_status()

    assert host._admission_transition_latch is None
    assert host.engine_process is None
    assert len(errors) == 1
    assert errors[0][0] == 'ENGINE STOPPED'


def test_heartbeat_admits_camera_only_pending_request_before_library_return():
    process = _Process()
    bridge = _Bridge(process)
    host = _host(process, bridge)
    request = _session_snapshot().request
    host._camera_pending_operation = SimpleNamespace(capture_request=request)
    host._reload_admission_policy = lambda **_kwargs: None

    host._update_status()

    assert host._admission_transition_latch is not None
    assert host._admission_transition_latch['original_request'] is request


def test_transition_prefers_retained_request_slots_over_future_desired_request():
    session = _session_snapshot()
    base_request = session.request
    desired = replace(base_request, request_token=20)
    active = replace(base_request, request_token=21)
    pending = replace(base_request, request_token=22)
    camera_dict = replace(base_request, request_token=23)
    camera_object = replace(base_request, request_token=24)

    def make_host():
        process = _Process()
        bridge = _Bridge(process)
        host = _host(process, bridge)
        host._desired_capture_request = desired
        return host

    # An accepted session remains the strongest original-A request source.
    host = make_host()
    host._accepted_capture_session = session
    host._manual_record_request = active
    host._manual_record_state = 'recording'
    host._pending_manual_record_request = pending
    host._camera_pending_operation = {'capture_request': camera_dict}
    host._begin_admission_transition(reason='active manual request')
    assert host._admission_transition_latch[
        'original_request'] is session.request

    # An active manual request must win when no accepted session exists.
    host = make_host()
    host._manual_record_request = active
    host._manual_record_state = 'recording'
    host._pending_manual_record_request = pending
    host._camera_pending_operation = {'capture_request': camera_dict}
    host._begin_admission_transition(reason='active over pending manual')
    assert host._admission_transition_latch[
        'original_request'] is active

    # A pending manual request must win over a pending camera request.
    host = make_host()
    host._pending_manual_record_request = pending
    host._camera_pending_operation = SimpleNamespace(
        capture_request=camera_object)
    host._begin_admission_transition(reason='pending manual over camera')
    assert host._admission_transition_latch[
        'original_request'] is pending

    # Both pending-camera representations must beat only the future desired
    # request when no older manual/session request is retained.
    for operation, expected in (
            ({'capture_request': camera_dict}, camera_dict),
            (SimpleNamespace(capture_request=camera_object), camera_object)):
        host = make_host()
        host._camera_pending_operation = operation
        host._begin_admission_transition(reason='pending camera request')
        assert host._admission_transition_latch[
            'original_request'] is expected


def test_live_recovery_generation_replaces_cache_without_releasing_old_owner():
    process = _Process()
    bridge = _Bridge(process)
    host = _host(process, bridge)
    old = host._retain_terminal_response_owner_cache(process, bridge, 8, 42)
    bridge.capture_generation = 43
    bridge.owner = object()
    new = host._retain_terminal_response_owner_cache(process, bridge, 8, 43)

    assert old is not None and new is not None and new is not old
    assert ('release', old) not in bridge.events
    assert host._terminal_response_owner_cache['native_generation'] == 43


def test_historical_bridge_owner_cannot_read_or_release_after_recovery():
    process = _Process()
    bridge = _Bridge(process)
    old_owner = bridge.owner
    bridge.capture_generation = 43
    bridge.owner = object()
    bridge.owners_by_generation[43] = bridge.owner

    with pytest.raises(RuntimeError):
        bridge.read_terminal_response_snapshot(old_owner, process_exited=True)
    assert bridge.release_terminal_response_owner(old_owner) is False
    assert bridge.read_terminal_response_snapshot(
        bridge.owner, process_exited=True) is bridge.snapshot


def test_normal_stop_invalidates_exact_cache_without_live_unmap():
    process = _Process()
    bridge = _Bridge(process)
    host = _host(process, bridge)
    host._capture_config = SimpleNamespace(cancel_apply=lambda: None)
    host._clear_focus_action = lambda: None
    host._refresh_capture_summary = lambda: None
    host._engine_startup_output = None
    owner = host._retain_terminal_response_owner_cache(
        process, bridge, 8, 42)
    assert owner is bridge.owner
    process.code = 0

    host.stop_engine()

    assert host._terminal_response_owner_cache is None
    assert ('release', owner) not in bridge.events


def test_preaccepted_popen_status_supplies_native_generation_before_shutdown():
    process = _Process()
    bridge = _Bridge(process)
    bridge.capture_generation = 42
    host = _host(process, bridge)
    host._accepted_capture_session = None

    host._begin_admission_transition(reason='pre-Popen warmup')

    latch = host._admission_transition_latch
    assert latch['native_generation'] == 42
    assert bridge.request_calls == 1


def test_mismatched_cached_mapping_cannot_authorize_terminal_close():
    process = _Process(code=0)
    bridge = _Bridge(process)
    host = _host(process, bridge)
    host._accepted_capture_session = SimpleNamespace(
        process_generation=8, native_capture_generation=42)
    host._terminal_response_owner_cache = {
        'process': process, 'bridge': bridge,
        'process_generation': 8, 'native_generation': 41,
        'owner': bridge.owner,
    }

    host._begin_admission_transition(reason='revoked')
    host._pump_admission_transition()

    assert not any(event == ('read', True) for event in bridge.events)
    assert host._admission_transition_latch is not None
    assert host._terminal_response_owner_cache['native_generation'] == 41


def test_retain_retries_while_alive_but_never_after_exit():
    process = _Process()
    bridge = _Bridge(process)
    attempts = iter((None, bridge.owner))

    def delayed_retain(**kwargs):
        bridge.events.append(('retain', kwargs['expected_capture_generation']))
        return next(attempts)

    bridge.retain_terminal_response_owner = delayed_retain
    host = _host(process, bridge)
    host._begin_admission_transition(reason='revoked')
    assert bridge.request_calls == 0
    host._pump_admission_transition()
    assert bridge.request_calls == 1
    process.code = 0
    host._pump_admission_transition()
    assert bridge.events.count(('retain', 42)) == 2
    assert bridge.shutdown_calls == 0


def test_missing_mapping_still_reaches_bounded_escalation(monkeypatch):
    import main

    clock = [0.0]
    monkeypatch.setattr(main.time, 'monotonic', lambda: clock[0])
    process = _Process()
    bridge = _Bridge(process)
    bridge.retain_terminal_response_owner = lambda **_kwargs: None
    host = _host(process, bridge)
    host._begin_admission_transition(reason='revoked')

    clock[0] = 2.1
    host._pump_admission_transition()
    clock[0] = 3.2
    host._pump_admission_transition()

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert host._admission_transition_latch is not None


def test_poll_exception_is_not_exit_proof_but_still_escalates(monkeypatch):
    import main

    clock = [0.0]
    monkeypatch.setattr(main.time, 'monotonic', lambda: clock[0])
    process = _PollErrorProcess()
    bridge = _Bridge(process)
    host = _host(process, bridge)
    host._begin_admission_transition(reason='revoked')

    clock[0] = 2.1
    host._pump_admission_transition()
    clock[0] = 3.2
    host._pump_admission_transition()

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert host._admission_transition_latch is not None


def test_false_native_shutdown_is_not_a_clean_shutdown_proof():
    process = _Process()
    bridge = _Bridge(process, shutdown_accept=False)
    host = _host(process, bridge)

    host._begin_admission_transition(reason='revoked')
    latch = host._admission_transition_latch
    assert latch['native_shutdown_submitted'] is False
    assert latch['shutdown_requested'] is False
    process.code = 0
    host._pump_admission_transition()
    assert latch['native_shutdown_submitted'] is False


def test_remapped_terminal_owner_cannot_be_released_as_a_success():
    process = _Process()
    bridge = _Bridge(process)
    host = _host(process, bridge)
    host._begin_admission_transition(reason='revoked')
    process.code = 0
    bridge.remapped = True
    host._pump_admission_transition()

    assert host._admission_transition_latch is not None
    assert any(event[0] == 'release' for event in bridge.events)


def test_escalation_waits_one_second_after_terminate(monkeypatch):
    import main

    clock = [0.0]
    monkeypatch.setattr(main.time, 'monotonic', lambda: clock[0])
    process = _Process()
    bridge = _Bridge(process)
    host = _host(process, bridge)
    host._begin_admission_transition(reason='revoked')

    clock[0] = 2.1
    host._pump_admission_transition()
    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    clock[0] = 3.0
    host._pump_admission_transition()
    assert process.kill_calls == 0
    clock[0] = 3.11
    host._pump_admission_transition()
    assert process.kill_calls == 1
    assert host._admission_transition_latch is not None


def test_terminal_callback_reentry_releases_a_without_clearing_successor_b():
    process = _Process()
    bridge = _Bridge(process, snapshot=None)
    host = _host(process, bridge)
    host._begin_admission_transition(reason='revoked')
    old_latch = host._admission_transition_latch
    successor_process = _Process()
    successor = {'reason': 'B', 'process': successor_process,
                 'timer': 'B-timer'}

    def install_successor(_snapshot, _latch):
        host.engine_process = successor_process
        host._admission_transition_latch = successor

    host._admission_handle_terminal_snapshot = install_successor
    process.code = 0
    host._pump_admission_transition()

    assert old_latch['terminal_classified'] is True
    assert any(event[0] == 'release' for event in bridge.events)
    assert host._admission_transition_latch is successor
    assert host.engine_process is successor_process


def test_forced_uncertain_owner_skips_generic_shutdown_tails(monkeypatch):
    import main

    clock = [0.0]
    monkeypatch.setattr(main.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(main.time, 'sleep', lambda seconds: clock.__setitem__(
        0, clock[0] + seconds))
    process = _Process()
    bridge = _Bridge(process)
    host = _host(process, bridge)
    host._begin_admission_transition(reason='forced exit')
    host._shutdown_forced = True
    host._perform_full_shutdown()

    assert host._shutdown_complete is True
    assert host._admission_transition_latch is not None
    assert bridge.shutdown_calls == 0


def test_forced_success_uses_non_qt_tail_and_logs_original_receipt(monkeypatch):
    process = _Process()
    bridge = _Bridge(process, snapshot=SimpleNamespace(
        save_response=('error', 'forced'), manual_response=None))
    host = _host(process, bridge)
    host._capture_config = SimpleNamespace(deactivate=lambda *_args: None)
    host._clear_focus_action = lambda: None
    host._set_capture_apply_state = lambda *_args: None
    host._capture_health_snapshot = None
    host._capture_health = SimpleNamespace(observe=lambda **_kwargs: None)
    host._retain_terminal_response_owner_cache(process, bridge, 8, 42)
    host._begin_admission_transition(reason='forced success')
    process.code = 0
    host._shutdown_forced = True
    called = []
    host._set_status = lambda *_args: called.append('status')
    host._shutdown_mark = lambda *_args: called.append('qt-tail')
    host._perform_full_shutdown()

    assert host._shutdown_complete is True
    assert host._admission_transition_latch is None
    assert not called
    assert any('ForcedAdmissionFacts' in message
               for kind, message, _args in host._lifecycle_log.messages
               if kind == 'info')


def test_regular_quit_defers_before_storage_or_native_teardown():
    process = _Process()
    bridge = _Bridge(process)
    host = _host(process, bridge)
    closed = []
    host.library_storage_controller = SimpleNamespace(
        close=lambda: closed.append('storage'))

    host.request_full_exit()

    assert host._shutdown_requested is True
    assert host._shutdown_pending_exit is True
    assert closed == []
    assert bridge.request_calls == 1
    host.request_full_exit()
    assert host._shutdown_pending_exit is True
    assert bridge.request_calls == 1


def test_editor_veto_precedes_admission_shutdown():
    host = _host(_Process(), _Bridge())
    host._active_clip_viewer = SimpleNamespace(
        clip_page=SimpleNamespace(_prepare_detail_boundary=lambda: False))
    errors = []
    host.push_error = lambda *args, **_kwargs: errors.append(args)

    host.request_full_exit()

    assert errors
    assert host._shutdown_requested is False
    assert host._admission_transition_latch is None


def test_shutdown_after_focus_resume_retains_current_mapping_not_stale_session():
    process = _Process()
    bridge = _Bridge(process)
    host = _host(process, bridge)
    original = _session_snapshot()
    host._accepted_capture_session = original
    bridge.capture_generation = original.native_capture_generation + 1

    host._begin_admission_transition(reason='exit after focus resume')
    latch = host._admission_transition_latch

    assert latch['native_generation'] == bridge.capture_generation
    assert latch['terminal_owner'] is not None
    assert bridge.request_calls == 1
    assert latch['session'] is original
    assert original.native_capture_generation != latch['native_generation']
    process.code = 0
    host._pump_admission_transition()
    assert host._admission_transition_latch is None
    assert host.engine_process is None


def test_shutdown_retries_generation_that_was_unavailable_at_first_poll():
    process = _Process()
    bridge = _Bridge(process)
    host = _host(process, bridge)
    bridge.capture_generation = 0
    host._begin_admission_transition(reason='exit during startup')
    assert bridge.request_calls == 0

    bridge.capture_generation = 43
    host._pump_admission_transition()

    assert host._admission_transition_latch['native_generation'] == 43
    assert bridge.request_calls == 1
    process.code = 0
    host._pump_admission_transition()
    assert host._admission_transition_latch is None


def test_full_exit_deadline_quits_without_faking_owner_cleanup(monkeypatch):
    import main

    callbacks = []
    monkeypatch.setattr(main.QTimer, 'singleShot',
                        lambda delay, callback: callbacks.append((delay, callback)))
    quits = []
    monkeypatch.setattr(main, 'QApplication', SimpleNamespace(
        instance=lambda: SimpleNamespace(quit=lambda: quits.append(True))))
    host = _host(_PollErrorProcess())
    host.request_full_exit()
    latch = host._admission_transition_latch
    host.request_full_exit()

    assert [delay for delay, _callback in callbacks] == [4000]
    callbacks[0][1]()

    assert quits == [True]
    assert host._admission_transition_latch is latch
    assert host._shutdown_complete is False
    assert host.bridge.shutdown_calls == 0


def test_activation_during_full_exit_does_not_restore_stopping_window():
    import main

    main.MainWindow.restore_main_window(SimpleNamespace(_shutdown_requested=True))


def test_timed_out_save_requires_exact_session_and_generation():
    import main
    from core.save_state import SaveState, SaveStateMachine

    session = _session_snapshot()
    save_state = SaveStateMachine()
    result = save_state.submit(
        'clip.mp4', 30, 0.0,
        session_snapshot=session, process_generation=8,
        native_capture_generation=42)
    operation = result.operation
    timeout_notice = save_state.on_tick(1.1)
    assert timeout_notice is not None
    assert operation.state is SaveState.TIMED_OUT
    host = _host(None, _Bridge())
    host._save_state = save_state
    host._admission_handle_terminal_snapshot = (
        main.MainWindow._admission_handle_terminal_snapshot.__get__(host))
    latch = {
        'session': session, 'process_generation': 8,
        'native_generation': 42, 'save_state': save_state,
        'save_operation': operation, 'manual_state': 'idle',
        'manual_path': None, 'manual_request': None,
        'manual_session': None, 'pending_manual_path': None,
        'pending_manual_request': None,
    }
    host._admission_transition_latch = latch
    host._on_save_outcome = lambda outcome: setattr(host, '_outcome', outcome)

    host._admission_handle_terminal_snapshot(
        SimpleNamespace(capture_generation=42,
                        save_response=('saved', 'A'), manual_response=None),
        latch)

    assert operation.result_emitted is True
    assert host._outcome.operation is operation


def test_wrong_save_generation_fails_exact_a_once():
    import main
    from core.save_state import SaveState, SaveStateMachine

    session = _session_snapshot()
    save_state = SaveStateMachine()
    result = save_state.submit(
        'clip.mp4', 30, 0.0,
        session_snapshot=session, process_generation=8,
        native_capture_generation=42)
    operation = result.operation
    operation.context['native_capture_generation'] = 43
    timeout_notice = save_state.on_tick(1.1)
    assert timeout_notice is not None
    assert operation.state is SaveState.TIMED_OUT
    host = _host(None, _Bridge())
    host._save_state = save_state
    host._admission_handle_terminal_snapshot = (
        main.MainWindow._admission_handle_terminal_snapshot.__get__(host))
    latch = {
        'session': session, 'process_generation': 8,
        'native_generation': 42, 'save_state': save_state,
        'save_operation': operation, 'manual_state': 'idle',
        'manual_path': None, 'manual_request': None,
        'manual_session': None, 'pending_manual_path': None,
        'pending_manual_request': None,
    }
    host._admission_transition_latch = latch
    host._on_save_outcome = lambda _outcome: None

    host._admission_handle_terminal_snapshot(
        SimpleNamespace(capture_generation=99,
                        save_response=('saved', 'wrong A'),
                        manual_response=None),
        latch)

    assert operation.result_emitted is True
    assert operation.state.name == 'ENGINE_FAILED'


def test_missing_save_response_fails_retained_operation_once():
    import main
    from core.save_state import SaveState, SaveStateMachine

    session = _session_snapshot()
    save_state = SaveStateMachine()
    result = save_state.submit(
        'clip.mp4', 30, 0.0,
        session_snapshot=session, process_generation=8,
        native_capture_generation=42)
    operation = result.operation
    assert save_state.on_tick(1.1) is not None
    host = _host(None, _Bridge())
    host._save_state = save_state
    latch = {
        'session': session, 'process_generation': 8,
        'native_generation': 42, 'save_state': save_state,
        'save_operation': operation, 'manual_state': 'idle',
        'manual_path': None, 'manual_request': None,
        'manual_session': None, 'pending_manual_path': None,
        'pending_manual_request': None,
    }
    host._admission_transition_latch = latch
    outcomes = []
    host._on_save_outcome = outcomes.append
    main.MainWindow._admission_handle_terminal_snapshot.__get__(host)(
        SimpleNamespace(capture_generation=42, save_response=None,
                        manual_response=None), latch)

    assert operation.result_emitted is True
    assert operation.state is SaveState.ENGINE_FAILED
    assert len(outcomes) == 1


def test_save_started_callback_reentry_cannot_fail_successor_or_a():
    import main
    from core.save_state import SaveStateMachine

    session = _session_snapshot()
    save_state = SaveStateMachine()
    result = save_state.submit(
        'clip.mp4', 30, 0.0,
        session_snapshot=session, process_generation=8,
        native_capture_generation=42)
    operation = result.operation
    save_state.on_tick(0.1)
    host = _host(None, _Bridge())
    host._save_state = save_state
    latch = {
        'session': session, 'process_generation': 8,
        'native_generation': 42, 'save_state': save_state,
        'save_operation': operation, 'manual_state': 'idle',
        'manual_path': None, 'manual_request': None,
        'manual_session': None, 'pending_manual_path': None,
        'pending_manual_request': None,
    }
    successor = {'reason': 'B'}
    host._admission_transition_latch = latch

    def install_successor(_outcome):
        host._admission_transition_latch = successor

    host._on_save_outcome = install_successor
    main.MainWindow._admission_handle_terminal_snapshot.__get__(host)(
        SimpleNamespace(capture_generation=42,
                        save_response=('started', 'A'),
                        manual_response=None), latch)

    assert host._admission_transition_latch is successor
    assert operation.state.name == 'ENGINE_ACCEPTED'
    assert operation.result_emitted is False


def test_save_terminal_response_matrix_covers_live_and_timed_out_states():
    import main
    from core.save_state import EngineEvent, OutcomeKind, SaveState, SaveStateMachine

    states = ('requested', 'waiting', 'accepted', 'timed_out')
    responses = (
        ('saved', 'complete'),
        ('error', 'failed'),
        ('started', 'ack-only'),
        None,
        ('unknown', 'unattributed'),
    )
    for state_name in states:
        for response in responses:
            session = _session_snapshot()
            save_state = SaveStateMachine()
            result = save_state.submit(
                'clip.mp4', 30, 0.0,
                session_snapshot=session, process_generation=8,
                native_capture_generation=42)
            operation = result.operation
            if state_name == 'waiting':
                save_state.on_tick(0.1)
                assert operation.state is SaveState.WAITING_FOR_ACK
            elif state_name == 'accepted':
                save_state.on_event(
                    EngineEvent.SAVE_STARTED, 'A', 0.1)
                assert operation.state is SaveState.ENGINE_ACCEPTED
            elif state_name == 'timed_out':
                save_state.on_tick(1.1)
                assert operation.state is SaveState.TIMED_OUT

            host = _host(None, _Bridge())
            host._save_state = save_state
            host._admission_handle_terminal_snapshot = (
                main.MainWindow._admission_handle_terminal_snapshot.__get__(
                    host))
            latch = {
                'session': session, 'process_generation': 8,
                'native_generation': 42, 'save_state': save_state,
                'save_operation': operation, 'manual_state': 'idle',
                'manual_path': None, 'manual_request': None,
                'manual_session': None, 'pending_manual_path': None,
                'pending_manual_request': None,
                'pending_manual_session': None,
            }
            host._admission_transition_latch = latch
            outcomes = []
            host._on_save_outcome = outcomes.append
            host._admission_handle_terminal_snapshot(
                SimpleNamespace(capture_generation=42,
                                save_response=response,
                                manual_response=None), latch)
            assert operation.result_emitted is True
            terminal = [outcome for outcome in outcomes
                        if outcome.operation is operation
                        and outcome.kind in (
                            OutcomeKind.COMPLETED, OutcomeKind.FAILED)]
            assert len(terminal) == 1
            expected_state = (SaveState.ENGINE_COMPLETED
                              if response == ('saved', 'complete')
                              else SaveState.ENGINE_FAILED)
            assert operation.state is expected_state
            if response == ('started', 'ack-only'):
                assert len(outcomes) in (1, 2)
            else:
                assert len(outcomes) == 1


def test_save_session_metadata_rejects_a_and_accepts_matching_b_lineage():
    import dataclasses
    import main
    from core.save_state import SaveStateMachine

    session_a = _session_snapshot()
    save_a = SaveStateMachine()
    operation_a = save_a.submit(
        'clip-a.mp4', 30, 0.0,
        session_snapshot=session_a, process_generation=8,
        native_capture_generation=42).operation
    assert save_a.on_tick(1.1) is not None
    request_b = dataclasses.replace(
        session_a.request, request_token=2, profile_revision=2,
        profile_name='QA profile B')
    session_b_foreign = dataclasses.replace(
        session_a, request=request_b, process_generation=9,
        native_capture_generation=43)
    host = _host(None, _Bridge())
    host._save_state = save_a
    latch_a = {
        'session': session_b_foreign, 'process_generation': 9,
        'native_generation': 43, 'save_state': save_a,
        'save_operation': operation_a, 'manual_state': 'idle',
        'manual_path': None, 'manual_request': None,
        'manual_session': None, 'pending_manual_path': None,
        'pending_manual_request': None, 'pending_manual_session': None,
    }
    host._admission_transition_latch = latch_a
    failed_outcomes = []
    host._on_save_outcome = failed_outcomes.append
    main.MainWindow._admission_handle_terminal_snapshot.__get__(host)(
        SimpleNamespace(capture_generation=43,
                        save_response=('saved', 'wrong A'),
                        manual_response=None), latch_a)
    assert operation_a.result_emitted is True
    assert operation_a.state.name == 'ENGINE_FAILED'
    assert len(failed_outcomes) == 1

    save_b = SaveStateMachine()
    operation_b = save_b.submit(
        'clip-b.mp4', 30, 0.0,
        session_snapshot=session_a, process_generation=8,
        native_capture_generation=42).operation
    assert save_b.on_tick(1.1) is not None
    session_b = dataclasses.replace(
        session_a, request=request_b, process_generation=8,
        native_capture_generation=42)
    host._save_state = save_b
    latch_b = dict(latch_a, session=session_b, process_generation=8,
                   native_generation=42, save_state=save_b,
                   save_operation=operation_b)
    host._admission_transition_latch = latch_b
    accepted_outcomes = []
    host._on_save_outcome = accepted_outcomes.append
    main.MainWindow._admission_handle_terminal_snapshot.__get__(host)(
        SimpleNamespace(capture_generation=42,
                        save_response=('saved', 'matching B metadata'),
                        manual_response=None), latch_b)
    assert operation_b.result_emitted is True
    assert operation_b.state.name == 'ENGINE_COMPLETED'
    assert len(accepted_outcomes) == 1


def test_save_outcome_callback_replaces_current_operation_without_failing_b():
    import main
    from core.save_state import SaveStateMachine

    session_a = _session_snapshot()
    save_a = SaveStateMachine()
    operation_a = save_a.submit(
        'clip-a.mp4', 30, 0.0,
        session_snapshot=session_a, process_generation=8,
        native_capture_generation=42).operation
    save_b = SaveStateMachine()
    operation_b = save_b.submit(
        'clip-b.mp4', 30, 0.0,
        session_snapshot=session_a, process_generation=8,
        native_capture_generation=42).operation
    host = _host(None, _Bridge())
    host._save_state = save_a
    latch = {
        'session': session_a, 'process_generation': 8,
        'native_generation': 42, 'save_state': save_a,
        'save_operation': operation_a, 'manual_state': 'idle',
        'manual_path': None, 'manual_request': None,
        'manual_session': None, 'pending_manual_path': None,
        'pending_manual_request': None, 'pending_manual_session': None,
    }
    host._admission_transition_latch = latch

    def install_current_b(_outcome):
        host._save_state = save_b

    host._on_save_outcome = install_current_b
    main.MainWindow._admission_handle_terminal_snapshot.__get__(host)(
        SimpleNamespace(capture_generation=42,
                        save_response=('saved', 'A'),
                        manual_response=None), latch)

    assert operation_a.result_emitted is True
    assert operation_b.result_emitted is False


def test_started_callback_cannot_fail_successor_in_the_same_save_machine():
    from core.save_state import OutcomeKind, SaveState, SaveStateMachine

    session = _session_snapshot()
    state = SaveStateMachine()
    operation_a = state.submit(
        'clip-a.mp4', 30, 0.0, session_snapshot=session,
        process_generation=8, native_capture_generation=42).operation
    host = _host()
    host._save_state = state
    latch = {
        'session': session, 'process_generation': 8, 'native_generation': 42,
        'save_state': state, 'save_operation': operation_a, 'manual_state': 'idle',
    }
    host._admission_transition_latch = latch
    outcomes = []
    successors = []

    def replace_current(outcome):
        outcomes.append(outcome)
        assert outcome.kind is OutcomeKind.ACCEPTED
        state.cancel_active(0.2, reason='original owner replaced')
        submitted = state.submit(
            'clip-b.mp4', 30, 0.3, session_snapshot=session,
            process_generation=8, native_capture_generation=42)
        assert submitted.accepted
        successors.append(submitted.operation)

    host._on_save_outcome = replace_current
    host._admission_handle_terminal_snapshot(
        SimpleNamespace(capture_generation=42, save_response=('started', 'A'),
                        manual_response=None), latch)

    assert len(outcomes) == 1
    assert len(successors) == 1
    assert state.current is successors[0]
    assert state.current.state is SaveState.REQUESTED
    assert state.current.result_emitted is False
    assert state.current.error_detail == ''


def _manual_latch(session, *, state='recording', exit_proven=True,
                  shutdown=True, exit_code=0):
    path = object()
    request = session.request
    return ({
        'session': session, 'process_generation': 8,
        'native_generation': 42, 'save_state': None,
        'save_operation': None, 'manual_state': state,
        'manual_path': path, 'manual_request': request,
        'manual_session': session,
        'manual_process_generation': 8,
        'manual_native_generation': 42,
        'pending_manual_path': None, 'pending_manual_request': None,
        'pending_manual_session': None,
        'pending_manual_process_generation': None,
        'pending_manual_native_generation': None,
        'native_shutdown_submitted': shutdown,
        'native_exit_proven': exit_proven, 'exit_code': exit_code,
        'terminated': False, 'killed': False, 'error': False,
    }, path, request)


def test_manual_stopped_requires_native_exit_and_snapshot_generation():
    import main

    session = _session_snapshot()
    host = _host()
    latch, path, request = _manual_latch(session, exit_proven=False)
    host._admission_transition_latch = latch
    host._manual_record_state = 'recording'
    host._manual_record_path = path
    host._manual_record_request = request
    host._manual_record_session = session
    host._manual_record_process_generation = 8
    host._manual_record_native_generation = 42
    failed = []
    host._fail_manual_recording = lambda *args, **kwargs: failed.append(
        (args, kwargs))
    main.MainWindow._admission_handle_terminal_snapshot.__get__(host)(
        SimpleNamespace(capture_generation=42,
                        manual_response=('stopped', 'A'),
                        save_response=None), latch)

    assert failed


def test_manual_clean_stopped_requires_exact_owner_and_finalizes_once():
    import main

    session = _session_snapshot()
    host = _host()
    latch, path, request = _manual_latch(session)
    host._admission_transition_latch = latch
    host._manual_record_state = 'stopping'
    host._manual_record_path = path
    host._manual_record_request = request
    host._manual_record_session = session
    host._manual_record_process_generation = 8
    host._manual_record_native_generation = 42
    finalized = []
    host._finalize_manual_recording_file = lambda **kwargs: finalized.append(kwargs)
    main.MainWindow._admission_handle_terminal_snapshot.__get__(host)(
        SimpleNamespace(capture_generation=42,
                        manual_response=('stopped', 'A'),
                        save_response=None), latch)

    assert len(finalized) == 1


def test_manual_stopped_matching_response_accepts_proven_nonzero_exit():
    import main

    session = _session_snapshot()
    host = _host()
    latch, path, request = _manual_latch(
        session, shutdown=False, exit_code=1)
    host._admission_transition_latch = latch
    host._manual_record_state = 'recording'
    host._manual_record_path = path
    host._manual_record_request = request
    host._manual_record_session = session
    host._manual_record_process_generation = 8
    host._manual_record_native_generation = 42
    finalized = []
    host._finalize_manual_recording_file = (
        lambda **kwargs: finalized.append(kwargs))

    main.MainWindow._admission_handle_terminal_snapshot.__get__(host)(
        SimpleNamespace(capture_generation=42,
                        manual_response=('stopped', 'original exit'),
                        save_response=None), latch)

    assert len(finalized) == 1


def test_teardown_receipt_protects_finalizing_a_but_not_successor_b():
    session_a = _session_snapshot()
    session_b = _session_snapshot()
    host = _host()
    path_a = object()
    request_a = session_a.request
    host._manual_record_state = 'finalizing'
    host._manual_record_path = path_a
    host._manual_record_request = request_a
    host._manual_record_session = session_a
    receipt = {
        'manual_path': path_a,
        'manual_request': request_a,
        'manual_session': session_a,
        'pending_manual_path': None,
        'pending_manual_request': None,
        'pending_manual_session': None,
    }

    assert host._admission_receipt_matches_current(receipt, 'manual')

    host._manual_record_path = object()
    host._manual_record_request = session_b.request
    host._manual_record_session = session_b
    assert not host._admission_receipt_matches_current(receipt, 'manual')


def test_manual_saved_alone_or_wrong_generation_keeps_fragments():
    import main

    session = _session_snapshot()
    host = _host()
    latch, path, request = _manual_latch(session)
    host._admission_transition_latch = latch
    host._manual_record_state = 'recording'
    host._manual_record_path = path
    host._manual_record_request = request
    host._manual_record_session = session
    host._manual_record_process_generation = 8
    host._manual_record_native_generation = 42
    failed = []
    finalized = []
    host._fail_manual_recording = lambda *args, **kwargs: failed.append(
        (args, kwargs))
    host._finalize_manual_recording_file = lambda **kwargs: finalized.append(
        kwargs)
    main.MainWindow._admission_handle_terminal_snapshot.__get__(host)(
        SimpleNamespace(capture_generation=42,
                        manual_response=('saved', 'A'),
                        save_response=None), latch)
    assert failed and not finalized

    failed.clear()
    finalized.clear()
    latch['native_generation'] = 43
    main.MainWindow._admission_handle_terminal_snapshot.__get__(host)(
        SimpleNamespace(capture_generation=42,
                        manual_response=('stopped', 'A'),
                        save_response=None), latch)
    assert not failed
    assert not finalized

    latch['native_generation'] = 42
    main.MainWindow._admission_handle_terminal_snapshot.__get__(host)(
        SimpleNamespace(capture_generation=43,
                        manual_response=('stopped', 'A'),
                        save_response=None), latch)
    assert failed
    assert not finalized


def test_manual_preparing_cancels_only_matching_pending_session():
    import main

    session = _session_snapshot()
    host = _host()
    latch, _path, request = _manual_latch(session, state='preparing')
    host._admission_transition_latch = latch
    pending_path = object()
    latch['pending_manual_path'] = pending_path
    latch['pending_manual_request'] = request
    latch['pending_manual_session'] = session
    latch['pending_manual_process_generation'] = 8
    latch['pending_manual_native_generation'] = 42
    host._manual_record_state = 'preparing'
    host._pending_manual_record_path = pending_path
    host._pending_manual_record_request = request
    host._pending_manual_record_session = session
    host._pending_manual_record_process_generation = 8
    host._pending_manual_record_native_generation = 42
    latch['pending_manual_timer'] = host._manual_record_timer
    latch['pending_manual_future'] = None
    main.MainWindow._admission_handle_terminal_snapshot.__get__(host)(
        None, latch)

    assert host._manual_record_state == 'idle'
    assert host._pending_manual_record_path is None


def test_manual_preparing_timer_reentry_preserves_successor_b():
    import main

    session = _session_snapshot()
    host = _host()
    latch, _path, request = _manual_latch(session, state='preparing')
    successor = {'reason': 'B'}
    timer = _Timer()

    def install_successor_on_stop():
        timer.stopped += 1
        host._admission_transition_latch = successor

    timer.stop = install_successor_on_stop
    host._admission_transition_latch = latch
    pending_path = object()
    latch['pending_manual_path'] = pending_path
    latch['pending_manual_request'] = request
    latch['pending_manual_session'] = session
    latch['pending_manual_process_generation'] = 8
    latch['pending_manual_native_generation'] = 42
    latch['pending_manual_timer'] = timer
    latch['pending_manual_future'] = None
    host._manual_record_state = 'preparing'
    host._pending_manual_record_path = pending_path
    host._pending_manual_record_request = request
    host._pending_manual_record_session = session
    host._pending_manual_record_process_generation = 8
    host._pending_manual_record_native_generation = 42
    host._manual_record_timer = timer

    main.MainWindow._admission_handle_terminal_snapshot.__get__(host)(
        None, latch)

    assert host._admission_transition_latch is successor
    assert host._manual_record_state == 'preparing'
    assert host._pending_manual_record_path is pending_path


def test_camera_worker_callback_only_queues_main_continuation():
    host = _host()
    queued = []
    host._ui_call = SimpleNamespace(emit=queued.append)
    provider = _CameraProvider(worker=True)
    owner = object()
    target = {
        'provider': provider, 'owner_token': owner, 'nonce': object(),
        'provider_generation': 4, 'device_index': 1,
    }
    group = {'targets': [target], 'receipts': [],
             'providers': [(provider, owner)],
             'provider': provider, 'owner_token': owner}

    assert host._camera_cancel_owned_contexts(group) is True
    assert len(queued) == 1
    assert provider.calls and provider.calls[0]['owner_token'] is owner
    assert provider.calls[0]['request_token'] is target['nonce']
    assert host._camera_cleanup_complete(group) is True


def test_camera_worker_without_dispatcher_records_only_receipt():
    host = _host()
    provider = _CameraProvider(worker=True)
    owner = object()
    target = {
        'provider': provider, 'owner_token': owner, 'nonce': object(),
        'provider_generation': 4, 'device_index': 1,
    }
    group = {'targets': [target], 'receipts': [],
             'providers': [(provider, owner)],
             'provider': provider, 'owner_token': owner}

    assert host._camera_cancel_owned_contexts(group) is True
    assert host._ui_call is None
    assert host._camera_cleanup_complete(group) is True


def test_camera_false_callback_is_not_cleanup_proof():
    host = _host()
    provider = _CameraProvider(result=False)
    owner = object()
    target = {
        'provider': provider, 'owner_token': owner, 'nonce': object(),
        'provider_generation': 4, 'device_index': 1,
    }
    group = {'targets': [target], 'receipts': [],
             'providers': [(provider, owner)],
             'provider': provider, 'owner_token': owner}

    host._camera_cancel_owned_contexts(group)

    assert host._camera_cleanup_complete(group) is False
    assert host._camera_release_retained_group(group) is False


def test_camera_old_group_cannot_clear_successor_same_provider():
    provider = _CameraProvider()
    owner_a = object()
    owner_b = object()
    target_a = {
        'provider': provider, 'owner_token': owner_a, 'nonce': object(),
        'provider_generation': 4, 'device_index': 1,
    }
    target_b = {
        'provider': provider, 'owner_token': owner_b, 'nonce': object(),
        'provider_generation': 5, 'device_index': 1,
    }
    host = _host()
    host._camera_provider = provider
    host._camera_owner_token = owner_a
    host._camera_active_physical = target_a
    group = host._camera_retained_group()

    def install_b():
        host._camera_provider = provider
        host._camera_owner_token = owner_b
        host._camera_active_physical = target_b

    provider.on_submit = install_b
    host._camera_cancel_owned_contexts(group)
    assert host._camera_cleanup_complete(group) is True
    assert host._camera_release_retained_group(group) is True
    assert host._camera_active_physical is target_b
    assert host._camera_owner_token is owner_b


def test_admission_windows_cache_rejects_stale_generation_and_detaches_rows():
    import main

    host = _host()
    host._capture_admission_windows_generation = 4
    published = []
    host._settings_page_widget = SimpleNamespace(
        set_capture_admission_windows=published.append)
    host._set_capture_admission_windows_cache = (
        main.MainWindow._set_capture_admission_windows_cache.__get__(host))

    assert host._set_capture_admission_windows_cache(
        [{'exe_path': 'A.exe'}], generation=3) is False
    rows = [{'exe_path': 'B.exe'}]
    assert host._set_capture_admission_windows_cache(rows, generation=5)
    rows[0]['exe_path'] = 'mutated'
    assert host._capture_admission_windows_cache == [{'exe_path': 'B.exe'}]
    assert published[-1] == [{'exe_path': 'B.exe'}]


def test_passive_admission_summary_and_prebuilt_window_cache_are_lazy_safe():
    host = _host()
    page_updates = []
    host._settings_page_widget = SimpleNamespace(
        set_capture_admission_status=page_updates.append,
        set_capture_admission_windows=lambda rows: page_updates.append(
            ('windows', rows)))
    host.set_capture_admission_status(
        host._capture_admission_policy_summary(SimpleNamespace(enabled=False)))
    host._set_capture_admission_windows_cache(
        [{'exe_path': 'A.exe'}], generation=1)

    assert host._capture_admission_status_text == 'Capture admission disabled.'
    assert page_updates[0] == 'Capture admission disabled.'
    assert page_updates[-1] == ('windows', [{'exe_path': 'A.exe'}])
    assert host._capture_admission_policy_summary(
        SimpleNamespace(enabled=True)) == 'Approved Windows executables only.'
    assert host._capture_admission_policy_summary(None) == (
        'Capture admission unavailable.')
