"""Persistent executable exclusions, native compatibility, and shared picker."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

from core.capture_settings import CaptureConfig
from core.capture_session import ResolvedCaptureRequest, SourceSnapshot, CaptureSnapshotError
from core.running_apps import (
    RunningApp, executable_identity, normalize_application_audio_exclusions,
    encode_application_audio_exclusions,
)
from core.settings_manager import SettingsManager
from core.native_audio_capabilities import native_audio_exclusions_supported
from ui.audio_exclusions import AudioExclusionsWidget
from ui.running_app_picker import RunningAppPickerDialog


def _config(**kwargs):
    return CaptureConfig(fps=60, buffer_seconds=32, width=1920, height=1080,
        bitrate_kbps=25000, codec='auto', preset=4, monitor='', scaling='fit',
        audio_enabled=True, **kwargs)


def test_normalization_matches_native_identity_and_rejects_argument_injection():
    assert executable_identity(r'C:\Apps\Discord.EXE') == 'discord'
    assert normalize_application_audio_exclusions([
        'Discord.EXE', 'discord', 'chrome', r'C:\private\player.exe',
        'bad|injected', 'bad;injected', 'control\n', None]) == ['discord', 'chrome']
    assert encode_application_audio_exclusions(['Discord.EXE', 'chrome']) == 'discord|chrome'
    assert len(normalize_application_audio_exclusions([f'app{i}' for i in range(80)])) == 64


def test_settings_roundtrip_survives_new_pid_and_app_restart(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    settings = SettingsManager()
    assert settings.get('application_audio_exclusions') == []
    settings.set('application_audio_exclusions', ['discord', 'spotify'])
    assert settings.save_settings()
    reloaded = SettingsManager()
    assert reloaded.get('application_audio_exclusions') == ['discord', 'spotify']
    assert executable_identity('Discord.exe') in reloaded.get('application_audio_exclusions')


def test_exclusions_are_part_of_immutable_capture_request():
    config = _config(application_audio_tracks_enabled=True,
                     application_audio_exclusions='discord|spotify')
    request = ResolvedCaptureRequest(request_token=1, purpose='clips',
        source=SourceSnapshot('desktop'), capture_config=config)
    assert request.to_dict()['capture_config']['application_audio_exclusions'] == 'discord|spotify'
    later = replace(config, application_audio_exclusions='chrome')
    assert request.capture_config.application_audio_exclusions == 'discord|spotify'
    assert later.application_audio_exclusions == 'chrome'


@pytest.mark.parametrize('invalid', ['Discord', 'discord.exe', 'a|a', 'a||b',
    r'C:\App\private.exe', 'a|', ['discord'], '|'.join(f'a{i}' for i in range(65))])
def test_capture_snapshot_rejects_malformed_native_exclusions(invalid):
    with pytest.raises(CaptureSnapshotError, match='exclusions'):
        ResolvedCaptureRequest(request_token=1, purpose='clips',
            source=SourceSnapshot('desktop'),
            capture_config=_config(application_audio_exclusions=invalid))


def _capable_binary(tmp_path):
    binary = tmp_path / 'engine.exe'
    binary.write_bytes(b'--probe-encoder\0FTHR_AUDIO_EXCLUSIONS_CAPABILITY_V1')
    return binary


def test_older_engine_never_receives_unknown_probe(tmp_path):
    binary = tmp_path / 'old.exe'
    binary.write_bytes(b'--probe-encoder')
    def forbidden(*args, **kwargs):
        pytest.fail('old engine must not be executed')
    assert not native_audio_exclusions_supported(binary, runner=forbidden)


def test_native_qualification_is_cached_by_exact_executable_bytes(tmp_path):
    binary = _capable_binary(tmp_path)
    calls = []
    def runner(args, **kwargs):
        calls.append(args)
        assert args[1:] == ['--probe-encoder', '--audio-exclusions-capability']
        return SimpleNamespace(returncode=0, stdout=b'FTHR_AUDIO_EXCLUSIONS_CAPABILITY_V1\n')
    assert native_audio_exclusions_supported(binary, runner=runner)
    assert native_audio_exclusions_supported(binary, runner=runner)
    assert len(calls) == 1
    binary.write_bytes(b'--probe-encoder OLD ENGINE')
    assert not native_audio_exclusions_supported(binary, runner=runner)
    assert len(calls) == 1


@pytest.mark.parametrize('failure', ['wrong_output', 'exit_failure', 'timeout', 'replacement'])
def test_native_capability_fails_closed(tmp_path, failure):
    binary = _capable_binary(tmp_path)
    def runner(args, **kwargs):
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(args, 3)
        if failure == 'replacement':
            binary.write_bytes(b'old engine now at same path')
        return SimpleNamespace(returncode=1 if failure == 'exit_failure' else 0,
            stdout=b'wrong' if failure == 'wrong_output' else b'FTHR_AUDIO_EXCLUSIONS_CAPABILITY_V1')
    assert not native_audio_exclusions_supported(binary, runner=runner)


def test_widget_saves_only_executable_identity_and_handles_disk_failure(qtbot):
    values = {'application_audio_exclusions': []}
    settings = SimpleNamespace(get=lambda k, d=None: values.get(k, d),
        set=lambda k, v: values.__setitem__(k, v), save_settings=lambda: True)
    widget = AudioExclusionsWidget(settings)
    qtbot.addWidget(widget)
    changed = []
    widget.changed.connect(lambda: changed.append(True))
    widget.add_exclusion('Discord.EXE')
    assert values['application_audio_exclusions'] == ['discord']
    assert changed == [True]
    settings.save_settings = lambda: False
    widget.add_exclusion('chrome')
    assert values['application_audio_exclusions'] == ['discord']
    assert changed == [True]
    assert not widget.error.isHidden()
    settings.save_settings = lambda: True
    widget.remove_exclusion('discord')
    assert values['application_audio_exclusions'] == []


def test_picker_shows_icons_filters_and_omits_already_excluded_apps(qtbot, monkeypatch):
    monkeypatch.setattr('ui.running_app_picker.list_running_apps', lambda: [
        RunningApp('discord', 'Discord', '', 99), RunningApp('chrome', 'Google Chrome', '', 101)])
    picker = RunningAppPickerDialog(excluded=['discord'])
    qtbot.addWidget(picker)
    assert picker.apps.count() == 1
    assert not picker.apps.item(0).icon().isNull()
    picker.apps.setCurrentRow(0)
    assert picker.selected_app().identity == 'chrome'
    picker.search.setText('no-match')
    assert picker.apps.item(0).isHidden()
    assert picker.selected_app() is None
    assert not picker.confirm.isEnabled()


def test_running_inventory_deduplicates_pid_and_uses_path_free_identity(monkeypatch):
    from core import running_apps
    monkeypatch.setattr(running_apps, '_process_inventory', lambda: iter([
        (1, 'Discord.EXE', r'C:\Apps\Discord.exe'),
        (22, 'Discord.EXE', r'C:\Apps\Discord.exe'),
        (3, 'chrome.exe', r'C:\Apps\chrome.exe')]))
    apps = running_apps.list_running_apps()
    assert [app.identity for app in apps] == ['discord', 'chrome']
    assert len(apps) == 2


def test_settings_panel_enforces_future_program_scope(qtbot, monkeypatch, tmp_path):
    from main import _SettingsPage
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    for name in ('_populate_mic_devices', '_populate_playback_devices',
                 '_refresh_keyboard_windows', '_start_encoder_probe'):
        monkeypatch.setattr(_SettingsPage, name, lambda *args, **kwargs: None)
    page = _SettingsPage(SettingsManager())
    qtbot.addWidget(page)
    page.stack.setCurrentIndex(2)
    assert page.audio_exclusions_group.isHidden()
    page.audio_source_combo.setCurrentIndex(page.audio_source_combo.findData('programs'))
    assert not page.audio_exclusions_group.isHidden()
    events = []
    page.audio_capture_mode_changed.connect(events.append)
    page.audio_exclusions_widget.add_exclusion('discord')
    assert events
    assert 'device mix is disabled' in page.audio_source_hint.text()
    assert page.sm.get('application_audio_exclusions') == ['discord']


def test_native_capture_uses_the_tested_no_backup_policy():
    root = Path(__file__).resolve().parents[1]
    source = (root / 'FTHRcapture/FTHRclips/src/capture_engine.cpp').read_text()
    assert 'omit_default_mix_track_ = !config.RetainDefaultMixAudio();' in source
    assert source.index('if (config.ResolvedApplicationAudioTracksEnabled() && !config.RetainDefaultMixAudio()) break;') < source.index(
        'if (!audio_capture_.Initialize(nullptr, audio_cfg))')
