"""Hardware tests run with QT_QPA_PLATFORM=windows; headless CI tests fallback."""

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtGui import QColor, QImage
from PySide6.QtMultimedia import QVideoFrame

from ui.clip_viewer import ClipViewer, LiveVideoPreview
from ui.quick_video_preview import QuickVideoPreview, quick_preview_supported


def test_gpu_edits_bypass_software_and_failure_restores_fallback():
    surface = SimpleNamespace(gpu_ready=True)
    viewer = SimpleNamespace(
        _native_video_widget=surface, _live_clip_preview=True,
        _crop_rect=(0, 0, 100, 100), _stretch_ratio=1.4,
        _effects={'sharpness': 60, 'exposure': 50})
    assert not ClipViewer._requires_software_video(viewer)
    surface.gpu_ready = False
    assert ClipViewer._requires_software_video(viewer)
    viewer._live_clip_preview = False
    assert not ClipViewer._requires_software_video(viewer)


def _hardware_preview(qtbot):
    if not quick_preview_supported():
        pytest.skip('Hardware Qt Quick scene graph unavailable')
    preview = QuickVideoPreview()
    qtbot.addWidget(preview)
    preview.resize(128, 72)
    preview.show()
    return preview


def test_compact_transition_capture_includes_gpu_video(qtbot):
    from ui.shell_transitions import compact_snapshot
    preview = _hardware_preview(qtbot)
    image = QImage(128, 72, QImage.Format.Format_RGB32)
    image.fill(QColor(30, 110, 210))
    preview.videoSink().setVideoFrame(QVideoFrame(image))
    qtbot.wait(100)
    snapshot = compact_snapshot(preview).toImage()
    color = snapshot.pixelColor(snapshot.width() // 2, snapshot.height() // 2)
    assert max(abs(a - b) for a, b in zip(
        color.getRgb()[:3], (30, 110, 210), strict=True)) <= 3


def test_hardware_shader_matches_cpu_color_math_and_updates_paused_frame(qtbot):
    preview = _hardware_preview(qtbot)
    image = QImage(128, 72, QImage.Format.Format_RGB32)
    for y in range(72):
        for x in range(128):
            image.setPixelColor(x, y, QColor(
                45 if x < 64 else 160, 30 + x, 40 + y))
    sink = preview.videoSink()
    sink.setVideoFrame(QVideoFrame(image))
    for effects in (
        {}, {'exposure': 50}, {'contrast': -80}, {'saturation': -100},
        {'temperature': 70}, {'sharpness': 80},
        {'exposure': -30, 'contrast': 50, 'saturation': 40,
         'temperature': -60, 'sharpness': 40},
    ):
        preview.set_edit_state(None, effects, 1, 128, 72, '#000000', '#00e0c0')
        qtbot.wait(80)
        rendered = preview.grabFramebuffer()
        assert preview.gpu_ready
        assert not rendered.isNull()
        expected = LiveVideoPreview._apply_color_effects(image, effects)
        for x, y in ((20, 20), (63, 36), (64, 36), (100, 50)):
            actual = rendered.pixelColor(
                int(x * rendered.width() / 128), int(y * rendered.height() / 72))
            reference = expected.pixelColor(x, y)
            assert max(abs(a - b) for a, b in zip(
                actual.getRgb()[:3], reference.getRgb()[:3], strict=True)) <= 3
        assert preview.videoSink() is sink

    image.fill(QColor(160, 160, 160))
    sink.setVideoFrame(QVideoFrame(image))
    preview.set_edit_state(None, {'vignette': 100}, 1, 128, 72, '#000000', '#00e0c0')
    qtbot.wait(80)
    vignette_image = preview.grabFramebuffer()
    assert vignette_image.pixelColor(4, 4).red() < vignette_image.pixelColor(64, 36).red()
    preview.set_edit_state((32, 18, 64, 36), {'vignette': 100},
                           1.5, 128, 72, '#000000', '#00e0c0')
    qtbot.wait(80)
    assert preview.rootObject().property('cropActive')
    assert preview.rootObject().property('stretchRatio') == 1.5
    assert preview.gpu_ready


def test_hardware_player_keeps_gpu_output_during_slider_changes(qtbot, monkeypatch, tmp_path):
    import subprocess
    from core.ffmpeg_tools import get_ffmpeg_exe
    from PySide6.QtMultimedia import QMediaPlayer
    from ui import clip_viewer

    if not quick_preview_supported():
        pytest.skip('Hardware Qt Quick scene graph unavailable')
    clip = tmp_path / 'gpu-preview.mp4'
    subprocess.run([
        get_ffmpeg_exe(), '-v', 'error', '-f', 'lavfi', '-i',
        'testsrc2=size=640x360:rate=60', '-t', '4', '-c:v', 'libopenh264',
        '-pix_fmt', 'yuv420p', str(clip),
    ], check=True, timeout=20)
    monkeypatch.setattr(clip_viewer, 'discover_playback_sources', lambda _path, cancelled=None: ())
    viewer = ClipViewer(str(clip), None)
    qtbot.addWidget(viewer)
    viewer.show()
    try:
        qtbot.waitUntil(lambda: viewer._playback_ready, timeout=15000)
        assert isinstance(viewer._native_video_widget, QuickVideoPreview)
        viewer.player.play()
        qtbot.waitUntil(lambda: viewer._native_has_real_frame, timeout=10000)
        player, sink, source = viewer.player, viewer.player.videoSink(), viewer.player.source()
        assert source != QUrl()
        player.play()
        qtbot.waitUntil(lambda: player.playbackState() == QMediaPlayer.PlaybackState.PlayingState)
        before_frames = viewer._native_frame_count
        before_conversions = viewer.video_widget.preview_performance_snapshot()['conversion_count']
        for value in range(-80, 81, 20):
            viewer._on_effect_changed('exposure', value)
            viewer._on_effect_changed('saturation', -value)
            qtbot.wait(80)
        assert viewer.player is player
        assert player.videoSink() is sink
        assert player.source() == source
        assert viewer._native_video_widget.gpu_ready
        assert viewer._native_frame_count > before_frames + 10
        assert viewer._native_stats['frames_routed_to_software'] == 0
        assert viewer.video_widget.preview_performance_snapshot()['conversion_count'] == before_conversions
        assert not viewer.video_widget._software_preview_enabled
        # Fail the GPU while playing: the same sink continues to feed the
        # bounded CPU fallback, including when all effects are reset.
        viewer._native_video_widget._shader_failed()
        viewer._reset_effects()
        qtbot.waitUntil(lambda: viewer.video_widget.preview_performance_snapshot()[
            'conversion_count'] > before_conversions)
        assert viewer._video_renderer == 'software'
        assert viewer.player is player
        assert player.videoSink() is sink
        assert player.source() == source
    finally:
        viewer._teardown_player()
