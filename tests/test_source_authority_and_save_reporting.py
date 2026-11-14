from pathlib import Path
import os
import sys
import threading
import time
from functools import partial
from types import SimpleNamespace

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'FTHR_UI'))


def _settings_adapter(values, *, save=None):
    """Keep source fixtures on one dict and expose the real policy reader."""
    settings = SimpleNamespace()
    settings.settings = values
    settings.values = values
    settings.get = lambda key, default=None: values.get(key, default)
    settings.set = lambda key, value: values.__setitem__(key, value)
    settings.update = lambda updates: values.update(updates)
    settings.save_settings = save or (lambda: True)

    def read_capture_admission_policy():
        from core.settings_manager import SettingsManager

        return SettingsManager.read_capture_admission_policy(settings)

    settings.read_capture_admission_policy = read_capture_admission_policy
    return settings


def _bind_real_admission_helpers(host, main):
    for name, descriptor in vars(main.MainWindow).items():
        if not name.startswith('_admission_'):
            continue
        helper = getattr(main.MainWindow, name)
        if isinstance(descriptor, staticmethod):
            setattr(host, name, helper)
        elif callable(helper):
            setattr(host, name, partial(helper, host))
    return host


def test_settings_source_authority_defaults_to_manual():
    from core.settings_manager import SettingsManager

    manager = object.__new__(SettingsManager)
    manager.config_file = Path('unused-settings.json')
    manager.settings = manager._load_settings()
    assert manager.get('capture_source_authority') == 'manual'


def test_self_exclusion_uses_pid_and_exact_product_executable_identity(monkeypatch):
    import core.game_detector as detector

    monkeypatch.setattr(detector.sys, 'executable',
                        r'C:\Program Files\FTHRClips\FTHRClips.exe')
    monkeypatch.setenv('LOCALAPPDATA', r'C:\Users\Tester\AppData\Local')
    assert detector.is_fthr_application_executable('unrelated.exe', os.getpid())
    assert detector.is_fthr_application_executable(
        r'C:\Program Files\FTHRClips\FTHR Uploader.exe', 4242)
    assert detector.is_fthr_application_executable(
        r'C:\Users\Tester\AppData\Local\FTHR Clips\plugins\uploader\FTHR Uploader.exe',
        4242)
    assert not detector.is_fthr_application_executable(
        r'C:\Users\Tester\AppData\Local\Other\FTHR Uploader.exe', 4242)
    assert not detector.is_fthr_application_executable(
        r'C:\OtherVendor\FTHRClipsSomething.exe', 4242)
    assert not detector.is_fthr_application_executable(
        r'C:\OtherVendor\FTHRClips.exe', 4242)


def test_video_presentation_duration_never_falls_back_to_audio_or_container():
    from core.media_metadata import parse_ffprobe_video_presentation_duration

    payload = {
        'streams': [
            {'codec_type': 'video', 'start_time': '-0.066667',
             'duration': '5.000000'},
            {'codec_type': 'audio', 'duration': '30.000000'},
        ],
        'format': {'duration': '30.000000'},
    }
    assert parse_ffprobe_video_presentation_duration(payload) == pytest.approx(5.0)
    payload['streams'][0].pop('duration')
    assert parse_ffprobe_video_presentation_duration(payload) is None


@pytest.mark.parametrize(('actual', 'expected'), [
    (30.0, 'full'),
    (29.984, 'full'),
    (29.0, 'partial'),
    (0.49, 'rejected'),
    (None, 'unknown'),
    (40.0, 'unknown'),
])
def test_saved_clip_duration_classification_uses_measured_video_only(actual, expected):
    from core.save_outcome import classify_saved_clip_duration

    assert classify_saved_clip_duration(
        requested_seconds=30.0, actual_video_seconds=actual, fps=60.0,
    ) == expected


def test_startup_never_overwrites_explicit_manual_source():
    pytest.importorskip('PySide6.QtCore')
    from main import MainWindow

    values = {
        'capture_mode': 'window', 'target_hwnd': 77,
        'target_window_name': 'Selected Window', 'target_clip_group': 'Selected',
        'capture_source_authority': 'manual',
    }
    settings = _settings_adapter(
        values,
        save=lambda: pytest.fail('manual source must not be rewritten'),
    )
    MainWindow._prepare_startup_capture_source(
        type('Window', (), {'settings_manager': settings})())
    assert values['capture_mode'] == 'window'
    assert values['target_hwnd'] == 77


def test_linux_startup_keeps_legacy_reset_to_desktop(monkeypatch):
    pytest.importorskip('PySide6.QtCore')
    import main
    from main import MainWindow

    monkeypatch.setattr(main.sys, 'platform', 'linux')
    values = {
        'capture_mode': 'window', 'target_hwnd': 77,
        'target_window_name': 'Previously selected',
        'target_clip_group': 'Previously selected',
        'target_window_identity': {'hwnd': 77, 'pid': 8},
        'capture_source_authority': 'manual',
    }
    saves = []
    settings = _settings_adapter(
        values, save=lambda: saves.append(dict(values)))

    MainWindow._prepare_startup_capture_source(
        SimpleNamespace(settings_manager=settings))

    assert values['capture_mode'] == 'desktop'
    assert values['target_hwnd'] == 0
    assert values['target_window_identity'] is None
    assert values['target_clip_group'] == ''
    assert len(saves) == 1


def test_auto_detection_switches_from_a_manual_desktop_source(monkeypatch):
    pytest.importorskip('PySide6.QtCore')
    import main
    from core.game_detector import GameWindow
    from main import MainWindow

    values = {
        'capture_source_authority': 'manual', 'game_detection_enabled': True,
        'game_detection_mode': 'auto', 'capture_mode': 'desktop', 'target_hwnd': 0,
        'game_detection_blacklist': [],
    }
    settings = _settings_adapter(values)
    switched = []
    candidate = GameWindow(hwnd=88, pid=9, title='Game',
                           exe_path=r'C:\Games\g.exe', class_name='GameWindow')
    fake = type('Main', (), {
        'settings_manager': settings,
        '_game_window_hwnd': lambda self, window: window.hwnd,
        '_switch_capture_to_game': lambda self, window: switched.append(window),
    })()
    _bind_real_admission_helpers(fake, main)
    monkeypatch.setattr(main, 'is_capture_window_valid', lambda _hwnd: True)
    monkeypatch.setattr(main, 'get_window_info', lambda _hwnd, own_pid=None: candidate)
    MainWindow._handle_game_candidate(fake, candidate)
    assert switched == [candidate]


def test_manual_selected_game_metadata_can_refresh_without_authorizing_switch(
        monkeypatch):
    pytest.importorskip('PySide6.QtCore')
    import main
    from core.game_detector import GameWindow
    from core.source_authority import window_identity
    from main import MainWindow

    candidate = GameWindow(
        hwnd=88, pid=9, title='Game - New Match',
        exe_path=r'C:\Games\g.exe', exe_name='g.exe', class_name='GameWindow')
    values = {
        'capture_source_authority': 'manual', 'game_detection_enabled': True,
        'game_detection_mode': 'auto', 'capture_mode': 'window',
        'target_hwnd': 88, 'target_window_identity': window_identity(candidate),
        'target_window_name': 'Game - Previous Match', 'target_clip_group': 'g',
        'game_detection_blacklist': [],
    }
    settings = _settings_adapter(values)
    switched = []
    fake = type('Main', (), {
        'settings_manager': settings,
        '_game_window_hwnd': lambda self, window: window.hwnd,
        '_switch_capture_to_game': lambda self, window: switched.append(window),
    })()
    _bind_real_admission_helpers(fake, main)
    monkeypatch.setattr(main, 'is_capture_window_valid', lambda _hwnd: True)
    monkeypatch.setattr(main, 'get_window_info', lambda _hwnd, own_pid=None: candidate)

    MainWindow._handle_game_candidate(fake, candidate)

    assert values['capture_mode'] == 'window'
    assert values['target_hwnd'] == candidate.hwnd
    assert values['target_window_name'] == candidate.display_name
    assert switched == []


def test_linux_game_detection_keeps_legacy_automatic_handoff(monkeypatch):
    pytest.importorskip('PySide6.QtCore')
    import main
    from main import MainWindow

    monkeypatch.setattr(main.sys, 'platform', 'linux')
    settings_values = {
        'game_detection_enabled': True, 'game_detection_mode': 'auto',
        'capture_mode': 'desktop', 'target_hwnd': 0,
    }
    settings = _settings_adapter(settings_values)
    switched = []
    fake = SimpleNamespace(
        settings_manager=settings,
        _game_window_hwnd=lambda window: window['hwnd'],
        _switch_capture_to_game=lambda window: switched.append(window),
    )
    _bind_real_admission_helpers(fake, main)
    candidate = {'hwnd': 41, 'title': 'Linux Game', 'display_name': 'Linux Game',
                 'is_game': True}

    MainWindow._handle_game_candidate(fake, candidate)

    assert switched == [candidate]


def test_explicit_window_choice_persists_windows_process_identity(qapp, monkeypatch):
    pytest.importorskip('PySide6.QtCore')
    import main

    monkeypatch.setattr(main, '_enumerate_capturable_windows', lambda: [])
    live = main.GameWindow(
        hwnd=11, pid=22, title='Picked', exe_path=r'C:\Games\picked.exe',
        exe_name='picked.exe', class_name='PickedWindow', process_created=123456)
    monkeypatch.setattr(main, 'get_window_info',
                        lambda _hwnd, own_pid=None: live)

    settings = _settings_adapter({'capture_mode': 'desktop', 'target_hwnd': 0})
    popup = main.SourcePopup(settings)
    window = {
        'hwnd': 11, 'pid': 22, 'display_name': 'Picked', 'title': 'Picked',
        'exe_path': r'C:\Games\picked.exe', 'exe_name': 'picked.exe',
        'class_name': 'PickedWindow', 'process_created': 123456,
    }

    assert popup.set_capture_window(window)
    identity = settings.get('target_window_identity')
    assert identity['hwnd'] == 11
    assert identity['pid'] == 22
    assert identity['process_created'] == 123456
    assert settings.get('capture_source_authority') == 'manual'
    popup.close()


def test_reactivating_current_desktop_explicitly_claims_manual_authority(
        qapp, monkeypatch):
    pytest.importorskip('PySide6.QtCore')
    import main

    monkeypatch.setattr(main.sys, 'platform', 'win32')
    monkeypatch.setattr(main, '_enumerate_capturable_windows', lambda: [])
    monkeypatch.setattr(main, 'enumerate_windows_monitors', lambda: [])

    settings = _settings_adapter({
        'capture_mode': 'desktop', 'capture_source_authority': 'automatic',
        'target_hwnd': 0,
    })
    popup = main.SourcePopup(settings)
    changes = []
    popup.source_authority_changed.connect(changes.append)

    # QComboBox emits activated when the operator reselects the current item,
    # while currentIndexChanged does not fire for that interaction.
    popup.mode_combo.activated.emit(popup.mode_combo.currentIndex())

    assert settings.get('capture_source_authority') == 'manual'
    assert changes == ['manual']
    popup.close()


def test_reused_manual_hwnd_is_shown_unavailable_not_retargeted(qapp, monkeypatch):
    pytest.importorskip('PySide6.QtCore')
    import main
    from core.source_authority import window_identity

    monkeypatch.setattr(main.sys, 'platform', 'win32')
    monkeypatch.setattr(main, '_enumerate_capturable_windows', lambda: [
        {'hwnd': 11, 'title': 'Reused', 'display_name': 'Reused',
         'is_game': False, 'exe_path': '', 'exe_name': ''},
        {'hwnd': 12, 'title': 'Other', 'display_name': 'Other',
         'is_game': False, 'exe_path': '', 'exe_name': ''},
    ])
    monkeypatch.setattr(main, 'enumerate_windows_monitors', lambda: [])
    replacement = main.GameWindow(
        hwnd=11, pid=303, title='Reused', exe_path=r'C:\Games\new.exe',
        exe_name='new.exe', class_name='GameWindow', process_created=200)
    monkeypatch.setattr(main, 'get_window_info',
                        lambda hwnd, own_pid=None: replacement if hwnd == 11 else None)

    old = main.GameWindow(
        hwnd=11, pid=202, title='Old', exe_path=r'C:\Games\old.exe',
        exe_name='old.exe', class_name='GameWindow', process_created=100)
    settings = _settings_adapter({
        'capture_mode': 'window', 'target_hwnd': 11,
        'target_window_name': 'Old', 'target_clip_group': 'old',
        'target_window_identity': window_identity(old),
    })
    popup = main.SourcePopup(settings)
    assert popup.window_combo.currentText().startswith('Unavailable')
    assert popup.cur_hwnd == 11
    assert settings.get('target_hwnd') == 11
    assert settings.get('target_window_identity')['pid'] == 202
    popup.close()


def test_authorized_windows_auto_switch_and_stale_restart_guard(monkeypatch):
    pytest.importorskip('PySide6.QtCore')
    import main
    from core.game_detector import GameWindow
    from core.source_authority import window_identity
    from main import MainWindow

    monkeypatch.setattr(main.sys, 'platform', 'win32')
    candidate = GameWindow(
        hwnd=31, pid=32, title='Game', exe_path=r'C:\Games\game.exe',
        exe_name='game.exe', class_name='GameWindow', process_created=42)
    values = {
        'capture_source_authority': 'automatic', 'game_detection_enabled': True,
        'game_detection_mode': 'auto', 'capture_mode': 'desktop', 'target_hwnd': 0,
    }
    settings = _settings_adapter(values)
    switched = []
    fake = SimpleNamespace(
        settings_manager=settings,
        _game_window_hwnd=lambda window: window.hwnd,
        _switch_capture_to_game=lambda window: switched.append(window),
    )
    _bind_real_admission_helpers(fake, main)
    monkeypatch.setattr(main, 'is_capture_window_valid', lambda _hwnd: True)
    monkeypatch.setattr(main, 'get_window_info',
                        lambda _hwnd, own_pid=None: candidate)
    MainWindow._handle_game_candidate(fake, candidate)
    assert switched == [candidate]

    # A delayed restart from a superseded source intent must not restart engine.
    values.update({
        'capture_mode': 'window', 'target_hwnd': candidate.hwnd,
        'target_window_identity': window_identity(candidate),
    })
    fake._source_authority_revision = 2
    restarts = []
    fake._restart_capture_engine = lambda: restarts.append(True)
    assert not MainWindow._automatic_switch_still_current(
        fake, candidate.hwnd, window_identity(candidate), revision=1)
    MainWindow._restart_automatic_capture_if_current(
        fake, candidate.hwnd, window_identity(candidate), revision=1)
    assert restarts == []


def test_stale_legacy_dict_candidate_cannot_switch_reused_hwnd(monkeypatch):
    pytest.importorskip('PySide6.QtCore')
    import main
    from main import MainWindow

    monkeypatch.setattr(main.sys, 'platform', 'win32')
    candidate = {
        'hwnd': 31, 'title': 'Old Game', 'exe_path': r'C:\Games\old.exe',
        'exe_name': 'old.exe', 'is_game': True,
    }
    live = main.GameWindow(
        hwnd=31, pid=654, title='New App', exe_path=r'C:\Games\new.exe',
        exe_name='new.exe', class_name='GameWindow', process_created=321)
    values = {
        'capture_source_authority': 'automatic', 'game_detection_enabled': True,
        'game_detection_mode': 'auto', 'capture_mode': 'desktop',
        'target_hwnd': 0,
    }
    settings = _settings_adapter(values)
    switched = []
    fake = SimpleNamespace(
        settings_manager=settings,
        _game_window_hwnd=lambda window: window['hwnd'],
        _switch_capture_to_game=lambda window: switched.append(window),
    )
    _bind_real_admission_helpers(fake, main)
    monkeypatch.setattr(main, 'is_capture_window_valid', lambda _hwnd: True)
    monkeypatch.setattr(main, 'get_window_info',
                        lambda _hwnd, own_pid=None: live)

    MainWindow._handle_game_candidate(fake, candidate)

    assert switched == []
    assert values['capture_mode'] == 'desktop'


def test_windows_game_geometry_update_does_not_bypass_native_reconfigure(
        monkeypatch):
    pytest.importorskip('PySide6.QtCore')
    import main
    from core.source_authority import window_identity
    from main import MainWindow

    monkeypatch.setattr(main.sys, 'platform', 'win32')
    previous = main.GameWindow(
        hwnd=31, pid=32, title='Game', exe_path=r'C:\Games\game.exe',
        exe_name='game.exe', class_name='GameWindow', process_created=42,
        right=1920, bottom=1080, monitor_width=1920, monitor_height=1080)
    current = main.GameWindow(
        hwnd=31, pid=32, title='Game', exe_path=r'C:\Games\game.exe',
        exe_name='game.exe', class_name='GameWindow', process_created=42,
        right=2560, bottom=1440, monitor_width=2560, monitor_height=1440)
    values = {
        'capture_source_authority': 'automatic', 'game_detection_enabled': True,
        'game_detection_mode': 'auto', 'capture_mode': 'window',
        'target_hwnd': 31, 'target_window_identity': window_identity(previous),
        'game_detection_blacklist': [], 'target_clip_group': '',
    }
    settings = _settings_adapter(values)
    geometry_restarts = []
    fake = SimpleNamespace(
        settings_manager=settings, _active_game_window=previous,
        engine_process=SimpleNamespace(poll=lambda: None),
        _game_window_hwnd=lambda window: window.hwnd,
        _on_game_capture_geometry_changed=lambda *args: geometry_restarts.append(args),
    )
    _bind_real_admission_helpers(fake, main)
    monkeypatch.setattr(main, 'is_capture_window_valid', lambda _hwnd: True)
    monkeypatch.setattr(main, 'get_window_info',
                        lambda _hwnd, own_pid=None: current)

    MainWindow._handle_game_candidate(fake, current)

    assert fake._active_game_window is current
    assert geometry_restarts == []


def test_manual_source_intent_invalidates_pending_game_prompt():
    pytest.importorskip('PySide6.QtCore')
    from main import MainWindow

    values = {'capture_source_authority': 'automatic'}
    settings = _settings_adapter(values)
    pending = object()
    stopped = []
    fake = SimpleNamespace(
        settings_manager=settings, _source_authority_revision=4,
        _pending_game_window=pending, _pending_game_candidate_revision=4,
        _game_dismiss_timer=SimpleNamespace(stop=lambda: stopped.append(True)),
        _auto_capture_hwnd=10, _auto_capture_identity={'hwnd': 10},
        _active_game_hwnd=10, _active_game_window=pending)

    MainWindow._on_manual_capture_source_intent(fake)

    assert values['capture_source_authority'] == 'manual'
    assert fake._source_authority_revision == 5
    assert fake._pending_game_window is None
    assert fake._auto_capture_hwnd == 0
    assert fake._auto_capture_identity is None
    assert fake._active_game_hwnd is None
    assert stopped == [True]


def test_display_reconfigure_callback_is_invalidated_by_source_intent(monkeypatch):
    pytest.importorskip('PySide6.QtCore')
    import main
    from main import MainWindow

    monkeypatch.setattr(main.sys, 'platform', 'win32')
    callbacks = []
    monkeypatch.setattr(main.QTimer, 'singleShot',
                        lambda _delay, callback: callbacks.append(callback))
    flags = int(main.CaptureHealthFlag.RECONFIGURE_REQUIRED)
    bridge = SimpleNamespace(get_status=lambda: {'capture_health_flags': flags})
    values = {
        'capture_mode': 'window', 'target_hwnd': 44,
        'target_window_identity': {'hwnd': 44, 'pid': 4},
        'capture_source_authority': 'automatic',
    }
    settings = _settings_adapter(values)
    restarted = []
    fake = SimpleNamespace(
        _engine_gen=7, engine_process=object(), bridge=bridge,
        settings_manager=settings, _source_authority_revision=3,
        _manual_record_state='idle', _restart_pending=False,
        _capture_settings_applying=False,
        _restart_capture_engine=lambda: restarted.append(True),
    )
    _bind_real_admission_helpers(fake, main)
    fake._owns_engine_attempt = MainWindow._owns_engine_attempt.__get__(fake)

    MainWindow._maybe_restart_for_display_mode_change(fake, {
        'connected': True, 'capture_health_flags': flags})
    assert len(callbacks) == 1
    fake._source_authority_revision += 1
    callbacks.pop()()
    assert restarted == []


def test_display_reconfigure_budget_needs_healthy_frame_progress_to_reset(
        monkeypatch):
    pytest.importorskip('PySide6.QtCore')
    import main
    from main import MainWindow

    monkeypatch.setattr(main.sys, 'platform', 'win32')
    now = [100.0]
    monkeypatch.setattr(main.time, 'monotonic', lambda: now[0])
    callbacks = []
    monkeypatch.setattr(main.QTimer, 'singleShot',
                        lambda _delay, callback: callbacks.append(callback))
    flags = int(main.CaptureHealthFlag.RECONFIGURE_REQUIRED)
    bridge = SimpleNamespace(get_status=lambda: {'capture_health_flags': flags})
    settings = _settings_adapter({})
    fake = SimpleNamespace(
        _engine_gen=1, engine_process=object(), bridge=bridge,
        settings_manager=settings, _source_authority_revision=0,
        _manual_record_state='idle', _restart_pending=False,
        _capture_settings_applying=False, _display_mode_restart_times=[],
        _capture_health=SimpleNamespace(_healthy_since=None),
        _capture_health_snapshot=SimpleNamespace(
            state=main.CaptureHealthState.INITIALIZING),
        _restart_capture_engine=lambda: None,
    )
    _bind_real_admission_helpers(fake, main)
    fake._owns_engine_attempt = MainWindow._owns_engine_attempt.__get__(fake)

    for generation in (1, 2, 3):
        fake._engine_gen = generation
        MainWindow._maybe_restart_for_display_mode_change(fake, {
            'connected': True, 'capture_health_flags': flags})
        callbacks.pop()()
        now[0] += 45  # a long delay alone cannot clear the incident budget
    fake._engine_gen = 4
    MainWindow._maybe_restart_for_display_mode_change(fake, {
        'connected': True, 'capture_health_flags': flags})
    assert callbacks == []
    assert len(fake._display_mode_restart_times) == 3

    fake._engine_gen = 5
    fake._capture_health_snapshot.state = main.CaptureHealthState.HEALTHY
    fake._capture_health._fresh_duration_s = 30.0
    fake._capture_health._healthy_since = now[0] - 30
    MainWindow._maybe_restart_for_display_mode_change(fake, {
        'connected': True, 'capture_health_flags': flags})
    assert fake._display_mode_restart_times == []
    assert len(callbacks) == 1
    # Health polling observes the failure before the stability callback runs.
    fake._capture_health_snapshot.state = main.CaptureHealthState.FAILED
    fake._capture_health._fresh_duration_s = 0.0
    fake._capture_health._healthy_since = None
    callbacks.pop()()
    assert len(fake._display_mode_restart_times) == 1


def test_display_reconfigure_waits_for_live_window_geometry_to_settle(monkeypatch):
    pytest.importorskip('PySide6.QtCore')
    import main
    from core.source_authority import window_identity
    from main import MainWindow

    monkeypatch.setattr(main.sys, 'platform', 'win32')
    callbacks = []
    monkeypatch.setattr(main.QTimer, 'singleShot',
                        lambda _delay, callback: callbacks.append(callback))
    original = main.GameWindow(
        hwnd=55, pid=66, title='Game', exe_path=r'C:\Games\game.exe', exe_name='game.exe',
        class_name='GameWindow', process_created=77,
        right=1920, bottom=1080, monitor_width=1920, monitor_height=1080)
    resized = main.GameWindow(
        hwnd=55, pid=66, title='Game', exe_path=r'C:\Games\game.exe', exe_name='game.exe',
        class_name='GameWindow', process_created=77,
        right=2560, bottom=1440, monitor_width=2560, monitor_height=1440)
    probes = iter((original, resized, resized, resized))
    monkeypatch.setattr(main, 'get_window_info',
                        lambda *_args, **_kwargs: next(probes))
    flags = int(main.CaptureHealthFlag.RECONFIGURE_REQUIRED)
    bridge = SimpleNamespace(get_status=lambda: {'capture_health_flags': flags})
    identity = window_identity(original)
    values = {
        'capture_mode': 'window', 'target_hwnd': 55,
        'target_window_identity': identity,
        'capture_source_authority': 'automatic',
    }
    settings = _settings_adapter(values)
    restarted = []
    fake = SimpleNamespace(
        _engine_gen=9, engine_process=object(), bridge=bridge,
        settings_manager=settings, _source_authority_revision=2,
        _manual_record_state='idle', _restart_pending=False,
        _capture_settings_applying=False, _display_mode_restart_times=[],
        _restart_capture_engine=lambda: restarted.append(True),
    )
    _bind_real_admission_helpers(fake, main)
    fake._owns_engine_attempt = MainWindow._owns_engine_attempt.__get__(fake)

    MainWindow._maybe_restart_for_display_mode_change(fake, {
        'connected': True, 'capture_health_flags': flags})
    assert len(callbacks) == 1
    assert fake._display_mode_restart_times == []
    callbacks.pop()()
    assert restarted == []
    assert fake._display_mode_restart_times == []
    assert fake._display_mode_restart_generation is None

    # A game can open a small startup window before switching to its full
    # render size. Reobserve the same failed generation and restart once its
    # newest geometry has remained stable for the next debounce interval.
    MainWindow._maybe_restart_for_display_mode_change(fake, {
        'connected': True, 'capture_health_flags': flags})
    assert len(callbacks) == 1
    callbacks.pop()()
    assert restarted == [True]
    assert len(fake._display_mode_restart_times) == 1


def test_final_publication_waits_for_measured_video_duration_off_ui_thread(
        tmp_path, monkeypatch):
    pytest.importorskip('PySide6.QtCore')
    import main
    from main import MainWindow

    events = []
    probe_threads = []
    finished = threading.Event()
    ui_thread_id = threading.get_ident()
    durations = {
        'full.mp4': 60.0, 'partial.mp4': 43.917, 'rejected.mp4': 0.4,
    }
    queued_ui = []
    notifications = []
    cards = []
    statuses = []

    def probe(path):
        probe_threads.append(threading.get_ident())
        return durations[Path(path).name]

    monkeypatch.setattr(main, 'probe_video_presentation_duration', probe)
    monkeypatch.setattr(main, 'emit_event',
                        lambda *args, **kwargs: events.append((args, kwargs)))
    monkeypatch.setattr(main, 'third_party_keyboard_settings',
                        lambda _settings: {'enabled': False})
    monkeypatch.setattr(main, 'select_post_route', lambda **_kwargs: ('simple', False))
    monkeypatch.setattr(main.QTimer, 'singleShot', lambda *_args: None)

    class _Dispatcher:
        def emit(self, callback):
            queued_ui.append(callback)
            finished.set()

    class _Signal:
        def emit(self, *args):
            notifications.append(args)

    fake = SimpleNamespace(
        _save_duration_reports={},
        _save_duration_probe_inflight=set(),
        _published_final_clips=set(),
        _clip_diagnostic_started_by_path={},
        _capture_config=SimpleNamespace(active=SimpleNamespace(fps=60)),
        _ui_call=_Dispatcher(),
        _clip_readiness=SimpleNamespace(can_access=lambda _path: True),
        capture_card=SimpleNamespace(
            show_clip=lambda *args: cards.append(args),
            show_error=lambda: statuses.append(('error', None))),
        clip_saved=_Signal(),
        _set_status=lambda *args: statuses.append(('status', args[0])),
        _update_status=lambda: None,
        _clip_readiness_warnings=(),
    )
    fake._clip_readiness.warnings = lambda _path: ()
    fake._begin_saved_clip_duration_probe = (
        lambda path, key: MainWindow._begin_saved_clip_duration_probe(fake, path, key))
    fake._publish_final_clip = (
        lambda path, duration: MainWindow._publish_final_clip(fake, path, duration))

    for name in ('full.mp4', 'partial.mp4', 'rejected.mp4'):
        path = tmp_path / name
        path.write_bytes(b'video')
        op = SimpleNamespace(
            output_path=str(path), op_id=name, duration_seconds=60,
            context={}, requested_at=time.monotonic())
        MainWindow._register_saved_clip_duration_report(fake, op, path)
        published_before_probe = len(notifications)
        MainWindow._publish_final_clip(fake, str(path), 60)
        assert finished.wait(2)
        assert len(notifications) == published_before_probe
        assert cards == []
        queued_ui.pop(0)()
        # Duplicate native/finalizer callbacks never repeat a card.
        MainWindow._publish_final_clip(fake, str(path), 60)
        finished.clear()

    classified = [kwargs['classification'] for args, kwargs in events
                  if args[1] == 'save_duration_classified']
    assert classified == ['full', 'partial', 'rejected']
    assert len(probe_threads) == 3
    assert all(thread_id != ui_thread_id for thread_id in probe_threads)
    assert [kwargs['requested_duration_seconds'] for args, kwargs in events
            if args[1] == 'save_finalized'] == [60, 60, 60]
    assert len(notifications) == 2
    assert cards == []
    assert ('status', 'PARTIAL SAVED 43.9/60s') in statuses
    assert statuses.count(('error', None)) == 1
    assert sum(args[1] == 'save_rejected' for args, _ in events) == 1


def test_duration_probe_capacity_is_bounded_and_publishes_unknown(monkeypatch):
    pytest.importorskip('PySide6.QtCore')
    import main
    from main import MainWindow

    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    monkeypatch.setattr(main, '_SAVE_DURATION_PROBE_SLOTS', slots)
    monkeypatch.setattr(main, 'probe_video_presentation_duration',
                        lambda _path: pytest.fail('saturated probe must not start'))
    events, callbacks, published, statuses = [], [], [], []
    dispatched = threading.Event()

    class _Dispatcher:
        def emit(self, callback):
            callbacks.append(callback)
            dispatched.set()

    reports = {'clip': {
        'operation_id': 'op-1', 'requested_seconds': 30, 'fps': 60,
        'duration_classification': None, 'actual_video_seconds': None,
        'probe_complete': False,
    }}
    fake = SimpleNamespace(
        _save_duration_reports=reports, _save_duration_probe_inflight=set(),
        _ui_call=_Dispatcher(),
        capture_card=SimpleNamespace(
            show_prompt=lambda value: statuses.append(value),
            show_error=lambda: statuses.append('error')),
        _set_status=lambda value, _style: statuses.append(value),
    )
    monkeypatch.setattr(main, 'emit_event',
                        lambda *args, **kwargs: events.append((args, kwargs)))
    monkeypatch.setattr(MainWindow, '_publish_final_clip',
                        lambda _self, path, duration: published.append((path, duration)))

    MainWindow._begin_saved_clip_duration_probe(fake, 'clip.mp4', 'clip')

    assert dispatched.wait(1)
    callbacks.pop()()
    assert reports['clip']['probe_complete'] is True
    assert reports['clip']['duration_classification'] == 'unknown'
    assert reports['clip']['duration_probe_reason'] == 'duration_probe_capacity'
    assert published == [('clip.mp4', 30)]
    assert any(kwargs.get('reason') == 'duration_probe_capacity'
               for args, kwargs in events if args[1] == 'save_duration_classified')
    assert statuses == []  # Unverified duration must not create another popup.
    slots.release()


def test_duration_probe_exception_releases_concurrency_slot(monkeypatch):
    pytest.importorskip('PySide6.QtCore')
    import main
    from main import MainWindow

    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(main, '_SAVE_DURATION_PROBE_SLOTS', slots)
    worker_finished = threading.Event()
    callbacks, events = [], []

    def fail_probe(_path):
        raise OSError('ffprobe timeout')

    class _Dispatcher:
        def emit(self, callback):
            callbacks.append(callback)
            worker_finished.set()

    fake = SimpleNamespace(
        _save_duration_reports={'clip': {
            'operation_id': 'op-2', 'requested_seconds': 30, 'fps': 60,
            'duration_classification': None, 'actual_video_seconds': None,
            'probe_complete': False,
        }},
        _save_duration_probe_inflight=set(), _ui_call=_Dispatcher(),
        capture_card=SimpleNamespace(show_prompt=lambda _value: None,
                                     show_error=lambda: None),
        _set_status=lambda *_args: None,
    )
    monkeypatch.setattr(main, 'probe_video_presentation_duration', fail_probe)
    monkeypatch.setattr(main, 'emit_event',
                        lambda *args, **kwargs: events.append((args, kwargs)))
    monkeypatch.setattr(MainWindow, '_publish_final_clip',
                        lambda *_args: None)

    MainWindow._begin_saved_clip_duration_probe(fake, 'clip.mp4', 'clip')

    assert worker_finished.wait(2)
    callbacks.pop()()
    report = fake._save_duration_reports['clip']
    assert report['probe_complete'] is True
    assert report['duration_classification'] == 'unknown'
    assert report['duration_probe_reason'] == 'duration_probe_failed'
    assert slots.acquire(blocking=False)
    slots.release()
