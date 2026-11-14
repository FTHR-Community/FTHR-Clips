"""Run the production Windows capture pipeline against a separate D3D11 stimulus.

Cases use BACKEND,ENCODER,WIDTHxHEIGHT,FPS[,STORAGE[,CODEC[,SOURCE_FPS]]].
A source FPS of zero presents one image. Rendered, encoded and distinct source
cadence are measured separately. Evidence stays in a new external directory.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from fractions import Fraction
import json
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.run_windows_capture_smoke import build_harness

NAME = "windows-fps-performance-smoke"
FFMPEG_BIN = ROOT / "FTHRcapture/FTHRclips/third_party/ffmpeg/bin"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class Case:
    backend: str
    encoder: str
    width: int
    height: int
    fps: int
    storage: str
    codec: str
    source_fps: int | None


def dimensions(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([1-9]\d*)x([1-9]\d*)", value)
    if match is None:
        raise argparse.ArgumentTypeError("Expected WIDTHxHEIGHT")
    return int(match[1]), int(match[2])


def parse_case(value: str) -> Case:
    fields = value.split(",")
    if not 4 <= len(fields) <= 7:
        raise argparse.ArgumentTypeError(__doc__.splitlines()[2])
    backend, encoder, geometry, fps = fields[:4]
    storage, codec, source = (fields[4:] + ["", "", ""])[:3]
    storage, codec = storage or "memory", codec or "h264"
    width, height = dimensions(geometry)
    if (backend not in {"dxgi", "wgc", "window"} or encoder not in {"nvidia", "intel", "software"}
            or storage not in {"memory", "disk"} or codec not in {"h264", "hevc", "av1"}):
        raise argparse.ArgumentTypeError("Unsupported backend, encoder, storage or codec")
    if not fps.isdecimal() or not 1 <= int(fps) <= 240:
        raise argparse.ArgumentTypeError("Capture FPS must be 1–240")
    if source and (not source.isdecimal() or not 0 <= int(source) <= 480):
        raise argparse.ArgumentTypeError("Source FPS must be 0–480")
    if not (2 <= width <= 7680 and 2 <= height <= 4320 and not (width % 2 or height % 2)):
        raise argparse.ArgumentTypeError("Output dimensions must be even and at most 7680x4320")
    return Case(backend, encoder, width, height, int(fps), storage, codec, int(source) if source else None)


def crop_rect(value: str) -> str:
    try:
        x, y, width, height = map(float, value.split(","))
    except ValueError as error:
        raise argparse.ArgumentTypeError("Expected normalized x,y,width,height") from error
    if not (0 <= x < 1 and 0 <= y < 1 and 0 < width <= 1 - x and 0 < height <= 1 - y):
        raise argparse.ArgumentTypeError("Crop must be within the source")
    return value


def markers(path: Path, prefix: str) -> list[dict]:
    decoder = json.JSONDecoder()
    records = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        index = line.find(prefix + " {")
        if index >= 0:
            try:
                data, _ = decoder.raw_decode(line[index + len(prefix) + 1:])
                records.append(data)
            except json.JSONDecodeError:
                continue
    return records


def await_source(source: subprocess.Popen, log: Path) -> dict:
    deadline = time.monotonic() + 15
    while source.poll() is None and time.monotonic() < deadline:
        ready = markers(log, "FPS_SOURCE_READY")
        if ready:
            return ready[-1]
        time.sleep(0.05)
    raise RuntimeError(f"Source did not report readiness. See {log}")


def stop_child(process: subprocess.Popen, stop_file: Path) -> bool:
    if process.poll() is not None:
        return True
    stop_file.touch()
    try:
        process.wait(timeout=5)
        return True
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        return False


def sample_process(process: subprocess.Popen, psutil_module) -> dict:
    if psutil_module is not None:
        try:
            proc = psutil_module.Process(process.pid)
            cpu = proc.cpu_times()
            memory = proc.memory_info()
            return {"cpu_seconds": cpu.user + cpu.system, "rss_bytes": memory.rss,
                    "private_bytes": memory.private, "handles": proc.num_handles(),
                    "threads": proc.num_threads(), "api": "psutil"}
        except psutil_module.NoSuchProcess:
            return {}
    from tools.qualify_windows_hardware import process_snapshot
    import ctypes
    from ctypes import wintypes

    snapshot = process_snapshot(process)
    count = wintypes.DWORD()
    handles = ctypes.windll.kernel32.GetProcessHandleCount(wintypes.HANDLE(process._handle), ctypes.byref(count))
    return {"cpu_seconds": snapshot["process_cpu_seconds"], "rss_bytes": snapshot["working_set_bytes"],
            "private_bytes": snapshot["private_bytes"], "handles": count.value if handles else None,
            "threads": snapshot["process_thread_count"], "api": "win32"}


def sample_gpu(command: str) -> dict:
    try:
        result = subprocess.run([command,
            "--query-gpu=index,utilization.gpu,utilization.encoder,utilization.memory,power.draw",
            "--format=csv,noheader,nounits"], capture_output=True, text=True,
            timeout=0.8, creationflags=CREATE_NO_WINDOW)
        return {"gpu_wide_csv": result.stdout.splitlines(), "exit_code": result.returncode}
    except subprocess.TimeoutExpired:
        return {"timeout": True}


def frame_checksum(frame_id: int) -> int:
    crc = 0
    for byte in frame_id.to_bytes(2, "little"):
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ (0x07 if crc & 0x80 else 0)) & 0xff
    return crc


def marker_geometry(source: dict, monitor: dict, window: bool, width: int, height: int,
                    scaling: str = "stretch", crop: tuple[int, int, int, int] | None = None):
    cell = source["cell"]
    left = 0 if window else source["x"] - monitor["x"]
    top = 0 if window else source["y"] - monitor["y"]
    canvas_w = source["width"] if window else monitor["width"]
    canvas_h = source["height"] if window else monitor["height"]
    if crop:
        crop_x, crop_y, canvas_w, canvas_h = crop
        left -= crop_x
        top -= crop_y
    if canvas_w <= 0 or canvas_h <= 0:
        raise ValueError("Capture canvas has invalid geometry")
    fit_w, fit_h = width, height
    if scaling == "fit":
        if canvas_w * height > width * canvas_h:
            fit_h = (width * canvas_h // canvas_w) & ~1
        else:
            fit_w = (height * canvas_w // canvas_h) & ~1
    offset_x = ((width - fit_w) // 2) & ~1
    offset_y = ((height - fit_h) // 2) & ~1
    centers = [offset_x + ((2 * left + cell * (3 + 2 * index)) * fit_w) // (2 * canvas_w)
               for index in range(28)]
    y = offset_y + (top + 2 * cell) * fit_h // canvas_h
    if (centers[0] < 1 or centers[-1] + 1 >= width or not 1 <= y < height - 1
               or min(second - first for first, second in zip(centers, centers[1:], strict=True)) < 3):
        raise ValueError("Frame-ID marker is outside the canvas or too small after scaling")
    x = centers[0] - 1
    return x, y - 1, centers[-1] - x + 2, [center - x for center in centers]


def read_marker(frame: bytes, stride: int, centers: list[int]) -> int | None:
    marker = 0
    for index, x in enumerate(centers):
        total = sum(frame[y * stride + x + dx] for y in range(3) for dx in (-1, 0, 1))
        if 9 * 80 < total < 9 * 175:
            return None
        marker |= int(total >= 9 * 175) << index
    frame_id = (marker >> 4) & 0xffff
    if marker & 0xf != 5 or marker >> 20 != frame_checksum(frame_id):
        return None
    return frame_id


def decode_marker(clip: Path, geometry, timeout: int) -> dict:
    x, y, strip_width, centers = geometry
    command = [str(FFMPEG_BIN / "ffmpeg.exe"), "-v", "error", "-xerror", "-i", str(clip),
               "-map", "0:v:0", "-vf", f"crop={strip_width}:3:{x}:{y}:exact=1,format=gray",
               "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    frames = valid = runs = repeats = backward = forward = 0
    previous = None
    ids = set()
    with clip.with_suffix(".decode.log").open("wb") as log:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log, creationflags=CREATE_NO_WINDOW) as proc:
            expired = threading.Event()

            def abort():
                if proc.poll() is None:
                    expired.set()
                    proc.kill()

            timer = threading.Timer(timeout, abort)
            timer.start()
            try:
                frame_bytes = strip_width * 3
                while chunk := proc.stdout.read(frame_bytes):
                    if len(chunk) != frame_bytes:
                        raise RuntimeError(f"Truncated decoded frame strip in {clip}")
                    frames += 1
                    frame_id = read_marker(chunk, strip_width, centers)
                    if frame_id is None:
                        previous = None
                        continue
                    valid += 1
                    ids.add(frame_id)
                    if previous is None:
                        runs += 1
                    else:
                        distance = (frame_id - previous) & 0xffff
                        if distance == 0:
                            repeats += 1
                        elif distance < 0x8000:
                            forward += 1
                        else:
                            backward += 1
                    previous = frame_id
                code = proc.wait()
            finally:
                timer.cancel()
    if code or expired.is_set():
        raise RuntimeError(f"Full FFmpeg decode failed for {clip} (exit {code}, timeout={expired.is_set()})")
    return {"decoded_frames": frames, "valid_frames": valid, "invalid_frames": frames - valid,
            "marker_coverage": valid / frames if frames else 0, "distinct_frame_ids_modulo_65536": len(ids),
            "consecutive_repeats": repeats, "forward_id_changes": forward,
            "backward_id_changes": backward, "valid_runs": runs,
            "observed_distinct_frame_lower_bound": len(ids)}


def inspect_timestamps(clip: Path, timeout: int) -> dict:
    raw = subprocess.check_output([str(FFMPEG_BIN / "ffprobe.exe"), "-v", "error",
        "-select_streams", "v:0", "-show_packets", "-show_entries",
        "packet=pts_time,dts_time,duration_time,flags", "-of", "json", str(clip)],
        timeout=timeout, creationflags=CREATE_NO_WINDOW)
    packets = json.loads(raw)["packets"]
    pts = [float(packet["pts_time"]) for packet in packets]
    dts = [float(packet["dts_time"]) for packet in packets]
    gaps = [b - a for a, b in zip(pts, pts[1:], strict=True)]
    valid = (bool(packets) and all(gap > 0 for gap in gaps)
             and all(b > a for a, b in zip(dts, dts[1:], strict=True)))
    if not valid:
        raise ValueError(f"Missing or nonmonotonic video packet timestamps in {clip}")
    return {"packet_count": len(packets), "pts_strictly_increasing": True,
        "dts_strictly_increasing": True, "first_packet_keyframe": "K" in packets[0].get("flags", ""),
        "median_pts_gap_ms": statistics.median(gaps) * 1000 if gaps else None,
        "max_pts_gap_ms": max(gaps) * 1000 if gaps else None}


def inspect_clip(clip: Path, source: dict, monitor: dict, case: Case, timeout: int,
                 scaling: str, crop: tuple[int, int, int, int] | None) -> dict:
    started = time.monotonic()
    command = [str(FFMPEG_BIN / "ffprobe.exe"), "-v", "error", "-count_frames", "-show_streams",
               "-show_format", "-of", "json", str(clip)]
    raw = subprocess.check_output(command, timeout=timeout, creationflags=CREATE_NO_WINDOW)
    clip.with_suffix(".ffprobe.json").write_bytes(raw)
    probe_time = time.monotonic() - started
    probe = json.loads(raw)
    video = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
    duration = float(video.get("duration") or probe["format"]["duration"])
    if duration <= 0:
        raise ValueError(f"No positive video duration in {clip}")
    marker = marker_geometry(source, monitor, case.backend == "window", video["width"], video["height"],
                             scaling, crop)
    started = time.monotonic()
    evidence = decode_marker(clip, marker, timeout)
    audio_streams = sum(stream["codec_type"] == "audio" for stream in probe["streams"])
    if audio_streams:
        with clip.with_suffix(".audio-decode.log").open("wb") as log:
            subprocess.run([str(FFMPEG_BIN / "ffmpeg.exe"), "-v", "error", "-xerror", "-i", str(clip),
                "-map", "0:a", "-f", "null", "-"], stdout=log, stderr=subprocess.STDOUT,
                timeout=timeout, check=True, creationflags=CREATE_NO_WINDOW)
    if int(video["nb_read_frames"]) != evidence["decoded_frames"]:
        raise ValueError(f"ffprobe and full marker decode frame counts disagree for {clip}")
    nominal_fps = float(Fraction(video["r_frame_rate"])) if video["r_frame_rate"] != "0/0" else None
    return {"file": clip.name, "codec": video["codec_name"], "width": video["width"], "height": video["height"],
            "video_duration_s": duration, "nominal_fps": nominal_fps,
            "timestamps": inspect_timestamps(clip, timeout), "audio_streams": audio_streams,
            "encoded_fps": evidence["decoded_frames"] / duration,
            "observed_unique_source_fps_lower_bound": evidence["observed_distinct_frame_lower_bound"] / duration,
            "ffprobe_s": probe_time, "full_decode_s": time.monotonic() - started, **evidence}


def run_case(args, case: Case, directory: Path, exe: Path, monitor: dict, psutil_module) -> dict:
    source_fps = args.source_fps if case.source_fps is None else case.source_fps
    source_w, source_h = args.source_size
    output = directory / "artifacts"
    output.mkdir(parents=True)
    source_stop = directory / "source.stop"
    capture_stop = directory / "capture.stop"
    source_log = directory / "source.log"
    capture_log = directory / "capture.log"
    result = {"case": asdict(case), "source_fps_requested": source_fps, "samples": [], "media": [],
              "scaling": args.scaling, "crop_requested": args.crop, "audio_requested": args.audio}
    source = capture = None
    ready = None
    with source_log.open("wb") as source_stdout, capture_log.open("wb") as capture_stdout:
        try:
            source = subprocess.Popen([str(exe), "--source", "--width", str(source_w), "--height", str(source_h),
                "--fps", str(source_fps), "--x", str(monitor["x"]), "--y", str(monitor["y"]),
                "--stop-file", str(source_stop)], stdout=source_stdout, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW)
            ready = await_source(source, source_log)
            result["source"] = ready
            capture = subprocess.Popen([str(exe), "--capture", "--output", str(output),
                "--stop-file", str(capture_stop), "--monitor", monitor["device_path"], "--hwnd", str(ready["hwnd"]),
                "--backend", case.backend, "--encoder", case.encoder, "--codec", case.codec, "--storage", case.storage,
                "--width", str(case.width), "--height", str(case.height), "--fps", str(case.fps),
                "--bitrate-kbps", str(args.bitrate_kbps), "--warmup", str(args.warmup), "--duration", str(args.duration),
                "--save-seconds", str(args.save_seconds), "--saves", str(args.saves), "--manual", str(int(args.manual)),
                "--scaling", args.scaling, "--audio", str(int(args.audio)),
                *(["--crop", args.crop] if args.crop else [])],
                stdout=capture_stdout, stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW)
            started = time.monotonic()
            next_sample = started
            while capture.poll() is None:
                now = time.monotonic()
                if source.poll() is not None:
                    raise RuntimeError(f"Stimulus exited while capture was active. See {source_log}")
                if now - started > args.warmup + args.duration + 70:
                    raise TimeoutError(f"Capture process exceeded its time budget. See {capture_log}")
                if now >= next_sample:
                    sample = {"elapsed_s": now - started, **sample_process(capture, psutil_module)}
                    if args.gpu_executable:
                        sample["gpu"] = sample_gpu(args.gpu_executable)
                    result["samples"].append(sample)
                    next_sample = now + 1
                time.sleep(min(0.2, max(0.01, next_sample - time.monotonic())))
        except (OSError, ValueError, RuntimeError) as error:
            result["error"] = str(error)
        finally:
            if capture is not None:
                result["capture_graceful_stop"] = stop_child(capture, capture_stop)
                result["capture_exit"] = capture.returncode
            if source is not None:
                result["source_graceful_stop"] = stop_child(source, source_stop)
                result["source_exit"] = source.returncode

    result["capture_identity"] = markers(capture_log, "FPS_CAPTURE_READY")
    result["capture_stats"] = markers(capture_log, "FPS_CAPTURE_STATS")
    result["capture_health"] = [event for event in markers(capture_log, "FTHR_DIAGNOSTIC_EVENT")
                                if event.get("event") == "capture_health_snapshot"]
    result["saves"] = markers(capture_log, "FPS_SAVE")
    result["source_stats"] = markers(source_log, "FPS_SOURCE_STATS")
    done = markers(source_log, "FPS_SOURCE_DONE")
    if done and done[-1]["elapsed_s"] > 0:
        result["source_presents_per_second"] = done[-1]["presented"] / done[-1]["elapsed_s"]
    errors = [result["error"]] if "error" in result else []
    crop = None
    match = re.search(r"\[Crop\] Encoder input (\d+)x(\d+) at (\d+),(\d+)",
                      capture_log.read_text(encoding="utf-8", errors="replace"))
    if match:
        crop_w, crop_h, crop_x, crop_y = map(int, match.groups())
        crop = (crop_x, crop_y, crop_w, crop_h)
        result["crop_applied"] = crop
    elif args.crop and tuple(map(float, args.crop.split(","))) != (0, 0, 1, 1):
        errors.append("Native engine did not report applying the requested crop")
    expected_size = case.width, case.height
    if crop:
        scale = min(case.width / crop_w, case.height / crop_h)
        expected_size = tuple(max(2, int(size * scale) & ~1) for size in (crop_w, crop_h))
    if result.get("capture_exit") != 0 or result.get("source_exit") != 0:
        errors.append("Native capture or stimulus did not complete successfully")
    clips = sorted(output.glob("*.mp4"))
    if len(clips) != args.saves + int(args.manual):
        errors.append("Some requested MP4 files are missing")
    if ready is not None:
        for clip in clips:
            try:
                media = inspect_clip(clip, ready, monitor, case, max(90, 2 * args.duration), args.scaling, crop)
                result["media"].append(media)
                if (media["width"], media["height"]) != expected_size:
                    errors.append(f"Output size mismatch in {clip.name}")
                if media["marker_coverage"] < args.min_marker_coverage:
                    errors.append(f"Frame ID marker coverage below threshold in {clip.name}")
                if media["encoded_fps"] < case.fps * args.min_encoded_ratio:
                    errors.append(f"Encoded FPS below threshold in {clip.name}")
                if media["observed_unique_source_fps_lower_bound"] < case.fps * args.min_unique_ratio:
                    errors.append(f"Distinct source FPS below threshold in {clip.name}")
            except (OSError, ValueError, RuntimeError, StopIteration, subprocess.SubprocessError) as error:
                errors.append(str(error))
    if source_fps and result.get("source_presents_per_second", 0) < source_fps * args.min_source_ratio:
        errors.append("D3D11 source did not meet the requested render-rate threshold")
    result["errors"] = errors
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New directory outside the repo")
    parser.add_argument("--exe", type=Path, help="Prebuilt harness executable")
    parser.add_argument("--build-only", action="store_true", help="Compile without creating a window or starting capture")
    parser.add_argument("--monitor", help="Exact monitor device path. Defaults to primary display")
    parser.add_argument("--case", type=parse_case, action="append", help="Repeat for a sequential case matrix")
    parser.add_argument("--source-size", type=dimensions, default=(1280, 720))
    parser.add_argument("--source-fps", type=int, default=180, help="0 presents once for a static stimulus")
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--duration", type=int, default=12)
    parser.add_argument("--save-seconds", type=int, default=5)
    parser.add_argument("--saves", type=int, default=1)
    parser.add_argument("--manual", action="store_true")
    parser.add_argument("--audio", action="store_true", help="Enable the engine's default audio capture")
    parser.add_argument("--scaling", choices=("stretch", "fit"), default="stretch")
    parser.add_argument("--crop", type=crop_rect, help="Normalized x,y,width,height. Keep the frame-ID strip visible.")
    parser.add_argument("--bitrate-kbps", type=int, default=12000)
    parser.add_argument("--nvidia-smi", action="store_true", help="Sample whole-GPU telemetry with extra overhead")
    parser.add_argument("--min-encoded-ratio", type=float, default=0.0, help="Optional FPS / target threshold")
    parser.add_argument("--min-unique-ratio", type=float, default=0.0, help="Optional distinct source FPS / target threshold; leave zero for static or low-rate sources")
    parser.add_argument("--min-source-ratio", type=float, default=0.0, help="Optional Presents / source target threshold")
    parser.add_argument("--min-marker-coverage", type=float, default=0.95)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        parser.error("The performance harness requires Windows")
    if (not 0 <= args.source_fps <= 480 or not 320 <= args.source_size[0] <= 7680
            or not 180 <= args.source_size[1] <= 4320 or not 0 <= args.warmup <= 120
            or not 1 <= args.duration <= 3600 or not 1 <= args.save_seconds <= 120 or not 1 <= args.saves <= 32
            or not 500 <= args.bitrate_kbps <= 200000 or not all(0 <= ratio <= 1 for ratio in
                (args.min_encoded_ratio, args.min_source_ratio, args.min_marker_coverage, args.min_unique_ratio))):
        parser.error("A capture, source or measurement option is out of range")
    output = args.output.resolve()
    if output.is_relative_to(ROOT) or output.exists():
        parser.error("--output must be a new directory outside the repository")
    args.gpu_executable = shutil.which("nvidia-smi") if args.nvidia_smi else None
    if args.nvidia_smi and not args.gpu_executable:
        parser.error("nvidia-smi was requested but is unavailable")
    if args.exe is not None and not args.exe.is_file():
        parser.error(f"Harness executable does not exist: {args.exe}")
    output.mkdir(parents=True)
    exe = args.exe.resolve() if args.exe else build_harness(output, ROOT / "tools" / f"{NAME.replace('-', '_')}.cpp", NAME, 300)
    if args.build_only:
        print(exe)
        return 0

    from FTHR_UI.core.windows_monitor import enumerate_windows_monitors
    try:
        import psutil
    except ImportError:
        psutil = None
    monitor = next((item for item in enumerate_windows_monitors() if
        (item.device_path.casefold() == args.monitor.casefold() if args.monitor else item.primary)), None)
    if monitor is None:
        parser.error("Selected monitor is unavailable")
    results = {"schema_version": 1, "harness": str(exe), "monitor": asdict(monitor), "runs": []}
    for index, case in enumerate(args.case or [Case("dxgi", "nvidia", 1280, 720, 60, "memory", "h264", None)], 1):
        directory = output / f"{index:02d}-{case.backend}-{case.encoder}-{case.width}x{case.height}-{case.fps}"
        result = run_case(args, case, directory, exe, results["monitor"], psutil)
        results["runs"].append(result)
        (output / "results.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"output": str(directory), "media": result["media"], "errors": result["errors"]}), flush=True)
    return int(any(run["errors"] for run in results["runs"]))


if __name__ == "__main__":
    sys.exit(main())
