"""Immutable capture request and accepted-session value contracts.

The value objects in this module intentionally contain no live manager,
process, QObject, device, or filesystem references.  They are pure snapshots
that can be carried across asynchronous capture and save boundaries.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import math
import re
from types import MappingProxyType

from core.camera_overlay import clamp_overlay_rect
from core.camera_overlay import DEFAULT_OVERLAY_RECT
from core.camera_overlay import DEFAULT_IMAGE_OVERLAY_RECT
from core.capture_settings import CaptureConfig
from core.capture_settings import MAX_CLIP_SECONDS
from core.capture_settings import validate_fps
from core.capture_settings import validate_normal_clip_length
from core.replay_storage import ReplayStorageSettings
from core.game_profiles import ProfileValidationError
from core.game_profiles import canonical_exe_path
from core.running_apps import encode_application_audio_exclusions


_CONTROL_CHARS = re.compile(r'[\x00-\x1f\x7f]')
_LINE_SEPARATORS = '\x85\u2028\u2029'
_CODECS = {'auto', 'h264', 'hevc', 'av1'}
_ENCODERS = {'auto', 'nvenc', 'amf', 'qsv', 'software'}
_SCALING = {'stretch', 'fit'}
_PURPOSES = {'clips', 'recording'}
_EFFECT_KEYS = {
    'capture_card_enabled',
    'image_overlay_enabled',
    'image_overlay_path',
    'image_overlay_opacity',
    'image_overlay_fit',
    'image_overlay_rect',
    'image_overlays',
    'keyboard_overlay_enabled',
    'keyboard_overlay_rect',
    'third_party_keyboard',
    'watermark_enabled',
}
_IDENTITY_KEYS = {
    'hwnd', 'pid', 'exe_path', 'class_name', 'process_created',
}
_SOURCE_MODES = {'desktop', 'window'}
_AUTHORITIES = {'manual', 'automatic'}
_EFFECT_BOOL_KEYS = {
    'capture_card_enabled', 'image_overlay_enabled',
    'keyboard_overlay_enabled', 'watermark_enabled',
}
_EFFECT_FIT_KEYS = {'image_overlay_fit'}
_IMAGE_LAYER_KEYS = {'path', 'enabled', 'opacity', 'fit', 'rect'}
_KEYBOARD_KEYS = {
    'enabled', 'hwnd', 'window_name', 'color', 'intensity', 'rect',
}
_COLOR_RE = re.compile(r'^#[0-9a-fA-F]{6}$')
_TRACK_NAME_RE = re.compile(r"^[A-Za-z0-9 ._+()'\-]{1,40}$")
_DEFAULT_KEYBOARD_RECT = {
    'x': 0.30, 'y': 0.70, 'w': 0.40, 'h': 0.25,
}


class CaptureSnapshotError(ValueError):
    """A capture request/session value is malformed or unsafe to snapshot."""


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        # Values outside the finite numeric snapshot contract fail closed.
        return False


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item)
                                 for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, float) and not math.isfinite(value):
            raise CaptureSnapshotError('non-finite snapshot value')
        return value
    raise CaptureSnapshotError(
        f'unsupported snapshot value type: {type(value).__name__}')


def _detach(value):
    if isinstance(value, Mapping):
        return {key: _detach(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_detach(item) for item in value]
    if isinstance(value, tuple):
        return [_detach(item) for item in value]
    return value


def _text(value: object, name: str, *, optional: bool = False,
          maximum: int = 4096) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise CaptureSnapshotError(f'{name} must be a bounded non-empty string')
    if (_CONTROL_CHARS.search(value)
            or any(char in value for char in _LINE_SEPARATORS)):
        raise CaptureSnapshotError(f'{name} contains control characters')
    return value


def _endpoint(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise CaptureSnapshotError(f'{name} must be a bounded string')
    if (value != value.strip() or _CONTROL_CHARS.search(value)
            or any(char in value for char in _LINE_SEPARATORS)
            or any(char in ';|' for char in value)):
        raise CaptureSnapshotError(f'{name} contains unsafe ABI characters')
    return value


def _require_string_keys(value: Mapping, name: str) -> None:
    if any(not isinstance(key, str) for key in value):
        raise CaptureSnapshotError(f'{name} keys must be strings')


def _rect(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise CaptureSnapshotError('camera rectangle must contain x, y, w and h')
    _require_string_keys(value, 'camera rectangle')
    if set(value) != {'x', 'y', 'w', 'h'}:
        raise CaptureSnapshotError('camera rectangle must contain x, y, w and h')
    values = {}
    for key in ('x', 'y', 'w', 'h'):
        if not _is_number(value[key]):
            raise CaptureSnapshotError(f'camera rectangle {key} is invalid')
        values[key] = float(value[key])
    return clamp_overlay_rect(values)


def _encoded_audio_tracks(value: object) -> str:
    if not isinstance(value, str):
        raise CaptureSnapshotError('audio_device_tracks must be an encoded string')
    if not value:
        return value
    entries = value.split('|')
    if len(entries) > 4:
        raise CaptureSnapshotError('audio_device_tracks has too many entries')
    seen: set[str] = set()
    for entry in entries:
        if entry.count(';') != 1:
            raise CaptureSnapshotError('audio_device_tracks has invalid ABI separators')
        name, endpoint = entry.split(';')
        if (not _TRACK_NAME_RE.fullmatch(name)
                or name != name.strip()):
            raise CaptureSnapshotError('audio track name is invalid')
        if (not endpoint or endpoint != endpoint.strip()
                or len(endpoint) > 2048
                or _CONTROL_CHARS.search(endpoint)
                or any(char in endpoint for char in _LINE_SEPARATORS)
                or any(char in ';|' for char in endpoint)):
            raise CaptureSnapshotError('audio track endpoint is invalid')
        if endpoint in seen:
            raise CaptureSnapshotError('audio track endpoints must be unique')
        seen.add(endpoint)
    return value


def _identity(value: object) -> MappingProxyType | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise CaptureSnapshotError('live_identity must be a mapping or null')
    _require_string_keys(value, 'live_identity')
    unknown = set(value) - _IDENTITY_KEYS
    if unknown:
        raise CaptureSnapshotError(
            f'unknown source identity fields: {sorted(unknown)}')
    result = {}
    for key, item in value.items():
        if key in ('hwnd', 'pid'):
            if not _is_int(item) or item < 0:
                raise CaptureSnapshotError(f'{key} must be a non-negative integer')
            result[key] = item
        elif key == 'exe_path':
            result[key] = canonical_exe_path(item)
        elif key == 'process_created':
            if item is not None and not (
                    (_is_number(item))
                    or (isinstance(item, str) and len(item) <= 4096
                        and not _CONTROL_CHARS.search(item))):
                raise CaptureSnapshotError('process_created has invalid type')
            result[key] = item
        else:
            result[key] = _text(item, key)
    return _freeze(result)


@dataclass(frozen=True)
class SourceSnapshot:
    """Detached source identity used by one capture request."""

    source_mode: str
    exe_path: str | None = None
    live_identity: Mapping | None = None
    authority: str = 'manual'
    display_name: str = ''
    clip_group: str = ''
    monitor_identity: str = ''

    def __post_init__(self):
        if (not isinstance(self.source_mode, str)
                or self.source_mode not in _SOURCE_MODES):
            raise CaptureSnapshotError('source_mode must be desktop or window')
        if (not isinstance(self.authority, str)
                or self.authority not in _AUTHORITIES):
            raise CaptureSnapshotError('authority must be manual or automatic')
        object.__setattr__(self, 'source_mode', self.source_mode)
        if self.exe_path is not None:
            object.__setattr__(self, 'exe_path', canonical_exe_path(self.exe_path))
        object.__setattr__(self, 'live_identity', _identity(self.live_identity))
        object.__setattr__(self, 'authority', self.authority)
        for field_name in ('display_name', 'clip_group', 'monitor_identity'):
            value = getattr(self, field_name)
            if value is None:
                value = ''
            if value == '':
                normalized = ''
            else:
                normalized = _text(value, field_name, maximum=4096)
            object.__setattr__(self, field_name, normalized)

    def to_dict(self) -> dict[str, object]:
        return {
            'source_mode': self.source_mode,
            'exe_path': self.exe_path,
            'live_identity': _detach(self.live_identity),
            'authority': self.authority,
            'display_name': self.display_name,
            'clip_group': self.clip_group,
            'monitor_identity': self.monitor_identity,
        }


@dataclass(frozen=True)
class CameraSnapshot:
    enabled: bool = False
    device_index: int = 0
    overlay_rect: Mapping = field(
        default_factory=lambda: dict(DEFAULT_OVERLAY_RECT))

    def __post_init__(self):
        if not isinstance(self.enabled, bool):
            raise CaptureSnapshotError('camera enabled must be boolean')
        if not _is_int(self.device_index) or not 0 <= self.device_index <= 255:
            raise CaptureSnapshotError('camera device index must be 0..255')
        object.__setattr__(self, 'overlay_rect', _freeze(_rect(self.overlay_rect)))

    def to_dict(self) -> dict[str, object]:
        return {
            'enabled': self.enabled,
            'device_index': self.device_index,
            'overlay_rect': _detach(self.overlay_rect),
        }


@dataclass(frozen=True)
class ProfileReference:
    profile_id: str | None = None
    profile_revision: int = 0
    profile_name: str | None = None
    profile_document_revision: int = 0

    def __post_init__(self):
        if self.profile_id is None:
            if (not _is_int(self.profile_revision)
                    or self.profile_revision != 0
                    or self.profile_name is not None):
                raise CaptureSnapshotError(
                    'an absent profile must have zero revision and no name')
        else:
            try:
                from core.game_profiles import _validate_uuid
                _validate_uuid(self.profile_id)
            except (ProfileValidationError, ImportError) as exc:
                raise CaptureSnapshotError('profile_id is invalid') from exc
            if not _is_int(self.profile_revision) or self.profile_revision < 1:
                raise CaptureSnapshotError('profile revision must be positive')
            if self.profile_name is None:
                raise CaptureSnapshotError('profile name is required')
            _text(self.profile_name, 'profile_name', maximum=80)
        if (not _is_int(self.profile_document_revision)
                or self.profile_document_revision < 0):
            raise CaptureSnapshotError('profile document revision is invalid')

    @property
    def revision(self):
        return self.profile_revision

    @property
    def name(self):
        return self.profile_name

    @property
    def document_revision(self):
        return self.profile_document_revision


def _validate_capture_config(config: object) -> CaptureConfig:
    if not isinstance(config, CaptureConfig):
        raise CaptureSnapshotError('capture_config must be a CaptureConfig')
    try:
        fps = validate_fps(config.fps)
        buffer_seconds = config.buffer_seconds
        if not _is_int(buffer_seconds) or not 0 <= buffer_seconds <= MAX_CLIP_SECONDS:
            raise ValueError('buffer_seconds is outside the native bound')
        width, height = config.width, config.height
        if (not _is_int(width) or not _is_int(height)
                or width > 16384 or height > 16384):
            raise ValueError('capture dimensions are invalid')
        if not ((width == 0 and height == 0)
                or (width > 0 and height > 0)):
            raise ValueError('capture dimensions are invalid')
        bitrate = config.bitrate_kbps
        if not _is_int(bitrate) or not 500 <= bitrate <= 200_000:
            raise ValueError('bitrate is outside the encoder bound')
        codec = _text(config.codec, 'codec')
        if codec not in _CODECS:
            raise ValueError('codec is invalid')
        if not _is_int(config.preset) or not 1 <= config.preset <= 7:
            raise ValueError('encoder preset is invalid')
        monitor = config.monitor
        if (not isinstance(monitor, str) or len(monitor) > 4096
                or _CONTROL_CHARS.search(monitor)
                or any(char in monitor for char in _LINE_SEPARATORS)):
            raise ValueError('monitor is invalid')
        scaling = _text(config.scaling, 'scaling')
        if scaling not in _SCALING:
            raise ValueError('scaling is invalid')
        if config.capture_backend not in ('auto', 'dxgi', 'wgc'):
            raise ValueError('capture_backend is invalid')
        for field_name in ('audio_enabled', 'multiband_enabled',
                           'separate_audio_enabled', 'microphone_mono',
                           'application_audio_tracks_enabled', 'crop_enabled',
                           'keep_system_audio_backup',
                           'allow_replay_gaps', 'allow_low_fps_clips',
                           'capture_cursor'):
            if not isinstance(getattr(config, field_name), bool):
                raise ValueError(f'{field_name} must be boolean')
        for field_name in ('audio_device_tracks', 'encoder'):
            if not isinstance(getattr(config, field_name), str):
                raise ValueError(f'{field_name} must be a string')
        audio_device_tracks = _encoded_audio_tracks(config.audio_device_tracks)
        exclusions = config.application_audio_exclusions
        if (not isinstance(exclusions, str)
                or exclusions != encode_application_audio_exclusions(exclusions.split('|'))):
            raise ValueError('application_audio_exclusions must contain bounded executable identities')
        microphone_endpoint_id = _endpoint(
            config.microphone_endpoint_id, 'microphone_endpoint_id')
        playback_endpoint_id = _endpoint(
            config.playback_endpoint_id, 'playback_endpoint_id')
        encoder = _text(config.encoder, 'encoder')
        if encoder not in _ENCODERS:
            raise ValueError('encoder is invalid')
        normal_seconds = validate_normal_clip_length(config.normal_clip_seconds)
        crop_values = (config.crop_x, config.crop_y, config.crop_w, config.crop_h)
        if not all(_is_number(value) for value in crop_values):
            raise ValueError('crop values must be finite numbers')
        if any(value < 0 or value > 1 for value in crop_values):
            raise ValueError('crop values must be normalized')
        if config.crop_enabled and (
                config.crop_w <= 0 or config.crop_h <= 0
                or config.crop_x + config.crop_w > 1
                or config.crop_y + config.crop_h > 1):
            raise ValueError('crop rectangle exceeds the frame')
        replay = config.replay_storage
        if (not isinstance(replay, ReplayStorageSettings)
                or type(replay.memory_mib) is not int
                or type(replay.disk_mib) is not int
                or replay != ReplayStorageSettings.from_value(replay.to_dict())):
            raise ValueError('replay storage settings are invalid')
    except (TypeError, ValueError) as exc:
        raise CaptureSnapshotError(f'invalid CaptureConfig: {exc}') from exc
    return CaptureConfig(
        fps=fps, buffer_seconds=buffer_seconds, width=width, height=height,
        bitrate_kbps=bitrate, codec=codec, preset=config.preset,
        monitor=monitor, scaling=scaling, audio_enabled=config.audio_enabled,
        encoder=encoder, multiband_enabled=config.multiband_enabled,
        separate_audio_enabled=config.separate_audio_enabled,
        microphone_endpoint_id=microphone_endpoint_id,
        playback_endpoint_id=playback_endpoint_id,
        microphone_mono=config.microphone_mono,
        audio_device_tracks=audio_device_tracks,
        application_audio_tracks_enabled=config.application_audio_tracks_enabled,
        application_audio_exclusions=exclusions,
        keep_system_audio_backup=config.keep_system_audio_backup,
        normal_clip_seconds=normal_seconds, crop_enabled=config.crop_enabled,
        crop_x=float(config.crop_x), crop_y=float(config.crop_y),
        crop_w=float(config.crop_w), crop_h=float(config.crop_h),
        replay_storage=replay,
        allow_replay_gaps=config.allow_replay_gaps,
        allow_low_fps_clips=config.allow_low_fps_clips,
        capture_cursor=config.capture_cursor,
        capture_backend=config.capture_backend,
    )


def _effects(value: object) -> MappingProxyType:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise CaptureSnapshotError('postprocess effects must be a mapping')
    _require_string_keys(value, 'postprocess effects')
    unknown = set(value) - _EFFECT_KEYS
    if unknown:
        raise CaptureSnapshotError(
            f'unknown capture postprocess fields: {sorted(unknown)}')
    normalized = {}
    for key, item in value.items():
        if key in _EFFECT_BOOL_KEYS:
            if not isinstance(item, bool):
                raise CaptureSnapshotError(f'{key} must be boolean')
            normalized[key] = item
        elif key == 'image_overlay_path':
            if (not isinstance(item, str) or len(item) > 4096
                    or _CONTROL_CHARS.search(item)
                    or any(char in item for char in _LINE_SEPARATORS)):
                raise CaptureSnapshotError('image_overlay_path is invalid')
            normalized[key] = item
        elif key == 'image_overlay_opacity':
            if not _is_int(item) or not 10 <= item <= 100:
                raise CaptureSnapshotError('image_overlay_opacity must be 10..100')
            normalized[key] = item
        elif key in _EFFECT_FIT_KEYS:
            if not isinstance(item, str) or item not in {'fit', 'fill'}:
                raise CaptureSnapshotError('image_overlay_fit is invalid')
            normalized[key] = item
        elif key in ('image_overlay_rect', 'keyboard_overlay_rect'):
            normalized[key] = _rect(item)
        elif key == 'image_overlays':
            if not isinstance(item, (list, tuple)) or len(item) > 128:
                raise CaptureSnapshotError('image_overlays must contain at most 128 layers')
            layers = []
            for layer in item:
                if not isinstance(layer, Mapping):
                    raise CaptureSnapshotError('image overlay must be a mapping')
                _require_string_keys(layer, 'image overlay')
                if set(layer) - _IMAGE_LAYER_KEYS:
                    raise CaptureSnapshotError('image overlay has unknown fields')
                path = layer.get('path')
                if (not isinstance(path, str) or not path
                        or len(path) > 4096 or _CONTROL_CHARS.search(path)
                        or any(char in path for char in _LINE_SEPARATORS)):
                    raise CaptureSnapshotError('image overlay path is invalid')
                enabled = layer.get('enabled', True)
                opacity = layer.get('opacity', 100)
                fit = layer.get('fit', 'fit')
                rect = layer.get('rect', DEFAULT_IMAGE_OVERLAY_RECT)
                if not isinstance(enabled, bool):
                    raise CaptureSnapshotError('image overlay enabled must be boolean')
                if not _is_int(opacity) or not 10 <= opacity <= 100:
                    raise CaptureSnapshotError('image overlay opacity must be 10..100')
                if not isinstance(fit, str) or fit not in {'fit', 'fill'}:
                    raise CaptureSnapshotError('image overlay fit is invalid')
                layers.append({
                    'path': path,
                    'enabled': enabled,
                    'opacity': opacity,
                    'fit': fit,
                    'rect': _rect(rect),
                })
            normalized[key] = layers
        elif key == 'third_party_keyboard':
            if not isinstance(item, Mapping) or set(item) - _KEYBOARD_KEYS:
                raise CaptureSnapshotError('third_party_keyboard has unknown fields')
            _require_string_keys(item, 'third_party_keyboard')
            enabled = item.get('enabled', False)
            hwnd = item.get('hwnd', 0)
            window_name = item.get('window_name', '')
            color = item.get('color', '#00ff00')
            intensity = item.get('intensity', 58)
            rect = item.get('rect', _DEFAULT_KEYBOARD_RECT)
            if not isinstance(enabled, bool):
                raise CaptureSnapshotError('third_party_keyboard enabled must be boolean')
            if not _is_int(hwnd) or hwnd < 0:
                raise CaptureSnapshotError('third_party_keyboard hwnd is invalid')
            if (not isinstance(window_name, str) or len(window_name) > 4096
                    or _CONTROL_CHARS.search(window_name)
                    or any(char in window_name for char in _LINE_SEPARATORS)):
                raise CaptureSnapshotError('third_party_keyboard window_name is invalid')
            if not isinstance(color, str) or not _COLOR_RE.fullmatch(color):
                raise CaptureSnapshotError('third_party_keyboard color is invalid')
            if not _is_int(intensity) or not 0 <= intensity <= 100:
                raise CaptureSnapshotError('third_party_keyboard intensity is invalid')
            normalized[key] = {
                'enabled': enabled,
                'hwnd': hwnd,
                'window_name': window_name,
                'color': color,
                'intensity': intensity,
                'rect': _rect(rect),
            }
    return _freeze(normalized)


def _config_dict(config: CaptureConfig) -> dict[str, object]:
    return {
        'fps': config.fps,
        'buffer_seconds': config.buffer_seconds,
        'width': config.width,
        'height': config.height,
        'bitrate_kbps': config.bitrate_kbps,
        'codec': config.codec,
        'preset': config.preset,
        'monitor': config.monitor,
        'scaling': config.scaling,
        'audio_enabled': config.audio_enabled,
        'encoder': config.encoder,
        'multiband_enabled': config.multiband_enabled,
        'separate_audio_enabled': config.separate_audio_enabled,
        'microphone_endpoint_id': config.microphone_endpoint_id,
        'playback_endpoint_id': config.playback_endpoint_id,
        'microphone_mono': config.microphone_mono,
        'audio_device_tracks': config.audio_device_tracks,
        'application_audio_tracks_enabled': config.application_audio_tracks_enabled,
        'application_audio_exclusions': config.application_audio_exclusions,
        'keep_system_audio_backup': config.keep_system_audio_backup,
        'normal_clip_seconds': config.normal_clip_seconds,
        'crop_enabled': config.crop_enabled,
        'crop_x': config.crop_x,
        'crop_y': config.crop_y,
        'crop_w': config.crop_w,
        'crop_h': config.crop_h,
        'replay_storage': config.replay_storage.to_dict(),
        'allow_replay_gaps': config.allow_replay_gaps,
        'allow_low_fps_clips': config.allow_low_fps_clips,
        'capture_cursor': config.capture_cursor,
        'capture_backend': config.capture_backend,
    }


@dataclass(frozen=True)
class ResolvedCaptureRequest:
    """The complete immutable capture input for one future operation."""

    request_token: int
    purpose: str
    source: SourceSnapshot
    capture_config: CaptureConfig
    mic_gain_percent: int = 100
    camera: CameraSnapshot = field(default_factory=CameraSnapshot)
    effects: Mapping = field(default_factory=dict)
    profile_id: str | None = None
    profile_revision: int = 0
    profile_name: str | None = None
    profile_document_revision: int = 0
    source_intent_revision: int = 0

    def __post_init__(self):
        if not _is_int(self.request_token) or self.request_token < 0:
            raise CaptureSnapshotError('request_token must be non-negative')
        if (not isinstance(self.purpose, str)
                or self.purpose not in _PURPOSES):
            raise CaptureSnapshotError('capture purpose must be clips or recording')
        if not isinstance(self.source, SourceSnapshot):
            raise CaptureSnapshotError('source must be a SourceSnapshot')
        object.__setattr__(self, 'capture_config',
                           _validate_capture_config(self.capture_config))
        if not _is_int(self.mic_gain_percent) or not 0 <= self.mic_gain_percent <= 200:
            raise CaptureSnapshotError('microphone gain must be an integer from 0 to 200')
        if not isinstance(self.camera, CameraSnapshot):
            raise CaptureSnapshotError('camera must be a CameraSnapshot')
        object.__setattr__(self, 'effects', _effects(self.effects))
        reference = ProfileReference(
            self.profile_id, self.profile_revision, self.profile_name,
            self.profile_document_revision)
        object.__setattr__(self, 'profile_id', reference.profile_id)
        object.__setattr__(self, 'profile_revision', reference.profile_revision)
        object.__setattr__(self, 'profile_name', reference.profile_name)
        object.__setattr__(self, 'profile_document_revision',
                           reference.profile_document_revision)
        if (not _is_int(self.source_intent_revision)
                or self.source_intent_revision < 0):
            raise CaptureSnapshotError('source intent revision is invalid')

    @property
    def source_snapshot(self):
        return self.source

    @property
    def config(self):
        return self.capture_config

    @property
    def camera_snapshot(self):
        return self.camera

    @property
    def postprocess(self):
        return self.effects

    @property
    def profile(self):
        return ProfileReference(
            self.profile_id, self.profile_revision, self.profile_name,
            self.profile_document_revision)

    @classmethod
    def from_values(cls, **values):
        """Construct with descriptive aliases used by integration adapters."""
        if 'source' not in values and 'source_snapshot' in values:
            values['source'] = values.pop('source_snapshot')
        if 'capture_config' not in values and 'config' in values:
            values['capture_config'] = values.pop('config')
        if 'camera' not in values and 'camera_snapshot' in values:
            values['camera'] = values.pop('camera_snapshot')
        if 'effects' not in values and 'postprocess' in values:
            values['effects'] = values.pop('postprocess')
        return cls(**values)

    def to_dict(self) -> dict[str, object]:
        return {
            'request_token': self.request_token,
            'purpose': self.purpose,
            'profile_id': self.profile_id,
            'profile_revision': self.profile_revision,
            'profile_name': self.profile_name,
            'profile_document_revision': self.profile_document_revision,
            'source_intent_revision': self.source_intent_revision,
            'source': self.source.to_dict(),
            'capture_config': _config_dict(self.capture_config),
            'mic_gain_percent': self.mic_gain_percent,
            'camera': self.camera.to_dict(),
            'effects': _detach(self.effects),
        }


@dataclass(frozen=True)
class CaptureSessionSnapshot:
    """An accepted request bound to process and native generations."""

    request: ResolvedCaptureRequest
    process_generation: int
    native_capture_generation: int
    accepted_config_token: int = field(kw_only=True)
    manual_consent_nonce: str | None = None
    allowlist_revision: int | None = None

    def __post_init__(self):
        if not isinstance(self.request, ResolvedCaptureRequest):
            raise CaptureSnapshotError('request must be a ResolvedCaptureRequest')
        for name in ('process_generation', 'native_capture_generation'):
            value = getattr(self, name)
            if not _is_int(value) or value < 0:
                raise CaptureSnapshotError(f'{name} must be non-negative')
        if (not _is_int(self.accepted_config_token)
                or self.accepted_config_token <= 0):
            raise CaptureSnapshotError(
                'accepted_config_token must be a positive integer')
        if self.manual_consent_nonce is not None:
            _text(self.manual_consent_nonce, 'manual_consent_nonce', maximum=512)
        if (self.allowlist_revision is not None
                and (not _is_int(self.allowlist_revision)
                     or self.allowlist_revision < 0)):
            raise CaptureSnapshotError('allowlist revision is invalid')

    @property
    def resolved_request(self):
        return self.request

    @property
    def source(self):
        return self.request.source

    @property
    def profile(self):
        return self.request.profile

    def to_dict(self) -> dict[str, object]:
        return {
            'request': self.request.to_dict(),
            'process_generation': self.process_generation,
            'native_capture_generation': self.native_capture_generation,
            'accepted_config_token': self.accepted_config_token,
            'manual_consent_nonce': self.manual_consent_nonce,
            'allowlist_revision': self.allowlist_revision,
        }


CaptureRequest = ResolvedCaptureRequest
SessionSnapshot = CaptureSessionSnapshot


def build_resolved_capture_request(*, request_token: int, purpose: str,
                                   source: SourceSnapshot,
                                   capture_config: CaptureConfig,
                                   mic_gain_percent: int = 100,
                                   camera: CameraSnapshot | None = None,
                                   effects: Mapping | None = None,
                                   profile_id: str | None = None,
                                   profile_revision: int = 0,
                                   profile_name: str | None = None,
                                   profile_document_revision: int = 0,
                                   source_intent_revision: int = 0
                                   ) -> ResolvedCaptureRequest:
    return ResolvedCaptureRequest(
        request_token=request_token, purpose=purpose, source=source,
        capture_config=capture_config, mic_gain_percent=mic_gain_percent,
        camera=CameraSnapshot() if camera is None else camera,
        effects={} if effects is None else effects, profile_id=profile_id,
        profile_revision=profile_revision, profile_name=profile_name,
        profile_document_revision=profile_document_revision,
        source_intent_revision=source_intent_revision)


def bind_capture_session(request: ResolvedCaptureRequest, *,
                         process_generation: int,
                         native_capture_generation: int,
                         accepted_config_token: int,
                         manual_consent_nonce: str | None = None,
                         allowlist_revision: int | None = None
                         ) -> CaptureSessionSnapshot:
    return CaptureSessionSnapshot(
        request=request, process_generation=process_generation,
        native_capture_generation=native_capture_generation,
        accepted_config_token=accepted_config_token,
        manual_consent_nonce=manual_consent_nonce,
        allowlist_revision=allowlist_revision)
