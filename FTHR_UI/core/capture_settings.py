"""Authoritative capture-setting policy and requested/active state tracking."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


NORMAL_CLIP_VALUES = (5, 10, 15, 30, 45, 60, 90, 120, 180, 240, 300, 600, 900, 1200, 1800)
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


def _validate_int(value: int, *, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f'{name} must be an integer')
    if not minimum <= value <= maximum:
        raise ValueError(f'{name} must be between {minimum} and {maximum}')
    return value


def validate_normal_clip_length(value: int) -> int:
    return _validate_int(value, name='normal clip length', minimum=5, maximum=1800)




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
    normal_clip_seconds: int = 30
    crop_enabled: bool = False
    crop_x: float = 0.0
    crop_y: float = 0.0
    crop_w: float = 1.0
    crop_h: float = 1.0


def compute_buffer_seconds(normal_seconds: int) -> int:
    """Return a native-safe ring size while retaining the usual safety margin."""
    normal = validate_normal_clip_length(normal_seconds)
    return min(1800, normal + 2)


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
