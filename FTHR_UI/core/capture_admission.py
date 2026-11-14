"""Strict, immutable capture admission policy contracts.

The policy is deliberately independent of Qt, process enumeration and the
capture engine.  It records only lexical Windows executable identities.  The
caller remains responsible for proving that a live window/process identity is
complete and current before applying an allowed decision.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json

from core.game_profiles import ProfileDataError
from core.game_profiles import ProfileValidationError
from core.game_profiles import canonical_exe_path


STORAGE_KEY = 'capture_admission_policy'
SCHEMA_VERSION = 1
MAX_EXECUTABLES = 128
MAX_DOCUMENT_BYTES = 1024 * 1024

_POLICY_KEYS = frozenset({
    'version', 'revision', 'enabled', 'executable_paths',
})
_SOURCE_MODES = frozenset({'window', 'desktop', 'unknown'})


class AdmissionDataError(ValueError):
    """A persisted policy is unavailable because its schema is invalid."""


class AdmissionValidationError(AdmissionDataError):
    """A proposed policy or admission value contains invalid data."""


def _is_int(value: object) -> bool:
    return type(value) is int


def _require_mapping(value: object, name: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise AdmissionDataError(f'{name} must be an object')
    if any(not isinstance(key, str) for key in value):
        raise AdmissionDataError(f'{name} keys must be strings')
    return value


def _validate_revision(value: object, name: str = 'policy revision') -> int:
    if not _is_int(value) or value < 0:
        raise AdmissionValidationError(
            f'{name} must be a non-negative integer')
    return value


def _canonical(value: object, *, error_type=AdmissionValidationError) -> str:
    try:
        return canonical_exe_path(value)
    except (ProfileDataError, ProfileValidationError, TypeError, ValueError) as exc:
        raise error_type('exe_path must be a canonical Windows .exe path') from exc


def _normalize_paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise AdmissionValidationError('executable_paths must be a list')
    if len(value) > MAX_EXECUTABLES:
        raise AdmissionValidationError(
            f'executable_paths cannot contain more than {MAX_EXECUTABLES} paths')
    paths: list[str] = []
    seen: set[str] = set()
    for item in value:
        path = _canonical(item)
        if path in seen:
            raise AdmissionValidationError(
                'executable_paths must contain unique canonical paths')
        seen.add(path)
        paths.append(path)
    return tuple(paths)


def _check_document_size(value: Mapping) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    except (TypeError, ValueError) as exc:
        raise AdmissionValidationError(
            'capture admission policy is not JSON serializable') from exc
    if len(encoded.encode('utf-8')) > MAX_DOCUMENT_BYTES:
        raise AdmissionValidationError(
            'capture admission policy exceeds 1 MiB')


def _detach(value):
    if isinstance(value, Mapping):
        return {key: _detach(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_detach(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class AdmissionPolicy:
    """Immutable versioned executable-admission policy."""

    version: int = SCHEMA_VERSION
    revision: int = 0
    enabled: bool = False
    executable_paths: tuple[str, ...] = ()

    __hash__ = None

    def __post_init__(self):
        if not _is_int(self.version) or self.version != SCHEMA_VERSION:
            raise AdmissionDataError(
                'unsupported capture admission policy version')
        revision = _validate_revision(self.revision)
        if not isinstance(self.enabled, bool):
            raise AdmissionValidationError('enabled must be a boolean')
        paths = _normalize_paths(self.executable_paths)
        object.__setattr__(self, 'revision', revision)
        object.__setattr__(self, 'executable_paths', paths)
        _check_document_size(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        document = {
            'version': self.version,
            'revision': self.revision,
            'enabled': self.enabled,
            'executable_paths': list(self.executable_paths),
        }
        _check_document_size(document)
        return document

    def with_enabled(self, enabled: bool) -> 'AdmissionPolicy':
        return with_enabled(self, enabled)

    def add_executable(self, exe_path: str) -> 'AdmissionPolicy':
        return add_executable(self, exe_path)

    def remove_executable(self, exe_path: str) -> 'AdmissionPolicy':
        return remove_executable(self, exe_path)


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """Immutable result of a pure policy admission evaluation."""

    allowed: bool
    code: str
    reason: str
    policy_revision: int
    exe_path: str | None

    __hash__ = None

    def __post_init__(self):
        if not isinstance(self.allowed, bool):
            raise AdmissionValidationError('allowed must be a boolean')
        if not isinstance(self.code, str) or not self.code.strip():
            raise AdmissionValidationError('code must be a non-empty string')
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise AdmissionValidationError('reason must be a non-empty string')
        _validate_revision(self.policy_revision, 'policy_revision')
        if self.exe_path is not None:
            path = _canonical(self.exe_path)
            object.__setattr__(self, 'exe_path', path)

    def to_dict(self) -> dict[str, object]:
        return {
            'allowed': self.allowed,
            'code': self.code,
            'reason': self.reason,
            'policy_revision': self.policy_revision,
            'exe_path': self.exe_path,
        }


def empty_policy() -> AdmissionPolicy:
    return AdmissionPolicy()


def policy_from_dict(value: object) -> AdmissionPolicy:
    mapping = _require_mapping(value, 'capture admission policy')
    if set(mapping) != _POLICY_KEYS:
        raise AdmissionDataError(
            'capture admission policy has unknown or missing fields')
    if not _is_int(mapping['version']) or mapping['version'] != SCHEMA_VERSION:
        raise AdmissionDataError(
            'unsupported capture admission policy version')
    if type(mapping['executable_paths']) is not list:
        raise AdmissionValidationError('executable_paths must be a JSON list')
    policy = AdmissionPolicy(
        version=mapping['version'],
        revision=mapping['revision'],
        enabled=mapping['enabled'],
        executable_paths=mapping['executable_paths'],
    )
    # Validate the persisted representation itself as well as the normalized
    # form.  A caller must not bypass the 1 MiB contract with a large raw
    # string that happens to normalize to a shorter path.
    _check_document_size(_detach(mapping))
    return policy


def policy_from_raw(value: object) -> AdmissionPolicy:
    return policy_from_dict(value)


def _coerce_policy(value: AdmissionPolicy | Mapping) -> AdmissionPolicy:
    if isinstance(value, AdmissionPolicy):
        return value
    return policy_from_dict(value)


def with_enabled(policy: AdmissionPolicy | Mapping,
                 enabled: bool) -> AdmissionPolicy:
    current = _coerce_policy(policy)
    if not isinstance(enabled, bool):
        raise AdmissionValidationError('enabled must be a boolean')
    if enabled == current.enabled:
        return current
    return AdmissionPolicy(
        current.version, current.revision + 1, enabled,
        current.executable_paths)


def add_executable(policy: AdmissionPolicy | Mapping,
                   exe_path: str) -> AdmissionPolicy:
    current = _coerce_policy(policy)
    canonical = _canonical(exe_path)
    if canonical in current.executable_paths:
        return current
    if len(current.executable_paths) >= MAX_EXECUTABLES:
        raise AdmissionValidationError(
            f'executable_paths cannot contain more than {MAX_EXECUTABLES} paths')
    return AdmissionPolicy(
        current.version, current.revision + 1, current.enabled,
        current.executable_paths + (canonical,))


def remove_executable(policy: AdmissionPolicy | Mapping,
                      exe_path: str) -> AdmissionPolicy:
    current = _coerce_policy(policy)
    canonical = _canonical(exe_path)
    if canonical not in current.executable_paths:
        return current
    return AdmissionPolicy(
        current.version, current.revision + 1, current.enabled,
        tuple(path for path in current.executable_paths if path != canonical))


def validate_policy_transition(current: AdmissionPolicy | Mapping,
                               candidate: AdmissionPolicy | Mapping) -> None:
    current_policy = _coerce_policy(current)
    candidate_policy = _coerce_policy(candidate)
    if candidate_policy.to_dict() == current_policy.to_dict():
        return
    if candidate_policy.revision != current_policy.revision + 1:
        raise AdmissionValidationError(
            'policy revision must advance exactly by one')
    current_semantics = current_policy.to_dict()
    candidate_semantics = candidate_policy.to_dict()
    current_semantics.pop('revision')
    candidate_semantics.pop('revision')
    if current_semantics == candidate_semantics:
        raise AdmissionValidationError(
            'policy revision cannot change without a semantic change')


def _decision(allowed: bool, code: str, reason: str,
              revision: int, exe_path: str | None = None) -> AdmissionDecision:
    return AdmissionDecision(allowed, code, reason, revision, exe_path)


def evaluate_admission(policy: AdmissionPolicy | Mapping,
                       source_mode: str,
                       exe_path: object,
                       identity_valid: bool) -> AdmissionDecision:
    """Evaluate policy-only admission without host/process lookups.

    ``identity_valid`` is supplied by the caller after it has checked the
    complete live window/process identity.  This function never performs that
    check itself.
    """
    try:
        current = _coerce_policy(policy)
    except (AdmissionDataError, AdmissionValidationError, TypeError, ValueError):
        return _decision(False, 'policy-unavailable',
                         'Capture admission policy is unavailable.', 0)

    revision = current.revision
    if not isinstance(source_mode, str) or source_mode not in _SOURCE_MODES:
        return _decision(False, 'invalid-source-mode',
                         'The capture source mode is invalid.', revision)
    if type(identity_valid) is not bool:
        return _decision(False, 'identity-incomplete',
                         'The live capture identity is incomplete.', revision)

    if not current.enabled:
        canonical = None
        try:
            canonical = _canonical(exe_path)
        except AdmissionValidationError:
            # A disabled policy does not inspect executable identity.  The
            # caller's existing source/health guards remain authoritative.
            pass
        return _decision(True, 'policy-disabled',
                         'Capture admission is disabled; the legacy source gates apply.',
                         revision, canonical)
    if source_mode == 'unknown':
        return _decision(False, 'source-unavailable',
                         'The capture source is unavailable.', revision)
    if not identity_valid:
        return _decision(False, 'identity-incomplete',
                         'The live capture identity is incomplete.', revision)
    try:
        canonical = _canonical(exe_path)
    except AdmissionValidationError:
        return _decision(False, 'invalid-executable',
                         'The capture executable identity is invalid.', revision)
    if source_mode != 'window':
        return _decision(False, 'desktop-not-approved',
                         'Only approved game windows may be captured.',
                         revision, canonical)
    if canonical not in current.executable_paths:
        return _decision(False, 'source-not-approved',
                         'This executable is not approved for capture.',
                         revision, canonical)
    return _decision(True, 'admitted',
                     'The executable is approved for capture.', revision, canonical)


__all__ = [
    'AdmissionDataError', 'AdmissionDecision', 'AdmissionPolicy',
    'AdmissionValidationError', 'MAX_DOCUMENT_BYTES', 'MAX_EXECUTABLES',
    'SCHEMA_VERSION', 'STORAGE_KEY', 'add_executable', 'empty_policy',
    'evaluate_admission', 'policy_from_dict', 'policy_from_raw',
    'remove_executable', 'validate_policy_transition', 'with_enabled',
]
