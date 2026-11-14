import io
import json
import zipfile

import pytest
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage

from core.plugin_manager import PluginError, PluginManager
from ui.plugin_settings import PluginSettingsWidget, _PluginCover


def png(width=64, height=32):
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(0xff22aa77)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, 'PNG')
    return bytes(buffer.data())


def pack(tmp_path, *, cover='cover.png', data=None, include=True, version='1.0.0'):
    manifest = {'id': 'example.cover', 'name': 'Cover test', 'author': 'Tests',
                'version': version, 'api_version': 1, 'type': 'test', 'contributes': {}}
    if cover is not None:
        manifest['cover'] = cover
    path = tmp_path / 'pack.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('manifest.json', json.dumps(manifest))
        if include and cover is not None:
            archive.writestr(cover, png() if data is None else data)
    return path


def test_cover_survives_toggle_restart_update_and_can_be_removed(tmp_path):
    manager = PluginManager(tmp_path / 'installed')
    path = pack(tmp_path)
    assert manager.archive_cover(path) == png()
    manager.install(path)
    manager.set_enabled('example.cover', False)
    manager = PluginManager(manager.root)
    assert manager.cover_image('example.cover') == png()
    image = png(80, 40)
    manager.install(pack(tmp_path, data=image, version='1.1.0'))
    assert manager.cover_image('example.cover') == image
    assert not manager.list_plugins()[0]['enabled']
    manager.set_enabled('example.cover', True)
    manager.install(pack(tmp_path, cover=None, version='1.2.0'))
    assert manager.cover_image('example.cover') == b''


@pytest.mark.parametrize('options', [
    {'cover': '../cover.png'}, {'cover': 'https://example.com/cover.png'},
    {'cover': 'cover.svg'}, {'cover': 123, 'include': False},
    {'include': False}, {'data': b'not an image'},
    {'data': b'\x89PNG\r\n\x1a\ntruncated'},
    {'data': b'\x89PNG\r\n\x1a\n' + b'0' * (4 * 1024 * 1024)},
    {'data': png(4097, 1)},
])
def test_invalid_covers_cannot_replace_an_installed_package(tmp_path, options):
    manager = PluginManager(tmp_path / 'installed')
    manager.install(pack(tmp_path))
    original = (manager.root / 'example.cover.zip').read_bytes()
    with pytest.raises(PluginError):
        manager.install(pack(tmp_path, **options))
    assert (manager.root / 'example.cover.zip').read_bytes() == original


def test_cover_is_visible_in_review_and_installed_row_with_no_cover_fallback(qtbot, tmp_path):
    manager = PluginManager(tmp_path / 'installed')
    widget = PluginSettingsWidget(manager=manager)
    qtbot.addWidget(widget)
    widget.resize(760, 600)
    widget.show()
    widget._review_archive(pack(tmp_path))
    assert widget._preview_cover.isVisible()
    assert not widget._preview_cover.pixmap().isNull()
    widget._install()
    assert any(cover.isVisible() for cover in widget.findChildren(_PluginCover))
    widget._review_archive(pack(tmp_path, cover=None, version='1.1.0'))
    assert not widget._preview_cover.isVisible()
    widget._install()
    assert manager.cover_image('example.cover') == b''


def test_cover_corrupted_after_install_does_not_hide_remove_controls(qtbot, tmp_path):
    manager = PluginManager(tmp_path / 'installed')
    manager.install(pack(tmp_path))
    path = manager.root / 'example.cover.zip'
    original = zipfile.ZipFile(io.BytesIO(path.read_bytes()))
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('manifest.json', original.read('manifest.json'))
        archive.writestr('cover.png', b'broken')
    widget = PluginSettingsWidget(manager=manager)
    qtbot.addWidget(widget)
    assert 'example.cover' in widget._row_actions
