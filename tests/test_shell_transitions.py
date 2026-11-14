"""Shell animation interruption, cleanup, and restart-status regressions."""

from types import SimpleNamespace
from functools import partial


from PySide6.QtWidgets import QLabel, QWidget



def _bind_real_restart_request(host, MainWindow):
    """Give the Main restart route a detached request without bypassing it."""
    from core.capture_session import SourceSnapshot, build_resolved_capture_request
    from core.capture_settings import CaptureConfig

    config = CaptureConfig(
        fps=60, buffer_seconds=32, width=1920, height=1080,
        bitrate_kbps=25_000, codec='h264', preset=4, monitor='',
        scaling='stretch', audio_enabled=False)
    request = build_resolved_capture_request(
        request_token=1, purpose='clips',
        source=SourceSnapshot(source_mode='desktop'),
        capture_config=config)
    host._resolve_capture_request = lambda *_args, **_kwargs: request
    host._prepare_next_capture_request = partial(
        MainWindow._prepare_next_capture_request, host)
    host._manual_prepare_owns_capture = partial(
        MainWindow._manual_prepare_owns_capture, host)
    host._capture_resources_busy = partial(
        MainWindow._capture_resources_busy, host)
    host._keyboard_overlay_capture = SimpleNamespace(
        configure=lambda _config: None)
    host._capture_owner_snapshot = partial(
        MainWindow._capture_owner_snapshot, host)
    host._capture_owner_snapshot_is_current = partial(
        MainWindow._capture_owner_snapshot_is_current, host)
    if not hasattr(host, 'settings_manager'):
        settings = SimpleNamespace(settings={})
        settings.get = lambda key, default=None: settings.settings.get(
            key, default)

        def read_capture_admission_policy():
            from core.settings_manager import SettingsManager

            return SettingsManager.read_capture_admission_policy(settings)

        settings.read_capture_admission_policy = read_capture_admission_policy
        host.settings_manager = settings
    for name, descriptor in vars(MainWindow).items():
        if not name.startswith('_admission_'):
            continue
        helper = getattr(MainWindow, name)
        if isinstance(descriptor, staticmethod):
            setattr(host, name, helper)
        elif callable(helper):
            setattr(host, name, partial(helper, host))
    MainWindow._reload_admission_policy(
        host, authoritative=True, trigger_transition=False)


def test_restart_shortcut_status_survives_handshake_and_resets(qtbot, monkeypatch):
    from main import MainWindow

    label = QLabel()
    qtbot.addWidget(label)
    host = SimpleNamespace(
        status_label=label, _manual_record_state='idle',
        _background_ui_paused=False, _capture_settings_applying=False,
    )
    host._set_status = lambda text, style: MainWindow._set_status(host, text, style)
    host._capture_pending_text = lambda: MainWindow._capture_pending_text(host)
    host._set_capture_apply_state = lambda state: MainWindow._set_capture_apply_state(host, state)
    host._restart_capture_engine = lambda: host._set_capture_apply_state(True)
    MainWindow._restart_engine_shortcut(host)
    assert label.text() == 'Restarting engine'
    host._set_status('CONNECTING 1/10', '')
    assert label.text() == 'Restarting engine'
    host._set_status('STARTING CAPTURE', '')
    assert label.text() == 'Restarting engine'
    host._set_capture_apply_state(False)
    host._set_status('CAPTURING', '')
    assert label.text() == 'Capturing'
    host._set_capture_apply_state(True)
    assert label.text() == 'Applying settings'
    host._set_capture_apply_state(False)
    host._manual_record_state = 'recording'
    MainWindow._restart_engine_shortcut(host)
    assert not host._engine_restart_requested


def test_apply_timeout_invalidates_pending_launch(qtbot):
    from main import MainWindow
    from core.capture_settings import ApplyStatus, CaptureConfigTracker
    from PySide6.QtCore import QTimer

    owner = QWidget()
    qtbot.addWidget(owner)
    timer = QTimer(owner)
    timer.setSingleShot(True)
    timer.setInterval(20)
    errors = []
    stopped = []
    host = SimpleNamespace(
        _capture_settings_applying=False, _capture_apply_timer=timer,
        _capture_config=CaptureConfigTracker(), _restart_pending=True,
        _pending_launch_config=object(), _pending_launch_generation=12,
        _pending_launch_token=None,
        _engine_gen=12, _manual_record_state='idle',
        _set_status=lambda *_: None, _capture_pending_text=lambda: 'APPLYING SETTINGS',
        push_error=lambda *args, **kwargs: errors.append(args),
        _restart_capture_engine=lambda: None,
    )
    host._set_capture_apply_state = lambda state: MainWindow._set_capture_apply_state(host, state)

    def stop():
        stopped.append(True)
        host._engine_gen += 1

    host.stop_engine = stop
    host._finish_restart = partial(MainWindow._finish_restart, host)
    timer.timeout.connect(lambda: MainWindow._capture_apply_timed_out(host))
    host._set_capture_apply_state(True)
    qtbot.waitUntil(lambda: not host._capture_settings_applying)
    assert stopped == [] and host._engine_gen == 12
    assert not timer.isActive()
    assert host._pending_launch_config is None
    assert host._pending_launch_generation is None
    assert not host._restart_pending
    assert host._capture_config.status is ApplyStatus.FAILED
    assert errors[0][0] == 'SETTINGS APPLY TIMED OUT'
    # A completed attempt cancels the deadline; later delivery is harmless.
    host._set_capture_apply_state(True)
    host._set_capture_apply_state(False)
    MainWindow._capture_apply_timed_out(host)
    assert stopped == []


def test_timeout_before_queued_restart_cannot_launch_a_stale_engine(
        monkeypatch):
    from main import MainWindow
    from core.capture_settings import CaptureConfigTracker

    callbacks = []
    stopped = []
    errors = []
    host = SimpleNamespace(
        _file_operations_ready=True,
        _manual_record_state='idle',
        _capture_settings_applying=False,
        _capture_apply_timer=SimpleNamespace(
            start=lambda: None, stop=lambda: None),
        _capture_config=CaptureConfigTracker(),
        _restart_pending=False,
        _restart_deferred_for_save=False,
        _engine_gen=7,
        _set_capture_apply_state=lambda applying: setattr(
            host, '_capture_settings_applying', bool(applying)),
        _capture_pending_text=lambda: 'APPLYING SETTINGS',
        _set_status=lambda *_args: None,
        push_error=lambda *args, **kwargs: errors.append((args, kwargs)),
    )

    def stop_engine():
        stopped.append(True)
        host._engine_gen += 1

    host.stop_engine = stop_engine
    host._finish_restart = partial(MainWindow._finish_restart, host)
    _bind_real_restart_request(host, MainWindow)
    host._restart_capture_engine = partial(MainWindow._restart_capture_engine, host)
    monkeypatch.setattr(
        'main.QTimer.singleShot',
        lambda _delay, callback: callbacks.append(callback))

    MainWindow._restart_capture_engine(host)
    assert len(callbacks) == 1
    assert stopped == [True]
    MainWindow._capture_apply_timed_out(host)
    callbacks.pop()()

    assert stopped == [True]
    assert host._restart_pending is False
    assert host._capture_settings_applying is False
    assert errors and errors[0][0][0] == 'SETTINGS APPLY TIMED OUT'


def test_old_restart_callback_cannot_clear_new_restart_intent(
        monkeypatch):
    from main import MainWindow
    from core.capture_settings import CaptureConfigTracker

    callbacks = []
    stopped = []
    bridge_creations = []
    host = SimpleNamespace(
        _file_operations_ready=True,
        _manual_record_state='idle',
        _capture_settings_applying=False,
        _capture_apply_timer=SimpleNamespace(
            start=lambda: None, stop=lambda: None),
        _capture_config=CaptureConfigTracker(),
        _restart_pending=False,
        _restart_deferred_for_save=False,
        _engine_gen=7,
        _set_capture_apply_state=lambda applying: setattr(
            host, '_capture_settings_applying', bool(applying)),
        _capture_pending_text=lambda: 'APPLYING SETTINGS',
        _set_status=lambda *_args: None,
        push_error=lambda *args, **kwargs: None,
    )

    def stop_engine():
        stopped.append(True)
        host._engine_gen += 1

    host.stop_engine = stop_engine
    host._finish_restart = partial(MainWindow._finish_restart, host)
    _bind_real_restart_request(host, MainWindow)
    monkeypatch.setattr(
        'main.QTimer.singleShot',
        lambda _delay, callback: callbacks.append(callback))
    monkeypatch.setattr(
        'main.CaptureBridge',
        lambda: bridge_creations.append(True))

    MainWindow._restart_capture_engine(host)
    assert len(callbacks) == 1
    old_callback = callbacks.pop()
    assert stopped == [True]

    # B is a distinct restart intent and remains applying while stale A fires.
    host._engine_gen += 1
    host._restart_pending = True
    host._capture_settings_applying = True
    old_callback()

    assert bridge_creations == []
    assert stopped == [True]
    assert host._engine_gen == 9
    assert host._restart_pending is True
    assert host._capture_settings_applying is True
