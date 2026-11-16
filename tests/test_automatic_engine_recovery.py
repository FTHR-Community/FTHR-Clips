from types import SimpleNamespace

import pytest


def host():
    return SimpleNamespace(
        _engine_gen=4, _capture_intent_revision=8,
        _source_authority_revision=2, _admission_epoch=1,
        engine_process=None, _manual_record_state='idle',
        _admission_transition_active=lambda: False,
        _restart_capture_engine=lambda: None,
    )


@pytest.fixture
def scheduled(monkeypatch):
    import main
    callbacks = []
    monkeypatch.setattr(main.QTimer, 'singleShot',
                        lambda delay, callback: callbacks.append((delay, callback)))
    return main, callbacks


@pytest.mark.parametrize('kind,limit,delays', [
    ('startup', 1, [1000]), ('crash', 3, [1000, 2000, 4000]),
])
def test_retries_are_bounded_across_engine_generations(scheduled, kind, limit, delays):
    main, callbacks = scheduled
    window = host()
    restarted = []
    window._restart_capture_engine = lambda: restarted.append(window._engine_gen)
    for _ in range(limit):
        assert main.MainWindow._schedule_engine_failure_recovery(window, kind)
        callbacks[-1][1]()
        window._engine_gen += 2
    assert len(restarted) == limit
    assert [delay for delay, _ in callbacks] == delays
    assert not main.MainWindow._schedule_engine_failure_recovery(window, kind)


@pytest.mark.parametrize('field,value', [
    ('_engine_gen', 5),  # manual stop/replacement
    ('_capture_intent_revision', 9), ('_source_authority_revision', 3),
    ('_admission_epoch', 2), ('_shutdown_requested', True),
    ('_shutdown_complete', True), ('engine_process', object()),
    ('_manual_record_state', 'recording'), ('_restart_pending', True),
    ('_capture_settings_applying', True), ('_file_operations_ready', False),
    ('_admission_transition_active', lambda: True),
])
def test_delayed_retry_cannot_override_new_intent_or_busy_owner(scheduled, field, value):
    main, callbacks = scheduled
    window = host()
    restarted = []
    window._restart_capture_engine = lambda: restarted.append(True)
    assert main.MainWindow._schedule_engine_failure_recovery(window, 'crash')
    setattr(window, field, value)
    callbacks[0][1]()
    assert restarted == []
    assert not getattr(window, '_automatic_engine_attempts', {})
    assert window._automatic_engine_retry is None


def test_duplicate_retry_and_stall_do_not_schedule(scheduled):
    main, callbacks = scheduled
    window = host()
    assert not main.MainWindow._schedule_engine_failure_recovery(window, 'stalled')
    assert main.MainWindow._schedule_engine_failure_recovery(window, 'crash')
    assert not main.MainWindow._schedule_engine_failure_recovery(window, 'crash')
    assert len(callbacks) == 1


@pytest.mark.parametrize('native_failure', [False, True])
def test_startup_failure_only_retries_missing_handshake(scheduled, monkeypatch, native_failure):
    main, callbacks = scheduled
    from test_engine_error_recovery import _host, _Bridge, _run_start
    window = _host(main, _Bridge(), startup_text=(
        'FTHR_STARTUP_ERROR: ENCODER_INIT_FAILED: no supported codec'
        if native_failure else ''))
    started, workers, _ = _run_start(monkeypatch, main, window)
    assert started
    workers[0]()
    window._queued_callbacks.pop()()
    assert len(callbacks) == (0 if native_failure else 1)
    if not native_failure:
        restarted = []
        window._restart_capture_engine = lambda: restarted.append(True)
        callbacks[0][1]()
        assert restarted == [True]


def test_crash_handler_schedules_replacement_after_retiring_dead_owner(scheduled):
    main, callbacks = scheduled
    from test_engine_error_recovery import _host, _Bridge, _Process
    from core.capture_health import CaptureHealthMonitor
    bridge = _Bridge()
    window = _host(main, bridge)
    window._capture_health = CaptureHealthMonitor()
    process = _Process()
    process.returncode = 17
    window.engine_process = process
    window._engine_process_generation = 0
    assert main.MainWindow._publish_engine_crash_recovery(window, process, bridge)
    assert window.engine_process is None
    assert len(callbacks) == 1
    restarted = []
    window._restart_capture_engine = lambda: restarted.append(True)
    callbacks[0][1]()
    assert restarted == [True]


@pytest.fixture
def failed_backend(scheduled, monkeypatch):
    main, callbacks = scheduled
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    state = {
        'connected': True,
        'capture_generation': 11,
        'capture_health_flags': int(main.CaptureHealthFlag.RECONFIGURE_REQUIRED),
    }
    window = host()
    window.engine_process = object()
    window.bridge = SimpleNamespace(get_status=lambda: state)
    window.settings_manager = {}
    window._owns_engine_attempt = main.MainWindow._owns_engine_attempt.__get__(window)
    return main, callbacks, window, state


def test_backend_retries_back_off_across_engine_generations(failed_backend):
    main, callbacks, window, status = failed_backend
    restarted = []
    window._restart_capture_engine = lambda: restarted.append(window._engine_gen)
    for delay in (750, 1500, 3000):
        main.MainWindow._maybe_restart_for_display_mode_change(window, status)
        main.MainWindow._maybe_restart_for_display_mode_change(window, status)
        assert len(callbacks) == 1
        interval, retry = callbacks.pop()
        assert interval == delay
        retry()
        window._engine_gen += 1
    main.MainWindow._maybe_restart_for_display_mode_change(window, status)
    assert restarted == [4, 5, 6]
    assert callbacks == []


def test_save_starting_during_backoff_defers_recovery_without_charging_attempt(
        failed_backend):
    main, callbacks, window, status = failed_backend
    restarted = []
    window._restart_capture_engine = lambda: restarted.append(True)
    main.MainWindow._maybe_restart_for_display_mode_change(window, status)

    busy = True
    window._save_state = SimpleNamespace(is_busy=lambda: busy)
    callbacks.pop()[1]()
    main.MainWindow._maybe_restart_for_display_mode_change(window, status)
    assert not restarted
    assert not callbacks
    assert not getattr(window, '_display_mode_restart_times', [])

    busy = False
    main.MainWindow._maybe_restart_for_display_mode_change(window, status)
    callbacks.pop()[1]()
    assert restarted == [True]


@pytest.mark.parametrize('field,value', [
    ('_capture_intent_revision', 9),
    ('_source_intent_revision', 1),
    ('_source_authority_revision', 3),
    ('_admission_epoch', 2),
    ('_capture_settings_timer', SimpleNamespace(isActive=lambda: True)),
])
def test_backend_callback_yields_to_a_new_user_intent(failed_backend, field, value):
    main, callbacks, window, status = failed_backend
    restarted = []
    window._restart_capture_engine = lambda: restarted.append(True)
    main.MainWindow._maybe_restart_for_display_mode_change(window, status)
    setattr(window, field, value)
    callbacks.pop()[1]()
    assert not restarted
    assert not getattr(window, '_display_mode_restart_times', [])


@pytest.mark.parametrize('updated_status', [
    {'capture_health_flags': 0},
    {'capture_generation': 12},
    {'is_recording': True},
])
def test_backend_callback_leaves_a_recovered_or_busy_engine_running(
        failed_backend, updated_status):
    main, callbacks, window, status = failed_backend
    restarted = []
    window._restart_capture_engine = lambda: restarted.append(True)
    main.MainWindow._maybe_restart_for_display_mode_change(window, status)
    status.update(updated_status)
    callbacks.pop()[1]()
    assert not restarted
    assert not getattr(window, '_display_mode_restart_times', [])


@pytest.mark.parametrize('attempts', [[], [1.0, 2.0, 3.0]])
def test_heartbeat_never_reapplies_a_consumed_request(failed_backend, attempts):
    main, callbacks, window, status = failed_backend
    restarted = []
    window._restart_capture_engine = lambda **kwargs: restarted.append(kwargs)
    window._capture_resources_busy = lambda: False
    window._latest_capture_intent_consumed = True
    window._latest_capture_intent_state = 'accepted'
    window._desired_capture_request = object()
    window._accepted_capture_session = SimpleNamespace(
        request=window._desired_capture_request)
    window._display_mode_restart_times = attempts
    main.MainWindow._maybe_restart_for_display_mode_change(window, status)
    assert main.MainWindow._apply_latest_desired_request_if_idle(window) is False
    assert not restarted
    assert len(callbacks) == (0 if len(attempts) == 3 else 1)


def test_failed_backend_yields_to_a_new_queued_capture_request(failed_backend):
    main, callbacks, window, status = failed_backend
    restarted = []
    window._restart_capture_engine = lambda **kwargs: restarted.append(kwargs)
    window._capture_resources_busy = lambda: False
    window._capture_owner_snapshot = lambda *_args: None
    window._desired_capture_request = object()
    window._latest_capture_intent_consumed = False
    window._latest_capture_intent_state = 'queued'
    main.MainWindow._maybe_restart_for_display_mode_change(window, status)
    assert not callbacks
    main.MainWindow._apply_latest_desired_request_if_idle(window)
    assert restarted == [{'_prepared_request': window._desired_capture_request}]


def test_effective_replay_duration_warning_opens_storage_and_clears_on_full_history(
        scheduled):
    main, _ = scheduled
    shown, removed, categories = [], [], []
    window = SimpleNamespace(
        settings_manager={},
        _open_settings_category=categories.append,
    )
    summary = 'Replay history temporarily reduced from 120s to 60s to fit available memory.'
    warnings = main.extract_startup_warnings(
        f'FTHR_STARTUP_WARNING: REPLAY_DURATION_REDUCED: {summary}\n')
    main.MainWindow._update_replay_duration_warning(window, warnings)
    assert not shown
    window.error_bar = SimpleNamespace(
        push=lambda *args, **kwargs: shown.append((args, kwargs)),
        remove_by_key=removed.append)
    main.MainWindow._publish_replay_duration_warning(window)
    (title, detail, level, actions), options = shown.pop()
    assert (title, detail, level) == ('REPLAY DURATION REDUCED', summary, 'warning')
    assert options == {'key': 'replay-duration-reduced', 'dismiss_on_action': False}
    assert actions[0][0] == 'OPEN STORAGE SETTINGS'
    actions[0][1]()
    assert categories == [10]
    main.MainWindow._update_replay_duration_warning(window, ())
    assert removed == ['replay-duration-reduced']
