"""Programs-only backup preference and its immutable native launch boundary."""
from dataclasses import replace
import ast
import inspect
import textwrap
from types import SimpleNamespace

import pytest

from core.capture_profile_policy import build_capture_config
from core.capture_session import ResolvedCaptureRequest, SourceSnapshot, CaptureSnapshotError
from core.capture_settings import CaptureConfig
from core.native_audio_capabilities import (
    native_audio_backup_control_supported, native_audio_exclusions_supported,
    native_audio_policy_arguments,
)
from core.settings_manager import SettingsManager
from main import _SettingsPage


@pytest.fixture
def backup_page(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    for name in ('_populate_mic_devices', '_populate_playback_devices',
                 '_refresh_keyboard_windows', '_start_encoder_probe'):
        monkeypatch.setattr(_SettingsPage, name, lambda *args, **kwargs: None)
    page = _SettingsPage(SettingsManager())
    qtbot.addWidget(page)
    page.stack.setCurrentIndex(2)
    return page


def test_backup_checkbox_defaults_on_and_is_scoped_to_programs(backup_page):
    page = backup_page
    assert page.sm.get('keep_system_audio_backup') is True
    assert page.keep_system_audio_backup_check.isChecked()
    assert page.keep_system_audio_backup_check.isHidden()
    page.audio_source_combo.setCurrentIndex(page.audio_source_combo.findData('programs'))
    assert not page.keep_system_audio_backup_check.isHidden()
    assert page.keep_system_audio_backup_check.isEnabled()
    changed = []
    page.audio_capture_mode_changed.connect(changed.append)
    page.keep_system_audio_backup_check.setChecked(False)
    assert page.sm.get('keep_system_audio_backup') is False
    assert SettingsManager().get('keep_system_audio_backup') is False
    assert len(changed) == 1
    assert 'backup is off' in page.audio_source_hint.text()
    page.audio_source_combo.setCurrentIndex(page.audio_source_combo.findData('device'))
    assert page.keep_system_audio_backup_check.isHidden()
    assert page.sm.get('keep_system_audio_backup') is False


def test_exclusions_override_backup_without_losing_saved_preference(backup_page):
    page = backup_page
    page.audio_source_combo.setCurrentIndex(page.audio_source_combo.findData('programs'))
    page.audio_exclusions_widget.add_exclusion('discord')
    assert page.keep_system_audio_backup_check.isChecked()
    assert not page.keep_system_audio_backup_check.isEnabled()
    assert 'exclusions disable' in page.system_audio_backup_hint.text()
    assert page.sm.get('keep_system_audio_backup') is True
    page.audio_exclusions_widget.remove_exclusion('discord')
    assert page.keep_system_audio_backup_check.isEnabled()
    assert page.keep_system_audio_backup_check.isChecked()


def test_backup_checkbox_rolls_back_failed_save(backup_page, monkeypatch):
    page = backup_page
    monkeypatch.setattr(page.sm, 'save_settings', lambda: False)
    changed = []
    page.audio_capture_mode_changed.connect(changed.append)
    page.keep_system_audio_backup_check.setChecked(False)
    assert page.keep_system_audio_backup_check.isChecked()
    assert page.sm.get('keep_system_audio_backup') is True
    assert not page.system_audio_backup_save_error.isHidden()
    assert changed == []


def test_routine_guidance_moves_to_tooltips_but_save_failure_remains_visible(backup_page, monkeypatch):
    page = backup_page
    page.audio_source_combo.setCurrentIndex(page.audio_source_combo.findData('programs'))
    assert page.audio_source_hint.isHidden()
    assert page.system_audio_backup_hint.isHidden()
    assert 'backup' in page.audio_source_combo.toolTip().lower()
    assert 'future Programs captures' in page.keep_system_audio_backup_check.toolTip()
    page.keep_system_audio_backup_check.setChecked(False)
    assert page.audio_source_hint.isHidden()
    assert page.system_audio_backup_hint.isHidden()
    assert 'backup is off' in page.audio_source_combo.toolTip()
    monkeypatch.setattr(page.sm, 'save_settings', lambda: False)
    page.keep_system_audio_backup_check.setChecked(True)
    page._sync_audio_source_mode()
    assert not page.system_audio_backup_save_error.isHidden()


@pytest.mark.parametrize('purpose', ['clips', 'recording'])
@pytest.mark.parametrize('keep_backup', [True, False])
def test_profile_and_manual_recording_config_take_global_backup_preference(purpose, keep_backup):
    values = dict(clip_length=30, framerate=60, resolution='source',
        bitrate_level='high', custom_bitrate_kbps=25000,
        recording_framerate=60, recording_resolution='source',
        recording_bitrate_level='high', recording_custom_bitrate_kbps=25000,
        audio_source_mode='programs', keep_system_audio_backup=not keep_backup)
    config = build_capture_config(values=values,
        global_values={'platform': 'win32', 'keep_system_audio_backup': keep_backup},
        purpose=purpose, bitrate_presets={'source': {'high': 25000}},
        resolution_to_dims=lambda name: (0, 0), monitor='', codec='auto',
        encoder='auto', preset=4, scaling='fit')
    assert config.keep_system_audio_backup is keep_backup
    assert config.application_audio_tracks_enabled
    request = ResolvedCaptureRequest(request_token=1, purpose=purpose,
        source=SourceSnapshot('desktop'), capture_config=config)
    assert request.to_dict()['capture_config']['keep_system_audio_backup'] is keep_backup
    assert request.capture_config.keep_system_audio_backup is keep_backup
    assert replace(config, keep_system_audio_backup=not keep_backup) != request.capture_config


def test_legacy_requested_config_and_global_snapshot_keep_backup_preference(monkeypatch):
    import main
    from core.windows_monitor import MonitorChoice
    monitor = MonitorChoice(gdi_name=r'\\.\DISPLAY1', friendly_name='Primary',
        device_path=r'\\?\DISPLAY#PRIMARY\1', primary=True)
    monkeypatch.setattr(main, 'enumerate_windows_monitors', lambda: [monitor])
    values = dict(capture_monitor=monitor.device_path, audio_source_mode='programs',
                  keep_system_audio_backup=False)
    settings = SimpleNamespace(get=lambda key, default=None: values.get(key, default),
        set=lambda key, value: values.__setitem__(key, value), save_settings=lambda: True)
    host = SimpleNamespace(settings_manager=settings, capture_fps=60, clip_duration=30,
        capture_width=1920, capture_height=1080, capture_bitrate=25000,
        _active_game_window=None)
    assert not main.MainWindow._requested_capture_config(host).keep_system_audio_backup
    snapshot = main.MainWindow._capture_global_defaults_snapshot(host)
    assert snapshot['keep_system_audio_backup'] is False
    values['keep_system_audio_backup'] = True
    assert snapshot['keep_system_audio_backup'] is False


@pytest.mark.parametrize('invalid', [None, 0, 1, 'false'])
def test_backup_flag_requires_real_boolean_at_session_boundary(invalid):
    config = CaptureConfig(fps=60, buffer_seconds=32, width=1920, height=1080,
        bitrate_kbps=25000, codec='auto', preset=4, monitor='', scaling='fit',
        audio_enabled=True, keep_system_audio_backup=invalid)
    with pytest.raises(CaptureSnapshotError, match='keep_system_audio_backup'):
        ResolvedCaptureRequest(request_token=1, purpose='clips',
            source=SourceSnapshot('desktop'), capture_config=config)


def test_exclusion_capability_cannot_qualify_backup_control(tmp_path):
    binary = tmp_path / 'old-policy-engine.exe'
    binary.write_bytes(b'--probe-encoder\0FTHR_AUDIO_EXCLUSIONS_CAPABILITY_V1')
    def runner(args, **kwargs):
        assert args[-1] == '--audio-exclusions-capability'
        return SimpleNamespace(returncode=0, stdout=b'FTHR_AUDIO_EXCLUSIONS_CAPABILITY_V1')
    assert native_audio_exclusions_supported(binary, runner=runner)
    assert not native_audio_backup_control_supported(binary, runner=runner)


def test_backup_capability_probe_qualifies_exact_bytes_and_checks_response(tmp_path):
    binary = tmp_path / 'policy-engine.exe'
    binary.write_bytes(b'--probe-encoder\0FTHR_AUDIO_BACKUP_CAPABILITY_V1')
    calls = []
    def runner(args, **kwargs):
        assert args[-1] == '--audio-backup-capability'
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=b'FTHR_AUDIO_BACKUP_CAPABILITY_V1\n')
    assert native_audio_backup_control_supported(binary, runner=runner)
    assert native_audio_backup_control_supported(binary, runner=runner)
    assert len(calls) == 1
    binary.write_bytes(b'--probe-encoder\0FTHR_AUDIO_BACKUP_CAPABILITY_V1 changed')
    assert not native_audio_backup_control_supported(binary,
        runner=lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b'wrong'))


def test_launch_abi_carries_the_accepted_backup_preference():
    import main
    tree = ast.parse(textwrap.dedent(inspect.getsource(main.MainWindow.start_engine)))
    call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and node.func.attr == 'extend'
        and node.args and isinstance(node.args[0], ast.Call)
        and isinstance(node.args[0].func, ast.Name)
        and node.args[0].func.id == 'native_audio_policy_arguments')
    for keep_backup in (False, True):
        config = SimpleNamespace(application_audio_exclusions='discord',
                                 keep_system_audio_backup=keep_backup,
                                 audio_enabled=True, application_audio_tracks_enabled=True)
        args = eval(compile(ast.Expression(call.args[0]), '<audio launch args>', 'eval'),
                    {'launch_config': config, 'native_audio_policy_arguments': native_audio_policy_arguments})
        assert args == ['--audio-exclusions', 'discord'] + (
            [] if keep_backup else ['--keep-system-audio-backup', '0'])


def test_launcher_blocks_unsupported_backup_off_before_capture_popen():
    import main
    tree = ast.parse(textwrap.dedent(inspect.getsource(main.MainWindow.start_engine)))
    guard = next(node for node in ast.walk(tree) if isinstance(node, ast.If)
        and any(isinstance(part, ast.Name) and part.id == 'native_audio_backup_control_supported'
                for part in ast.walk(node.test)))
    popen = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and node.func.attr == 'Popen')
    assert guard.lineno < popen.lineno
    condition = compile(ast.Expression(guard.test), '<audio capability guard>', 'eval')
    for audio_enabled, programs, keep_backup, supported, expected_block in [
            (True, True, False, False, True),
            (True, True, False, True, False),
            (True, True, True, False, False),
            (True, False, False, False, False),
            (False, True, False, False, False)]:
        config = SimpleNamespace(audio_enabled=audio_enabled,
            application_audio_tracks_enabled=programs, application_audio_exclusions='',
            keep_system_audio_backup=keep_backup)
        blocked = eval(condition, {
            'sys': SimpleNamespace(platform='win32'), 'launch_config': config,
            'self': SimpleNamespace(engine_path='old-engine.exe'),
            'native_audio_exclusions_supported': lambda _engine: False,
            'native_audio_backup_control_supported': lambda _engine, _supported=supported: _supported})
        assert bool(blocked) is expected_block
