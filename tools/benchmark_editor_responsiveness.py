"""Repeatable real-Qt playback/scrub smoke with isolated settings and JSON evidence.

Use --baseline-dir with saved clip_viewer.py/playback_proxy.py to compare revisions.
The benchmark uses generated media and does not alter the user's settings or drafts.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


class Settings:
    def __init__(self, mode):
        self.values = {'editor_preview_quality': mode}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value

    def save_settings(self):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('clip', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline-dir', type=Path)
    parser.add_argument('--software', action='store_true')
    parser.add_argument('--effects', action='store_true')
    parser.add_argument('--quality', default='auto')
    parser.add_argument('--seconds', type=float, default=3)
    parser.add_argument('--prepare-proxy', action='store_true')
    parser.add_argument('--width', type=int, default=1680)
    parser.add_argument('--height', type=int, default=1050)
    parser.add_argument('--exercise-modes', action='store_true')
    args = parser.parse_args()
    os.environ['QT_MEDIA_BACKEND'] = 'windows'
    sys.path.insert(0, str(ROOT / 'FTHR_UI'))
    from PySide6.QtCore import QThreadPool
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    if args.baseline_dir:
        for name, relative in [('core.playback_proxy', 'core/playback_proxy.py'),
                               ('ui.clip_viewer', 'ui/clip_viewer.py')]:
            # Original __file__ preserves relative resource lookup in the saved source.
            spec = importlib.util.spec_from_file_location(name, ROOT / 'FTHR_UI' / relative)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            source = args.baseline_dir / Path(relative).name
            exec(compile(source.read_text(encoding='utf-8'), str(source), 'exec'),
                 module.__dict__)
    import ui.clip_viewer as cv
    from core import playback_proxy
    args.output.parent.mkdir(parents=True, exist_ok=True)
    playback_proxy.CACHE_DIR = args.output.parent / ('cache-' + args.output.stem)
    prepared = None
    prepare_ms = None
    if args.prepare_proxy:
        import threading
        start = time.perf_counter()
        if args.baseline_dir:
            prepared = playback_proxy.prepare_playback_path(
                str(args.clip.resolve()), threading.Event())
        else:
            prepared = playback_proxy.prepare_playback_path(
                str(args.clip.resolve()), threading.Event(), mode=args.quality)
        prepare_ms = (time.perf_counter() - start) * 1000
    if args.software:
        cv.quick_preview_supported = lambda: False
    viewer = None
    errors = []
    report = {'clip': str(args.clip.resolve()), 'baseline': bool(args.baseline_dir),
              'software_requested': args.software, 'effects': args.effects,
              'quality': args.quality, 'prepared_path': prepared,
              'prepare_ms': prepare_ms}

    def pump(seconds):
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline:
            app.processEvents()
            time.sleep(.002)

    def until(predicate, timeout=15):
        deadline = time.perf_counter() + timeout
        while not predicate():
            if time.perf_counter() >= deadline:
                raise TimeoutError('Editor readiness/frame timeout')
            pump(.005)

    try:
        started = time.perf_counter()
        viewer = cv.ClipViewer(str(args.clip.resolve()), None,
                               settings_manager=Settings(args.quality))
        viewer.resize(args.width, args.height)
        viewer.show()
        viewer.audio_output.setMuted(True)
        viewer.player.errorOccurred.connect(lambda *_: errors.append(viewer.player.errorString()))
        until(lambda: viewer._playback_ready)
        report['ready_ms'] = (time.perf_counter() - started) * 1000
        if args.effects:
            viewer._on_effect_changed('contrast', 20)
            viewer._on_effect_changed('sharpness', 30)
        viewer.player.play()
        pump(.5)
        cpu = time.process_time()
        wall = time.perf_counter()
        pump(args.seconds)
        report['cpu_one_core_percent'] = 100 * (time.process_time() - cpu) / (time.perf_counter() - wall)
        report['playback'] = viewer.render_performance_summary()
        viewer.player.pause()
        pump(.15)
        native_seeks = []
        initial_generation = viewer._seek_generation
        positions = []
        # Repeated input faster than the old 50ms debounce, representing a continuous drag.
        viewer.trim_slider.dragging = 'playhead'
        wall = time.perf_counter()
        for i in range(60):
            pct = .15 + .65 * i / 59
            viewer._on_seek_requested(pct)
            pump(.016)
            positions.append(viewer.player.position())
            snapshot = viewer.video_widget.preview_performance_snapshot()
            native_seeks.append(snapshot.get('latest_video_pts_ms'))
        report['scrub_duration_ms'] = (time.perf_counter() - wall) * 1000
        report['scrub_player_distinct_positions'] = len(set(positions))
        report['scrub_preview_distinct_pts'] = len({x for x in native_seeks if x is not None})
        report['scrub_input_events'] = 60
        report['scrub_generations'] = viewer._seek_generation - initial_generation
        viewer.trim_slider.dragging = None
        finish = getattr(viewer, '_on_playhead_scrub_finished', None)
        if finish is not None:
            finish()
        target = int(viewer.duration_ms * .8)
        until(lambda: abs(viewer.player.position() - target) < 150)
        pump(.5)
        report['final_seek_error_ms'] = abs(viewer.player.position() - target)
        report['after_scrub'] = viewer.render_performance_summary()
        if args.exercise_modes:
            from PySide6.QtMultimedia import QMediaPlayer
            original = (viewer.clip_path, viewer._src_w, viewer._src_h)
            editor_state = viewer._capture_editor_state()
            mode_results = []
            for mode in ('540p', '720p', 'original', 'auto'):
                viewer.player.pause()
                viewer._on_seek_requested(.2)
                pump(.3)
                viewer.preview_quality_combo.setCurrentIndex(
                    viewer.preview_quality_combo.findData(mode))
                until(lambda: viewer._playback_ready
                      and viewer._renderer_resume_position_ms is None)
                if mode != 'original':
                    until(lambda: viewer._proxy_checked, timeout=25)
                    until(lambda: viewer._playback_ready
                          and viewer._renderer_resume_position_ms is None)
                pump(.2)
                assert original == (viewer.clip_path, viewer._src_w, viewer._src_h)
                assert viewer._capture_editor_state() == editor_state
                assert abs(viewer.player.position() - int(viewer.duration_ms * .2)) < 150
                mode_results.append({'mode': mode, 'path': viewer._playback_path,
                                     'position': viewer.player.position()})
            viewer.player.play()
            pump(.7)
            before = viewer.player.position()
            viewer.preview_quality_combo.setCurrentIndex(
                viewer.preview_quality_combo.findData('original'))
            until(lambda: viewer._playback_ready
                  and viewer._renderer_resume_position_ms is None
                  and viewer.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState)
            after = viewer.player.position()
            assert after >= before - 150, (before, after)
            assert original == (viewer.clip_path, viewer._src_w, viewer._src_h)
            assert viewer._capture_editor_state() == editor_state
            viewer.player.pause()
            pump(.2)
            report['mode_pause_200ms'] = viewer.render_performance_summary()
            viewer.grab().save(str(args.output.with_name(
                args.output.stem + '-pause-200ms.png')))
            pump(.6)
            report['mode_pause_800ms'] = viewer.render_performance_summary()
            assert (report['mode_pause_800ms']['native_frame_available']
                    or report['mode_pause_800ms']['software_preview']['has_real_frame'])
            report['mode_switches'] = mode_results
            report['playing_mode_switch'] = {'before_ms': before, 'after_ms': after}
        viewer.grab().save(str(args.output.with_suffix('.png')))
        report['errors'] = errors
        report['passed'] = not errors and report['final_seek_error_ms'] < 150
    except Exception as exc:
        report['passed'] = False
        report['failure'] = repr(exc)
        raise
    finally:
        if viewer is not None:
            viewer.close()
        pump(.2)
        QThreadPool.globalInstance().waitForDone(10000)
        args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps({k: v for k, v in report.items()
                          if k not in ('playback', 'after_scrub', 'mode_pause_200ms',
                                       'mode_pause_800ms')}, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
