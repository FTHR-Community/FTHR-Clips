import pytest
from pathlib import Path
from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QWidget

from ui.quick_video_preview import quick_preview_supported
from ui.shell_transitions import SnapshotSlide, SnapshotFlight


def _parent(qtbot):
    if not quick_preview_supported():
        pytest.skip('Native hardware compositor test')
    parent = QWidget()
    qtbot.addWidget(parent)
    parent.resize(200, 100)
    parent.setStyleSheet('background: green')
    parent.show()
    return parent


def _pixmap(color):
    image = QPixmap(200, 100)
    image.fill(QColor(color))
    return image


def test_gpu_slide_matches_positions_and_reuses_surface(qtbot):
    parent = _parent(qtbot)
    slide = SnapshotSlide(parent, _pixmap('red'), _pixmap('blue'), 1, horizontal=True)
    slide.show()
    slide.raise_()
    surface = slide._surface
    assert surface is not None
    slide._advance(.5)
    qtbot.wait(100)
    image = parent.grab().toImage()
    assert image.pixelColor(image.width() // 4, image.height() // 2) == QColor('red')
    assert image.pixelColor(image.width() * 3 // 4, image.height() // 2) == QColor('blue')
    slide.hide()
    assert not surface._keys
    second = SnapshotSlide(parent, _pixmap('blue'), _pixmap('red'), -1, horizontal=True)
    second.show()
    assert second._surface is surface
    surface._fail()
    assert second._surface is None
    second._advance(.5)
    qtbot.wait(20)
    fallback = parent.grab().toImage()
    assert fallback.pixelColor(fallback.width() // 4, fallback.height() // 2) == QColor('red')
    assert fallback.pixelColor(fallback.width() * 3 // 4, fallback.height() // 2) == QColor('blue')


def test_gpu_restore_preserves_transparency_and_releases_image(qtbot):
    parent = _parent(qtbot)
    flight = SnapshotFlight(parent, _pixmap('red'), QRect(0, 0, 20, 10), QRect(0, 0, 200, 100))
    qtbot.addWidget(flight)
    flight.show()
    surface = flight._surface
    assert surface is not None
    surface.set_flight(QRect(20, 20, 100, 60), .5)
    qtbot.wait(100)
    image = surface.grabFramebuffer()
    center = image.pixelColor(image.width() // 4, image.height() // 2)
    assert center.red() > 245 and 120 <= center.alpha() <= 136
    assert image.pixelColor(0, 0).alpha() == 0
    flight.hide()
    assert flight._pixmap.isNull() and not surface._keys


def test_destroying_unshown_surface_releases_provider_images(qtbot):
    from ui.quick_snapshot import SnapshotSurface
    parent = _parent(qtbot)
    surface = SnapshotSurface(parent)
    surface.set_images(_pixmap('red'), _pixmap('blue'))
    provider, keys = surface._provider, tuple(surface._keys)
    assert all(key in provider.images for key in keys)
    surface.deleteLater()
    qtbot.waitUntil(lambda: all(key not in provider.images for key in keys))


def test_real_playback_survives_rapid_switches_on_reused_gpu_surface(qtbot, monkeypatch, tmp_path):
    import subprocess
    from core.ffmpeg_tools import get_ffmpeg_exe
    from ui.clip_editor_window import ClipEditorWindow
    from ui import clip_grid
    from PySide6.QtMultimedia import QMediaPlayer
    if not quick_preview_supported():
        pytest.skip('Native hardware playback test')
    monkeypatch.setattr(clip_grid, 'THUMB_CACHE_DIR', str(tmp_path / 'thumbnails'))
    clips = [tmp_path / 'red.mp4', tmp_path / 'blue.mp4']
    for clip, color in zip(clips, ('red', 'blue'), strict=True):
        subprocess.run([
            get_ffmpeg_exe(), '-v', 'error', '-f', 'lavfi', '-i',
            f'color=c={color}:size=1280x720:rate=60', '-t', '4',
            '-c:v', 'libopenh264', '-pix_fmt', 'yuv420p', '-g', '30', str(clip),
        ], check=True, timeout=20)
    window = ClipEditorWindow(str(clips[0]), None)
    qtbot.addWidget(window)
    window.resize(1100, 700)
    window.show()
    surface = window._native_video_widget
    try:
        qtbot.waitUntil(lambda: window._native_has_real_frame, timeout=15000)
        for index in range(8):
            window.replace_clip(str(clips[index % 2]), navigation_step=1)
            qtbot.wait(45)
        qtbot.waitUntil(lambda: window._native_has_real_frame, timeout=15000)
        qtbot.waitUntil(lambda: window.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState)
        qtbot.wait(250)
        assert window._native_video_widget is surface
        assert Path(window.player.source().toLocalFile()) == clips[1]
        frame = surface.grabFramebuffer()
        color = frame.pixelColor(frame.width() // 2, frame.height() // 2)
        assert color.blue() > 200 and color.red() < 40
        window.player.pause()
        window._on_effect_changed('exposure', 30)
        qtbot.wait(100)
        assert surface.gpu_ready
        assert window._native_stats['frames_routed_to_software'] == 0
    finally:
        window._teardown_player(wait_for_workers=False)
