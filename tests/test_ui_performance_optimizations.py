"""Correctness at cache invalidation, cancellation and presentation boundaries."""

import json
import sys
import threading

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor
from PySide6.QtMultimedia import QAudioFormat
from PySide6.QtWidgets import QLabel, QWidget
from shiboken6 import isValid

from core import ffmpeg_playback, media_process, playback_proxy
from ui import clip_viewer
from ui.clip_viewer import TimelineSegment, TrimSlider
from ui.shell_transitions import compact_snapshot


def test_timeline_cached_pixels_match_fresh_render_after_edits_and_undo(qtbot):
    slider = TrimSlider(60_000)
    qtbot.addWidget(slider)
    slider.resize(1200, 116)
    slider.segments = [TimelineSegment(0, .3), TimelineSegment(.3, .6, True),
                       TimelineSegment(.6, 1)]
    slider.set_hide_deleted_segments(True)
    for position, deleted, selected, zoom in ((.1, True, 0, 1), (.7, False, 1, 4),
                                             (.2, True, 2, 2), (.9, False, 0, 1)):
        slider.segments[1].deleted = deleted
        slider.selected_segment = selected
        slider.set_zoom(zoom)
        slider.set_playhead(position)
        cached = slider.grab().toImage()
        background = slider._timeline_background.cacheKey()
        assert cached == slider.grab().toImage()
        assert slider._timeline_background.cacheKey() == background
        slider._timeline_background_key = None
        assert cached == slider.grab().toImage()
        ranges = [(s.start_pct, s.end_pct) for s in slider.segments if not s.deleted]
        total = sum(end - start for start, end in ranges)
        for source in (0, .2, .3, .5, .6, .8, 1):
            expected = sum(max(0, min(source, end) - start) for start, end in ranges) / total
            assert slider._source_to_display(source) == pytest.approx(expected)


def test_compact_snapshot_preserves_children_and_bounds_backing_store(qtbot):
    widget = QWidget()
    qtbot.addWidget(widget)
    widget.resize(2400, 1600)
    widget.setStyleSheet('background: red')
    child = QLabel(widget)
    child.setGeometry(1200, 0, 1200, 1600)
    child.setStyleSheet('background: blue')
    snapshot = compact_snapshot(widget)
    logical_size = snapshot.size() / snapshot.devicePixelRatioF()
    assert logical_size.width() <= 560 and logical_size.height() <= 350
    image = snapshot.toImage()
    assert image.pixelColor(image.width() // 4, image.height() // 2) == QColor('red')
    assert image.pixelColor(image.width() * 3 // 4, image.height() // 2) == QColor('blue')


def test_audio_discovery_reuses_validated_results_and_invalidates_sidecar(tmp_path, monkeypatch):
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'first media')
    calls = []
    monkeypatch.setattr(ffmpeg_playback, 'probe_audio_streams',
                        lambda *_: calls.append('probe') or ())
    monkeypatch.setattr(ffmpeg_playback, 'read_manifest_for_media',
                        lambda *_: calls.append('manifest') or None)
    for _ in range(2):
        assert ffmpeg_playback.discover_playback_sources(str(media)) == ()
    assert calls == ['probe', 'manifest']
    sidecar = ffmpeg_playback.manifest_path_for(media)
    sidecar.write_text('{}')
    ffmpeg_playback.discover_playback_sources(str(media))
    assert len(calls) == 4
    sidecar.unlink()
    media.write_bytes(b'changed media')
    ffmpeg_playback.discover_playback_sources(str(media))
    assert len(calls) == 6


def test_cancelled_audio_discovery_cannot_seed_cache(tmp_path, monkeypatch):
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'media')
    cancel = threading.Event()
    calls = []

    def probe(*_):
        calls.append(True)
        cancel.set()
        return ()

    monkeypatch.setattr(ffmpeg_playback, 'probe_audio_streams', probe)
    ffmpeg_playback.discover_playback_sources(str(media), cancel)
    cancel.clear()
    ffmpeg_playback.discover_playback_sources(str(media), cancel)
    assert len(calls) == 2


def test_proxy_negative_decision_is_reused_but_failed_probe_is_retried(tmp_path, monkeypatch):
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'media')
    monkeypatch.setattr(playback_proxy, 'get_ffprobe_exe', lambda: 'ffprobe')
    calls = []

    def run(*_, **__):
        calls.append(True)
        if len(calls) == 1:
            return None
        return 0, json.dumps({'format': {'duration': '60'}, 'packets': [
            {'pts_time': str(i), 'flags': 'K_'} for i in range(8)]}).encode(), b''

    monkeypatch.setattr(playback_proxy, 'run_media_process', run)
    for _ in range(3):
        assert playback_proxy.prepare_playback_path(str(media), threading.Event()) == str(media)
    assert len(calls) == 2
    media.write_bytes(b'changed media')
    playback_proxy.prepare_playback_path(str(media), threading.Event())
    assert len(calls) == 3


def test_cancelled_filmstrip_resumes_completed_samples(qtbot, tmp_path, monkeypatch):
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'media')
    slider = TrimSlider(10_000)
    qtbot.addWidget(slider)
    commands = []
    finished = threading.Event()

    def run(command, cancel, *, on_stdout, **_):
        commands.append(command)
        if len(commands) == 1:
            on_stdout(bytes((255, 0, 0)) * (180 * 100 * 2))
            cancel.set()
            finished.set()
            return None
        on_stdout(bytes((0, 255, 0)) * (180 * 100 * 2))
        return 0, b'', b''

    monkeypatch.setattr(media_process, 'run_media_process', run)
    monkeypatch.setattr(clip_viewer, 'get_ffmpeg_exe', lambda: 'ffmpeg')
    slider.load_thumbnails(str(media), 4)
    qtbot.waitUntil(finished.is_set)
    key = clip_viewer._timeline_cache_key(str(media), 4)
    assert clip_viewer._cached_timeline_images(key) is None
    assert len(clip_viewer._cached_timeline_images(key, allow_partial=True)) == 2
    slider.load_thumbnails(str(media), 4)
    qtbot.waitUntil(lambda: len(slider._thumbnails) == 4)
    assert commands[1][commands[1].index('-ss') + 1] == '5.000000000'
    assert commands[1][commands[1].index('-frames:v') + 1] == '2'
    assert slider._thumbnails[0].toImage().pixelColor(0, 0) == QColor('red')
    assert slider._thumbnails[3].toImage().pixelColor(0, 0) == QColor(0, 255, 0)


def test_optional_jobs_do_not_starve_timeline_or_interactive_work(monkeypatch):
    running = threading.Event()
    release = threading.Event()
    calls = []

    def run(command, *_args, **_kwargs):
        calls.append(command[0])
        if command[0] == 'proxy':
            running.set()
            assert release.wait(3)
        return 0, b'ok', b''

    monkeypatch.setattr(media_process, '_run_media_process', run)
    worker = threading.Thread(target=lambda: media_process.run_media_process(
        ['proxy'], threading.Event(), background='proxy'))
    worker.start()
    try:
        assert running.wait(1)
        assert media_process.run_media_process(
            ['timeline'], threading.Event(), background='timeline')[1] == b'ok'
        assert media_process.run_media_process(['seek'], threading.Event())[1] == b'ok'
        cancelled = threading.Event()
        cancelled.set()
        assert media_process.run_media_process(['cancelled'], cancelled, background='proxy') is None
        assert calls == ['proxy', 'timeline', 'seek']
    finally:
        release.set()
        worker.join(3)


def test_streaming_child_is_cancelled_and_reaped_after_partial_output():
    cancel = threading.Event()
    chunks = []

    def received(chunk):
        chunks.append(chunk)
        cancel.set()

    before = media_process.media_process_snapshot()[0]
    result = media_process.run_media_process(
        [sys.executable, '-c',
         'import sys,time; sys.stdout.buffer.write(b"x"*54000); sys.stdout.flush(); time.sleep(10)'],
        cancel, timeout_seconds=2, on_stdout=received, background='timeline')
    assert result is None and chunks == [b'x' * 54000]
    assert media_process.media_process_snapshot()[0] == before


def test_async_audio_stop_keeps_callbacks_alive_until_native_close(qtbot, monkeypatch):
    opening = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    class Mixer:
        def __init__(self, *_):
            opening.set()
            assert release.wait(3)

        def close(self):
            closed.set()

    audio_format = QAudioFormat()
    audio_format.setSampleRate(48_000)
    audio_format.setChannelCount(2)
    audio_format.setSampleFormat(QAudioFormat.SampleFormat.Float)
    monkeypatch.setattr(ffmpeg_playback, 'InProcessFFmpegMixer', Mixer)
    monkeypatch.setattr(ffmpeg_playback.FFmpegPlaybackController, '_select_output_format',
                        lambda *_: (None, audio_format, lambda pcm: pcm))
    parent = QWidget()
    qtbot.addWidget(parent)
    controller = ffmpeg_playback.FFmpegPlaybackController('clip.mp4', (), parent)
    destroyed = []
    controller.destroyed.connect(lambda *_: destroyed.append(closed.is_set()))
    controller.start()
    try:
        assert opening.wait(1)
        controller.stop_async()
        assert isValid(controller) and controller.parent() is None
        assert controller in ffmpeg_playback._RETIRING_AUDIO_CONTROLLERS
        assert not controller._sink_timer.isActive()
        heartbeat = []
        QTimer.singleShot(0, lambda: heartbeat.append(True))
        qtbot.waitUntil(lambda: bool(heartbeat))
        assert not closed.is_set()
        controller._keep_sink_running()  # queued PCM cannot restart the endpoint
    finally:
        release.set()
    qtbot.waitUntil(lambda: bool(destroyed))
    assert destroyed == [True]
    assert controller not in ffmpeg_playback._RETIRING_AUDIO_CONTROLLERS
