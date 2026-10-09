"""Read saved-stream FPS and bitrate from FFprobe for the editor.

OpenCV decoder estimates are suitable for seeking, but not presentation
as container metadata. Missing probe values remain explicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections import OrderedDict
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any, Mapping

from core.ffmpeg_tools import FFmpegUnavailable, get_ffprobe_exe


_NO_WINDOW = {'creationflags': 0x08000000} if sys.platform == 'win32' else {}
_MAX_PACKET_PROBE_OUTPUT = 64 * 1024 * 1024


@dataclass(frozen=True)
class VideoMetadata:
    duration_seconds: float | None
    width: int | None
    height: int | None
    average_fps: float | None
    real_fps: float | None
    video_bitrate_bps: int | None
    total_bitrate_bps: int | None
    fps_source: str | None


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        # Malformed metadata is unknown, never a fabricated positive value.
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        # Invalid packet timing leaves physical-timeline evidence inconclusive.
        return None
    return parsed if math.isfinite(parsed) else None


def _positive_int(value: Any) -> int | None:
    parsed = _positive_float(value)
    return int(parsed) if parsed is not None else None


def parse_frame_rate(value: Any) -> float | None:
    """Return a sane positive FPS from an FFprobe rational or decimal."""

    if value in (None, '', 'N/A', '0/0'):
        return None
    try:
        rate = float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError, OverflowError):
        # Invalid rationals are unknown rather than a guessed frame rate.
        return None
    # Anything beyond 1000 FPS is not useful clip metadata and is almost
    # certainly a codec time-base artifact rather than a presentation rate.
    return rate if math.isfinite(rate) and 0 < rate <= 1000 else None


def parse_ffprobe_video_metadata(
        payload: str | Mapping[str, Any], *, file_size: int | None = None,
) -> VideoMetadata | None:
    """Parse the first real video stream from an FFprobe JSON document."""

    try:
        document = json.loads(payload) if isinstance(payload, str) else payload
    except (TypeError, json.JSONDecodeError):
        # Corrupt probe JSON provides no trustworthy media metadata.
        return None
    if not isinstance(document, Mapping):
        return None
    streams = document.get('streams')
    if not isinstance(streams, list):
        return None
    video = next(
        (stream for stream in streams
         if isinstance(stream, Mapping) and stream.get('codec_type') == 'video'),
        None,
    )
    if video is None:
        return None

    stream_tags = video.get('tags')
    if not isinstance(stream_tags, Mapping):
        stream_tags = {}
    format_data = document.get('format')
    if not isinstance(format_data, Mapping):
        format_data = {}
    format_tags = format_data.get('tags')
    if not isinstance(format_tags, Mapping):
        format_tags = {}

    # FTHR writers publish the configured values explicitly. Prefer those
    # values over FFprobe's packet-derived estimates when present; the latter
    # can be skewed by hardware encoder timing or container interleave details.
    configured_fps = (
        parse_frame_rate(format_tags.get('fthr_frame_rate'))
        or parse_frame_rate(stream_tags.get('fthr_frame_rate')))
    average_fps = configured_fps or parse_frame_rate(video.get('avg_frame_rate'))
    real_fps = parse_frame_rate(video.get('r_frame_rate'))
    fps_source = 'fthr_frame_rate' if configured_fps is not None else (
        'avg_frame_rate' if average_fps is not None else (
            'r_frame_rate' if real_fps is not None else None))
    configured_bitrate = (
        _positive_int(format_tags.get('fthr_video_bitrate_bps'))
        or _positive_int(stream_tags.get('fthr_video_bitrate_bps')))
    presented_fps = average_fps if average_fps is not None else real_fps

    duration = _positive_float(video.get('duration'))
    if duration is None:
        duration = _positive_float(format_data.get('duration'))

    total_bitrate = _positive_int(format_data.get('bit_rate'))
    if total_bitrate is None and duration is not None:
        size = _positive_int(format_data.get('size')) or _positive_int(file_size)
        if size is not None:
            # This fallback is explicitly TOTAL container bitrate. It is never
            # reused as video bitrate because FTHR clips may have many AAC stems.
            total_bitrate = int(round(size * 8 / duration))

    return VideoMetadata(
        duration_seconds=duration,
        width=_positive_int(video.get('width')),
        height=_positive_int(video.get('height')),
        average_fps=presented_fps,
        real_fps=real_fps,
        video_bitrate_bps=(configured_bitrate
                           or _positive_int(video.get('bit_rate'))),
        total_bitrate_bps=total_bitrate,
        fps_source=fps_source,
    )


def probe_video_metadata(
        media_path: str | os.PathLike[str], *, timeout_seconds: float = 4.0,
) -> VideoMetadata | None:
    """Probe one file without guessing missing media facts."""

    try:
        probe = get_ffprobe_exe()
        result = subprocess.run(
            [
                probe, '-v', 'error',
                '-show_entries',
                'stream=index,codec_type,width,height,avg_frame_rate,'
                'r_frame_rate,duration,bit_rate:'
                'format_tags=fthr_frame_rate,fthr_video_bitrate_bps:'
                'format=duration,size,bit_rate',
                '-of', 'json', os.fspath(media_path),
            ],
            capture_output=True, text=True, timeout=timeout_seconds,
            check=False, **_NO_WINDOW,
        )
    except (FFmpegUnavailable, OSError, subprocess.SubprocessError):
        # A failed timing probe is intentionally inconclusive; the caller
        # treats it as needing the conservative CFR repair path.
        return None
    if result.returncode != 0:
        return None
    try:
        file_size = Path(media_path).stat().st_size
    except OSError:
        file_size = None
    return parse_ffprobe_video_metadata(result.stdout, file_size=file_size)


def _probe_video_packets(
        media_path: str | os.PathLike[str], *, timeout_seconds: float,
) -> list[Mapping[str, Any]] | None:
    """Read bounded packet timing evidence without decoding media."""

    try:
        probe = get_ffprobe_exe()
        result = subprocess.run(
            [
                probe, '-v', 'error', '-select_streams', 'v:0',
                '-show_entries', 'packet=duration_time,pts_time',
                '-of', 'json', os.fspath(media_path),
            ],
            capture_output=True, text=True, timeout=timeout_seconds,
            check=False, **_NO_WINDOW,
        )
    except (FFmpegUnavailable, OSError, subprocess.SubprocessError):
        # Unavailable packet timing remains inconclusive for the caller policy.
        return None
    if result.returncode != 0:
        return None
    # A normal FTHR replay is bounded to a few hundred seconds.  Refuse an
    # unexpectedly large ffprobe response rather than retaining an arbitrary
    # packet list in the finalization worker.
    stdout = result.stdout
    if isinstance(stdout, (str, bytes)) and len(stdout) > _MAX_PACKET_PROBE_OUTPUT:
        return None
    try:
        packets = json.loads(stdout).get('packets')
    except (TypeError, json.JSONDecodeError, AttributeError):
        # Malformed packet JSON cannot establish a valid source timeline.
        return None
    if not isinstance(packets, list) or not packets:
        return None
    return [packet for packet in packets if isinstance(packet, Mapping)]


def evaluate_physical_video_timeline(
        packets: list[Mapping[str, Any]], expected_fps: float,
) -> bool | None:
    """Check packet PTS gaps against the requested cadence.

    Nominal FPS can conceal missing intervals that a CFR repair would otherwise
    expand into a misleading clip.
    """

    if (not math.isfinite(expected_fps)
            or expected_fps <= 0
            or expected_fps > 1000
            or not packets):
        return None
    pts = []
    for packet in packets:
        # Decoder-preroll packets may legitimately have negative PTS values;
        # only finiteness and ordering are relevant to physical gaps.
        value = _finite_float(packet.get('pts_time'))
        if value is None:
            return None
        pts.append(value)
    if len(pts) < 2:
        # Older FFprobe builds may not expose packet PTS. Duration evidence is
        # still useful, but it cannot prove a physical timeline either way.
        return None
    gaps = [right - left for left, right in zip(pts, pts[1:], strict=False)]
    if any(gap <= 0 for gap in gaps):
        return False
    largest_gap = max(gaps)
    expected_duration = 1.0 / expected_fps
    max_bounded_gap = max(1.0, expected_duration * 4.0)
    return math.isfinite(largest_gap) and largest_gap <= max_bounded_gap


def probe_video_physical_timeline(
        media_path: str | os.PathLike[str], expected_fps: float, *,
        timeout_seconds: float = 60.0,
) -> bool | None:
    """Probe whether the physical packet PTS timeline has no large holes."""

    packets = _probe_video_packets(
        media_path, timeout_seconds=timeout_seconds)
    if packets is None:
        return None
    return evaluate_physical_video_timeline(packets, expected_fps)


@dataclass(frozen=True)
class VideoCfrProbeEvidence:
    """One packet probe result used by the finalization gate."""

    cfr: bool | None
    physical_timeline_bounded: bool | None


_timing_cache: OrderedDict[tuple, VideoCfrProbeEvidence] = OrderedDict()
_timing_cache_lock = threading.Lock()


def _timing_fingerprint(media_path, expected_fps):
    try:
        path = Path(media_path).resolve()
        stat = path.stat()
        return (str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
                expected_fps)
    except OSError:
        return None


def probe_video_cfr_evidence(
        media_path: str | os.PathLike[str], expected_fps: float, *,
        timeout_seconds: float = 60.0,
) -> VideoCfrProbeEvidence:
    """Reuse timing evidence only while the exact media file is unchanged.

    Source validation and final publication often inspect the same packets.
    Keep only small summaries, and invalidate after any overlay/audio rewrite.
    Failed probes are retried rather than cached.
    """
    key = _timing_fingerprint(media_path, expected_fps)
    if key is not None:
        with _timing_cache_lock:
            evidence = _timing_cache.get(key)
            if evidence is not None:
                _timing_cache.move_to_end(key)
                return evidence
    evidence = _probe_video_cfr_evidence(
        media_path, expected_fps, timeout_seconds=timeout_seconds)
    if (key is not None and evidence.physical_timeline_bounded is not None
            and key == _timing_fingerprint(media_path, expected_fps)):
        with _timing_cache_lock:
            _timing_cache[key] = evidence
            _timing_cache.move_to_end(key)
            while len(_timing_cache) > 32:
                _timing_cache.popitem(last=False)
    return evidence


def _probe_video_cfr_evidence(
        media_path: str | os.PathLike[str], expected_fps: float, *,
        timeout_seconds: float,
) -> VideoCfrProbeEvidence:
    """Probe duration and physical PTS evidence in one FFprobe invocation."""

    if (not math.isfinite(expected_fps)
            or expected_fps <= 0
            or expected_fps > 1000):
        return VideoCfrProbeEvidence(None, None)
    packets = _probe_video_packets(
        media_path, timeout_seconds=timeout_seconds)
    if packets is None:
        return VideoCfrProbeEvidence(None, None)

    expected_duration = 1.0 / expected_fps
    tolerance = max(0.00005, expected_duration * 0.002)
    physical_timeline = evaluate_physical_video_timeline(packets, expected_fps)
    for packet in packets:
        duration = _positive_float(packet.get('duration_time'))
        if duration is None:
            return VideoCfrProbeEvidence(None, physical_timeline)
        if abs(duration - expected_duration) > tolerance:
            return VideoCfrProbeEvidence(False, physical_timeline)

    # Nominal durations without packet PTS cannot prove a healthy CFR stream.
    if physical_timeline is not True:
        return VideoCfrProbeEvidence(physical_timeline, physical_timeline)
    return VideoCfrProbeEvidence(True, True)


def probe_video_cfr(
        media_path: str | os.PathLike[str], expected_fps: float, *,
        timeout_seconds: float = 60.0,
) -> bool | None:
    """Check sample durations rather than advertised average FPS.

    Return True for CFR, False when repair is needed, or None if probing fails.
    Callers conservatively repair inconclusive results.
    """

    return probe_video_cfr_evidence(
        media_path, expected_fps, timeout_seconds=timeout_seconds).cfr


def format_fps(value: float | None) -> str:
    if value is None or not math.isfinite(value) or value <= 0:
        return 'Unavailable'
    rounded_integer = round(value)
    if abs(value - rounded_integer) < 0.005:
        return f'{rounded_integer} FPS'
    return f'{value:.2f}'.rstrip('0').rstrip('.') + ' FPS'


def format_bitrate(value_bps: int | None) -> str:
    if value_bps is None or value_bps <= 0:
        return 'Unavailable'
    if value_bps >= 1_000_000:
        return f'{value_bps / 1_000_000:.2f}'.rstrip('0').rstrip('.') + ' Mbps'
    return f'{value_bps / 1000:.0f} kbps'
