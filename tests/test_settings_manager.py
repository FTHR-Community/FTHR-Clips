import sys
import json
sys.path.insert(0, '/home/tom/FTHR_Clips/FTHR_UI')

from core.settings_manager import SettingsManager


def test_new_defaults_present(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    sm = SettingsManager()
    assert sm.get('codec_pref')     == 'auto'
    assert sm.get('encoder_preset') == 4


def test_old_config_gets_new_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    cfg_file = tmp_path / '.fthr' / 'settings.json'
    cfg_file.parent.mkdir(parents=True)
    with open(cfg_file, 'w') as f:
        json.dump({'clip_length': 60}, f)

    sm = SettingsManager()
    assert sm.get('codec_pref')     == 'auto'
    assert sm.get('encoder_preset') == 4
    assert sm.get('clip_length')    == 60   # existing value preserved


def test_extended_clip_length_default(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    sm = SettingsManager()
    assert sm.get('extended_clip_length') == 60


def test_old_config_gets_extended_clip_length_default(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    cfg_file = tmp_path / '.fthr' / 'settings.json'
    cfg_file.parent.mkdir(parents=True)
    with open(cfg_file, 'w') as f:
        json.dump({'clip_length': 30}, f)
    sm = SettingsManager()
    assert sm.get('extended_clip_length') == 60
    assert sm.get('clip_length') == 30  # existing value unaffected
