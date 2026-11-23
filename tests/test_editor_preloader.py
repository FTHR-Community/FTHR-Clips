import subprocess
import threading

import pytest
from PySide6.QtCore import QObject
from PySide6.QtMultimedia import QMediaPlayer

from core.audio_manifest import manifest_path_for
from core.ffmpeg_tools import FFmpegUnavailable, get_ffmpeg_exe, software_h264_encoder
from ui import clip_viewer, editor_preloader
from ui.editor_preloader import EditorPreloader


@pytest.fixture
def video_path(tmp_path):
    try:
        ffmpeg = get_ffmpeg_exe()
    except FFmpegUnavailable:
        pytest.skip('FFmpeg is required for the media handoff test')
    path = tmp_path / 'preload.mp4'
    subprocess.run(
        [ffmpeg, '-v', 'error', '-nostdin', '-y',
         '-f', 'lavfi', '-i', 'color=blue:size=320x180:rate=30',
         '-f', 'lavfi', '-i', 'sine=frequency=300:sample_rate=48000',
         '-t', '3', '-c:v', software_h264_encoder(ffmpeg), '-c:a', 'aac',
         '-movflags', '+faststart', str(path)],
        check=True, capture_output=True, timeout=20,
    )
    return path


def _wait_for_media(preloader, video_path, qtbot):
    preloader.request(video_path)
    qtbot.waitUntil(lambda: preloader._ready is not None, timeout=5000)
    prepared = preloader._ready
    player = prepared._player
    qtbot.waitUntil(lambda: player.mediaStatus() in (
        QMediaPlayer.MediaStatus.LoadedMedia,
        QMediaPlayer.MediaStatus.BufferedMedia,
        QMediaPlayer.MediaStatus.InvalidMedia,
    ), timeout=5000)
    if player.mediaStatus() == QMediaPlayer.MediaStatus.InvalidMedia:
        pytest.skip(f'The test platform cannot open generated H.264/AAC: {player.errorString()}')
    return prepared


@pytest.mark.parametrize('reuse_editor', (False, True))
def test_preload_handoff_keeps_source_and_resumes_normal_autoplay(
        monkeypatch, qtbot, video_path, reuse_editor):
    monkeypatch.setattr(editor_preloader, 'prepare_compatibility_path',
                        lambda path, _cancelled: path)
    preloader = EditorPreloader()
    warmed = _wait_for_media(preloader, video_path, qtbot)
    player = warmed._player
    original_sink = warmed._sink
    assert player.playbackState() == QMediaPlayer.PlaybackState.StoppedState
    assert player.audioOutput().isMuted()

    assert preloader.take(video_path) is warmed
    assert preloader.take(video_path) is None
    set_source_calls = []
    set_source = player.setSource

    def track_set_source(source):
        set_source_calls.append(source)
        set_source(source)

    monkeypatch.setattr(player, 'setSource', track_set_source)
    rediscovery = []

    def discover_again(path, _cancelled):
        rediscovery.append(path)
        return ()

    monkeypatch.setattr(clip_viewer, 'discover_playback_sources', discover_again)
    if reuse_editor:
        previous_path = video_path.with_name('previous.mp4')
        previous_path.write_bytes(video_path.read_bytes())
        viewer = clip_viewer.ClipViewer(str(previous_path), None, embedded=True)
        viewer.load_clip(str(video_path), autoplay=True, preloaded_media=warmed)
    else:
        viewer = clip_viewer.ClipViewer(str(video_path), None, autoplay=True,
                                        preloaded_media=warmed)
    qtbot.addWidget(viewer)
    warmed.dispose()
    viewer.show()
    qtbot.waitUntil(lambda: viewer._media_ready and viewer._playback_ready, timeout=5000)
    qtbot.waitUntil(lambda: player.playbackState() == QMediaPlayer.PlaybackState.PlayingState,
                    timeout=5000)
    render_sink = (viewer._native_video_widget.videoSink() if viewer._native_output_enabled
                   else viewer.video_widget.video_sink)
    qtbot.waitUntil(lambda: render_sink.videoFrame().isValid(), timeout=5000)
    assert viewer.player is player
    assert player.parent() is viewer
    assert player.videoSink() is original_sink
    assert render_sink is not original_sink
    assert not player.audioOutput().isMuted()
    assert not set_source_calls
    assert not rediscovery


@pytest.mark.parametrize('change', ('original', 'manifest', 'preview'))
def test_taken_media_rejects_files_changed_during_hover(monkeypatch, qtbot, video_path,
                                                       change):
    alternate = video_path.with_name('preview.mp4')
    alternate.write_bytes(video_path.read_bytes())
    monkeypatch.setattr(editor_preloader, 'cached_playback_path',
                        lambda *_args: str(alternate))
    preloader = EditorPreloader()
    warmed = _wait_for_media(preloader, video_path, qtbot)
    player = warmed._player
    changed = {'original': video_path, 'manifest': manifest_path_for(video_path),
               'preview': alternate}[change]
    if change == 'preview':
        # Media Foundation can deny writes to an open playback file.
        identity = editor_preloader.file_identity

        def changed_identity(path):
            if str(path) == str(changed):
                raise FileNotFoundError(path)
            return identity(path)

        monkeypatch.setattr(editor_preloader, 'file_identity', changed_identity)
    else:
        with changed.open('ab') as handle:
            handle.write(b'changed during hover')

    assert preloader.take(video_path) is None
    assert player.source().isEmpty()
    assert player.audioOutput() is None
    warmed.dispose()


def test_cancelled_compatibility_inspection_does_not_start_audio(monkeypatch, tmp_path):
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'media')
    request = editor_preloader._Request(str(clip), 'auto', 0.0)
    monkeypatch.setattr(editor_preloader, 'cached_playback_path', lambda path, quality: path)

    def cancel_during_inspection(path, cancelled):
        cancelled.set()
        return path

    monkeypatch.setattr(editor_preloader, 'prepare_compatibility_path', cancel_during_inspection)
    monkeypatch.setattr(editor_preloader, 'discover_playback_sources',
                        lambda *_: pytest.fail('Cancelled preload started another probe'))
    assert editor_preloader._inspect(request) is None


def test_only_latest_queued_probe_is_started(monkeypatch, qtbot, tmp_path):
    started = []
    first_started = threading.Event()
    release_first = threading.Event()
    latest_started = threading.Event()

    def inspect(request):
        started.append(request)
        if len(started) == 1:
            first_started.set()
            release_first.wait(5)
        else:
            latest_started.set()

    monkeypatch.setattr(editor_preloader, '_inspect', inspect)
    parent = QObject()
    preloader = EditorPreloader(parent)
    first, skipped, last = (str(tmp_path / f'{name}.mp4')
                            for name in ('first', 'skipped', 'last'))
    preloader.request(first)
    try:
        qtbot.waitUntil(first_started.is_set)
        preloader.request(first)
        assert not started[0].cancelled.is_set()
        preloader.request(skipped)
        preloader.request(last)
        assert started[0].cancelled.is_set()
        assert len(started) == 1
    finally:
        release_first.set()
    qtbot.waitUntil(latest_started.is_set)
    assert [request.path for request in started] == [first, last]
    preloader.clear()
    qtbot.waitUntil(lambda: preloader._active is None)
    assert preloader._ready is None


def test_destroying_owner_cancels_active_probe(monkeypatch, qtbot, tmp_path):
    started = threading.Event()
    cancelled = threading.Event()

    def inspect(request):
        started.set()
        if request.cancelled.wait(5):
            cancelled.set()

    monkeypatch.setattr(editor_preloader, '_inspect', inspect)
    parent = QObject()
    preloader = EditorPreloader(parent)
    preloader.request(tmp_path / 'clip.mp4')
    qtbot.waitUntil(started.is_set)
    parent.deleteLater()
    qtbot.waitUntil(cancelled.is_set)
