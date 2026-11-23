from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'FTHR_UI'))

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

    popup.show()
    qtbot.waitExposed(popup)
    assert popup.width() == 320

    popup.mode_combo.setCurrentIndex(1)
    popup._fit_to_visible_content()
    assert popup.width() == 320
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
    assert 'assets/fthr_logo.png' in joined
    assert 'scale2ref=' in joined
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


def test_cut_export_waits_for_audio_inventory_before_creating_silent_video():
    from types import SimpleNamespace
    viewer = SimpleNamespace(clip_path='video.mp4', _audio_inventory_complete=False)
    args = ('ffmpeg', 0, 9, 'export.mp4', None, ['-c:v', 'libopenh264'])
    cuts = [(0, 2), (7, 9)]
    with pytest.raises(ValueError, match='Audio tracks are still loading'):
        ClipViewer._build_export_cmd(viewer, *args, segments=cuts)
    viewer._audio_inventory_complete = True
    command = ClipViewer._build_export_cmd(viewer, *args, segments=cuts)
    assert 'concat=n=2:v=1:a=0' in ' '.join(command)


@pytest.mark.parametrize('width,height', [(320, 180), (180, 320)])
def test_real_watermark_reads_assets_and_leaves_video_tail_intact(
        tmp_path, monkeypatch, width, height):
    import numpy as np
    from core import capture_card_watermark as mark
    from core.ffmpeg_tools import FFmpegUnavailable, get_ffmpeg_exe

    try:
        ffmpeg = get_ffmpeg_exe()
    except FFmpegUnavailable as error:
        pytest.skip(str(error))
    assets = tmp_path / "Jane's clips, [test]; assets"
    (assets / 'fonts').mkdir(parents=True)
    for name in ('fthr_logo.png', 'fonts/Oswald-Bold.ttf', 'fonts/Oswald-Regular.ttf'):
        shutil.copyfile(mark._ASSETS / name, assets / name)
    monkeypatch.setattr(mark, '_ASSETS', assets)
    graph = ';'.join(mark.capture_card_watermark_filters('0:v'))
    result = subprocess.run(
        [ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
         f'color=c=0x347258:s={width}x{height}:r=5:d=4', '-filter_complex', graph,
         '-map', '[vout]', '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'],
        capture_output=True, check=True, timeout=20)
    frames = np.frombuffer(result.stdout, np.uint8).reshape(-1, height, width, 3)
    assert len(frames) == 20
    assert (frames[-1] == frames[0]).all()
    assert np.any(frames[5, height // 2:, width // 2:] != frames[-1, height // 2:, width // 2:])
    assert (frames[5, :height // 2] == frames[-1, :height // 2]).all()


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
