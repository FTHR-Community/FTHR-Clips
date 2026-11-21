"""Exercise protected WGC focus with generated fullscreen scenes on --monitor.

Runs one video-only history-preserving pause and one audio-enabled generation
reset. Temporarily changes foreground focus and plays a test tone in audio mode.
Logs and decoded-verified clips stay in a new external --output directory.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

from run_windows_capture_smoke import ROOT, build_harness, validate_output_directory

FRAME_PIXELS = 32 * 18
FRAME_BYTES = FRAME_PIXELS * 3


def verify_clip(clip: Path, ffmpeg: Path, audio: bool) -> dict:
    probe = json.loads(subprocess.check_output([
        str(ffmpeg / 'ffprobe.exe'), '-v', 'error', '-show_format', '-show_streams',
        '-of', 'json', str(clip),
    ], creationflags=subprocess.CREATE_NO_WINDOW, timeout=30))
    duration = float(probe['format']['duration'])
    expected = (2.5, 5.5) if audio else (5.5, 10.0)
    if not expected[0] <= duration <= expected[1]:
        raise RuntimeError(f'{clip.name}: unexpected replay duration {duration:.3f}s')
    audio_streams = sum(stream['codec_type'] == 'audio' for stream in probe['streams'])
    if bool(audio_streams) != audio:
        raise RuntimeError(f'{clip.name}: unexpected number of audio streams ({audio_streams})')

    decode = [str(ffmpeg / 'ffmpeg.exe'), '-v', 'error', '-xerror',
              '-err_detect', 'explode', '-i', str(clip)]
    raw = subprocess.check_output([
        *decode, '-map', '0:v:0', '-vf', 'scale=32:18:flags=area',
        '-pix_fmt', 'rgb24', '-fps_mode', 'passthrough', '-f', 'rawvideo', '-',
    ], creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
    if not raw or len(raw) % FRAME_BYTES:
        raise RuntimeError(f'{clip.name}: decoding did not produce complete frames')

    before_frames = after_frames = 0
    for offset in range(0, len(raw), FRAME_BYTES):
        before = after = 0
        for pixel in range(offset, offset + FRAME_BYTES, 3):
            red, green, blue = raw[pixel:pixel + 3]
            if red > 165 and blue > 165 and green < 80:
                raise RuntimeError(f'{clip.name}: recorded the off-focus magenta marker')
            before += red > 140 and red - max(green, blue) > 75
            after += green > 140 and green - max(red, blue) > 75
        if before >= FRAME_PIXELS * 0.90:
            if audio or after_frames:
                raise RuntimeError(f'{clip.name}: unexpected pre-pause scene')
            before_frames += 1
        elif after >= FRAME_PIXELS * 0.90:
            after_frames += 1
        else:
            raise RuntimeError(f'{clip.name}: frame does not contain the owned foreground scene')
    if after_frames < 60 or (not audio and before_frames < 60):
        raise RuntimeError(f'{clip.name}: too little verified foreground footage')
    if audio:
        subprocess.run([*decode, '-map', '0:a:0', '-f', 'null', '-'], check=True,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
    return {
        'file': clip.name, 'audio_enabled': audio, 'audio_streams': audio_streams,
        'requested_duration_seconds': 30, 'duration_seconds': duration,
        'decoded_pre_pause_frames': before_frames, 'decoded_post_pause_frames': after_frames,
        'decoded_off_focus_frames': 0,
    }


def run_mode(exe: Path, source_exe: Path, monitor: str, output: Path, mode: str) -> None:
    log_path = output / f'focus-{mode}.log'
    with log_path.open('w', encoding='utf-8') as log:
        source = subprocess.Popen(
            [str(source_exe), '--source', monitor, mode], stdout=log,
            stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            result = subprocess.run(
                [str(exe), monitor, str(output), str(source.pid), mode], stdout=log,
                stderr=subprocess.STDOUT, timeout=45,
                creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                raise RuntimeError(f'{mode} focus harness exited {result.returncode}; see {log_path}')
        finally:
            if source.poll() is None:
                try:
                    source.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    source.terminate()
                    source.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--monitor', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('Windows is required')
    try:
        output = validate_output_directory(args.output, cycles=2, manual=False)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))

    exe = build_harness(output, ROOT / 'tools/windows_focus_capture_smoke.cpp', 'focus-smoke')
    # Only the synthetic source uses the basename recognized by protected routing.
    source_exe = exe.with_name('valorant.exe')
    shutil.copy2(exe, source_exe)
    ffmpeg = ROOT / 'FTHRcapture/FTHRclips/third_party/ffmpeg/bin'
    results = []
    for mode in ('video', 'audio'):
        run_mode(exe, source_exe, args.monitor, output, mode)
        results.append(verify_clip(output / f'focus-{mode}.mp4', ffmpeg, audio=mode == 'audio'))
    (output / 'result.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print(f'Video-only focus preservation, audio focus reset and full decode passed. Evidence: {output}')


if __name__ == '__main__':
    main()
