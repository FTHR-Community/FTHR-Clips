"""Focus-resume generation recovery at hotkey and teardown boundaries."""

from functools import partial

from test_game_profile_integration import _request, _session, _save_host


def _recovery_host(main, tmp_path):
    original = _session(_request(1, 'original'))
    host, bridge = _save_host(main, original, tmp_path)
    host._refresh_capture_summary = lambda: None
    host._engine_bridge_owner = bridge
    host._rebind_recovered_session = partial(
        main.MainWindow._rebind_recovered_session, host)
    bridge.capture_generation = original.native_capture_generation + 1
    return host, bridge, original


def test_hotkey_reconciles_focus_resume_before_admitting_save(monkeypatch, tmp_path):
    import main

    host, bridge, original = _recovery_host(main, tmp_path)
    process = host.engine_process
    monkeypatch.setattr(main, 'clips_directory_from', lambda _manager: tmp_path)
    monkeypatch.setattr(main, 'emit_event', lambda *_args, **_kwargs: None)

    main.MainWindow._save_clip(host, 30)

    assert bridge.save_calls
    operation = host._save_state.current
    rebound = operation.context['session_snapshot']
    assert rebound is host._accepted_capture_session
    assert rebound.request is original.request
    assert rebound.native_capture_generation == bridge.capture_generation
    assert original.native_capture_generation != bridge.capture_generation
    assert host.engine_process is process


def test_rebind_preserves_pending_save_original_generation(tmp_path):
    import main

    host, bridge, original = _recovery_host(main, tmp_path)
    operation = host._save_state.submit(
        str(tmp_path / 'pending.mp4'), 30, 0,
        session_snapshot=original,
        native_capture_generation=original.native_capture_generation,
        process_generation=original.process_generation).operation

    assert host._rebind_recovered_session(bridge.get_status())

    assert operation.context['session_snapshot'] is original
    assert operation.context['native_capture_generation'] == original.native_capture_generation
    assert host._accepted_capture_session.native_capture_generation == bridge.capture_generation


def test_rebind_rejects_successor_installed_by_retain_callback(tmp_path):
    import main

    host, bridge, original = _recovery_host(main, tmp_path)
    successor = _session(_request(2, 'successor'))
    retain = bridge.retain_terminal_response_owner

    def reenter(**kwargs):
        owner = retain(**kwargs)
        host._accepted_capture_session = successor
        return owner

    bridge.retain_terminal_response_owner = reenter

    assert not host._rebind_recovered_session(bridge.get_status())
    assert host._accepted_capture_session is successor
    assert original.native_capture_generation != bridge.capture_generation
