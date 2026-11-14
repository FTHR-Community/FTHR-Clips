"""Bounded, root-scoped storage accounting and cleanup planning.

This module deliberately contains no Qt and no destructive filesystem operation.
It is the data side of the storage settings feature: callers take a metadata
snapshot, run :func:`build_inventory` on a worker, and pass the resulting
identities back through the existing journaled trash route.
"""

from __future__ import annotations

import os
import json
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from core.clip_files import is_library_media_path
from core.audio_manifest import AudioManifestError, manifest_path_for, validate_manifest
from core.library_index import canonical_media_path


MAX_POLICY_BYTES = (1 << 63) - 1
MAX_POLICY_DAYS = 36500
MAX_CANDIDATES = 20
MIN_AGE_SECONDS = 60
POLICY_VERSION = 1
_STAT_FIELDS = ("st_dev", "st_ino", "size", "mtime_ns")
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class StoragePolicyError(ValueError):
    """A policy is malformed and therefore cannot authorize cleanup."""


@dataclass(frozen=True)
class StoragePolicy:
    """Validated policy for one canonical clips root.

    Zero means disabled for numeric limits.  Booleans are deliberately
    validated with ``type(value) is bool`` so JSON integers cannot enable a
    cleanup pass by accident.
    """

    version: int = POLICY_VERSION
    max_library_bytes: int = 0
    automatic_cleanup: bool = False
    trash_max_bytes: int = 0
    trash_retention_days: int = 0
    automatic_trash_cleanup: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping | None, *, strict: bool = True):
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            if strict:
                raise StoragePolicyError("storage policy must be an object")
            return cls()
        version = value.get("version", POLICY_VERSION)
        if type(version) is not int or version != POLICY_VERSION:
            if strict:
                raise StoragePolicyError("unsupported storage policy version")
            return cls()
        defaults = {
            "max_library_bytes": 0,
            "automatic_cleanup": False,
            "trash_max_bytes": 0,
            "trash_retention_days": 0,
            "automatic_trash_cleanup": False,
        }
        values = {key: value.get(key, default)
                  for key, default in defaults.items()}
        for key in ("automatic_cleanup", "automatic_trash_cleanup"):
            if type(values[key]) is not bool:
                if strict:
                    raise StoragePolicyError(f"{key} must be a boolean")
                return cls()
        for key in ("max_library_bytes", "trash_max_bytes", "trash_retention_days"):
            current = values[key]
            maximum = MAX_POLICY_DAYS if key == "trash_retention_days" else MAX_POLICY_BYTES
            if (type(current) is not int or current < 0 or current > maximum):
                if strict:
                    raise StoragePolicyError(f"{key} is outside its supported range")
                return cls()
        return cls(version=version, **values)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "max_library_bytes": self.max_library_bytes,
            "automatic_cleanup": self.automatic_cleanup,
            "trash_max_bytes": self.trash_max_bytes,
            "trash_retention_days": self.trash_retention_days,
            "automatic_trash_cleanup": self.automatic_trash_cleanup,
        }

    @property
    def cleanup_enabled(self) -> bool:
        return bool(self.automatic_cleanup or self.automatic_trash_cleanup)


def canonical_root(path) -> str:
    """Return a lexical comparison form; worker identity checks resolve links."""
    raw = os.path.abspath(os.path.expandvars(os.fspath(path)))
    return os.path.normcase(raw)


def _lstat_identity(path) -> dict | None:
    try:
        info = os.lstat(path)
    except OSError:
        # A vanished or inaccessible path has no safe identity to admit.
        return None
    if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE):
        return None
    if int(getattr(info, "st_dev", 0)) == 0 or int(getattr(info, "st_ino", 0)) == 0:
        return None
    return {
        "st_dev": int(info.st_dev),
        "st_ino": int(info.st_ino),
        "size": int(info.st_size),
        "mtime_ns": int(info.st_mtime_ns),
    }


def root_identity(path) -> dict:
    """Capture stable identity for a regular, non-reparse directory.

    Directory size and mtime are intentionally excluded.  Creating another
    clip changes those values even though ownership of the selected directory
    is unchanged.  The selected directory and its safe, non-reparse
    ancestors are retained by stable device/inode identity so a replacement
    of the path chain cannot silently become the owned root.
    """
    root = os.path.abspath(os.fspath(path))
    selected = _lstat_identity(root)
    if selected is None or not os.path.isdir(root):
        raise OSError(f"Storage root is missing or linked: {root}")
    current = root
    ancestors = []
    while True:
        identity = _lstat_identity(current)
        if identity is None or not os.path.isdir(current):
            raise OSError(f"Storage root is missing or linked: {current}")
        ancestors.append((identity["st_dev"], identity["st_ino"]))
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return {
        "st_dev": selected["st_dev"],
        "st_ino": selected["st_ino"],
        "ancestors": tuple(ancestors),
    }


def root_identities_match(left, right) -> bool:
    """Compare root ownership using stable IDs, never directory size/mtime."""
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        return False
    for key in ("st_dev", "st_ino"):
        if left.get(key) != right.get(key):
            return False
    left_ancestors = left.get("ancestors")
    right_ancestors = right.get("ancestors")
    if left_ancestors is None or right_ancestors is None:
        return left_ancestors is None and right_ancestors is None
    try:
        return tuple(tuple(int(value) for value in item) for item in left_ancestors) == tuple(
            tuple(int(value) for value in item) for item in right_ancestors)
    except (TypeError, ValueError):
        # Malformed persisted ancestor data cannot prove root ownership.
        return False


def policy_for_root(settings, root, *, strict=False) -> StoragePolicy:
    """Read one root policy without mutating malformed settings."""
    raw = settings.get("storage_policies", {}) if settings is not None else {}
    if not isinstance(raw, Mapping):
        return StoragePolicy()
    entry = raw.get(canonical_root(root))
    return StoragePolicy.from_mapping(entry, strict=strict)


def policy_map_with(settings, root, policy: StoragePolicy) -> dict:
    """Return a copy of ``storage_policies`` with one validated root entry."""
    if not isinstance(policy, StoragePolicy):
        raise TypeError("policy must be a StoragePolicy")
    raw = settings.get("storage_policies", {}) if settings is not None else {}
    if not isinstance(raw, Mapping):
        raise StoragePolicyError("stored storage policies are malformed")
    result = dict(raw)
    key = canonical_root(root)
    if key in raw:
        # Do not silently replace a future or malformed entry while saving a
        # different interpretation of the same root.
        StoragePolicy.from_mapping(raw[key], strict=True)
    result[key] = policy.to_dict()
    return result


def _freeze_mapping(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_mapping(item) for key, item in value.items()})
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(_freeze_mapping(item) for item in value)
    return value


@dataclass(frozen=True)
class StorageFile:
    path: str
    identity: Mapping[str, int]
    category: str
    size: int
    mtime_ns: int
    clip_id: str | None = None
    role: str = "PRIMARY"
    protected: bool = False
    favorite: bool = False
    complete: bool = True
    eligible: bool = False
    reason: str | None = None
    nlink: int = 1
    trash_id: str | None = None
    bundle_mtime_ns: int = 0
    bundle_created_utc: str | None = None
    automatic_eligible: bool = False
    bundle_size: int = 0
    bundle_valid: bool = True

    def __post_init__(self):
        identity = dict(self.identity)
        if set(identity) != set(_STAT_FIELDS) or any(type(identity[k]) is not int for k in _STAT_FIELDS):
            raise ValueError("StorageFile identity must contain four integer stat fields")
        object.__setattr__(self, "identity", MappingProxyType(identity))


@dataclass(frozen=True)
class StorageInventory:
    root: str
    root_identity: Mapping[str, object]
    files: tuple[StorageFile, ...] = ()
    active_bytes: int = 0
    trash_bytes: int = 0
    companion_bytes: int = 0
    other_bytes: int = 0
    imported_bytes: int = 0
    cover_bytes: int = 0
    free_bytes: int | None = None
    complete: bool = True
    warnings: tuple[str, ...] = ()
    scanned_at: float = 0.0

    def __post_init__(self):
        object.__setattr__(self, "root_identity", MappingProxyType(dict(self.root_identity)))
        object.__setattr__(self, "files", tuple(self.files))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @property
    def total_bytes(self) -> int:
        return (self.active_bytes + self.trash_bytes + self.companion_bytes
                + self.other_bytes)

    @property
    def accounted_bytes(self) -> int:
        return self.total_bytes


@dataclass(frozen=True)
class StorageCandidate:
    action: str
    file: StorageFile
    estimated_reclaimable_bytes: int = 0
    members: tuple[StorageFile, ...] = ()

    @property
    def path(self):
        return self.file.path

    @property
    def identity(self):
        return self.file.identity

    @property
    def trash_id(self):
        return self.file.trash_id


@dataclass(frozen=True)
class StoragePreview:
    candidates: tuple[StorageCandidate, ...]
    needed_bytes: int
    estimated_reclaimable_bytes: int
    remaining_bytes: int
    complete: bool
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class StorageBindingsSnapshot:
    """Small immutable metadata hint consumed by the inventory worker."""

    files: tuple[Mapping, ...] = ()
    by_path: Mapping = field(default_factory=dict)
    trashed_ids: frozenset[str] = frozenset()

    def __post_init__(self):
        frozen_files = tuple(_freeze_mapping(item) for item in self.files)
        object.__setattr__(self, "files", frozen_files)
        object.__setattr__(self, "by_path", _freeze_mapping(self.by_path))
        object.__setattr__(self, "trashed_ids", frozenset(self.trashed_ids))

    def __getitem__(self, key):
        return {"files": self.files, "by_path": self.by_path,
                "trashed_ids": self.trashed_ids}[key]


def _snapshot_entry(snapshot, path):
    entry = None
    if isinstance(snapshot, StorageBindingsSnapshot):
        entry = snapshot.by_path.get(canonical_root(path)) or snapshot.by_path.get(path)
    elif isinstance(snapshot, Mapping):
        by_path = snapshot.get("by_path", snapshot)
        if isinstance(by_path, Mapping):
            entry = (by_path.get(canonical_root(path))
                     or by_path.get(canonical_media_path(path))
                     or by_path.get(path))
            if (isinstance(entry, Mapping) and "identity" not in entry
                    and "files" not in snapshot):
                entry = dict(entry)
                entry["_storage_hint_only"] = True
    return entry


def _snapshot_files(snapshot):
    """Return the complete registered-file projection from a storage snapshot."""
    if isinstance(snapshot, StorageBindingsSnapshot):
        return snapshot.files
    if not isinstance(snapshot, Mapping):
        return ()
    files = snapshot.get("files")
    if isinstance(files, (list, tuple)):
        return tuple(files)
    by_path = snapshot.get("by_path", snapshot)
    if not isinstance(by_path, Mapping):
        return ()
    result = []
    for path, entry in by_path.items():
        if isinstance(entry, Mapping) and "path" not in entry:
            entry = dict(entry)
            entry["path"] = path
        if (isinstance(entry, Mapping) and "identity" not in entry
                and "files" not in snapshot):
            entry = dict(entry)
            entry["_storage_hint_only"] = True
        result.append(entry)
    return tuple(result)


def _file_key(path, identity, clip_id=None):
    if isinstance(identity, Mapping):
        values = identity
    else:
        values = dict(zip(_STAT_FIELDS, identity, strict=True))
    if (set(values) != set(_STAT_FIELDS)
            or any(type(values[field]) is not int for field in _STAT_FIELDS)):
        raise ValueError("file identity requires four integer stat fields")
    return (str(path), tuple(int(values[field]) for field in _STAT_FIELDS),
            None if clip_id is None else str(clip_id))


def _whitelist_parts(entries):
    paths = set()
    keys = set()
    trash_ids = set()
    if isinstance(entries, (str, bytes, os.PathLike)):
        entries = (entries,)
    for entry in entries or ():
        if isinstance(entry, (str, bytes, os.PathLike)):
            value = os.fsdecode(os.fspath(entry))
            paths.add(value)
            trash_ids.add(value)
            continue
        if isinstance(entry, Mapping):
            path = entry.get("path")
            identity = entry.get("identity")
            if path is not None and isinstance(identity, Mapping):
                try:
                    keys.add(_file_key(path, identity, entry.get("clip_id")))
                except (KeyError, TypeError, ValueError):
                    # A malformed whitelist entry must not authorize cleanup.
                    continue
            continue
        if isinstance(entry, (tuple, list)) and len(entry) >= 2:
            path, identity = entry[0], entry[1]
            try:
                keys.add(_file_key(path, identity, entry[2] if len(entry) > 2 else None))
            except (KeyError, TypeError, ValueError):
                # A malformed whitelist entry must not authorize cleanup.
                continue
    return paths, keys, trash_ids


def _is_partial(name: str) -> bool:
    folded = name.casefold()
    return (folded.startswith(".") or folded.endswith((".partial", ".tmp", ".part", ".recovery"))
            or ".fthr-" in folded)


def _category(relative: str, name: str) -> str:
    parts = [part.casefold() for part in Path(relative).parts]
    if ".fthr-trash" in parts:
        return "trash"
    # Profile/cache namespaces may be nested in the selected root, but they
    # are technical accounting data rather than library media.  The cover
    # namespace itself is excluded by its exact ``<DB>.covers`` path below.
    if any(part in {".fthr", "cache", "caches", "assets"}
           for part in parts):
        return "other"
    if any(part.startswith(".fthr-") for part in parts):
        return "other"
    if any(part in {"exported", "shared"} for part in parts):
        return "other"
    if name.casefold().endswith((".json", ".yaml", ".yml", ".txt", ".db", ".sqlite3")):
        return "companion"
    if _is_partial(name):
        return "other"
    if not is_library_media_path(name):
        return "other"
    return "active"


def _accounting_priority(category: str) -> int:
    return {"active": 4, "trash": 3, "companion": 2, "other": 1}.get(category, 0)


def _snapshot_role(entry) -> str:
    if not isinstance(entry, Mapping):
        return ""
    if entry.get("_storage_hint_only") is True and "role" not in entry:
        return "PRIMARY"
    raw_role = entry.get("role", "")
    return str(getattr(raw_role, "value", raw_role))


def _metadata_flags(entry):
    if not isinstance(entry, Mapping):
        # A media file without a registration is eligible for an explicit
        # manual review.  Automatic cleanup still rejects it below because it
        # has no current clip identity and no exact binding.
        return None, False, False, None
    flags = entry.get("flags", entry.get("library_flags", {}))
    if not isinstance(flags, Mapping):
        return entry.get("clip_id"), False, False, "malformed metadata"
    protected = flags.get("protected", False)
    favorite = flags.get("favorite", False)
    if type(protected) is not bool or type(favorite) is not bool:
        return entry.get("clip_id"), False, False, "malformed metadata"
    return entry.get("clip_id"), protected, favorite, entry.get("error")


def _walk_files(root, cancel=None):
    """Yield ``(path, stat_result, relative)`` without following links."""
    root = os.path.abspath(root)
    root_info = os.lstat(root)
    if (not stat.S_ISDIR(root_info.st_mode)
            or stat.S_ISLNK(root_info.st_mode)
            or bool(getattr(root_info, "st_file_attributes", 0) & _REPARSE)):
        raise OSError(f"Storage walk root is missing or linked: {root}")
    if (int(getattr(root_info, "st_dev", 0)) == 0
            or int(getattr(root_info, "st_ino", 0)) == 0):
        raise OSError(f"Storage walk root has no stable identity: {root}")
    stack = [(root, "", (int(root_info.st_dev), int(root_info.st_ino)))]
    while stack:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("storage inventory cancelled")
        directory, relative, expected_directory = stack.pop()
        try:
            directory_info = os.stat(directory, follow_symlinks=False)
            if (not stat.S_ISDIR(directory_info.st_mode)
                    or stat.S_ISLNK(directory_info.st_mode)
                    or bool(getattr(directory_info, "st_file_attributes", 0) & _REPARSE)
                    or (int(getattr(directory_info, "st_dev", 0)),
                        int(getattr(directory_info, "st_ino", 0))) != expected_directory):
                yield directory, None, relative
                continue
            with os.scandir(directory) as entries:
                for entry in entries:
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError("storage inventory cancelled")
                    path = entry.path
                    try:
                        if entry.is_symlink():
                            continue
                        cached = entry.stat(follow_symlinks=False)
                    except OSError:
                        # A disappearing directory entry cannot authorize a complete scan.
                        yield path, None, os.path.join(relative, entry.name)
                        continue
                    if bool(getattr(cached, "st_file_attributes", 0) & _REPARSE):
                        continue
                    try:
                        info = os.stat(path, follow_symlinks=False)
                    except OSError:
                        # The authoritative no-follow identity is unavailable.
                        yield path, None, os.path.join(relative, entry.name)
                        continue
                    attributes = int(getattr(info, "st_file_attributes", 0))
                    if (stat.S_ISLNK(info.st_mode)
                            or attributes & _REPARSE):
                        continue
                    child_relative = os.path.join(relative, entry.name)
                    if stat.S_ISDIR(info.st_mode):
                        directory_identity = (int(getattr(info, "st_dev", 0)),
                                              int(getattr(info, "st_ino", 0)))
                        if 0 in directory_identity:
                            yield path, None, child_relative
                            continue
                        stack.append((path, child_relative, directory_identity))
                    elif stat.S_ISREG(info.st_mode):
                        if (int(getattr(info, "st_dev", 0)) == 0
                                or int(getattr(info, "st_ino", 0)) == 0
                                or int(getattr(info, "st_nlink", 0)) <= 0):
                            # A regular file without stable IDs cannot be counted safely.
                            yield path, None, child_relative
                            continue
                        yield path, info, child_relative
        except OSError:
            # An unreadable or replaced directory makes this inventory incomplete.
            yield directory, None, relative


def _external_usage(roots, cancel=None):
    total = 0
    warnings = []
    for raw in roots or ():
        try:
            for _path, info, _ in _walk_files(raw, cancel):
                if info is not None:
                    total += int(info.st_size)
        except (InterruptedError, OSError) as error:
            if isinstance(error, InterruptedError):
                raise
            warnings.append(f"Could not inspect external folder {raw}: {error}")
    return total, warnings


def _is_under_root(path, root) -> bool:
    try:
        canonical_import = canonical_root(root)
        return os.path.commonpath((canonical_root(path), canonical_import)) == canonical_import
    except (OSError, ValueError):
        # An unrepresentable path cannot be proven to belong to the root.
        return False


def _validated_manifest_companion(primary_path, files_by_path, metadata_snapshot, cancel=None):
    """Return a validated adjacent manifest and its bounded byte size.

    The inventory worker validates only the bounded manifest document.  The
    media hash is checked by the existing registration/journal path before a
    move, so a storage refresh never hashes every video.
    """
    manifest_path = str(manifest_path_for(primary_path))
    item = files_by_path.get(canonical_root(manifest_path))
    if item is None:
        # A sidecar is optional for legacy clips.  A registered-but-missing
        # sidecar is different: the clip bundle is incomplete and cannot be
        # admitted to automatic cleanup.
        if _snapshot_entry(metadata_snapshot, manifest_path) is not None:
            return None, "audio manifest is missing"
        return None, None
    if item.category == "trash" or item.nlink != 1:
        return None, "audio manifest is missing or has unsafe links"
    if not item.complete:
        return None, "audio manifest is busy or incomplete"
    if item.size > 1024 * 1024:
        return None, "audio manifest exceeds the 1 MiB limit"
    try:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("storage inventory cancelled")
        with open(item.path, "rb") as stream:
            payload = stream.read(1024 * 1024 + 1)
        if len(payload) != item.size or len(payload) > 1024 * 1024:
            return None, "audio manifest changed during inspection"
        after = os.stat(item.path, follow_symlinks=False)
        after_identity = {
            "st_dev": int(after.st_dev), "st_ino": int(after.st_ino),
            "size": int(after.st_size), "mtime_ns": int(after.st_mtime_ns),
        }
        if (not stat.S_ISREG(after.st_mode)
                or after_identity != dict(item.identity)):
            return None, "audio manifest changed during inspection"
        manifest = json.loads(payload.decode("utf-8"))
        validate_manifest(manifest)
        if manifest.get("media_file") != os.path.basename(primary_path):
            return None, "audio manifest basename does not match the clip"
    except InterruptedError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError,
            AudioManifestError):
        return None, "audio manifest is invalid"
    metadata_entry = _snapshot_entry(metadata_snapshot, manifest_path)
    if metadata_entry is not None:
        role = metadata_entry.get("role", "SIDECAR") if isinstance(metadata_entry, Mapping) else ""
        role = str(getattr(role, "value", role))
        state = metadata_entry.get("binding_state") if isinstance(metadata_entry, Mapping) else None
        state = str(getattr(state, "value", state))
        if (role != "SIDECAR"
                or state in {"CONFLICT", "MISSING", "TRASHED", "PURGING"}
                or metadata_entry.get("complete") is not True):
            return None, "audio manifest has an ambiguous registration"
        stored_identity = metadata_entry.get("identity")
        try:
            if (not isinstance(stored_identity, Mapping)
                    or set(stored_identity) != set(_STAT_FIELDS)
                    or any(type(stored_identity[field]) is not int
                           for field in _STAT_FIELDS)
                    or dict(stored_identity) != dict(item.identity)):
                return None, "audio manifest identity changed"
        except (KeyError, TypeError, ValueError):
            return None, "audio manifest identity changed"
        clip_id = metadata_entry.get("clip_id")
    else:
        clip_id = None
    return item, clip_id


def _reconcile_primary_bundles(files, metadata_snapshot, cancel=None):
    """Validate registered clip groups and attach safe adjacent manifests."""
    files_by_path = {canonical_root(item.path): item for item in files}
    snapshot_files = _snapshot_files(metadata_snapshot)
    registered_by_clip = {}
    for entry in snapshot_files:
        if not isinstance(entry, Mapping) or entry.get("clip_id") is None:
            continue
        registered_by_clip.setdefault(str(entry["clip_id"]), []).append(entry)

    def registered_group_is_complete(clip_id):
        """Reject missing/unknown registered companions before selecting a primary."""
        entries = registered_by_clip.get(str(clip_id), ())
        if not entries:
            return True
        for entry in entries:
            path = entry.get("path")
            if not isinstance(path, str):
                return False
            scanned = files_by_path.get(canonical_root(path))
            if scanned is None or scanned.nlink != 1:
                return False
            role = _snapshot_role(entry)
            if role not in {"PRIMARY", "SIDECAR"}:
                return False
            state = str(getattr(
                entry.get("binding_state"), "value", entry.get("binding_state", "")))
            if state in {"CONFLICT", "MISSING", "TRASHED", "PURGING", "SHARED"}:
                return False
            if entry.get("complete") is not True:
                return False
            stored_identity = entry.get("identity")
            if stored_identity is None:
                if entry.get("_storage_hint_only") is True:
                    # Older lightweight callers may provide only clip/flag hints;
                    # the authoritative manager snapshot always includes identity.
                    continue
                return False
            if (not isinstance(stored_identity, Mapping)
                    or set(stored_identity) != set(_STAT_FIELDS)
                    or any(type(stored_identity[field]) is not int
                           for field in _STAT_FIELDS)
                    or dict(stored_identity) != dict(scanned.identity)):
                return False
            if (entry.get("shared") is True
                    or str(entry.get("provenance", "")).casefold() == "shared"):
                return False
        return True

    by_clip = {}
    for item in files:
        if item.clip_id is not None:
            by_clip.setdefault(item.clip_id, []).append(item)
    blocked_clips = set()
    for clip_id, members in by_clip.items():
        primaries = [member for member in members
                     if member.role == "PRIMARY" and member.category == "active"]
        invalid = len(primaries) != 1
        invalid |= any(member.role not in {"PRIMARY", "SIDECAR"}
                       or not member.complete
                       for member in members)
        if len(primaries) == 1:
            expected_manifest = canonical_root(manifest_path_for(primaries[0].path))
            invalid |= any(member.role == "SIDECAR"
                           and canonical_root(member.path) != expected_manifest
                           for member in members)
        if clip_id is not None and not registered_group_is_complete(clip_id):
            invalid = True
        if invalid:
            blocked_clips.add(clip_id)

    replacements = {}
    for item in files:
        if item.category != "active" or item.role != "PRIMARY":
            continue
        manifest, manifest_value = _validated_manifest_companion(
            item.path, files_by_path, metadata_snapshot, cancel)
        blocked = item.clip_id in blocked_clips if item.clip_id is not None else False
        bundle_size = item.size
        bundle_valid = True
        members = ()
        if manifest is not None:
            bundle_size += manifest.size
            members = (manifest,)
            if (manifest_value is not None and item.clip_id is not None
                    and manifest_value not in (None, item.clip_id)):
                blocked = True
        elif manifest_value is not None:
            bundle_valid = False
            blocked = True
        if blocked:
            bundle_valid = False
        eligible = item.eligible and bundle_valid
        automatic = item.automatic_eligible and bundle_valid
        reason = item.reason
        if not bundle_valid:
            reason = "clip bundle has an invalid or ambiguous companion"
        replacements[item.path] = (
            item.__class__(item.path, item.identity, item.category, item.size,
                           item.mtime_ns, item.clip_id, item.role, item.protected,
                           item.favorite, item.complete, eligible, reason,
                           item.nlink, item.trash_id, item.bundle_mtime_ns,
                           item.bundle_created_utc, automatic, bundle_size,
                           bundle_valid), members)
    result = []
    members_by_path = {}
    for item in files:
        replacement = replacements.get(item.path)
        if replacement is None:
            result.append(item)
        else:
            result.append(replacement[0])
            members_by_path[item.path] = replacement[1]
    return result, members_by_path


def _trash_members_from_snapshot(metadata_snapshot):
    """Return the exact registered member paths for TRASHED bundles."""
    if isinstance(metadata_snapshot, StorageBindingsSnapshot):
        values = metadata_snapshot.by_path.values()
    elif isinstance(metadata_snapshot, Mapping):
        raw = metadata_snapshot.get("by_path", metadata_snapshot)
        values = raw.values() if isinstance(raw, Mapping) else ()
    else:
        values = ()
    expected = {}
    for raw_entry in values:
        if not isinstance(raw_entry, Mapping):
            continue
        state = str(getattr(raw_entry.get("binding_state"), "value",
                            raw_entry.get("binding_state", "")))
        path = raw_entry.get("path")
        if state != "TRASHED" or not isinstance(path, str):
            continue
        parts = Path(path).parts
        folded = [part.casefold() for part in parts]
        if ".fthr-trash" not in folded:
            continue
        index = folded.index(".fthr-trash")
        if index + 1 >= len(parts):
            continue
        expected.setdefault(parts[index + 1], set()).add(canonical_root(path))
    return expected


def _reconcile_trash_bundles(files, metadata_snapshot):
    """Reject unknown, partial, replaced, or extra Trash bundle members."""
    expected = _trash_members_from_snapshot(metadata_snapshot)
    actual = {}
    for item in files:
        if item.category == "trash" and item.trash_id is not None:
            actual.setdefault(item.trash_id, []).append(item)
    result = []
    for item in files:
        if item.category != "trash" or item.trash_id is None:
            result.append(item)
            continue
        members = actual[item.trash_id]
        actual_paths = {canonical_root(member.path) for member in members}
        valid = bool(expected.get(item.trash_id)) and actual_paths == expected[item.trash_id]
        if valid:
            result.append(item)
            continue
        result.append(item.__class__(
            item.path, item.identity, item.category, item.size, item.mtime_ns,
            item.clip_id, item.role, item.protected, item.favorite, False,
            False, "Trash bundle is incomplete or has unknown members",
            item.nlink, item.trash_id, item.bundle_mtime_ns,
            item.bundle_created_utc, False, item.bundle_size, False))
    return result


def build_inventory(root, *, metadata_snapshot=None, imported_roots=(), cancel=None,
                    cover_bytes=0, cover_root=None, now=None, free_bytes=True,
                    trash_created_utc=None, busy_paths=()) -> StorageInventory:
    """Build one bounded, cancellable usage snapshot for ``root``.

    This function only reads the filesystem.  It never calls a metadata getter
    per file and never follows links or reparse points.
    """
    root = os.path.abspath(os.fspath(root))
    root_stat = root_identity(root)
    representatives = {}
    totals = {"active": 0, "trash": 0, "companion": 0, "other": 0}
    warnings = []
    complete = True
    imported_bytes = 0
    imported_roots = tuple(imported_roots or ())
    if isinstance(busy_paths, (str, bytes, os.PathLike)):
        busy_paths = (busy_paths,)
    busy_keys = {canonical_root(path) for path in (busy_paths or ())
                 if path is not None}
    for path, info, relative in _walk_files(root, cancel):
        if info is None:
            complete = False
            warnings.append(f"Could not inspect {path}")
            continue
        key = (int(info.st_dev), int(info.st_ino))
        identity = {
            "st_dev": int(info.st_dev), "st_ino": int(info.st_ino),
            "size": int(info.st_size), "mtime_ns": int(info.st_mtime_ns),
        }
        imported_here = any(_is_under_root(path, imported)
                            for imported in imported_roots)
        category = ("other" if imported_here
                    or (cover_root is not None and _is_under_root(path, cover_root))
                    else _category(relative, os.path.basename(path)))
        entry = _snapshot_entry(metadata_snapshot, path)
        clip_id, protected, favorite, metadata_error = _metadata_flags(entry)
        raw_role = entry.get("role", "PRIMARY") if isinstance(entry, Mapping) else "PRIMARY"
        role = getattr(raw_role, "value", raw_role)
        role = str(role)
        complete_value = entry.get("complete", True) if isinstance(entry, Mapping) else True
        complete_file = type(complete_value) is bool and complete_value
        reason = metadata_error
        if canonical_root(path) in busy_keys:
            complete_file = False
            reason = "path is busy in an active GUI operation"
        if entry is not None and type(complete_value) is not bool:
            reason = "malformed metadata"
        if category == "trash":
            if metadata_error is not None:
                complete_file = False
                reason = metadata_error
            stored_identity = entry.get("identity") if isinstance(entry, Mapping) else None
            if (not isinstance(stored_identity, Mapping)
                    or set(stored_identity) != set(_STAT_FIELDS)
                    or dict(stored_identity) != identity):
                complete_file = False
                reason = "Trash member identity is unknown or changed"
        automatic_eligible = False
        if category != "active":
            eligible = False
            if reason is None:
                reason = "not an active library primary"
        else:
            raw_binding_state = (entry.get("binding_state", "")
                                 if isinstance(entry, Mapping) else "")
            binding_state = getattr(raw_binding_state, "value", raw_binding_state)
            binding_state = str(binding_state)
            stored_identity = entry.get("identity") if isinstance(entry, Mapping) else None
            identity_matches = (stored_identity is None
                                or stored_identity == identity)
            exact_identity = (isinstance(stored_identity, Mapping)
                              and set(stored_identity) == set(_STAT_FIELDS)
                              and all(type(stored_identity[field]) is int
                                      for field in _STAT_FIELDS)
                              and dict(stored_identity) == identity)
            eligible = bool(role == "PRIMARY" and not protected and not favorite
                            and complete_file and identity_matches
                            and (entry is None or metadata_error is None)
                            and binding_state not in {"CONFLICT", "MISSING", "TRASHED", "PURGING"})
            if not eligible and reason is None:
                reason = "protected, unregistered, incomplete, or ambiguous"
            elif eligible and clip_id is None:
                reason = "unregistered media requires explicit review"
            elif eligible and binding_state in {"UNVERIFIED", "LEGACY_UNVERIFIED"}:
                reason = "legacy binding requires explicit review"
            elif eligible and binding_state not in {"STAT_BOUND", "CONTENT_VERIFIED"}:
                reason = "automatic cleanup requires a verified binding"
            elif eligible:
                automatic_eligible = True
            if automatic_eligible and not exact_identity:
                automatic_eligible = False
                reason = "automatic cleanup requires a current file identity"
        trash_id = None
        bundle_mtime_ns = 0
        bundle_created = None
        parts = Path(relative).parts
        if ".fthr-trash" in (part.casefold() for part in parts):
            index = next((i for i, part in enumerate(parts) if part.casefold() == ".fthr-trash"), None)
            if index is not None and index + 1 < len(parts):
                trash_id = parts[index + 1]
                try:
                    bundle_path = os.path.join(root, *parts[:index + 2])
                    bundle_info = os.lstat(bundle_path)
                    if (stat.S_ISDIR(bundle_info.st_mode)
                            and not (int(getattr(bundle_info, "st_file_attributes", 0))
                                     & _REPARSE)):
                        bundle_mtime_ns = int(bundle_info.st_mtime_ns)
                except OSError:
                    # The candidate remains conservative: retention age cannot
                    # be authorized without a readable Trash bundle record.
                    bundle_mtime_ns = 0
                if isinstance(trash_created_utc, Mapping):
                    value = trash_created_utc.get(trash_id)
                    if isinstance(value, str) and value:
                        bundle_created = value
        item = StorageFile(path, identity, category, identity["size"], identity["mtime_ns"],
                           str(clip_id) if clip_id is not None else None,
                           str(role), protected, favorite, complete_file, eligible,
                           reason, int(getattr(info, "st_nlink", 1)), trash_id,
                           bundle_mtime_ns, bundle_created,
                           automatic_eligible)
        current = representatives.get(key)
        if (current is None
                or _accounting_priority(item.category) > _accounting_priority(current[0].category)
                or (_accounting_priority(item.category) == _accounting_priority(current[0].category)
                    and canonical_root(item.path) < canonical_root(current[0].path))):
            representatives[key] = (item, imported_here)
    files = []
    for item, imported_here in representatives.values():
        files.append(item)
        totals[item.category] += item.size
        if imported_here:
            imported_bytes += item.size
    # Imported folders outside the owned root are intentionally not walked:
    # their bytes are read-only and cannot authorize a cleanup decision.
    if any(not _is_under_root(imported, root) for imported in imported_roots):
        warnings.append("Imported folders outside the Clips folder are excluded from cleanup.")
    files, _manifest_members = _reconcile_primary_bundles(
        files, metadata_snapshot, cancel)
    files = _reconcile_trash_bundles(files, metadata_snapshot)
    # Recompute only the active eligibility projection; physical accounting is
    # unchanged because adjacent manifests are already counted as companions.
    if now is None:
        now = time.time()
    try:
        free = int(os.statvfs(root).f_bavail * os.statvfs(root).f_frsize) if free_bytes else None
    except (AttributeError, OSError):
        try:
            import shutil
            free = int(shutil.disk_usage(root).free) if free_bytes else None
        except OSError:
            free = None
            complete = False
            warnings.append("Free-space information is unavailable")
    return StorageInventory(root, root_stat, tuple(files), totals["active"],
                            totals["trash"], totals["companion"], totals["other"],
                            imported_bytes, int(cover_bytes), free, complete,
                            tuple(warnings), float(now))


def select_candidates(inventory: StorageInventory, policy: StoragePolicy, *,
                      needed_bytes=0, now=None, manual_whitelist=None,
                      automatic=False, max_items=MAX_CANDIDATES):
    """Select an oldest-first reviewed batch without modifying anything."""
    if not inventory.complete:
        return ()
    if max_items < 0:
        raise ValueError("max_items must be non-negative")
    if max_items == 0:
        return ()
    if int(needed_bytes) <= 0:
        return ()
    max_items = min(int(max_items), MAX_CANDIDATES)
    now = time.time() if now is None else float(now)
    whitelist_paths, whitelist_keys, _ = _whitelist_parts(manual_whitelist)
    candidates = []
    for item in inventory.files:
        allowed = (item.eligible and item.category == "active" and item.nlink == 1
                   and (not automatic or item.clip_id is not None))
        key = _file_key(item.path, item.identity, item.clip_id)
        whitelisted = item.path in whitelist_paths or key in whitelist_keys
        if automatic and not item.automatic_eligible:
            allowed = False
        if (whitelist_paths or whitelist_keys) and not whitelisted:
            allowed = False
        if item.mtime_ns / 1_000_000_000 > now - MIN_AGE_SECONDS:
            allowed = False
        if not allowed:
            continue
        candidates.append(StorageCandidate("trash", item, item.bundle_size or item.size))
    candidates.sort(key=lambda value: (value.file.mtime_ns, value.file.path.casefold()))
    result = []
    reclaimed = 0
    for candidate in candidates:
        result.append(candidate)
        reclaimed += candidate.estimated_reclaimable_bytes
        if reclaimed >= max(0, int(needed_bytes)) or len(result) >= max_items:
            break
    return tuple(result)


def select_trash_candidates(inventory: StorageInventory, policy: StoragePolicy, *,
                            now=None, max_items=MAX_CANDIDATES, manual_whitelist=None,
                            needed_bytes=0):
    """Select old trash entries for an explicitly reviewed purge pass."""
    if not inventory.complete:
        return ()
    if int(max_items) <= 0:
        return ()
    now = time.time() if now is None else float(now)
    eligible = []
    whitelist_paths, whitelist_keys, whitelist_trash_ids = _whitelist_parts(manual_whitelist)
    overflow = max(0, inventory.trash_bytes - policy.trash_max_bytes) if policy.trash_max_bytes else 0
    groups = {}
    for item in inventory.files:
        if item.category != "trash":
            continue
        groups.setdefault(item.trash_id or item.path, []).append(item)
    for members in groups.values():
        # A Trash bundle is the unit of purge.  One protected/favorite or
        # incomplete companion therefore protects the complete bundle.
        if any(member.nlink != 1 or member.protected or member.favorite
               or not member.complete or not member.bundle_created_utc
               for member in members):
            continue
        bundle_times = {member.bundle_created_utc for member in members}
        if len(bundle_times) != 1:
            continue
        primaries = [member for member in members if member.role == "PRIMARY"]
        if len(primaries) != 1 or any(member.role not in {"PRIMARY", "SIDECAR"}
                                      for member in members):
            continue
        item = primaries[0]
        size = sum(member.size for member in members)
        age_valid = True
        age_time = 0.0
        try:
            from datetime import datetime
            age_time = datetime.fromisoformat(
                item.bundle_created_utc.replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, OverflowError):
            # A malformed journal timestamp cannot authorize a purge.
            age_valid = False
        age = max(0.0, now - age_time)
        age_match = (age_valid and policy.trash_retention_days > 0
                     and age >= policy.trash_retention_days * 86400)
        whitelist_match = item.trash_id in whitelist_trash_ids or any(
            member.path in whitelist_paths
            or _file_key(member.path, member.identity, member.clip_id) in whitelist_keys
            for member in members
        )
        size_match = overflow > 0 or needed_bytes > 0
        whitelist_provided = bool(whitelist_paths or whitelist_keys or whitelist_trash_ids)
        if whitelist_provided and not whitelist_match:
            continue
        if not age_match and not size_match:
            # A manual whitelist identifies reviewed rows, but cannot create
            # purge work after a fresh pass has no retention/size need.
            continue
        eligible.append(StorageCandidate("purge", item, size, tuple(members)))
    def bundle_order(value):
        created = value.file.bundle_created_utc
        if created:
            try:
                from datetime import datetime
                timestamp = datetime.fromisoformat(
                    created.replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError, OverflowError):
                timestamp = float("inf")
        else:
            timestamp = float("inf")
        return timestamp, value.file.path.casefold()

    eligible.sort(key=bundle_order)
    result = []
    reclaimed = 0
    target = max(overflow, int(needed_bytes))
    for candidate in eligible:
        result.append(candidate)
        reclaimed += candidate.estimated_reclaimable_bytes
        if target and reclaimed >= target:
            break
        if len(result) >= min(int(max_items), MAX_CANDIDATES):
            break
    return tuple(result)


def make_preview(inventory: StorageInventory, policy: StoragePolicy, *, now=None,
                 manual_whitelist=None, max_items=MAX_CANDIDATES,
                 automatic=False) -> StoragePreview:
    needed = max(0, inventory.total_bytes - policy.max_library_bytes) if policy.max_library_bytes else 0
    trash_needed = max(0, inventory.trash_bytes - policy.trash_max_bytes) if policy.trash_max_bytes else 0
    trash_candidates = (() if automatic and not policy.automatic_trash_cleanup else
                        select_trash_candidates(
                            inventory, policy, now=now,
                            manual_whitelist=manual_whitelist,
                            needed_bytes=trash_needed, max_items=max_items))
    trash_reclaimed = sum(item.estimated_reclaimable_bytes for item in trash_candidates)
    candidates = (() if automatic and not policy.automatic_cleanup else
                  select_candidates(
                      inventory, policy,
                      needed_bytes=max(0, needed - trash_reclaimed), now=now,
                      manual_whitelist=manual_whitelist,
                      automatic=automatic,
                      max_items=max(0, int(max_items) - len(trash_candidates))))
    candidates = tuple(trash_candidates) + tuple(candidates)
    reclaimed = sum(item.estimated_reclaimable_bytes for item in candidates)
    return StoragePreview(candidates, max(needed, trash_needed), reclaimed,
                          max(0, max(needed, trash_needed) - reclaimed),
                          inventory.complete, inventory.warnings)


def quota_admission(inventory: StorageInventory, policy: StoragePolicy, incoming_bytes: int) -> tuple[bool, str]:
    """Return a conservative save admission decision from a fresh snapshot."""
    if type(incoming_bytes) is not int or incoming_bytes < 0:
        raise ValueError("incoming_bytes must be a non-negative integer")
    if not inventory.complete:
        return False, "Storage usage is still being checked; try again shortly."
    if not policy.max_library_bytes:
        return True, ""
    if inventory.total_bytes >= policy.max_library_bytes:
        return False, "The configured clips storage limit has been reached. Review storage cleanup in Settings."
    if inventory.total_bytes + incoming_bytes <= policy.max_library_bytes:
        return True, ""
    return False, "The configured clips storage limit has been reached. Review storage cleanup in Settings."


# Descriptive aliases used by the controller and external checks.
build_storage_inventory = build_inventory
select_storage_candidates = select_candidates
preview_storage = make_preview
storage_root_identity = root_identity
