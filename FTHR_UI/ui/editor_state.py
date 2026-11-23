"""Immutable clip edits, persisted drafts, and output-size constraints."""

import math
import os
from dataclasses import dataclass

from core.audio_processing import SourceProcessing
from core.export_profiles import MAX_TARGET_DIMENSION, MIN_TARGET_DIMENSION
from core.playback_mix_model import MAX_IMPORTED_AUDIO_TRACKS, PlaybackSource


@dataclass(frozen=True)
class EditorSnapshot:
    """One reversible edit state; playback and timeline navigation are excluded."""

    trim_start: float
    trim_end: float
    segments: tuple[tuple[float, float, bool], ...]
    selected_segment: int
    crop_rect: tuple[int, int, int, int] | None
    stretch_ratio: float
    effects: tuple[tuple[str, int], ...]
    speed_rate: float = 1.0
    preserve_pitch: bool = True
    master_volume: int = 80
    source_volumes: tuple[tuple[str, int], ...] = ()
    source_mutes: tuple[tuple[str, bool], ...] = ()
    imported_sources: tuple[PlaybackSource, ...] = ()
    removed_audio_sources: tuple[str, ...] = ()
    limiter_enabled: bool = True
    stretch_size: tuple[int, int] | None = None
    audio_segments: tuple[tuple[float, float, bool], ...] = ((0.0, 1.0, False),)
    source_audio_segments: tuple[tuple[str, tuple[tuple[float, float, bool], ...]], ...] = ()


_EDITOR_EFFECT_LIMITS = {
    'exposure': (-100, 100), 'contrast': (-100, 100),
    'saturation': (-100, 100), 'temperature': (-100, 100),
    'sharpness': (0, 100), 'vignette': (0, 100),
}

# The bundled software encoder is the export fallback for all hardware paths.
_MAX_STRETCH_PIXELS = 9_437_184


def _stretch_axis_limits(source: int) -> tuple[int, int]:
    minimum = max(MIN_TARGET_DIMENSION, 2 * math.ceil(source / 8))
    maximum = min(MAX_TARGET_DIMENSION, 4 * source)
    return minimum, maximum


def _valid_stretch_size(size) -> bool:
    return (isinstance(size, (list, tuple)) and len(size) == 2
            and all(type(axis) is int and MIN_TARGET_DIMENSION <= axis <= MAX_TARGET_DIMENSION
                    and axis % 2 == 0 for axis in size)
            and size[0] * size[1] <= _MAX_STRETCH_PIXELS)


def _bound_stretch_size(size: tuple[int, int], source_w: int,
                        source_h: int) -> tuple[int, int] | None:
    """Preserve the requested dimensions where the new crop permits them."""
    min_w, max_w = _stretch_axis_limits(source_w)
    min_h, max_h = _stretch_axis_limits(source_h)
    max_w = min(max_w, (_MAX_STRETCH_PIXELS // min_h) & ~1)
    if min_w > max_w or min_h > max_h:
        return None
    width = max(min_w, min(max_w, size[0]))
    max_h = min(max_h, (_MAX_STRETCH_PIXELS // width) & ~1)
    return width, max(min_h, min(max_h, size[1]))


def _stretch_preview_ratio(ratio: float, size: tuple[int, int] | None,
                           crop_rect, source_w: int, source_h: int) -> float:
    if size is None:
        return ratio
    width, height = crop_rect[2:] if crop_rect else (source_w, source_h)
    return (size[0] / size[1]) * (max(1, height) / max(1, width))


def _editor_snapshot_to_draft(state: EditorSnapshot) -> dict:
    """Convert an immutable history snapshot into compact JSON-safe data."""

    return {
        'trim_start': state.trim_start,
        'trim_end': state.trim_end,
        'segments': [list(segment) for segment in state.segments],
        'audio_segments': [list(segment) for segment in state.audio_segments],
        'source_audio_segments': {
            source_id: [list(segment) for segment in segments]
            for source_id, segments in state.source_audio_segments},
        'selected_segment': state.selected_segment,
        'crop_rect': list(state.crop_rect) if state.crop_rect else None,
        'stretch_ratio': state.stretch_ratio,
        'stretch_size': list(state.stretch_size) if state.stretch_size else None,
        'effects': dict(state.effects),
        'speed_rate': state.speed_rate,
        'preserve_pitch': state.preserve_pitch,
        'master_volume': state.master_volume,
        'source_volumes': dict(state.source_volumes),
        'source_mutes': dict(state.source_mutes),
        'imported_audio': [
            {'source_id': source.source_id, 'display_name': source.display_name,
             'media_path': source.media_path, 'container_index': source.container_index,
             'audio_index': source.audio_index, 'input_gain_percent': source.input_gain_percent,
             'mono': source.mono}
            for source in state.imported_sources],
        'removed_audio_sources': list(state.removed_audio_sources),
        'limiter_enabled': state.limiter_enabled,
    }


def _parse_segments(raw_segments):
    if not isinstance(raw_segments, list) or not 1 <= len(raw_segments) <= 500:
        return None
    segments = []
    previous_end = 0.0
    try:
        for index, raw in enumerate(raw_segments):
            if not isinstance(raw, (list, tuple)) or len(raw) != 3:
                return None
            start, end = float(raw[0]), float(raw[1])
            deleted = bool(raw[2])
            if (not math.isfinite(start) or not math.isfinite(end)
                    or start < 0.0 or end > 1.0 or end <= start
                    or (index == 0 and abs(start) > 0.0001)
                    or (index > 0 and abs(start - previous_end) > 0.0001)):
                return None
            # Preserve compatibility with drafts that have rounding at joins.
            if end <= previous_end:
                return None
            segments.append((previous_end, end, deleted))
            previous_end = end
    except (TypeError, ValueError):
        # Invalid persisted coordinates must never reach timeline paint logic.
        return None
    if abs(previous_end - 1.0) > 0.0001:
        return None
    start, _, deleted = segments[-1]
    segments[-1] = (start, 1.0, deleted)
    return tuple(segments)


def _editor_snapshot_from_draft(payload: dict) -> EditorSnapshot | None:
    """Validate persisted editor data before it is allowed near the widgets."""

    if not isinstance(payload, dict):
        return None
    try:
        trim_start = float(payload.get('trim_start', 0.0))
        trim_end = float(payload.get('trim_end', 1.0))
        stretch_ratio = float(payload.get('stretch_ratio', 1.0))
        speed_rate = float(payload.get('speed_rate', 1.0))
    except (TypeError, ValueError):
        # A malformed external draft is ignored in favor of editor defaults.
        return None
    if (not all(math.isfinite(value) for value in
                (trim_start, trim_end, stretch_ratio, speed_rate))
            or not 0.0 <= trim_start < trim_end <= 1.0
            or not 0.25 <= stretch_ratio <= 4.0
            or not 0.25 <= speed_rate <= 2.0):
        return None

    raw_size = payload.get('stretch_size')
    if raw_size is not None and not _valid_stretch_size(raw_size):
        return None
    segments = _parse_segments(payload.get('segments'))
    audio_segments = _parse_segments(payload.get('audio_segments', [[0.0, 1.0, False]]))
    if not segments or not any(not segment[2] for segment in segments) or not audio_segments:
        return None
    raw_source_segments = payload.get('source_audio_segments', {})
    if not isinstance(raw_source_segments, dict) or len(raw_source_segments) > 64:
        return None
    source_audio_segments = []
    for source_id, data in raw_source_segments.items():
        if not isinstance(source_id, str) or not source_id:
            return None
        lane = _parse_segments(data)
        if not lane:
            return None
        source_audio_segments.append((source_id, lane))

    try:
        selected = int(payload.get('selected_segment', 0))
    except (TypeError, ValueError):
        selected = 0
    selected = max(0, min(selected, len(segments) - 1))

    crop_rect = None
    raw_crop = payload.get('crop_rect')
    if isinstance(raw_crop, (list, tuple)) and len(raw_crop) == 4:
        try:
            candidate = tuple(int(value) for value in raw_crop)
            x, y, width, height = candidate
            if (0 <= x <= 100_000 and 0 <= y <= 100_000
                    and 2 <= width <= 100_000 and 2 <= height <= 100_000):
                crop_rect = candidate
        except (TypeError, ValueError):
            crop_rect = None

    raw_effects = payload.get('effects', {})
    if not isinstance(raw_effects, dict):
        raw_effects = {}
    effects = []
    for key, (minimum, maximum) in _EDITOR_EFFECT_LIMITS.items():
        try:
            value = int(raw_effects.get(key, 0))
        except (TypeError, ValueError):
            value = 0
        effects.append((key, max(minimum, min(maximum, value))))

    raw_preserve_pitch = payload.get('preserve_pitch', True)
    preserve_pitch = (raw_preserve_pitch
                      if isinstance(raw_preserve_pitch, bool) else True)

    raw_volumes = payload.get('source_volumes', {})
    raw_mutes = payload.get('source_mutes', {})
    if not isinstance(raw_volumes, dict) or not isinstance(raw_mutes, dict):
        return None
    if len(raw_volumes) > 64 or len(raw_mutes) > 64:
        return None
    if (any(not isinstance(key, str) or type(value) is not int or not 0 <= value <= 100
            for key, value in raw_volumes.items())
            or any(not isinstance(key, str) or not isinstance(value, bool)
                   for key, value in raw_mutes.items())):
        return None
    master = payload.get('master_volume', 80)
    if type(master) is not int or not 0 <= master <= 100:
        return None
    raw_imports = payload.get('imported_audio', [])
    removed = payload.get('removed_audio_sources', [])
    if (not isinstance(raw_imports, list) or len(raw_imports) > MAX_IMPORTED_AUDIO_TRACKS
            or not isinstance(removed, list) or len(removed) > 64
            or any(not isinstance(key, str) for key in removed)):
        return None
    imports = []
    for item in raw_imports:
        if not isinstance(item, dict):
            return None
        path = item.get('media_path')
        if (not isinstance(path, str) or not os.path.isabs(path) or '\x00' in path
                or not isinstance(item.get('source_id'), str)
                or not item['source_id'].startswith('import:')
                or not isinstance(item.get('display_name'), str)
                or type(item.get('container_index')) is not int
                or type(item.get('audio_index')) is not int
                or item['container_index'] < 0 or item['audio_index'] < 0):
            return None
        imports.append(PlaybackSource(
            item['source_id'], item['display_name'], 'track', item['container_index'],
            item['audio_index'], media_path=path, available=os.path.isfile(path),
            input_gain_percent=item.get('input_gain_percent', 100), mono=item.get('mono', False)))
        try:
            SourceProcessing(imports[-1].input_gain_percent, imports[-1].mono)
        except ValueError:
            # Reject the draft as a unit if any imported track has invalid processing settings.
            return None
    if len({source.source_id for source in imports}) != len(imports):
        return None
    limiter = payload.get('limiter_enabled', True)
    if not isinstance(limiter, bool):
        return None

    return EditorSnapshot(
        trim_start=trim_start,
        trim_end=trim_end,
        segments=segments,
        selected_segment=selected,
        crop_rect=crop_rect,
        stretch_ratio=stretch_ratio,
        effects=tuple(effects),
        speed_rate=speed_rate,
        preserve_pitch=preserve_pitch,
        master_volume=master,
        source_volumes=tuple(sorted(raw_volumes.items())),
        source_mutes=tuple(sorted(raw_mutes.items())),
        imported_sources=tuple(imports),
        removed_audio_sources=tuple(removed),
        limiter_enabled=limiter,
        stretch_size=tuple(raw_size) if raw_size is not None else None,
        audio_segments=audio_segments,
        source_audio_segments=tuple(sorted(source_audio_segments)),
    )
