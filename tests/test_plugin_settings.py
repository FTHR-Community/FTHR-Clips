"""Exercise plugin controls with temporary packages and real persistence."""
import io
import json
from pathlib import Path
import shutil
import zipfile

import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from core.plugin_manager import PluginManager
from core.theme_manager import ThemeManager
from ui.plugin_settings import PluginSettingsWidget


def test_remove_dialog_has_themed_buttons_and_safe_keyboard_default(qtbot):
    from ui.plugin_settings import RemovePluginDialog
    from PySide6.QtWidgets import QDialog

    dialog = RemovePluginDialog('<b>Gary Mode</b>')
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert dialog.findChild(QPushButton, 'fthrDialogClose') is not None
    assert dialog.cancel_button.isVisible() and dialog.remove_button.isVisible()
    assert 'border' in dialog.remove_button.styleSheet()
    assert dialog.cancel_button.isDefault()
    assert any(label.text() == 'Remove <b>Gary Mode</b>?'
               and label.textFormat() == Qt.TextFormat.PlainText
               for label in dialog.findChildren(QLabel))
    qtbot.keyClick(dialog.cancel_button, Qt.Key.Key_Return)
    assert dialog.result() == QDialog.DialogCode.Rejected
    dialog.show()
    qtbot.keyClick(dialog, Qt.Key.Key_Escape)
    assert dialog.result() == QDialog.DialogCode.Rejected
    dialog.show()
    dialog.remove_button.click()
    assert dialog.result() == QDialog.DialogCode.Accepted


@pytest.fixture(autouse=True)
def isolate_theme(monkeypatch, tmp_path):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setattr(ThemeManager, '_instance', None)
    assert ThemeManager()._config_file == tmp_path / '.fthr' / 'theme' / 'theme.json'


def _package(path, *, kind='export-presets', name='Review pack', version='1.0.0', description=None):
    if kind == 'theme':
        contribution, member = 'theme', 'theme.zip'
        content = io.BytesIO()
        with zipfile.ZipFile(content, 'w') as theme:
            theme.writestr('theme.json', '{"colors": {"ACCENT": "#66ccaa"}}')
        data = content.getvalue()
    else:
        contribution, member = 'export_presets', 'presets.json'
        data = json.dumps([{'preset_id': 'small', 'name': 'Small', 'target_size_mb': 8}])
    manifest = {
        'id': 'test.review', 'api_version': 1, 'type': kind, 'name': name,
        'author': 'Test creator', 'version': version, 'contributes': {contribution: member},
    }
    with zipfile.ZipFile(path, 'w') as archive:
        if description is not None:
            manifest['description'] = description
        archive.writestr('manifest.json', json.dumps(manifest))
        archive.writestr(member, data)
    return path


def _button(widget, accessible_name):
    return next(button for button in widget.findChildren(QPushButton)
                if button.isVisible() and button.accessibleName() == accessible_name)


def test_description_is_visible_before_install_and_on_installed_row(qtbot, tmp_path):
    description = 'Captures the full monitor, including <other windows>, with the cursor.'
    manager = PluginManager(tmp_path / 'installed')
    widget = PluginSettingsWidget(manager=manager)
    qtbot.addWidget(widget)
    widget.show()
    widget._review_archive(_package(tmp_path / 'pack.zip', description=description))
    assert description in widget._preview_details.text()
    assert widget._preview_details.textFormat() == Qt.TextFormat.PlainText
    assert manager.list_plugins() == []
    widget.install_button.click()
    assert any(label.text() == description and label.isVisible()
               and label.textFormat() == Qt.TextFormat.PlainText
               for label in widget.findChildren(QLabel))


def test_install_update_disable_and_remove_export_pack(qtbot, monkeypatch, tmp_path):
    from ui import plugin_settings

    manager = PluginManager(tmp_path / 'installed')
    selected = _package(tmp_path / 'pack.zip')
    monkeypatch.setattr(plugin_settings.QFileDialog, 'getOpenFileName',
                        lambda *_: (str(selected), ''))
    widget = PluginSettingsWidget(manager=manager)
    qtbot.addWidget(widget)
    widget.show()
    assert any(label.text() == 'No plugins installed.' for label in widget.findChildren(QLabel))

    widget.choose_button.click()
    assert widget.install_button.isVisible()
    assert 'Test creator' in widget._preview_details.text()
    assert manager.list_plugins() == []
    widget.cancel_button.click()
    assert not widget.install_button.isVisible()

    widget.choose_button.click()
    with qtbot.waitSignal(widget.presets_changed):
        widget.install_button.click()
    assert PluginManager(manager.root).list_plugins()[0]['enabled']
    with qtbot.waitSignal(widget.presets_changed):
        _button(widget, 'Disable Review pack').click()
    assert not manager.list_plugins()[0]['enabled']
    assert manager.export_presets() == ()

    selected = _package(tmp_path / 'updated.zip', version='1.1.0')
    widget.choose_button.click()
    assert widget.install_button.text() == 'Update plugin'
    assert 'Installed version: 1.0.0' in widget._preview_details.text()
    with qtbot.waitSignal(widget.presets_changed):
        widget.install_button.click()
    assert not manager.list_plugins()[0]['enabled']
    _button(widget, 'Enable Review pack').click()
    assert manager.export_presets()[0].name == 'Small'

    monkeypatch.setattr(plugin_settings.RemovePluginDialog, 'exec',
                        lambda *_: plugin_settings.QDialog.DialogCode.Accepted)
    with qtbot.waitSignal(widget.presets_changed):
        _button(widget, 'Remove Review pack').click()
    assert not manager.list_plugins()


def test_invalid_archive_and_explicit_theme_apply(qtbot, monkeypatch, tmp_path):
    from ui import plugin_settings

    theme_config = tmp_path / '.fthr' / 'theme' / 'theme.json'
    selected = tmp_path / 'invalid.zip'
    selected.write_text('not an archive', encoding='utf-8')
    monkeypatch.setattr(plugin_settings.QFileDialog, 'getOpenFileName',
                        lambda *_: (str(selected), ''))
    manager = PluginManager()
    widget = PluginSettingsWidget(manager=manager)
    qtbot.addWidget(widget)
    widget.show()
    widget.choose_button.click()
    assert 'Could not read invalid.zip' in widget.status.text()
    assert not widget.install_button.isVisible()

    selected = _package(tmp_path / 'theme.zip', kind='theme', name='Mint')
    widget.choose_button.click()
    widget.install_button.click()
    assert 'is installed' in widget.status.text()
    assert manager.list_plugins()[0]['enabled']
    with qtbot.waitSignal(widget.theme_applied):
        _button(widget, 'Apply Mint theme').click()
    assert 'saved in Customize' in widget.status.text()
    assert json.loads(theme_config.read_text(encoding='utf-8'))['colors']['ACCENT'] == '#66ccaa'
    _button(widget, 'Disable Mint').click()
    assert not _button(widget, 'Apply Mint theme').isEnabled()
    assert any('leaves that theme in place' in label.text() for label in widget.findChildren(QLabel))


def _drag_events(urls):
    mime = QMimeData()
    mime.setUrls(urls)
    args = Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
    # Keep mime alive alongside both Qt events.
    return mime, QDragEnterEvent(QPoint(12, 12), *args), QDropEvent(QPointF(12, 12), *args)


def test_drop_test_zip_previews_before_installing(qtbot, monkeypatch, tmp_path):
    from ui import plugin_settings

    sample = Path(__file__).resolve().parents[1] / 'examples' / 'plugins' / 'fthr-test-plugin.zip'
    path = tmp_path / 'test.ZIP'
    shutil.copyfile(sample, path)
    manager = PluginManager(tmp_path / 'installed')
    widget = PluginSettingsWidget(manager=manager)
    qtbot.addWidget(widget)
    widget.show()
    mime, drag, drop = _drag_events([QUrl.fromLocalFile(str(path))])

    QApplication.sendEvent(widget.drop_area, drag)
    assert drag.isAccepted()
    assert widget.drop_area.property('dragActive')
    QApplication.sendEvent(widget.drop_area, QDragLeaveEvent())
    assert not widget.drop_area.property('dragActive')

    mime, drag, drop = _drag_events([QUrl.fromLocalFile(str(path))])
    QApplication.sendEvent(widget.drop_area, drag)
    move = QDragMoveEvent(
        widget.choose_button.geometry().center(), Qt.DropAction.CopyAction, mime,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget.drop_area, move)
    assert move.isAccepted()
    QApplication.sendEvent(widget.drop_area, drop)
    assert drop.isAccepted()
    assert not widget.drop_area.property('dragActive')
    assert widget.install_button.isVisible()
    assert widget._preview_name.text() == 'FTHR Test Plugin'
    assert 'Test plugin' in widget._preview_details.text()
    assert 'does not change your settings' in widget._preview_details.text()
    assert not manager.root.exists()

    widget.install_button.click()
    assert manager.list_plugins()[0]['type'] == 'test'
    assert manager.export_presets() == ()
    assert not (tmp_path / '.fthr' / 'theme' / 'theme.json').exists()
    _button(widget, 'Disable FTHR Test Plugin').click()
    assert not manager.list_plugins()[0]['enabled']
    _button(widget, 'Enable FTHR Test Plugin').click()
    assert manager.list_plugins()[0]['enabled']
    monkeypatch.setattr(plugin_settings.RemovePluginDialog, 'exec',
                        lambda *_: plugin_settings.QDialog.DialogCode.Accepted)
    _button(widget, 'Remove FTHR Test Plugin').click()
    assert manager.list_plugins() == []


@pytest.mark.parametrize('source', ['remote', 'network', 'multiple', 'wrong extension', 'directory'])
def test_drop_rejects_other_inputs(qtbot, tmp_path, source):
    path = _package(tmp_path / 'plugin.zip')
    archive = QUrl.fromLocalFile(str(path))
    text_file = tmp_path / 'readme.txt'
    text_file.touch()
    directory = tmp_path / 'folder.zip'
    directory.mkdir()
    urls = {
        'remote': [QUrl('https://example.invalid/plugin.zip')],
        'network': [QUrl('file://server/share/plugin.zip')],
        'multiple': [archive, archive],
        'wrong extension': [QUrl.fromLocalFile(str(text_file))],
        'directory': [QUrl.fromLocalFile(str(directory))],
    }[source]
    manager = PluginManager(tmp_path / 'installed')
    widget = PluginSettingsWidget(manager=manager)
    qtbot.addWidget(widget)
    widget.show()
    mime, drag, drop = _drag_events(urls)

    QApplication.sendEvent(widget.drop_area, drag)
    QApplication.sendEvent(widget.drop_area, drop)
    assert not drag.isAccepted()
    assert not drop.isAccepted()
    assert not widget.drop_area.property('dragActive')
    assert not widget.install_button.isVisible()
    assert not manager.root.exists()
