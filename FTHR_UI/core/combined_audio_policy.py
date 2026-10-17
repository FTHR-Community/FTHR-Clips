"""Choose which native audio streams need combined-track finalization.

Only add sources absent from Default Mix to avoid counting audio twice.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class AudioStreamDescriptor:
    """An audio stream with both its container and audio-ordinal indexes."""

    container_index: int
    audio_index: int
    title: str | None = None
    handler_name: str | None = None


_SYSTEM_LABELS = {'system audio', 'default mix', 'system'}
_MICROPHONE_LABELS = {'microphone', 'mic', 'mic audio'}


def _source_type(entry: Any) -> str:
    if not isinstance(entry, dict):
        return ''
    return str(entry.get('source_type') or '').strip().casefold()


def _manifest_stream_indexes(manifest: dict[str, Any]) -> set[int]:
    """Return verified manifest indexes for the system/mic base sources."""
    selected: set[int] = set()
    for entry in manifest.get('sources', ()):
        if _source_type(entry) not in {'system', 'microphone'}:
            continue
        if isinstance(entry, dict) and isinstance(entry.get('stream_index'), int):
            selected.add(entry['stream_index'])
    return selected


def _label_set(stream: AudioStreamDescriptor) -> set[str]:
    return {
        value.strip().casefold()
        for value in (stream.title, stream.handler_name)
        if isinstance(value, str) and value.strip()
    }


def select_combined_audio_streams(
    manifest: dict[str, Any] | None,
    streams: Iterable[AudioStreamDescriptor],
) -> tuple[int, ...]:
    """Select audio ordinals for a combined track.

    Prefer a validated manifest; application-only stems yield no selection
    because Default Mix already includes them. Legacy clips use stream labels,
    falling back to all streams only when none are labeled.
    """
    ordered = tuple(streams)
    if manifest is not None:
        manifest_indexes = _manifest_stream_indexes(manifest)
        return tuple(stream.audio_index for stream in ordered
                     if stream.container_index in manifest_indexes)

    labeled: list[int] = []
    for stream in ordered:
        labels = _label_set(stream)
        if labels & (_SYSTEM_LABELS | _MICROPHONE_LABELS):
            labeled.append(stream.audio_index)
    if labeled:
        return tuple(labeled)
    return tuple(stream.audio_index for stream in ordered)


def build_combined_audio_filter(audio_indexes: Iterable[int]) -> str:
    """Build the production filter used for a combined native audio track.

    Native audio streams can have different first presentation timestamps.  A
    multi-input ``amix`` otherwise starts each input at its first decoded
    sample and silently erases that offset.  Normalize each input with
    ``aresample=first_pts=0`` first; this preserves the stream's shared
    timeline while keeping the existing limiter and mix policy unchanged.

    The single-input path intentionally remains ``anull`` for compatibility.
    """
    indexes = tuple(audio_indexes)
    if not indexes:
        raise ValueError('at least one audio stream is required')
    audio_inputs = ''.join(f'[0:a:{index}]' for index in indexes)
    if len(indexes) == 1:
        return f'{audio_inputs}anull[aout]'

    normalized_labels = tuple(f'audio_input_{position}'
                              for position in range(len(indexes)))
    normalized = ';'.join(
        f'[0:a:{index}]aresample=first_pts=0:async=0[{label}]'
        for index, label in zip(indexes, normalized_labels, strict=True))
    mix_inputs = ''.join(f'[{label}]' for label in normalized_labels)
    mix = (
        f'{mix_inputs}amix=inputs={len(indexes)}:normalize=0:'
        'duration=longest:dropout_transition=0,'
        'alimiter=limit=0.98:level=disabled[aout]')
    return f'{normalized};{mix}'


def descriptors_from_ffprobe_streams(
    streams: Iterable[dict[str, Any]],
) -> tuple[AudioStreamDescriptor, ...]:
    """Convert ffprobe's stream objects into stable audio ordinals."""
    result: list[AudioStreamDescriptor] = []
    audio_index = 0
    for stream in streams:
        if not isinstance(stream, dict) or stream.get('codec_type') != 'audio':
            continue
        container_index = stream.get('index')
        if not isinstance(container_index, int):
            continue
        tags = stream.get('tags')
        tags = tags if isinstance(tags, dict) else {}
        result.append(AudioStreamDescriptor(
            container_index=container_index,
            audio_index=audio_index,
            title=tags.get('title') if isinstance(tags.get('title'), str) else None,
            handler_name=(tags.get('handler_name')
                          if isinstance(tags.get('handler_name'), str) else None),
        ))
        audio_index += 1
    return tuple(result)
