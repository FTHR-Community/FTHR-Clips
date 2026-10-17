from __future__ import annotations

import threading
from types import SimpleNamespace


def _stub(main, calls):
    class FakeBridge:
        def shutdown(self):
            calls.append('shutdown')

        def initialize(self):
            calls.append('initialize')
            return False

        def is_connected(self):
            return False

    stub = SimpleNamespace(
        bridge=FakeBridge(),
        engine_process=SimpleNamespace(poll=lambda: None),
        _reconnect_counter=0,
        _capture_settings_applying=False,
        _engine_connect_thread=None,
        _set_status=lambda *a, **k: None,
        # 2.0 status-poll collaborators that this test does not exercise.
        _capture_pending_text=lambda: 'APPLYING SETTINGS',
        _advance_recoverable_errors=lambda **_k: None,
        _schedule_owned_hardware_check=lambda *_a: None,
    )
    stub._owns_engine_attempt = (
        lambda _generation, process, bridge:
        stub.engine_process is process and stub.bridge is bridge)
    stub._connect_worker_active = main.MainWindow._connect_worker_active.__get__(stub)
    stub._update_status = main.MainWindow._update_status.__get__(stub)
    return stub, FakeBridge


def test_status_poll_leaves_bridge_alone_while_connect_worker_runs(monkeypatch):
    import main

    calls = []
    window, fake_bridge = _stub(main, calls)
    monkeypatch.setattr(main, 'CaptureBridge', fake_bridge)

    gate = threading.Event()
    worker = threading.Thread(target=gate.wait, name='fthr-engine-connect', daemon=True)
    worker.start()
    window._engine_connect_thread = worker
    try:
        for _ in range(4):  # covers the "% 3 == 1" reconnect tick
            window._update_status()
        assert calls == []  # the worker owns the bridge
    finally:
        gate.set()
        worker.join(1)

    window._reconnect_counter = 0
    window._update_status()  # worker finished: reconnect resumes
    assert 'initialize' in calls


def test_connect_worker_inactive_without_thread():
    import main

    stub = SimpleNamespace(_engine_connect_thread=None)
    assert main.MainWindow._connect_worker_active.__get__(stub)() is False
