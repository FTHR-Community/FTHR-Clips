"""Strict, immutable per-game capture profile contracts.

This module deliberately contains no Qt, process, or filesystem lookup code.
Executable identity is lexical Windows identity; a profile never grants
source authority or approval by itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
import ntpath
import re
from types import MappingProxyType
import uuid

from core.camera_overlay import clamp_overlay_rect
from core.camera_overlay import DEFAULT_OVERLAY_RECT
from core.capture_settings import AUDIO_CAPTURE_MODES
from core.capture_settings import AUDIO_SOURCE_MODES
from core.capture_settings import MAX_AUDIO_DEVICE_TRACKS
from core.capture_settings import normalize_audio_device_tracks
from core.capture_settings import validate_fps
from core.capture_settings import validate_normal_clip_length


STORAGE_KEY = 'game_capture_profiles'
SCHEMA_VERSION = 1
MAX_PROFILES = 128
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_EXE_PATH_LENGTH = 32767
MAX_PROFILE_NAME_LENGTH = 80
MAX_ENDPOINT_ID_LENGTH = 2048
MAX_TRACK_NAME_LENGTH = 80

_GROUP_FIELDS = {
    'clips': {
        'clip_length', 'framerate', 'resolution', 'bitrate_level',
        'custom_bitrate_kbps',
    },
    'recording': {
        'recording_framerate', 'recording_resolution',
        'recording_bitrate_level', 'recording_custom_bitrate_kbps',
    },
    'audio': {
        'audio_capture_enabled', 'audio_capture_mode', 'audio_source_mode',
        'audio_device_tracks', 'per_app_audio_tracks', 'mic_device_id',
        'playback_device_id', 'mic_downmix_mono', 'mic_volume',
    },
    'camera': {
        'camera_enabled', 'camera_device_index', 'camera_overlay_rect',
    },
}
_PROFILE_KEYS = {'id', 'revision', 'name', 'exe_path', 'overrides'}
_RECT_KEYS = {'x', 'y', 'w', 'h'}
_CONTROL_CHARS = re.compile(r'[\x00-\x1f\x7f]')
_SINGLE_LINE_SEPARATORS = '\x85\u2028\u2029'
_RESOLUTIONS = {'source', '480p', '720p', '1080p', '1440p'}
_QUALITY_LEVELS = {'low', 'medium', 'high', 'custom'}


class ProfileDataError(ValueError):
    """A persisted catalog is unavailable because its schema is invalid."""


class ProfileValidationError(ProfileDataError):
    """A proposed catalog or override contains invalid data."""


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _deep_freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _deep_freeze(item)
                                 for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, float) and not math.isfinite(value):
            raise ProfileValidationError('non-finite value is not supported')
        return value
    raise ProfileValidationError(
        f'unsupported value type: {type(value).__name__}')


def _deep_detach(value):
    if isinstance(value, Mapping):
        return {key: _deep_detach(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_deep_detach(item) for item in value]
    if isinstance(value, tuple):
        return [_deep_detach(item) for item in value]
    return value


class ResolvedCaptureSettings(Mapping):
    """Immutable resolved leaves with detached compatibility views."""

    __slots__ = ('_values',)

    def __init__(self, values: Mapping):
        object.__setattr__(self, '_values', _deep_freeze(values))

    def __setattr__(self, name, value):
        raise AttributeError('resolved capture settings are immutable')

    def __delattr__(self, name):
        raise AttributeError('resolved capture settings are immutable')

    def __getitem__(self, key):
        return _deep_detach(self._values[key])

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def get(self, key, default=None):
        if key not in self._values:
            return default
        return _deep_detach(self._values[key])

    def to_dict(self) -> dict[str, object]:
        return _deep_detach(self._values)


def _require_mapping(value: object, name: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise ProfileValidationError(f'{name} must be an object')
    if any(not isinstance(key, str) for key in value):
        raise ProfileValidationError(f'{name} keys must be strings')
    return value


def _validate_text(value: object, name: str, maximum: int,
                   *, trim: bool = False) -> str:
    if not isinstance(value, str):
        raise ProfileValidationError(f'{name} must be a string')
    if (_CONTROL_CHARS.search(value) or '\n' in value or '\r' in value
            or any(char in value for char in _SINGLE_LINE_SEPARATORS)):
        raise ProfileValidationError(f'{name} contains control characters')
    normalized = value.strip() if trim else value
    if not normalized:
        raise ProfileValidationError(f'{name} must not be empty')
    if len(normalized) > maximum:
        raise ProfileValidationError(f'{name} is too long')
    return normalized


def canonical_exe_path(value: object) -> str:
    """Return a canonical ordinary absolute Windows executable path.

    This intentionally never consults the host filesystem.  Drive and UNC
    paths are accepted; device namespaces, ADS syntax, roots, and ambiguous
    Win32 trailing-dot/space components are rejected.
    """
    if not isinstance(value, str):
        raise ProfileValidationError('exe_path must be a string')
    if not value or len(value) > MAX_EXE_PATH_LENGTH:
        raise ProfileValidationError('exe_path length is invalid')
    if _CONTROL_CHARS.search(value):
        raise ProfileValidationError('exe_path contains control characters')
    path = value.replace('/', '\\')
    if any(char in path for char in '<>"?*|'):
        raise ProfileValidationError('exe_path contains forbidden characters')
    folded = path.casefold()
    if folded.startswith(('\\\\?\\', '\\\\.\\', '\\??\\')):
        raise ProfileValidationError('device namespace paths are not allowed')
    colon_positions = [index for index, char in enumerate(path) if char == ':']
    if colon_positions and colon_positions != [1]:
        raise ProfileValidationError('alternate data stream syntax is not allowed')

    drive, tail = ntpath.splitdrive(path)
    if (len(drive) == 2 and drive[1] == ':'
            and ('A' <= drive[0] <= 'Z' or 'a' <= drive[0] <= 'z')):
        if not tail.startswith('\\'):
            raise ProfileValidationError('drive-relative paths are not allowed')
    elif drive.startswith('\\\\'):
        # ntpath treats ``\\server\\share`` as the UNC drive.  A share
        # itself is a root and cannot be an executable location.
        share_parts = drive[2:].split('\\')
        if len(share_parts) != 2 or not all(share_parts):
            raise ProfileValidationError('UNC share root is not an exe path')
        if not tail.startswith('\\'):
            raise ProfileValidationError('UNC share root is not an exe path')
        for component in share_parts:
            if component[-1] in (' ', '.'):
                raise ProfileValidationError(
                    'trailing dot or space components are not allowed')
    else:
        raise ProfileValidationError('exe_path must be a full drive or UNC path')

    for component in tail.split('\\'):
        if component and component not in ('.', '..') \
                and component[-1] in (' ', '.'):
            raise ProfileValidationError(
                'trailing dot or space components are not allowed')

    normalized = ntpath.normpath(path)
    normalized = normalized.replace('/', '\\')
    normalized_drive, normalized_tail = ntpath.splitdrive(normalized)
    if not normalized_tail.startswith('\\'):
        raise ProfileValidationError('exe_path is not absolute')
    filename = normalized_tail.rsplit('\\', 1)[-1]
    if not filename or not filename.casefold().endswith('.exe'):
        raise ProfileValidationError('exe_path must name an .exe file')
    if normalized_drive.startswith('\\\\') and not normalized_tail[1:]:
        raise ProfileValidationError('UNC share root is not an exe path')
    canonical = ntpath.normcase(normalized)
    if len(canonical) > MAX_EXE_PATH_LENGTH:
        raise ProfileValidationError('canonical exe_path is too long')
    return canonical


def _validate_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise ProfileValidationError('profile id must be a canonical UUID')
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ProfileValidationError('profile id must be a canonical UUID') from exc
    canonical = str(parsed)
    if value != canonical:
        raise ProfileValidationError('profile id must be a canonical UUID')
    return canonical


def _validate_revision(value: object, name: str) -> int:
    if not _is_int(value) or value < 0:
        raise ProfileValidationError(f'{name} must be a non-negative integer')
    return value


def _validate_endpoint(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProfileValidationError(f'{name} must be a string or null')
    if value == '':
        return value
    if value != value.strip() or len(value) > MAX_ENDPOINT_ID_LENGTH:
        raise ProfileValidationError(f'{name} has invalid whitespace or length')
    if _CONTROL_CHARS.search(value) or any(char in ';|' for char in value):
        raise ProfileValidationError(f'{name} contains unsafe ABI characters')
    return value


def _validate_rect(value: object) -> dict[str, float]:
    mapping = _require_mapping(value, 'camera_overlay_rect')
    if set(mapping) != _RECT_KEYS:
        raise ProfileValidationError(
            'camera_overlay_rect must contain exactly x, y, w and h')
    converted: dict[str, float] = {}
    for key in _RECT_KEYS:
        item = mapping[key]
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ProfileValidationError(f'camera rectangle {key} must be numeric')
        try:
            number = float(item)
        except (OverflowError, ValueError) as exc:
            raise ProfileValidationError(
                f'camera rectangle {key} must be finite') from exc
        if not math.isfinite(number):
            raise ProfileValidationError(f'camera rectangle {key} must be finite')
        converted[key] = number
    return clamp_overlay_rect(converted)


def _validate_tracks(value: object) -> list[dict[str, str]]:
    if not isinstance(value, (list, tuple)):
        raise ProfileValidationError('audio_device_tracks must be a list')
    if len(value) > MAX_AUDIO_DEVICE_TRACKS:
        raise ProfileValidationError('too many audio device tracks')
    tracks: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        mapping = _require_mapping(item, 'audio device track')
        if set(mapping) != {'id', 'name'}:
            raise ProfileValidationError('audio device tracks have unknown fields')
        endpoint = _validate_endpoint(mapping['id'], 'audio track id')
        if not endpoint:
            raise ProfileValidationError('audio track id must not be empty')
        name = _validate_text(mapping['name'], 'audio track name',
                              MAX_TRACK_NAME_LENGTH, trim=True)
        if endpoint in seen:
            raise ProfileValidationError('audio track ids must be unique')
        seen.add(endpoint)
        tracks.append({'id': endpoint, 'name': name})
    normalized = normalize_audio_device_tracks(tracks)
    if len(normalized) != len(tracks):
        raise ProfileValidationError('audio device tracks could not be normalized')
    return normalized


def _validate_leaf(group: str, key: str, value: object):
    if group in ('clips', 'recording'):
        if key.endswith('clip_length'):
            try:
                return validate_normal_clip_length(value)
            except (TypeError, ValueError) as exc:
                raise ProfileValidationError(str(exc)) from exc
        if key.endswith('framerate'):
            try:
                return validate_fps(value)
            except ValueError as exc:
                raise ProfileValidationError(str(exc)) from exc
        if key.endswith('resolution'):
            if not isinstance(value, str) or value not in _RESOLUTIONS:
                raise ProfileValidationError(f'{key} has an invalid resolution')
            return value
        if key.endswith('bitrate_level'):
            if not isinstance(value, str) or value not in _QUALITY_LEVELS:
                raise ProfileValidationError(f'{key} has an invalid quality level')
            return value
        if key.endswith('custom_bitrate_kbps'):
            if not _is_int(value) or not 500 <= value <= 200_000:
                raise ProfileValidationError(
                    f'{key} must be an integer between 500 and 200000')
            return value
    if group == 'audio':
        if key in ('audio_capture_enabled', 'mic_downmix_mono'):
            if not isinstance(value, bool):
                raise ProfileValidationError(f'{key} must be a boolean')
            return value
        if key == 'audio_capture_mode':
            if not isinstance(value, str) or value not in AUDIO_CAPTURE_MODES:
                raise ProfileValidationError('audio_capture_mode is invalid')
            return value
        if key == 'audio_source_mode':
            if (value is not None
                    and (not isinstance(value, str)
                         or value not in AUDIO_SOURCE_MODES)):
                raise ProfileValidationError('audio_source_mode is invalid')
            return value
        if key == 'audio_device_tracks':
            return _validate_tracks(value)
        if key == 'per_app_audio_tracks':
            if value is not None and not isinstance(value, bool):
                raise ProfileValidationError('per_app_audio_tracks must be boolean or null')
            return value
        if key in ('mic_device_id', 'playback_device_id'):
            return _validate_endpoint(value, key)
        if key == 'mic_volume':
            if not _is_int(value) or not 0 <= value <= 200:
                raise ProfileValidationError('mic_volume must be an integer from 0 to 200')
            return value
    if group == 'camera':
        if key == 'camera_enabled':
            if not isinstance(value, bool):
                raise ProfileValidationError('camera_enabled must be a boolean')
            return value
        if key == 'camera_device_index':
            if not _is_int(value) or not 0 <= value <= 255:
                raise ProfileValidationError(
                    'camera_device_index must be an integer from 0 to 255')
            return value
        if key == 'camera_overlay_rect':
            return _validate_rect(value)
    raise ProfileValidationError(f'unknown profile field {group}.{key}')


def _normalize_overrides(value: object) -> dict[str, dict[str, object]]:
    groups = _require_mapping(value, 'overrides')
    normalized: dict[str, dict[str, object]] = {}
    for group, leaves in groups.items():
        if group not in _GROUP_FIELDS:
            raise ProfileValidationError(f'unknown override group {group!r}')
        leaf_mapping = _require_mapping(leaves, f'overrides.{group}')
        unknown = set(leaf_mapping) - _GROUP_FIELDS[group]
        if unknown:
            raise ProfileValidationError(
                f'unknown override field(s) in {group}: {sorted(unknown)}')
        normalized[group] = {
            key: _validate_leaf(group, key, item)
            for key, item in leaf_mapping.items()
        }
    return normalized


def _normalize_profile_data(value: object) -> dict[str, object]:
    mapping = _require_mapping(value, 'profile')
    if set(mapping) != _PROFILE_KEYS:
        raise ProfileValidationError('profile has unknown or missing fields')
    revision = _validate_revision(mapping['revision'], 'profile revision')
    if revision < 1:
        raise ProfileValidationError('profile revision must be positive')
    return {
        'id': _validate_uuid(mapping['id']),
        'revision': revision,
        'name': _validate_text(mapping['name'], 'profile name',
                               MAX_PROFILE_NAME_LENGTH, trim=True),
        'exe_path': canonical_exe_path(mapping['exe_path']),
        'overrides': _normalize_overrides(mapping['overrides']),
    }


@dataclass(frozen=True)
class GameCaptureProfile:
    """One immutable sparse profile."""

    id: str
    revision: int
    name: str
    exe_path: str
    overrides: Mapping[str, Mapping[str, object]]

    __hash__ = None

    def __post_init__(self):
        normalized = _normalize_profile_data({
            'id': self.id,
            'revision': self.revision,
            'name': self.name,
            'exe_path': self.exe_path,
            'overrides': self.overrides,
        })
        object.__setattr__(self, 'id', normalized['id'])
        object.__setattr__(self, 'revision', normalized['revision'])
        object.__setattr__(self, 'name', normalized['name'])
        object.__setattr__(self, 'exe_path', normalized['exe_path'])
        object.__setattr__(self, 'overrides', _deep_freeze(normalized['overrides']))

    def to_dict(self) -> dict[str, object]:
        return {
            'id': self.id,
            'revision': self.revision,
            'name': self.name,
            'exe_path': self.exe_path,
            'overrides': _deep_detach(self.overrides),
        }

    def get(self, key: str, default=None):
        return self.to_dict().get(key, default)


@dataclass(frozen=True)
class GameProfileCatalog:
    """Immutable versioned profile document."""

    version: int = SCHEMA_VERSION
    revision: int = 0
    profiles: tuple[GameCaptureProfile, ...] = ()

    __hash__ = None

    def __post_init__(self):
        if self.version != SCHEMA_VERSION or not _is_int(self.version):
            raise ProfileDataError('unsupported game profile document version')
        revision = _validate_revision(self.revision, 'catalog revision')
        if not isinstance(self.profiles, (list, tuple)):
            raise ProfileValidationError('profiles must be a list')
        if len(self.profiles) > MAX_PROFILES:
            raise ProfileValidationError('too many game profiles')
        normalized = tuple(
            GameCaptureProfile(**_normalize_profile_data(
                item.to_dict() if isinstance(item, GameCaptureProfile) else item))
            for item in self.profiles)
        ids = [profile.id for profile in normalized]
        if len(ids) != len(set(ids)):
            raise ProfileValidationError('profile ids must be unique')
        paths = [profile.exe_path for profile in normalized]
        if len(paths) != len(set(paths)):
            raise ProfileValidationError('profile executable paths must be unique')
        object.__setattr__(self, 'revision', revision)
        object.__setattr__(self, 'profiles', normalized)
        _check_document_size(self.to_dict())

    @classmethod
    def from_dict(cls, value: object) -> 'GameProfileCatalog':
        mapping = _require_mapping(value, 'game profile document')
        if set(mapping) != {'version', 'revision', 'profiles'}:
            raise ProfileDataError('game profile document has unknown or missing fields')
        catalog = cls(mapping['version'], mapping['revision'], mapping['profiles'])
        _check_document_size(_deep_detach(mapping))
        return catalog

    def to_dict(self) -> dict[str, object]:
        document = {
            'version': self.version,
            'revision': self.revision,
            'profiles': [profile.to_dict() for profile in self.profiles],
        }
        _check_document_size(document)
        return document

    def get(self, key: str, default=None):
        return self.to_dict().get(key, default)

    def __getitem__(self, key: str):
        return self.to_dict()[key]

    def create_profile(self, name: str, exe_path: str,
                       overrides: Mapping | None = None, *,
                       profile_id: str | None = None):
        return create_profile(self, name, exe_path, overrides,
                              profile_id=profile_id)

    def update_profile(self, profile_id: str, **changes):
        return update_profile(self, profile_id, **changes)

    def rename_profile(self, profile_id: str, name: str):
        return rename_profile(self, profile_id, name)

    def delete_profile(self, profile_id: str):
        return delete_profile(self, profile_id)

    def reset_override(self, profile_id: str, group: str,
                       key: str | None = None):
        return reset_override(self, profile_id, group, key)


ProfileCatalog = GameProfileCatalog
GameProfile = GameCaptureProfile


def _check_document_size(value: object) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    except (TypeError, ValueError) as exc:
        raise ProfileValidationError('profile document is not JSON serializable') from exc
    if len(encoded.encode('utf-8')) > MAX_DOCUMENT_BYTES:
        raise ProfileValidationError('profile document exceeds 1 MiB')


def empty_catalog() -> GameProfileCatalog:
    return GameProfileCatalog()


def catalog_from_dict(value: object) -> GameProfileCatalog:
    return GameProfileCatalog.from_dict(value)


def catalog_from_raw(value: object) -> GameProfileCatalog:
    if value is _MISSING:
        return empty_catalog()
    return catalog_from_dict(value)


_MISSING = object()


def catalog_to_dict(value: GameProfileCatalog | Mapping) -> dict[str, object]:
    catalog = value if isinstance(value, GameProfileCatalog) else catalog_from_dict(value)
    return catalog.to_dict()


def _coerce_catalog(value: GameProfileCatalog | Mapping) -> GameProfileCatalog:
    return value if isinstance(value, GameProfileCatalog) else catalog_from_dict(value)


def _next_catalog(profiles, revision: int) -> GameProfileCatalog:
    return GameProfileCatalog(SCHEMA_VERSION, revision, tuple(profiles))


def find_profile(catalog: GameProfileCatalog | Mapping,
                 exe_path: object) -> GameCaptureProfile | None:
    catalog = _coerce_catalog(catalog)
    try:
        canonical = canonical_exe_path(exe_path)
    except ProfileDataError:
        # An invalid live lookup is simply not a profile match; malformed
        # saved catalogs were validated above and still propagate errors.
        return None
    return next((profile for profile in catalog.profiles
                 if profile.exe_path == canonical), None)


def create_profile(catalog: GameProfileCatalog | Mapping, name: str,
                   exe_path: str, overrides: Mapping | None = None,
                   *, profile_id: str | None = None) -> GameProfileCatalog:
    catalog = _coerce_catalog(catalog)
    profile = GameCaptureProfile(
        id=profile_id or str(uuid.uuid4()), revision=1, name=name,
        exe_path=exe_path, overrides={} if overrides is None else overrides)
    if any(existing.id == profile.id for existing in catalog.profiles):
        raise ProfileValidationError('profile id already exists')
    if any(existing.exe_path == profile.exe_path for existing in catalog.profiles):
        raise ProfileValidationError('profile executable path already exists')
    return _next_catalog((*catalog.profiles, profile), catalog.revision + 1)


_UNSET = object()


def update_profile(catalog: GameProfileCatalog | Mapping, profile_id: str,
                   *, name=_UNSET, exe_path=_UNSET,
                   overrides=_UNSET) -> GameProfileCatalog:
    catalog = _coerce_catalog(catalog)
    target_id = _validate_uuid(profile_id)
    current = next((item for item in catalog.profiles if item.id == target_id), None)
    if current is None:
        raise ProfileValidationError('unknown profile id')
    candidate = GameCaptureProfile(
        id=current.id,
        revision=current.revision,
        name=current.name if name is _UNSET else name,
        exe_path=current.exe_path if exe_path is _UNSET else exe_path,
        overrides=current.overrides if overrides is _UNSET else overrides,
    )
    if candidate.to_dict() == current.to_dict():
        return catalog
    candidate = GameCaptureProfile(
        candidate.id, current.revision + 1, candidate.name,
        candidate.exe_path, candidate.overrides)
    if any(item.id != target_id and item.exe_path == candidate.exe_path
           for item in catalog.profiles):
        raise ProfileValidationError('profile executable path already exists')
    return _next_catalog(
        tuple(candidate if item.id == target_id else item
              for item in catalog.profiles), catalog.revision + 1)


def rename_profile(catalog, profile_id: str, name: str) -> GameProfileCatalog:
    return update_profile(catalog, profile_id, name=name)


def delete_profile(catalog: GameProfileCatalog | Mapping,
                   profile_id: str) -> GameProfileCatalog:
    catalog = _coerce_catalog(catalog)
    target_id = _validate_uuid(profile_id)
    if not any(item.id == target_id for item in catalog.profiles):
        raise ProfileValidationError('unknown profile id')
    return _next_catalog(
        tuple(item for item in catalog.profiles if item.id != target_id),
        catalog.revision + 1)


def reset_override(catalog: GameProfileCatalog | Mapping, profile_id: str,
                   group: str, key: str | None = None) -> GameProfileCatalog:
    catalog = _coerce_catalog(catalog)
    target = next((item for item in catalog.profiles
                   if item.id == _validate_uuid(profile_id)), None)
    if target is None:
        raise ProfileValidationError('unknown profile id')
    if group not in _GROUP_FIELDS:
        raise ProfileValidationError(f'unknown override group {group!r}')
    if key is not None and key not in _GROUP_FIELDS[group]:
        raise ProfileValidationError(f'unknown override field {group}.{key}')
    overrides = target.to_dict()['overrides']
    if group not in overrides:
        return catalog
    if key is None:
        overrides.pop(group, None)
    elif key in overrides[group]:
        overrides[group].pop(key)
        if not overrides[group]:
            overrides.pop(group, None)
    else:
        return catalog
    return update_profile(catalog, target.id, overrides=overrides)


def validate_catalog_transition(current: GameProfileCatalog | Mapping,
                                candidate: GameProfileCatalog | Mapping) -> None:
    """Validate revisions for a candidate being persisted."""
    current = _coerce_catalog(current)
    candidate = _coerce_catalog(candidate)
    current_by_id = {profile.id: profile for profile in current.profiles}
    candidate_by_id = {profile.id: profile for profile in candidate.profiles}
    if candidate.to_dict() == current.to_dict():
        return
    if candidate.revision != current.revision + 1:
        raise ProfileValidationError('catalog revision must advance exactly by one')
    profile_semantic_changed = set(current_by_id) != set(candidate_by_id)
    for profile_id, new_profile in candidate_by_id.items():
        old_profile = current_by_id.get(profile_id)
        if old_profile is None:
            if new_profile.revision != 1:
                raise ProfileValidationError('new profiles must start at revision one')
            profile_semantic_changed = True
            continue
        old_without_revision = old_profile.to_dict()
        new_without_revision = new_profile.to_dict()
        old_without_revision.pop('revision')
        new_without_revision.pop('revision')
        if old_without_revision == new_without_revision:
            if new_profile.revision != old_profile.revision:
                raise ProfileValidationError(
                    'unchanged profiles must retain their revision')
        elif new_profile.revision != old_profile.revision + 1:
            raise ProfileValidationError(
                'changed profiles must advance exactly by one')
        else:
            profile_semantic_changed = True
    if not profile_semantic_changed:
        raise ProfileValidationError(
            'catalog revision cannot change without a profile semantic change')


def _setting_get(settings, key: str, default):
    if isinstance(settings, Mapping):
        return settings.get(key, default)
    getter = getattr(settings, 'get', None)
    if not callable(getter):
        raise ProfileValidationError('global settings must provide get()')
    return getter(key, default)


_GLOBAL_DEFAULTS = {
    'clip_length': 30,
    'framerate': 60,
    'resolution': 'source',
    'bitrate_level': 'medium',
    'custom_bitrate_kbps': 25_000,
    'recording_framerate': 60,
    'recording_resolution': 'source',
    'recording_bitrate_level': 'medium',
    'recording_custom_bitrate_kbps': 25_000,
    'audio_capture_enabled': True,
    'audio_capture_mode': 'combined',
    'audio_source_mode': None,
    'audio_device_tracks': [],
    'per_app_audio_tracks': None,
    'mic_device_id': None,
    'playback_device_id': None,
    'mic_downmix_mono': False,
    'mic_volume': 100,
    'camera_enabled': False,
    'camera_device_index': 0,
    'camera_overlay_rect': DEFAULT_OVERLAY_RECT,
}

_KEY_GROUP = {
    key: group for group, keys in _GROUP_FIELDS.items() for key in keys
}


def resolve_profile_settings(profile: GameCaptureProfile | None,
                             global_settings) -> ResolvedCaptureSettings:
    """Resolve one sparse profile against a detached current global snapshot."""
    values: dict[str, object] = {}
    for key, group in _KEY_GROUP.items():
        if (profile is not None and group in profile.overrides
                and key in profile.overrides[group]):
            values[key] = _deep_detach(profile.overrides[group][key])
            continue
        default = _GLOBAL_DEFAULTS[key]
        raw = _setting_get(global_settings, key, default)
        try:
            values[key] = _validate_leaf(group, key, raw)
        except ProfileDataError as exc:
            raise ProfileDataError(f'invalid inherited global {key}: {exc}') from exc
    return ResolvedCaptureSettings(values)


def resolve_capture_settings(catalog: GameProfileCatalog | Mapping,
                              exe_path: object,
                             global_settings) -> ResolvedCaptureSettings:
    profile = find_profile(catalog, exe_path)
    return resolve_profile_settings(profile, global_settings)


def resolve_profile(catalog: GameProfileCatalog | Mapping, exe_path: object,
                    global_settings):
    """Return ``(profile, effective_settings)`` for a future request."""
    catalog = _coerce_catalog(catalog)
    profile = find_profile(catalog, exe_path)
    return profile, resolve_profile_settings(profile, global_settings)
