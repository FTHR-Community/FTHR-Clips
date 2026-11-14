"""Authoritative capture-setting policy and requested/active state tracking."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
import re

from core.replay_storage import ReplayStorageSettings


NORMAL_CLIP_VALUES = (5, 10, 15, 30, 45, 60, 90, 120, 180, 240, 300)
MAX_CLIP_SECONDS = 900
FPS_VALUES = (30, 60, 90, 120, 144, 165, 180, 240)
AUDIO_CAPTURE_MODE_COMBINED = 'combined'
AUDIO_CAPTURE_MODE_SEPARATED = 'separated'
AUDIO_CAPTURE_MODES = (
    AUDIO_CAPTURE_MODE_COMBINED,
    AUDIO_CAPTURE_MODE_SEPARATED,
)


MAX_AUDIO_DEVICE_TRACKS = 4
_UNSAFE_TRACK_NAME_CHARS = re.compile(r"[^A-Za-z0-9 ._+()'\-]")
MAX_AUDIO_DEVICE_TRACK_NAME = 40


def _unsafe_audio_endpoint_id(value: str) -> bool:
    """Reject values that could change the native ``name;id|...`` ABI."""
    return any(char in ';|' or ord(char) < 32 or ord(char) == 127
               for char in value)


def normalize_audio_device_tracks(value: object) -> list[dict[str, str]]:
    """Return bounded, manifest-safe endpoint records from editable JSON.

    Endpoint IDs are opaque native values and therefore are never sanitized:
    malformed IDs are dropped so they cannot inject separators into argv[26].
    Display names are user-facing metadata and may be reduced to the manifest
    safe character set.
    """
    if not isinstance(value, (list, tuple)):
        return []
    tracks: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict):
            continue
        endpoint_id = str(entry.get('id') or '').strip()
        if (not endpoint_id or _unsafe_audio_endpoint_id(endpoint_id)
                or endpoint_id in seen):
            continue
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
    """Serialize endpoint records into the engine's single ABI argument."""
    return '|'.join(f"{entry['name']};{entry['id']}"
                    for entry in normalize_audio_device_tracks(tracks))


def resolve_audio_tracks(mode: object, device_tracks: object,
                         per_app_tracks: object) -> tuple[bool, bool]:
    """Return ``(separate_audio, application_tracks)`` for legacy callers."""
    separated = normalize_audio_capture_mode(mode) == AUDIO_CAPTURE_MODE_SEPARATED
    application = separated if per_app_tracks is None else bool(per_app_tracks)
    return (separated or application
            or bool(normalize_audio_device_tracks(device_tracks)),
            application)


AUDIO_SOURCE_DEVICE = 'device'
AUDIO_SOURCE_COMBINED = 'combined'
AUDIO_SOURCE_PROGRAMS = 'programs'
AUDIO_SOURCE_DEVICES = 'devices'
AUDIO_SOURCE_MODES = (AUDIO_SOURCE_DEVICE, AUDIO_SOURCE_COMBINED,
                      AUDIO_SOURCE_PROGRAMS, AUDIO_SOURCE_DEVICES)


def effective_audio_source_mode(source_mode: object, capture_mode: object,
                                device_tracks: object,
                                per_app_tracks: object) -> str:
    """Resolve saved mode first, then derive mode for legacy settings."""
    separated = (normalize_audio_capture_mode(capture_mode)
                 == AUDIO_CAPTURE_MODE_SEPARATED)
    if source_mode == AUDIO_SOURCE_DEVICE:
        # The previous device choice used the saved capture mode to decide
        # whether to mix. The renamed device choice is saved with separated.
        return AUDIO_SOURCE_DEVICE if separated else AUDIO_SOURCE_COMBINED
    if source_mode in AUDIO_SOURCE_MODES:
        return str(source_mode)
    if per_app_tracks is True or (per_app_tracks is None and separated):
        return AUDIO_SOURCE_PROGRAMS
    if normalize_audio_device_tracks(device_tracks):
        return AUDIO_SOURCE_DEVICES
    return AUDIO_SOURCE_DEVICE if separated else AUDIO_SOURCE_COMBINED


def resolve_audio_plan(source_mode: object, capture_mode: object,
                       device_tracks: object, per_app_tracks: object
                       ) -> tuple[bool, bool, list[dict[str, str]]]:
    """Return ``(separate_audio, application_tracks, device_tracks)``."""
    mode = effective_audio_source_mode(
        source_mode, capture_mode, device_tracks, per_app_tracks)
    if mode == AUDIO_SOURCE_PROGRAMS:
        return True, True, []
    if mode == AUDIO_SOURCE_DEVICES:
        return True, False, normalize_audio_device_tracks(device_tracks)
    return mode == AUDIO_SOURCE_DEVICE, False, []


def audio_plan_from_settings(settings) -> tuple[bool, bool, list[dict[str, str]]]:
    """Resolve all audio source settings through one shared policy."""
    return resolve_audio_plan(
        settings.get('audio_source_mode', None),
        settings.get('audio_capture_mode', AUDIO_CAPTURE_MODE_COMBINED),
        settings.get('audio_device_tracks', []),
        settings.get('per_app_audio_tracks', None))


def normalize_audio_capture_mode(value: object) -> str:
    """Return the only two persisted audio-capture modes we support.

    Settings are user-editable JSON, so unknown values fail closed to the
    default combined mode instead of accidentally exposing a partial mixer.
    """
    return (AUDIO_CAPTURE_MODE_SEPARATED
            if str(value).strip().lower() == AUDIO_CAPTURE_MODE_SEPARATED
            else AUDIO_CAPTURE_MODE_COMBINED)


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
    audio_device_tracks: str = ''
    application_audio_tracks_enabled: bool = False
    # Path-free executable identities, encoded as one bounded native argument.
    application_audio_exclusions: str = ''
    keep_system_audio_backup: bool = True
    normal_clip_seconds: int = 30
    crop_enabled: bool = False
    crop_x: float = 0.0
    crop_y: float = 0.0
    crop_w: float = 1.0
    crop_h: float = 1.0
    replay_storage: ReplayStorageSettings = ReplayStorageSettings()
    allow_replay_gaps: bool = True
    allow_low_fps_clips: bool = True
    capture_cursor: bool = True
    # Frozen launch policy, separate from persisted source/cursor preferences.
    capture_backend: str = 'auto'


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
    applying: CaptureConfig | None = field(default=None, init=False)
    applying_token: int | None = field(default=None, init=False)
    accepted_token: int | None = field(default=None, init=False)
    _token_counter: int = field(default=0, init=False, repr=False)
    _transition_revision: int = field(default=0, init=False, repr=False)

    @property
    def transition_revision(self) -> int:
        return self._transition_revision

    def _advance_transition(self) -> None:
        self._transition_revision += 1

    def __post_init__(self) -> None:
        if self.requested is None:
            self.requested = self.active

    def request(self, config: CaptureConfig) -> None:
        self.requested = config
        self.error = ''
        self._advance_transition()
        if self.applying is None:
            self.status = ApplyStatus.REQUESTED

    def begin_apply(self) -> int:
        if self.requested is None:
            raise RuntimeError('no capture configuration has been requested')
        self.applying = self.requested
        self._token_counter += 1
        self.applying_token = self._token_counter
        self.accepted_token = None
        self.status = ApplyStatus.APPLYING
        self.error = ''
        self._advance_transition()
        return self.applying_token

    def succeed(self, token: int | None = None) -> bool:
        if token is not None:
            if (self.applying is None
                    or self.applying_token != token):
                return False
        elif self.applying is None:
            raise RuntimeError('no capture configuration is being applied')
        completed = self.applying
        completed_token = self.applying_token
        self.active = completed
        self.applying = None
        self.applying_token = None
        self.accepted_token = completed_token
        self.status = (ApplyStatus.ACTIVE
                       if self.requested == self.active
                       else ApplyStatus.REQUESTED)
        self.error = ''
        self._advance_transition()
        return True

    def fail(self, error: str, token: int | None = None) -> bool:
        if token is not None and (
                self.applying is None or self.applying_token != token):
            return False
        self.applying = None
        self.applying_token = None
        self.accepted_token = None
        self.status = ApplyStatus.FAILED
        self.error = str(error)
        self._advance_transition()
        return True

    def cancel_apply(self) -> None:
        had_applying = self.applying is not None
        self.applying = None
        self.applying_token = None
        self.accepted_token = None
        self._advance_transition()
        if had_applying and self.status is ApplyStatus.APPLYING:
            self.status = ApplyStatus.REQUESTED

    def deactivate(self, error: str) -> None:
        """Record that no running engine owns the previous active snapshot."""
        self.cancel_apply()
        self.active = None
        self.status = ApplyStatus.FAILED
        self.error = str(error)
        self._advance_transition()
