"""Clip-local audio sources and gain/headroom rules shared by playback and export."""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Any, Iterable, Sequence


CANONICAL_SAMPLE_RATE = 48_000
CANONICAL_CHANNELS = 2
LIMITER_CEILING = 0.98


@dataclass(frozen=True)
class ProbedAudioStream:
    """One audio stream reported by the container probe.

    ``container_index`` is FFmpeg's absolute stream index. ``audio_index`` is
    its ordinal among audio streams, which is what FFmpeg's ``0:a:N`` mapping
    syntax requires for export filters.
    """

    container_index: int
    audio_index: int
    title: str | None = None
    # Capture-owned container marker. ``combined`` means the source streams
    # have already been mixed into one recorded track, so only MASTER remains
    # editable in the clip editor.
    audio_mode: str | None = None


@dataclass(frozen=True)
class PlaybackSource:
    """A source row belonging to this open clip only."""

    source_id: str
    display_name: str
    source_type: str
    container_index: int | None
    audio_index: int | None
    icon_reference: str | None = None
    available: bool = True
    # ``base`` is the hidden recorded desktop mix. ``delta`` is an isolated
    # application stem applied as (user_gain - 1) against that base. ``direct``
    # sources such as microphones and imported tracks use their slider gain.
    mix_role: str = 'direct'
    editable: bool = True
    # Privacy-safe executable identity from a verified native manifest. This
    # is a basename-like key (never a path) and lets the editor resolve a
    # friendly legacy label and, while the app is running, its real icon.
    persistent_identity: str | None = None


@dataclass(frozen=True)
class SourceMixState:
    """Non-destructive current-editor state for one source."""

    gain_percent: int = 100
    muted: bool = False

    @property
    def gain(self) -> float:
        return 0.0 if self.muted else max(0, min(100, self.gain_percent)) / 100.0


def source_icon_key(source: PlaybackSource) -> str:
    """Return a safe, semantic icon key without treating manifest text as a path.

    The capture manifest intentionally stores a privacy-safe icon *reference*,
    not an executable or image path.  Consume the known references where they
    exist and use a generic type-specific marker for legacy/imported media.
    """

    reference = (source.icon_reference or '').casefold()
    if reference == 'microphone':
        return 'microphone'
    if reference == 'system-audio':
        return 'system'
    if reference in {'windows-app-icon', 'generic-app'}:
        return 'application'
    if source.source_type == 'microphone':
        return 'microphone'
    if source.source_type == 'system':
        return 'system'
    if source.source_type == 'application':
        return 'application'
    return 'track'


def source_display_name(source: PlaybackSource) -> str:
    """Label the native Default Mix as system audio in the viewer.

    Other labels retain their manifest or container-provided names.
    """

    identity = (source.persistent_identity or '').casefold()
    if identity in {'fpsaimtrainer', 'fpsaimtrainer-win64-shipping'}:
        return "KovaaK's"
    if source.source_type == 'system' and source.display_name.casefold() == 'default mix':
        return 'System Audio'
    return source.display_name


def order_playback_sources(sources: Sequence[PlaybackSource]) -> tuple[PlaybackSource, ...]:
    """Order application stems, editable system audio, microphone, then imports.

    Use container indices as stable tie-breakers; display names are not IDs.
    """

    priority = {'application': 0, 'system': 1, 'microphone': 2, 'track': 3}
    return tuple(sorted(
        sources,
        key=lambda source: (
            priority.get(source.source_type, 4),
            source.container_index if source.container_index is not None else 2**31 - 1,
            source.audio_index if source.audio_index is not None else 2**31 - 1,
            source.source_id,
        ),
    ))


def build_playback_sources(
    manifest: dict[str, Any] | None,
    streams: Sequence[ProbedAudioStream],
) -> tuple[PlaybackSource, ...]:
    """Build editable sources from verified manifests or container stream labels.

    System and microphone tracks are independent unless application stems exist.
    With stems, Default Mix is hidden and app sliders apply
    ``base + (gain - 1) * app_stem``. Combined captures expose no source sliders,
    even if an old sidecar remains.
    """

    by_container = {stream.container_index: stream for stream in streams}
    audio_mode = next((stream.audio_mode for stream in streams
                       if stream.audio_mode in {'combined', 'separated'}), None)
    combined_capture = audio_mode == 'combined'
    if manifest:
        entries = sorted(manifest.get('sources', ()),
                         key=lambda entry: int(entry.get('stream_index', -1)))
        has_application_stem = any(
            entry.get('source_type') == 'application' for entry in entries)
        sources: list[PlaybackSource] = []
        for entry in entries:
            name = str(entry.get('display_name', 'Audio Track'))
            compatibility_base = bool(
                has_application_stem
                and entry.get('source_type') == 'system'
                and name.casefold() == 'default mix')
            application_delta = bool(
                has_application_stem
                and entry.get('source_type') == 'application')
            container_index = entry.get('stream_index')
            stream = by_container.get(container_index)
            sources.append(PlaybackSource(
                source_id=str(entry.get('source_uuid', f'manifest:{container_index}')),
                display_name=name,
                source_type=str(entry.get('source_type', 'application')),
                container_index=container_index if isinstance(container_index, int) else None,
                audio_index=stream.audio_index if stream else None,
                icon_reference=entry.get('icon_reference'),
                available=stream is not None,
                mix_role=('base' if compatibility_base else
                          'delta' if application_delta else 'direct'),
                editable=not compatibility_base and not combined_capture,
                persistent_identity=(str(entry.get('persistent_identity'))
                                     if entry.get('persistent_identity') else None),
            ))
        return order_playback_sources(sources)

    return order_playback_sources(tuple(
        PlaybackSource(
            source_id=f'stream:{stream.container_index}',
            display_name=(stream.title.strip() if stream.title and stream.title.strip()
                          else f'Track {stream.audio_index + 1}'),
            source_type='track',
            container_index=stream.container_index,
            audio_index=stream.audio_index,
            editable=not combined_capture,
        )
        for stream in streams
    ))


def source_gain(source: PlaybackSource, states: dict[str, SourceMixState]) -> float:
    """Return the source's current linear gain, respecting availability."""

    if not source.available:
        return 0.0
    if source.mix_role == 'base':
        return 1.0
    gain = states.get(source.source_id, SourceMixState()).gain
    if source.mix_role == 'delta':
        return gain - 1.0
    return gain


def active_source_count(sources: Iterable[PlaybackSource],
                        states: dict[str, SourceMixState]) -> int:
    """Count non-muted, decodable source rows for deterministic headroom."""

    return sum(abs(source_gain(source, states)) > 1e-6 for source in sources)


def headroom_for_active_sources(active_sources: int) -> float:
    """Return the conservative equal-power mix headroom.

    One source remains at recorded level.  Two equal-level stems receive -3 dB
    total headroom, four receive -6 dB, and so on.  This is not per-source
    normalization and changes no stored media.
    """

    return 1.0 / sqrt(max(1, active_sources))


def limit_sample(value: float) -> float:
    """Instantaneous zero-lookahead hard limiter used by live output.

    The canonical stream is already headroom-scaled.  The final clamp only
    protects the output device from an unusual correlated full-scale sum; it
    adds no lookahead latency and never writes back into a clip.
    """

    return max(-LIMITER_CEILING, min(LIMITER_CEILING, value))


def mix_interleaved_float(
    tracks: Sequence[Sequence[float]],
    gains: Sequence[float],
    master_gain: float = 1.0,
) -> list[float]:
    """Mix equal-format interleaved PCM for deterministic tests/fallbacks."""

    if len(tracks) != len(gains):
        raise ValueError('tracks and gains must have the same length')
    sample_count = max((len(track) for track in tracks), default=0)
    active = sum(gain > 0.0 for gain in gains)
    headroom = headroom_for_active_sources(active)
    master = max(0.0, min(1.0, master_gain))
    output = [0.0] * sample_count
    for index in range(sample_count):
        mixed = sum(track[index] * gain
                    for track, gain in zip(tracks, gains, strict=True)
                    if index < len(track))
        output[index] = limit_sample(mixed * headroom) * master
    return output


def mono_to_stereo(samples: Sequence[float]) -> list[float]:
    """Duplicate a canonical mono source into the two playback channels."""

    return [channel for sample in samples for channel in (sample, sample)]


def resample_linear(samples: Sequence[float], source_rate: int,
                    destination_rate: int) -> list[float]:
    """Small deterministic reference resampler used only by unit tests.

    Production decode/resampling occurs in the native FFmpeg bridge.  Keeping
    this reference here lets the source-model contract cover 44.1 kHz inputs
    without pretending that Python performs the live decode work.
    """

    if source_rate <= 0 or destination_rate <= 0:
        raise ValueError('sample rates must be positive')
    if source_rate == destination_rate or not samples:
        return list(samples)
    output_size = max(1, round(len(samples) * destination_rate / source_rate))
    scale = source_rate / destination_rate
    output: list[float] = []
    for index in range(output_size):
        position = index * scale
        lower = min(int(position), len(samples) - 1)
        upper = min(lower + 1, len(samples) - 1)
        fraction = position - lower
        output.append(samples[lower] * (1.0 - fraction) + samples[upper] * fraction)
    return output


def ffmpeg_mix_filter(
    sources: Sequence[PlaybackSource],
    states: dict[str, SourceMixState],
    master_gain: float,
    *,
    output_label: str = 'aout',
) -> tuple[list[str], str | None]:
    """Return the export filter chain implementing the viewer's mix policy.

    FFmpeg's limiter is intentionally export-only.  It is transparent below
    the same 0.98 ceiling as live playback; unlike live output, export can use
    lookahead because it has no interactive latency budget.
    """

    selected = [source for source in sources
                if source.audio_index is not None and source.available]
    if not selected:
        return [], None
    filters: list[str] = []
    input_labels: list[str] = []
    for index, source in enumerate(selected):
        gain = source_gain(source, states)
        label = f'src{index}'
        filters.append(
            f'[0:a:{source.audio_index}]aresample=first_pts=0:async=0,'
            f'volume={gain:.6f}[{label}]')
        input_labels.append(f'[{label}]')
    active = active_source_count(selected, states)
    headroom = headroom_for_active_sources(active)
    master = max(0, min(100, master_gain)) / 100.0
    filters.append(
        f'{"".join(input_labels)}amix=inputs={len(input_labels)}:normalize=0,'
        f'volume={headroom:.6f},alimiter=limit={LIMITER_CEILING}:level=disabled,'
        f'volume={master:.6f}'
        f'[{output_label}]')
    return filters, f'[{output_label}]'
