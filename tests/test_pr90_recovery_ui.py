from types import SimpleNamespace


def _host(main, *, pending=True, pending_generation=7,
          engine_generation=7, process_generation=7):
    return SimpleNamespace(
        _display_mode_restart_pending=pending,
        _display_mode_restart_pending_generation=pending_generation,
        _engine_gen=engine_generation,
        _engine_process_generation=process_generation,
    )


def test_failed_resize_is_suppressed_only_for_active_current_restart():
    import main
    from core.capture_health import CaptureHealthFlag

    status = {
        'capture_health_flags': int(
            CaptureHealthFlag.BACKEND_FAILED
            | CaptureHealthFlag.RECONFIGURE_REQUIRED),
    }
    host = _host(main)

    assert main.MainWindow._display_mode_resize_restart_is_active(
        host, status)


def test_resize_flag_without_pending_restart_keeps_failure_visible():
    import main
    from core.capture_health import CaptureHealthFlag

    status = {'capture_health_flags': int(CaptureHealthFlag.RECONFIGURE_REQUIRED)}
    for host in (
        _host(main, pending=False),
        _host(main, pending_generation=6),
        _host(main, process_generation=6),
    ):
        assert not main.MainWindow._display_mode_resize_restart_is_active(
            host, status)


def test_backend_failure_without_reconfigure_waits_for_owned_recovery():
    import main
    from core.capture_health import CaptureHealthFlag

    status = {'capture_health_flags': int(CaptureHealthFlag.BACKEND_FAILED)}
    assert main.MainWindow._display_mode_resize_restart_is_active(
        _host(main), status)
    assert not main.MainWindow._display_mode_resize_restart_is_active(
        _host(main, pending=False), status)


def test_manual_recording_keeps_resize_failure_visible():
    import main
    from core.capture_health import CaptureHealthFlag

    status = {'capture_health_flags': int(CaptureHealthFlag.RECONFIGURE_REQUIRED)}
    host = _host(main)
    host._manual_record_state = 'recording'
    assert not main.MainWindow._display_mode_resize_restart_is_active(
        host, status)
