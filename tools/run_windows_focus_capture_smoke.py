"""Verify WGC focus pauses preserve replay using two temporary test windows.

Temporarily changes foreground focus. Build artifacts and three short clips
stay in --output, which must be outside the repository. No game is required.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
import subprocess
import sys

from run_windows_capture_smoke import ROOT, build_harness


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--monitor', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('Windows is required')
    output = args.output.resolve()
    if output.is_relative_to(ROOT):
        parser.error('--output must be outside the repository')
    output.mkdir(parents=True, exist_ok=True)
    # The existing process-name heuristic selects the actual anti-cheat
    # monitor/focus path for this isolated test process.
    exe = build_harness(output, ROOT / 'tools/windows_focus_capture_smoke.cpp', 'focus-smoke')
    source_exe = exe.with_name('valorant.exe')
    shutil.copy2(exe, source_exe)
    with (output / 'focus.log').open('w', encoding='utf-8') as log:
        source = subprocess.Popen([str(source_exe), '--source'], stdout=log,
                                  stderr=subprocess.STDOUT,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            subprocess.run([str(exe), args.monitor, str(output)], stdout=log,
                           stderr=subprocess.STDOUT, timeout=60, check=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        finally:
            if source.poll() is None:
                source.terminate()
            source.wait(timeout=5)
    ffmpeg = ROOT / 'FTHRcapture/FTHRclips/third_party/ffmpeg/bin'
    results = []
    for cycle in range(1, 4):
        clip = output / f'focus-{cycle}.mp4'
        subprocess.run([str(ffmpeg / 'ffmpeg.exe'), '-v', 'error', '-i', str(clip),
                        '-f', 'null', '-'], check=True, timeout=30,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        probe = json.loads(subprocess.check_output([
            str(ffmpeg / 'ffprobe.exe'), '-v', 'error', '-show_format',
            '-of', 'json', str(clip)], creationflags=subprocess.CREATE_NO_WINDOW))
        duration = float(probe['format']['duration'])
        if not 3.0 <= duration <= 12.0:
            raise RuntimeError(f'Unexpected paused replay duration: {duration}')
        if results and not 0.7 <= duration - results[-1]['actual_duration_seconds'] <= 3.0:
            raise RuntimeError(f'Replay history was lost across a focus change: {duration}')
        results.append({'cycle': cycle, 'saved': True, 'full_decode': True,
                        'actual_duration_seconds': duration,
                        'requested_duration_seconds': 30})
    (output / 'result.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print(f'3 replay-preserving focus pause/resume/save/decode cycles passed. Evidence: {output}')


if __name__ == '__main__':
    main()
