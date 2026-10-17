"""Measure fresh and cached timeline renders without starting capture."""

import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ui-root', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'FTHR_UI')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    sys.path.insert(0, str(args.ui_root.resolve()))
    from PySide6.QtWidgets import QApplication
    from ui.clip_viewer import TimelineSegment, TrimSlider

    app = QApplication([])
    timeline = TrimSlider(300_000)
    timeline.resize(1400, 116)
    results = []
    for count in (10, 100, 300, 1000):
        timeline.segments = [TimelineSegment(index / count, (index + 1) / count,
                                             index % 3 == 1) for index in range(count)]
        for hide_deleted in (False, True):
            timeline.hide_deleted_segments = hide_deleted
            started = time.perf_counter()
            timeline.grab()
            fresh_ms = (time.perf_counter() - started) * 1000
            samples = []
            for _ in range(7):
                started = time.perf_counter()
                timeline.grab()
                samples.append((time.perf_counter() - started) * 1000)
            results.append({'segments': count, 'hide_deleted': hide_deleted,
                            'fresh_render_ms': round(fresh_ms, 3),
                            'median_repeat_render_ms': round(statistics.median(samples), 3)})
    document = {'ui_root': str(args.ui_root.resolve()), 'platform': app.platformName(),
                'widget_size': [1400, 116], 'repeat_samples': 7, 'results': results}
    output = json.dumps(document, indent=2)
    print(output)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + '\n', encoding='utf-8')
    timeline.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
