"""The downloadable test plugin has no contributions or settings side effects."""
import json
from pathlib import Path
import zipfile

import pytest

from core.plugin_manager import PluginError, PluginManager


def test_sample_noop_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    manager = PluginManager()
    sample = Path(__file__).resolve().parents[1] / 'examples/plugins/fthr-test-plugin.zip'
    manifest = manager.inspect_archive(sample)
    assert manifest['type'] == 'test'
    assert manifest['contributes'] == {}
    manager.install(sample)
    assert manager.list_plugins()[0]['enabled']
    assert manager.export_presets() == ()
    manager.set_enabled(manifest['id'], False)
    assert not PluginManager().list_plugins()[0]['enabled']
    manager.set_enabled(manifest['id'], True)
    with pytest.raises(PluginError):
        manager.apply_theme(manifest['id'])
    assert list((tmp_path / '.fthr').iterdir()) == [manager.root]
    manager.uninstall(manifest['id'])
    assert manager.list_plugins() == []


@pytest.mark.parametrize('contributes,extra', [({'theme': 'theme.zip'}, False), ({}, True)])
def test_noop_rejects_contributions_and_extra_files(tmp_path, contributes, extra):
    archive = tmp_path / 'bad.zip'
    with zipfile.ZipFile(archive, 'w') as package:
        package.writestr('manifest.json', json.dumps({
            'id': 'com.fthr.test', 'name': 'Test', 'version': '1.0.0',
            'api_version': 1, 'author': 'FTHR', 'type': 'test', 'contributes': contributes,
        }))
        if extra:
            package.writestr('run.py', 'raise Exception()')
    with pytest.raises(PluginError):
        PluginManager(tmp_path / 'installed').install(archive)
