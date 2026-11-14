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
