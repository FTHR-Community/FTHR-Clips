import sys, json
sys.path.insert(0, '/home/tom/FTHR_Clips/FTHR_UI')

from pathlib import Path
from core.presets_manager import PresetsManager


def test_save_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    pm = PresetsManager()
    pm.save('Gaming', {'clip_length': 60, 'framerate': 144})
    data = pm.load('Gaming')
    assert data == {'clip_length': 60, 'framerate': 144}


def test_list_names(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    pm = PresetsManager()
    pm.save('A', {'clip_length': 30})
    pm.save('B', {'clip_length': 60})
    assert 'A' in pm.names()
    assert 'B' in pm.names()


def test_delete(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    pm = PresetsManager()
    pm.save('ToDelete', {'clip_length': 30})
    pm.delete('ToDelete')
    assert 'ToDelete' not in pm.names()


def test_load_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    pm = PresetsManager()
    assert pm.load('NonExistent') is None
