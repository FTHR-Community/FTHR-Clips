from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json
import os
import subprocess
import threading

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


def test_root_favicon_loads_for_window_and_tray(qapp):
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
            setCurrentIndex=lambda idx, **_kwargs: calls.append(('page', idx))),
        _clip_subcategories=_SettingsPage._clip_subcategories,
        _audio_preview_timer=timer,
        settings_search=SimpleNamespace(clear=lambda: None, text=lambda: ''),
        _clear_search_highlight=lambda: None,
        _sync_settings_navigation=lambda _index: None,
        _category_switch_timer=category_timer,
        _pending_category_index=0,
        _ensure_category=lambda _index: None,
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
            setCurrentIndex=lambda idx, **_kwargs: calls.append(('page', idx))),
        _clip_subcategories=_SettingsPage._clip_subcategories,
        _category_switch_timer=SimpleNamespace(
            start=lambda: calls.append('category-start')),
        _audio_preview_timer=SimpleNamespace(
            stop=lambda: calls.append('audio-stop'),
            start=lambda: calls.append('audio-start')),
        settings_search=SimpleNamespace(clear=lambda: None, text=lambda: ''),
        _clear_search_highlight=lambda: None,
        _sync_settings_navigation=lambda _index: None,
        _pending_category_index=0,
        _ensure_category=lambda _index: None,
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
        _queue_capture_settings_restart=lambda: calls.append('scheduled'),
    )

    MainWindow._on_encoder_config_changed(fake)

    assert calls == ['scheduled']
    body = _method_source('_on_encoder_config_changed', '_finish_restart')
    assert 'set_encoder_config' not in body


def _manual_session_a(main, *, token=1):
    """Build the immutable accepted request owned by a manual-record host."""
    from core.capture_settings import CaptureConfig

    config = CaptureConfig(
        fps=60, buffer_seconds=32, width=1920, height=1080,
        bitrate_kbps=25_000, codec='h264', preset=4, monitor='',
        scaling='stretch', audio_enabled=False)
    request = main.build_resolved_capture_request(
        request_token=token, purpose='recording',
        source=main.SourceSnapshot(
            source_mode='desktop', display_name='Desktop',
            clip_group='Desktop'),
        capture_config=config, mic_gain_percent=100,
        effects={'capture_card_enabled': True})
    session = main.bind_capture_session(
        request, process_generation=1, native_capture_generation=1,
        accepted_config_token=token)
    return request, session


def test_audio_toggle_requests_capture_restart():
    body = _method_source('_on_audio_capture_changed', '_requested_capture_config')
    assert 'self._restart_capture_engine()' in body


def test_game_detection_startup_preserves_manual_window_target():
    pytest.importorskip('PySide6.QtCore')
    from main import MainWindow

    settings = SimpleNamespace(
        values={
            'capture_mode': 'window',
            'target_hwnd': 328546,
            'target_window_name': 'Windows Input Experience',
            'target_clip_group': 'Old Game',
        },
        get=lambda key, default=None: settings.values.get(key, default),
        update=lambda values: settings.values.update(values),
        save_settings=lambda: True,
    )
    fake = SimpleNamespace(settings_manager=settings)

    MainWindow._prepare_startup_capture_source(fake)

    assert settings.values['capture_mode'] == 'window'
    assert settings.values['target_hwnd'] == 328546
    assert settings.values['target_window_name'] == 'Windows Input Experience'
    assert settings.values['target_clip_group'] == 'Old Game'


def test_game_detection_can_apply_sources_without_a_deferred_ui():
    pytest.importorskip('PySide6.QtCore')
    from main import MainWindow
    from test_capture_admission_integration import _host as _admission_host

    values = {'capture_mode': 'desktop', 'target_hwnd': 0,
              'target_window_name': ''}
    settings = SimpleNamespace(
        get=lambda key, default=None: values.get(key, default),
        update=lambda updates: values.update(updates),
        save_settings=lambda: True,
    )
    # Keep the real F13 policy/proof chain; only omit the deferred UI.
    fake = _admission_host(settings)

    assert MainWindow._set_capture_window_source(
        fake, {'hwnd': 77, 'display_name': 'Actual Game'})
    assert values['capture_mode'] == 'window'
    assert values['target_hwnd'] == 77
    assert values['target_window_name'] == 'Actual Game'
    assert values['target_clip_group'] == 'Actual Game'

    assert MainWindow._set_capture_desktop_source(fake)
    assert values['capture_mode'] == 'desktop'
    assert values['target_hwnd'] == 0
    assert values['target_window_name'] == ''
    assert values['target_clip_group'] == ''
    assert fake._admission_policy.enabled is False
    assert 'capture_admission_policy' not in values


@pytest.mark.parametrize('source_mode', ['window', 'desktop'])
def test_deferred_source_settings_still_obey_enabled_empty_admission(
        monkeypatch, source_mode):
    import main
    from test_capture_admission_integration import _host as _admission_host

    policy = {'version': 1, 'revision': 1, 'enabled': True,
              'executable_paths': []}
    values = {'capture_mode': 'desktop', 'target_hwnd': 0,
              'target_window_name': '', 'target_clip_group': '',
              'capture_admission_policy': policy}
    before = json.dumps(values, sort_keys=True).encode('utf-8')
    saves = []
    settings = SimpleNamespace(
        get=lambda key, default=None: values.get(key, default),
        update=lambda updates: values.update(updates),
        save_settings=lambda: saves.append(dict(values)))
    host = _admission_host(settings)
    candidate = {'hwnd': 77, 'pid': 321, 'process_created': 9876,
                 'exe_path': r'C:\Games\actual.exe',
                 'class_name': 'GameWindow', 'display_name': 'Actual Game'}
    looked_up = []
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    monkeypatch.setattr(
        main, 'get_window_info',
        lambda hwnd, **kwargs:
        looked_up.append(hwnd) or SimpleNamespace(**candidate))

    if source_mode == 'window':
        assert main.MainWindow._set_capture_window_source(host, candidate) is False
        assert looked_up == [77]
    else:
        assert main.MainWindow._set_capture_desktop_source(host) is False
        assert looked_up == []
    assert host._admission_policy.enabled is True
    assert host._admission_policy.executable_paths == ()
    assert json.dumps(values, sort_keys=True).encode('utf-8') == before
    assert saves == []


def test_game_detection_defers_windows_geometry_change_to_native_reconfigure(
        monkeypatch):
    pytest.importorskip('PySide6.QtCore')
    import main
    from test_capture_admission_integration import _host as _admission_host

    values = {
        'game_detection_enabled': True,
        'capture_source_authority': 'automatic',
        'capture_mode': 'window',
        'target_hwnd': 77,
    }
    persisted = []
    settings = SimpleNamespace(
        get=lambda key, default=None: values.get(key, default),
        update=lambda updates: values.update(updates),
        save_settings=lambda: persisted.append(dict(values)))
    original = main.GameWindow(
        hwnd=77, pid=9, title='Actual Game', exe_name='game.exe',
        exe_path=r'C:\Games\game.exe', class_name='GameWindow',
        right=1920, bottom=1080, monitor_width=1920, monitor_height=1080)
    resized = main.GameWindow(
        hwnd=77, pid=9, title='Actual Game', exe_name='game.exe',
        exe_path=r'C:\Games\game.exe', class_name='GameWindow',
        right=2560, bottom=1440, monitor_width=2560, monitor_height=1440)
    from core.source_authority import window_identity
    values['target_window_identity'] = window_identity(original)
    restarts = []
    fake = _admission_host(settings)
    fake._active_game_window = original
    fake.engine_process = SimpleNamespace(poll=lambda: None)
    fake._manual_record_state = 'idle'
    fake._game_window_hwnd = lambda window: int(window.hwnd)
    fake._restart_capture_engine = lambda **kwargs: restarts.append(kwargs)
    fake._on_game_capture_geometry_changed = (
        main.MainWindow._on_game_capture_geometry_changed.__get__(fake))
    monkeypatch.setattr(main, 'is_capture_window_valid', lambda _hwnd: True)
    monkeypatch.setattr(main, 'get_window_info', lambda _hwnd, own_pid=None: resized)

    main.MainWindow._handle_game_candidate(fake, resized)

    assert fake._active_game_window is resized
    assert restarts == []
    assert values['target_clip_group'] == 'game'
    assert persisted == [values]


def test_game_detection_defers_geometry_restart_until_recording_closes(
        monkeypatch):
    pytest.importorskip('PySide6.QtCore')
    import main
    from core.game_profiles import empty_catalog
    from test_game_profile_integration import _resolve_host, _patch_resolver, _bind

    errors = []
    restarts = []
    callbacks = []
    original = main.GameWindow(
        hwnd=77, pid=9, title='Actual Game', exe_name='game.exe',
        right=1920, bottom=1080, monitor_width=1920, monitor_height=1080)
    resized = main.GameWindow(
        hwnd=77, pid=9, title='Actual Game', exe_name='game.exe',
        right=2560, bottom=1440, monitor_width=2560, monitor_height=1440)
    # Preserve the real request/admission/idle-coordinator chain introduced by
    # F12/F13. Only the OS resolver inputs and final native restart are doubles.
    fake, _settings, live_window = _resolve_host(main, empty_catalog())
    _bind(fake, main, '_capture_owner_snapshot', '_capture_owner_snapshot_is_current')
    _patch_resolver(monkeypatch, main, live_window)
    fake._manual_record_state = 'recording'
    fake._pending_game_geometry_restart = False
    fake._engine_profile = 'clips'
    fake._shutdown_complete = False
    fake.push_error = lambda *args, **kwargs: errors.append((args, kwargs))
    fake._restart_capture_engine = lambda **kwargs: restarts.append(kwargs)
    monkeypatch.setattr(
        main.QTimer, 'singleShot',
        lambda _delay, callback: callbacks.append(callback))

    main.MainWindow._on_game_capture_geometry_changed(
        fake, original, resized)

    assert fake._pending_game_geometry_restart is True
    assert restarts == []
    assert errors and errors[0][0][0] == 'GAME RESOLUTION CHANGED'

    # Model the actual file-closed boundary, rather than asking an idle
    # coordinator to restart while a recording still owns the capture source.
    fake._manual_record_state = 'idle'
    main.MainWindow._restore_clip_capture_profile(fake)

    assert fake._pending_game_geometry_restart is False
    assert len(callbacks) == 1
    assert restarts == []
    request = fake._desired_capture_request
    assert request.purpose == 'clips'
    callbacks[0]()
    assert restarts == [{'_prepared_request': request}]
    assert restarts[0]['_prepared_request'] is request


def test_legacy_game_crop_is_ignored_on_windows_and_retained_on_linux(monkeypatch):
    pytest.importorskip('PySide6.QtCore')
    import main
    from core.windows_monitor import MonitorChoice

    # The production Windows resolver must be deterministic, but this test
    # runs on a headless host with no real display topology. Supply one
    # canonical primary monitor so the test reaches the crop-policy branch.
    monkeypatch.setattr(
        main, 'enumerate_windows_monitors',
        lambda: [MonitorChoice(
            gdi_name='DISPLAY-PATH',
            friendly_name='Test display',
            device_path=r'\\?\DISPLAY#TEST\001',
            primary=True,
        )],
    )

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

    if main.sys.platform == 'win32':
        assert not config.crop_enabled
        assert (config.crop_x, config.crop_y, config.crop_w, config.crop_h) == (0, 0, 1, 1)
    else:
        assert config.crop_enabled
        assert (config.crop_x, config.crop_y, config.crop_w, config.crop_h) == (
            0.1, 0.2, 0.7, 0.6)


def test_engine_launch_hard_disables_multiband_and_forwards_encoder_selection():
    body = _method_source('start_engine', '_read_engine_startup_output')
    assert "multiband_arg = '0'" in body
    assert 'launch_config.multiband_enabled' not in body
    assert 'encoder_pref_int' in body




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
    body = _method_source('_save_clip', '_show_save_busy_feedback')

    handoff = body.index('if not owner_bridge.save_clip(')
    registration = body.index('submit = self._save_state.submit(')
    accepted_guard = body.index('if not submit.accepted:')
    feedback = body.index('self.capture_card.show_captured(')
    polling = body.index('self._save_poll_timer.start()')

    assert handoff < registration < accepted_guard < feedback < polling
    assert 'self.capture_card.show_clip(' not in body


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


@pytest.mark.parametrize('outcome', [
    'missing', 'vanished', 'replaced', 'not-ready', 'published-removed',
])
def test_unusable_final_clip_never_emits_success_feedback(
        tmp_path, monkeypatch, outcome):
    pytest.importorskip('PySide6.QtCore')
    from core.clip_readiness import ClipReadinessRegistry, ClipReadinessState
    from main import MainWindow, QTimer

    clip = tmp_path / f'{outcome}.mp4'
    retained = tmp_path / f'{outcome}-original.mp4'
    original_payload = b'original-final-clip'
    replacement_payload = b'replacement-final-clip'
    readiness = ClipReadinessRegistry()
    expected_identity = None
    if outcome != 'missing':
        clip.write_bytes(original_payload)
        info = clip.stat()
        expected_identity = {
            'st_dev': int(info.st_dev),
            'st_ino': int(info.st_ino),
            'size': int(info.st_size),
            'mtime_ns': int(info.st_mtime_ns),
        }
    if outcome in {'vanished', 'replaced', 'published-removed'}:
        clip.replace(retained)
    if outcome == 'replaced':
        clip.write_bytes(replacement_payload)
    readiness.engine_committed(str(clip), needs_finalization=True)

    notifications = []
    errors = []
    statuses = []
    observed = []
    grid_updates = []
    published = []
    events = []
    monkeypatch.setattr(QTimer, 'singleShot', lambda *_args: None)
    fake = SimpleNamespace(
        _published_final_clips=set(),
        _failed_final_clips=set(),
        _finalized_clip_identities={},
        _clip_readiness=readiness,
        _set_status=lambda *args: statuses.append(args),
        push_error=lambda *args, **kwargs: errors.append((args, kwargs)),
        library_storage_controller=SimpleNamespace(
            observe_completed_clip=lambda *args, **kwargs:
            observed.append((args, kwargs))),
        clip_grid=SimpleNamespace(
            upsert_saved_clip=lambda *args, **kwargs:
            grid_updates.append((args, kwargs))),
        clip_saved=SimpleNamespace(emit=published.append),
        capture_card=SimpleNamespace(
            show_clip=lambda *args: notifications.append(args)),
    )
    key = os.path.normcase(os.path.abspath(str(clip)))
    if outcome == 'published-removed':
        fake._published_final_clips.add(key)
    monkeypatch.setattr(
        'main.emit_event',
        lambda *args, **kwargs: events.append((args, kwargs)))

    MainWindow._publish_final_clip(
        fake, str(clip), 5, expected_identity=expected_identity)
    MainWindow._publish_final_clip(
        fake, str(clip), 5, expected_identity=expected_identity)

    assert notifications == []
    assert observed == []
    assert grid_updates == []
    assert published == []
    assert not any(args[:2] == ('clip_save', 'finalization_completed')
                   for args, _fields in events)
    assert key in fake._failed_final_clips
    assert key not in fake._finalized_clip_identities
    assert readiness.state(str(clip)) is ClipReadinessState.FINALIZATION_FAILED
    assert not readiness.can_access(str(clip))
    if outcome == 'published-removed':
        assert errors == []
        assert statuses == []
    else:
        assert len(errors) == 1
        assert errors[0][0][:2] == (
            'CLIP FINALIZATION FAILED',
            'The final clip file is unavailable. The base save was not '
            'reported as a completed clip.')
        assert len(statuses) == 1
        assert statuses[0][0] == 'FINALIZATION FAILED'
    if outcome == 'replaced':
        assert retained.read_bytes() == original_payload
        assert clip.read_bytes() == replacement_payload
    elif outcome in {'vanished', 'published-removed'}:
        assert retained.read_bytes() == original_payload
        assert not clip.exists()
    elif outcome == 'not-ready':
        assert clip.read_bytes() == original_payload
    else:
        assert not clip.exists()


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
    monkeypatch.setattr(
        main, 'probe_video_presentation_duration',
        lambda *_args: pytest.fail('shutdown must not add a duration probe'))
    request_a, session_a = _manual_session_a(main)
    fake = SimpleNamespace(
        _manual_record_path=recording,
        _manual_record_request=request_a,
        _manual_record_session=session_a,
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
    assert completed == [((recording,), {
        'publish_ui': False, 'session_snapshot': session_a})]
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
    request_a, session_a = _manual_session_a(main)
    fake = SimpleNamespace(
        _manual_record_path=recording,
        _manual_record_request=request_a,
        _manual_record_session=session_a,
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
        '_recover_fragments': True, '_writer_closed': True,
        'expected_recording': recording,
        'expected_session': session_a,
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


def _manual_recording_duration_host(main, recording):
    """Keep production state transitions while isolating app services."""
    queued = []
    notifications = []
    finalization_warnings = []
    ready = threading.Event()

    def enqueue(callback):
        queued.append(callback)
        ready.set()

    request_a, session_a = _manual_session_a(main)

    class Button:
        text = ''
        tooltip = ''

        def setText(self, text):
            self.text = text

        def setToolTip(self, text):
            self.tooltip = text

        def setEnabled(self, _enabled):
            pass

    host = SimpleNamespace(
        _manual_record_state='starting', _manual_record_path=recording,
        _manual_record_request=request_a, _manual_record_session=session_a,
        _accepted_capture_session=session_a,
        _pending_manual_record_path=None, _manual_record_finalize_thread=None,
        _manual_record_timer=SimpleNamespace(stop=lambda: None),
        record_btn=Button(), _ui_call=SimpleNamespace(emit=enqueue),
        _set_status=lambda *_args: None, _update_status=lambda: None,
        _restore_clip_capture_profile=lambda: None,
        _format_recording_elapsed=main.MainWindow._format_recording_elapsed,
        _manual_record_button_qss=lambda *_args: '',
        capture_card=SimpleNamespace(show_recording_saved=lambda *args, **kwargs:
                                     notifications.append((args, kwargs))),
        _record_finalization_warning=lambda *args: finalization_warnings.append(args),
        _fail_manual_recording=lambda *args, **kwargs:
        pytest.fail(f'unexpected recording failure: {args!r} {kwargs!r}'),
    )
    host._save_state = SimpleNamespace(is_busy=lambda: False)
    host._mux_threads = ()
    host._shutdown_requested = False
    host._shutdown_complete = False
    host._restart_pending = False
    host._capture_settings_applying = False
    host._pending_launch_config = None
    host._latest_capture_intent_state = 'none'
    host._latest_capture_intent_consumed = True
    host._desired_capture_request = None
    host._publish_capture_profile_status = lambda **_kwargs: None
    host._capture_resources_busy = (
        lambda **kwargs: main.MainWindow._capture_resources_busy(
            host, **kwargs))
    host._apply_latest_desired_request_if_idle = (
        lambda: main.MainWindow._apply_latest_desired_request_if_idle(host))
    host._finalization_warnings = finalization_warnings
    host._clear_manual_stop_request = (
        lambda: main.MainWindow._clear_manual_stop_request(host))
    host._mark_manual_recording_started = (
        lambda: main.MainWindow._mark_manual_recording_started(host))
    host._persist_manual_recording_highlights = (
        lambda recording, expected_identity, **kwargs:
        main.MainWindow._persist_manual_recording_highlights(
            host, recording, expected_identity, **kwargs))
    host._finalize_manual_recording_file = (
        lambda: main.MainWindow._finalize_manual_recording_file(host))
    host._complete_manual_recording_file = (
        lambda path, **kwargs:
        main.MainWindow._complete_manual_recording_file(host, path, **kwargs))
    return host, queued, notifications, ready


def test_delayed_recording_ack_uses_measured_video_not_live_or_audio_duration(
        tmp_path, monkeypatch):
    """The ACK is simulated; the two-second video/five-second audio are real."""
    import json
    import main
    from core.ffmpeg_tools import get_ffmpeg_exe, get_ffprobe_exe

    recording = tmp_path / 'video2-audio5.mp4'
    subprocess.run([
        get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error', '-n',
        '-f', 'lavfi', '-i', 'color=c=black:s=160x90:r=30:d=2',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:d=5',
        '-map', '0:v', '-map', '1:a', '-c:v', 'mpeg4', '-q:v', '5',
        '-c:a', 'aac', str(recording),
    ], check=True, capture_output=True, timeout=30)
    metadata = json.loads(subprocess.check_output([
        get_ffprobe_exe(), '-v', 'error', '-show_streams', '-show_format',
        '-of', 'json', str(recording),
    ], timeout=15))
    video_duration = next(float(s['duration']) for s in metadata['streams']
                          if s['codec_type'] == 'video')
    assert video_duration == pytest.approx(2.0, abs=1 / 30)
    assert float(metadata['format']['duration']) >= 4.9

    monkeypatch.setattr(main, 'set_theme_style', lambda *_args: None)
    monkeypatch.setattr(main.QTimer, 'singleShot', lambda *_args: None)
    clock = {'now': 103.5}
    monkeypatch.setattr(main, 'time', SimpleNamespace(monotonic=lambda: clock['now']))
    host, queued, notifications, ready = _manual_recording_duration_host(main, recording)
    reply = {'value': ('started', '')}
    host.bridge = SimpleNamespace(
        peek_manual_recording_response=lambda: reply['value'],
        consume_manual_recording_response=lambda: reply.update(value=None),
        get_status=lambda: {'is_recording': True},
    )
    main.MainWindow._pump_manual_recording_responses(host)
    clock['now'] = 104.0
    main.MainWindow._pump_manual_recording_responses(host)
    assert '00:00:00' in host.record_btn.text
    assert '\u2248' in host.record_btn.text
    assert 'elapsed' in host.record_btn.tooltip.lower()
    assert notifications == []

    owner = threading.get_ident()
    probes = []
    real_validate = main.probe_media
    real_duration = main.probe_video_presentation_duration

    def validate(path):
        probes.append(('validate', threading.get_ident()))
        return real_validate(path)

    def measure(path):
        probes.append(('duration', threading.get_ident()))
        return real_duration(path)

    monkeypatch.setattr(main, 'probe_media', validate)
    monkeypatch.setattr(main, 'probe_video_presentation_duration', measure)
    host._manual_record_state = 'stopping'
    reply['value'] = ('stopped', '')
    main.MainWindow._pump_manual_recording_responses(host)
    assert ready.wait(10), 'validation worker did not queue completion'
    host._manual_record_finalize_thread.join(2)
    assert notifications == []
    assert [kind for kind, _thread in probes] == ['validate', 'duration']
    assert all(thread != owner for _kind, thread in probes)
    queued.pop()()
    assert len(notifications) == 1
    args, options = notifications[0]
    assert args == (recording.name,)
    assert options['duration_seconds'] == pytest.approx(video_duration)
    assert host._manual_record_state == 'idle'


@pytest.mark.parametrize('outcome', ['unknown', 'probe-error'])
def test_optional_recording_duration_failure_does_not_block_saved_notification(
        tmp_path, monkeypatch, outcome):
    import main

    recording = tmp_path / 'recording.mp4'
    recording.write_bytes(b'validated by isolated probe')
    host, queued, notifications, ready = _manual_recording_duration_host(main, recording)
    monkeypatch.setattr(main, 'probe_media', lambda _path: None)

    def duration(_path):
        if outcome == 'probe-error':
            raise RuntimeError('optional duration unavailable')
        return None

    monkeypatch.setattr(main, 'probe_video_presentation_duration', duration)
    monkeypatch.setattr(main, 'set_theme_style', lambda *_args: None)
    monkeypatch.setattr(main.QTimer, 'singleShot', lambda *_args: None)
    main.MainWindow._finalize_manual_recording_file(host)
    assert ready.wait(2)
    host._manual_record_finalize_thread.join(2)
    queued.pop()()
    assert len(notifications) == 1
    assert notifications[0][1].get('duration_seconds') is None
    assert host._manual_record_state == 'idle'


def test_stale_recording_duration_completion_cannot_publish_another_recording(
        tmp_path, monkeypatch):
    import main

    recording = tmp_path / 'old.mp4'
    recording.write_bytes(b'validated by isolated probe')
    host, queued, notifications, ready = _manual_recording_duration_host(main, recording)
    monkeypatch.setattr(main, 'probe_media', lambda _path: None)
    monkeypatch.setattr(main, 'probe_video_presentation_duration', lambda _path: 2.0)
    main.MainWindow._finalize_manual_recording_file(host)
    assert ready.wait(2)
    host._manual_record_finalize_thread.join(2)
    newer = tmp_path / 'new.mp4'
    host._manual_record_path = newer
    host._manual_record_state = 'recording'
    queued.pop()()
    assert notifications == []
    assert host._manual_record_path == newer
    assert host._manual_record_state == 'recording'


def test_rejected_recording_is_not_duration_probed_or_published(tmp_path, monkeypatch):
    import main

    recording = tmp_path / 'recoverable.mp4'
    recording.write_bytes(b'recoverable fragments')
    host, queued, notifications, ready = _manual_recording_duration_host(main, recording)
    failures = []
    probes = []
    host._fail_manual_recording = lambda *args, **kwargs: failures.append((args, kwargs))

    def reject(_path):
        raise RuntimeError('invalid video')

    monkeypatch.setattr(main, 'probe_media', reject)
    monkeypatch.setattr(main, 'probe_video_presentation_duration',
                        lambda path: probes.append(path))
    main.MainWindow._finalize_manual_recording_file(host)
    assert ready.wait(2)
    host._manual_record_finalize_thread.join(2)
    queued.pop()()
    assert probes == []
    assert notifications == []
    assert failures == [(('invalid video',), {
        'keep_recording': True, 'expected_recording': recording,
        '_recover_fragments': True, '_writer_closed': True,
        'expected_session': host._manual_record_session})]
    assert recording.read_bytes() == b'recoverable fragments'
