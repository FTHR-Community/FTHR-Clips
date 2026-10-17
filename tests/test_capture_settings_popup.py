from __future__ import annotations

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
    popup.add_custom_length_btn.click()
    assert popup.clip_combo.currentData() == 30
    assert settings.save_calls == 0
    assert not popup._restart_pending


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
    assert not popup.restart_btn.isVisible()

    popup.recording_qual_combo.setCurrentText('Custom')
    popup._set_recording_custom_bitrate(42_000)

    assert popup.get_recording_bitrate() == 42_000
    assert settings.get('recording_bitrate_level') == 'custom'
    assert settings.get('recording_custom_bitrate_kbps') == 42_000
    assert settings.get('bitrate_level') == 'high'
    assert popup._restart_pending is False


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
        SimpleNamespace(device_path=r'\\?\display#primary', primary=True, hmonitor=0, gdi_name=r'\\.\DISPLAY1'),
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
        SimpleNamespace(device_path=r'\\?\display#primary', primary=True, hmonitor=0, gdi_name=r'\\.\DISPLAY1'),
        SimpleNamespace(device_path=r'\\?\display#secondary', primary=False, hmonitor=0, gdi_name=r'\\.\DISPLAY2'),
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
        SimpleNamespace(device_path=r'\\?\display#primary'),
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
        ),
        SimpleNamespace(
            device_path=r'\\?\display#two',
            gdi_name=r'\\.\DISPLAY2', hmonitor=0,
            friendly_name='Two',
            primary=False,
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
            gdi_name=r'\\.\DISPLAY1', hmonitor=0),
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

    def fail_resolution(_self):
        raise MonitorSelectionError(result)

    popen_calls = []
    monkeypatch.setattr(main.subprocess, 'Popen',
                        lambda *args, **kwargs: popen_calls.append(args))
    statuses = []
    host = SimpleNamespace(
        _diagnostic_engine_start_count=0,
        _capture_config=CaptureConfigTracker(),
        _restart_pending=True,
        _requested_capture_config=lambda: fail_resolution(None),
        _set_capture_apply_state=lambda applying: None,
        _set_status=lambda text, style: statuses.append(text),
        push_error=lambda *args, **kwargs: None,
    )

    assert main.MainWindow.start_engine(host) is False
    assert popen_calls == []
    assert host._capture_config.error.startswith('test unresolved monitor')
    assert statuses == ['MONITOR UNAVAILABLE']


def test_start_engine_popen_boundary_contains_canonical_monitor_path(monkeypatch):
    import main
    from core.capture_settings import CaptureConfig, CaptureConfigTracker

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
    )

    class _FakeProcess:
        stdout = None

    popen_commands = []
    monkeypatch.setattr(
        main.subprocess, 'Popen',
        lambda command, **kwargs: (popen_commands.append(command)
                                   or _FakeProcess()))
    monkeypatch.setattr(
        main.threading, 'Thread',
        lambda *args, **kwargs: SimpleNamespace(start=lambda: None))

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
        settings_manager=_SettingsStub(),
    )

    assert main.MainWindow.start_engine(host) is True
    assert len(popen_commands) == 1
    assert popen_commands[0][10] == canonical
