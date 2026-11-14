"""Manual preparation uses the same detached request through native admission."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.capture_session import SourceSnapshot
from test_game_profile_integration import (
    _ALPHA_IDENTITY, _Button, _Process, _Timer, _WINDOW,
    _accepted_tracker, _bind, _owned_host, _patch_resolver, _request, _session,
)


@pytest.fixture
def preparing(tmp_path, monkeypatch, request):
    import main

    profile = getattr(request, 'param', 120)
    reused_recording_quality = profile == 'reused'
    recording_fps = 120 if reused_recording_quality else profile
    source = SourceSnapshot(
        source_mode='window', exe_path=_WINDOW['exe_path'],
        live_identity=_ALPHA_IDENTITY, clip_group='Alpha')
    clips = _request(1, source=source)
    active = clips
    if reused_recording_quality:
        active = replace(
            clips, purpose='recording', capture_config=replace(clips.capture_config, fps=120))
    host, old_session, process, bridge, _tracker = _owned_host(main, active)
    callbacks, launches, stops, library = [], [], [], []
    ready = SimpleNamespace(renderable=False, now=10.0)
    live = dict(_WINDOW)
    _patch_resolver(monkeypatch, main, live)
    monkeypatch.setattr(main, 'time', SimpleNamespace(monotonic=lambda: ready.now))
    monkeypatch.setattr(main, 'is_capture_window_renderable', lambda hwnd: ready.renderable)
    monkeypatch.setattr(main, 'set_theme_style', lambda *_args: None)
    monkeypatch.setattr(main.QTimer, 'singleShot',
                        lambda delay, callback: callbacks.append((delay, callback)))
    monkeypatch.setattr(main, 'recording_directory_from', lambda _settings: tmp_path)
    monkeypatch.setattr(main.shutil, 'disk_usage', lambda _path: SimpleNamespace(free=2**30))
    host._manual_record_path = None
    host._manual_record_timer = _Timer()
    host.record_btn = _Button()
    host._save_state = SimpleNamespace(is_busy=lambda: False)
    host._storage_admission = lambda _path: (True, '')
    host._capture_pending_text = lambda: 'APPLYING SETTINGS'
    host._open_recordings_folder = lambda: None
    host.clip_grid = SimpleNamespace(set_active_output_paths=library.append)
    _bind(host, main, '_toggle_manual_recording', '_start_manual_recording',
          '_continue_prepared_manual_recording', '_submit_manual_recording_start',
          '_fail_manual_recording', '_clear_manual_stop_request',
          '_restore_clip_capture_profile', '_rebind_equivalent_capture_request')

    def prepare(purpose):
        intent = host._begin_capture_intent()
        config = clips.capture_config
        if purpose == 'recording':
            config = replace(config, fps=recording_fps)
        request = replace(
            clips, request_token=intent + 1, purpose=purpose, capture_config=config,
            source_intent_revision=host._source_intent_revision)
        host._capture_request_intents[request.request_token] = intent
        host._desired_capture_request = request
        host._latest_capture_intent_state = 'valid'
        return request

    def stop():
        stops.append(host.engine_process)
        host.engine_process = None
        host._accepted_capture_session = None
        host._engine_gen += 1

    def finish_restart(generation, *, prepared_request):
        launches.append(prepared_request)
        host._engine_process_generation = generation
        host.engine_process = _Process()
        host._capture_config = _accepted_tracker(prepared_request)
        host._accepted_capture_session = _session(prepared_request, process=generation)
        host._restart_pending = False
        host._set_capture_apply_state(False)
        host._consume_capture_intent(prepared_request)

    def run_callback(delay):
        index = next(i for i, (interval, _) in enumerate(callbacks) if interval == delay)
        callbacks.pop(index)[1]()

    host._prepare_next_capture_request = prepare
    host.stop_engine = stop
    host._finish_restart = finish_restart
    host._start_manual_recording()
    assert host._manual_record_state == 'preparing'
    assert host.record_btn.text == 'Cancel' and host.record_btn.enabled
    assert 'Restore the selected game window' in host.statuses[-1]
    return SimpleNamespace(
        main=main, host=host, bridge=bridge, process=process, ready=ready,
        live=live, callbacks=callbacks, run=run_callback, launches=launches,
        stops=stops, library=library, old_session=old_session,
        request=host._pending_manual_record_request, path=host._pending_manual_record_path)


def test_minimized_window_retries_until_restored_and_admits_its_original_request(preparing):
    state = preparing
    state.run(500)
    state.run(500)
    assert state.host.engine_process is state.process
    assert state.host._accepted_capture_session is state.old_session
    assert not state.bridge.recording_calls and not state.stops
    assert state.library == [frozenset({state.path})]
    state.ready.renderable = True
    state.run(500)
    assert state.stops == [state.process]
    state.run(1000)
    state.run(500)
    assert state.launches == [state.request]
    assert state.host._manual_record_request is state.request
    assert state.bridge.recording_calls == [str(state.path)]
    assert state.host._manual_record_state == 'starting'
    assert state.library[-1] == frozenset({state.path})
    state.host._manual_record_state = 'finalizing'
    state.host.capture_card = SimpleNamespace(show_recording_saved=Mock())
    state.host._update_status = lambda: None
    state.path.write_bytes(b'finalized video')
    state.main.MainWindow._complete_manual_recording_file(
        state.host, state.path, publish_ui=True, duration_seconds=2.0,
        expected_identity=state.main.media_identity(state.path))
    assert state.host._manual_record_state == 'idle'
    assert state.library[-1] == frozenset()


@pytest.mark.parametrize('interruption', ['closed', 'replaced', 'source', 'superseded', 'deadline', 'cancel'])
def test_interrupted_preparation_releases_path_and_ignores_stale_callbacks(preparing, interruption):
    state = preparing
    host = state.host
    if interruption == 'closed':
        state.live.clear()
    elif interruption == 'replaced':
        state.live['process_created'] += 1
    elif interruption == 'source':
        host._source_intent_revision += 1
    elif interruption == 'superseded':
        host._prepare_next_capture_request('clips')
    elif interruption == 'deadline':
        state.ready.now = 40.0
    else:
        host._toggle_manual_recording()
    state.run(500)
    assert host._manual_record_state == 'idle'
    assert host.record_btn.text == 'Record' and host.record_btn.enabled
    assert not host._capture_resources_busy()
    assert host._pending_manual_record_request is None
    assert not state.bridge.recording_calls
    assert not state.stops
    assert state.library[-1] == frozenset()
    assert not any(delay == 500 for delay, _ in state.callbacks)
    if interruption != 'cancel':
        assert 'press record' in host.errors[-1][0][1].lower()


def test_cancel_during_restart_delay_rejects_stale_recording_launch(preparing):
    state = preparing
    state.ready.renderable = True
    state.run(500)
    assert state.host._restart_pending
    state.host._toggle_manual_recording()
    assert not state.host._restart_pending
    assert not state.host._capture_settings_applying
    state.run(1000)
    state.run(500)
    assert not state.launches and not state.bridge.recording_calls
    assert state.host._manual_record_state == 'idle'
    assert state.library[-1] == frozenset()


@pytest.mark.parametrize('preparing', ['reused'], indirect=True)
def test_matching_profile_waits_for_exact_resumed_native_generation(preparing):
    state = preparing
    flags = state.main.CaptureHealthFlag
    current_flags = flags.PAUSED
    get_status = state.bridge.get_status
    state.bridge.get_status = lambda: dict(get_status(), capture_health_flags=current_flags)
    state.ready.renderable = True

    state.run(500)
    assert state.host.statuses[-1] == 'Waiting for capture to resume.'
    assert not state.bridge.recording_calls

    state.bridge.capture_generation += 1
    current_flags = flags.ACTIVE
    state.run(500)
    assert state.host._manual_record_state == 'preparing'
    assert state.host._pending_manual_record_request is state.request
    assert not state.bridge.recording_calls
    assert not state.host.errors

    assert state.main.MainWindow._rebind_recovered_session(
        state.host, state.bridge.get_status())
    state.run(500)
    assert state.bridge.recording_calls == [str(state.path)]
    assert state.host._manual_record_session.request is state.request
    assert state.host._manual_record_native_generation == state.bridge.capture_generation
    assert not state.stops


@pytest.mark.parametrize('preparing', [60], indirect=True)
def test_changed_process_is_never_treated_as_native_generation_recovery(preparing):
    state = preparing
    state.ready.renderable = True
    state.bridge.capture_generation += 1
    state.host.engine_process = _Process()
    state.host._engine_process_generation += 1
    state.host._engine_gen += 1
    state.run(500)
    assert state.host._manual_record_state == 'idle'
    assert not state.bridge.recording_calls
    assert 'owner changed' in state.host.errors[-1][0][1]


@pytest.mark.parametrize('preparing', ['reused'], indirect=True)
def test_cancel_restores_replay_quality_after_reusing_recording_profile(preparing):
    state = preparing
    assert state.host._engine_profile == 'recording'
    assert state.host._accepted_capture_session.request.capture_config.fps == 120
    state.host._toggle_manual_recording()
    assert state.host._engine_profile == 'clips'
    assert state.host._desired_capture_request.purpose == 'clips'
    assert state.host._desired_capture_request.capture_config.fps == 60
    assert not state.stops and not state.bridge.recording_calls


def test_cancelled_retry_cannot_start_a_new_preparation_for_the_same_filename(
        preparing, monkeypatch):
    state = preparing
    state.host._toggle_manual_recording()
    monkeypatch.setattr(state.main, 'next_available_output_path', lambda *_args: state.path)
    state.host._start_manual_recording()
    assert state.host._pending_manual_record_path == state.path
    assert state.host._pending_manual_record_request is not state.request
    state.ready.renderable = True
    state.run(500)
    assert not state.stops and not state.bridge.recording_calls
    state.run(500)
    assert state.stops == [state.process]


def test_shutdown_snapshots_real_engine_after_revoking_preparation(preparing, monkeypatch):
    state = preparing
    host = state.host
    retained = []
    host._begin_admission_transition = lambda **kwargs: retained.append(
        (host.engine_process, host._manual_record_state, kwargs['reason']))
    host._defer_full_exit_for_admission = lambda: False
    host._perform_full_shutdown = Mock()
    host._lifecycle_log = SimpleNamespace(info=lambda *_args: None)
    host._tray_icon = None
    host.hide = Mock()
    monkeypatch.setattr(state.main.MainWindow, '_arm_full_exit_deadline', lambda _host: None)
    state.main.MainWindow.request_full_exit(host)
    state.ready.renderable = True
    state.run(500)
    assert retained == [(state.process, 'idle', 'application shutdown')]
    assert host._shutdown_requested
    assert host._engine_profile == 'clips'
    assert not state.launches and not state.bridge.recording_calls
    assert state.library[-1] == frozenset()


def test_library_path_stays_hidden_until_uncertain_writer_exits(preparing):
    state = preparing
    host = state.host
    state.ready.renderable = True
    state.run(500)
    state.run(1000)
    state.run(500)
    host._fail_manual_recording('The recording writer did not acknowledge Stop.')
    assert state.library[-1] == frozenset({state.path})
    state.main.MainWindow._release_closed_recording_paths(host)
    assert state.library[-1] == frozenset({state.path})
    host.engine_process.returncode = 0
    state.main.MainWindow._release_closed_recording_paths(host)
    assert state.library[-1] == frozenset()
