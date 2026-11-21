"""Build or run the protected WGC 1080p to 900p to 1080p replay qualification.

The live run temporarily changes the selected monitor's mode and foreground
window. Use a 1080p SDR monitor with a driver-supported 1600x900 mode. The
fullscreen source is synthetic. With --with-system-audio, silence other apps
and keep the default playback endpoint audible so the loopback hears the tones.
The capture requests an unavailable microphone endpoint to exclude live input.
"""

from __future__ import annotations

import argparse
from array import array
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from run_windows_capture_smoke import ROOT, build_harness, validate_output_directory


FRAME_WIDTH = 64
FRAME_HEIGHT = 36
SAMPLE_RATE = 48000
TONE_FREQUENCIES = (440, 660, 880)
MAX_MARKER_SKEW_S = 0.08  # Endpoint packet, WGC presentation, and 20 ms audio analysis bins.
COLORS = ("red", "green", "blue")
FFMPEG_BIN = ROOT / "FTHRcapture/FTHRclips/third_party/ffmpeg/bin"


def verify_capture_log(log, canvas, encoded, expected_sizes):
    trace = log.read_text(encoding="utf-8", errors="replace")
    start = re.search(r"^RESIZE_START backend=WGC_MONITOR encoder=(.+) generation=(\d+) ",
                      trace, re.MULTILINE)
    if not start:
        raise RuntimeError("production protected WGC startup marker is missing")
    generation = int(start[2])
    events = []
    event_lines = []
    marker_lines = {}
    for line_number, line in enumerate(trace.splitlines()):
        marker = re.fullmatch(r"RESIZE_MARKER_REQUEST stage=(\d+)", line)
        if marker:
            marker_lines[int(marker[1])] = line_number
        prefix = "FTHR_DIAGNOSTIC_EVENT "
        if line.startswith(prefix) and '"wgc_monitor_resize_accepted"' in line:
            event = json.loads(line[len(prefix):])
            if (event.get("event") == "wgc_monitor_resize_accepted"
                    and event.get("reason") == "source_size_changed"):
                events.append(event)
                event_lines.append(line_number)
    source_sizes = [(event["source_width"], event["source_height"]) for event in events]
    if source_sizes != expected_sizes:
        raise RuntimeError(f"WGC did not report the expected adopted source sizes: {source_sizes}")
    if any(marker_lines.get(stage, -1) <= line for stage, line in enumerate(event_lines, 2)):
        raise RuntimeError("a mode's synchronized marker was requested before WGC adopted the new source")
    for event, (source_width, source_height) in zip(events, source_sizes, strict=True):
        if (event["capture_generation"] != generation or event["history_retained"] is not True
                or (event["canvas_width"], event["canvas_height"]) != canvas
                or (event["encoded_width"], event["encoded_height"]) != encoded
                or event["pool_width"] < source_width or event["pool_height"] < source_height):
            raise RuntimeError(f"unexpected WGC resize acceptance: {event}")
    return {"capture_generation": generation, "encoder": start[1], "resize_events": events}


def verify_tone_references(log, count):
    trace = log.read_text(encoding="utf-8", errors="replace")
    references = []
    for line in trace.splitlines():
        if not line.startswith("RESIZE_TONE "):
            continue
        fields = dict(field.split("=", 1) for field in line.split()[1:])
        references.append({key: float(value) if key.endswith("_ms") else int(value)
                           for key, value in fields.items()})
    if [r["scene"] for r in references] != list(range(1, count + 1)):
        raise RuntimeError("missing independent loopback timing references")
    for ref in references:
        if (ref["paint_issued_qpc_100ns"] < ref["estimated_onset_qpc_100ns"]
                or ref["onset_packet_span_ms"] <= 0
                or ref["paint_after_estimate_ms"] > MAX_MARKER_SKEW_S * 1000):
            raise RuntimeError(f"synthetic marker itself missed its timing reference: {ref}")
    return references


def process(command, *, timeout=30, **kwargs):
    return subprocess.run(
        [str(arg) for arg in command], timeout=timeout, check=True,
        creationflags=subprocess.CREATE_NO_WINDOW, **kwargs,
    )


def capture_stdout(command):
    return process(command, stdout=subprocess.PIPE).stdout


def pixel(frame, x, y):
    index = (y * FRAME_WIDTH + x) * 3
    return frame[index : index + 3]


def visual_markers(raw, frame_times, count):
    frame_bytes = FRAME_WIDTH * FRAME_HEIGHT * 3
    if len(raw) != len(frame_times) * frame_bytes:
        raise RuntimeError("FFmpeg frames do not match ffprobe presentation times")
    frames = [[] for _ in COLORS]
    view = memoryview(raw)
    for index, timestamp in enumerate(frame_times):
        frame = view[index * frame_bytes : (index + 1) * frame_bytes]
        samples = [pixel(frame, x, y) for x in (10, 53) for y in (9, 26)]
        for color, observations in enumerate(frames):
            if all(rgb[color] > 130 and all(rgb[color] - rgb[c] > 85
                   for c in range(3) if c != color) for rgb in samples):
                xs = [x for x in range(5, 58) if min(pixel(frame, x, 18)) > 185]
                corners = all(min(pixel(frame, x, y)) > 185
                              for x in (5, 59) for y in (2, 33))
                observations.append((timestamp, sum(xs) // len(xs) if xs else None, corners))
                break
    if any(frames[count:]):
        raise RuntimeError("found a marker from a later display mode")
    markers = []
    for color, observations in zip(COLORS[:count], frames[:count], strict=True):
        positions = {x for _, x, corners in observations if x is not None and corners}
        correct_frames = [(pts, x) for pts, x, corners in observations if x is not None and corners]
        if (len(correct_frames) < 30 or len(positions) < 10
                or len(correct_frames) < 0.9 * len(observations)):
            raise RuntimeError(
                f"{color} marker lacks moving frames or intact corner anchors "
                f"({len(correct_frames)}/{len(observations)} frames, {len(positions)} positions)")
        first, last = correct_frames[0][0], correct_frames[-1][0]
        if last - first < 1.0 or (markers and first <= markers[-1]["last_video_s"]):
            raise RuntimeError(f"{color} marker was truncated or appeared out of order")
        markers.append({"color": color, "first_video_s": first, "last_video_s": last,
                        "correct_frames": len(correct_frames), "moving_positions": len(positions)})
    return markers


def audio_markers(pcm, markers):
    samples = array("h")
    samples.frombytes(pcm)
    window = SAMPLE_RATE // 50  # 20 ms analysis bins.
    references = [
        [(math.cos(2 * math.pi * hz * i / SAMPLE_RATE),
          math.sin(2 * math.pi * hz * i / SAMPLE_RATE)) for i in range(window)]
        for hz in TONE_FREQUENCIES
    ]
    onsets = [[] for _ in TONE_FREQUENCIES]
    for start in range(0, len(samples) - window + 1, window):
        chunk = samples[start : start + window]
        energy = sum(sample * sample for sample in chunk)
        if energy < 20**2 * window:
            continue
        scores = []
        for reference in references:
            real = sum(sample * cos for sample, (cos, _) in zip(chunk, reference, strict=True))
            imag = sum(sample * sin for sample, (_, sin) in zip(chunk, reference, strict=True))
            scores.append((real**2 + imag**2) * 2 / (window * energy))
        tone = scores.index(max(scores))
        if scores[tone] > 0.65:
            onsets[tone].append(start / SAMPLE_RATE)
    for marker, hz, hits in zip(markers, TONE_FREQUENCIES, onsets, strict=False):
        if len(hits) < 12:
            raise RuntimeError(f"missing {hz} Hz audio marker ({len(hits)} matching windows)")
        skew = hits[0] - marker["first_video_s"]
        if abs(skew) > MAX_MARKER_SKEW_S:
            raise RuntimeError(f"{hz} Hz audio/visual marker skew was {skew:+.3f} s")
        marker.update(tone_hz=hz, first_audio_s=hits[0], av_skew_s=round(skew, 6))


def verify_clip(clip, count, width, height, audio, ffmpeg_bin, *, reporter_length=False):
    ffmpeg = ffmpeg_bin / "ffmpeg.exe"
    ffprobe = ffmpeg_bin / "ffprobe.exe"
    streams = json.loads(capture_stdout([
        ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", clip,
    ]))
    video_streams = [s for s in streams["streams"] if s["codec_type"] == "video"]
    audio_streams = [s for s in streams["streams"] if s["codec_type"] == "audio"]
    if len(video_streams) != 1 or len(audio_streams) != int(audio):
        raise RuntimeError(f"unexpected streams in {clip.name}")
    video = video_streams[0]
    if (video["width"], video["height"]) != (width, height):
        raise RuntimeError(f"encoded canvas changed to {video['width']}x{video['height']}")
    frame_info = json.loads(capture_stdout([
        ffprobe, "-v", "error", "-show_frames", "-select_streams", "v:0", "-show_entries",
        "frame=best_effort_timestamp_time,width,height", "-of", "json", clip,
    ]))["frames"]
    if any((f["width"], f["height"]) != (width, height) for f in frame_info):
        raise RuntimeError("video dimensions changed mid-clip")
    times = [float(f["best_effort_timestamp_time"]) for f in frame_info]
    gaps = [end - start for start, end in zip(times, times[1:], strict=False)]
    if not gaps or min(gaps) <= 0 or max(gaps) > 0.250001:
        raise RuntimeError(f"invalid video presentation timeline in {clip.name}")
    fps = len(gaps) / (times[-1] - times[0])
    if fps < 47.99:
        raise RuntimeError(f"effective video cadence fell to {fps:.2f} fps")
    if reporter_length and not 44.5 <= times[-1] - times[0] <= 45.5:
        raise RuntimeError(f"45s save returned {times[-1] - times[0]:.3f}s of video")
    # Both extraction commands decode the entirety of their respective streams.
    decode = [ffmpeg, "-v", "error", "-xerror", "-err_detect", "explode", "-i", clip]
    raw = capture_stdout([
        *decode, "-map", "0:v:0", "-vf", f"scale={FRAME_WIDTH}:{FRAME_HEIGHT}:flags=area",
        "-pix_fmt", "rgb24", "-fps_mode", "passthrough", "-threads", "1", "-f", "rawvideo", "-",
    ])
    markers = visual_markers(raw, times, count)
    if audio:
        pcm = capture_stdout([
            *decode, "-map", "0:a:0", "-af", "aresample=async=1000:first_pts=0",
            "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-",
        ])
        audio_markers(pcm, markers)
        audio_end = len(pcm) / (2 * SAMPLE_RATE)
        if abs(audio_end - times[-1]) > 0.25:
            raise RuntimeError(f"audio/video end skew was {audio_end - times[-1]:+.3f} s")
    return {"file": clip.name, "codec": video["codec_name"], "width": width, "height": height,
            "duration_s": float(streams["format"]["duration"]), "video_frames": len(times),
            "fps": round(fps, 3), "largest_video_gap_s": round(max(gaps), 6), "markers": markers}


def run_live(args, exe, output, monitor, left, top):
    source_exe = output / "valorant.exe"
    shutil.copy2(exe, source_exe)
    source_environment = dict(os.environ)
    source_environment["PATH"] = str(exe.parent) + os.pathsep + source_environment.get("PATH", "")
    with (output / "source.log").open("wb") as source_log:
        source = subprocess.Popen(
            [str(source_exe), "--source", left, top, str(int(args.with_system_audio))],
            stdout=source_log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
            env=source_environment,
        )
        try:
            with (output / "capture.log").open("wb") as capture_log:
                command = [
                    exe, monitor, output, source.pid, int(args.with_system_audio),
                    int(args.fixed_720p), {"auto": 0, "nvidia": 1, "amd": 2, "intel": 3}[args.encoder],
                ]
                if args.start_900p:
                    command.append("--start-900p")
                if args.reporter_length:
                    command.append("--reporter-length")
                process(command, timeout=130 if args.reporter_length else 65,
                        stdout=capture_log, stderr=subprocess.STDOUT)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            print(f"Capture failed. Synthetic source exit status: {source.poll()}; logs: {output}",
                  file=sys.stderr)
            raise
        finally:
            try:
                # The runner can still restore after a host crash or timeout.
                mode = output / "display-mode.bin"
                if mode.is_file():
                    with (output / "restore.log").open("wb") as log:
                        process([exe, "--restore", mode], stdout=log, stderr=subprocess.STDOUT)
            finally:
                if source.poll() is None:
                    source.terminate()
                source.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--list-monitors", action="store_true", help="build and list modes without changing them")
    action.add_argument("--run-display-mode-test", action="store_true", help="opt in to actual display mode changes")
    parser.add_argument("--monitor", help="stable monitor path from --list-monitors, required if ambiguous")
    parser.add_argument("--output", type=Path, required=True, help="empty scratch directory outside the repository")
    parser.add_argument("--executable", type=Path, help="reuse a built protected-resize-smoke.exe")
    parser.add_argument("--encoder", choices=("auto", "nvidia", "amd", "intel"), default="auto")
    parser.add_argument("--fixed-720p", action="store_true", help="exercise fixed 720p output instead of Native")
    parser.add_argument("--start-900p", action="store_true", help="initialize at 900p and grow the WGC pool to 1080p")
    parser.add_argument("--reporter-length", action="store_true",
                        help="warm up 51s using a 47s buffer, then require full 45s replays")
    parser.add_argument("--with-system-audio", action="store_true", help="capture loopback and verify matched tones")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Windows is required")
    output = validate_output_directory(args.output, cycles=1, manual=False)
    exe = args.executable.resolve() if args.executable else build_harness(
        output, ROOT / "tools/windows_protected_monitor_resize_smoke.cpp", "protected-resize-smoke",
    )
    monitors = capture_stdout([exe, "--list-monitors"]).decode(errors="replace")
    print(monitors, end="", flush=True)
    if args.list_monitors:
        return 0
    candidates = []
    for line in monitors.splitlines():
        fields = line.split("\t")
        if len(fields) == 6 and fields[3:5] == ["1920x1080", "900p_available=1"] and fields[5]:
            if not args.monitor or fields[5].casefold() == args.monitor.casefold():
                candidates.append(fields)
    if len(candidates) != 1:
        parser.error("select one active 1080p monitor with a usable 900p mode")
    if any(not (FFMPEG_BIN / f"{name}.exe").is_file() for name in ("ffmpeg", "ffprobe")):
        parser.error("install the FFmpeg runtime with tools/fetch_third_party.py --ffmpeg")
    _, left, top, _, _, monitor = candidates[0]
    print("Activate the fullscreen synthetic source if Windows leaves it in the background.", flush=True)
    run_live(args, exe, output, monitor, left, top)
    baseline = (1600, 900) if args.start_900p else (1920, 1080)
    encoded = (1280, 720) if args.fixed_720p else baseline
    expected_sizes = [(1920, 1080)] if args.start_900p else [(1600, 900), (1920, 1080)]
    clips = (("growth.mp4", 2),) if args.start_900p else (("downshift.mp4", 2), ("round-trip.mp4", 3))
    result = verify_capture_log(output / "capture.log", baseline, encoded, expected_sizes)
    if args.with_system_audio:
        result["tone_clock_references"] = verify_tone_references(output / "source.log", len(expected_sizes) + 1)
    result["clips"] = [
        verify_clip(output / name, count, *encoded, args.with_system_audio, FFMPEG_BIN,
                    reporter_length=args.reporter_length)
        for name, count in clips
    ]
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Protected WGC resize, replay save and full decode passed. Evidence: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
