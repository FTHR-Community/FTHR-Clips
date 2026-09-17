"""Focused PySide6 UI/runtime smoke coverage for AUDIT-013."""

from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Signal, Slot


class _SignalWorker(QObject):
    finished = Signal(str)

    @Slot()
    def run(self) -> None:
        self.finished.emit('worker-ok')


class _CaptureCardStub:
    def __init__(self, _settings_manager=None):
        self.restarted = False
        self.closed = False

    def restart(self) -> None:
        self.restarted = True

    def play_startup(self) -> None:
        pass

    def show_background_capture(self, _source: str) -> None:
        pass

    def hide_background_capture(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _CaptureBridgeStub:
    def get_audio_mappings(self) -> dict:
        return {}


def test_queued_worker_signal_crosses_qthread(qtbot):
    thread = QThread()
    worker = _SignalWorker()
    worker.moveToThread(thread)
    received: list[str] = []
    thread.started.connect(worker.run)
    worker.finished.connect(received.append)
    worker.finished.connect(thread.quit)

    thread.start()
    qtbot.waitUntil(lambda: received == ['worker-ok'], timeout=2000)
    assert thread.wait(2000)


def test_main_window_constructs_with_side_effect_boundaries(
        qtbot, monkeypatch, tmp_path):
    """Build the real widget tree without starting capture/audio/subprocesses."""
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))

    import main

    monkeypatch.setattr(main, 'CaptureCardClient', _CaptureCardStub)
    monkeypatch.setattr(main, 'CaptureBridge', _CaptureBridgeStub)
    monkeypatch.setattr(main.MainWindow, '_setup_hotkeys', lambda _self: None)
    monkeypatch.setattr(main.MainWindow, '_start_mic_recorder',
                        lambda _self: None)
    monkeypatch.setattr(main.MainWindow, 'start_engine', lambda _self: None)
    monkeypatch.setattr(main.MainWindow, 'stop_engine', lambda _self: None)

    window = main.MainWindow()
    qtbot.addWidget(window)

    assert window.centralWidget() is not None
    assert window.windowTitle().startswith('FTHR Clips')
    assert window.styleSheet()
    assert window.settings_manager.config_file.parent == tmp_path / '.fthr'
    assert window.close_btn is not window.power_btn
    assert window.power_btn.toolTip() == 'Shut down FTHR Clips and stop capture'
    window._on_close_to_tray_changed(False)
    assert window.power_btn.isHidden()
    window._on_close_to_tray_changed(True)
    if main.sys.platform == 'win32':
        assert not window.power_btn.isHidden()
    else:
        # Linux does not expose the Windows-only full-shutdown title-bar button.
        assert window.power_btn.isHidden()

    # Exercise the actual title-bar control through the shared cleanup path.
    # Run the deferred callback inline and keep pytest's QApplication alive.
    monkeypatch.setattr(main.QApplication, 'instance', staticmethod(lambda: None))
    monkeypatch.setattr(
        main.QTimer, 'singleShot', staticmethod(lambda _delay, callback: callback()))
    window.power_btn.click()

    assert window._shutdown_requested
    assert window._shutdown_complete
    assert window.capture_card.closed


def test_background_start_defers_main_ui_but_keeps_core_services(
        qtbot, monkeypatch, tmp_path):
    """Windows-login mode must not build the library/editor before restore."""
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))

    import main

    monkeypatch.setattr(main, 'CaptureCardClient', _CaptureCardStub)
    monkeypatch.setattr(main, 'CaptureBridge', _CaptureBridgeStub)
    monkeypatch.setattr(main.MainWindow, '_setup_hotkeys', lambda _self: None)
    monkeypatch.setattr(main.MainWindow, '_start_mic_recorder',
                        lambda _self: None)
    monkeypatch.setattr(main.MainWindow, 'start_engine', lambda _self: None)
    monkeypatch.setattr(main.MainWindow, 'stop_engine', lambda _self: None)

    window = main.MainWindow(background_start=True)
    qtbot.addWidget(window)

    assert window.centralWidget() is None
    assert not hasattr(window, 'clip_grid')
    assert window._background_services_started

    # Keep the test Qt application alive while the app-level cleanup runs.
    monkeypatch.setattr(main.QApplication, 'instance', staticmethod(lambda: None))
    window._perform_full_shutdown()
    window._shutdown_requested = True
    window.close()


def test_share_dialog_constructs_and_emits_selection(qtbot):
    from ui.clip_viewer import ShareModeDialog

    dialog = ShareModeDialog()
    qtbot.addWidget(dialog)
    selected: list[bool] = []
    dialog.mode_selected.connect(selected.append)

    dialog.mode_selected.emit(True)

    assert selected == [True]
    assert dialog.isModal() is False
