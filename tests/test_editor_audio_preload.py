"""A prepared multitrack mixer must stay silent and survive the editor handoff."""

import subprocess

import pytest
from PySide6.QtMultimedia import QMediaPlayer

from core.ffmpeg_playback import PlaybackError, _load_bridge
from core.ffmpeg_tools import get_ffmpeg_exe, software_h264_encoder
from ui import editor_preloader
from ui.clip_viewer import ClipViewer


@pytest.fixture
def prepared(qtbot, tmp_path, monkeypatch):
    try:
        _load_bridge()
    except PlaybackError:
        pytest.skip('Native playback mixer is not built on this host')
    ffmpeg = get_ffmpeg_exe()
    path = tmp_path / 'multitrack.mp4'
    subprocess.run([ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i',
                    'color=blue:size=320x180:rate=30', '-f', 'lavfi', '-i',
                    'sine=frequency=300:sample_rate=48000', '-f', 'lavfi', '-i',
                    'sine=frequency=600:sample_rate=48000', '-t', '3',
                    '-map', '0:v', '-map', '1:a', '-map', '2:a',
                    '-c:v', software_h264_encoder(ffmpeg), '-c:a', 'aac',
                    '-movflags', '+faststart', str(path)],
                   check=True, capture_output=True, timeout=20)
    monkeypatch.setattr(editor_preloader, 'prepare_compatibility_path', lambda path, token: path)
    loader = editor_preloader.EditorPreloader()
    loader.request(str(path))
    qtbot.waitUntil(lambda: loader._ready is not None and loader._ready._mixer is not None,
                    timeout=8000)
    warm = loader._ready
    qtbot.waitUntil(lambda: warm._mixer.preparation_result is not None, timeout=8000)
    assert warm._mixer.preparation_result == (True, '')
    qtbot.waitUntil(lambda: warm._player.mediaStatus() == QMediaPlayer.MediaStatus.LoadedMedia,
                    timeout=8000)
    yield loader, warm, path
    loader.clear()
    warm.dispose()


def test_multitrack_decoder_prepared_silently_and_adopted(prepared, qtbot):
    loader, warm, path = prepared
    mixer = warm._mixer
    assert mixer._sink is None
    assert mixer._queue.frames == 0
    assert not mixer._worker._playing
    assert loader.take(path) is warm
    viewer = ClipViewer(str(path), None, autoplay=False, preloaded_media=warm)
    qtbot.addWidget(viewer)
    try:
        qtbot.waitUntil(lambda: viewer._audio_preparation_ready, timeout=5000)
        assert viewer._audio_mixer is mixer
        assert viewer._audio_mixer_live
        assert len(viewer._playback_sources) == 2
        assert not mixer._worker._playing
        viewer.start_autoplay()
        qtbot.waitUntil(lambda: viewer.player.playbackState()
                        == QMediaPlayer.PlaybackState.PlayingState, timeout=5000)
    finally:
        viewer._teardown_player()


def test_unused_multitrack_preload_retires_decoder_without_blocking(prepared, qtbot):
    loader, warm, path = prepared
    mixer = warm._mixer
    loader.clear()
    assert mixer._stopping
    assert warm._mixer is None
    qtbot.waitUntil(lambda: not mixer._worker.is_alive(), timeout=5000)


def test_changed_limiter_does_not_reuse_wrong_audio_graph(prepared, qtbot):
    loader, warm, path = prepared
    mixer = warm._mixer
    loader.take(path)
    viewer = ClipViewer(str(path), None, autoplay=False, preloaded_media=warm)
    qtbot.addWidget(viewer)
    viewer._limiter_enabled = False
    try:
        qtbot.waitUntil(lambda: viewer._audio_preparation_ready, timeout=5000)
        assert viewer._audio_mixer is not mixer
        assert mixer._stopping
    finally:
        viewer._teardown_player()
