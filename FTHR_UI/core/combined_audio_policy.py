"""Choose which native audio streams need combined-track finalization.

Only add sources absent from Default Mix to avoid counting audio twice.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import subprocess
from typing import Any, Iterable

from core.audio_manifest import manifest_path_for, read_manifest_for_media
from core.audio_processing import SourceProcessing, mixed_output_metadata, parse_source_handler
from core.capture_highlights import media_identity, read_capture_highlights, write_capture_highlights
from core.ffmpeg_tools import get_ffmpeg_exe, get_ffprobe_exe
from core.media_identity import file_identity
from core.transactional_output import (
    commit_staged_output, create_staged_output_path, discard_staged_output,
)


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
    native = parse_source_handler(stream.handler_name)
    return {
        value.strip().casefold()
        for value in (stream.title, stream.handler_name,
                      native['display_name'] if native else None)
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


def build_combined_audio_filter(
        audio_indexes: Iterable[int], *, pad_to_video_end: bool = False,
        streams: Iterable[AudioStreamDescriptor] = ()) -> str:
    """Build the production filter used for a combined native audio track.

    Native audio streams can have different first presentation timestamps.  A
    multi-input ``amix`` otherwise starts each input at its first decoded
    sample and silently erases that offset.  Normalize each input with
    ``aresample=first_pts=0`` first; this preserves the stream's shared
    timeline while keeping the existing limiter and mix policy unchanged.

    Render the gain and channel settings stored with native dry audio. A
    single input with default processing remains ``anull``.
    When ``pad_to_video_end`` is enabled, append ``apad`` after the existing
    limiter output. The caller's ``-shortest`` mux bound then ends the padded
    audio at the video presentation end without changing the leading timeline.
    """
    indexes = tuple(audio_indexes)
    if not indexes:
        raise ValueError('at least one audio stream is required')
    processing = {}
    for stream in streams:
        native = parse_source_handler(stream.handler_name)
        if native:
            processing[stream.audio_index] = SourceProcessing(
                native['gain_percent'], native['mono']).filter()
    if len(indexes) == 1:
        index = indexes[0]
        stage = f'[0:a:{index}]{processing.get(index, "anull")}'
        if not pad_to_video_end:
            return f'{stage}[aout]'
        return f'{stage},apad[aout]'

    normalized_labels = tuple(f'audio_input_{position}'
                              for position in range(len(indexes)))
    input_filters = {index: f'{stage},' for index, stage in processing.items() if stage != 'anull'}
    normalized = ';'.join(
        f'[0:a:{index}]{input_filters.get(index, "")}aresample=first_pts=0:async=0[{label}]'
        for index, label in zip(indexes, normalized_labels, strict=True))
    mix_inputs = ''.join(f'[{label}]' for label in normalized_labels)
    mix = (
        f'{mix_inputs}amix=inputs={len(indexes)}:normalize=0:'
        'duration=longest:dropout_transition=0,'
        'alimiter=limit=0.98:level=disabled')
    if pad_to_video_end:
        return f'{normalized};{mix}[limited];[limited]apad[aout]'
    return f'{normalized};{mix}[aout]'


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


def _manifest_identity(path: Path) -> tuple | None:
    try:
        return file_identity(path)
    except FileNotFoundError:
        return None


def combine_recording_audio(path: str | Path) -> None:
    """Render native system and microphone tracks into the recording's MP4.

    Keep the original recording and its markers until the mix is ready to
    replace it. A recording with no audio requires no rewrite.
    """
    media = Path(path)
    sidecar = manifest_path_for(media)
    staged = create_staged_output_path(media)
    parked_manifest = create_staged_output_path(sidecar)
    run_options = dict(capture_output=True, text=True, check=True,
                       encoding='utf-8', errors='replace',
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        identity = media_identity(media)
        probe = subprocess.run([
            get_ffprobe_exe(), '-v', 'error', '-show_entries',
            'format=duration:stream=index,codec_type:stream_tags=title,handler_name',
            '-of', 'json', str(media),
        ], timeout=30, **run_options)
        metadata = json.loads(probe.stdout)
        streams = tuple(stream for stream in descriptors_from_ffprobe_streams(
            metadata.get('streams', ())) if not (stream.handler_name or '').startswith(
                'FTHR reserved audio '))
        if not streams:
            return
        manifest_identity = _manifest_identity(sidecar)
        manifest = None
        if manifest_identity is not None:
            manifest = read_manifest_for_media(media)
            if manifest is None:
                raise RuntimeError('The recording audio manifest could not be verified.')
        indexes = select_combined_audio_streams(manifest, streams)
        if not indexes:
            raise RuntimeError('The recording has no system or microphone audio to combine.')
        markers = read_capture_highlights(media, expected_identity=identity)
        try:
            duration = float(metadata.get('format', {}).get('duration', 0))
        except (TypeError, ValueError):
            duration = 0
        timeout = max(120, duration * 2) if math.isfinite(duration) else 120
        subprocess.run([
            get_ffmpeg_exe(), '-nostdin', '-hide_banner', '-loglevel', 'error', '-xerror',
            '-i', str(media), '-filter_complex',
            build_combined_audio_filter(indexes, pad_to_video_end=True, streams=streams),
            '-map', '0:v:0', '-map', '[aout]', '-map_metadata', '0', '-map_chapters', '0',
            '-movflags', 'use_metadata_tags', *mixed_output_metadata(),
            '-metadata', 'fthr_audio_mode=combined', '-c:v', 'copy',
            '-c:a', 'aac', '-b:a', '192k', '-shortest', str(staged),
        ], timeout=timeout, **run_options)
        if markers is not None:
            write_capture_highlights(staged, markers)
        if (media_identity(media) != identity
                or _manifest_identity(sidecar) != manifest_identity):
            raise RuntimeError('The recording changed while its audio was being combined.')
        if manifest_identity is not None:
            sidecar.replace(parked_manifest)
        try:
            commit_staged_output(staged, media)
        except OSError:
            if manifest_identity is not None:
                parked_manifest.replace(sidecar)
            raise
        discard_staged_output(parked_manifest)
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or '').strip()
        raise RuntimeError(f'Could not combine recording audio. {detail}'.rstrip()) from error
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise RuntimeError(f'Could not combine recording audio. {error}') from error
    finally:
        discard_staged_output(staged)
