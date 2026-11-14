"""Versioned metadata contracts; registration does not imply file ownership."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


class UnsupportedSchemaVersion(OSError):
    """Stored metadata is newer than this application can safely modify."""


class BindingState(str, Enum):
    UNVERIFIED = 'UNVERIFIED'
    LEGACY_UNVERIFIED = 'LEGACY_UNVERIFIED'
    CONFLICT = 'CONFLICT'
    STAT_BOUND = 'STAT_BOUND'
    CONTENT_VERIFIED = 'CONTENT_VERIFIED'
    MISSING = 'MISSING'
    DETACHED = 'DETACHED'
    TRASHED = 'TRASHED'
    PURGING = 'PURGING'


class TrashStatus(str, Enum):
    TRASHED = 'TRASHED'
    RESTORED = 'RESTORED'
    PURGING = 'PURGING'
    PURGED = 'PURGED'


@dataclass(frozen=True)
class TrashItem:
    trash_id: str
    clip_id: str
    original_path: str
    stored_path: str
    created_utc: str
    status: TrashStatus
    error: str | None = None


class FileRole(str, Enum):
    PRIMARY = 'PRIMARY'
    DERIVED = 'DERIVED'
    SIDECAR = 'SIDECAR'


class FileOperationState(str, Enum):
    PREPARED = 'PREPARED'
    APPLYING = 'APPLYING'
    ROLLING_BACK = 'ROLLING_BACK'
    ROLLED_BACK = 'ROLLED_BACK'
    COMMITTED = 'COMMITTED'
    RECOVERY_REQUIRED = 'RECOVERY_REQUIRED'


@dataclass(frozen=True)
class RenameOperation:
    op_id: str
    version: int
    kind: str
    clip_id: str
    state: FileOperationState
    plan: dict
    error: str | None = None
    cleanup_done: bool = False

    def __post_init__(self):
        object.__setattr__(self, 'plan', deepcopy(self.plan))


@dataclass(frozen=True)
class MutationResult:
    op_id: str
    status: FileOperationState
    old_path: str
    new_path: str
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == FileOperationState.COMMITTED


@dataclass(frozen=True)
class SchemaState:
    version: int
    kind: str
    writable: bool


@dataclass(frozen=True)
class MigrationResult:
    from_version: int
    to_version: int
    changed: bool
    migrated_entries: int = 0
    backup_path: Path | None = None


@dataclass(frozen=True)
class FileEvidence:
    path: str
    stat: Mapping[str, int]
    content_sha256: str | None = None

    def __post_init__(self):
        object.__setattr__(self, 'stat', MappingProxyType(dict(self.stat)))


@dataclass(frozen=True)
class ClipBinding:
    clip_id: str | None
    file_id: str | None
    path: str | None
    state: BindingState


@dataclass(frozen=True)
class ClipFile:
    file_id: str
    clip_id: str
    role: FileRole
    path: str
    path_key: str
    binding_state: BindingState
    identity: Mapping[str, int] | None = None
    content_sha256: str | None = None
    provenance: str | None = None

    def __post_init__(self):
        if self.identity is not None:
            object.__setattr__(self, 'identity', MappingProxyType(dict(self.identity)))


@dataclass(frozen=True)
class EditorProject:
    """An editor DTO, separate from library tags and from project persistence."""
    version: int = 2
    sources: tuple[str, ...] = ()
    state: dict = field(default_factory=dict)
    unknown_fields: dict = field(default_factory=dict)

    def __post_init__(self):
        if type(self.version) is not int or self.version != 2:
            raise ValueError('unsupported editor project version')
        if (not isinstance(self.sources, (list, tuple))
                or any(not isinstance(source, str) or not source for source in self.sources)
                or not isinstance(self.state, dict) or not isinstance(self.unknown_fields, dict)):
            raise ValueError('invalid editor project fields')
        object.__setattr__(self, 'sources', tuple(self.sources))
        object.__setattr__(self, 'state', deepcopy(self.state))
        object.__setattr__(self, 'unknown_fields', deepcopy(self.unknown_fields))

    def to_dict(self) -> dict:
        result = deepcopy(self.unknown_fields)
        result.update(version=2, sources=list(self.sources), state=deepcopy(self.state))
        return result

    @classmethod
    def from_dict(cls, value: dict) -> EditorProject:
        if not isinstance(value, dict):
            raise ValueError('editor project must be an object')
        return cls(version=value.get('version'), sources=value.get('sources', ()),
                   state=value.get('state', {}),
                   unknown_fields={key: item for key, item in value.items()
                                   if key not in {'version', 'sources', 'state'}})

    @classmethod
    def from_legacy_draft(cls, draft: dict, *, file_id: str | None = None) -> EditorProject:
        if (not isinstance(draft, dict) or type(draft.get('version')) is not int
                or draft['version'] != 1 or not isinstance(draft.get('state'), dict)):
            raise ValueError('unsupported legacy editor draft')
        return cls(sources=(file_id,) if file_id is not None else (),
                   state=draft['state'], unknown_fields={'legacy_draft': deepcopy(draft)})

    def to_legacy_draft(self) -> dict:
        original = self.unknown_fields.get('legacy_draft')
        if not isinstance(original, dict):
            raise ValueError('project was not adapted from a legacy draft')
        result = deepcopy(original)
        result['state'] = deepcopy(self.state)
        return result
