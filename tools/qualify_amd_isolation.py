#!/usr/bin/env python3
"""Opt-in AMD-only isolation qualification runner.

The native helper is deliberately built in a caller-supplied, fresh output
directory.  This runner does not alter the checkout, create the application's
shared-memory mapping, or fall back to another vendor.  A and B validate
controlled moving/color content; C validates only real WGC/SaveClip evidence
and never claims controlled content. A uses the production single-texture
pool. --compare-array adds the old texture-array layout as a separate
diagnostic control after the production stages.
--compare-nv12 runs paired BGRA/NV12 input-format trials in alternating order;
content, identity and CPU-side timings are evaluated independently of production.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import statistics
import subprocess
import sys
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

def build_harness(*args, **kwargs):
    # Prebuilt-helper delivery is stdlib-only; build support stays optional.
    from run_windows_capture_smoke import build_harness as build
    return build(*args, **kwargs)


AMD_REQUIRED_EXIT = 20
HELPER_PROTOCOL_VERSION = "3"
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
GRAY_LEVELS = (0, 64, 128, 255)
GRAY_LEVEL_TOLERANCE = 6


def file_evidence(path: Path) -> dict[str, Any]:
    """Hash actual bytes; absence is evidence, not a synthesized identity."""
    if not path.is_file():
        return {"path": str(path), "available": False}
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": str(path), "available": True,
            "size_bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def apply_geometry_profile(args: argparse.Namespace) -> None:
    if args.field_geometry:
        args.width, args.height = 1920, 1080
        args.output_width, args.output_height = 2560, 1440


def parse_amf_failure_evidence(log_text: str) -> dict[str, Any]:
    """Keep observed native log codes separate from FFmpeg/HRESULT domains.

    Correlation is limited to this isolated helper subprocess and its FFmpeg
    context token. It is not an underlying HRESULT or proof of a driver cause.
    """
    native: list[dict[str, Any]] = []
    boundaries: list[dict[str, Any]] = []
    pattern = re.compile(
        r"\[(h264_amf|hevc_amf|av1_amf) @ ([0-9A-Fa-f]+)\] "
        r"(SubmitInput|CreateSurfaceFromDX11Native|Drain)\(\) failed\s+with error (\d+)")
    for line in log_text.splitlines():
        match = pattern.search(line)
        if match and len(native) < 8:
            native.append({"domain": "AMF_RESULT", "result": int(match[4]),
                           "operation": match[3], "codec": match[1],
                           "ffmpeg_context": match[2], "source": "observed_ffmpeg_log",
                           "correlation_scope": "isolated_helper_subprocess",
                           "sdk_header_revision": "unavailable:not_recorded"})
        marker = "FTHR_DIAGNOSTIC_EVENT "
        if marker in line and len(boundaries) < 8:
            payload = line.split(marker, 1)[1]
            if len(payload) < 8192:
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and event.get("event") == "amf_boundary_failed":
                    boundaries.append(event)
    return {"native_amf_results": native, "boundary_events": boundaries}


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
    # Fixed neutral markers test range/gamma separately from quadrant colors.
    gray_errors = [
        max(abs(channel - level) for channel in pixel(
            frame, min(width - 1, (2 * index + 1) * width // 8), 15 * height // 16))
        for frame in range(count) for index, level in enumerate(GRAY_LEVELS)
    ]
    gray_valid = max(gray_errors, default=256) <= GRAY_LEVEL_TOLERANCE
    valid = luma > BLACK_LUMA_THRESHOLD and moving and distinct and all(color_matches) and gray_valid
    return {
        "valid": valid,
        "moving": moving,
        "nonblack": luma > BLACK_LUMA_THRESHOLD,
        "quadrants_distinct": distinct,
        "quadrants_match_expected": all(color_matches),
        "gray_levels_match_expected": gray_valid,
        "max_gray_level_error": max(gray_errors, default=256),
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
    """Require one matching stage record, including the actual input layout."""
    mode_token = {
        "a": "direct_gpu",
        "a-array": "direct_gpu",
        "b": "production_transform",
        "b-bgra": "comparison_transform",
        "b-nv12": "comparison_transform",
        "c": "real_wgc_saveclip",
    }[mode]
    required = {
        "vendor": "AMD", "backend": "ffmpeg-amf", "mode": mode_token,
        "protocol": HELPER_PROTOCOL_VERSION,
        "input_layout": "texture_array" if mode == "a-array" else "single_texture",
        "status": "transport_decode_pass_content_pending" if mode == "c" else "pass",
    }
    if mode == "c":
        required["capture_backend"] = "WGC_MONITOR"
    else:
        required["input_format"] = "NV12" if mode == "b-nv12" else "BGRA"
        required["input_array_size"] = "8" if mode == "a-array" else "1"
    if mode in {"b-bgra", "b-nv12"}:
        required.update({
            "color_conversion": "bt709_full_to_studio" if mode == "b-nv12" else "production_bgra",
            "pattern": "quadrants_gray_bar_v2", "codec": "h264_amf", "bitrate_kbps": "8000",
        })
    fields = parse_reported_identity(log_text, mode)
    missing = [f"{key}={value}" for key, value in required.items() if fields.get(key) != value]
    if mode != "c":
        try:
            slice_index = int(fields.get("input_slice", ""))
        except ValueError:
            slice_index = -1
        if not 0 <= slice_index < (8 if mode == "a-array" else 1):
            missing.append("valid_input_slice")
    return (not missing, "" if not missing else "missing_identity:" + ",".join(missing))


def parse_reported_identity(log_text: str, mode: str) -> dict[str, str]:
    """Extract the helper's key/value evidence into structured report data."""
    native_stage = mode if mode == "probe" else mode.upper().replace("-", "_")
    marker = f"AMD_ISOLATION stage={native_stage} "
    fields: dict[str, str] = {}
    for line in log_text.splitlines():
        if marker not in line:
            continue
        fields = {}
        for match in re.finditer(r"([A-Za-z_][A-Za-z_0-9]*)=(?:\"([^\"]*)\"|(\S+))", line):
            fields[match.group(1)] = match.group(2) if match.group(2) is not None else match.group(3)
    return fields


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


def _run_process(command: Sequence[str], *, timeout: float, text: bool = True,
                 runtime_dir: Path | None = None) -> subprocess.CompletedProcess[Any]:
    environment = None
    if runtime_dir is not None:
        environment = dict(os.environ)
        environment["PATH"] = str(runtime_dir) + os.pathsep + environment.get("PATH", "")
    return subprocess.run(
        list(command),
        capture_output=True,
        text=text,
        timeout=timeout,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        env=environment,
    )


def run_logged(command: Sequence[str], log_path: Path, timeout: float,
               runtime_dir: Path | None = None) -> dict[str, Any]:
    """Run a stage with a hard subprocess timeout and retain stdout/stderr."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command_text = shlex.join([str(item) for item in command])
    try:
        completed = _run_process(command, timeout=timeout, runtime_dir=runtime_dir)
    except subprocess.TimeoutExpired as error:
        def output_text(value: str | bytes | None) -> str:
            return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
        stdout = output_text(error.stdout)
        stderr = output_text(error.stderr)
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


def _runtime_tool(name: str, runtime_dir: Path | None = None) -> Path:
    path = (runtime_dir or ROOT / "FTHRcapture" / "FTHRclips" / "third_party" / "ffmpeg" / "bin") / name
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


def parse_comparison_timings(fields: dict[str, str], args: argparse.Namespace) -> dict[str, Any]:
    """Require measured evidence from the same final helper record as identity."""
    expected = {
        "width": args.output_width, "height": args.output_height,
        "source_width": args.width, "source_height": args.height,
        "fps": args.fps, "frames": args.frames, "submitted_frames": args.frames,
        "warmup_frames": min(12, args.frames // 4),
        "measured_frames": args.frames - min(12, args.frames // 4),
    }
    if fields.get("timing_scope") != "cpu_api_wall":
        return {"valid": False, "reason": "missing_cpu_timing_scope"}
    try:
        for key, value in expected.items():
            if int(fields.get(key, "")) != value:
                return {"valid": False, "reason": f"comparison_configuration_mismatch:{key}"}
        metrics = {"end_to_end_ms": float(fields["end_to_end_ms"])}
        if not math.isfinite(metrics["end_to_end_ms"]) or metrics["end_to_end_ms"] <= 0:
            return {"valid": False, "reason": "invalid_pipeline_timing"}
        for phase in ("upload", "prepare", "encode"):
            values = [float(fields[f"{phase}_p{percentile}_ms"]) for percentile in (50, 95, 99)]
            if (any(not math.isfinite(value) or value < 0 or value > metrics["end_to_end_ms"]
                    for value in values)
                    or values != sorted(values)):
                return {"valid": False, "reason": f"invalid_percentiles:{phase}"}
            metrics.update({f"{phase}_p{percentile}_ms": value
                            for percentile, value in zip((50, 95, 99), values, strict=True)})
    except (KeyError, ValueError, OverflowError):
        return {"valid": False, "reason": "missing_or_invalid_comparison_measurements"}
    return {"valid": True, "scope": "cpu_api_wall", "gpu_execution_time_measured": False,
            **expected, **metrics}


def run_format_comparison(executable: Path, output: Path, args: argparse.Namespace) -> dict[str, Any]:
    rounds = []
    for index in range(args.comparison_rounds):
        order = ("b-bgra", "b-nv12") if index % 2 == 0 else ("b-nv12", "b-bgra")
        directory = output / "nv12-comparison" / f"round-{index + 1}"
        directory.mkdir(parents=True)
        stages = {mode: {"status": "not_run", "role": "format_comparison",
                         "reason": "previous_stage_failed"} for mode in order}
        rounds.append({"order": list(order), "stages": stages})
        for mode in order:
            stages[mode] = _run_stage(executable, mode, directory, args)
            if stages[mode].get("status") != "pass":
                return {**summarize_format_comparison(rounds), "planned_rounds": args.comparison_rounds,
                        "first_failed_mode": mode, "stopped_after_failure": True}
    return {**summarize_format_comparison(rounds), "planned_rounds": args.comparison_rounds}


def summarize_format_comparison(rounds: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "status": "failed", "role": "format_comparison", "rounds": rounds,
        "production_default": "BGRA", "automatic_format_selection": False,
        "gpu_execution_time_measured": False,
    }
    if not rounds:
        return {**summary, "reason": "no_comparison_rounds"}
    identity = None
    ratios = []
    keys = ("adapter_luid_high", "adapter_luid_low", "driver_version",
            "codec", "fps", "width", "height", "source_width", "source_height",
            "frames", "bitrate_kbps", "input_layout", "input_array_size", "pattern")
    for run in rounds:
        stages = run["stages"]
        for mode in ("b-bgra", "b-nv12"):
            stage = stages[mode]
            if stage.get("status") != "pass" or not stage.get("timings", {}).get("valid"):
                return {**summary, "reason": f"invalid_comparison_stage:{mode}"}
            fields = stage["identity"]["reported"]
            try:
                int(fields["adapter_luid_high"])
                int(fields["adapter_luid_low"])
                if int(fields["driver_version"]) <= 0:
                    raise ValueError("driver identity unavailable")
                actual = tuple(fields[key] for key in keys)
            except (KeyError, ValueError):
                return {**summary, "reason": "adapter_or_driver_identity_unavailable"}
            if identity is None:
                identity = actual
            if actual != identity:
                return {**summary, "reason": "comparison_identity_or_configuration_changed"}
        ratio = (stages["b-nv12"]["timings"]["end_to_end_ms"]
                 / stages["b-bgra"]["timings"]["end_to_end_ms"])
        if not math.isfinite(ratio):
            return {**summary, "reason": "invalid_timing_ratio"}
        ratios.append(ratio)
    return {**summary, "status": "pass", "reason": None,
            "end_to_end_nv12_over_bgra": ratios,
            "median_end_to_end_nv12_over_bgra": statistics.median(ratios),
            "measurement_scope": "unpaced_synthetic_pipeline_including_pattern_upload_drain_and_mux",
            "gameplay_performance_proven": False}


def _run_stage(executable: Path, mode: str, output: Path, args: argparse.Namespace) -> dict[str, Any]:
    artifact = output / f"{mode}.mp4"
    log = output / f"{mode}.log"
    command = _stage_command(executable, mode, artifact, args)
    stage_timeout = max(
        args.timeout,
        float(args.warmup_seconds + args.duration_seconds + 30) if mode == "c" else args.timeout,
    )
    run = run_logged(command, log, stage_timeout, getattr(args, "ffmpeg_bin", None))
    stage: dict[str, Any] = {
        "mode": mode,
        "role": ("format_comparison" if mode in {"b-bgra", "b-nv12"} else
                 "comparison_control" if mode == "a-array" else
                 "identity_probe" if mode == "probe" else "production"),
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
    stage["evidence_files"] = [file_evidence(path) for path in (log, artifact)]
    stage["amf_failure_evidence"] = parse_amf_failure_evidence(log_text)
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
        fields = parse_reported_identity(log_text, mode)
        protocol_ok = fields.get("protocol") == HELPER_PROTOCOL_VERSION
        identity_ok = (fields.get("vendor") == "AMD"
                       and fields.get("backend") == "ffmpeg-amf"
                       and fields.get("status") == "pass" and protocol_ok)
        reason = ("" if identity_ok else
                  "helper_protocol_mismatch:rebuild_helper" if not protocol_ok else
                  "missing_probe_amd_identity")
        stage["identity"] = {"valid": identity_ok, "reason": reason, "reported": fields}
        if run["returncode"] == AMD_REQUIRED_EXIT:
            stage.update({"status": "AMD_HARDWARE_REQUIRED", "reason": "AMD_HARDWARE_REQUIRED"})
        elif run["returncode"] == 0 and identity_ok:
            stage["status"] = "pass"
        else:
            stage.update({"status": "failed", "reason": reason if run["returncode"] == 0
                          else f"probe_exit_{run['returncode']}"})
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
    if mode in {"b-bgra", "b-nv12"}:
        stage["timings"] = parse_comparison_timings(stage["identity"]["reported"], args)
        if not stage["timings"]["valid"]:
            stage.update({"status": "failed", "reason": stage["timings"]["reason"]})
            return stage
    if mode == "c" and (stage["save_attempts"]["attempted"] != 1
                        or stage["save_attempts"]["completed"] != 1):
        stage.update({"status": "failed", "reason": "missing_completed_save_attempt"})
        return stage
    direct = mode in {"a", "a-array"}
    controlled = direct or mode in {"b", "b-bgra", "b-nv12"}
    expected_size = (args.width, args.height) if direct else (args.output_width, args.output_height)
    try:
        stage["media"] = _decode_media(
            _runtime_tool("ffmpeg.exe", args.ffmpeg_bin), _runtime_tool("ffprobe.exe", args.ffmpeg_bin), artifact,
            expected_size, float(args.fps), controlled,
            args.frames if controlled else None, args.timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        stage["media"] = {"valid": False, "reason": f"subprocess_error:{error}"}
    if (mode in {"b-bgra", "b-nv12"} and stage["media"]["valid"]
            and stage["media"].get("pts", {}).get("count") != args.frames):
        stage["media"].update({"valid": False, "reason": "comparison_decoded_frame_count_mismatch"})
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
    parser.add_argument("--helper", type=Path,
                        help="use a prebuilt amd-isolation-smoke.exe; no compiler/build required")
    parser.add_argument("--ffmpeg-bin", type=Path,
                        help="explicit packaged pinned FFmpeg executable/DLL directory; recorded hashes identify actual bytes")
    parser.add_argument("--monitor-path", help="explicit WGC monitor device path required for C")
    parser.add_argument("--adapter-index", type=int, default=0)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--output-width", type=int, default=1920)
    parser.add_argument("--output-height", type=int, default=1080)
    parser.add_argument("--field-geometry", action="store_true",
                        help="opt-in 1920x1080 to 2560x1440; run the native-size default first")
    parser.add_argument("--compare-array", action="store_true",
                        help="run the legacy BGRA texture-array control after production; "
                             "its outcome is reported separately from production")
    parser.add_argument("--compare-nv12", action="store_true",
                        help="compare production BGRA with the explicit GPU NV12 candidate "
                             "in separate processes; production remains BGRA")
    parser.add_argument("--comparison-rounds", type=int, default=2,
                        help="1 to 3 format-comparison rounds; order alternates BGRA/NV12")
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
    if not 1 <= args.comparison_rounds <= 3:
        raise ValueError("--comparison-rounds must be in [1,3]")


def main(argv: Sequence[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    apply_geometry_profile(args)
    try:
        validate_runner_args(args)
        if args.ffmpeg_bin:
            args.ffmpeg_bin = args.ffmpeg_bin.resolve(strict=True)
            required = ("ffmpeg.exe", "ffprobe.exe", "avcodec-62.dll", "avdevice-62.dll",
                        "avfilter-11.dll", "avformat-62.dll", "avutil-60.dll",
                        "swresample-6.dll", "swscale-9.dll")
            missing = [name for name in required if not (args.ffmpeg_bin / name).is_file()]
            if missing:
                raise ValueError("--ffmpeg-bin missing runtime files: " + ", ".join(missing))
    except (OSError, ValueError) as error:
        print(f"AMD_ISOLATION_RUNNER_ERROR {error}", file=sys.stderr)
        return 2
    results: dict[str, Any] = {
        "schema_version": 3,
        "status": "running",
        "production_status": "not_run",
        "array_control_status": "not_run",
        "compare_array": args.compare_array,
        "compare_nv12": args.compare_nv12,
        "nv12_comparison": {"status": "not_run", "reason": "pending" if args.compare_nv12
                            else "comparison_not_requested"},
        "production_input_layout": "single_texture",
        "production_input_format": "BGRA",
        "hardware_required": True,
        "adapter_index": args.adapter_index,
        "source_size": [args.width, args.height],
        "output_size": [args.output_width, args.output_height],
        "fps": args.fps,
        "geometry_profile": "field_1080_to_1440" if args.field_geometry else "native_or_explicit",
        "source_evidence": [file_evidence(path) for path in (
            Path(__file__), TOOLS / "amd_isolation_smoke.cpp",
            ROOT / "FTHRcapture/FTHRclips/src/ffmpeg_amf_replay_encoder.cpp",
            ROOT / "FTHRcapture/FTHRclips/include/ffmpeg_amf_replay_encoder.h",
            TOOLS / "ffmpeg_manifest.json")],
        "runtime_evidence": [file_evidence(_runtime_tool(name, args.ffmpeg_bin)) for name in (
            "ffmpeg.exe", "ffprobe.exe", "avcodec-62.dll", "avutil-60.dll")],
        "subprocess_timeout_seconds": args.timeout,
        "stages": {
            "a-array": {"status": "not_run", "role": "comparison_control",
                        "reason": "pending" if args.compare_array else "comparison_not_requested"},
        },
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
        if args.helper:
            executable = args.helper.resolve(strict=True)
            if not executable.is_file():
                raise ValueError("--helper must name a prebuilt executable file")
            results["build"] = {"status": "not_run:prebuilt_helper", "executable": str(executable),
                                "executable_evidence": file_evidence(executable)}
        else:
            build_dir = output / "build"
            build_dir.mkdir(parents=True, exist_ok=True)
            executable = build_harness(
                build_dir, TOOLS / "amd_isolation_smoke.cpp", "amd-isolation-smoke",
                timeout_seconds=max(600.0, args.timeout * 8),
            )
            results["build"] = {"status": "pass", "output": str(build_dir), "executable": str(executable),
                                "executable_evidence": file_evidence(executable)}
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
        results["production_status"] = "AMD_HARDWARE_REQUIRED"
        results["nv12_comparison"] = {"status": "not_run", "reason": "AMD_HARDWARE_REQUIRED"}
        for mode in ("a", "a-array", "b", "c"):
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
    if args.compare_nv12:
        results["nv12_comparison"] = run_format_comparison(executable, output, args)
    if args.compare_array:
        # A legacy driver failure must not prevent the production stages from
        # running, or be mistaken for a production failure.
        results["stages"]["a-array"] = _run_stage(executable, "a-array", output, args)
    ab_pass = all(results["stages"][mode].get("status") == "pass" for mode in ("a", "b"))
    c_transport = results["stages"]["c"].get("status") == "transport_decode_pass_content_pending"
    all_pass = ab_pass and c_transport
    results["save_attempts"] = results["stages"]["c"].get(
        "save_attempts", results["save_attempts"])
    results["status"] = "PARTIAL" if all_pass else "failed"
    results["production_status"] = results["status"]
    results["array_control_status"] = results["stages"]["a-array"]["status"]
    result_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps({"status": results["status"],
                      "production_status": results["production_status"],
                      "array_control_status": results["array_control_status"],
                      "nv12_comparison_status": results["nv12_comparison"]["status"],
                      "result": str(result_path)}, indent=2))
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
