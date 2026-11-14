from __future__ import annotations

from functools import partial
from types import SimpleNamespace

import pytest


class _SettingsStub:
    def __init__(self) -> None:
        self.settings = {
            'clip_length': 30,
            'framerate': 60,
            'resolution': 'source',
            'bitrate_level': 'high',
            'custom_bitrate_kbps': 25_000,
            'recording_framerate': 30,
            'recording_resolution': '1080p',
            'recording_bitrate_level': 'medium',
            'recording_custom_bitrate_kbps': 18_000,
        }
        self.save_calls = 0

    def get(self, key, default=None):
        return self.settings.get(key, default)

    def set(self, key, value):
        self.settings[key] = value

    def save_settings(self):
        self.save_calls += 1
        return True

    def read_capture_admission_policy(self):
        from core.settings_manager import SettingsManager

        return SettingsManager.read_capture_admission_policy(self)


def _bind_real_admission_helpers(host, main):
    """Bind Main's admission boundary and initialize its real policy cache."""
    for name, descriptor in vars(main.MainWindow).items():
        if not name.startswith('_admission_'):
            continue
        helper = getattr(main.MainWindow, name)
        if isinstance(descriptor, staticmethod):
            setattr(host, name, helper)
        elif callable(helper):
            setattr(host, name, partial(helper, host))
    if not hasattr(host, 'settings_manager'):
        host.settings_manager = _SettingsStub()
    if not hasattr(host, '_close_engine_startup_output'):
        host._close_engine_startup_output = partial(
            main.MainWindow._close_engine_startup_output, host)
    if not hasattr(host, '_engine_startup_output'):
        host._engine_startup_output = None
    if not hasattr(host, '_diagnostics'):
        host._diagnostics = None
    main.MainWindow._reload_admission_policy(
        host, authoritative=True, trigger_transition=False)
    return host


class _FailingSettingsStub(_SettingsStub):
    def save_settings(self):
        self.save_calls += 1
        return False


def test_custom_clip_length_is_independent_saved_and_emitted(qtbot, monkeypatch):
    from main import CaptureSettingsPopup, CustomClipLengthDialog
    from PySide6.QtWidgets import QDialog

    settings = _SettingsStub()
    popup = CaptureSettingsPopup(settings)
    qtbot.addWidget(popup)
    normal_changes = []
    popup.clip_length_changed.connect(normal_changes.append)
    monkeypatch.setattr(CustomClipLengthDialog, 'exec', lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(CustomClipLengthDialog, 'seconds', lambda self: 137)
    with qtbot.waitSignal(popup.custom_lengths_changed):
        popup.add_custom_length_btn.click()
    assert normal_changes == []
    assert settings.get('clip_length') == 30
    assert settings.get('custom_clip_lengths') == [137]
    assert popup.clip_combo.currentData() == 30
    assert popup.clip_combo.findData(137) == -1
    restored = CaptureSettingsPopup(settings)
    qtbot.addWidget(restored)
    assert restored._custom_clip_lengths() == [137]
    assert restored.clip_combo.currentData() == 30
    monkeypatch.setattr(CustomClipLengthDialog, 'seconds', lambda self: 300)
    popup.add_custom_length_btn.click()
    assert settings.get('custom_clip_lengths') == [137, 300]
    popup._remove_custom_length(137)
    assert settings.get('custom_clip_lengths') == [300]
    assert settings.get('clip_length') == 30


def test_cancel_custom_clip_length_restores_selection(qtbot, monkeypatch):
    from main import CaptureSettingsPopup, CustomClipLengthDialog
    from PySide6.QtWidgets import QDialog

    settings = _SettingsStub()
    popup = CaptureSettingsPopup(settings)
    qtbot.addWidget(popup)
    monkeypatch.setattr(CustomClipLengthDialog, 'exec', lambda self: QDialog.DialogCode.Rejected)
    with qtbot.assertNotEmitted(popup.restart_needed):
        popup.add_custom_length_btn.click()
    assert popup.clip_combo.currentData() == 30
    assert settings.save_calls == 0


def test_custom_clip_choices_validate_deduplicate_and_reload(qtbot):
    from main import CaptureSettingsPopup

    settings = _SettingsStub()
    settings.settings['custom_clip_lengths'] = [31, 31, True, 'bad', 901, 0]
    popup = CaptureSettingsPopup(settings)
    qtbot.addWidget(popup)
    assert popup._custom_clip_lengths() == [31]
    assert popup.clip_combo.findData(31) == -1
    settings.settings['clip_length'] = 31
    popup.reload_from_settings()
    assert popup.clip_combo.currentData() == 31


def test_custom_clip_dialog_uses_minutes_and_app_titlebar(qtbot):
    from main import CustomClipLengthDialog
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget

    dialog = CustomClipLengthDialog(59)
    qtbot.addWidget(dialog)
    assert not hasattr(dialog, 'unit_combo')
    assert dialog.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert dialog.findChild(QWidget, 'fthrDialogClose') is not None
    assert dialog.duration_spin.suffix() == ' min'
    assert dialog.seconds() == 60
    dialog.duration_spin.setValue(3)
    assert dialog.seconds() == 180


def test_custom_clip_dialog_supports_fifteen_minutes_and_warns(qtbot):
    from main import CustomClipLengthDialog

    dialog = CustomClipLengthDialog(137)
    qtbot.addWidget(dialog)
    dialog.duration_spin.setValue(15)
    assert dialog.seconds() == 900
    assert dialog.memory_warning.isVisibleTo(dialog)
    dialog.duration_spin.setValue(20)
    assert dialog.seconds() == 900
    dialog.duration_spin.setValue(0)
    assert dialog.seconds() == 60
    assert not dialog.memory_warning.isVisibleTo(dialog)


def test_fifteen_minute_clip_persists_and_reloads(qtbot, monkeypatch):
    from main import CaptureSettingsPopup, CustomClipLengthDialog
    from PySide6.QtWidgets import QDialog
    settings = _SettingsStub()
    popup = CaptureSettingsPopup(settings)
    qtbot.addWidget(popup)
    monkeypatch.setattr(CustomClipLengthDialog, 'exec', lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(CustomClipLengthDialog, 'seconds', lambda self: 900)
    popup.add_custom_length_btn.click()
    assert settings.get('clip_length') == 30
    assert settings.get('custom_clip_lengths') == [900]
    popup.reload_from_settings()
    assert popup.clip_combo.currentData() == 30
    assert popup._custom_clip_lengths() == [900]


def test_custom_bitrate_control_persists_and_steps_by_500(qtbot):
    from main import CaptureSettingsPopup

    settings = _SettingsStub()
    popup = CaptureSettingsPopup(settings)
    qtbot.addWidget(popup)

    popup.qual_combo.setCurrentText('Custom')
    assert popup.custom_bitrate_edit.isEnabled()
    assert popup.get_bitrate() == 25_000

    popup._adjust_custom_bitrate(500)
    assert popup.get_bitrate() == 25_500
    assert settings.get('custom_bitrate_kbps') == 25_500

    popup.custom_bitrate_edit.setText('42000')
    popup._commit_custom_bitrate_text()
    assert popup.get_bitrate() == 42_000
    assert settings.get('bitrate_level') == 'custom'


def test_custom_bitrate_is_clamped_to_supported_range(qtbot):
    from main import CaptureSettingsPopup

    popup = CaptureSettingsPopup(_SettingsStub())
    qtbot.addWidget(popup)
    popup.qual_combo.setCurrentText('Custom')

    popup._set_custom_bitrate(-1)
    assert popup.get_bitrate() == 500
    assert not popup.bitrate_minus_btn.isEnabled()


def test_recording_profile_is_independent_and_needs_no_manual_apply(qtbot):
    from main import CaptureSettingsPopup

    settings = _SettingsStub()
    popup = CaptureSettingsPopup(settings)
    qtbot.addWidget(popup)

    popup.recording_profile_btn.click()
    assert popup.recording_fields.isVisibleTo(popup)
    assert not popup.clip_fields.isVisibleTo(popup)
    assert popup.recording_fps_combo.currentText() == '30'
    assert popup.recording_res_combo.currentText() == '1080p'
    with qtbot.assertNotEmitted(popup.restart_needed):
        popup.recording_qual_combo.setCurrentText('Custom')
        popup._set_recording_custom_bitrate(42_000)

    assert popup.get_recording_bitrate() == 42_000
    assert settings.get('recording_bitrate_level') == 'custom'
    assert settings.get('recording_custom_bitrate_kbps') == 42_000
    assert settings.get('bitrate_level') == 'high'


def test_capture_profiles_share_the_popup_surface_without_helper_copy(qtbot):
    from main import CaptureSettingsPopup

    popup = CaptureSettingsPopup(_SettingsStub())
    qtbot.addWidget(popup)

    assert not hasattr(popup, 'profile_hint')
    assert popup.clip_fields.styleSheet() == 'background: transparent;'
    assert popup.recording_fields.styleSheet() == 'background: transparent;'


def test_recording_profile_builds_the_encoder_config(monkeypatch):
    import main

    settings = _SettingsStub()
    settings.settings.update({
        'recording_framerate': 30,
        'recording_resolution': '720p',
        'recording_bitrate_level': 'custom',
        'recording_custom_bitrate_kbps': 12_500,
        'capture_monitor': '',
        'codec_pref': 'auto',
        'encoder_preset': 4,
        'scaling_mode': 'stretch',
        'audio_capture_enabled': True,
        'encoder_pref': 'auto',
        'multiband_audio_enabled': False,
        'mic_device_id': '',
        'target_hwnd': 0,
    })
    host = SimpleNamespace(
        settings_manager=settings,
        capture_fps=60,
        capture_width=0,
        capture_height=0,
        capture_bitrate=50_000,
        clip_duration=30,
        _active_game_window=None,
    )
    monkeypatch.setattr(main, 'enumerate_windows_monitors', lambda: [
        SimpleNamespace(device_path=r'\\?\display#primary', primary=True,
                        hmonitor=0, gdi_name=r'\\.\DISPLAY1',
                        width=0, height=0, refresh_rate=0.0),
    ])

    config = main.MainWindow._requested_capture_config(host, 'recording')

    assert (config.fps, config.width, config.height, config.bitrate_kbps) == (
        30, 1280, 720, 12_500)


def test_stale_stored_monitor_recovers_to_primary_for_native_startup(monkeypatch):
    import main

    settings = _SettingsStub()
    settings.settings['capture_monitor'] = r'\\?\display#stale-monitor'
    host = SimpleNamespace(
        settings_manager=settings,
        capture_fps=60,
        capture_width=1920,
        capture_height=1080,
        capture_bitrate=25_000,
        clip_duration=30,
        _active_game_window=None,
    )

    monitor_choices = [
        SimpleNamespace(device_path=r'\\?\display#primary', primary=True,
                        hmonitor=0, gdi_name=r'\\.\DISPLAY1',
                        width=0, height=0, refresh_rate=0.0),
        SimpleNamespace(device_path=r'\\?\display#secondary', primary=False,
                        hmonitor=0, gdi_name=r'\\.\DISPLAY2',
                        width=0, height=0, refresh_rate=0.0),
    ]
    monkeypatch.setattr(main, 'enumerate_windows_monitors', lambda: monitor_choices)

    config = main.MainWindow._requested_capture_config(host)

    assert config.monitor == r'\\?\display#primary'
    assert settings.get('capture_monitor') == r'\\?\display#primary'
    assert settings.save_calls == 1


def test_background_startup_resolves_monitor_without_source_popup(monkeypatch):
    import main

    settings = _SettingsStub()
    settings.settings['capture_monitor'] = ''
    host = SimpleNamespace(
        settings_manager=settings,
        capture_fps=60,
        capture_width=1920,
        capture_height=1080,
        capture_bitrate=25_000,
        clip_duration=30,
        _active_game_window=None,
    )

    monitor_choices = [
        SimpleNamespace(device_path=r'\\?\display#primary', primary=True,
                        gdi_name=r'\\.\DISPLAY1', width=0, height=0,
                        refresh_rate=0.0),
    ]
    monkeypatch.setattr(main, 'enumerate_windows_monitors',
                        lambda: monitor_choices)
    config = main.MainWindow._requested_capture_config(host)

    assert config.monitor == r'\\?\display#primary'
    assert settings.get('capture_monitor') == r'\\?\display#primary'
    assert settings.save_calls == 1


def test_source_popup_recovers_stale_monitor_to_current_primary(
        monkeypatch, qtbot):
    import main

    monkeypatch.setattr(main.sys, 'platform', 'win32')
    stale_monitor = r'\\?\display#stale-monitor'
    primary_monitor = r'\\?\display#primary'
    settings = _SettingsStub()
    settings.settings['capture_monitor'] = stale_monitor
    monitor_choices = [SimpleNamespace(
        device_path=primary_monitor,
        primary=True,
        gdi_name=r'\\.\DISPLAY1', hmonitor=0,
        friendly_name='Primary display',
        width=0, height=0, refresh_rate=0.0,
    )]
    monkeypatch.setattr(main, 'enumerate_windows_monitors',
                        lambda: monitor_choices)

    popup = main.SourcePopup(settings)
    qtbot.addWidget(popup)

    assert popup.monitor_combo.currentData() == primary_monitor
    assert settings.get('capture_monitor') == primary_monitor
    assert settings.save_calls == 1

    popup.monitor_combo.setCurrentIndex(
        popup.monitor_combo.findData(primary_monitor))
    assert settings.get('capture_monitor') == primary_monitor


def test_source_popup_shows_native_monitor_metadata_without_qt_screen_lookup(
        monkeypatch, qtbot):
    import main

    monkeypatch.setattr(main.sys, 'platform', 'win32')
    settings = _SettingsStub()
    monitor_choices = [
        SimpleNamespace(
            device_path=r'\\?\display#a',
            primary=True,
            gdi_name='FTHR_TEST_DISPLAY1',
            friendly_name='27G2G8',
            width=1920,
            height=1080,
            refresh_rate=144.0,
            hmonitor=0,
        ),
        SimpleNamespace(
            device_path=r'\\?\display#b',
            primary=False,
            gdi_name='FTHR_TEST_DISPLAY2',
            friendly_name='LG ULTRAWIDE',
            width=2560,
            height=1080,
            refresh_rate=59.94,
            hmonitor=0,
        ),
    ]
    monkeypatch.setattr(main, 'enumerate_windows_monitors',
                        lambda: monitor_choices)

    popup = main.SourcePopup(settings)
    qtbot.addWidget(popup)

    labels = [popup.monitor_combo.itemText(i)
              for i in range(popup.monitor_combo.count())]
    assert labels == [
        '27G2G8 · Display 1 · 1920×1080 @ 144 Hz · Primary',
        'LG ULTRAWIDE · Display 2 · 2560×1080 @ 59.94 Hz',
    ]
    assert [popup.monitor_combo.itemData(i)
            for i in range(popup.monitor_combo.count())] == [
        r'\\?\display#a', r'\\?\display#b']


def test_source_popup_never_exposes_diagnostic_sentinel_as_monitor_data(
        monkeypatch, qtbot):
    import main

    monkeypatch.setattr(main.sys, 'platform', 'win32')
    sentinel = 'unavailable:not_configured'
    settings = _SettingsStub()
    settings.settings['capture_monitor'] = sentinel
    monitor_choices = [
        SimpleNamespace(
            device_path=r'\\?\display#one',
            gdi_name=r'\\.\DISPLAY1', hmonitor=0,
            friendly_name='One',
            primary=False,
            width=0, height=0, refresh_rate=0.0,
        ),
        SimpleNamespace(
            device_path=r'\\?\display#two',
            gdi_name=r'\\.\DISPLAY2', hmonitor=0,
            friendly_name='Two',
            primary=False,
            width=0, height=0, refresh_rate=0.0,
        ),
    ]
    monkeypatch.setattr(main, 'enumerate_windows_monitors',
                        lambda: monitor_choices)

    popup = main.SourcePopup(settings)
    qtbot.addWidget(popup)

    combo_values = [popup.monitor_combo.itemData(i)
                    for i in range(popup.monitor_combo.count())]
    assert sentinel not in combo_values
    assert popup.cur_monitor == ''
    assert settings.get('capture_monitor') == sentinel


def test_monitor_persistence_failure_restores_previous_value_and_blocks_config(
        monkeypatch):
    import main
    from core.windows_monitor import MonitorSelectionError

    stale_monitor = r'\\?\display#stale-monitor'
    settings = _FailingSettingsStub()
    settings.settings['capture_monitor'] = stale_monitor
    host = SimpleNamespace(
        settings_manager=settings,
        capture_fps=60,
        capture_width=1920,
        capture_height=1080,
        capture_bitrate=25_000,
        clip_duration=30,
        _active_game_window=None,
    )
    monkeypatch.setattr(main, 'enumerate_windows_monitors', lambda: [
        SimpleNamespace(
            device_path=r'\\?\display#primary', primary=True,
            gdi_name=r'\\.\DISPLAY1', hmonitor=0,
            width=0, height=0, refresh_rate=0.0),
    ])

    with pytest.raises(MonitorSelectionError) as raised:
        main.MainWindow._requested_capture_config(host)

    assert raised.value.result.code == 'MONITOR_PERSIST_FAILED'
    assert settings.get('capture_monitor') == stale_monitor
    assert settings.save_calls == 1


def test_start_engine_stops_before_popen_when_monitor_resolution_fails(
        monkeypatch):
    import main
    from core.capture_settings import CaptureConfigTracker
    from core.windows_monitor import (
        MonitorSelectionError, MonitorSelectionResult, MonitorSelectionState)

    result = MonitorSelectionResult(
        state=MonitorSelectionState.NOT_CONFIGURED,
        code='MONITOR_SELECTION_NOT_CONFIGURED',
        reason='test unresolved monitor',
        previous_state=MonitorSelectionState.NOT_CONFIGURED,
        previous_path='',
    )

    def fail_resolution(*_args, **_kwargs):
        raise MonitorSelectionError(result)

    popen_calls = []
    monkeypatch.setattr(main.subprocess, 'Popen',
                        lambda *args, **kwargs: popen_calls.append(args))
    statuses = []
    host = SimpleNamespace(
        _diagnostic_engine_start_count=0,
        _capture_config=CaptureConfigTracker(),
        _restart_pending=True,
        _resolve_capture_request=fail_resolution,
        _set_capture_apply_state=lambda applying: None,
        _set_status=lambda text, style: statuses.append(text),
        push_error=lambda *args, **kwargs: None,
    )
    _bind_real_admission_helpers(host, main)

    assert main.MainWindow.start_engine(host) is False
    assert popen_calls == []
    assert host._capture_config.error.startswith('test unresolved monitor')
    assert statuses == ['MONITOR UNAVAILABLE']


@pytest.mark.parametrize('mic_gain_percent, allow_replay_gaps', [
    (0, False), (100, True), (200, False),
])
def test_start_engine_popen_boundary_contains_canonical_monitor_path(
        monkeypatch, mic_gain_percent, allow_replay_gaps):
    import main
    from core.capture_settings import CaptureConfig, CaptureConfigTracker
    from core.capture_session import ResolvedCaptureRequest, SourceSnapshot
    from core.source_authority import window_identity

    canonical = r'\\?\display#amd-primary'
    config = CaptureConfig(
        fps=60,
        buffer_seconds=32,
        width=1920,
        height=1080,
        bitrate_kbps=25_000,
        codec='h264',
        preset=4,
        monitor=canonical,
        scaling='stretch',
        audio_enabled=False,
        allow_replay_gaps=allow_replay_gaps,
        allow_low_fps_clips=not allow_replay_gaps,
    )

    class _FakeProcess:
        stdout = None

    class _FakeStartupOutput:
        def attach(self, _stream):
            return None

    popen_commands = []
    popen_environments = []
    monkeypatch.setenv('FTHR_ALLOW_REPLAY_GAPS', '1' if not allow_replay_gaps else '0')
    monkeypatch.setattr(
        main.subprocess, 'Popen',
        lambda command, **kwargs: (popen_commands.append(command)
                                   or popen_environments.append(kwargs['env'])
                                   or _FakeProcess()))
    monkeypatch.setattr(
        main, 'EngineLogCapture', lambda *args, **kwargs: _FakeStartupOutput())
    monkeypatch.setattr(
        main.threading, 'Thread',
        lambda *args, **kwargs: SimpleNamespace(start=lambda: None))

    settings = _SettingsStub()
    settings.settings.update({
        'capture_mode': 'window', 'target_hwnd': 4242,
        'target_window_identity': None,
        'mic_volume': 100,
    })
    candidate = main.GameWindow(
        hwnd=4242, pid=321, title='Game', exe_path=r'C:\Games\game.exe',
        exe_name='game.exe', class_name='GameWindow', process_created=9876)
    settings.settings['target_window_identity'] = window_identity(candidate)
    request = ResolvedCaptureRequest(
        request_token=17,
        purpose='clips',
        source=SourceSnapshot(
            source_mode='window', exe_path=candidate.exe_path,
            live_identity=window_identity(candidate),
            display_name=candidate.title),
        capture_config=config,
        # This is the profile-resolved value; the global is deliberately
        # changed below after the immutable request has been prepared.
        mic_gain_percent=mic_gain_percent,
    )
    settings.settings['mic_volume'] = 13
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    monkeypatch.setattr(main, 'get_window_info',
                        lambda *_args, **_kwargs: candidate)

    host = SimpleNamespace(
        _diagnostic_engine_start_count=0,
        _capture_config=CaptureConfigTracker(),
        _requested_capture_config=lambda: config,
        _diagnostic_capture_config=main.MainWindow._diagnostic_capture_config,
        _restart_pending=False,
        _set_capture_apply_state=lambda applying: None,
        _set_status=lambda text, style: None,
        push_error=lambda *args, **kwargs: None,
        engine_path=__import__('pathlib').Path(__import__('sys').executable),
        _diagnostics=None,
        _capture_health=SimpleNamespace(
            reset=lambda preserve_recovery_budget: None),
        _close_engine_startup_output=lambda: None,
        settings_manager=settings,
    )
    host._resolve_capture_request = (
        lambda *args, **kwargs: kwargs.get('prepared_request') or request)
    host._capture_request_intent_id = partial(
        main.MainWindow._capture_request_intent_id, host)
    host._prepared_source_is_live = partial(
        main.MainWindow._prepared_source_is_live, host)
    host._owns_capture_apply = partial(
        main.MainWindow._owns_capture_apply, host)
    _bind_real_admission_helpers(host, main)

    assert main.MainWindow.start_engine(host, _prepared_request=request) is True
    assert len(popen_commands) == 1
    assert popen_commands[0][7:9] == ['1', '4242']
    assert popen_commands[0][10] == canonical
    assert popen_commands[0][16] == str(mic_gain_percent)
    assert popen_environments[0]['FTHR_ALLOW_REPLAY_GAPS'] == str(int(allow_replay_gaps))
    assert popen_environments[0]['FTHR_ALLOW_LOW_FPS_CLIPS'] == str(int(not allow_replay_gaps))


@pytest.mark.parametrize('hwnd, saved_identity, live_window', [
    (0, None, None),
    (4242, {
        'hwnd': 4242, 'pid': 321, 'exe_path': r'C:\Games\old.exe',
        'class_name': 'OldWindow', 'process_created': 9876,
    }, SimpleNamespace(
        hwnd=4242, pid=654, exe_path=r'C:\Games\replacement.exe',
        class_name='NewWindow', process_created=1122)),
])
def test_start_engine_never_falls_back_to_desktop_for_missing_or_reused_window(
        monkeypatch, hwnd, saved_identity, live_window):
    import main
    from core.capture_settings import CaptureConfig, CaptureConfigTracker
    from core.capture_session import ResolvedCaptureRequest, SourceSnapshot

    config = CaptureConfig(
        fps=60, buffer_seconds=32, width=1920, height=1080,
        bitrate_kbps=25_000, codec='h264', preset=4,
        monitor=r'\\?\display#amd-primary', scaling='stretch',
        audio_enabled=False)
    popen_calls = []
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    monkeypatch.setattr(main, 'get_window_info',
                        lambda *_args, **_kwargs: live_window)
    monkeypatch.setattr(main.subprocess, 'Popen',
                        lambda *args, **kwargs: popen_calls.append(args))
    settings = _SettingsStub()
    settings.settings.update({
        'capture_mode': 'window', 'target_hwnd': hwnd,
        'target_window_identity': saved_identity,
    })
    source = SourceSnapshot(
        source_mode='window',
        exe_path=(saved_identity or {}).get('exe_path') or None,
        live_identity=saved_identity,
        display_name='Game',
    )
    request = ResolvedCaptureRequest(
        request_token=17, purpose='clips', source=source,
        capture_config=config, mic_gain_percent=100)
    statuses = []
    errors = []
    host = SimpleNamespace(
        _diagnostic_engine_start_count=0,
        _capture_config=CaptureConfigTracker(),
        _resolve_capture_request=(
            lambda *args, **kwargs:
            kwargs.get('prepared_request') or request),
        _restart_pending=True,
        _set_capture_apply_state=lambda applying: None,
        _set_status=lambda text, style: statuses.append(text),
        push_error=lambda *args, **kwargs: errors.append(args),
        settings_manager=settings,
    )
    host._capture_request_intent_id = partial(
        main.MainWindow._capture_request_intent_id, host)
    host._latest_intent_owns_request = partial(
        main.MainWindow._latest_intent_owns_request, host)
    host._consume_capture_intent = partial(
        main.MainWindow._consume_capture_intent, host)
    host._capture_request_intents = {request.request_token: 1}
    host._latest_capture_intent_id = 1
    host._latest_capture_intent_state = 'valid'
    host._latest_capture_intent_consumed = False
    host._desired_capture_request = request
    host._desired_capture_intent_id = 1
    host._prepared_source_is_live = partial(
        main.MainWindow._prepared_source_is_live, host)
    host._owns_capture_apply = partial(
        main.MainWindow._owns_capture_apply, host)
    _bind_real_admission_helpers(host, main)

    assert main.MainWindow.start_engine(host) is False
    assert popen_calls == []
    assert settings.get('capture_mode') == 'window'
    assert settings.get('target_hwnd') == hwnd
    assert statuses == ['WINDOW UNAVAILABLE']
    assert errors and 'Reselect the window' in errors[0][1]


@pytest.mark.parametrize('reentrant_hook', [
    None, 'capture_apply_state', 'status', 'emit_event',
])
def test_window_preflight_reentry_keeps_newer_capture_intent_and_owner(
        monkeypatch, reentrant_hook):
    """A stale window veto cannot publish A over a reentrant B request."""
    import main
    from core.capture_settings import CaptureConfig, CaptureConfigTracker
    from core.capture_session import (
        CameraSnapshot, ResolvedCaptureRequest, SourceSnapshot)

    config_a = CaptureConfig(
        fps=60, buffer_seconds=32, width=1920, height=1080,
        bitrate_kbps=25_000, codec='h264', preset=4,
        monitor=r'\\?\display#amd-primary', scaling='stretch',
        audio_enabled=False)
    config_b = CaptureConfig(
        fps=30, buffer_seconds=32, width=1920, height=1080,
        bitrate_kbps=25_000, codec='h264', preset=4,
        monitor=r'\\?\display#amd-primary', scaling='stretch',
        audio_enabled=False)
    saved_identity = {
        'hwnd': 4242, 'pid': 321, 'exe_path': r'C:\Games\old.exe',
        'class_name': 'OldWindow', 'process_created': 9876,
    }
    replacement_window = SimpleNamespace(
        hwnd=4242, pid=654, exe_path=r'C:\Games\replacement.exe',
        class_name='NewWindow', process_created=1122)
    live_window = None if reentrant_hook is None else replacement_window
    source = SourceSnapshot(
        source_mode='window', exe_path=saved_identity['exe_path'],
        live_identity=saved_identity, display_name='Game')
    request_a = ResolvedCaptureRequest(
        request_token=17, purpose='clips', source=source,
        capture_config=config_a, mic_gain_percent=100,
        camera=CameraSnapshot(enabled=True, device_index=3))
    request_b = ResolvedCaptureRequest(
        request_token=18, purpose='clips', source=source,
        capture_config=config_b, mic_gain_percent=100)

    monkeypatch.setattr(main.sys, 'platform', 'win32')
    monkeypatch.setattr(main, 'get_window_info',
                        lambda *_args, **_kwargs: live_window)
    popen_calls = []
    camera_calls = []
    events = []
    statuses = []
    errors = []
    after_b_publication = []
    state = {'b_started': False, 'a_failed_before_b': False}
    tracker = CaptureConfigTracker()
    settings = _SettingsStub()
    settings.settings.update({
        'capture_mode': 'window', 'target_hwnd': 4242,
        'target_window_identity': saved_identity,
    })
    host = SimpleNamespace(
        _diagnostic_engine_start_count=0,
        _capture_config=tracker,
        _resolve_capture_request=(
            lambda *args, **kwargs: kwargs.get('prepared_request') or request_a),
        _restart_pending=True,
        _set_capture_apply_state=lambda _applying: None,
        _set_status=lambda text, _style: statuses.append(text),
        push_error=lambda *args, **kwargs: errors.append((args, kwargs)),
        settings_manager=settings,
    )
    host._capture_request_intents = {
        request_a.request_token: 1, request_b.request_token: 2,
    }
    host._latest_capture_intent_id = 1
    host._latest_capture_intent_state = 'valid'
    host._latest_capture_intent_consumed = False
    host._desired_capture_request = request_a
    host._desired_capture_intent_id = 1
    host._pending_capture_reason = 'A pending'
    host._capture_request_intent_id = partial(
        main.MainWindow._capture_request_intent_id, host)
    host._latest_intent_owns_request = partial(
        main.MainWindow._latest_intent_owns_request, host)
    host._consume_capture_intent = partial(
        main.MainWindow._consume_capture_intent, host)
    host._prepared_source_is_live = partial(
        main.MainWindow._prepared_source_is_live, host)
    _bind_real_admission_helpers(host, main)

    def begin_b():
        assert tracker.status.name == 'FAILED'
        assert tracker.error.startswith('The selected window')
        assert tracker.requested is None
        assert tracker.applying is None
        state['a_failed_before_b'] = True
        tracker.request(config_b)
        tracker.begin_apply()
        host._capture_request_token = request_b.request_token
        host._capture_intent_revision = 2
        host._latest_capture_intent_id = 2
        host._latest_capture_intent_state = 'valid'
        host._latest_capture_intent_consumed = False
        host._desired_capture_request = request_b
        host._desired_capture_intent_id = 2
        host._pending_capture_reason = 'B pending'
        host._restart_pending = True
        state['b_started'] = True

    def set_capture_apply_state(applying):
        if state['b_started']:
            after_b_publication.append(('apply_state', applying))
        if reentrant_hook == 'capture_apply_state' and not state['b_started']:
            begin_b()

    def set_status(text, _style):
        if state['b_started']:
            after_b_publication.append(('status', text))
        statuses.append(text)
        if reentrant_hook == 'status' and not state['b_started']:
            begin_b()

    def emit_event(*args, **kwargs):
        if state['b_started']:
            after_b_publication.append(('event', args))
        events.append((args, kwargs))
        if reentrant_hook == 'emit_event' and not state['b_started']:
            begin_b()

    host._set_capture_apply_state = set_capture_apply_state
    host._set_status = set_status
    host._request_camera_apply = (
        lambda *args, **kwargs: camera_calls.append((args, kwargs)))
    monkeypatch.setattr(main, 'emit_event', emit_event)
    monkeypatch.setattr(
        main.subprocess, 'Popen',
        lambda *args, **kwargs: popen_calls.append((args, kwargs)))

    assert main.MainWindow.start_engine(host) is False
    assert camera_calls == []
    assert popen_calls == []
    assert state['a_failed_before_b'] is (reentrant_hook is not None)
    if reentrant_hook is None:
        assert tracker.status.name == 'FAILED'
        assert tracker.error.startswith('The selected window')
        assert tracker.requested is None
        assert tracker.applying is None
        assert statuses == ['WINDOW UNAVAILABLE']
        assert events and events[-1][0][:2] == ('engine', 'start_failed')
        assert errors and 'Reselect the window' in errors[0][0][1]
    else:
        assert state['b_started'] is True
        assert tracker.status.name == 'APPLYING'
        assert tracker.applying is config_b
        assert host._desired_capture_request is request_b
        assert host._latest_capture_intent_id == 2
        assert host._pending_capture_reason == 'B pending'
        assert errors == []
        assert not after_b_publication
        if reentrant_hook == 'capture_apply_state':
            assert statuses == [] and events == []
        elif reentrant_hook == 'status':
            assert statuses == ['WINDOW UNAVAILABLE'] and events == []
        else:
            assert statuses == ['WINDOW UNAVAILABLE']
            assert events and events[-1][0][:2] == ('engine', 'start_failed')


def test_capture_preferences_restart_only_after_successful_persistence(qtbot, monkeypatch):
    from main import CaptureSettingsPopup

    settings = _SettingsStub()
    popup = CaptureSettingsPopup(settings)
    qtbot.addWidget(popup)
    assert settings.save_calls == 0
    with qtbot.waitSignal(popup.restart_needed):
        popup.fps_combo.setCurrentText('30')
    assert settings.get('framerate') == 30

    monkeypatch.setattr(settings, 'save_settings', lambda: False)
    with qtbot.assertNotEmitted(popup.restart_needed):
        popup.fps_combo.setCurrentText('60')
    assert settings.get('framerate') == 30
    assert popup.fps_combo.currentText() == '30'
    assert not popup._save_error.isHidden()


def test_capture_restart_coalesces_rapid_settings_edits(qtbot):
    from main import MainWindow
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QWidget

    owner = QWidget()
    qtbot.addWidget(owner)
    timer = QTimer(owner)
    timer.setSingleShot(True)
    timer.setInterval(30)
    restarts = []
    host = SimpleNamespace(
        _capture_settings_timer=timer,
        _restart_capture_engine=lambda: restarts.append(True),
    )
    timer.timeout.connect(partial(MainWindow._on_capture_settings_restart_requested, host))

    assert not timer.isActive()
    for _ in range(3):
        MainWindow._queue_capture_settings_restart(host)
    assert restarts == []
    qtbot.waitUntil(lambda: len(restarts) == 1)
    assert not timer.isActive()
