#!/usr/bin/env python3
"""Opt-in AMD-only isolation qualification runner.

The native helper is deliberately built in a caller-supplied, fresh output
directory.  This runner does not alter the checkout, create the application's
shared-memory mapping, or fall back to another vendor.  A and B validate
controlled moving/color content; C validates only real WGC/SaveClip evidence
and never claims controlled content.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from run_windows_capture_smoke import build_harness  # noqa: E402


AMD_REQUIRED_EXIT = 20
AMF_UNAVAILABLE_EXIT = 21
HARNESS_FAILURE_EXIT = 22
DEFAULT_TIMEOUT_SECONDS = 90.0
CONTROLLED_SAMPLE_FRAMES = 12
CONTROLLED_CHECK_WIDTH = 320
CONTROLLED_CHECK_HEIGHT = 180
BLACK_LUMA_THRESHOLD = 3.0
MOTION_DELTA_THRESHOLD = 0.5
QUADRANT_SEPARATION_THRESHOLD = 8.0
EXPECTED_COLOR_DISTANCE_THRESHOLD = 100.0


def validate_monotonic_pts(values: Sequence[float]) -> dict[str, Any]:
    """Return deterministic evidence that decoded PTS values are increasing."""
    if len(values) < 2:
        return {"valid": False, "count": len(values), "reason": "too_few_pts"}
    previous = None
    for index, value in enumerate(values):
        if not math.isfinite(value):
            return {
                "valid": False,
                "count": len(values),
                "reason": f"non_finite_pts_at_{index}",
            }
        if previous is not None and value <= previous:
            return {
                "valid": False,
                "count": len(values),
                "reason": f"non_increasing_pts_at_{index}",
            }
        previous = value
    return {
        "valid": True,
        "count": len(values),
        "first": values[0],
        "last": values[-1],
        "elapsed": values[-1] - values[0],
    }


def classify_rgb_frames(width: int, height: int, raw: bytes) -> dict[str, Any]:
    """Check A/B's deterministic quadrants and moving bar after full decode.

    This intentionally uses a few stable interior samples instead of judging
    the whole lossy image.  It is pure and is exercised by the runner tests.
    """
    frame_size = width * height * 3
    if width < 4 or height < 4 or frame_size <= 0:
        return {"valid": False, "reason": "invalid_dimensions"}
    count = len(raw) // frame_size
    if count < 2 or len(raw) != count * frame_size:
        return {"valid": False, "reason": "incomplete_rgb_frames", "frame_count": count}

    def pixel(frame: int, x: int, y: int) -> tuple[int, int, int]:
        offset = frame * frame_size + (y * width + x) * 3
        return tuple(raw[offset:offset + 3])  # type: ignore[return-value]

    points = [
        (max(1, width // 4), max(1, height // 4)),
        (min(width - 2, (3 * width) // 4), max(1, height // 4)),
        (max(1, width // 4), min(height - 2, (3 * height) // 4)),
        (min(width - 2, (3 * width) // 4), min(height - 2, (3 * height) // 4)),
    ]
    means: list[float] = []
    quadrant_samples: list[list[tuple[int, int, int]]] = [[] for _ in points]
    for frame in range(count):
        total = 0.0
        for quadrant, (x, y) in enumerate(points):
            sample = pixel(frame, x, y)
            quadrant_samples[quadrant].append(sample)
            total += sum(sample) / 3.0
        means.append(total / len(points))

    luma = sum(means) / len(means)
    separations = []
    for left in range(len(points)):
        for right in range(left + 1, len(points)):
            separations.append(
                sum(abs(a - b) for a, b in zip(
                    quadrant_samples[left][0], quadrant_samples[right][0], strict=True))
            )
    motion = sum(abs(means[index] - means[index - 1]) for index in range(1, count)) / (count - 1)
    # The bar can pass through a sampled quadrant at some frames.  Include a
    # coarse row scan so a bar crossing a quadrant boundary is still visible.
    row_y = height // 2
    row_means = []
    for frame in range(count):
        row_means.append(
            sum(sum(pixel(frame, x, row_y)) / 3.0 for x in range(0, width, max(1, width // 16)))
            / len(range(0, width, max(1, width // 16)))
        )
    row_motion = sum(abs(row_means[index] - row_means[index - 1]) for index in range(1, count)) / (count - 1)
    moving = motion > MOTION_DELTA_THRESHOLD or row_motion > MOTION_DELTA_THRESHOLD
    distinct = min(separations, default=0.0) > QUADRANT_SEPARATION_THRESHOLD
    # BuildPattern's BGRA source is intentionally unambiguous: after RGB
    # decode the quadrants are blue, green, red, then neutral gray.  A bounded
    # Euclidean tolerance covers YUV conversion/lossy compression while
    # rejecting swapped channels and the materially wrong near-colors that a
    # pairwise-separation-only check would accept.
    first_samples = [samples[0] for samples in quadrant_samples]
    expected_colors = ((35, 35, 220), (35, 220, 35), (220, 35, 35), (175, 175, 175))
    color_distances = [
        math.sqrt(sum((actual - expected) ** 2 for actual, expected in zip(
            sample, expected_color, strict=True)))
        for sample, expected_color in zip(first_samples, expected_colors, strict=True)
    ]
    color_matches = [distance <= EXPECTED_COLOR_DISTANCE_THRESHOLD for distance in color_distances]
    # A coarse full-image diff catches motion even when the bar is between
    # the four quadrant samples or happens to preserve their means.
    x_step = max(1, width // 32)
    y_step = max(1, height // 18)
    diff_total = 0.0
    diff_count = 0
    for frame in range(1, count):
        for y in range(0, height, y_step):
            for x in range(0, width, x_step):
                current = pixel(frame, x, y)
                previous = pixel(frame - 1, x, y)
                diff_total += sum(abs(a - b) for a, b in zip(current, previous, strict=True)) / 3.0
                diff_count += 1
    full_image_motion = diff_total / diff_count if diff_count else 0.0
    moving = moving or full_image_motion > MOTION_DELTA_THRESHOLD
    valid = luma > BLACK_LUMA_THRESHOLD and moving and distinct and all(color_matches)
    return {
        "valid": valid,
        "moving": moving,
        "nonblack": luma > BLACK_LUMA_THRESHOLD,
        "quadrants_distinct": distinct,
        "quadrants_match_expected": all(color_matches),
        "quadrant_color_matches": color_matches,
        "quadrant_color_distances": color_distances,
        "frame_count": count,
        "mean_luma": luma,
        "mean_motion": motion,
        "row_motion": row_motion,
        "full_image_motion": full_image_motion,
        "minimum_quadrant_separation": min(separations, default=0.0),
        "reason": None if valid else "controlled_content_check_failed",
    }


def validate_helper_identity(log_text: str, mode: str) -> tuple[bool, str]:
    """Fail closed unless the helper reports the exact AMD backend/mode."""
    required = ["vendor=AMD", "backend=ffmpeg-amf"]
    mode_token = {
        "a": "mode=direct_gpu",
        "b": "mode=production_transform",
        "c": "mode=real_wgc_saveclip",
    }[mode]
    required.append(mode_token)
    if mode == "c":
        required.append("capture_backend=WGC_MONITOR")
    missing = [token for token in required if token not in log_text]
    return (not missing, "" if not missing else "missing_identity:" + ",".join(missing))


def parse_reported_identity(log_text: str, mode: str) -> dict[str, str]:
    """Extract the helper's key/value evidence into structured report data."""
    native_stage = mode if mode == "probe" else mode.upper()
    marker = f"AMD_ISOLATION stage={native_stage} "
    for line in log_text.splitlines():
        if marker not in line:
            continue
        fields: dict[str, str] = {}
        for match in re.finditer(r"([A-Za-z_]+)=(?:\"([^\"]*)\"|(\S+))", line):
            fields[match.group(1)] = match.group(2) if match.group(2) is not None else match.group(3)
        return fields
    return {}


def parse_save_attempts(log_text: str) -> dict[str, int]:
    """Count actual C SaveClip calls; a planned but unrun C is not a failure."""
    attempts = sum(
        1 for line in log_text.splitlines()
        if "AMD_ISOLATION SAVE_ATTEMPT stage=C" in line
    )
    completed = int(
        "AMD_ISOLATION stage=C status=transport_decode_pass_content_pending" in log_text
    )
    return {
        "planned": 1,
        "denominator": attempts,
        "attempted": attempts,
        "completed": completed if attempts else 0,
        "failed": max(0, attempts - (completed if attempts else 0)),
    }


def _run_process(command: Sequence[str], *, timeout: float, text: bool = True) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        list(command),
        capture_output=True,
        text=text,
        timeout=timeout,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def run_logged(command: Sequence[str], log_path: Path, timeout: float) -> dict[str, Any]:
    """Run a stage with a hard subprocess timeout and retain stdout/stderr."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command_text = shlex.join([str(item) for item in command])
    try:
        completed = _run_process(command, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or ""
        stderr = error.stderr or ""
        log_path.write_text(
            f"COMMAND {command_text}\nTIMEOUT seconds={timeout}\n{stdout}\n{stderr}",
            encoding="utf-8",
            errors="replace",
        )
        return {"status": "timeout", "returncode": None, "timeout_seconds": timeout}
    except OSError as error:
        log_path.write_text(
            f"COMMAND {command_text}\nPROCESS_ERROR {error}\n",
            encoding="utf-8",
            errors="replace",
        )
        return {"status": "error", "returncode": None, "error": str(error)}
    log_path.write_text(
        f"COMMAND {command_text}\nRETURN_CODE {completed.returncode}\n"
        f"STDOUT\n{completed.stdout}\nSTDERR\n{completed.stderr}\n",
        encoding="utf-8",
        errors="replace",
    )
    return {
        "status": "completed",
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _json_command(command: Sequence[str], timeout: float) -> tuple[dict[str, Any] | None, str | None]:
    try:
        completed = _run_process(command, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, str(error)
    if completed.returncode != 0:
        return None, completed.stderr.strip() or f"command_exit_{completed.returncode}"
    try:
        return json.loads(completed.stdout), None
    except json.JSONDecodeError as error:
        return None, f"invalid_json:{error}"


def _runtime_tool(name: str) -> Path:
    path = ROOT / "FTHRcapture" / "FTHRclips" / "third_party" / "ffmpeg" / "bin" / name
    return path


def _stream_info(ffprobe: Path, media: Path, timeout: float) -> tuple[dict[str, Any] | None, str | None]:
    data, error = _json_command(
        [str(ffprobe), "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=codec_name,width,height,avg_frame_rate,nb_frames", "-of", "json", str(media)],
        timeout,
    )
    if error:
        return None, error
    streams = data.get("streams", []) if data else []
    if not streams:
        return None, "video_stream_missing"
    stream = streams[0]
    try:
        width = int(stream["width"])
        height = int(stream["height"])
        rate_text = str(stream["avg_frame_rate"])
        numerator, denominator = rate_text.split("/", 1)
        rate = float(numerator) / float(denominator)
    except (KeyError, ValueError, ZeroDivisionError) as error:
        return None, f"invalid_stream_metadata:{error}"
    stream = dict(stream)
    stream.update({"width": width, "height": height, "fps": rate})
    return stream, None


def _pts_info(ffprobe: Path, media: Path, timeout: float) -> tuple[dict[str, Any], str | None]:
    data, error = _json_command(
        [str(ffprobe), "-v", "error", "-select_streams", "v:0", "-show_frames",
         "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(media)],
        timeout,
    )
    if error:
        return {}, error
    values = []
    for frame in data.get("frames", []):
        try:
            values.append(float(frame["best_effort_timestamp_time"]))
        except (KeyError, TypeError, ValueError):
            return {}, "invalid_decoded_pts"
    result = validate_monotonic_pts(values)
    return result, None if result.get("valid") else str(result.get("reason"))


def _decode_media(
    ffmpeg: Path,
    ffprobe: Path,
    media: Path,
    expected_size: tuple[int, int] | None,
    expected_fps: float,
    controlled: bool,
    expected_frame_count: int | None,
    timeout: float,
) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(media), "valid": False}
    if not media.is_file() or media.stat().st_size == 0:
        result["reason"] = "media_missing_or_empty"
        return result
    stream, error = _stream_info(ffprobe, media, timeout)
    if error:
        result["reason"] = error
        return result
    result["stream"] = stream
    if stream["codec_name"] != "h264":
        result["reason"] = f"unexpected_codec:{stream['codec_name']}"
        return result
    if expected_size and (stream["width"], stream["height"]) != expected_size:
        result["reason"] = f"unexpected_dimensions:{stream['width']}x{stream['height']}"
        return result
    if abs(stream["fps"] - expected_fps) > max(0.5, expected_fps * 0.03):
        result["reason"] = f"unexpected_fps:{stream['fps']}"
        return result
    decode = _run_process(
        [str(ffmpeg), "-v", "error", "-xerror", "-err_detect", "explode", "-i", str(media),
         "-map", "0:v:0", "-f", "null", "-"],
        timeout=timeout,
    )
    if decode.returncode != 0:
        result["reason"] = "strict_decode_failed"
        result["decode_stderr"] = decode.stderr[-2000:]
        return result
    pts, error = _pts_info(ffprobe, media, timeout)
    result["pts"] = pts
    if error:
        result["reason"] = error
        return result
    if expected_frame_count is not None and pts["count"] < expected_frame_count:
        result["reason"] = (
            f"decoded_frame_coverage_short:{pts['count']}<{expected_frame_count}"
        )
        return result
    if controlled:
        sample_count = min(CONTROLLED_SAMPLE_FRAMES, int(pts["count"]))
        if sample_count < 2:
            result["reason"] = "too_few_controlled_samples"
            return result
        sample_indices = [
            round(index * (int(pts["count"]) - 1) / (sample_count - 1))
            for index in range(sample_count)
        ]
        select_expression = "+".join(f"eq(n,{index})" for index in sample_indices)
        raw = _run_process(
            [str(ffmpeg), "-v", "error", "-xerror", "-i", str(media), "-frames:v",
             str(sample_count), "-vf",
             f"scale={CONTROLLED_CHECK_WIDTH}:{CONTROLLED_CHECK_HEIGHT}:flags=bilinear,"
             f"select='{select_expression}'", "-vsync", "0", "-f", "rawvideo",
             "-pix_fmt", "rgb24", "-"],
            timeout=timeout,
            text=False,
        )
        if raw.returncode != 0:
            result["reason"] = "controlled_content_decode_failed"
            return result
        result["content"] = classify_rgb_frames(
            CONTROLLED_CHECK_WIDTH, CONTROLLED_CHECK_HEIGHT, raw.stdout)
        if not result["content"]["valid"]:
            result["reason"] = result["content"]["reason"]
            return result
    else:
        result["content"] = {"controlled_content": False, "status": "not_claimed"}
    result["valid"] = True
    return result


def validate_output_directory(output: Path) -> Path:
    output = output.resolve()
    if output.is_relative_to(ROOT):
        raise ValueError("--output must be outside the repository")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise FileExistsError(f"--output must be a new or empty directory: {output}")
    output.mkdir(parents=True, exist_ok=True)
    return output


def _stage_command(executable: Path, mode: str, output: Path, args: argparse.Namespace) -> list[str]:
    command = [str(executable), "--mode", mode, "--output", str(output)]
    command += ["--adapter-index", str(args.adapter_index), "--width", str(args.width),
                "--height", str(args.height), "--output-width", str(args.output_width),
                "--output-height", str(args.output_height), "--fps", str(args.fps),
                "--frames", str(args.frames), "--warmup-seconds", str(args.warmup_seconds),
                "--duration-seconds", str(args.duration_seconds)]
    if args.monitor_path:
        command += ["--monitor-path", args.monitor_path]
    return command


def _run_stage(executable: Path, mode: str, output: Path, args: argparse.Namespace) -> dict[str, Any]:
    artifact = output / f"{mode}.mp4"
    log = output / f"{mode}.log"
    command = _stage_command(executable, mode, artifact, args)
    stage_timeout = max(
        args.timeout,
        float(args.warmup_seconds + args.duration_seconds + 30) if mode == "c" else args.timeout,
    )
    run = run_logged(command, log, stage_timeout)
    stage: dict[str, Any] = {
        "mode": mode,
        "artifact": str(artifact),
        "log": str(log),
        "command": command,
        "run": {key: value for key, value in run.items() if key not in {"stdout", "stderr"}},
    }
    if mode == "c":
        stage["save_attempts"] = {
            "planned": 1, "denominator": 0, "attempted": 0, "completed": 0, "failed": 0,
        }
    log_text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    if mode == "c":
        stage["save_attempts"] = parse_save_attempts(log_text)
    if run["status"] == "timeout":
        stage.update({"status": "timeout", "reason": "subprocess_timeout",
                      "timeout_seconds": stage_timeout})
        return stage
    if run["status"] != "completed":
        stage.update({"status": "failed", "reason": run.get("error", "subprocess_error")})
        return stage
    log_text = log.read_text(encoding="utf-8", errors="replace")
    if mode == "probe":
        identity_ok = "vendor=AMD" in log_text and "AMD_ISOLATION stage=probe status=pass" in log_text
        stage["identity"] = {"valid": identity_ok, "reason": "" if identity_ok else "missing_probe_amd_identity"}
        if run["returncode"] == AMD_REQUIRED_EXIT:
            stage.update({"status": "AMD_HARDWARE_REQUIRED", "reason": "AMD_HARDWARE_REQUIRED"})
        elif run["returncode"] == 0 and identity_ok:
            stage["status"] = "pass"
        else:
            stage.update({"status": "failed", "reason": f"probe_exit_{run['returncode']}"})
        return stage
    identity_ok, identity_reason = validate_helper_identity(log_text, mode)
    stage["identity"] = {
        "valid": identity_ok,
        "reason": identity_reason,
        "reported": parse_reported_identity(log_text, mode),
    }
    if not identity_ok:
        stage.update({"status": "failed", "reason": identity_reason})
        return stage
    if run["returncode"] != 0:
        stage.update({"status": "failed", "reason": f"helper_exit_{run['returncode']}"})
        return stage
    expected_size = (args.width, args.height) if mode == "a" else (args.output_width, args.output_height)
    try:
        stage["media"] = _decode_media(
            _runtime_tool("ffmpeg.exe"), _runtime_tool("ffprobe.exe"), artifact,
            expected_size, float(args.fps), mode in {"a", "b"},
            args.frames if mode in {"a", "b"} else None, args.timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        stage["media"] = {"valid": False, "reason": f"subprocess_error:{error}"}
    if not stage["media"]["valid"]:
        stage.update({"status": "failed", "reason": stage["media"].get("reason", "media_validation_failed")})
    else:
        stage["status"] = (
            "transport_decode_pass_content_pending" if mode == "c" else "pass"
        )
    if mode == "c":
        stage["save_attempts"] = parse_save_attempts(log_text)
    return stage


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new external evidence directory")
    parser.add_argument("--monitor-path", help="explicit WGC monitor device path required for C")
    parser.add_argument("--adapter-index", type=int, default=0)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=360)
    parser.add_argument("--output-width", type=int, default=960)
    parser.add_argument("--output-height", type=int, default=540)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--warmup-seconds", type=int, default=8)
    parser.add_argument("--duration-seconds", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
                        help="hard timeout per helper/validation subprocess")
    return parser


def validate_runner_args(args: argparse.Namespace) -> None:
    """Mirror the native diagnostic bounds before starting a build/capture."""
    if args.frames < CONTROLLED_SAMPLE_FRAMES:
        raise ValueError(f"--frames must be at least {CONTROLLED_SAMPLE_FRAMES}")
    for name in ("width", "height", "output_width", "output_height"):
        value = getattr(args, name)
        if value < 64 or value > 4096 or value % 2:
            raise ValueError(f"--{name.replace('_', '-')} must be even and in [64,4096]")
    if args.fps < 1 or args.fps > 360:
        raise ValueError("--fps must be in [1,360]")


def main(argv: Sequence[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    try:
        validate_runner_args(args)
    except ValueError as error:
        print(f"AMD_ISOLATION_RUNNER_ERROR {error}", file=sys.stderr)
        return 2
    results: dict[str, Any] = {
        "schema_version": 1,
        "status": "running",
        "hardware_required": True,
        "adapter_index": args.adapter_index,
        "source_size": [args.width, args.height],
        "output_size": [args.output_width, args.output_height],
        "fps": args.fps,
        "subprocess_timeout_seconds": args.timeout,
        "stages": {},
        "save_attempts": {
            "planned": 1, "denominator": 0, "attempted": 0, "completed": 0, "failed": 0,
        },
        "unresolved_boundaries": [
            "pinned_ffmpeg_amf_receive_packet_has_no_outer_deadline",
            "same_driver_gpu_queue_ordering_not_proven",
        ],
    }
    try:
        output = validate_output_directory(args.output)
    except (OSError, ValueError) as error:
        print(f"AMD_ISOLATION_RUNNER_ERROR {error}", file=sys.stderr)
        return 2
    result_path = output / "result.json"
    results["status"] = "building"
    result_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    try:
        build_dir = output / "build"
        build_dir.mkdir(parents=True, exist_ok=True)
        executable = build_harness(
            build_dir, TOOLS / "amd_isolation_smoke.cpp", "amd-isolation-smoke",
            timeout_seconds=max(600.0, args.timeout * 8),
        )
        results["build"] = {"status": "pass", "output": str(build_dir), "executable": str(executable)}
    except Exception as error:  # noqa: BLE001 - retain build diagnostics in result
        results["status"] = "failed"
        results["build"] = {"status": "failed", "reason": str(error)}
        result_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"AMD_ISOLATION_BUILD_FAILED {error}", file=sys.stderr)
        return 1

    probe = _run_stage(executable, "probe", output, args)
    results["stages"]["probe"] = probe
    if probe.get("run", {}).get("returncode") == AMD_REQUIRED_EXIT:
        results["status"] = "AMD_HARDWARE_REQUIRED"
        for mode in ("a", "b", "c"):
            results["stages"][mode] = {"status": "not_run", "reason": "AMD_HARDWARE_REQUIRED"}
        result_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print("AMD_HARDWARE_REQUIRED")
        return AMD_REQUIRED_EXIT
    if probe.get("status") != "pass":
        results["status"] = "failed"
        result_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        return 1

    for mode in ("a", "b"):
        results["stages"][mode] = _run_stage(executable, mode, output, args)
    if args.monitor_path:
        results["stages"]["c"] = _run_stage(executable, "c", output, args)
    else:
        results["stages"]["c"] = {"status": "not_run", "reason": "monitor_path_required"}
    ab_pass = all(results["stages"][mode].get("status") == "pass" for mode in ("a", "b"))
    c_transport = results["stages"]["c"].get("status") == "transport_decode_pass_content_pending"
    all_pass = ab_pass and c_transport
    results["save_attempts"] = results["stages"]["c"].get(
        "save_attempts", results["save_attempts"])
    results["status"] = "PARTIAL" if all_pass else "failed"
    result_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps({"status": results["status"], "result": str(result_path)}, indent=2))
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
