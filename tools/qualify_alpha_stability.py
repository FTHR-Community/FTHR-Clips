#!/usr/bin/env python3
"""Qualify Windows clipping with bounded runs and diagnostic artifacts.

Write outside the repository by default. Stop on a failed health check,
retaining engine logs, samples, and the JSON report.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from qualify_windows_hardware import (  # noqa: E402
    CAPTURE_HEALTH_BACKEND_FAILED,
    CODECS,
    CREATE_NO_WINDOW,
    CaptureBridge,
    build_engine_command as _build_native_engine_command,
    choose_monitor,
    connect_bridge,
    event_resolution,
    machine_inventory,
    process_cpu_seconds,
    process_memory_bytes,
    process_thread_count,
    read_adapter_topology_event,
    verify_no_running_engine,
)
from core.windows_monitor import enumerate_windows_monitors  # noqa: E402


DEFAULT_WARMUP_SECONDS = 70
DEFAULT_SOAK_SECONDS = 15 * 60
DEFAULT_TOTAL_CAPTURE_SECONDS = 30 * 60
DEFAULT_STALL_TIMEOUT_SECONDS = 15.0

# Keep this mapping next to the qualification command rather than duplicating
# the native parser's positional contract at each call site.  argv[17] is
# documented in docs/engine-startup.md and is intentionally still present in
# the command built by qualify_windows_hardware.
ENCODER_PREFERENCE = {
    "auto": 0,
    "nvenc": 1,
    "amf": 2,
    "qsv": 3,
}
ENCODER_EXPECTATIONS: dict[str, dict[str, str | None]] = {
    "auto": {"backend": None, "vendor": None},
    "nvenc": {"backend": "native-nvenc", "vendor": "NVIDIA"},
    "amf": {"backend": "ffmpeg-amf", "vendor": "AMD"},
    "qsv": {"backend": "ffmpeg-qsv", "vendor": "Intel"},
}
CAPTURE_BACKEND_EXPECTATIONS = {
    "auto": None,
    "wgc": "WGC_MONITOR",
    "dxgi": "DXGI_OUTPUT_DUPLICATION",
}
CODEC_PROBE_NAMES = {"h264": "h264", "hevc": "hevc", "av1": "av1"}

SAMPLE_WIDTH = 32
SAMPLE_HEIGHT = 18
SAMPLE_FPS = 5
SAMPLE_FRAMES = 12
MAX_RAW_SAMPLE_BYTES = SAMPLE_WIDTH * SAMPLE_HEIGHT * SAMPLE_FRAMES
BLACK_LUMA_THRESHOLD = 8.0
UNIFORM_PIXEL_RANGE = 2
FROZEN_MEAN_DELTA = 1.0
AV_TIMESTAMP_BOUND_SECONDS = 1.0


def build_engine_command(
    args: argparse.Namespace, codec: str, monitor_path: str,
) -> list[str]:
    """Build the native command and set the documented encoder slot.

    The shared hardware qualification helper owns the complete argv shape.
    This wrapper changes only argv[17], preserving all retired/crop/audio
    slots while allowing this harness to request a concrete backend.
    """
    command = _build_native_engine_command(args, codec, monitor_path)
    encoder = getattr(args, "encoder", "auto")
    try:
        command[17] = str(ENCODER_PREFERENCE[encoder])
    except KeyError as error:
        raise ValueError(f"unsupported encoder choice: {encoder}") from error
    return command


@dataclass(frozen=True)
class PacketPoint:
    pts: float | None
    duration: float | None
    flags: str = ""
    stream_index: int | None = None


@dataclass
class ClipResult:
    name: str
    requested_seconds: int
    save_latency_seconds: float
    file_size_bytes: int
    video_packets: int
    audio_packets: int
    actual_codec: str | None
    width: int | None
    height: int | None
    effective_fps: float
    video_duration_seconds: float | None
    audio_duration_seconds: float | None
    audio_stream_count: int
    audio_stream_durations_seconds: list[float | None]
    maximum_video_gap_seconds: float | None
    first_video_pts_seconds: float | None
    last_video_pts_seconds: float | None
    av_start_delta_seconds: float | None
    av_end_delta_seconds: float | None
    av_timestamps_bounded: bool
    av_stream_deltas: list[dict[str, Any]]
    dimensions_valid: bool
    content_sample_classification: str
    content_sample_valid: bool
    content_sample_frame_count: int
    content_sample_max_frame_delta: float | None
    content_sample_mean_luma: float | None
    full_decode: bool
    video_timeline_valid: bool
    audio_duration_sane: bool
    partial_absent: bool
    publish_count: int
    validation_error: str | None = None


def run_checked(
    command: list[str], *, timeout: float = 120.0, cwd: Path | None = None,
) -> str:
    """Run a tool without shell interpolation and return stdout."""
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=cwd,
        creationflags=CREATE_NO_WINDOW,
    )
    return completed.stdout


def _float_or_none(value: Any) -> float | None:
    if value in (None, "", "N/A", "nan", "NaN"):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def parse_packet_points(payload: dict[str, Any], codec_type: str) -> list[PacketPoint]:
    """Extract packet PTS without sorting or silently filling missing values."""
    points: list[PacketPoint] = []
    for packet in payload.get("packets", []):
        if packet.get("codec_type") != codec_type:
            continue
        points.append(PacketPoint(
            _float_or_none(packet.get("pts_time")),
            _float_or_none(packet.get("duration_time")),
            str(packet.get("flags", "")),
            int(packet["stream_index"])
            if str(packet.get("stream_index", "")).isdigit() else None,
        ))
    return points


def timeline_health(
    packets: Iterable[PacketPoint],
    *,
    requested_seconds: int,
    expected_fps: int,
    max_gap_seconds: float | None = None,
) -> dict[str, Any]:
    """Validate real packet timestamps and report gaps.

    The packet order is retained.  Missing or backwards timestamps fail the
    check instead of being sorted or regenerated, which catches fake CFR
    expansion and sparse replay output.
    """
    points = list(packets)
    if not points:
        return {
            "valid": False, "reason": "no_video_packets", "packet_count": 0,
            "first_pts": None, "last_pts": None, "maximum_gap": None,
            "coverage_seconds": 0.0, "effective_fps": 0.0,
        }
    if any(point.pts is None for point in points):
        return {
            "valid": False, "reason": "video_packet_missing_pts",
            "packet_count": len(points), "first_pts": None, "last_pts": None,
            "maximum_gap": None, "coverage_seconds": 0.0,
            "effective_fps": 0.0,
        }
    timestamps = [point.pts for point in points]
    assert all(value is not None for value in timestamps)
    deltas = [
        right - left
        for left, right in zip(timestamps, timestamps[1:], strict=False)
    ]
    if any(delta <= 0 for delta in deltas):
        return {
            "valid": False, "reason": "video_pts_not_strictly_increasing",
            "packet_count": len(points), "first_pts": timestamps[0],
            "last_pts": timestamps[-1],
            "maximum_gap": max(deltas, default=0.0),
            "coverage_seconds": max(0.0, timestamps[-1] - timestamps[0]),
            "effective_fps": 0.0,
        }
    maximum_gap = max(deltas, default=0.0)
    coverage = max(0.0, timestamps[-1] - timestamps[0])
    effective_fps = ((len(points) - 1) / coverage) if coverage > 0 else 0.0
    # A short save can be partial at the start of a running ring, but it must
    # contain a real sequence.  Permit one frame less than the requested
    # interval and reject long frozen intervals in every clip.
    min_coverage = max(0.5, requested_seconds - max(1.0, 2.0 / max(expected_fps, 1)))
    permitted_gap = max_gap_seconds if max_gap_seconds is not None else max(
        0.25, 4.0 / max(expected_fps, 1))
    valid = coverage >= min_coverage and maximum_gap <= permitted_gap
    reason = None
    if coverage < min_coverage:
        reason = "video_coverage_short"
    elif maximum_gap > permitted_gap:
        reason = "video_gap_too_large"
    elif len(points) > 4 and effective_fps < max(1.0, expected_fps * 0.5):
        valid = False
        reason = "video_effective_rate_too_low"
    elif not effective_fps_suitable(effective_fps, expected_fps):
        valid = False
        reason = "video_effective_rate_mismatch"
    return {
        "valid": valid,
        "reason": reason,
        "packet_count": len(points),
        "first_pts": timestamps[0],
        "last_pts": timestamps[-1],
        "maximum_gap": maximum_gap,
        "coverage_seconds": coverage,
        "effective_fps": effective_fps,
    }


def effective_fps_suitable(effective_fps: float, expected_fps: int) -> bool:
    """Return whether packet cadence is plausibly the requested high FPS.

    Packet cadence is measured over a saved clip and is not expected to be
    exactly nominal (encoder delay and the ring boundary affect the first and
    last packet).  A 15% tolerance, with a minimum of 3 FPS, catches a 60 FPS
    source accidentally encoded at 30 FPS while allowing ordinary jitter at
    60/120/144 FPS.
    """
    if effective_fps <= 0 or expected_fps <= 0:
        return False
    tolerance = max(3.0, expected_fps * 0.15)
    return expected_fps - tolerance <= effective_fps <= expected_fps + tolerance


def _timestamp_bounds(packets: Iterable[PacketPoint]) -> tuple[float, float] | None:
    timed = [packet for packet in packets if packet.pts is not None]
    if not timed:
        return None
    starts = [packet.pts for packet in timed]
    assert all(value is not None for value in starts)
    end = max(
        packet.pts + (packet.duration or 0.0)
        for packet in timed
        if packet.pts is not None
    )
    return min(starts), end


def av_timestamp_deltas(
    video_packets: Iterable[PacketPoint],
    audio_packets: Iterable[PacketPoint],
    *,
    max_delta_seconds: float = AV_TIMESTAMP_BOUND_SECONDS,
) -> dict[str, Any]:
    """Report signed A/V start/end deltas from packet timestamps.

    The aggregate audio bounds cover all audio streams.  No timestamp is
    sorted, synthesized, or filled in; missing bounds are explicitly reported
    as unavailable and cannot accidentally pass a required-audio check.
    """
    video_bounds = _timestamp_bounds(video_packets)
    audio_bounds = _timestamp_bounds(audio_packets)
    if video_bounds is None or audio_bounds is None:
        return {
            "start_delta": None,
            "end_delta": None,
            "bounded": audio_bounds is None,
            "video_start": video_bounds[0] if video_bounds else None,
            "video_end": video_bounds[1] if video_bounds else None,
            "audio_start": audio_bounds[0] if audio_bounds else None,
            "audio_end": audio_bounds[1] if audio_bounds else None,
        }
    start_delta = audio_bounds[0] - video_bounds[0]
    end_delta = audio_bounds[1] - video_bounds[1]
    return {
        "start_delta": start_delta,
        "end_delta": end_delta,
        "bounded": (
            abs(start_delta) <= max_delta_seconds
            and abs(end_delta) <= max_delta_seconds
        ),
        "video_start": video_bounds[0],
        "video_end": video_bounds[1],
        "audio_start": audio_bounds[0],
        "audio_end": audio_bounds[1],
    }


def av_timestamp_deltas_per_stream(
    video_packets: Iterable[PacketPoint],
    audio_streams: Iterable[tuple[int | None, Iterable[PacketPoint]]],
    *,
    max_delta_seconds: float = AV_TIMESTAMP_BOUND_SECONDS,
) -> list[dict[str, Any]]:
    """Return bounded signed A/V deltas for every audio stream.

    ``av_timestamp_deltas`` remains useful for an aggregate summary, but the
    qualification decision uses this per-stream result so a bad microphone or
    system-audio track cannot be hidden by a healthy companion stream.
    """
    video_bounds = _timestamp_bounds(video_packets)
    results: list[dict[str, Any]] = []
    for stream_index, packets in audio_streams:
        audio_bounds = _timestamp_bounds(packets)
        item: dict[str, Any] = {
            "stream_index": stream_index,
            "start_delta_seconds": None,
            "end_delta_seconds": None,
            "bounded": False,
        }
        if video_bounds is not None and audio_bounds is not None:
            start_delta = audio_bounds[0] - video_bounds[0]
            end_delta = audio_bounds[1] - video_bounds[1]
            item.update({
                "start_delta_seconds": start_delta,
                "end_delta_seconds": end_delta,
                "bounded": (
                    abs(start_delta) <= max_delta_seconds
                    and abs(end_delta) <= max_delta_seconds
                ),
            })
        results.append(item)
    return results


def classify_raw_sample(
    raw: bytes,
    *,
    width: int = SAMPLE_WIDTH,
    height: int = SAMPLE_HEIGHT,
    max_frames: int = SAMPLE_FRAMES,
    min_frames: int = 3,
) -> dict[str, Any]:
    """Classify a bounded grayscale FFmpeg sample without external packages.

    The classifier deliberately consumes at most ``max_frames`` tiny frames.
    It marks black/uniform/frozen output as invalid and only accepts a sample
    with measurable inter-frame change as ``moving``.  It is kept independent
    of FFmpeg so deterministic unit tests need no media binaries.
    """
    frame_size = width * height
    base = {
        "classification": "unavailable",
        "valid": False,
        "moving": False,
        "black": False,
        "uniform": False,
        "frozen": False,
        "frame_count": 0,
        "max_frame_delta": None,
        "mean_frame_delta": None,
        "mean_luma": None,
        "luma_range": None,
        "reason": None,
    }
    if width <= 0 or height <= 0 or max_frames <= 0:
        base["reason"] = "invalid_sample_bounds"
        return base
    if len(raw) > frame_size * max_frames:
        base["reason"] = "sample_too_large"
        return base
    if len(raw) == 0:
        base["reason"] = "sample_empty"
        return base
    if len(raw) % frame_size:
        base["reason"] = "sample_truncated"
        return base
    frame_count = len(raw) // frame_size
    base["frame_count"] = frame_count
    if frame_count < min_frames:
        base["reason"] = "sample_too_short"
        return base

    frames = [
        raw[offset:offset + frame_size]
        for offset in range(0, len(raw), frame_size)
    ]
    means = [sum(frame) / frame_size for frame in frames]
    ranges = [max(frame) - min(frame) for frame in frames]
    frame_deltas = [
        sum(abs(left - right) for left, right in zip(previous, current, strict=True))
        / frame_size
        for previous, current in zip(frames, frames[1:], strict=False)
    ]
    mean_luma = sum(means) / frame_count
    max_delta = max(frame_deltas, default=0.0)
    mean_delta = sum(frame_deltas) / len(frame_deltas) if frame_deltas else 0.0
    luma_range = max(means) - min(means)
    base.update({
        "mean_luma": mean_luma,
        "luma_range": luma_range,
        "max_frame_delta": max_delta,
        "mean_frame_delta": mean_delta,
    })
    is_black = max(means) <= BLACK_LUMA_THRESHOLD
    is_uniform = (
        max(ranges, default=0) <= UNIFORM_PIXEL_RANGE
        and luma_range <= UNIFORM_PIXEL_RANGE
    )
    is_frozen = max_delta <= FROZEN_MEAN_DELTA
    base.update({"black": is_black, "uniform": is_uniform, "frozen": is_frozen})
    if is_black:
        base["classification"] = "black"
        base["reason"] = "essentially_black"
    elif is_uniform:
        base["classification"] = "uniform"
        base["reason"] = "essentially_uniform"
    elif is_frozen:
        base["classification"] = "frozen"
        base["reason"] = "no_decoded_picture_change"
    else:
        base["classification"] = "moving"
        base["moving"] = True
        base["valid"] = True
    return base


# Descriptive alias for callers that do not know the raw FFmpeg output name.
classify_decoded_sample = classify_raw_sample


def _decode_content_sample(
    ffmpeg: Path,
    path: Path,
    *,
    timeout: float,
) -> dict[str, Any]:
    """Decode only a bounded, tiny grayscale sample from a clip."""
    command = [
        str(ffmpeg), "-v", "error", "-xerror", "-i", str(path),
        "-vf", f"fps={SAMPLE_FPS},scale={SAMPLE_WIDTH}:{SAMPLE_HEIGHT}:flags=area,format=gray",
        "-frames:v", str(SAMPLE_FRAMES), "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, OSError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired) as error:
        return {
            "classification": "unavailable",
            "valid": False,
            "moving": False,
            "black": False,
            "uniform": False,
            "frozen": False,
            "frame_count": 0,
            "max_frame_delta": None,
            "mean_frame_delta": None,
            "mean_luma": None,
            "luma_range": None,
            "reason": f"sample_decode_failed:{type(error).__name__}",
        }
    return classify_raw_sample(completed.stdout)


def _active_codec_backend(codec: str) -> tuple[str | None, str | None]:
    normalized = codec.strip().lower()
    if "nvenc" in normalized:
        return "native-nvenc", "NVIDIA"
    if "_amf" in normalized or normalized.endswith("amf"):
        return "ffmpeg-amf", "AMD"
    if "_qsv" in normalized or normalized.endswith("qsv"):
        return "ffmpeg-qsv", "Intel"
    return None, None


def _luid_identity(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, dict):
        return None
    high = value.get("high_part")
    low = value.get("low_part")
    if isinstance(high, bool) or isinstance(low, bool):
        return None
    try:
        return int(high), int(low)
    except (TypeError, ValueError):
        return None


def verify_encoder_evidence(
    requested_encoder: str,
    active_codec: str | None,
    topology_event: dict[str, Any] | None,
    *,
    requested_width: int = 0,
    requested_height: int = 0,
) -> dict[str, Any]:
    """Verify backend/vendor and same-adapter identity from native evidence.

    An explicit backend is never inferred from a codec suffix alone: the
    native topology event must prove the backend, vendor, and adapter LUID
    identities.  Monitor and capture must always share an adapter.  AMF/QSV
    must also share the encoder adapter; an explicitly requested NVENC path
    may prove a supported cross-adapter route and is reported as such.
    """
    expected = ENCODER_EXPECTATIONS.get(requested_encoder)
    if expected is None:
        return {"valid": False, "reason": "unknown_requested_encoder"}
    event = topology_event if isinstance(topology_event, dict) else {}
    codec_backend, codec_vendor = _active_codec_backend(active_codec or "")
    actual_backend = event.get("encoder_backend")
    actual_backend = actual_backend if isinstance(actual_backend, str) else None
    actual_vendor = event.get("encoder_adapter", event.get("encoder_vendor"))
    actual_vendor = actual_vendor if isinstance(actual_vendor, str) else None
    monitor_luid = _luid_identity(event.get("monitor_adapter_luid"))
    capture_luid = _luid_identity(event.get("capture_device_luid"))
    encoder_luid = _luid_identity(event.get("encoder_adapter_luid"))
    monitor_capture_same_adapter = (
        monitor_luid is not None
        and capture_luid is not None
        and monitor_luid == capture_luid
    )
    encoder_same_adapter = (
        capture_luid is not None
        and encoder_luid is not None
        and capture_luid == encoder_luid
    )
    cross_adapter = (
        monitor_capture_same_adapter
        and encoder_luid is not None
        and capture_luid is not None
        and encoder_luid != capture_luid
    )
    cross_adapter_allowed = (
        requested_encoder == "nvenc"
        and actual_backend == "native-nvenc"
        and cross_adapter
    )
    adapter_identity_valid = monitor_capture_same_adapter and (
        encoder_same_adapter or cross_adapter_allowed
    )
    same_adapter = monitor_capture_same_adapter and encoder_same_adapter
    topology_resolution = event_resolution(event, "encoder")
    recognized_hardware = (
        actual_backend in {"native-nvenc", "ffmpeg-amf", "ffmpeg-qsv"}
        and actual_vendor in {"NVIDIA", "AMD", "Intel"}
        and codec_backend is not None
        and codec_vendor is not None
    )
    reasons: list[str] = []
    if topology_event is None:
        reasons.append("adapter_topology_event_missing")
    if not active_codec:
        reasons.append("active_codec_missing")
    if not recognized_hardware:
        reasons.append("hardware_backend_vendor_unproven")
    if recognized_hardware and (
        actual_backend != codec_backend or actual_vendor != codec_vendor
    ):
        reasons.append("active_codec_backend_vendor_mismatch")
    if not adapter_identity_valid:
        reasons.append("same_adapter_identity_unproven")
    if expected["backend"] is not None and actual_backend != expected["backend"]:
        reasons.append("requested_backend_mismatch")
    if expected["vendor"] is not None and actual_vendor != expected["vendor"]:
        reasons.append("requested_vendor_mismatch")
    if topology_resolution is None:
        reasons.append("encoder_resolution_unproven")
    elif (
        (requested_width > 0 and topology_resolution[0] != requested_width)
        or (requested_height > 0 and topology_resolution[1] != requested_height)
    ):
        reasons.append("encoder_resolution_mismatch")
    return {
        "valid": not reasons,
        "requested_encoder": requested_encoder,
        "expected_backend": expected["backend"] or "auto",
        "expected_vendor": expected["vendor"] or "auto",
        "active_codec": active_codec,
        "actual_backend": actual_backend,
        "actual_vendor": actual_vendor,
        "codec_backend": codec_backend,
        "codec_vendor": codec_vendor,
        "monitor_adapter_luid": event.get("monitor_adapter_luid"),
        "capture_device_luid": event.get("capture_device_luid"),
        "encoder_adapter_luid": event.get("encoder_adapter_luid"),
        "same_adapter": same_adapter,
        "monitor_capture_same_adapter": monitor_capture_same_adapter,
        "encoder_same_adapter": encoder_same_adapter,
        "cross_adapter": cross_adapter,
        "cross_adapter_allowed": cross_adapter_allowed,
        "adapter_identity_valid": adapter_identity_valid,
        "encoder_resolution": topology_resolution,
        "reasons": reasons,
        "topology_event": event or None,
    }


def verify_capture_backend(
    expected_backend: str,
    topology_event: dict[str, Any] | None,
) -> dict[str, Any]:
    """Require native topology evidence for the requested capture backend.

    The engine may legitimately fall back from WGC to DXGI, but a physical
    qualification must not silently treat that fallback as the requested
    scenario.  ``auto`` therefore still requires a known backend in the
    topology event; callers that need a specific path use ``wgc`` or
    ``dxgi``.
    """
    expected = CAPTURE_BACKEND_EXPECTATIONS.get(expected_backend)
    if expected_backend not in CAPTURE_BACKEND_EXPECTATIONS:
        return {
            "valid": False,
            "expected_backend": expected_backend,
            "actual_backend": None,
            "reasons": ["unknown_expected_capture_backend"],
        }
    event = topology_event if isinstance(topology_event, dict) else {}
    actual = event.get("capture_backend")
    actual = actual if isinstance(actual, str) else None
    reasons: list[str] = []
    if topology_event is None:
        reasons.append("adapter_topology_event_missing")
    elif actual not in set(CAPTURE_BACKEND_EXPECTATIONS.values()) - {None}:
        reasons.append("capture_backend_unproven")
    elif expected is not None and actual != expected:
        reasons.append("requested_capture_backend_mismatch")
    return {
        "valid": not reasons,
        "expected_backend": expected or "reported",
        "actual_backend": actual,
        "reasons": reasons,
        "topology_event": event or None,
    }


def _stream_duration(stream: dict[str, Any]) -> float | None:
    duration = _float_or_none(stream.get("duration"))
    if duration is not None:
        return duration
    tags = stream.get("tags") or {}
    return _float_or_none(tags.get("DURATION"))


def _packet_stream_duration(packets: list[PacketPoint]) -> float | None:
    timed_packets = [packet for packet in packets if packet.pts is not None]
    if not timed_packets:
        return None
    start = timed_packets[0].pts
    assert start is not None
    end = max(
        packet.pts + (packet.duration or 0.0)
        for packet in timed_packets
        if packet.pts is not None
    )
    return max(0.0, end - start)


def _duration_sane(
    video_duration: float | None,
    audio_duration: float | None,
    requested_seconds: int,
) -> bool:
    if video_duration is None:
        return False
    if audio_duration is None:
        return True
    # AAC priming is normally well below a quarter second.  Cap the allowance
    # at one second so a multi-second missing track cannot pass a long clip.
    tolerance = max(0.25, min(1.0, requested_seconds * 0.02))
    return abs(video_duration - audio_duration) <= tolerance


def strict_probe_clip(
    ffprobe: Path,
    ffmpeg: Path,
    path: Path,
    *,
    name: str,
    requested_seconds: int,
    save_latency: float,
    expected_fps: int,
    expected_codec: str | None = None,
    expected_width: int = 0,
    expected_height: int = 0,
    require_audio: bool = True,
    publish_count: int = 1,
) -> ClipResult:
    """Probe and fully decode a clip, failing on FFmpeg errors."""
    probe = json.loads(run_checked([
        str(ffprobe), "-v", "error", "-show_format", "-show_streams",
        "-of", "json", str(path),
    ]))
    packet_payload = json.loads(run_checked([
        str(ffprobe), "-v", "error", "-show_packets",
        "-show_entries",
        "packet=codec_type,stream_index,pts_time,duration_time,flags", "-of", "json",
        str(path),
    ]))
    streams = probe.get("streams", [])
    video_stream = next(
        (stream for stream in streams if stream.get("codec_type") == "video"),
        None,
    )
    if video_stream is None:
        raise RuntimeError(f"{name}: no video stream")
    audio_streams = [
        stream for stream in streams if stream.get("codec_type") == "audio"
    ]
    video_packets = parse_packet_points(packet_payload, "video")
    audio_packets = parse_packet_points(packet_payload, "audio")
    health = timeline_health(
        video_packets,
        requested_seconds=requested_seconds,
        expected_fps=expected_fps,
    )
    video_duration = _stream_duration(video_stream)
    audio_durations: list[float | None] = []
    audio_stream_packet_groups: list[tuple[int | None, list[PacketPoint]]] = []
    for stream in audio_streams:
        stream_index = stream.get("index")
        normalized_stream_index = (
            int(stream_index)
            if isinstance(stream_index, int)
            or (isinstance(stream_index, str) and stream_index.isdigit())
            else None
        )
        stream_packets = [
            packet for packet in audio_packets
            if packet.stream_index == normalized_stream_index
        ]
        audio_stream_packet_groups.append((normalized_stream_index, stream_packets))
        audio_durations.append(
            _stream_duration(stream) or _packet_stream_duration(stream_packets))
    audio_duration = max(
        (duration or 0.0 for duration in audio_durations), default=0.0,
    ) or None
    audio_sane = (
        bool(audio_streams) if require_audio else True
    ) and all(
        duration is not None
        and _duration_sane(video_duration, duration, requested_seconds)
        for duration in audio_durations
    )
    actual_codec = video_stream.get("codec_name")
    actual_codec = actual_codec if isinstance(actual_codec, str) else None
    expected_probe_codec = CODEC_PROBE_NAMES.get(expected_codec or "")
    codec_valid = (
        expected_codec is None
        or (
            expected_probe_codec is not None
            and actual_codec is not None
            and actual_codec.lower() == expected_probe_codec
        )
    )
    width = video_stream.get("width")
    height = video_stream.get("height")
    width = width if isinstance(width, int) and not isinstance(width, bool) else None
    height = height if isinstance(height, int) and not isinstance(height, bool) else None
    dimensions_valid = (
        (expected_width <= 0 or width == expected_width)
        and (expected_height <= 0 or height == expected_height)
    )
    av_deltas = av_timestamp_deltas(video_packets, audio_packets)
    av_stream_deltas = av_timestamp_deltas_per_stream(
        video_packets, audio_stream_packet_groups)
    av_timestamps_bounded = (
        all(item["bounded"] for item in av_stream_deltas)
        if audio_streams else bool(av_deltas["bounded"])
    )
    partial_absent = not Path(f"{path}.partial").exists()
    decode_error: str | None = None
    try:
        # -xerror makes a decode warning fatal; -err_detect explode asks the
        # demuxer/decoder to reject recoverable corruption as well.
        run_checked([
            str(ffmpeg), "-v", "error", "-xerror", "-err_detect", "explode",
            "-i", str(path), "-map", "0", "-f", "null", "NUL",
        ], timeout=max(300.0, requested_seconds * 20.0))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        decode_error = str(error)
    content_sample = _decode_content_sample(
        ffmpeg, path, timeout=max(60.0, min(300.0, requested_seconds * 5.0)))
    content_sample_valid = bool(content_sample.get("valid"))
    valid = bool(
        health["valid"]
        and audio_sane
        and codec_valid
        and av_timestamps_bounded
        and dimensions_valid
        and partial_absent
        and not decode_error
        and content_sample_valid
    )
    validation_error = decode_error
    if validation_error is None and not health["valid"]:
        validation_error = str(health["reason"])
    if validation_error is None and not audio_sane:
        validation_error = "audio_duration_mismatch"
    if validation_error is None and not codec_valid:
        validation_error = "encoded_codec_mismatch"
    if validation_error is None and not av_timestamps_bounded and audio_packets:
        validation_error = "av_timestamp_delta_too_large"
    if validation_error is None and not dimensions_valid:
        validation_error = "encoded_dimensions_mismatch"
    if validation_error is None and not partial_absent:
        validation_error = "partial_file_present"
    if validation_error is None and not content_sample_valid:
        validation_error = str(
            content_sample.get("reason") or "decoded_content_not_moving")
    return ClipResult(
        name=name,
        requested_seconds=requested_seconds,
        save_latency_seconds=round(save_latency, 3),
        file_size_bytes=path.stat().st_size,
        video_packets=len(video_packets),
        audio_packets=len(audio_packets),
        actual_codec=actual_codec,
        width=width,
        height=height,
        effective_fps=float(health["effective_fps"]),
        video_duration_seconds=video_duration,
        audio_duration_seconds=audio_duration,
        audio_stream_count=len(audio_streams),
        audio_stream_durations_seconds=audio_durations,
        maximum_video_gap_seconds=health["maximum_gap"],
        first_video_pts_seconds=health["first_pts"],
        last_video_pts_seconds=health["last_pts"],
        av_start_delta_seconds=av_deltas["start_delta"],
        av_end_delta_seconds=av_deltas["end_delta"],
        av_timestamps_bounded=av_timestamps_bounded,
        av_stream_deltas=av_stream_deltas,
        dimensions_valid=dimensions_valid,
        content_sample_classification=str(content_sample["classification"]),
        content_sample_valid=content_sample_valid,
        content_sample_frame_count=int(content_sample["frame_count"]),
        content_sample_max_frame_delta=content_sample["max_frame_delta"],
        content_sample_mean_luma=content_sample["mean_luma"],
        full_decode=decode_error is None,
        video_timeline_valid=bool(health["valid"]),
        audio_duration_sane=audio_sane,
        partial_absent=partial_absent,
        publish_count=publish_count,
        validation_error=validation_error if not valid else None,
    )


def process_handle_count(process: subprocess.Popen) -> int | None:
    """Return handles owned by the engine process when Windows exposes it."""
    if sys.platform != "win32":
        return None
    getter = getattr(ctypes.windll.kernel32, "GetProcessHandleCount", None)
    if getter is None:
        return None
    count = wintypes.DWORD()
    if not getter(wintypes.HANDLE(process._handle), ctypes.byref(count)):
        return None
    return int(count.value)


def process_snapshot(process: subprocess.Popen, now: float | None = None) -> dict[str, Any]:
    working_set, private_bytes = process_memory_bytes(process)
    return {
        "captured_monotonic": now if now is not None else time.monotonic(),
        "working_set_bytes": working_set,
        "private_bytes": private_bytes,
        "process_cpu_seconds": process_cpu_seconds(process),
        "process_thread_count": process_thread_count(process),
        "process_handle_count": process_handle_count(process),
    }


class QualificationSampler:
    """Bounded one-sample-per-interval process/engine health recorder."""

    def __init__(
        self,
        bridge: CaptureBridge,
        process: subprocess.Popen,
        path: Path,
        started_at: float | None = None,
    ):
        self.bridge = bridge
        self.process = process
        self.path = path
        self.started_at = started_at if started_at is not None else time.monotonic()
        self.samples: list[dict[str, Any]] = []
        self._last_progress_at = time.monotonic()
        self._last_frame_count: int | None = None

    def sample(self) -> dict[str, Any]:
        now = time.monotonic()
        status = self.bridge.get_status()
        item = {
            "captured_utc": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(now - self.started_at, 3),
            "status": status,
            **process_snapshot(self.process, now),
        }
        if self.samples:
            previous = self.samples[-1]
            wall_delta = now - float(previous["captured_monotonic"])
            cpu_now = item.get("process_cpu_seconds")
            cpu_before = previous.get("process_cpu_seconds")
            item["process_cpu_percent"] = (
                round(max(0.0, float(cpu_now) - float(cpu_before))
                / wall_delta * 100.0, 3)
                if wall_delta > 0 and cpu_now is not None and cpu_before is not None
                else None
            )
        else:
            item["process_cpu_percent"] = None
        frames = status.get("frames_captured")
        if (
            isinstance(frames, int)
            and (
                self._last_frame_count is None
                or frames > self._last_frame_count
            )
        ):
            self._last_progress_at = now
        self._last_frame_count = frames if isinstance(frames, int) else self._last_frame_count
        item["seconds_since_frame_progress"] = round(now - self._last_progress_at, 3)
        self.samples.append(item)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(item, separators=(",", ":")) + "\n")
        return item

    def check(
        self,
        *,
        max_stall_seconds: float,
        require_frame_progress: bool = True,
    ) -> dict[str, Any]:
        item = self.sample()
        if self.process.poll() is not None:
            raise RuntimeError(f"engine exited with code {self.process.returncode}")
        status = item["status"]
        if not status.get("connected", False):
            raise RuntimeError("capture bridge disconnected")
        flags = int(status.get("capture_health_flags", 0))
        if flags & CAPTURE_HEALTH_BACKEND_FAILED:
            raise RuntimeError(f"capture health failed with flags {flags:#x}")
        if require_frame_progress and item["seconds_since_frame_progress"] > max_stall_seconds:
            raise TimeoutError(
                "capture produced no fresh frames for "
                f"{item['seconds_since_frame_progress']:.1f}s")
        return item


def wait_for_frames(
    bridge: CaptureBridge,
    process: subprocess.Popen,
    minimum_frames: int,
    timeout: float,
    *,
    sampler: QualificationSampler | None = None,
    interval: float = 1.0,
    max_stall_seconds: float = DEFAULT_STALL_TIMEOUT_SECONDS,
    activity_check: Callable[[], None] | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if activity_check is not None:
            activity_check()
        if sampler is None:
            if process.poll() is not None:
                raise RuntimeError(f"engine exited with code {process.returncode}")
            status = bridge.get_status()
            if status.get("capture_health_flags", 0) & CAPTURE_HEALTH_BACKEND_FAILED:
                raise RuntimeError(
                    f"capture health failed with flags {status['capture_health_flags']:#x}")
        else:
            status = sampler.check(
                max_stall_seconds=max_stall_seconds,
                require_frame_progress=True,
            )["status"]
        if int(status.get("frames_captured", 0)) >= minimum_frames:
            return
        time.sleep(min(interval, max(0.01, deadline - time.monotonic())))
    raise TimeoutError(f"capture did not reach {minimum_frames} frames")


def _drain_save_responses(bridge: CaptureBridge) -> None:
    while bridge.peek_save_response() is not None:
        bridge.consume_save_response()


def save_and_wait(
    bridge: CaptureBridge,
    path: Path,
    requested_seconds: int,
    *,
    timeout: float = 90.0,
    progress_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Submit one save and require exactly one terminal saved response.

    File probing is intentionally separate.  During a long strict FFmpeg
    decode the engine must continue to be sampled for stalls.
    """
    _drain_save_responses(bridge)
    started = time.monotonic()
    if not bridge.save_clip(str(path), requested_seconds):
        raise RuntimeError(f"could not submit save for {path.name}")
    terminal_responses = 0
    deadline = started + timeout
    next_progress_check = started
    while time.monotonic() < deadline:
        if progress_check is not None and time.monotonic() >= next_progress_check:
            progress_check()
            next_progress_check = time.monotonic() + 1.0
        response = bridge.peek_save_response()
        if response is None:
            time.sleep(0.05)
            continue
        kind, detail = response
        bridge.consume_save_response()
        if kind == "started":
            continue
        terminal_responses += 1
        if terminal_responses != 1:
            raise RuntimeError(f"save published more than once for {path.name}")
        if kind == "error":
            raise RuntimeError(detail or f"engine rejected {path.name}")
        if kind == "saved":
            if not path.is_file():
                raise RuntimeError(f"engine reported saved but {path} is missing")
            # The shared-memory response is single-slot.  Give a second
            # terminal response a short chance to surface before validating
            # the file, so a duplicate publication cannot be mistaken for a
            # successful exactly-once save.
            settle_deadline = time.monotonic() + 0.25
            while time.monotonic() < settle_deadline:
                duplicate = bridge.peek_save_response()
                if duplicate is not None:
                    duplicate_kind, duplicate_detail = duplicate
                    bridge.consume_save_response()
                    if duplicate_kind != "started":
                        terminal_responses += 1
                        raise RuntimeError(
                            f"save published {terminal_responses} times for {path.name}: "
                            f"{duplicate_detail or duplicate_kind}")
                time.sleep(0.01)
            return {
                "path": path,
                "requested_seconds": requested_seconds,
                "save_latency_seconds": time.monotonic() - started,
                "publish_count": terminal_responses,
            }
    raise TimeoutError(f"save did not complete for {path.name}")


def save_and_probe(
    bridge: CaptureBridge,
    ffprobe: Path,
    ffmpeg: Path,
    path: Path,
    requested_seconds: int,
    *,
    expected_fps: int,
    expected_codec: str | None = None,
    expected_width: int = 0,
    expected_height: int = 0,
    require_audio: bool = True,
    timeout: float = 90.0,
) -> ClipResult:
    """Submit, wait, and strictly validate one save outside a soak."""
    saved = save_and_wait(
        bridge, path, requested_seconds, timeout=timeout)
    result = strict_probe_clip(
        ffprobe, ffmpeg, path, name=path.stem,
        requested_seconds=requested_seconds,
        save_latency=float(saved["save_latency_seconds"]),
        expected_fps=expected_fps,
        expected_codec=expected_codec,
        expected_width=expected_width,
        expected_height=expected_height,
        require_audio=require_audio,
        publish_count=int(saved["publish_count"]),
    )
    if result.publish_count != 1 or result.validation_error:
        raise RuntimeError(
            f"{path.name} failed clip validation: {result.validation_error}")
    return result


def _default_output() -> Path:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(base) / f"FTHR-alpha-stability-qualification-{stamp}"


def start_moving_source(
    ffplay: Path,
    log_path: Path,
    *,
    fps: int = 60,
) -> subprocess.Popen:
    """Start a deterministic visible test pattern for the selected monitor."""
    if fps <= 0:
        raise ValueError("moving source FPS must be positive")
    if not ffplay.is_file():
        raise FileNotFoundError(
            f"moving source requires ffplay.exe; expected {ffplay}")
    command = [
        str(ffplay), "-hide_banner", "-loglevel", "warning", "-alwaysontop",
        "-window_title", "FTHR alpha qualification moving source",
        "-left", "40", "-top", "40",
        "-f", "lavfi", "-i",
        f"testsrc2=size=1280x720:rate={fps}", "-t", "3600", "-an",
    ]
    log = log_path.open("w", encoding="utf-8", errors="replace")
    # This window is intentionally visible: the operator must see the moving
    # source on the monitor being qualified.  stdout/stderr remain bounded in
    # the artifact file and the handle is closed during cleanup.
    try:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
    except Exception:
        raise
    finally:
        # Popen duplicates the inherited handle.  Closing the parent copy keeps
        # the artifact writable and avoids a leaked file descriptor.
        log.close()
    return process


def stop_process(process: subprocess.Popen | None, timeout: float = 5.0) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=timeout)


def shutdown_engine(
    bridge: CaptureBridge | None,
    process: subprocess.Popen | None,
) -> dict[str, Any]:
    """Apply the existing bounded shutdown policy and return evidence."""
    result: dict[str, Any] = {
        "shutdown_requested": False,
        "graceful_exit": False,
        "escalated": None,
        "exit_code": None,
    }
    if process is None:
        return result
    if bridge is not None and process.poll() is None:
        result["shutdown_requested"] = bool(bridge.request_engine_shutdown())
        try:
            process.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            pass
    if process.poll() is None:
        result["escalated"] = "terminate"
        process.terminate()
        try:
            process.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            result["escalated"] = "kill"
            process.kill()
            process.wait(timeout=5.0)
    result["graceful_exit"] = result["escalated"] is None and process.poll() is not None
    result["exit_code"] = process.returncode
    return result


def require_process_alive(process: subprocess.Popen | None, label: str) -> None:
    if process is None or process.poll() is not None:
        raise RuntimeError(f"{label} exited during qualification")


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")


def _save_plan(args: argparse.Namespace) -> list[int]:
    return (
        [args.short_duration_seconds] * args.short_saves
        + [30] * args.thirty_saves
        + [60] * args.sixty_saves
    )


def qualify(args: argparse.Namespace) -> dict[str, Any]:
    if args.output.exists() and any(args.output.iterdir()):
        raise FileExistsError(
            f"qualification output already contains files: {args.output}; "
            "choose a fresh --output directory")
    args.output.mkdir(parents=True, exist_ok=True)
    report_path = args.output / "qualification-report.json"
    sample_path = args.output / "engine-samples.jsonl"
    engine_log_path = args.output / "engine.log"
    ffplay = args.ffplay or args.ffmpeg.with_name("ffplay.exe")
    report: dict[str, Any] = {
        "schema": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "machine": machine_inventory(),
        "policy": {
            "moving_source": "ffplay testsrc2 visible window",
            "strict_decode": "ffmpeg -xerror -err_detect explode",
            "failure_preserves_evidence": True,
            "output_directory": str(args.output),
        },
        "requested": {
            "codec": args.codec, "fps": args.fps, "warmup_seconds": args.warmup_seconds,
            "soak_seconds": args.soak_seconds,
            "total_capture_seconds": args.total_capture_seconds,
            "width": args.width, "height": args.height,
            "encoder": args.encoder,
            "expected_encoder_backend": (
                ENCODER_EXPECTATIONS[args.encoder]["backend"] or "auto"),
            "expected_encoder_vendor": (
                ENCODER_EXPECTATIONS[args.encoder]["vendor"] or "auto"),
            "expected_capture_backend": args.expected_capture_backend,
            "short_duration_seconds": args.short_duration_seconds,
            "short_saves": args.short_saves, "thirty_saves": args.thirty_saves,
            "sixty_saves": args.sixty_saves,
        },
        "samples": str(sample_path), "engine_log": str(engine_log_path),
        "clips": [], "save_attempts": [], "save_failures": [],
        "save_publish_counts": {}, "status": "RUNNING",
    }
    _write_report(report_path, report)
    process: subprocess.Popen | None = None
    bridge: CaptureBridge | None = None
    moving_source: subprocess.Popen | None = None
    engine_log = None
    shutdown_recorded = False
    try:
        verify_no_running_engine()
        monitor_path, monitor_name = choose_monitor(args.monitor)
        selected_monitor = next(
            (item for item in enumerate_windows_monitors()
             if item.device_path == monitor_path),
            None,
        )
        if selected_monitor is not None and not selected_monitor.primary:
            raise RuntimeError(
                "alpha qualification moving source is restricted to the primary "
                "monitor; select the primary monitor or omit --monitor")
        report["monitor"] = {"name": monitor_name, "device_path": monitor_path}
        report["moving_source_monitor_policy"] = "primary monitor only"
        _write_report(report_path, report)
        command = build_engine_command(args, args.codec, monitor_path)
        engine_log = engine_log_path.open("w", encoding="utf-8", errors="replace")
        try:
            process = subprocess.Popen(
                command, stdout=engine_log, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception:
            raise
        finally:
            # The child owns its inherited handle after Popen returns.
            engine_log.close()
            engine_log = None
        bridge = connect_bridge(process)
        active_codec = bridge.get_active_codec()
        report["active_codec_at_connect"] = active_codec
        sampler = QualificationSampler(bridge, process, sample_path, time.monotonic())
        moving_source = start_moving_source(
            ffplay, args.output / "moving-source.log", fps=args.fps)
        started = time.monotonic()
        wait_for_frames(
            bridge, process, max(args.fps * args.warmup_seconds, args.fps * 5),
            max(30.0, args.warmup_seconds + 30.0), sampler=sampler,
            max_stall_seconds=args.stall_timeout_seconds,
            activity_check=lambda: require_process_alive(moving_source, "moving source"),
        )
        active_codec = bridge.get_active_codec()
        report["active_codec"] = active_codec
        if args.codec not in active_codec.lower():
            raise RuntimeError(
                f"requested {args.codec}, engine reports {active_codec or 'empty'}")
        report["warmup_completed_seconds"] = round(time.monotonic() - started, 3)
        _write_report(report_path, report)

        plan = _save_plan(args)
        pending_clips: list[dict[str, Any]] = []
        plan_start = time.monotonic()
        soak_deadline = plan_start + args.soak_seconds
        next_index = 0
        # Spread saves through the moving-source soak.  A save is only started
        # after health sampling, and every result is persisted immediately.
        while time.monotonic() < soak_deadline or next_index < len(plan):
            if moving_source is None or moving_source.poll() is not None:
                raise RuntimeError("moving source exited during soak")
            sampler.check(max_stall_seconds=args.stall_timeout_seconds)
            if next_index < len(plan):
                elapsed = time.monotonic() - plan_start
                target = (next_index + 1) * args.soak_seconds / max(len(plan), 1)
                if elapsed >= target or time.monotonic() + 90.0 >= soak_deadline:
                    duration = plan[next_index]
                    clip_path = args.output / f"clip_{next_index + 1:02d}_{duration}s.mp4"
                    try:
                        saved = save_and_wait(
                            bridge, clip_path, duration,
                            progress_check=lambda: sampler.check(
                                max_stall_seconds=args.stall_timeout_seconds),
                        )
                    except Exception as error:
                        failed_save = {
                            "name": clip_path.name,
                            "requested_seconds": duration,
                            "error": f"{type(error).__name__}: {error}",
                        }
                        report["save_attempts"].append({
                            **failed_save, "status": "failed",
                        })
                        report["save_failures"].append(failed_save)
                        # Keep the original single-failure field for consumers
                        # of schema-1 reports while retaining the full
                        # denominator for a complete qualification run.
                        report.setdefault("failed_save", failed_save)
                        _write_report(report_path, report)
                        next_index += 1
                        if args.continue_on_save_failure:
                            continue
                        raise
                    pending_clips.append(saved)
                    report["save_attempts"].append({
                        "name": clip_path.name,
                        "requested_seconds": duration,
                        "status": "saved",
                        "save_latency_seconds": saved["save_latency_seconds"],
                        "publish_count": saved["publish_count"],
                    })
                    report["save_publish_counts"][clip_path.name] = saved["publish_count"]
                    report["saved_clips"] = [
                        {
                            "name": item["path"].name,
                            "requested_seconds": item["requested_seconds"],
                            "save_latency_seconds": item["save_latency_seconds"],
                            "publish_count": item["publish_count"],
                        }
                        for item in pending_clips
                    ]
                    next_index += 1
                    _write_report(report_path, report)
                    continue
            if time.monotonic() >= soak_deadline and next_index >= len(plan):
                break
            time.sleep(min(1.0, max(0.05, soak_deadline - time.monotonic())))

        # End the moving visual and leave the desktop idle/static for the rest
        # of the requested 30-minute active-capture window.
        stop_process(moving_source)
        moving_source = None
        report["moving_source_stopped_seconds"] = round(
            time.monotonic() - started, 3)
        remaining = max(0.0, args.total_capture_seconds - (time.monotonic() - started))
        idle_deadline = time.monotonic() + remaining
        while time.monotonic() < idle_deadline:
            sampler.check(
                max_stall_seconds=args.stall_timeout_seconds,
                require_frame_progress=False,
            )
            time.sleep(min(1.0, max(0.05, idle_deadline - time.monotonic())))
        final_sample = sampler.sample()
        report["final_capture_sample"] = final_sample
        report["actual_capture_elapsed_seconds"] = round(
            time.monotonic() - started, 3)
        shutdown = shutdown_engine(bridge, process)
        shutdown_recorded = True
        report["shutdown"] = shutdown
        report["engine_stopped_before_validation"] = True
        if not shutdown["graceful_exit"]:
            raise RuntimeError("engine did not exit gracefully before clip validation")
        # The native event is written by the child process.  Read it only after
        # shutdown so the final adapter-topology resolution is complete and no
        # partial line can be mistaken for physical parity evidence.
        shutdown_active_codec = bridge.get_active_codec() if bridge is not None else None
        if shutdown_active_codec:
            active_codec = shutdown_active_codec
        report["active_codec_after_shutdown"] = active_codec
        topology_event = read_adapter_topology_event(engine_log_path)
        capture_evidence = verify_capture_backend(
            args.expected_capture_backend, topology_event)
        report["capture_evidence"] = capture_evidence
        if not capture_evidence["valid"]:
            raise RuntimeError(
                "capture backend evidence failed: "
                + ", ".join(capture_evidence["reasons"])
            )
        encoder_evidence = verify_encoder_evidence(
            args.encoder,
            active_codec,
            topology_event,
            requested_width=args.width,
            requested_height=args.height,
        )
        report["encoder_evidence"] = encoder_evidence
        _write_report(report_path, report)
        if not encoder_evidence["valid"]:
            raise RuntimeError(
                "encoder parity evidence failed: "
                + ", ".join(encoder_evidence["reasons"])
            )
        topology_width, topology_height = encoder_evidence["encoder_resolution"]
        expected_probe_width = args.width or topology_width
        expected_probe_height = args.height or topology_height
        bridge.shutdown()
        bridge = None
        # Probe only after capture has completed so strict FFmpeg work cannot
        # create a blind interval in the health sampler.
        for saved in pending_clips:
            try:
                result = strict_probe_clip(
                    args.ffprobe, args.ffmpeg, saved["path"],
                    name=saved["path"].stem,
                    requested_seconds=saved["requested_seconds"],
                    save_latency=saved["save_latency_seconds"],
                    expected_fps=args.fps,
                    expected_codec=args.codec,
                    expected_width=expected_probe_width,
                    expected_height=expected_probe_height,
                    require_audio=args.audio,
                    publish_count=saved["publish_count"],
                )
            except Exception as error:
                report["failed_probe"] = {
                    "name": saved["path"].name,
                    "requested_seconds": saved["requested_seconds"],
                    "error": f"{type(error).__name__}: {error}",
                }
                _write_report(report_path, report)
                raise
            report["clips"].append(asdict(result))
            _write_report(report_path, report)
            if result.publish_count != 1 or result.validation_error:
                raise RuntimeError(
                    f"{saved['path'].name} failed clip validation: "
                    f"{result.validation_error}")
        if report["save_failures"]:
            raise RuntimeError(
                f"{len(report['save_failures'])} save attempt(s) failed; "
                "see save_attempts and save_failures in the report")
        report["status"] = "PASSED"
        report["completed_utc"] = datetime.now(timezone.utc).isoformat()
        return report
    except Exception as error:
        report["status"] = "FAILED"
        report["failure"] = f"{type(error).__name__}: {error}"
        report["failed_utc"] = datetime.now(timezone.utc).isoformat()
        raise
    finally:
        stop_process(moving_source)
        if not shutdown_recorded:
            shutdown = shutdown_engine(bridge, process)
            report["shutdown"] = shutdown
            shutdown_recorded = True
            if report.get("status") == "PASSED" and not shutdown["graceful_exit"]:
                report["status"] = "FAILED"
                report["failure"] = "engine did not exit gracefully"
        if bridge is not None:
            bridge.shutdown()
        if process is not None and process.poll() is None:
            stop_process(process, timeout=10.0)
        if engine_log is not None:
            engine_log.close()
        _write_report(report_path, report)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="P0 Windows alpha stability qualification; artifacts are external by default")
    parser.add_argument("--codec", choices=list(CODECS), default="h264")
    parser.add_argument(
        "--encoder", choices=list(ENCODER_PREFERENCE), default="auto",
        help="native encoder preference for argv[17] (default: auto)",
    )
    parser.add_argument("--monitor", help="normalized monitor device-interface path")
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--width", type=int, default=0)
    parser.add_argument("--height", type=int, default=0)
    parser.add_argument("--bitrate", type=int, default=16000)
    parser.add_argument("--scaling-mode", choices=["stretch", "fit"], default="stretch")
    parser.add_argument("--warmup-seconds", type=int, default=DEFAULT_WARMUP_SECONDS)
    parser.add_argument("--soak-seconds", type=int, default=DEFAULT_SOAK_SECONDS)
    parser.add_argument("--total-capture-seconds", type=int, default=DEFAULT_TOTAL_CAPTURE_SECONDS)
    parser.add_argument("--stall-timeout-seconds", type=float, default=DEFAULT_STALL_TIMEOUT_SECONDS)
    parser.add_argument("--short-saves", type=int, default=20)
    parser.add_argument(
        "--short-duration-seconds", type=int, default=5,
        help="duration for the short-save bucket (default: 5)",
    )
    parser.add_argument("--thirty-saves", type=int, default=5)
    parser.add_argument("--sixty-saves", type=int, default=3)
    parser.add_argument(
        "--expected-capture-backend", choices=list(CAPTURE_BACKEND_EXPECTATIONS),
        default="auto",
        help="require native WGC or DXGI topology evidence; auto still fails closed",
    )
    parser.add_argument(
        "--continue-on-save-failure", action="store_true",
        help="complete the configured save denominator after a save error",
    )
    parser.add_argument("--audio", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--separate-audio", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--engine", type=Path, default=ROOT / "FTHRcapture/x64/Release/FTHRclips.exe")
    ffmpeg_bin = ROOT / "FTHRcapture/FTHRclips/third_party/ffmpeg/bin"
    parser.add_argument("--ffmpeg", type=Path, default=ffmpeg_bin / "ffmpeg.exe")
    parser.add_argument("--ffprobe", type=Path, default=ffmpeg_bin / "ffprobe.exe")
    parser.add_argument("--ffplay", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=_default_output())
    args = parser.parse_args(argv)
    if any(value < 0 for value in (
        args.warmup_seconds, args.soak_seconds, args.total_capture_seconds,
        args.short_saves, args.short_duration_seconds,
        args.thirty_saves, args.sixty_saves,
    )):
        parser.error("durations and save counts must be non-negative")
    if args.total_capture_seconds < args.warmup_seconds + args.soak_seconds:
        parser.error(
            "--total-capture-seconds must include warmup and soak "
            "(the remaining time is the idle/static phase)")
    if args.stall_timeout_seconds <= 0:
        parser.error("--stall-timeout-seconds must be positive")
    if args.short_duration_seconds <= 0:
        parser.error("--short-duration-seconds must be positive")
    if args.fps <= 0:
        parser.error("--fps must be positive")
    if args.width < 0 or args.height < 0:
        parser.error("--width and --height must be non-negative")
    return args


def main(argv: list[str] | None = None) -> int:
    if sys.platform != "win32":
        raise SystemExit("This qualification harness runs only on Windows.")
    args = parse_args(argv)
    for required in (args.engine, args.ffmpeg, args.ffprobe):
        if not required.is_file():
            raise SystemExit(f"Required executable not found: {required}")
    args.output = args.output.resolve()
    try:
        args.output.relative_to(ROOT)
    except ValueError:
        pass
    else:
        raise SystemExit(
            f"Qualification output must be outside the repository: {args.output}")
    try:
        report = qualify(args)
    except Exception as error:
        print(f"Qualification FAILED: {error}", file=sys.stderr)
        print(f"Evidence: {args.output}", file=sys.stderr)
        return 1
    if report.get("status") != "PASSED":
        print(
            f"Qualification FAILED: {report.get('failure', 'qualification did not pass')}",
            file=sys.stderr,
        )
        print(f"Evidence: {args.output}", file=sys.stderr)
        return 1
    print(f"Qualification passed. Report: {args.output / 'qualification-report.json'}")
    print(json.dumps({
        "status": report.get("status"),
        "clips": len(report.get("clips", [])),
        "output": str(args.output),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
