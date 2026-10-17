"""Profile the real Windows Qt shell with a read-only library and no capture.

Runs a repeatable wheel workload inside this process. It never injects input
into another application or restarts the user's active capture session.
"""
from __future__ import annotations

import argparse
import cProfile
import json
import logging
import os
from pathlib import Path
import pstats
import shutil
import sys
import tempfile
import time
from contextlib import ExitStack
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'FTHR_UI'))


class _NoCapture:
    def __init__(self, *_args):
        pass

    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--platform', default='windows' if sys.platform == 'win32' else 'offscreen')
    parser.add_argument('--steps', type=int, default=160)
    parser.add_argument('--hold-seconds', type=int, default=0,
                        help='Keep the measured native window open for manual UI inspection.')
    args = parser.parse_args()
    os.environ['QT_QPA_PLATFORM'] = args.platform
    original_home = Path.home()
    original = json.loads((original_home / '.fthr/settings.json').read_text(encoding='utf-8'))
    original_streams = sys.stdout, sys.stderr
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='fthr-scroll-profile-') as temp, ExitStack() as stack:
        profile_home = Path(temp)
        stack.enter_context(patch('pathlib.Path.home', return_value=profile_home))
        from PySide6.QtCore import QTimer, QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        from PySide6.QtWidgets import QApplication
        from PySide6.QtTest import QTest
        import main as application
        from ui.app_style import apply_app_style
        from ui import clip_grid
        # Copy warm cache entries into the disposable profile; media is only read.
        cache = profile_home / 'thumbnails'
        shutil.copytree(original_home / '.fthr/thumbnails', cache, dirs_exist_ok=True)
        clip_grid.THUMB_CACHE_DIR = str(cache)
        app = QApplication.instance() or QApplication([])
        application._load_fonts()
        apply_app_style(app)
        stack.enter_context(patch.object(application, 'CaptureCardClient', _NoCapture))
        stack.enter_context(patch.object(application, 'CaptureBridge', _NoCapture))
        for method in ('_setup_hotkeys', '_start_mic_recorder', 'start_engine', 'stop_engine'):
            stack.enter_context(patch.object(application.MainWindow, method, lambda *_: None))
        for method in ('_start_encoder_probe', '_populate_mic_devices', '_populate_playback_devices', '_populate_camera_devices'):
            stack.enter_context(patch.object(application._SettingsPage, method, lambda *_: None))
        settings = application.SettingsManager()
        for key in ('clips_directory', 'imported_clip_folders'):
            settings.set(key, original.get(key))
        settings.set('pause_ui_in_background', False)
        settings.set('camera_enabled', False)
        settings.set('third_party_keyboard_enabled', False)
        settings.save_settings()
        window = application.MainWindow()
        window.setWindowTitle('FTHR Clips — scroll profiling (capture disabled)')
        window.resize(1280, 976)
        window.show()
        QTest.qWait(2500)
        grid, scroll = window.clip_grid, window.clip_scroll_area
        samples, virtual_times = [], []
        baseline_update = grid._update_virtualized_cards
        grid._virtual_update_timer.timeout.disconnect()

        def measured_update():
            started = time.perf_counter()
            baseline_update()
            virtual_times.append((time.perf_counter() - started) * 1000)

        grid._virtual_update_timer.timeout.connect(measured_update)
        sampler = QTimer(window)
        sampler.setTimerType(Qt.TimerType.PreciseTimer)
        sampler.setInterval(8)
        last = time.perf_counter()

        def sample():
            nonlocal last
            now = time.perf_counter()
            samples.append((now - last) * 1000)
            last = now

        sampler.timeout.connect(sample)
        profiler = cProfile.Profile()
        profiler.enable()
        sampler.start()
        values = []
        # Use native QWidget painting and the application's normal wheel path.
        # Down and back up covers uncached entry and cached card recycling.
        for direction in (-120, 120):
            for _ in range(args.steps):
                event = QWheelEvent(QPointF(500, 400), QPointF(500, 400),
                                    QPoint(), QPoint(0, direction),
                                    Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                    Qt.ScrollPhase.NoScrollPhase, False)
                scroll.wheelEvent(event)
                QTest.qWait(16)
                values.append(scroll.verticalScrollBar().value())
        QTest.qWait(200)
        sampler.stop()
        profiler.disable()
        profiler.dump_stats(str(args.output / 'scroll.prof'))
        with (args.output / 'profile.txt').open('w', encoding='utf-8') as report:
            pstats.Stats(profiler, stream=report).strip_dirs().sort_stats('cumulative').print_stats(40)

        def summary(numbers):
            ordered = sorted(numbers)
            return {'count': len(numbers), 'median_ms': round(ordered[len(ordered)//2], 2),
                    'p95_ms': round(ordered[min(len(ordered)-1, int(len(ordered)*.95))], 2),
                    'max_ms': round(max(ordered), 2)} if ordered else {}

        result = {'platform': app.platformName(), 'records': len(grid._records),
                  'scale': window.devicePixelRatioF(), 'steps': args.steps,
                  'scroll_distance': max(values) - min(values),
                  'gui_timer': summary(samples), 'virtual_updates': summary(virtual_times),
                  'gui_gaps_over_33ms': sum(value > 33 for value in samples),
                  'live_cards': len(grid._thumb_widgets), 'pooled_cards': len(grid._recycled_cards)}
        (args.output / 'metrics.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        window.grab().save(str(args.output / 'native-library.png'))
        print(json.dumps(result))
        if args.hold_seconds:
            QTimer.singleShot(args.hold_seconds * 1000, app.quit)
            app.exec()
        window._shutdown_requested = True
        window._perform_full_shutdown()
        window.close()
        logging.shutdown()
        for stream in (sys.stdout, sys.stderr):
            if isinstance(stream, application._LogTee) and stream._log is not None:
                stream._log.close()
        sys.stdout, sys.stderr = original_streams


if __name__ == '__main__':
    main()
