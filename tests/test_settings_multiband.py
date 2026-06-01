import sys, json
sys.path.insert(0, '/home/tom/FTHR_Clips/FTHR_UI')
from core.settings_manager import SettingsManager


def test_multiband_defaults_present(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    sm = SettingsManager()
    assert sm.get('multiband_audio_enabled') is False
    cats = sm.get('audio_categories')
    assert isinstance(cats, list)
    names = [c['name'] for c in cats]
    assert 'Game' in names
    assert 'Sonstige' in names
    for c in cats:
        assert 'name' in c and 'volume' in c and 'patterns' in c


def test_old_source_volumes_migrated(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    cfg = tmp_path / '.fthr' / 'settings.json'
    cfg.parent.mkdir(parents=True)
    with open(cfg, 'w') as f:
        json.dump({'source_volumes': {'game': 90, 'discord': 70}}, f)

    sm = SettingsManager()
    cats = {c['name']: c for c in sm.get('audio_categories')}
    assert cats['Game']['volume'] == 90
    assert cats['Discord']['volume'] == 70


def test_multiband_toggle_default_false(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    sm = SettingsManager()
    assert sm.get('multiband_audio_enabled') is False
