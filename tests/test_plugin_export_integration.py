"""Plugin presets are visible to export consumers without entering user storage."""
import json
import zipfile

import pytest

from core.export_profiles import ExportPreset, ExportPresetManager


def test_plugin_presets_follow_enabled_state_without_changing_personal_presets(tmp_path, monkeypatch, qtbot):
    from core.plugin_manager import PluginManager

    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    manager = PluginManager()
    archive = tmp_path / 'presets.zip'
    with zipfile.ZipFile(archive, 'w') as package:
        package.writestr('manifest.json', json.dumps({
            'id': 'com.example.exports', 'name': 'Export pack',
            'version': '1.0.0', 'api_version': 1, 'author': 'Example',
            'type': 'export-presets',
            'contributes': {'export_presets': 'presets.json'},
        }))
        package.writestr('presets.json', json.dumps([
            {'preset_id': 'small', 'name': 'Small clip', 'target_size_mb': 12},
        ]))
    manager.install(archive)
    presets = ExportPresetManager()
    presets.save(ExportPreset('personal', 'My preset', 30))
    plugin_id = 'plugin:com.example.exports:small'
    assert presets.get(plugin_id).target_size_mb == 12
    from ui.clip_viewer import ShareModeDialog
    from ui.export_presets_widget import ExportPresetsWidget

    picker = ShareModeDialog()
    qtbot.addWidget(picker)
    assert picker._preset_combo.findData(plugin_id) >= 0
    editor = ExportPresetsWidget()
    qtbot.addWidget(editor)
    editor.refresh(plugin_id)
    assert 'Plugin' in editor.combo.currentText()
    assert not editor.edit_btn.isEnabled()
    assert not editor.delete_btn.isEnabled()
    assert [p.preset_id for p in presets.custom()] == ['personal']
    with pytest.raises(ValueError):
        presets.save(ExportPreset(plugin_id, 'Overwrite', 20))
    assert not presets.delete(plugin_id)
    manager.set_enabled('com.example.exports', False)
    assert presets.get(plugin_id) is None
    assert presets.get('personal') is not None
    manager.set_enabled('com.example.exports', True)
    assert presets.get(plugin_id) is not None
    manager.uninstall('com.example.exports')
    assert presets.get(plugin_id) is None
    assert presets.get('personal') is not None


def test_explicit_preset_storage_does_not_load_plugins(tmp_path):
    presets = ExportPresetManager(tmp_path / 'isolated.json')
    assert [p.preset_id for p in presets.all()] == ['discord']
