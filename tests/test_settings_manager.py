import sys
import json
import pytest
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent / 'FTHR_UI'))

from core.settings_manager import SettingsManager, clips_directory_from


def test_new_defaults_present(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    sm = SettingsManager()
    assert sm.get('codec_pref')     == 'auto'
    assert sm.get('encoder_preset') == 4
    assert sm.get('custom_bitrate_kbps') == 25_000
    assert sm.get('recording_framerate') == 60
    assert sm.get('recording_resolution') == 'source'
    assert sm.get('recording_bitrate_level') == 'medium'
    assert sm.get('error_notifications_enabled') is True
    assert sm.get('audio_capture_mode') == 'combined'
    assert sm.get('clip_viewer_autoplay') is True
    assert sm.get('clips_directory') == str(tmp_path / 'FTHR_Clips')


def test_configured_clips_directory_is_resolved(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    sm = SettingsManager()
    selected = tmp_path / 'Library'
    sm.set('clips_directory', str(selected))

    assert clips_directory_from(sm) == selected.resolve()


def test_old_config_gets_new_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    cfg_file = tmp_path / '.fthr' / 'settings.json'
    cfg_file.parent.mkdir(parents=True)
    with open(cfg_file, 'w') as f:
        json.dump({'clip_length': 60}, f)

    sm = SettingsManager()
    assert sm.get('codec_pref')     == 'auto'
    assert sm.get('encoder_preset') == 4
    assert sm.get('custom_bitrate_kbps') == 25_000
    assert sm.get('clip_length')    == 60   # existing value preserved


def test_old_capture_quality_seeds_recording_profile(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    cfg_file = tmp_path / '.fthr' / 'settings.json'
    cfg_file.parent.mkdir(parents=True)
    cfg_file.write_text(json.dumps({
        'framerate': 144,
        'resolution': '1440p',
        'bitrate_level': 'custom',
        'custom_bitrate_kbps': 72_500,
    }), encoding='utf-8')

    sm = SettingsManager()

    assert sm.get('recording_framerate') == 144
    assert sm.get('recording_resolution') == '1440p'
    assert sm.get('recording_bitrate_level') == 'custom'
    assert sm.get('recording_custom_bitrate_kbps') == 72_500


@pytest.mark.parametrize('codec', ['h264', 'hevc', 'av1'])
def test_codec_preference_persists_across_restart(tmp_path, monkeypatch, codec):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)

    first_run = SettingsManager()
    first_run.set('codec_pref', codec)
    assert first_run.save_settings() is True

    restarted = SettingsManager()
    assert restarted.get('codec_pref') == codec


def test_clip_viewer_autoplay_preference_persists_across_restart(
        tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)

    first_run = SettingsManager()
    first_run.set('clip_viewer_autoplay', False)
    assert first_run.save_settings() is True

    restarted = SettingsManager()
    assert restarted.get('clip_viewer_autoplay') is False


def test_extended_clips_are_not_in_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    sm = SettingsManager()
    assert sm.get('extended_clip_length') is None
    assert 'save_extended_clip' not in sm.get('hotkeys')


def test_old_extended_clip_settings_are_removed(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    cfg_file = tmp_path / '.fthr' / 'settings.json'
    cfg_file.parent.mkdir(parents=True)
    with open(cfg_file, 'w') as f:
        json.dump({'clip_length': 30, 'extended_clip_length': 120,
                   'hotkeys': {'save_extended_clip': 'F10', 'save_clip': 'F8'}}, f)
    sm = SettingsManager()
    assert sm.get('extended_clip_length') is None
    assert 'save_extended_clip' not in sm.get('hotkeys')
    assert sm.get('clip_length') == 30  # existing value unaffected


def test_console_failure_does_not_reclassify_a_successful_write(
        tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    manager = SettingsManager()

    def broken_print(*_args, **_kwargs):
        raise UnicodeEncodeError('test', 'x', 0, 1, 'unencodable')

    monkeypatch.setattr('builtins.print', broken_print)

    assert manager.save_settings() is True
    assert manager.config_file.is_file()


def test_recording_and_library_paths_expand_environment(tmp_path, monkeypatch):
    from core.settings_manager import recording_directory_from
    monkeypatch.setenv('FTHR_TEST_STORAGE', str(tmp_path))
    from types import SimpleNamespace
    settings = {'clips_directory': '$FTHR_TEST_STORAGE/Library',
                'recording_directory': '$FTHR_TEST_STORAGE/Recordings'}
    sm = SimpleNamespace(get=lambda key, default=None: settings.get(key, default))
    assert clips_directory_from(sm) == (tmp_path / 'Library').resolve()
    assert recording_directory_from(sm) == (tmp_path / 'Recordings').resolve()
    if sys.platform == 'win32':
        settings['recording_directory'] = '%FTHR_TEST_STORAGE%/Recordings'
        assert recording_directory_from(sm) == (tmp_path / 'Recordings').resolve()
    settings['recording_directory'] = None
    assert recording_directory_from(sm) == (tmp_path / 'Library/Recordings').resolve()
