"""Authoritative capture-setting policy and requested/active state tracking."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, auto


NORMAL_CLIP_VALUES = (5, 10, 15, 30, 45, 60, 90, 120, 180, 240, 300)
MAX_CLIP_SECONDS = 900
FPS_VALUES = (30, 60, 90, 120, 144, 165, 180, 240)
AUDIO_CAPTURE_MODE_COMBINED = 'combined'
AUDIO_CAPTURE_MODE_SEPARATED = 'separated'
AUDIO_CAPTURE_MODES = (
    AUDIO_CAPTURE_MODE_COMBINED,
    AUDIO_CAPTURE_MODE_SEPARATED,
)


def normalize_audio_capture_mode(value: object) -> str:
    """Return the only two persisted audio-capture modes we support.

    Settings are user-editable JSON, so unknown values fail closed to the
    default combined mode instead of accidentally exposing a partial mixer.
    """
    return (AUDIO_CAPTURE_MODE_SEPARATED
            if str(value).strip().lower() == AUDIO_CAPTURE_MODE_SEPARATED
            else AUDIO_CAPTURE_MODE_COMBINED)


MAX_AUDIO_DEVICE_TRACKS = 4
_UNSAFE_TRACK_NAME_CHARS = re.compile(r"[^A-Za-z0-9 ._+()'\-]")
MAX_AUDIO_DEVICE_TRACK_NAME = 40


def normalize_audio_device_tracks(value: object) -> list[dict[str, str]]:
    """Clean the persisted extra-device list to ``[{'id', 'name'}]`` entries.

    The list is user-editable JSON: drop malformed or duplicate entries and cap
    its length so the engine never receives more than it can record.
    """
    if not isinstance(value, (list, tuple)):
        return []
    tracks: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict):
            continue
        endpoint_id = str(entry.get('id') or '').strip()
        if not endpoint_id or endpoint_id in seen:
            continue
        # Clip manifests only accept this character set; anything else would
        # make the whole sidecar unreadable.
        name = ' '.join(_UNSAFE_TRACK_NAME_CHARS.sub(
            ' ', str(entry.get('name') or '')).split())
        name = name[:MAX_AUDIO_DEVICE_TRACK_NAME].strip()
        if not name:
            name = f'Audio Device {len(tracks) + 1}'
        seen.add(endpoint_id)
        tracks.append({'id': endpoint_id, 'name': name})
        if len(tracks) >= MAX_AUDIO_DEVICE_TRACKS:
            break
    return tracks


def encode_audio_device_tracks(tracks: object) -> str:
    """Serialize the device list into the engine's single launch argument."""
    return '|'.join(f"{entry['name']};{entry['id']}"
                    for entry in normalize_audio_device_tracks(tracks))


def resolve_audio_tracks(mode: object, device_tracks: object,
                         per_app_tracks: object) -> tuple[bool, bool]:
    """Return ``(separate_audio, application_tracks)`` for the engine.

    ``per_app_tracks`` is the independent per-program option; ``None`` (never
    chosen) follows the separated mode so existing setups keep their behavior.
    Any extra track keeps the clip multi-track instead of being merged away.
    """
    separated = normalize_audio_capture_mode(mode) == AUDIO_CAPTURE_MODE_SEPARATED
    application = separated if per_app_tracks is None else bool(per_app_tracks)
    return (separated or application
            or bool(normalize_audio_device_tracks(device_tracks)),
            application)


AUDIO_SOURCE_DEVICE = 'device'
AUDIO_SOURCE_PROGRAMS = 'programs'
AUDIO_SOURCE_DEVICES = 'devices'
AUDIO_SOURCE_MODES = (AUDIO_SOURCE_DEVICE, AUDIO_SOURCE_PROGRAMS,
                      AUDIO_SOURCE_DEVICES)


def effective_audio_source_mode(source_mode: object, capture_mode: object,
                                device_tracks: object,
                                per_app_tracks: object) -> str:
    """Return what the clip records: one device, each program or many devices.

    A saved choice wins. Older settings carry no choice, so derive it from the
    per-program switch / separated mode and then from the device list.
    """
    if source_mode in AUDIO_SOURCE_MODES:
        return str(source_mode)
    separated = (normalize_audio_capture_mode(capture_mode)
                 == AUDIO_CAPTURE_MODE_SEPARATED)
    if per_app_tracks is True or (per_app_tracks is None and separated):
        return AUDIO_SOURCE_PROGRAMS
    if normalize_audio_device_tracks(device_tracks):
        return AUDIO_SOURCE_DEVICES
    return AUDIO_SOURCE_DEVICE


def resolve_audio_plan(source_mode: object, capture_mode: object,
                       device_tracks: object, per_app_tracks: object
                       ) -> tuple[bool, bool, list[dict[str, str]]]:
    """Return ``(separate_audio, application_tracks, device_tracks)``."""
    mode = effective_audio_source_mode(
        source_mode, capture_mode, device_tracks, per_app_tracks)
    separated = (normalize_audio_capture_mode(capture_mode)
                 == AUDIO_CAPTURE_MODE_SEPARATED)
    if mode == AUDIO_SOURCE_PROGRAMS:
        return True, True, []
    if mode == AUDIO_SOURCE_DEVICES:
        return True, False, normalize_audio_device_tracks(device_tracks)
    return separated, False, []


def audio_plan_from_settings(settings) -> tuple[bool, bool, list[dict[str, str]]]:
    """Read the saved audio choices and resolve them for the engine."""
    return resolve_audio_plan(
        settings.get('audio_source_mode', None),
        settings.get('audio_capture_mode', AUDIO_CAPTURE_MODE_COMBINED),
        settings.get('audio_device_tracks', []),
        settings.get('per_app_audio_tracks', None))


def _validate_int(value: int, *, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f'{name} must be an integer')
    if not minimum <= value <= maximum:
        raise ValueError(f'{name} must be between {minimum} and {maximum}')
    return value


def validate_normal_clip_length(value: int) -> int:
    return _validate_int(value, name='normal clip length', minimum=5, maximum=MAX_CLIP_SECONDS)




def validate_fps(value: int) -> int:
    return _validate_int(value, name='frame rate', minimum=15, maximum=240)


@dataclass(frozen=True)
class CaptureConfig:
    fps: int
    buffer_seconds: int
    width: int
    height: int
    bitrate_kbps: int
    codec: str
    preset: int
    monitor: str
    scaling: str
    audio_enabled: bool
    encoder: str = 'auto'
    multiband_enabled: bool = False
    # ``multiband_enabled`` is a retired ABI compatibility field. This new
    # flag controls whether the saved MP4 keeps system and microphone audio as
    # separate streams; combined audio is the default.
    separate_audio_enabled: bool = False
    microphone_endpoint_id: str = ''
    playback_endpoint_id: str = ''
    microphone_mono: bool = False
    # Extra playback devices ("name;endpoint|...") recorded as own tracks and
    # the independent per-program track switch.
    audio_device_tracks: str = ''
    application_audio_tracks_enabled: bool = False
    normal_clip_seconds: int = 30
    crop_enabled: bool = False
    crop_x: float = 0.0
    crop_y: float = 0.0
    crop_w: float = 1.0
    crop_h: float = 1.0


def custom_clip_lengths(values) -> list[int]:
    if not isinstance(values, list):
        return []
    result = set()
    for value in values:
        try:
            result.add(validate_normal_clip_length(value))
        except ValueError:
            # User-edited invalid entries must not register unusable hotkeys.
            pass
    return sorted(result)


def compute_buffer_seconds(normal_seconds: int, custom_lengths=None) -> int:
    """Retain the longest configured clip with a native-safe safety margin."""
    normal = max([validate_normal_clip_length(normal_seconds)]
                 + custom_clip_lengths(custom_lengths))
    return min(MAX_CLIP_SECONDS, normal + 2)


class ApplyStatus(Enum):
    ACTIVE = auto()
    REQUESTED = auto()
    APPLYING = auto()
    FAILED = auto()


@dataclass
class CaptureConfigTracker:
    active: CaptureConfig | None = None
    requested: CaptureConfig | None = None
    status: ApplyStatus = ApplyStatus.ACTIVE
    error: str = ''

    def __post_init__(self) -> None:
        if self.requested is None:
            self.requested = self.active

    def request(self, config: CaptureConfig) -> None:
        self.requested = config
        self.status = ApplyStatus.REQUESTED
        self.error = ''

    def begin_apply(self) -> None:
        if self.requested is None:
            raise RuntimeError('no capture configuration has been requested')
        self.status = ApplyStatus.APPLYING
        self.error = ''

    def succeed(self) -> None:
        if self.requested is None:
            raise RuntimeError('no capture configuration has been requested')
        self.active = self.requested
        self.status = ApplyStatus.ACTIVE
        self.error = ''

    def fail(self, error: str) -> None:
        self.status = ApplyStatus.FAILED
        self.error = str(error)

    def deactivate(self, error: str) -> None:
        """Record that no running engine owns the previous active snapshot."""
        self.active = None
        self.status = ApplyStatus.FAILED
        self.error = str(error)
