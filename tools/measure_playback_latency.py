"""Measure real Qt frame delivery; run each backend in a fresh process.

Example: python tools/measure_playback_latency.py clip.mp4 --backend windows
The small preview closes automatically. No source media or settings are changed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('clip', type=Path)
    parser.add_argument('--backend', choices=('windows', 'ffmpeg'), default='windows')
    parser.add_argument('--cycles', type=int, default=5)
    parser.add_argument('--muted', action='store_true')
    parser.add_argument('--editor', action='store_true', help='exercise the actual clip editor')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    os.environ['QT_MEDIA_BACKEND'] = args.backend
    from PySide6.QtCore import QUrl
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    from PySide6.QtWidgets import QApplication

    app = QApplication([])
    viewer = None
    if args.editor:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'FTHR_UI'))
        from ui.clip_viewer import ClipViewer
        viewer = ClipViewer(str(args.clip.resolve()), None)
        viewer.show()
        # The editor owns one stable player output. Windows neutral playback
        # uses the native widget sink; edited/non-Windows playback uses the
        # bounded software preview sink without reloading the source.
        if getattr(viewer, '_native_output_enabled', False):
            video_sink = viewer._native_video_widget.videoSink()
        else:
            video_sink = viewer.video_widget.video_sink
        audio = viewer.audio_output
        player = viewer.player
    else:
        video = QVideoWidget()
        video.setWindowTitle('FTHR playback latency check')
        video.resize(640, 360)
        video.show()
        audio = QAudioOutput()
        player = QMediaPlayer()
        player.setVideoOutput(video)
        player.setAudioOutput(audio)
        video_sink = video.videoSink()
    audio.setMuted(args.muted)
    frames = []
    errors = []
    video_sink.videoFrameChanged.connect(
        lambda frame: frames.append((time.perf_counter(), frame.startTime() // 1000))
        if frame.isValid() else None)
    player.errorOccurred.connect(lambda *_: errors.append(player.errorString()))

    def wait_until(predicate, timeout=8):
        deadline = time.perf_counter() + timeout
        while not predicate():
            if time.perf_counter() > deadline:
                raise TimeoutError(
                    f'No frame/state within {timeout}s; errors={errors}; '
                    f'frames={len(frames)} position={player.position()} '
                    f'state={player.playbackState()} status={player.mediaStatus()}')
            app.processEvents()
            time.sleep(0.002)

    results = []
    try:
        if viewer is None:
            player.setSource(QUrl.fromLocalFile(str(args.clip.resolve())))
        else:
            wait_until(lambda: viewer._playback_ready)
            # A multitrack clip may finish preparing its cached playback
            # source shortly after the first media-ready signal. Measure only
            # after that one bounded source transition has settled.
            settled = {'identity': None, 'since': time.perf_counter()}

            def playback_source_settled():
                identity = (
                    player.source().toString(), viewer._playback_path,
                    viewer._prepared_playback_path)
                if (identity != settled['identity']
                        or viewer._playback_source_worker is not None
                        or not viewer._playback_ready):
                    settled['identity'] = identity
                    settled['since'] = time.perf_counter()
                    return False
                return time.perf_counter() - settled['since'] >= 1.0

            wait_until(playback_source_settled, timeout=20)
            def editor_has_first_frame():
                native_ready = bool(
                    getattr(viewer, '_native_has_real_frame', False))
                software_ready = bool(
                    viewer.video_widget.preview_performance_snapshot()[
                        'has_real_frame'])
                return native_ready or software_ready

            wait_until(editor_has_first_frame)
        wait_until(lambda: player.mediaStatus() in (
            QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia))
        player.play()
        wait_until(lambda: len(frames) >= 5)
        for index in range(args.cycles):
            start = time.perf_counter()
            player.pause()
            pause_call = (time.perf_counter() - start) * 1000
            wait_until(lambda: player.playbackState() == QMediaPlayer.PausedState)
            pause_ms = (time.perf_counter() - start) * 1000
            fraction = 0.2 if index % 2 else 0.7
            if viewer is None:
                target = int(player.duration() * fraction)
            else:
                # Respect the editor's current kept ranges; persisted trim
                # state may make an arbitrary percentage a discarded point.
                ranges = viewer._kept_segments_ms()
                start_ms, end_ms = max(
                    ranges or [(0, viewer.duration_ms)],
                    key=lambda pair: pair[1] - pair[0])
                target = int(start_ms + (end_ms - start_ms) * fraction)
            before = len(frames)
            start = time.perf_counter()
            if viewer is None:
                player.setPosition(target)
            else:
                viewer._on_seek_requested(target / viewer.duration_ms)
            seek_call = (time.perf_counter() - start) * 1000
            wait_until(lambda target=target, before=before:
                       abs(player.position() - target) < 300
                       or any(abs(pts - target) < 300
                              for _, pts in frames[before:]))
            seek_ms = (time.perf_counter() - start) * 1000
            seek_frame_observed = any(
                abs(pts - target) < 300 for _, pts in frames[before:])
            before = len(frames)
            start = time.perf_counter()
            if viewer is None:
                player.play()
            else:
                viewer._toggle_play()
            play_call = (time.perf_counter() - start) * 1000
            wait_until(lambda target=target, before=before:
                       any(pts > target + 30 for _, pts in frames[before:]))
            play_ms = (time.perf_counter() - start) * 1000
            results.append(dict(pause_call_ms=pause_call, pause_ms=pause_ms,
                                seek_call_ms=seek_call,
                                seek_settle_ms=seek_ms,
                                seek_frame_observed=seek_frame_observed,
                                play_call_ms=play_call, play_frame_ms=play_ms))
        report = dict(backend=args.backend, editor=args.editor, muted=args.muted,
                      cycles=results, errors=errors)
        if viewer is not None:
            report['render_performance'] = viewer.render_performance_snapshot()
        print(json.dumps(report, indent=2))
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    finally:
        if viewer is not None:
            viewer.close()
        else:
            player.stop()
            player.setSource(QUrl())
            player.setVideoOutput(None)
            player.setAudioOutput(None)
            video.close()
        app.processEvents()


if __name__ == '__main__':
    main()
