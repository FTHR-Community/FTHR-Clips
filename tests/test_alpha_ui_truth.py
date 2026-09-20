from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest


ROOT = Path(__file__).resolve().parent.parent
MAIN_SOURCE = (ROOT / 'FTHR_UI' / 'main.py').read_text(encoding='utf-8')


def _method_source(name: str, next_name: str) -> str:
    start = MAIN_SOURCE.index(f'    def {name}')
    end = MAIN_SOURCE.index(f'    def {next_name}', start)
    return MAIN_SOURCE[start:end]


def test_retired_hotkey_path_and_update_surface_are_absent():
    assert '/tmp/fthr_hotkey.sock' not in MAIN_SOURCE
    assert 'You are on the latest version.' not in MAIN_SOURCE
    assert 'Automatic update checks are not available yet.' not in MAIN_SOURCE
    assert 'Version & Updates' not in MAIN_SOURCE


def test_dead_splash_control_is_not_exposed():
    assert 'Enable startup splash screen' not in MAIN_SOURCE


def test_root_favicon_loads_for_window_and_tray():
    pytest.importorskip('PySide6.QtGui')
    from main import _load_icon

    assert not _load_icon('favicon.ico', 32).isNull()


def test_gary_default_uses_the_approved_generated_logo():
    assert 'gary_default.png' not in MAIN_SOURCE
    assert "'assets' / 'fthr_logo.png'" in MAIN_SOURCE


def test_settings_stack_rapid_switch_keeps_only_latest_page(qapp):
    pytest.importorskip('PySide6.QtWidgets')
    from PySide6.QtWidgets import QWidget
    from main import SlidingStackedWidget

    stack = SlidingStackedWidget()
    pages = [QWidget() for _ in range(3)]
    for page in pages:
        stack.addWidget(page)
    stack.resize(800, 500)
    stack.show()

    for index in (1, 2, 0, 2, 1, 2):
        stack.setCurrentIndex(index)
    qapp.processEvents()

    assert stack.currentIndex() == 2
    assert pages[0].isHidden()
    assert pages[1].isHidden()
    assert not pages[2].isHidden()
    source = MAIN_SOURCE[
        MAIN_SOURCE.index('class SlidingStackedWidget'):
        MAIN_SOURCE.index('class _SettingsPage')
    ]
    assert 'QPropertyAnimation' not in source


def test_rapid_settings_tabs_debounce_audio_device_open():
    pytest.importorskip('PySide6.QtCore')
    from main import _SettingsPage

    calls = []
    category_timer = SimpleNamespace(
        start=lambda: calls.append('category-start'),
    )
    timer = SimpleNamespace(
        stop=lambda: calls.append('timer-stop'),
        start=lambda: calls.append('timer-start'),
    )
    meter = SimpleNamespace(
        stop=lambda: calls.append('meter-stop'),
        start=lambda _idx: calls.append('meter-start'),
    )
    fake = SimpleNamespace(
        stack=SimpleNamespace(
            count=lambda: 6,
            setCurrentIndex=lambda idx: calls.append(('page', idx))),
        _audio_preview_timer=timer,
        _category_switch_timer=category_timer,
        _pending_category_index=0,
        _background_ui_paused=False,
        mic_level_meter=meter,
        isVisible=lambda: True,
        _start_encoder_probe=lambda: calls.append('encoder-probe'),
        _stop_loopback=lambda: calls.append('loopback-stop'),
    )

    _SettingsPage._on_category_changed(fake, 2)
    _SettingsPage._on_category_changed(fake, 0)
    assert ('page', 2) not in calls
    assert ('page', 0) not in calls

    _SettingsPage._apply_pending_category(fake)

    assert 'meter-start' not in calls
    assert calls.count('timer-start') == 0
    assert calls[-2:] == ['meter-stop', 'loopback-stop']


def test_rapid_settings_tabs_only_apply_the_latest_page():
    pytest.importorskip('PySide6.QtCore')
    from main import _SettingsPage

    calls = []
    fake = SimpleNamespace(
        stack=SimpleNamespace(
            count=lambda: 6,
            setCurrentIndex=lambda idx: calls.append(('page', idx))),
        _category_switch_timer=SimpleNamespace(
            start=lambda: calls.append('category-start')),
        _audio_preview_timer=SimpleNamespace(
            stop=lambda: calls.append('audio-stop'),
            start=lambda: calls.append('audio-start')),
        _pending_category_index=0,
        _background_ui_paused=False,
        isVisible=lambda: True,
        _start_encoder_probe=lambda: calls.append('encoder-probe'),
        _stop_loopback=lambda: calls.append('loopback-stop'),
    )

    for index in (3, 4, 1, 5):
        _SettingsPage._on_category_changed(fake, index)

    assert not any(isinstance(call, tuple) for call in calls)
    assert fake._pending_category_index == 5

    _SettingsPage._apply_pending_category(fake)

    assert [call for call in calls if isinstance(call, tuple)] == [('page', 5)]
    assert calls.count('encoder-probe') == 1


def test_delayed_audio_preview_does_not_start_after_background_pause():
    pytest.importorskip('PySide6.QtCore')
    from main import _SettingsPage

    calls = []
    fake = SimpleNamespace(
        stack=SimpleNamespace(currentIndex=lambda: 2),
        mic_level_meter=SimpleNamespace(
            set_gain=lambda _gain: calls.append('gain'),
            start=lambda _idx: calls.append('start'),
        ),
        mic_vol_slider=SimpleNamespace(value=lambda: 100),
        _selected_mic_index=lambda: None,
        _background_ui_paused=True,
        isVisible=lambda: True,
    )

    _SettingsPage._start_audio_preview_if_current(fake)

    assert calls == []


@pytest.mark.parametrize(
    ('handler', 'next_handler'),
    [
        ('_on_clip_changed', 'reload_from_settings'),
        ('_on_fps_changed', '_on_res_changed'),
        ('_on_res_changed', '_on_qual_changed'),
        ('_on_qual_changed', '_mark_restart'),
    ],
)
def test_capture_popup_changes_are_explicitly_restart_required(
        handler, next_handler):
    assert 'self._mark_restart()' in _method_source(handler, next_handler)


def test_encoder_apply_uses_process_restart_not_stub_reconfigure():
    pytest.importorskip('PySide6.QtCore')
    from main import MainWindow

    calls: list[str] = []
    fake = SimpleNamespace(
        _set_status=lambda *_args: calls.append('status'),
        _restart_capture_engine=lambda: calls.append('restart'),
    )

    MainWindow._on_encoder_config_changed(fake)

    assert calls == ['status', 'restart']
    body = _method_source('_on_encoder_config_changed', '_finish_restart')
    assert 'set_encoder_config' not in body


def test_kde_source_button_routes_to_portal_picker():
    pytest.importorskip('PySide6.QtCore')
    from main import MainWindow

    calls = []
    fake = SimpleNamespace(
        source_popup=SimpleNamespace(isVisible=lambda: True),
        _uses_kde_portal_source_picker=lambda: True,
        _open_kde_portal_source_picker=lambda: calls.append('portal'),
    )

    MainWindow._toggle_source(fake)

    assert calls == ['portal']


def test_audio_toggle_requests_capture_restart():
    body = _method_source('_on_audio_capture_changed', '_requested_capture_config')
    assert 'self._restart_capture_engine()' in body


def test_game_detection_startup_discards_stale_window_target():
    pytest.importorskip('PySide6.QtCore')
    from main import MainWindow

    settings = SimpleNamespace(
        values={
            'capture_mode': 'window',
            'target_hwnd': 328546,
            'target_window_name': 'Windows Input Experience',
        },
        get=lambda key, default=None: settings.values.get(key, default),
        update=lambda values: settings.values.update(values),
        save_settings=lambda: True,
    )
    fake = SimpleNamespace(settings_manager=settings)

    MainWindow._prepare_startup_capture_source(fake)

    assert settings.values == {
        'capture_mode': 'desktop',
        'target_hwnd': 0,
        'target_window_name': '',
    }


def test_game_detection_can_apply_sources_without_a_deferred_ui():
    pytest.importorskip('PySide6.QtCore')
    from main import MainWindow

    values = {'capture_mode': 'desktop', 'target_hwnd': 0,
              'target_window_name': ''}
    settings = SimpleNamespace(
        get=lambda key, default=None: values.get(key, default),
        update=lambda updates: values.update(updates),
        save_settings=lambda: True,
    )
    fake = SimpleNamespace(settings_manager=settings)

    assert MainWindow._set_capture_window_source(
        fake, {'hwnd': 77, 'display_name': 'Actual Game'})
    assert values['capture_mode'] == 'window'
    assert values['target_hwnd'] == 77
    assert values['target_window_name'] == 'Actual Game'

    assert MainWindow._set_capture_desktop_source(fake)
    assert values['capture_mode'] == 'desktop'
    assert values['target_hwnd'] == 0
    assert values['target_window_name'] == ''


def test_game_detection_restarts_same_window_when_capture_geometry_changes(
        monkeypatch):
    pytest.importorskip('PySide6.QtCore')
    import main

    values = {
        'game_detection_enabled': True,
        'capture_mode': 'window',
        'target_hwnd': 77,
    }
    settings = SimpleNamespace(
        get=lambda key, default=None: values.get(key, default))
    original = main.GameWindow(
        hwnd=77, pid=9, title='Actual Game', exe_name='game.exe',
        right=1920, bottom=1080, monitor_width=1920, monitor_height=1080)
    resized = main.GameWindow(
        hwnd=77, pid=9, title='Actual Game', exe_name='game.exe',
        right=2560, bottom=1440, monitor_width=2560, monitor_height=1440)
    restarts = []
    fake = SimpleNamespace(
        settings_manager=settings,
        _active_game_window=original,
        _manual_record_state='idle',
        _game_window_hwnd=lambda window: int(window.hwnd),
        _restart_capture_engine=lambda **kwargs: restarts.append(kwargs),
    )
    fake._on_game_capture_geometry_changed = (
        main.MainWindow._on_game_capture_geometry_changed.__get__(fake))
    monkeypatch.setattr(main, 'is_capture_window_valid', lambda _hwnd: True)

    main.MainWindow._handle_game_candidate(fake, resized)

    assert fake._active_game_window is resized
    assert restarts == [{'allow_recording_prepare': False}]


def test_game_detection_defers_geometry_restart_until_recording_closes(
        monkeypatch):
    pytest.importorskip('PySide6.QtCore')
    import main

    errors = []
    restarts = []
    callbacks = []
    original = main.GameWindow(
        hwnd=77, pid=9, title='Actual Game', exe_name='game.exe',
        right=1920, bottom=1080, monitor_width=1920, monitor_height=1080)
    resized = main.GameWindow(
        hwnd=77, pid=9, title='Actual Game', exe_name='game.exe',
        right=2560, bottom=1440, monitor_width=2560, monitor_height=1440)
    fake = SimpleNamespace(
        _manual_record_state='recording',
        _pending_game_geometry_restart=False,
        _engine_profile='clips',
        _shutdown_complete=False,
        push_error=lambda *args, **kwargs: errors.append((args, kwargs)),
        _restart_capture_engine=lambda **kwargs: restarts.append(kwargs),
    )
    monkeypatch.setattr(
        main.QTimer, 'singleShot',
        lambda _delay, callback: callbacks.append(callback))

    main.MainWindow._on_game_capture_geometry_changed(
        fake, original, resized)

    assert fake._pending_game_geometry_restart is True
    assert restarts == []
    assert errors and errors[0][0][0] == 'GAME RESOLUTION CHANGED'

    main.MainWindow._restore_clip_capture_profile(fake)

    assert fake._pending_game_geometry_restart is False
    assert len(callbacks) == 1
    callbacks[0]()
    assert restarts == [{}]


def test_active_game_crop_is_part_of_requested_encoder_config(monkeypatch):
    pytest.importorskip('PySide6.QtCore')
    import main

    monkeypatch.setattr(
        main, 'normalize_monitor_device_path', lambda value: value)
    values = {
        'capture_monitor': 'DISPLAY-PATH',
        'target_hwnd': 55,
        'game_detection_custom_games': [{
            'label': 'Crop Game',
            'exe_path': 'C:\\Games\\crop-game.exe',
            'crop_profile': {
                'enabled': True, 'x': 0.1, 'y': 0.2,
                'w': 0.7, 'h': 0.6,
            },
        }],
    }
    settings = SimpleNamespace(
        get=lambda key, default=None: values.get(key, default),
        set=lambda key, value: values.__setitem__(key, value),
        save_settings=lambda: True,
    )
    fake = SimpleNamespace(
        settings_manager=settings,
        _active_game_window=main.GameWindow(
            hwnd=55, pid=9, title='Crop Game',
            exe_path='C:\\Games\\crop-game.exe', exe_name='crop-game.exe'),
        capture_fps=60,
        clip_duration=30,
        capture_width=0,
        capture_height=0,
        capture_bitrate=25_000,
    )

    config = main.MainWindow._requested_capture_config(fake)

    assert config.crop_enabled
    assert (config.crop_x, config.crop_y, config.crop_w, config.crop_h) == (
        0.1, 0.2, 0.7, 0.6)


def test_engine_launch_hard_disables_multiband_and_forwards_encoder_selection():
    body = _method_source('start_engine', '_read_engine_startup_output')
    assert "multiband_arg = '0'" in body
    assert 'launch_config.multiband_enabled' not in body
    assert 'encoder_pref_int' in body


def test_startup_limitations_never_open_modal_dialogs():
    hotkey_warning = _method_source('_warn_input_group', '_warn_no_engine')
    compositor_warning = _method_source(
        '_show_compositor_warning', '_on_game_appeared')

    assert 'QMessageBox' not in hotkey_warning
    assert '.exec()' not in hotkey_warning
    assert 'self.push_error(' in hotkey_warning
    assert 'QMessageBox' not in compositor_warning
    assert '.exec()' not in compositor_warning
    assert 'self.push_error(' in compositor_warning


def test_capture_health_does_not_fill_the_error_bar():
    body = _method_source('_update_status', '_set_status')

    assert 'CAPTURE IS NOT HEALTHY' not in body
    assert 'CAPTURE CONTENT LOOKS UNUSUAL' not in body


def test_bottom_error_bar_is_failure_only_and_can_be_muted():
    import main

    class _Bar:
        def __init__(self):
            self.pushed = []
            self.cleared = False

        def push(self, *args):
            self.pushed.append(args)

        def clear(self):
            self.cleared = True

    values = {'error_notifications_enabled': True}
    settings = SimpleNamespace(
        get=lambda key, default=None: values.get(key, default))
    bar = _Bar()
    fake = SimpleNamespace(error_bar=bar, settings_manager=settings)

    main.MainWindow.push_error(fake, 'GAME RESOLUTION CHANGED', 'routine')
    assert bar.pushed == []

    main.MainWindow.push_error(fake, 'CLIP SAVE FAILED', 'disk is full')
    assert bar.pushed[0][0:2] == (
        '[Error 014] CLIP SAVE FAILED', 'disk is full')

    values['error_notifications_enabled'] = False
    main.MainWindow.push_error(fake, 'UPLOAD FAILED', 'offline')
    assert len(bar.pushed) == 1

    main.MainWindow._on_error_notifications_changed(fake, False)
    assert bar.cleared is True


def test_successful_shortened_replay_has_no_bottom_warning():
    assert 'REPLAY BUFFER WARMING' not in MAIN_SOURCE


def test_manual_recording_success_uses_the_capture_card():
    body = _method_source(
        '_complete_manual_recording_file', '_fail_manual_recording')

    assert 'show_recording_saved' in body
    assert "'MANUAL RECORDING SAVED'" not in body


def test_windows_never_schedules_linux_compositor_warning():
    body = _method_source('_setup_hotkeys', '_on_hotkey_save_clip')
    assert "sys.platform != 'win32'" in body
    assert 'self._show_compositor_warning' in body


def test_named_notification_sounds_have_named_runtime_routes():
    capture_card = (
        ROOT / 'FTHR_UI' / 'ui' / 'capture_card.py'
    ).read_text(encoding='utf-8')
    settings = (
        ROOT / 'FTHR_UI' / 'core' / 'settings_manager.py'
    ).read_text(encoding='utf-8')

    for key in (
            'startup', 'upload_successful', 'upload_failed'):
        assert f"'{key}':" in capture_card
        assert f'sound_volume_{key}' in capture_card
        assert f"'sound_volume_{key}'" in settings
    assert 'show_upload_failed' in MAIN_SOURCE


def test_capture_feedback_is_sent_as_soon_as_save_request_is_registered():
    body = _method_source('_save_clip', '_show_clip_captured_feedback')

    handoff = body.index('if not self.bridge.save_clip(')
    registration = body.index('submit = self._save_state.submit(')
    accepted_guard = body.index('if not submit.accepted:')
    feedback = body.index('self._show_clip_captured_feedback(duration_seconds)')
    polling = body.index('self._save_poll_timer.start()')

    assert handoff < registration < accepted_guard < feedback < polling


def test_capture_feedback_uses_the_requested_clip_stats():
    pytest.importorskip('PySide6.QtCore')
    from main import MainWindow

    notifications = []
    fake = SimpleNamespace(
        _capture_config=SimpleNamespace(
            active=SimpleNamespace(fps=60, width=0, height=0)),
        capture_fps=30,
        capture_width=1920,
        capture_height=1080,
        capture_card=SimpleNamespace(
            show_clip=lambda *args: notifications.append(args)),
    )

    MainWindow._show_clip_captured_feedback(fake, 30)

    assert notifications == [(30, 60, 'SOURCE')]


def test_final_ready_clip_emits_once_without_duplicate_capture_card(
        tmp_path, monkeypatch):
    pytest.importorskip('PySide6.QtCore')
    from main import MainWindow, QTimer

    first_clip = tmp_path / 'ready-1.mp4'
    second_clip = tmp_path / 'ready-2.mp4'
    first_clip.write_bytes(b'complete')
    second_clip.write_bytes(b'complete')
    notifications = []
    emitted = []
    monkeypatch.setattr(QTimer, 'singleShot', lambda *_args: None)
    fake = SimpleNamespace(
        _published_final_clips=set(),
        _clip_readiness=SimpleNamespace(
            can_access=lambda _path: True,
            warnings=lambda _path: (),
        ),
        _set_status=lambda *_args: None,
        _update_status=lambda: None,
        clip_saved=SimpleNamespace(emit=emitted.append),
        capture_card=SimpleNamespace(
            show_clip=lambda *args: notifications.append(args)),
        _capture_config=SimpleNamespace(
            active=SimpleNamespace(fps=60, width=0, height=0)),
        capture_fps=60,
        capture_width=0,
        capture_height=0,
    )

    MainWindow._publish_final_clip(fake, str(first_clip), 5)
    MainWindow._publish_final_clip(fake, str(first_clip), 5)
    MainWindow._publish_final_clip(fake, str(second_clip), 10)

    assert emitted == [str(first_clip), str(second_clip)]
    assert notifications == []


def test_unusable_final_clip_never_emits_success_feedback(tmp_path, monkeypatch):
    pytest.importorskip('PySide6.QtCore')
    from main import MainWindow, QTimer

    notifications = []
    errors = []
    monkeypatch.setattr(QTimer, 'singleShot', lambda *_args: None)
    fake = SimpleNamespace(
        _published_final_clips=set(),
        _clip_readiness=SimpleNamespace(can_access=lambda _path: False),
        _set_status=lambda *_args: None,
        push_error=lambda *args, **kwargs: errors.append((args, kwargs)),
        capture_card=SimpleNamespace(
            show_clip=lambda *args: notifications.append(args)),
    )

    MainWindow._publish_final_clip(fake, str(tmp_path / 'missing.mp4'), 5)

    assert notifications == []
    assert len(errors) == 1


def test_manual_recording_close_only_validates_direct_fragmented_file(
        tmp_path, monkeypatch):
    pytest.importorskip('PySide6.QtCore')
    import main

    recording = tmp_path / 'Recordings' / 'recording.mp4'
    recording.parent.mkdir()
    recording.write_bytes(b'fragmented-video-and-audio')
    probed = []
    completed = []
    monkeypatch.setattr(
        main, 'probe_media', lambda path: probed.append(Path(path)))
    fake = SimpleNamespace(
        _manual_record_path=recording,
        _manual_record_state='stopping',
        _manual_record_timer=SimpleNamespace(stop=lambda: None),
        _normalize_clip_to_cfr=lambda *_: pytest.fail(
            'manual recording must not scan/re-encode the entire file at close'),
        _fail_manual_recording=lambda *args, **kwargs:
            pytest.fail(f'unexpected recording failure: {args!r} {kwargs!r}'),
        _complete_manual_recording_file=lambda *args, **kwargs:
            completed.append((args, kwargs)),
    )

    main.MainWindow._finalize_manual_recording_file(
        fake, publish_ui=False)

    assert probed == [recording]
    assert completed == [((recording,), {'publish_ui': False})]
    assert recording.read_bytes() == b'fragmented-video-and-audio'


@pytest.mark.parametrize('probe_error', [
    RuntimeError('no video stream'), subprocess.TimeoutExpired('ffprobe', 45),
])
def test_invalid_manual_recording_is_kept_for_fragment_recovery(
        tmp_path, monkeypatch, probe_error):
    pytest.importorskip('PySide6.QtCore')
    import main

    recording = tmp_path / 'Recordings' / 'recording.mp4'
    recording.parent.mkdir()
    recording.write_bytes(b'incomplete-last-fragment')
    monkeypatch.setattr(
        main, 'probe_media',
        lambda _path: (_ for _ in ()).throw(probe_error))
    failures = []
    fake = SimpleNamespace(
        _manual_record_path=recording,
        _manual_record_state='stopping',
        _manual_record_timer=SimpleNamespace(stop=lambda: None),
        _fail_manual_recording=lambda *args, **kwargs:
            failures.append((args, kwargs)),
        _complete_manual_recording_file=lambda *_args, **_kwargs:
            pytest.fail('invalid recording was published'),
    )

    main.MainWindow._finalize_manual_recording_file(
        fake, publish_ui=False)

    assert recording.read_bytes() == b'incomplete-last-fragment'
    assert failures == [((str(probe_error),), {
        'keep_recording': True,
        'expected_recording': recording,
    })]


def test_manual_recording_waits_for_native_close_ack_before_publishing(
        monkeypatch):
    pytest.importorskip('PySide6.QtCore')
    import main

    monkeypatch.setattr(main.time, 'monotonic', lambda: 101.0)
    finalized = []
    failures = []
    fake = SimpleNamespace(
        _manual_record_state='stopping',
        _manual_record_requested_at=100.0,
        _MANUAL_RECORDING_STOP_TIMEOUT_SECONDS=30.0,
        bridge=SimpleNamespace(
            peek_manual_recording_response=lambda: None,
            get_status=lambda: {'is_recording': False},
        ),
        _finalize_manual_recording_file=lambda: finalized.append(True),
        _fail_manual_recording=lambda *args, **kwargs:
            failures.append((args, kwargs)),
    )

    main.MainWindow._pump_manual_recording_responses(fake)

    assert finalized == []
    assert failures == []


def test_manual_recording_stopped_ack_publishes_the_closed_file():
    pytest.importorskip('PySide6.QtCore')
    import main

    finalized = []
    consumed = []
    fake = SimpleNamespace(
        _manual_record_state='stopping',
        bridge=SimpleNamespace(
            peek_manual_recording_response=lambda: ('stopped', ''),
            consume_manual_recording_response=lambda: consumed.append(True),
        ),
        _finalize_manual_recording_file=lambda: finalized.append(True),
    )

    main.MainWindow._pump_manual_recording_responses(fake)

    assert consumed == [True]
    assert finalized == [True]
