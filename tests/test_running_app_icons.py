"""Regression coverage for app icons on recorded audio stems."""
import pytest
from PySide6.QtGui import QColor, QIcon, QPixmap
from core import running_apps


@pytest.fixture
def icon_state(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(running_apps, '_paths', {})
    monkeypatch.setattr(running_apps, '_icons', {})
    monkeypatch.setattr(running_apps, '_last_scan', 0.0)
    monkeypatch.setattr(running_apps, '_process_inventory', lambda: iter(()))
    monkeypatch.setattr(running_apps, '_icon_cache_path', lambda identity: tmp_path / (identity + '.png'))
    monkeypatch.setattr(running_apps.sys, 'platform', 'win32')
    pixmap = QPixmap(32, 32)
    pixmap.fill(QColor('#21bbaa'))
    return QIcon(pixmap)


def test_unknown_app_has_no_computer_icon_and_is_not_cached(icon_state):
    assert running_apps.application_icon('missing', fallback=False).isNull()
    assert 'missing' not in running_apps._icons


def test_icon_survives_app_exit_and_editor_restart(icon_state, monkeypatch):
    monkeypatch.setattr(running_apps, '_process_inventory', lambda: iter([
        (42, 'Helium.exe', r'C:\Apps\Helium.exe')]))
    monkeypatch.setattr(running_apps, '_windows_executable_icon', lambda path: icon_state)
    first = running_apps.application_icon('helium', fallback=False).pixmap(24, 24).toImage()
    running_apps._paths.clear()
    running_apps._icons.clear()
    monkeypatch.setattr(running_apps, '_last_scan', 0.0)
    monkeypatch.setattr(running_apps, '_process_inventory', lambda: iter(()))
    saved = running_apps.application_icon('helium', fallback=False).pixmap(24, 24).toImage()
    assert not saved.isNull()
    assert saved.pixelColor(12, 12) == first.pixelColor(12, 12)


def test_missing_app_can_be_resolved_after_launch(icon_state, monkeypatch):
    assert running_apps.application_icon('overwatch', fallback=False).isNull()
    monkeypatch.setattr(running_apps, '_last_scan', 0.0)
    monkeypatch.setattr(running_apps, '_process_inventory', lambda: iter([
        (42, 'Overwatch.exe', r'C:\Games\Overwatch.exe')]))
    monkeypatch.setattr(running_apps, '_windows_executable_icon', lambda path: icon_state)
    assert not running_apps.application_icon('overwatch', fallback=False).isNull()


def test_failed_extraction_does_not_cache_generic_shell_icon(icon_state, monkeypatch):
    running_apps._paths['noicon'] = r'C:\Apps\noicon.exe'
    monkeypatch.setattr(running_apps, '_windows_executable_icon', lambda path: QIcon())
    assert running_apps.application_icon('noicon', fallback=False).isNull()
    assert 'noicon' not in running_apps._icons
