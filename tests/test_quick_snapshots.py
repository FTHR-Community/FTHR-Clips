import pytest
from pathlib import Path

from ui.quick_video_preview import quick_preview_supported


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
            window.replace_clip(str(clips[index % 2]))
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
