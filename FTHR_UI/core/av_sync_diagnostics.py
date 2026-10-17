"""Bounded FFprobe summaries for saved-clip A/V timeline diagnostics."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import subprocess
from typing import Any


class TimelineProbeError(RuntimeError):
    """The saved container did not provide a trustworthy stream timeline."""


@dataclass(frozen=True)
class StreamTimeline:
    start_time: float
    duration: float
    stream_index: int | None = None

    @property
    def end_time(self) -> float:
        return self.start_time + self.duration


@dataclass(frozen=True)
class ClipAvTimeline:
    video: StreamTimeline
    audio: StreamTimeline
    audio_stream_count: int
    audio_streams: tuple[StreamTimeline, ...] = ()

    @property
    def av_start_delta_ms(self) -> float:
        """Positive means audio starts later than video."""
        return (self.audio.start_time - self.video.start_time) * 1000.0

    @property
    def av_end_delta_ms(self) -> float:
        """Positive means audio ends later than video."""
        return (self.audio.end_time - self.video.end_time) * 1000.0

    def diagnostic_fields(self) -> dict[str, Any]:
        # Stream metadata is deliberately bounded: this is diagnostic context,
        # not a packet/frame log.  Keep the aggregate fields above stable for
        # existing consumers and expose enough per-stream timing to correlate
        # native stems without allowing an unbounded report.
        stream_limit = 16
        stream_fields = [
            {
                'stream_index': stream.stream_index,
                'start_time': stream.start_time,
                'duration': stream.duration,
                'end_time': stream.end_time,
            }
            for stream in self.audio_streams[:stream_limit]
        ]
        return {
            'timeline_time_unit': 'seconds',
            'post_mux_video_start_time': self.video.start_time,
            'post_mux_audio_start_time': self.audio.start_time,
            'post_mux_video_duration': self.video.duration,
            'post_mux_audio_duration': self.audio.duration,
            'post_mux_av_start_delta_ms': self.av_start_delta_ms,
            'post_mux_av_end_delta_ms': self.av_end_delta_ms,
            'audio_stream_count': self.audio_stream_count,
            'audio_stream_timelines': stream_fields,
            'audio_stream_timelines_truncated': max(
                0, len(self.audio_streams) - stream_limit),
        }


def _finite_number(value: Any, field: str, *, nonnegative: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise TimelineProbeError(f'{field} is unavailable') from error
    if not math.isfinite(number) or (nonnegative and number < 0):
        raise TimelineProbeError(f'{field} is invalid')
    return number


def parse_ffprobe_timeline(payload: dict[str, Any]) -> ClipAvTimeline:
    """Summarize the first video and the envelope of all audio streams."""
    streams = payload.get('streams')
    if not isinstance(streams, list):
        raise TimelineProbeError('stream list is unavailable')
    video: StreamTimeline | None = None
    audio_streams: list[StreamTimeline] = []
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        codec_type = stream.get('codec_type')
        if codec_type not in {'video', 'audio'}:
            continue
        timeline = StreamTimeline(
            start_time=_finite_number(stream.get('start_time'), 'start_time'),
            duration=_finite_number(
                stream.get('duration'), 'duration', nonnegative=True),
            stream_index=(stream.get('index')
                          if isinstance(stream.get('index'), int) else None),
        )
        if codec_type == 'video' and video is None:
            video = timeline
        elif codec_type == 'audio':
            audio_streams.append(timeline)
    if video is None:
        raise TimelineProbeError('video stream is unavailable')
    if not audio_streams:
        raise TimelineProbeError('audio stream is unavailable')
    audio_start = min(stream.start_time for stream in audio_streams)
    audio_end = max(stream.end_time for stream in audio_streams)
    return ClipAvTimeline(
        video=video,
        audio=StreamTimeline(audio_start, audio_end - audio_start),
        audio_stream_count=len(audio_streams),
        audio_streams=tuple(audio_streams),
    )


def probe_clip_av_timeline(
    ffprobe: str, media_path: str, *, timeout: float = 30.0,
) -> ClipAvTimeline:
    """Read only bounded stream metadata; never enumerate media packets."""
    try:
        result = subprocess.run(
            [
                ffprobe, '-v', 'error', '-show_entries',
                'stream=index,codec_type,start_time,duration',
                '-of', 'json', media_path,
            ],
            capture_output=True, text=True, timeout=timeout,
            creationflags=(getattr(subprocess, 'CREATE_NO_WINDOW', 0)),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise TimelineProbeError(type(error).__name__) from error
    if result.returncode != 0:
        detail = (result.stderr or '').strip().splitlines()
        raise TimelineProbeError(detail[-1] if detail else 'ffprobe failed')
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise TimelineProbeError('ffprobe returned invalid JSON') from error
    if not isinstance(payload, dict):
        raise TimelineProbeError('ffprobe returned an invalid document')
    return parse_ffprobe_timeline(payload)


__all__ = [
    'ClipAvTimeline', 'StreamTimeline', 'TimelineProbeError',
    'parse_ffprobe_timeline', 'probe_clip_av_timeline',
]
