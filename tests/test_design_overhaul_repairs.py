from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'FTHR_UI'))

from PySide6.QtWidgets import QLayout
from PySide6.QtWidgets import QLabel

from core.settings_manager import SettingsManager
from ui.clip_viewer import ClipViewer
import main
from main import MainWindow, SourcePopup, _SettingsPage


class _MemorySettings:
    def __init__(self, **values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value

    def save_settings(self):
        pass


def test_source_popup_is_constrained_to_visible_content(
        qtbot, monkeypatch):
    monkeypatch.setattr('main.enumerate_windows_monitors', lambda: [])
    monkeypatch.setattr('main._enumerate_capturable_windows', lambda: [])
    popup = SourcePopup(_MemorySettings(capture_mode='desktop'))
    qtbot.addWidget(popup)

    assert popup.layout().sizeConstraint() == QLayout.SizeConstraint.SetMinimumSize

    popup.mode_combo.setCurrentIndex(1)
    popup._fit_to_visible_content()
    assert popup.width() >= 300
    assert popup.height() == popup.sizeHint().height()


def test_audio_settings_restore_all_notification_volume_sliders(
        qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    settings = SettingsManager()
    page = _SettingsPage(settings)
    qtbot.addWidget(page)

    assert set(page.sound_sliders) == {
        'clip', 'screenshot', 'error', 'startup',
        'upload_successful', 'upload_failed',
    } | ({'master'} if sys.platform == 'win32' else set())
    assert page.error_notifications_check.isChecked()
    page.error_notifications_check.setChecked(False)
    assert settings.get('error_notifications_enabled') is False
    page.sound_sliders['clip'].setValue(37)
    assert settings.get('sound_volume_clip') == 37
    assert page.sound_value_labels['clip'].text() == '37%'


def test_watermark_forces_export_encode_and_starts_on_export_timeline():
    viewer = type('Viewer', (), {
        'clip_path': 'clip.mp4',
        '_audio_tracks': (),
        '_playback_sources': (),
        'sm': _MemorySettings(watermark_enabled=True),
    })()

    command = ClipViewer._build_export_cmd(
        viewer, 'ffmpeg', 18.0, 5.0, 'out.mp4', None,
        ['-c:v', 'libopenh264'])
    joined = ' '.join(command)

    assert '-ss 18.0' in joined
    assert '-filter_complex' in command
    assert "text='CAPTURED WITH'" in joined
    assert "text='FTHRClips'" in joined
    assert 'assets/favicon.ico' in joined
    assert "y='H-h-24'" in joined
    assert "enable='between(t,0,2.9)'" in joined
    assert command[command.index('-map') + 1] == '[vout]'


def test_watermark_disabled_keeps_no_edit_stream_copy():
    viewer = type('Viewer', (), {
        'clip_path': 'clip.mp4',
        '_audio_tracks': (),
        '_playback_sources': (),
        'sm': _MemorySettings(watermark_enabled=False),
    })()

    command = ClipViewer._build_export_cmd(
        viewer, 'ffmpeg', 0.0, 5.0, 'out.mp4', None,
        ['-c:v', 'libopenh264'])

    assert '-filter_complex' not in command
    assert command[-2:] == ['copy', 'out.mp4']


def test_top_bar_uses_the_bundled_brand_logo(monkeypatch):
    monkeypatch.setattr(
        main, 'ThemeManager',
        lambda: type('Theme', (), {
            'get_custom_icon_path': lambda _self, _name: None,
        })(),
    )
    host = type('LogoHost', (), {'_logo_label': QLabel()})()

    MainWindow._load_logo(host)

    assert not host._logo_label.pixmap().isNull()
    assert host._logo_label.text() == ''
