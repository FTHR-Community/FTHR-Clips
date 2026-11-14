from io import BytesIO
import json
from pathlib import Path
import stat
import sys
import zipfile

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'FTHR_UI'))

from core.plugin_manager import PluginError, PluginManager  # noqa: E402
from core.theme_manager import ThemeManager  # noqa: E402


def _zip(entries) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


def _pack(tmp_path, *, version='1.0.0', theme=None, extra=(), **overrides):
    kind = 'export-presets' if theme is None else 'theme'
    key, filename = ('export_presets', 'presets.json') if theme is None else ('theme', 'theme.zip')
    manifest = {
        'id': 'example.pack', 'name': 'Example Pack', 'version': version,
        'api_version': 1, 'author': 'Example', 'type': kind, 'contributes': {key: filename},
        **overrides,
    }
    payload = (json.dumps([{'preset_id': 'review', 'name': 'Review', 'target_size_mb': 30}])
               if theme is None else theme)
    archive = tmp_path / 'incoming.zip'
    archive.write_bytes(_zip([('manifest.json', json.dumps(manifest)), (filename, payload), *extra]))
    return archive


def test_presets_update_and_enable_lifecycle(tmp_path):
    root = tmp_path / 'installed'
    manager = PluginManager(root)
    archive = _pack(tmp_path)
    assert manager.inspect_archive(archive)['version'] == '1.0.0'
    assert not root.exists()
    assert manager.install(archive)['enabled']
    assert manager.export_presets()[0].preset_id == 'plugin:example.pack:review'
    manager.set_enabled('example.pack', False)
    assert manager.export_presets() == ()
    assert not manager.install(_pack(tmp_path, version='1.1.0'))['enabled']
    with pytest.raises(PluginError, match='newer'):
        manager.install(_pack(tmp_path))
    assert PluginManager(root).list_plugins()[0]['version'] == '1.1.0'
    manager.set_enabled('example.pack', True)
    assert manager.export_presets()[0].target_size_mb == 30
    manager.uninstall('example.pack')
    assert not manager.list_plugins()
    assert manager.export_presets() == ()


def test_package_description_survives_inspection_and_install(tmp_path):
    manager = PluginManager(tmp_path / 'installed')
    archive = _pack(tmp_path, description='Captures the full selected monitor.')
    assert manager.inspect_archive(archive)['description'] == 'Captures the full selected monitor.'
    manager.install(archive)
    assert manager.list_plugins()[0]['description'] == 'Captures the full selected monitor.'


@pytest.mark.parametrize('description', ['', ' ', 'line\nbreak', 'x' * 1001, 42])
def test_package_rejects_invalid_description(tmp_path, description):
    with pytest.raises(PluginError, match='description'):
        PluginManager(tmp_path / 'installed').inspect_archive(
            _pack(tmp_path, description=description))


def test_failed_replace_leaves_prior_pack_available(tmp_path, monkeypatch):
    manager = PluginManager(tmp_path / 'installed')
    manager.install(_pack(tmp_path))

    def fail_replace(*args):
        raise OSError('simulated failure')

    monkeypatch.setattr('core.plugin_manager.os.replace', fail_replace)
    with pytest.raises(OSError, match='simulated'):
        manager.install(_pack(tmp_path, version='2.0.0'))
    assert manager.list_plugins()[0]['version'] == '1.0.0'
    assert list(manager.root.iterdir()) == [manager.root / 'example.pack.zip']


@pytest.mark.parametrize('manifest, match', [
    ({'id': 'con.vendor'}, 'IDs'),
    ({'id': '../escape'}, 'IDs'),
    ({'version': '0.1-beta'}, 'version'),
    ({'type': 'python'}, 'type'),
    ({'api_version': True}, 'API'),
    ({'contributes': {'export_presets': '../presets.json'}}, 'ZIP root'),
])
def test_invalid_manifest(tmp_path, manifest, match):
    manager = PluginManager(tmp_path / 'installed')
    with pytest.raises(PluginError, match=match):
        manager.inspect_archive(_pack(tmp_path, **manifest))


@pytest.mark.parametrize('name, match', [
    ('../escape', 'Unsafe'),
    ('C:/absolute', 'Unsafe'),
    ('manifest.json.', 'Unsafe'),
    ('Presets.JSON', 'Duplicate'),
    ('run.py', 'declared contribution'),
])
def test_unsafe_archive_names(tmp_path, name, match):
    manager = PluginManager(tmp_path / 'installed')
    with pytest.raises(PluginError, match=match):
        manager.install(_pack(tmp_path, extra=[(name, b'payload')]))


def test_symlink_and_unpacked_size_rejected(tmp_path, monkeypatch):
    link = zipfile.ZipInfo('shortcut')
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    manager = PluginManager(tmp_path / 'installed')
    with pytest.raises(PluginError, match='ordinary'):
        manager.install(_pack(tmp_path, extra=[(link, b'target')]))
    archive = _pack(tmp_path, extra=[('large', b'x' * 8192)])
    monkeypatch.setattr('core.plugin_manager._MAX_ARCHIVE_BYTES', 4096)
    with pytest.raises(PluginError, match='unpacked size'):
        manager.install(archive)


@pytest.mark.parametrize('config, entries, match', [
    ({'icons': {'clip.png': '../escaped.png'}}, [], 'Unsafe'),
    ({'icons': {'clip.png': 'missing.png'}}, [], 'Missing'),
    ({'colors': {'ACCENT': '#000000; color: red'}}, [], 'hex color'),
    ({}, [('fonts/../../escape.ttf', b'')], 'Unsafe'),
    ({'icons': {'clip.png': 'clip.png'}}, [('icons/clip.png', b'<svg/>')], 'content'),
])
def test_nested_theme_validation(tmp_path, config, entries, match):
    manager = PluginManager(tmp_path / 'installed')
    theme = _zip([('theme.json', json.dumps(config)), *entries])
    with pytest.raises(PluginError, match=match):
        manager.install(_pack(tmp_path, theme=theme))


def test_theme_apply_is_explicit_and_leaves_editable_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(ThemeManager, '_instance', None)
    theme = ThemeManager()
    theme.set_color('ACCENT', '#abcdef')
    theme.save()
    theme_pack = _zip([('theme.json', '{"colors":{"ACCENT":"#badbee"}}')])
    manager = PluginManager()
    manager.install(_pack(tmp_path, theme=theme_pack))
    assert theme.get_color('ACCENT') == '#abcdef'
    manager.set_enabled('example.pack', False)
    with pytest.raises(PluginError, match='enabled'):
        manager.apply_theme('example.pack')
    manager.set_enabled('example.pack', True)
    manager.apply_theme('example.pack')
    assert theme.get_color('ACCENT') == '#badbee'
    manager.uninstall('example.pack')
    assert theme.get_color('ACCENT') == '#badbee'


def test_failed_theme_apply_restores_previous_theme(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(ThemeManager, '_instance', None)
    theme = ThemeManager()
    theme.set_color('ACCENT', '#abcdef')
    theme.save()
    import_theme = theme.import_theme

    def fail_incoming(path):
        if path.name == 'theme.zip':
            theme.set_color('ACCENT', '#badbee')
            return False
        return import_theme(path)

    monkeypatch.setattr(theme, 'import_theme', fail_incoming)
    manager = PluginManager()
    manager.install(_pack(tmp_path, theme=_zip([('theme.json', '{}')])))
    with pytest.raises(PluginError, match='restored'):
        manager.apply_theme('example.pack')
    assert theme.get_color('ACCENT') == '#abcdef'


def test_apply_reports_damaged_installed_theme(tmp_path):
    manager = PluginManager(tmp_path / 'installed')
    archive = _pack(tmp_path, theme=_zip([('theme.json', '{}')]))
    manifest = manager.install(archive)
    manifest.pop('enabled')
    damaged = _zip([('manifest.json', json.dumps(manifest)), ('theme.zip', b'damaged')])
    (manager.root / 'example.pack.zip').write_bytes(damaged)
    with pytest.raises(PluginError, match='theme ZIP is damaged'):
        manager.apply_theme('example.pack')


@pytest.mark.parametrize('filename', ['amber-theme.zip', 'review-presets.zip'])
def test_example_packs_install(tmp_path, filename):
    sample = Path(__file__).resolve().parents[1] / 'examples' / 'plugins' / filename
    manager = PluginManager(tmp_path)
    assert manager.install(sample)['enabled']


def test_failed_theme_save_restores_previous_theme_on_disk(tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(ThemeManager, '_instance', None)
    theme = ThemeManager()
    theme.set_color('ACCENT', '#abcdef')
    assert theme.save()
    manager = PluginManager()
    incoming = _zip([('theme.json', '{"colors":{"ACCENT":"#badbee"}}')])
    manager.install(_pack(tmp_path, theme=incoming))
    replace = os.replace
    failed = False

    def fail_first_theme_save(source, destination):
        nonlocal failed
        if Path(destination).name == 'theme.json' and not failed:
            failed = True
            raise OSError('simulated failure')
        return replace(source, destination)

    monkeypatch.setattr(os, 'replace', fail_first_theme_save)
    with pytest.raises(PluginError, match='restored'):
        manager.apply_theme('example.pack')
    assert failed
    assert theme.get_color('ACCENT') == '#abcdef'
    config = json.loads((tmp_path / '.fthr' / 'theme' / 'theme.json').read_text(encoding='utf-8'))
    assert config['colors']['ACCENT'] == '#abcdef'
