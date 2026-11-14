"""Declarative, resumable backup and restore for an owned clip library.

The service deliberately keeps the package format independent from SQLite.  A
package is a directory containing a bounded JSON manifest and immutable,
SHA-256 named objects.  Database changes are made only by the metadata
manager's atomic restore contract after every copied object has been checked.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat as stat_module
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from core.audio_manifest import manifest_path_for, validate_manifest as validate_audio_manifest
from core.clip_metadata_types import BindingState, FileRole
from core.clip_files import (
    PENDING_RESTORE_MARKER,
    is_fthr_temporary_dir,
    is_library_media_path,
    is_pending_restore_directory,
)
from core.clip_mutations import rename_no_replace
from core.library_covers import CoverAssetStore, cover_reference_from_png, parse_cover_reference


BACKUP_FORMAT_VERSION = 1
CHECKPOINT_FORMAT_VERSION = 1
CHUNK_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_FILES = 100_000
MAX_JSON_DEPTH = 64
MAX_AUDIO_MANIFEST_BYTES = 1024 * 1024
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.I)
_TECHNICAL_DIRS = frozenset({
    ".fthr", ".fthr-covers", ".fthr-trash", ".fthr-backup", ".fthr-restore",
})


@dataclass(frozen=True)
class BackupOptions:
    include_imported: bool = False
    include_trash: bool = True

    def __post_init__(self):
        if type(self.include_imported) is not bool or type(self.include_trash) is not bool:
            raise TypeError("backup options are boolean")


@dataclass(frozen=True)
class BackupProgress:
    phase: str
    completed: int
    total: int
    bytes_done: int = 0
    message: str = ""

    def __post_init__(self):
        if (not isinstance(self.phase, str) or type(self.completed) is not int
                or type(self.total) is not int or type(self.bytes_done) is not int
                or self.completed < 0 or self.total < 0 or self.bytes_done < 0):
            raise TypeError("invalid backup progress")


@dataclass(frozen=True)
class BackupResult:
    package_path: str
    completed: bool
    clips: int
    files: int
    excluded: tuple
    restore_uuid: str | None = None
    destination_root: str | None = None
    already_imported: bool = False
    restore_checkpoint_path: str | None = None


@dataclass(frozen=True)
class VerifiedBackup:
    package_path: str
    manifest_sha256: str
    manifest: dict

    @property
    def clips(self) -> int:
        return len(self.manifest.get("entries", ()))

    @property
    def files(self) -> int:
        return len(self.manifest.get("files", ())) + len(self.manifest.get("assets", ()))

    @property
    def excluded(self) -> tuple:
        return tuple(item.get("reason", "excluded") for item in self.manifest.get("excluded", ()))


class LibraryBackupService:
    """Create, verify, resume, and restore one owned library package.

    Callbacks are intentionally plain callables so this class remains usable
    from a worker thread without importing Qt or depending on a GUI loop.
    """

    def __init__(self, manager, owned_root, *, imported_roots=(), reserve=None,
                 release=None, current_root=None):
        self.manager = manager
        self.owned_root = self._absolute(owned_root, "owned_root")
        if isinstance(imported_roots, (str, bytes)):
            raise TypeError("imported_roots must be an iterable of paths")
        self.imported_roots = tuple(
            self._absolute(root, "imported_root") for root in imported_roots)
        self._reserve = reserve or (lambda _paths: True)
        self._release = release or (lambda _paths: None)
        self._current_root = current_root
        self._lock = threading.RLock()
        self._last_restore_checkpoint = None

    @property
    def pending_restore_checkpoint(self):
        """Return the latest durable restore checkpoint known to this worker."""
        return self._last_restore_checkpoint

    @staticmethod
    def _absolute(value, label):
        try:
            text = os.fspath(value)
        except TypeError as error:
            raise ValueError(f"{label} must be a filesystem path") from error
        if not isinstance(text, str) or not text.strip() or "\0" in text:
            raise ValueError(f"{label} is invalid")
        return os.path.abspath(text)

    @staticmethod
    def _key(path):
        return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))

    @classmethod
    def _under(cls, path, root):
        try:
            return os.path.commonpath((cls._key(path), cls._key(root))) == cls._key(root)
        except ValueError:
            # Different Windows drives cannot share an owned root.
            return False

    @staticmethod
    def _is_reparse(path):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            # A disappearing path is not a reparse point; later validation handles absence.
            return False
        return path.is_symlink() or bool(
            getattr(info, "st_file_attributes", 0)
            & getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))

    @classmethod
    def _safe_parents(cls, path, root, *, allow_missing=True):
        path = Path(path)
        root = Path(root)
        if not cls._under(path, root):
            raise OSError("path is outside its owned root")
        if root.exists():
            root_info = os.lstat(root)
            if (not stat_module.S_ISDIR(root_info.st_mode) or root.is_symlink()
                    or bool(getattr(root_info, "st_file_attributes", 0)
                            & getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))):
                raise OSError("owned parent is not a safe directory")
        elif not allow_missing:
            raise OSError("owned parent disappeared")
        current = path if path.exists() else path.parent
        while cls._key(current) != cls._key(root):
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                if not allow_missing:
                    raise OSError("path parent disappeared") from None
                current = current.parent
                continue
            if path != current and not stat_module.S_ISDIR(info.st_mode):
                raise OSError("path parent is not a directory")
            if current.is_symlink() or bool(
                    getattr(info, "st_file_attributes", 0)
                    & getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                raise OSError("path parent contains a reparse point")
            current = current.parent
        if not cls._under(root, Path(current)):
            raise OSError("owned root changed during path validation")

    @classmethod
    def _identity(cls, path, *, regular=True):
        try:
            info = os.lstat(path)
        except OSError as error:
            raise OSError(f"could not stat {path}: {error}") from error
        if cls._is_reparse(Path(path)):
            raise OSError("reparse points are not backup media")
        if regular and not stat_module.S_ISREG(info.st_mode):
            raise OSError("backup source is not a regular file")
        return {
            "st_dev": int(info.st_dev), "st_ino": int(info.st_ino),
            "size": int(info.st_size), "mtime_ns": int(info.st_mtime_ns),
        }

    @staticmethod
    def _same_identity(left, right):
        return (isinstance(left, dict) and isinstance(right, dict)
                and all(type(left.get(key)) is int and type(right.get(key)) is int
                        and left[key] == right[key]
                        for key in ("st_dev", "st_ino", "size", "mtime_ns")))

    @classmethod
    def _exclusive_identity(cls, path):
        """Return a regular file identity only while it has one owning link."""

        info = os.lstat(path)
        if getattr(info, "st_nlink", 1) != 1:
            raise OSError("backup output must not be a hardlink")
        return cls._identity(path)

    @classmethod
    def _assert_stable_parent(cls, parent, expected=None):
        parent = Path(parent)
        cls._safe_parents(parent, Path(parent.anchor), allow_missing=False)
        actual = cls._stable_directory_identity(parent)
        if expected is not None and not cls._same_stable_identity(actual, expected):
            raise OSError("backup output parent changed during publication")
        return actual

    @classmethod
    def _assert_owned_temp(cls, path, expected, parent, parent_identity):
        cls._assert_stable_parent(parent, parent_identity)
        actual = cls._exclusive_identity(path)
        if (actual["st_dev"], actual["st_ino"], actual["size"]) != (
                expected["st_dev"], expected["st_ino"], expected["size"]):
            raise OSError("backup temporary output changed before publication")
        return actual

    @classmethod
    def _remove_owned_temp(cls, path, expected, parent, parent_identity):
        cls._assert_owned_temp(path, expected, parent, parent_identity)
        Path(path).unlink()

    @staticmethod
    def _cancelled(cancel):
        return cancel is not None and (
            bool(cancel.is_set()) if hasattr(cancel, "is_set") else bool(cancel()))

    @classmethod
    def _check_cancel(cls, cancel):
        if cls._cancelled(cancel):
            raise InterruptedError("backup operation cancelled; checkpoint retained")

    @staticmethod
    def _emit(progress, phase, completed, total, bytes_done=0, message=""):
        if progress is not None:
            progress(BackupProgress(phase, completed, total, bytes_done, message))

    @classmethod
    def _canonical_json(cls, value):
        cls._validate_json(value)
        return json.dumps(value, ensure_ascii=True, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")

    @classmethod
    def _validate_json(cls, value, depth=0):
        if depth > MAX_JSON_DEPTH:
            raise ValueError("JSON nesting exceeds the backup limit")
        if value is None or isinstance(value, str) or isinstance(value, bytes):
            if isinstance(value, bytes):
                raise ValueError("backup JSON cannot contain bytes")
            return
        if type(value) is bool:
            return
        if type(value) is int:
            return
        if type(value) is float:
            if not math.isfinite(value):
                raise ValueError("backup JSON cannot contain non-finite numbers")
            return
        if isinstance(value, list):
            for item in value:
                cls._validate_json(item, depth + 1)
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("backup JSON keys must be text")
                cls._validate_json(item, depth + 1)
            return
        raise ValueError("backup JSON contains an unsupported value")

    @classmethod
    def _strict_json(cls, payload):
        if not isinstance(payload, (bytes, bytearray)) or len(payload) > MAX_MANIFEST_BYTES:
            raise OSError("backup JSON is missing or exceeds the bounded size")

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate backup JSON key")
                result[key] = value
            return result

        value = json.loads(payload.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda token: (_ for _ in ()).throw(
                               ValueError(f"invalid JSON constant {token}")))
        cls._validate_json(value)
        return value

    @staticmethod
    def _uuid(value, label):
        if not isinstance(value, str) or not _UUID.fullmatch(value):
            raise ValueError(f"{label} must be a canonical UUID")
        if str(UUID(value)) != value:
            raise ValueError(f"{label} must be a canonical UUID")
        return value

    @staticmethod
    def _digest(value, label="digest"):
        if not isinstance(value, str) or not _DIGEST.fullmatch(value):
            raise ValueError(f"{label} must be a lowercase SHA-256")
        return value

    @classmethod
    def _relative(cls, value, label="relative path"):
        if (not isinstance(value, str) or not value or value.startswith("/")
                or "\\" in value or "\0" in value or re.match(r"^[A-Za-z]:", value)):
            raise ValueError(f"{label} is not a relative POSIX path")
        pieces = value.split("/")
        for piece in pieces:
            if (not piece or piece in {".", ".."} or piece.endswith((".", " "))
                    or ":" in piece or any(ord(char) < 32 for char in piece)
                    or _RESERVED.fullmatch(piece)):
                raise ValueError(f"{label} contains an unsafe component")
        return value

    @classmethod
    def _relative_unique(cls, values):
        seen = set()
        for value in values:
            key = value.casefold()
            if key in seen:
                raise ValueError("backup path collision after case folding")
            seen.add(key)

    @classmethod
    def _trash_suffix(cls, relative, trash_id):
        if not isinstance(relative, str):
            return None
        parts = relative.split("/")
        if (len(parts) < 3 or parts[-3] != ".fthr-trash"
                or parts[-2] != trash_id or not parts[-1]):
            return None
        if any(cls._is_technical_component(part) for part in parts[:-3]):
            return None
        return "/".join(parts[-3:])

    @staticmethod
    def _is_technical_component(part):
        folded = str(part).casefold()
        return (folded.startswith(".fthr-") or folded in _TECHNICAL_DIRS
                or folded in {"exported", "shared", "cache", "caches", "db",
                              "database", "__pycache__"})

    @classmethod
    def _root_identity(cls, root):
        root_path = Path(root)
        info = cls._identity(root_path, regular=False)
        if not stat_module.S_ISDIR(os.stat(root_path, follow_symlinks=False).st_mode):
            raise OSError("owned root is not a directory")
        return info

    @classmethod
    def _stable_directory_identity(cls, root):
        path = Path(root)
        info = os.lstat(path)
        if not stat_module.S_ISDIR(info.st_mode) or cls._is_reparse(path):
            raise OSError("restore directory is not a safe directory")
        return {"st_dev": int(info.st_dev), "st_ino": int(info.st_ino)}

    @staticmethod
    def _same_stable_identity(left, right):
        return (isinstance(left, dict) and isinstance(right, dict)
                and type(left.get("st_dev")) is int
                and type(left.get("st_ino")) is int
                and left["st_dev"] == right["st_dev"]
                and left["st_ino"] == right["st_ino"])

    @staticmethod
    def _require_format_version(value, expected, label):
        if type(value) is not int or value != expected:
            raise ValueError(f"unsupported {label} version")

    def _package_identity(self, package):
        package = Path(package)
        self._safe_parents(package, Path(package.anchor), allow_missing=False)
        if package.is_symlink() or not package.is_dir():
            raise OSError("backup package must be a private directory")
        return self._stable_directory_identity(package)

    def _assert_package_identity(self, package, expected=None):
        identity = self._package_identity(package)
        if expected is not None and not self._same_stable_identity(identity, expected):
            raise OSError("backup package directory changed during operation")
        return identity

    def _assert_roots(self):
        self._safe_parents(Path(self.owned_root), Path(Path(self.owned_root).anchor),
                           allow_missing=False)
        if self._is_reparse(Path(self.owned_root)):
            raise OSError("owned root is a reparse point")
        owned_key = self._key(self.owned_root)
        for root in self.imported_roots:
            if self._key(root) == owned_key or self._under(self.owned_root, root):
                raise OSError("imported root overlaps the owned root")
            self._safe_parents(Path(root), Path(Path(root).anchor), allow_missing=False)

    def _assert_current_root(self):
        if self._current_root is None:
            return
        current = self._absolute(self._current_root(), "current_root")
        if self._key(current) != self._key(self.owned_root):
            raise OSError("library root changed during backup operation")
        self._stable_directory_identity(self.owned_root)

    def _source_root_for(self, path, include_imported):
        path = self._absolute(path, "source")
        for root in self.imported_roots:
            if self._under(path, root):
                if not include_imported:
                    return None, "imported source excluded by options"
                return root, "imported"
        if self._under(path, self.owned_root):
            return self.owned_root, "owned"
        return None, "source is outside configured roots"

    def _hash_file(self, path, expected=None, cancel=None):
        self._safe_parents(Path(path), Path(Path(path).anchor), allow_missing=False)
        before = self._identity(path)
        if expected is not None and not self._same_identity(before, expected):
            raise OSError("backup source identity changed before read")
        digest = hashlib.sha256()
        total = 0
        with Path(path).open("rb") as source:
            opened = os.fstat(source.fileno())
            opened_identity = {
                "st_dev": int(opened.st_dev), "st_ino": int(opened.st_ino),
                "size": int(opened.st_size), "mtime_ns": int(opened.st_mtime_ns),
            }
            if not self._same_identity(before, opened_identity):
                raise OSError("backup source identity changed at open")
            while True:
                self._check_cancel(cancel)
                chunk = source.read(CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
        after = self._identity(path)
        if not self._same_identity(before, after) or total != before["size"]:
            raise OSError("backup source changed during read")
        result = digest.hexdigest()
        if expected is not None and expected.get("content_sha256") not in (None, result):
            raise OSError("registered content digest is stale")
        return result, before, total

    def _read_bounded(self, path, limit, *, cancel=None, expected=None):
        self._safe_parents(Path(path), Path(Path(path).anchor), allow_missing=False)
        before = self._identity(path)
        if expected is not None and not self._same_identity(before, expected):
            raise OSError("bounded source identity changed before read")
        if before["size"] > limit:
            raise OSError("bounded source exceeds its size limit")
        with Path(path).open("rb") as source:
            opened = os.fstat(source.fileno())
            opened_identity = {
                "st_dev": int(opened.st_dev), "st_ino": int(opened.st_ino),
                "size": int(opened.st_size), "mtime_ns": int(opened.st_mtime_ns),
            }
            if not self._same_identity(before, opened_identity):
                raise OSError("bounded source identity changed at open")
            payload = bytearray()
            while len(payload) <= limit:
                self._check_cancel(cancel)
                chunk = source.read(min(CHUNK_BYTES, limit + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
            payload = bytes(payload)
        if len(payload) != before["size"] or len(payload) > limit:
            raise OSError("bounded source changed during read")
        after = self._identity(path)
        if not self._same_identity(before, after):
            raise OSError("bounded source changed during read")
        self._check_cancel(cancel)
        return payload

    def _read_audio_manifest(self, media, cancel=None):
        """Read one optional sidecar through the bounded backup reader."""

        media = Path(media)
        sidecar = manifest_path_for(media)
        if not sidecar.is_file():
            return None
        try:
            payload = self._read_bounded(
                sidecar, MAX_AUDIO_MANIFEST_BYTES, cancel=cancel)
            manifest = self._strict_json(payload)
            validate_audio_manifest(manifest, cancelled=cancel)
            if manifest.get("media_file") != media.name:
                return None
            media_identity = self._identity(media)
            digest, _identity, _size = self._hash_file(
                media, expected=media_identity, cancel=cancel)
            if manifest.get("media_sha256") != digest:
                return None
            return manifest
        except InterruptedError:
            raise
        except (OSError, ValueError):
            # Missing or malformed optional sidecars are intentionally absent evidence.
            return None

    def _copy_object(self, source, destination, expected, cancel, *, expected_digest=None,
                     temporary_parent=None):
        digest = hashlib.sha256()
        before = self._identity(source)
        if not self._same_identity(before, expected):
            raise OSError("backup source identity changed before copy")
        self._safe_parents(destination.parent, Path(destination.anchor), allow_missing=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination_parent_identity = self._assert_stable_parent(destination.parent)
        temporary_directory = destination.parent if temporary_parent is None else Path(temporary_parent)
        self._safe_parents(temporary_directory, Path(temporary_directory.anchor), allow_missing=True)
        if self._is_reparse(temporary_directory):
            raise OSError("backup temporary directory contains a reparse point")
        temporary_directory.mkdir(parents=True, exist_ok=True)
        temporary_parent_identity = self._assert_stable_parent(temporary_directory)
        temporary = temporary_directory / ("." + destination.name + "." + str(uuid4()) + ".partial")
        if temporary.exists() or self._is_reparse(temporary):
            raise FileExistsError("backup temporary path already exists")
        published_identity = None
        try:
            temporary_open_identity = None
            with Path(source).open("rb") as input_file, temporary.open("xb") as output:
                opened = os.fstat(input_file.fileno())
                opened_identity = {
                    "st_dev": int(opened.st_dev), "st_ino": int(opened.st_ino),
                    "size": int(opened.st_size), "mtime_ns": int(opened.st_mtime_ns),
                }
                if not self._same_identity(before, opened_identity):
                    raise OSError("backup source identity changed at open")
                while True:
                    self._check_cancel(cancel)
                    chunk = input_file.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
                temp_info = os.fstat(output.fileno())
                temporary_open_identity = {
                    "st_dev": int(temp_info.st_dev), "st_ino": int(temp_info.st_ino),
                    "size": int(temp_info.st_size), "mtime_ns": int(temp_info.st_mtime_ns),
                }
            after = self._identity(source)
            if not self._same_identity(before, after):
                raise OSError("backup source changed during copy")
            self._assert_owned_temp(temporary, temporary_open_identity,
                                    temporary_directory, temporary_parent_identity)
            actual = digest.hexdigest()
            if expected_digest is not None and actual != expected_digest:
                raise OSError("backup source digest changed during copy")
            self._assert_stable_parent(destination.parent, destination_parent_identity)
            self._assert_owned_temp(temporary, temporary_open_identity,
                                    temporary_directory, temporary_parent_identity)
            try:
                rename_no_replace(temporary, destination)
                published_identity = self._exclusive_identity(destination)
            except FileExistsError:
                self._assert_stable_parent(destination.parent, destination_parent_identity)
                self._assert_owned_temp(temporary, temporary_open_identity,
                                        temporary_directory, temporary_parent_identity)
                if self._hash_file(destination, cancel=cancel)[0] != actual:
                    raise OSError("immutable backup object collision") from None
                self._remove_owned_temp(temporary, temporary_open_identity,
                                        temporary_directory, temporary_parent_identity)
                published_identity = self._exclusive_identity(destination)
        except Exception:
            try:
                if temporary_open_identity is not None and temporary.exists():
                    self._remove_owned_temp(temporary, temporary_open_identity,
                                            temporary_directory, temporary_parent_identity)
            except OSError:
                # A changed parent or temporary leaf remains as an orphan by design.
                pass
            raise
        if published_identity is None:
            raise OSError("backup publication identity is missing")
        return actual, published_identity

    def _package_path(self, package):
        package = Path(self._absolute(package, "package"))
        self._package_identity(package)
        return package

    def _write_json(self, path, value):
        payload = self._canonical_json(value)
        if len(payload) > MAX_MANIFEST_BYTES:
            raise OSError("backup JSON exceeds the bounded size")
        path = Path(path)
        self._safe_parents(path.parent, Path(path.anchor), allow_missing=False)
        if self._is_reparse(path) or self._is_reparse(path.parent):
            raise OSError("backup state path contains a reparse point")
        parent_identity = self._assert_stable_parent(path.parent)
        temporary = path.with_name("." + path.name + "." + str(uuid4()) + ".partial")
        temporary_open_identity = None
        try:
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
                info = os.fstat(stream.fileno())
                temporary_open_identity = {
                    "st_dev": int(info.st_dev), "st_ino": int(info.st_ino),
                    "size": int(info.st_size), "mtime_ns": int(info.st_mtime_ns),
                }
            self._assert_owned_temp(temporary, temporary_open_identity,
                                    path.parent, parent_identity)
            self._assert_stable_parent(path.parent, parent_identity)
            self._assert_owned_temp(temporary, temporary_open_identity,
                                    path.parent, parent_identity)
            os.replace(temporary, path)
        except Exception:
            try:
                if temporary_open_identity is not None and temporary.exists():
                    self._remove_owned_temp(temporary, temporary_open_identity,
                                            path.parent, parent_identity)
            except OSError:
                # A changed parent or temporary leaf remains as an orphan by design.
                pass
            raise
        return payload

    def _read_checkpoint(self, package):
        self._package_identity(package)
        path = package / ".fthr-backup-state.json"
        if not path.is_file() or self._is_reparse(path):
            raise OSError("backup checkpoint is missing")
        checkpoint = self._strict_json(self._read_bounded(path, MAX_MANIFEST_BYTES))
        self._require_format_version(checkpoint.get("format_version"),
                                     CHECKPOINT_FORMAT_VERSION,
                                     "backup checkpoint")
        if checkpoint.get("kind") != "backup" or checkpoint.get("package_path") != str(package):
            raise OSError("backup checkpoint is not owned by this package")
        return checkpoint

    def _write_checkpoint(self, package, checkpoint, expected_identity=None):
        self._assert_package_identity(package, expected_identity)
        self._write_json(package / ".fthr-backup-state.json", checkpoint)

    @staticmethod
    def _checkpoint_options(checkpoint):
        options = checkpoint.get("options") if isinstance(checkpoint, dict) else None
        if (not isinstance(options, dict)
                or set(options) != {"include_imported", "include_trash"}
                or type(options.get("include_imported")) is not bool
                or type(options.get("include_trash")) is not bool):
            raise ValueError("backup checkpoint options are invalid")
        return BackupOptions(**options)

    def _validate_destination(self, destination, *, roots):
        destination = Path(self._absolute(destination, "backup destination"))
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("backup destination must be a new directory")
        parent = destination.parent
        self._safe_parents(parent, Path(parent.anchor), allow_missing=False)
        if self._is_reparse(parent) or not parent.is_dir():
            raise OSError("backup destination parent is not a safe directory")
        for root in roots:
            if self._under(destination, root) or self._under(root, destination):
                raise OSError("backup destination overlaps a source root")
        return destination

    def _source_name(self, path, fallback):
        name = Path(path).name
        if (not name or name in {".", ".."} or name.endswith((".", " "))
                or "\\" in name or "/" in name or ":" in name or _RESERVED.fullmatch(name)):
            return fallback
        return name

    def _snapshot_records(self, snapshot, options, cancel):
        entries = list(snapshot.get("entries", ()))
        files = list(snapshot.get("files", ()))
        by_clip = {}
        for item in files:
            if not isinstance(item, dict):
                raise ValueError("snapshot contains an invalid file row")
            by_clip.setdefault(item.get("clip_id"), []).append(item)
        trash_by_clip = {item.get("clip_id"): item for item in snapshot.get("trash", ())
                         if isinstance(item, dict)}
        selected_entries = []
        selected_files = []
        excluded = []
        source_records = []
        object_sources = {}
        included_clips = set()
        for entry in entries:
            self._check_cancel(cancel)
            clip_id = entry.get("clip_id")
            clip_files = by_clip.get(clip_id, [])
            trash = trash_by_clip.get(clip_id)
            if trash and trash.get("status") == "TRASHED" and not options.include_trash:
                excluded.append({"path": str(clip_id), "reason": "trash excluded by options"})
                continue
            if not clip_files:
                excluded.append({"path": str(clip_id), "reason": "registered clip has no files"})
                continue
            usable_files = [item for item in clip_files
                            if item.get("binding_state") != BindingState.DETACHED.value]
            primaries = [item for item in usable_files
                         if item.get("role") == FileRole.PRIMARY.value
                         and item.get("binding_state") in {
                             BindingState.STAT_BOUND.value,
                             BindingState.CONTENT_VERIFIED.value,
                             BindingState.TRASHED.value}]
            if len(primaries) != 1:
                excluded.append({"path": str(clip_id), "reason": "registered clip has no primary"})
                continue
            primary = primaries[0]
            clip_selected = []
            clip_failed = None
            for item in usable_files:
                state = item.get("binding_state")
                if state in {BindingState.DETACHED.value, BindingState.MISSING.value,
                             BindingState.UNVERIFIED.value, BindingState.LEGACY_UNVERIFIED.value,
                             BindingState.CONFLICT.value, BindingState.PURGING.value}:
                    if item is primary:
                        clip_failed = "primary binding is not verified"
                    else:
                        excluded.append({"path": str(item.get("path")),
                                         "reason": "unverified registered member"})
                    continue
                path = item.get("path")
                root, source_kind = self._source_root_for(path, options.include_imported)
                if root is None:
                    if item is primary:
                        clip_failed = source_kind
                    else:
                        excluded.append({"path": str(path), "reason": source_kind})
                    continue
                try:
                    expected = item.get("identity")
                    digest, identity, size = self._hash_file(
                        path, expected=expected, cancel=cancel)
                except (OSError, ValueError) as error:
                    if item is primary:
                        clip_failed = str(error)
                    else:
                        excluded.append({"path": str(path), "reason": str(error)})
                    continue
                source_path = self._absolute(path, "registered source")
                base = self._source_name(source_path, str(item["file_id"]) + ".bin")
                relative = (f".fthr-trash/{trash['trash_id']}/{base}"
                            if trash and trash.get("status") == "TRASHED"
                            else f"media/{clip_id}/{base}")
                self._relative(relative)
                record = dict(item)
                record.update(identity=identity, content_sha256=digest,
                              _source_path=source_path, _source_kind=source_kind,
                              _relative=relative, _size=size)
                if trash and trash.get("status") == "TRASHED":
                    record["binding_state"] = BindingState.TRASHED.value
                clip_selected.append(record)
                object_sources.setdefault(digest, (source_path, identity, source_kind))
                if item is primary:
                    sidecar = manifest_path_for(source_path)
                    has_registered_sidecar = any(
                        other.get("role") == FileRole.SIDECAR.value
                        and self._key(other.get("path", "")) == self._key(sidecar)
                        for other in usable_files)
                    if not has_registered_sidecar and sidecar.is_file():
                        try:
                            sidecar_manifest = self._read_audio_manifest(source_path, cancel)
                        except InterruptedError:
                            raise
                        if sidecar_manifest is not None:
                            sidecar_identity = self._identity(sidecar)
                            sidecar_digest, sidecar_identity, sidecar_size = self._hash_file(
                                sidecar, expected=sidecar_identity, cancel=cancel)
                            sidecar_relative = relative + ".fthr-audio.json"
                            sidecar_record = {
                                "file_id": str(uuid5(UUID(clip_id), "sidecar:" + sidecar.name)),
                                "clip_id": clip_id, "role": FileRole.SIDECAR.value,
                                "binding_state": BindingState.CONTENT_VERIFIED.value,
                                "path": str(sidecar), "path_key": "", "identity": sidecar_identity,
                                "content_sha256": sidecar_digest, "provenance": "validated audio sidecar",
                                "_source_path": str(sidecar), "_source_kind": source_kind,
                                "_relative": sidecar_relative, "_size": sidecar_size,
                            }
                            if trash and trash.get("status") == "TRASHED":
                                sidecar_record["binding_state"] = BindingState.TRASHED.value
                            if sidecar_digest not in object_sources:
                                object_sources[sidecar_digest] = (str(sidecar), sidecar_identity, source_kind)
                            clip_selected.append(sidecar_record)

            if clip_failed or not any(item.get("role") == FileRole.PRIMARY.value for item in clip_selected):
                excluded.append({"path": str(entry.get("path") or clip_id),
                                 "reason": clip_failed or "primary source unavailable"})
                continue
            if trash and trash.get("status") == "TRASHED" and any(
                    item.get("role") not in {
                        FileRole.PRIMARY.value, FileRole.SIDECAR.value}
                    for item in clip_selected):
                excluded.append({"path": str(entry.get("path") or clip_id),
                                 "reason": "trash bundle contains unsupported members"})
                continue
            if trash and trash.get("status") == "TRASHED":
                selected_primary = next(item for item in clip_selected
                                        if item.get("role") == FileRole.PRIMARY.value)
                expected_sidecar = self._key(
                    manifest_path_for(selected_primary["_source_path"]))
                if any(item.get("role") == FileRole.SIDECAR.value
                       and self._key(item.get("_source_path", "")) != expected_sidecar
                       for item in clip_selected):
                    excluded.append({"path": str(entry.get("path") or clip_id),
                                     "reason": "trash sidecar is not adjacent"})
                    continue
            included_clips.add(clip_id)
            selected_entries.append(dict(entry))
            selected_files.extend(clip_selected)
            for item in clip_selected:
                source_records.append(item)
        selected_entries = [item for item in selected_entries if item.get("clip_id") in included_clips]
        selected_files = [item for item in selected_files if item.get("clip_id") in included_clips]
        selected_clip_ids = {item["clip_id"] for item in selected_entries}
        trash = [dict(item) for item in snapshot.get("trash", ())
                 if item.get("clip_id") in selected_clip_ids and item.get("status") != "PURGED"]
        if not options.include_trash:
            trash = []
        albums = [dict(item) for item in snapshot.get("albums", ())]
        memberships = [dict(item) for item in snapshot.get("memberships", ())
                       if item.get("clip_id") in selected_clip_ids]
        searches = [dict(item) for item in snapshot.get("searches", ())]
        covers = self._cover_records(selected_entries, selected_clip_ids, cancel, object_sources)
        return {
            "entries": selected_entries, "files": selected_files, "trash": trash,
            "albums": albums, "memberships": memberships, "searches": searches,
            "covers": covers, "excluded": excluded, "_object_sources": object_sources,
            "_source_records": source_records,
        }

    def _cover_records(self, entries, clip_ids, cancel, object_sources):
        records = []
        store = CoverAssetStore(Path(self.manager.backup_cover_root).with_suffix(""))
        for entry in entries:
            self._check_cancel(cancel)
            value = entry.get("value")
            reference, error = parse_cover_reference(
                value.get("library_cover") if isinstance(value, dict) else None)
            if error is not None:
                raise ValueError("metadata contains an invalid cover reference")
            if reference is None:
                continue
            clip_id = entry["clip_id"]
            if clip_id not in clip_ids:
                continue
            source = store.asset_path_for_read(clip_id, reference.revision)
            root = self.manager.backup_cover_root
            if not self._under(source, root) or self._is_reparse(Path(source)):
                raise OSError("referenced cover is outside the private cover namespace")
            try:
                digest, identity, size = self._hash_file(source, cancel=cancel)
                payload = self._read_bounded(source, 32 * 1024 * 1024, cancel=cancel)
                checked = cover_reference_from_png(payload, revision=reference.revision)
                if checked.to_dict() != reference.to_dict():
                    raise OSError("referenced cover does not match its metadata")
            except (OSError, ValueError) as error:
                raise OSError("referenced cover cannot be verified") from error
            object_sources[digest] = (str(source), identity, "cover")
            records.append({"clip_id": clip_id, "reference": reference.to_dict(),
                            "sha256": digest, "size": size})
        return records

    def _walk_unregistered(self, known_paths, options, cancel):
        result = []
        known = {self._key(path) for path in known_paths}
        known.update(self._key(manifest_path_for(path)) for path in known_paths)
        database = self._key(getattr(self.manager, "_database", ""))
        technical_names = {name.casefold() for name in _TECHNICAL_DIRS}
        cover_root = getattr(self.manager, "backup_cover_root", None)
        cover_root_key = self._key(cover_root) if cover_root else None
        roots = [self.owned_root]
        if options.include_imported:
            roots.extend(self.imported_roots)
        stack = [Path(root) for root in roots]
        visited = set()
        while stack:
            root = stack.pop()
            self._check_cancel(cancel)
            root_key = self._key(root)
            if root_key in visited:
                continue
            visited.add(root_key)
            if is_pending_restore_directory(root):
                continue
            try:
                entries = list(os.scandir(root))
            except OSError:
                # Inaccessible source subtrees are excluded from the unregistered scan.
                continue
            for entry in entries:
                self._check_cancel(cancel)
                path = Path(entry.path)
                if self._is_reparse(path):
                    continue
                if entry.name.casefold() in technical_names:
                    continue
                if cover_root_key is not None and self._under(path, cover_root_key):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in {"Exported", "Shared"} or is_fthr_temporary_dir(entry.name):
                        continue
                    if is_pending_restore_directory(path):
                        continue
                    if (not options.include_imported
                            and any(self._under(path, imported) for imported in self.imported_roots)):
                        continue
                    stack.append(path)
                    continue
                if not is_library_media_path(entry.name):
                    continue
                if self._key(path) == database or self._key(path) in known:
                    continue
                try:
                    identity = self._identity(path)
                except OSError:
                    # Unsafe, vanished, or non-regular media is excluded conservatively.
                    continue
                relative_source = os.path.relpath(
                    path, self.owned_root if self._under(path, self.owned_root)
                    else next(root for root in self.imported_roots if self._under(path, root))
                ).replace(os.sep, "/")
                try:
                    relative_source = self._relative(relative_source, "unregistered source")
                except ValueError:
                    # A path that cannot be represented safely is not backed up.
                    continue
                result.append({"source_path": str(path), "identity": identity,
                               "relative": f"unregistered/{relative_source}"})
                if len(result) >= MAX_FILES:
                    raise OSError("backup contains too many files")
        return result

    def _build_manifest(self, package, options, cancel, progress, reserve=None,
                        checkpoint=None):
        self._assert_roots()
        self._assert_current_root()
        snapshot = self.manager.export_backup_snapshot()
        known_paths = [item.get("path") for item in snapshot.get("files", ())
                       if isinstance(item, dict) and item.get("path")]
        unregistered = self._walk_unregistered(known_paths, options, cancel)
        claims = [path for path in known_paths
                   if self._source_root_for(path, options.include_imported)[0] is not None]
        claims.extend(item["source_path"] for item in unregistered)
        for entry in snapshot.get("entries", ()):
            value = entry.get("value") if isinstance(entry, dict) else None
            reference, error = parse_cover_reference(
                value.get("library_cover") if isinstance(value, dict) else None)
            if error is None and reference is not None:
                claims.append(str(self.manager.cover_asset_path_for_read(
                    entry["clip_id"], reference.revision)))
        for source in tuple(claims):
            sidecar = manifest_path_for(source)
            if sidecar.is_file():
                claims.append(str(sidecar))
        claims = tuple(sorted({self._absolute(path, "source claim") for path in claims}))
        if reserve is not None and not reserve(claims):
            raise OSError("media paths are busy; backup was not started")
        records = self._snapshot_records(snapshot, options, cancel)
        records.pop("_source_records")
        object_sources = records.pop("_object_sources")
        backup_uuid = str(uuid4())
        for item in unregistered:
            self._check_cancel(cancel)
            source_root, source_kind = self._source_root_for(
                item["source_path"], options.include_imported)
            if source_root is None:
                records["excluded"].append({"path": item["source_path"],
                                             "reason": source_kind})
                continue
            try:
                digest, identity, size = self._hash_file(
                    item["source_path"], expected=item["identity"], cancel=cancel)
            except OSError as error:
                records["excluded"].append({"path": item["source_path"], "reason": str(error)})
                continue
            clip_id = str(uuid5(UUID(backup_uuid), "unregistered-clip:" + item["relative"]))
            file_id = str(uuid5(UUID(backup_uuid), "unregistered-file:" + item["relative"]))
            basename = self._source_name(item["source_path"], file_id + ".bin")
            relative = f"media/{clip_id}/{basename}"
            records["entries"].append({"clip_id": clip_id, "path": item["source_path"], "value": {}})
            records["files"].append({
                "file_id": file_id, "clip_id": clip_id, "role": FileRole.PRIMARY.value,
                "binding_state": BindingState.CONTENT_VERIFIED.value,
                "path": item["source_path"], "path_key": self._key(item["source_path"]),
                "identity": identity, "content_sha256": digest,
                "provenance": "unregistered owned media", "_source_path": item["source_path"],
                "_source_kind": source_kind,
                "_relative": relative, "_size": size,
            })
            object_sources.setdefault(digest, (item["source_path"], identity, source_kind))
            sidecar = manifest_path_for(item["source_path"])
            sidecar_manifest = self._read_audio_manifest(item["source_path"], cancel)
            if sidecar_manifest is not None:
                sidecar_identity = self._identity(sidecar)
                sidecar_digest, sidecar_identity, sidecar_size = self._hash_file(
                    sidecar, expected=sidecar_identity, cancel=cancel)
                sidecar_id = str(uuid5(UUID(backup_uuid), "unregistered-sidecar:" + item["relative"]))
                records["files"].append({
                    "file_id": sidecar_id, "clip_id": clip_id, "role": FileRole.SIDECAR.value,
                    "binding_state": BindingState.CONTENT_VERIFIED.value,
                    "path": str(sidecar), "path_key": self._key(sidecar),
                    "identity": sidecar_identity, "content_sha256": sidecar_digest,
                    "provenance": "validated audio sidecar", "_source_path": str(sidecar),
                    "_source_kind": source_kind, "_relative": relative + ".fthr-audio.json",
                    "_size": sidecar_size,
                })
                object_sources.setdefault(sidecar_digest, (
                    str(sidecar), sidecar_identity, source_kind))
        # The manifest stores only relative target paths; absolute source paths
        # remain confined to the private checkpoint source evidence.
        primary_relative = {
            row["clip_id"]: row["_relative"] for row in records["files"]
            if row.get("role") == FileRole.PRIMARY.value
        }
        trash_by_clip = {row.get("clip_id"): row for row in records["trash"]}
        for entry in records["entries"]:
            clip_id = entry["clip_id"]
            entry["path"] = (None if trash_by_clip.get(clip_id, {}).get("status") == "TRASHED"
                              else primary_relative.get(clip_id))
        manifest_trash = []
        for item in records["trash"]:
            clip_id = item["clip_id"]
            primary = primary_relative.get(clip_id)
            if primary is None:
                continue
            name = Path(primary).name
            if item.get("status") == "TRASHED":
                stored = next((row["_relative"] for row in records["files"]
                               if row.get("clip_id") == clip_id
                               and row.get("role") == FileRole.PRIMARY.value), primary)
                original = f"media/{clip_id}/{name}"
            else:
                stored, original = primary, primary
            manifest_trash.append({"trash_id": item["trash_id"], "clip_id": clip_id,
                                   "status": item["status"], "created_utc": item["created_utc"],
                                   "original_path": original, "stored_path": stored,
                                   "members": item.get("members", []), "error": item.get("error")})
        records["trash"] = manifest_trash
        object_rows = []
        for digest, (_source, identity, source_kind) in object_sources.items():
            object_rows.append({"sha256": digest, "size": identity["size"],
                                "source_kind": source_kind, "relative_path": f"objects/{digest}"})
        object_rows.sort(key=lambda row: (row["source_kind"] != "cover", row["sha256"]))
        files = []
        for item in records["files"]:
            row = {key: value for key, value in item.items() if not key.startswith("_")}
            row["relative_path"] = item["_relative"]
            row["path_key"] = item["_relative"]
            row["object_sha256"] = item["content_sha256"]
            row.pop("path", None)
            row["source_identity"] = item["identity"]
            files.append(row)
        file_rows_by_clip = {}
        for row in files:
            file_rows_by_clip.setdefault(row["clip_id"], []).append(row)
        for trash_row in manifest_trash:
            trash_row["members"] = [
                {key: member[key] for key in (
                    "file_id", "role", "relative_path", "binding_state",
                    "identity", "content_sha256", "provenance")}
                for member in sorted(file_rows_by_clip.get(trash_row["clip_id"], ()),
                                     key=lambda item: item["file_id"])
            ]
        entries = []
        for item in records["entries"]:
            row = {"clip_id": item["clip_id"], "path": item.get("path"),
                   "value": item.get("value", {})}
            entries.append(row)
        manifest = {
            "format_version": BACKUP_FORMAT_VERSION,
            "backup_uuid": backup_uuid,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "source_root": {"identity": self._root_identity(self.owned_root)},
            "options": {"include_imported": options.include_imported,
                        "include_trash": options.include_trash},
            "entries": entries,
            "files": files,
            "trash": records["trash"],
            "albums": records["albums"],
            "memberships": records["memberships"],
            "searches": records["searches"],
            "covers": records["covers"],
            "assets": [],
            "objects": object_rows,
            "excluded": records["excluded"],
        }
        if len(files) + len(manifest["assets"]) > MAX_FILES:
            raise OSError("backup contains too many files")
        self._validate_manifest(manifest)
        evidence = {}
        for item in records["files"]:
            evidence.setdefault(item["content_sha256"], {})[item["_source_path"]] = item["identity"]
        for item in unregistered:
            if "sha256" in item:
                evidence.setdefault(item["sha256"], {})[item["source_path"]] = item["identity"]
        for digest, (source, identity, _kind) in object_sources.items():
            evidence.setdefault(digest, {}).setdefault(source, identity)
        source_map = {}
        for digest, (source, identity, kind) in object_sources.items():
            aliases = [{"path": path, "identity": alias_identity}
                       for path, alias_identity in sorted(evidence.get(digest, {}).items())]
            source_map[digest] = {"path": source, "identity": identity, "kind": kind,
                                 "aliases": aliases}
        checkpoint = {
            "format_version": CHECKPOINT_FORMAT_VERSION, "kind": "backup",
            "package_path": str(package), "backup_uuid": manifest["backup_uuid"],
            "source_root": {"path": self.owned_root, "identity": manifest["source_root"]["identity"]},
            "options": manifest["options"], "manifest": manifest,
            "source_map": source_map, "completed_objects": [], "phase": "copying",
        }
        return manifest, checkpoint

    @classmethod
    def _validate_manifest(cls, manifest):
        if not isinstance(manifest, dict):
            raise ValueError("unsupported backup manifest version")
        cls._require_format_version(manifest.get("format_version"),
                                    BACKUP_FORMAT_VERSION,
                                    "backup manifest")
        expected_keys = {
            "format_version", "backup_uuid", "created_utc", "source_root", "options",
            "entries", "files", "trash", "albums", "memberships", "searches",
            "covers", "assets", "objects", "excluded",
        }
        if set(manifest) != expected_keys:
            raise ValueError("backup manifest has unknown or missing fields")
        cls._uuid(manifest.get("backup_uuid"), "backup_uuid")
        if not isinstance(manifest.get("created_utc"), str) or not manifest["created_utc"]:
            raise ValueError("backup manifest timestamp is invalid")
        source_root = manifest.get("source_root")
        if not isinstance(source_root, dict) or set(source_root) != {"identity"}:
            raise ValueError("backup source root evidence is invalid")
        identity = source_root["identity"]
        if (not isinstance(identity, dict)
                or set(identity) != {"st_dev", "st_ino", "size", "mtime_ns"}
                or any(type(identity.get(key)) is not int for key in identity)):
            raise ValueError("backup source root identity is invalid")
        options = manifest.get("options")
        if (not isinstance(options, dict) or set(options) != {"include_imported", "include_trash"}
                or type(options["include_imported"]) is not bool
                or type(options["include_trash"]) is not bool):
            raise ValueError("backup options are invalid")
        for name in ("entries", "files", "trash", "albums", "memberships", "searches",
                     "covers", "assets", "objects", "excluded"):
            if not isinstance(manifest.get(name), list):
                raise ValueError(f"manifest {name} must be a list")
        for item in manifest["excluded"]:
            if (not isinstance(item, dict) or set(item) != {"path", "reason"}
                    or not isinstance(item["path"], str)
                    or not isinstance(item["reason"], str)):
                raise ValueError("invalid backup exclusion row")
        if manifest["assets"]:
            # v1 restore records have no asset binding path; accepting these rows
            # would report a completed backup while silently dropping their bytes.
            raise ValueError("backup v1 does not support unbound assets")
        if not options["include_trash"] and manifest["trash"]:
            raise ValueError("backup includes Trash while Trash is disabled")
        if len(manifest["files"]) + len(manifest["assets"]) > MAX_FILES:
            raise ValueError("manifest contains too many files")
        if len(manifest["objects"]) > MAX_FILES:
            raise ValueError("manifest contains too many objects")
        entry_ids = set()
        entry_paths = []
        entries_by_id = {}
        cover_refs = set()
        for entry in manifest["entries"]:
            if not isinstance(entry, dict) or set(entry) != {"clip_id", "path", "value"}:
                raise ValueError("invalid backup entry row")
            clip_id = cls._uuid(entry["clip_id"], "backup clip_id")
            if clip_id in entry_ids:
                raise ValueError("duplicate backup clip id")
            entry_ids.add(clip_id)
            value = entry["value"]
            if not isinstance(value, dict):
                raise ValueError("backup metadata value must be an object")
            cls._validate_json(value)
            if entry["path"] is not None:
                path = cls._relative(entry["path"], "backup entry path")
                entry_paths.append(path)
            entries_by_id[clip_id] = entry
            reference, error = parse_cover_reference(value.get("library_cover"))
            if error is not None:
                raise ValueError("backup metadata has an invalid cover reference")
            if reference is not None:
                cover_refs.add((clip_id, reference.revision))
        for obj in manifest["objects"]:
            if (not isinstance(obj, dict)
                    or set(obj) != {"sha256", "size", "source_kind", "relative_path"}):
                raise ValueError("invalid backup object row")
            digest = cls._digest(obj.get("sha256"), "object sha256")
            if obj.get("relative_path") != f"objects/{obj['sha256']}":
                raise ValueError("backup object path is not hash-addressed")
            cls._relative(obj["relative_path"])
            if type(obj.get("size")) is not int or obj["size"] < 0:
                raise ValueError("backup object size is invalid")
            if not isinstance(obj.get("source_kind"), str):
                raise ValueError("backup object source kind is invalid")
        object_ids = {row["sha256"] for row in manifest["objects"]}
        if len(object_ids) != len(manifest["objects"]):
            raise ValueError("duplicate backup object")
        file_ids = set()
        file_paths = []
        files_by_clip = {}
        for item in manifest["files"]:
            required = {"file_id", "clip_id", "role", "binding_state", "identity",
                        "content_sha256", "provenance", "relative_path", "object_sha256",
                        "source_identity", "path_key"}
            if not isinstance(item, dict) or set(item) != required:
                raise ValueError("invalid backup file row")
            file_id = cls._uuid(item["file_id"], "backup file_id")
            clip_id = cls._uuid(item["clip_id"], "backup file clip_id")
            if file_id in file_ids or clip_id not in entry_ids:
                raise ValueError("backup file references an unknown or duplicate id")
            file_ids.add(file_id)
            if item["role"] not in {role.value for role in FileRole}:
                raise ValueError("backup file role is invalid")
            if item["binding_state"] not in {
                    BindingState.STAT_BOUND.value, BindingState.CONTENT_VERIFIED.value,
                    BindingState.TRASHED.value}:
                raise ValueError("backup file binding state is invalid")
            relative = item.get("relative_path")
            cls._relative(relative)
            if item.get("path_key") != relative:
                raise ValueError("backup file path_key must be its relative path")
            file_paths.append(relative)
            object_digest = cls._digest(item.get("object_sha256"), "file object sha256")
            if object_digest not in object_ids:
                raise ValueError("backup file references a missing object")
            for evidence in (item.get("identity"), item.get("source_identity")):
                if (not isinstance(evidence, dict)
                        or set(evidence) != {"st_dev", "st_ino", "size", "mtime_ns"}
                        or any(type(evidence.get(key)) is not int for key in evidence)):
                    raise ValueError("backup file identity is invalid")
            if item["content_sha256"] != object_digest:
                raise ValueError("backup file digest does not match its object")
            if item["provenance"] is not None and not isinstance(item["provenance"], str):
                raise ValueError("backup file provenance is invalid")
            files_by_clip.setdefault(clip_id, []).append(item)
        if len(file_ids) != len(manifest["files"]):
            raise ValueError("duplicate backup file id")
        for clip_id, entry in entries_by_id.items():
            members = files_by_clip.get(clip_id, [])
            primary = [row for row in members if row["role"] == FileRole.PRIMARY.value]
            if len(primary) != 1:
                raise ValueError("backup clip requires exactly one primary")
            if entry["path"] is not None and entry["path"] != primary[0]["relative_path"]:
                raise ValueError("backup entry path does not match primary")
            if entry["path"] is None and any(
                    row["binding_state"] != BindingState.TRASHED.value for row in members):
                raise ValueError("backup trash entry contains an active file")
            if entry["path"] is not None and any(
                    row["binding_state"] == BindingState.TRASHED.value for row in members):
                raise ValueError("active backup entry contains a TRASHED file")
        for item in manifest["assets"]:
            if (not isinstance(item, dict)
                    or set(item) != {"relative_path", "object_sha256", "source_identity"}):
                raise ValueError("invalid backup asset row")
            cls._relative(item["relative_path"])
            if cls._digest(item["object_sha256"], "asset object sha256") not in object_ids:
                raise ValueError("backup asset references a missing object")
        trash_ids = set()
        trash_by_clip = {}
        for item in manifest["trash"]:
            required = {"trash_id", "clip_id", "status", "created_utc", "original_path",
                        "stored_path", "members", "error"}
            if not isinstance(item, dict) or set(item) != required:
                raise ValueError("invalid backup trash row")
            trash_id = cls._uuid(item["trash_id"], "backup trash_id")
            clip_id = cls._uuid(item["clip_id"], "backup trash clip_id")
            if trash_id in trash_ids or clip_id not in entry_ids:
                raise ValueError("backup trash references an unknown or duplicate id")
            trash_ids.add(trash_id)
            trash_by_clip.setdefault(clip_id, []).append(item)
            if item["status"] not in {"TRASHED", "RESTORED", "PURGED"}:
                raise ValueError("backup contains unfinished trash")
            if not isinstance(item["created_utc"], str) or not item["created_utc"]:
                raise ValueError("backup trash timestamp is invalid")
            original = cls._relative(item["original_path"], "backup trash original path")
            stored = cls._relative(item["stored_path"], "backup trash stored path")
            if item["status"] == "TRASHED" and original.casefold().split("/").count(
                    ".fthr-trash"):
                raise ValueError("backup trash original path is inside its bundle")
            if not isinstance(item["members"], list):
                raise ValueError("backup trash members must be canonical list")
            cls._validate_json(item["members"])
            if item["status"] == "TRASHED":
                clip_files = files_by_clip.get(clip_id, [])
                primary = [row for row in clip_files
                           if row["role"] == FileRole.PRIMARY.value]
                if len(primary) != 1 or stored != primary[0]["relative_path"]:
                    raise ValueError("backup trash stored path does not match primary")
                stored_suffix = cls._trash_suffix(stored, trash_id)
                if stored_suffix is None or stored != stored_suffix:
                    raise ValueError("backup trash path is not a canonical terminal bundle path")
                if any(row["binding_state"] != BindingState.TRASHED.value
                       for row in clip_files):
                    raise ValueError("backup Trash files must all be TRASHED")
                canonical_members = [{key: member[key] for key in (
                    "file_id", "role", "relative_path", "binding_state",
                    "identity", "content_sha256", "provenance")}
                    for member in sorted(clip_files, key=lambda row: row["file_id"])]
                if item["members"] != canonical_members:
                    raise ValueError("backup trash members are not canonical")
                for member in item["members"]:
                    member_suffix = cls._trash_suffix(member["relative_path"], trash_id)
                    if (member_suffix is None
                            or member["relative_path"] != member_suffix):
                        raise ValueError("backup Trash member escaped its bundle")
            if item["error"] is not None and not isinstance(item["error"], str):
                raise ValueError("backup trash error is invalid")
        for clip_id, entry in entries_by_id.items():
            current = [row for row in trash_by_clip.get(clip_id, ()) if row["status"] == "TRASHED"]
            if entry["path"] is None and len(current) != 1:
                raise ValueError("backup trash entry requires exactly one current item")
            if entry["path"] is not None and current:
                raise ValueError("active backup entry has a current trash item")
        album_ids = set()
        for item in manifest["albums"]:
            if (not isinstance(item, dict)
                    or set(item) != {"album_id", "name", "created_utc"}):
                raise ValueError("invalid backup album row")
            album_id = cls._uuid(item["album_id"], "backup album_id")
            if album_id in album_ids or not isinstance(item["name"], str):
                raise ValueError("duplicate or invalid backup album")
            album_ids.add(album_id)
        for item in manifest["memberships"]:
            if (not isinstance(item, dict) or set(item) != {"album_id", "clip_id"}
                    or item["album_id"] not in album_ids or item["clip_id"] not in entry_ids):
                raise ValueError("backup membership references an unknown id")
        search_ids = set()
        for item in manifest["searches"]:
            if (not isinstance(item, dict)
                    or set(item) != {"search_id", "name", "created_utc", "criteria"}):
                raise ValueError("invalid backup search row")
            search_id = cls._uuid(item["search_id"], "backup search_id")
            if search_id in search_ids or not isinstance(item["criteria"], dict):
                raise ValueError("duplicate or invalid backup search")
            search_ids.add(search_id)
            cls._validate_json(item["criteria"])
        cover_ids = set()
        for item in manifest["covers"]:
            if (not isinstance(item, dict)
                    or set(item) != {"clip_id", "reference", "sha256", "size"}):
                raise ValueError("invalid cover row")
            clip_id = cls._uuid(item["clip_id"], "backup cover clip_id")
            reference, error = parse_cover_reference(item["reference"])
            if error is not None or reference is None or clip_id not in entry_ids:
                raise ValueError("invalid backup cover reference")
            digest = cls._digest(item["sha256"], "cover sha256")
            if digest not in object_ids or type(item["size"]) is not int or item["size"] <= 0:
                raise ValueError("invalid backup cover object")
            if (clip_id, reference.revision) in cover_ids:
                raise ValueError("duplicate backup cover reference")
            cover_ids.add((clip_id, reference.revision))
        if cover_ids != cover_refs:
            raise ValueError("backup cover references are incomplete")
        referenced_objects = {
            row["object_sha256"] for row in manifest["files"]
        } | {row["sha256"] for row in manifest["covers"]}
        if referenced_objects != object_ids:
            raise ValueError("backup object set is not exactly referenced by files and covers")
        cls._relative_unique(entry_paths)
        cls._relative_unique(file_paths + [item["relative_path"]
                             for item in manifest["assets"]])

    def _manifest_index(self, manifest):
        """Build one operation-local index for source and Trash classification."""

        covers_by_path = {}
        cover_root = Path(self._absolute(self.manager.backup_cover_root, "cover root"))
        for row in manifest.get("covers", ()):
            reference, error = parse_cover_reference(row.get("reference"))
            if error is not None or reference is None:
                raise ValueError("backup contains an invalid cover reference")
            expected = cover_root / row["clip_id"] / f"{reference.revision}.png"
            key = self._key(expected)
            previous = covers_by_path.get(key)
            if previous is not None and previous != row["sha256"]:
                raise OSError("cover source path maps to multiple digests")
            covers_by_path[key] = row["sha256"]
        files_by_clip = {}
        files_by_relative = {}
        for row in manifest.get("files", ()):
            files_by_clip.setdefault(row["clip_id"], []).append(row)
            files_by_relative[row["relative_path"].casefold()] = row
        trash_by_clip = {}
        trash_by_id = {}
        for row in manifest.get("trash", ()):
            trash_by_id[row["trash_id"]] = row
            if row.get("status") == "TRASHED":
                trash_by_clip[row["clip_id"]] = row
        trash_paths = {}
        for trash_id, row in trash_by_id.items():
            if row.get("status") != "TRASHED":
                continue
            for file_row in files_by_clip.get(row["clip_id"], ()):
                suffix = self._trash_suffix(file_row.get("relative_path"), trash_id)
                if suffix is not None:
                    key = suffix.casefold()
                    previous = trash_paths.get(key)
                    if previous is not None and previous != file_row["object_sha256"]:
                        raise OSError("Trash source path maps to multiple digests")
                    trash_paths[key] = file_row["object_sha256"]
        return {
            "covers_by_path": covers_by_path,
            "files_by_clip": files_by_clip,
            "files_by_relative": files_by_relative,
            "trash_by_clip": trash_by_clip,
            "trash_by_id": trash_by_id,
            "trash_paths": trash_paths,
            "include_trash": bool(manifest.get("options", {}).get("include_trash", True)),
        }

    def _source_evidence_class(self, path, manifest, include_imported, cancel=None,
                               *, index=None, expected_digest=None):
        """Classify one checkpoint source path without trusting its digest."""

        self._check_cancel(cancel)
        source_path = Path(self._absolute(path, "checkpoint source"))
        index = index or self._manifest_index(manifest)
        cover_root = Path(self._absolute(self.manager.backup_cover_root, "cover root"))
        source_key = self._key(source_path)
        if source_key in index["covers_by_path"]:
            if (expected_digest is not None
                    and index["covers_by_path"][source_key] != expected_digest):
                raise OSError("cover source digest is not referenced by this object")
            return "cover", cover_root
        try:
            owned_relative = Path(os.path.relpath(
                source_path, self.owned_root)).as_posix()
        except ValueError:
            owned_relative = ""
        owned_trash_suffix = None
        if self._under(source_path, self.owned_root):
            relative_parts = owned_relative.split("/") if owned_relative else []
            if len(relative_parts) >= 3 and relative_parts[-3] == ".fthr-trash":
                owned_trash_suffix = self._trash_suffix(
                    owned_relative, relative_parts[-2])
                if owned_trash_suffix is None:
                    raise OSError("Trash source has an unsafe technical ancestor")
                if owned_trash_suffix.casefold() not in index["trash_paths"]:
                    owned_trash_suffix = None
                elif not index["include_trash"]:
                    raise OSError("Trash source is excluded by backup options")
        for part in source_path.parts:
            if not self._is_technical_component(part):
                continue
            if part.casefold() == ".fthr-trash" and owned_trash_suffix is not None:
                continue
            raise OSError("technical source paths are not backup media")
        if owned_trash_suffix is not None and expected_digest is not None:
            if index["trash_paths"].get(owned_trash_suffix.casefold()) != expected_digest:
                raise OSError("Trash source digest is not referenced by this object")
        name = source_path.name.casefold()
        if name.endswith(".fthr-audio.json"):
            media_name = source_path.name[:-len(".fthr-audio.json")]
            media = source_path.with_name(media_name)
            if (not is_library_media_path(media)
                    or self._key(manifest_path_for(media)) != source_key):
                raise OSError("audio source is not an adjacent sidecar")
            root, kind = self._source_root_for(media, include_imported)
            if root is None:
                raise OSError("audio source is outside the configured roots")
            if self._read_audio_manifest(media, cancel) is None:
                raise OSError("audio sidecar is not hash-bound to its media")
            return kind, root
        if not is_library_media_path(source_path):
            raise OSError("checkpoint source is not supported library media")
        root, kind = self._source_root_for(source_path, include_imported)
        if root is None:
            raise OSError("checkpoint source is outside the configured roots")
        return kind, root

    def _validate_source_map(self, manifest, source_map, *, include_imported=False,
                             cancel=None, index=None):
        """Validate checkpoint source evidence against manifest references."""

        if not isinstance(source_map, dict):
            raise OSError("checkpoint source map is invalid")
        index = index or self._manifest_index(manifest)
        object_ids = {row["sha256"] for row in manifest["objects"]}
        if set(source_map) != object_ids:
            raise OSError("checkpoint source map does not match manifest objects")
        for digest in object_ids:
            self._check_cancel(cancel)
            source = source_map[digest]
            if (not isinstance(source, dict)
                    or set(source) != {"path", "identity", "kind", "aliases"}):
                raise OSError("checkpoint source evidence has unknown fields")
            if source["kind"] not in {"owned", "imported", "cover"}:
                raise OSError("checkpoint source kind is invalid")
            if not isinstance(source["aliases"], list):
                raise OSError("checkpoint source aliases are invalid")
            for evidence in [source["identity"], *source["aliases"]]:
                if isinstance(evidence, dict) and "identity" in evidence:
                    identity = evidence["identity"]
                else:
                    identity = evidence
                if (not isinstance(identity, dict)
                        or set(identity) != {"st_dev", "st_ino", "size", "mtime_ns"}
                        or any(type(identity.get(key)) is not int for key in identity)):
                    raise OSError("checkpoint source identity is invalid")
            source_class, _root = self._source_evidence_class(
                source["path"], manifest, include_imported, cancel, index=index,
                expected_digest=digest)
            if source_class != source["kind"]:
                raise OSError("checkpoint source kind does not match its path")
            if source_class == "cover":
                expected = self._key(source["path"])
                if index["covers_by_path"].get(expected) != digest:
                    raise OSError("checkpoint cover source digest is not referenced")
            for alias in source["aliases"]:
                if (not isinstance(alias, dict)
                        or set(alias) != {"path", "identity"}):
                    raise OSError("checkpoint source alias is invalid")
                alias_class, _alias_root = self._source_evidence_class(
                    alias["path"], manifest, include_imported, cancel, index=index,
                    expected_digest=digest)
                if alias_class not in {"owned", "imported", "cover"}:
                    raise OSError("checkpoint source alias class is invalid")

    def _copy_manifest_objects(self, package, manifest, source_map, completed, cancel,
                               progress, *, include_imported=False,
                               package_identity=None):
        package_identity = self._assert_package_identity(package, package_identity)
        manifest_index = self._manifest_index(manifest)
        self._validate_source_map(manifest, source_map,
                                  include_imported=include_imported, cancel=cancel,
                                  index=manifest_index)
        objects = manifest["objects"]
        for object_index, row in enumerate(objects):
            self._check_cancel(cancel)
            self._assert_package_identity(package, package_identity)
            digest = row["sha256"]
            destination = package / "objects" / digest
            source = source_map.get(digest)
            if not isinstance(source, dict):
                raise OSError("checkpoint source evidence is missing")
            source_path = source.get("path")
            identity = source.get("identity")
            _source_kind, root = self._source_evidence_class(
                source_path, manifest, include_imported, cancel, index=manifest_index,
                expected_digest=digest)
            if root is None:
                raise OSError("checkpoint source is no longer authorized")
            self._safe_parents(Path(source_path), Path(root), allow_missing=False)
            source_digest, _source_identity, source_size = self._hash_file(
                source_path, expected=identity, cancel=cancel)
            if source_digest != digest or source_size != row["size"]:
                raise OSError("backup source digest or size changed")
            for alias in source.get("aliases", ()):
                alias_path = alias.get("path")
                _alias_kind, alias_root = self._source_evidence_class(
                    alias_path, manifest, include_imported, cancel, index=manifest_index,
                    expected_digest=digest)
                if alias_root is None:
                    raise OSError("backup source alias is no longer authorized")
                self._safe_parents(Path(alias_path), Path(alias_root), allow_missing=False)
                alias_digest, _alias_identity, _alias_size = self._hash_file(
                    alias_path, expected=alias.get("identity"), cancel=cancel)
                if alias_digest != digest:
                    raise OSError("backup source alias digest changed")
            if destination.is_file() and not self._is_reparse(destination):
                existing_digest, _existing_identity, existing_size = self._hash_file(
                    destination, cancel=cancel)
                if existing_digest != digest or existing_size != row["size"]:
                    raise OSError("checkpoint object failed revalidation")
            else:
                self._copy_object(source_path, destination, identity, cancel,
                                  expected_digest=digest)
            completed.add(digest)
            self._emit(progress, "copy", object_index + 1, len(objects),
                       row.get("size", 0), digest)
        return completed

    def _result_from_manifest(self, package, manifest):
        excluded = tuple(f"{item.get('path', '')}: {item.get('reason', 'excluded')}"
                         for item in manifest.get("excluded", ()))
        return BackupResult(str(package), True, len(manifest["entries"]),
                            len(manifest["files"]) + len(manifest["assets"]), excluded)

    def create_backup(self, destination, *, options=None, cancel=None, progress=None):
        options = options or BackupOptions()
        if not isinstance(options, BackupOptions):
            raise TypeError("options must be BackupOptions")
        package = self._validate_destination(destination,
                                             roots=(self.owned_root, *self.imported_roots))
        package.mkdir(parents=False)
        package_identity = self._package_identity(package)
        claims = []
        try:
            initial = {"format_version": CHECKPOINT_FORMAT_VERSION, "kind": "backup",
                       "package_path": str(package), "backup_uuid": str(uuid4()),
                       "source_root": {"path": self.owned_root,
                                       "identity": self._root_identity(self.owned_root)},
                       "options": {"include_imported": options.include_imported,
                                   "include_trash": options.include_trash},
                       "manifest": None, "source_map": {}, "completed_objects": [],
                       "phase": "preparing"}
            self._write_checkpoint(package, initial, package_identity)
            def reserve(paths):
                if not self._reserve(paths):
                    return False
                claims.extend(paths)
                return True

            manifest, checkpoint = self._build_manifest(
                package, options, cancel, progress, reserve=reserve)
            self._write_checkpoint(package, checkpoint, package_identity)

            completed = self._copy_manifest_objects(
                package, manifest, checkpoint["source_map"], set(), cancel, progress,
                include_imported=options.include_imported,
                package_identity=package_identity)
            checkpoint["completed_objects"] = sorted(completed)
            checkpoint["phase"] = "manifest"
            self._write_checkpoint(package, checkpoint, package_identity)
            self._assert_package_identity(package, package_identity)
            manifest_bytes = self._write_json(package / "manifest.json", manifest)
            marker = {"format_version": BACKUP_FORMAT_VERSION,
                      "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest()}
            self._assert_package_identity(package, package_identity)
            self._write_json(package / "complete.json", marker)
            checkpoint["phase"] = "complete"
            self._write_checkpoint(package, checkpoint, package_identity)
            return self._result_from_manifest(package, manifest)
        finally:
            if claims:
                self._release(tuple(claims))

    def resume_backup(self, package, *, cancel=None, progress=None):
        self._check_cancel(cancel)
        self._assert_current_root()
        package = self._package_path(package)
        package_identity = self._package_identity(package)
        checkpoint = self._read_checkpoint(package)
        self._assert_package_identity(package, package_identity)
        self._assert_roots()
        if self._key(checkpoint["source_root"]["path"]) != self._key(self.owned_root):
            raise OSError("backup checkpoint belongs to another source root")
        if not self._same_stable_identity(
                self._stable_directory_identity(self.owned_root),
                checkpoint["source_root"]["identity"]):
            raise OSError("backup source root changed; resume refused")
        claims = []
        reserved = False
        try:
            if checkpoint.get("manifest") is None:
                options = self._checkpoint_options(checkpoint)

                def reserve(paths):
                    nonlocal reserved
                    if not self._reserve(paths):
                        return False
                    claims.extend(paths)
                    reserved = True
                    return True

                manifest, checkpoint = self._build_manifest(
                    package, options, cancel, progress, reserve=reserve)
            else:
                options = self._checkpoint_options(checkpoint)
                self._validate_manifest(checkpoint["manifest"])
                manifest = checkpoint["manifest"]
                if checkpoint.get("options") != manifest.get("options"):
                    raise OSError("backup checkpoint options differ from its manifest")
                if checkpoint.get("phase") != "complete":
                    self._validate_source_map(
                        manifest, checkpoint.get("source_map"),
                        include_imported=options.include_imported, cancel=cancel)
            if checkpoint.get("phase") == "complete":
                self._assert_package_identity(package, package_identity)
                verified = self.verify_backup(package, cancel=cancel, progress=progress)
                return self._result_from_manifest(package, verified.manifest)
            if not reserved:
                claims = [item["path"] for item in checkpoint.get("source_map", {}).values()]
                claims.extend(alias["path"]
                              for item in checkpoint.get("source_map", {}).values()
                              for alias in item.get("aliases", ()))
                claims = list(dict.fromkeys(claims))
                if not self._reserve(tuple(claims)):
                    raise OSError("media paths are busy; backup resume was not started")
                reserved = True

            completed = set(checkpoint.get("completed_objects", ()))
            completed = self._copy_manifest_objects(
                package, manifest, checkpoint["source_map"], completed, cancel, progress,
                include_imported=options.include_imported,
                package_identity=package_identity)
            checkpoint["completed_objects"] = sorted(completed)
            checkpoint["phase"] = "manifest"
            self._write_checkpoint(package, checkpoint, package_identity)
            self._assert_package_identity(package, package_identity)
            manifest_bytes = self._write_json(package / "manifest.json", manifest)
            self._assert_package_identity(package, package_identity)
            self._write_json(package / "complete.json", {
                "format_version": BACKUP_FORMAT_VERSION,
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest()})
            checkpoint["phase"] = "complete"
            self._write_checkpoint(package, checkpoint, package_identity)
            return self._result_from_manifest(package, manifest)
        finally:
            if reserved:
                self._release(tuple(claims))

    def verify_backup(self, package, *, cancel=None, progress=None):
        package = self._package_path(package)
        package_identity = self._package_identity(package)
        manifest_path = package / "manifest.json"
        complete_path = package / "complete.json"
        if (not manifest_path.is_file() or not complete_path.is_file()
                or self._is_reparse(manifest_path) or self._is_reparse(complete_path)):
            raise OSError("backup is incomplete")
        manifest_bytes = self._read_bounded(manifest_path, MAX_MANIFEST_BYTES)
        manifest = self._strict_json(manifest_bytes)
        self._validate_manifest(manifest)
        self._assert_package_identity(package, package_identity)
        marker = self._strict_json(self._read_bounded(complete_path, 1024 * 1024))
        self._require_format_version(marker.get("format_version"),
                                     BACKUP_FORMAT_VERSION, "backup completion")
        if marker.get("manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest():
            raise OSError("backup completion marker does not match the manifest")
        objects = {row["sha256"]: row for row in manifest["objects"]}
        if len(objects) != len(manifest["objects"]):
            raise ValueError("duplicate backup object")
        for index, digest in enumerate(sorted(objects)):
            self._check_cancel(cancel)
            self._assert_package_identity(package, package_identity)
            path = package / "objects" / digest
            if not path.is_file() or self._is_reparse(path):
                raise OSError("backup object is missing or unsafe")
            actual, _identity, size = self._hash_file(path, cancel=cancel)
            if actual != digest or size != objects[digest]["size"]:
                raise OSError("backup object digest or size mismatch")
            self._emit(progress, "verify", index + 1, len(objects), size, digest)
        return VerifiedBackup(str(package), marker["manifest_sha256"], manifest)

    def _restore_checkpoint_path(self, owned_root, restore_uuid):
        return Path(owned_root) / f".fthr-restore-{restore_uuid}.json"

    def _write_restore_marker(self, root, restore_uuid, manifest_sha256):
        marker = Path(root) / PENDING_RESTORE_MARKER
        self._safe_parents(marker.parent, Path(root), allow_missing=False)
        self._write_json(marker, {
            "format_version": BACKUP_FORMAT_VERSION,
            "restore_uuid": restore_uuid,
            "manifest_sha256": manifest_sha256,
        })

    def _cleanup_restore_marker(self, root, restore_uuid, manifest_sha256):
        """Remove only the marker owned by this restore, retaining a resume hint."""

        marker = Path(root) / PENDING_RESTORE_MARKER
        try:
            if not os.path.lexists(marker):
                return None
            if marker.is_symlink():
                return "pending restore marker is a link and remains protected"
            marker_identity = self._identity(marker)
            payload = self._strict_json(self._read_bounded(marker, 1024 * 1024))
            if not self._restore_marker_matches(payload, restore_uuid, manifest_sha256):
                return "pending restore marker ownership changed; resume required"
            root_identity = self._identity(root, regular=False)
            if self._identity(marker) != marker_identity:
                return "pending restore marker changed; resume required"
            marker.unlink()
            after = self._identity(root, regular=False)
            if (after["st_dev"], after["st_ino"]) != (
                    root_identity["st_dev"], root_identity["st_ino"]):
                return "pending restore marker cleanup changed its root identity"
            return None
        except (OSError, ValueError):
            return "pending restore marker could not be removed; resume required"

    @staticmethod
    def _restore_marker_matches(payload, restore_uuid, manifest_sha256):
        if not isinstance(payload, dict):
            return False
        if (type(payload.get("format_version")) is not int
                or payload.get("format_version") != BACKUP_FORMAT_VERSION):
            return False
        return (set(payload) == {"format_version", "restore_uuid", "manifest_sha256"}
                and payload.get("restore_uuid") == restore_uuid
                and payload.get("manifest_sha256") == manifest_sha256)

    def _require_restore_marker(self, root, restore_uuid, manifest_sha256):
        marker = Path(root) / PENDING_RESTORE_MARKER
        if marker.is_symlink() or not marker.is_file():
            raise OSError("published restore marker is missing")
        payload = self._strict_json(self._read_bounded(marker, 1024 * 1024))
        if not self._restore_marker_matches(payload, restore_uuid, manifest_sha256):
            raise OSError("published restore marker ownership changed")

    def _verify_published_tree(self, root, verified, restore_uuid, *, cancel=None):
        """Check every planned file, digest, ID reference, and owned marker."""

        self._check_cancel(cancel)
        self._require_restore_marker(root, restore_uuid, verified.manifest_sha256)
        expected = {PENDING_RESTORE_MARKER}
        clip_ids = {self._uuid(row["clip_id"], "published clip_id")
                    for row in verified.manifest["entries"]}
        file_ids = set()
        restore_index = self._restore_manifest_index(verified.manifest, restore_uuid)
        trash_map = restore_index["trash_map"]
        trash_by_clip = restore_index["trash_by_clip"]
        objects = {row["sha256"]: row for row in verified.manifest["objects"]}
        planned = {}
        for row in verified.manifest["files"]:
            self._check_cancel(cancel)
            file_id = self._uuid(row["file_id"], "published file_id")
            clip_id = self._uuid(row["clip_id"], "published file clip_id")
            if file_id in file_ids or clip_id not in clip_ids:
                raise OSError("published restore file IDs are not unique or registered")
            file_ids.add(file_id)
            relative = row["relative_path"]
            trash_row = trash_by_clip.get(row["clip_id"])
            if trash_row is not None:
                old_id = trash_row["trash_id"]
                prefix = f".fthr-trash/{old_id}/"
                if relative.startswith(prefix):
                    relative = relative.replace(prefix,
                                                f".fthr-trash/{trash_map[old_id]}/", 1)
                else:
                    relative = relative.replace(f"media/{row['clip_id']}/",
                                                f".fthr-trash/{trash_map[old_id]}/", 1)
            else:
                relative = relative.replace(
                    f"media/{row['clip_id']}/",
                    f"media/{str(uuid5(UUID(restore_uuid), 'clip:' + row['clip_id']))}/", 1)
            key = relative.casefold()
            expected.add(key)
            object_row = objects.get(row["object_sha256"])
            if object_row is None:
                raise OSError("published restore object reference is missing")
            planned[key] = (row, object_row)
        if len(planned) != len(verified.manifest["files"]):
            raise OSError("published restore paths are duplicated")
        stack = [Path(root)]
        seen = set()
        while stack:
            self._check_cancel(cancel)
            current = stack.pop()
            try:
                entries = list(os.scandir(current))
            except OSError as error:
                raise OSError("published restore tree cannot be inspected") from error
            for entry in entries:
                self._check_cancel(cancel)
                child = Path(entry.path)
                if self._is_reparse(child):
                    raise OSError("published restore tree contains a reparse point")
                if entry.is_dir(follow_symlinks=False):
                    stack.append(child)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    raise OSError("published restore tree contains a non-file")
                relative = child.relative_to(root).as_posix().casefold()
                if relative not in expected:
                    raise OSError("published restore tree contains an unexpected file")
                if relative == PENDING_RESTORE_MARKER:
                    continue
                planned_row = planned.get(relative)
                if planned_row is None or relative in seen:
                    raise OSError("published restore tree contains a duplicate file")
                row, object_row = planned_row
                actual, _identity, size = self._hash_file(child, cancel=cancel)
                if actual != row["object_sha256"] or size != object_row["size"]:
                    raise OSError("published restore file digest or size changed")
                seen.add(relative)
        if seen != set(planned):
            raise OSError("published restore tree is missing a planned file")

    @staticmethod
    def _restore_manifest_index(manifest, restore_uuid):
        trash_by_clip = {row["clip_id"]: row for row in manifest["trash"]
                         if row["status"] == "TRASHED"}
        trash_map = {row["trash_id"]: str(uuid5(UUID(restore_uuid),
                                                   "trash:" + row["trash_id"]))
                     for row in manifest["trash"]}
        files_by_clip = {}
        primary_by_clip = {}
        for row in manifest["files"]:
            files_by_clip.setdefault(row["clip_id"], []).append(row)
            if row["role"] == FileRole.PRIMARY.value:
                primary_by_clip[row["clip_id"]] = row
        return {"trash_by_clip": trash_by_clip, "trash_map": trash_map,
                "files_by_clip": files_by_clip, "primary_by_clip": primary_by_clip}

    def _restore_claims(self, verified, restore_uuid, destination_root):
        """Reserve every final path while staging through the DB commit."""

        destination_root = Path(destination_root)
        restore_index = self._restore_manifest_index(verified.manifest, restore_uuid)
        trash_map = restore_index["trash_map"]
        trash_by_clip = restore_index["trash_by_clip"]
        paths = [str(Path(verified.package_path)), str(destination_root)]
        for row in verified.manifest["files"]:
            relative = row["relative_path"]
            trash_row = trash_by_clip.get(row["clip_id"])
            if trash_row is not None:
                old_id = trash_row["trash_id"]
                prefix = f".fthr-trash/{old_id}/"
                if relative.startswith(prefix):
                    relative = relative.replace(prefix,
                                                f".fthr-trash/{trash_map[old_id]}/", 1)
                else:
                    relative = relative.replace(f"media/{row['clip_id']}/",
                                                f".fthr-trash/{trash_map[old_id]}/", 1)
            else:
                relative = relative.replace(
                    f"media/{row['clip_id']}/",
                    f"media/{str(uuid5(UUID(restore_uuid), 'clip:' + row['clip_id']))}/", 1)
            paths.append(str(self._restore_target(destination_root, relative)))
        clip_map = {entry["clip_id"]: str(uuid5(UUID(restore_uuid), "clip:" + entry["clip_id"]))
                    for entry in verified.manifest["entries"]}
        cover_store = CoverAssetStore(Path(self.manager.backup_cover_root).with_suffix(""))
        for row in verified.manifest["covers"]:
            reference, error = parse_cover_reference(row.get("reference"))
            if error is None and reference is not None and row["clip_id"] in clip_map:
                paths.append(str(cover_store.asset_path(clip_map[row["clip_id"]],
                                                        reference.revision)))
        return tuple(sorted(set(paths), key=self._key))

    def _restore_target(self, root, relative):
        relative = self._relative(relative)
        target = Path(root, *relative.split("/"))
        if not self._under(target, root):
            raise OSError("restore target escaped destination root")
        return target

    def _stage_object(self, package, digest, target, root, cancel, *, package_identity=None):
        package_identity = self._assert_package_identity(package, package_identity)
        source = package / "objects" / digest
        if not source.is_file() or self._is_reparse(source):
            raise OSError("restore object is missing")
        self._safe_parents(source, package, allow_missing=False)
        self._safe_parents(target.parent, root, allow_missing=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target_parent_identity = self._assert_stable_parent(target.parent)
        if target.exists() or target.is_symlink():
            observed = self._exclusive_identity(target)
            actual, hashed_identity, _size = self._hash_file(
                target, expected=observed, cancel=cancel)
            if actual == digest:
                self._assert_stable_parent(target.parent, target_parent_identity)
                observed = self._exclusive_identity(target)
                if not self._same_identity(observed, hashed_identity):
                    raise OSError("restore staging file changed after hash")
                return observed, target_parent_identity
            raise FileExistsError("restore staging path already contains another object")
        stage_container = Path(root).parent
        if self._is_reparse(stage_container):
            raise OSError("restore stage container is not a private directory")
        scratch = stage_container / ".scratch"
        self._safe_parents(scratch, stage_container, allow_missing=True)
        scratch.mkdir(parents=True, exist_ok=True)
        _actual, published_identity = self._copy_object(
            source, target, self._identity(source), cancel,
            expected_digest=digest, temporary_parent=scratch)
        self._assert_package_identity(package, package_identity)
        self._assert_stable_parent(target.parent, target_parent_identity)
        actual, hashed_identity, _size = self._hash_file(
            target, expected=published_identity, cancel=cancel)
        if actual != digest:
            raise OSError("restore staging object digest changed")
        return hashed_identity, target_parent_identity

    def _set_mtime_nofollow(self, path, timestamp, expected_identity):
        """Set one existing file timestamp without following a replacement link."""

        path = Path(path)
        if type(timestamp) is not int:
            raise OSError("staged restore timestamp is invalid")
        if os.name != "nt":
            os.utime(path, ns=(timestamp, timestamp), follow_symlinks=False)
            return
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
        create_file.restype = wintypes.HANDLE
        set_file_time = kernel32.SetFileTime
        set_file_time.argtypes = [wintypes.HANDLE,
                                  ctypes.POINTER(wintypes.FILETIME),
                                  ctypes.POINTER(wintypes.FILETIME),
                                  ctypes.POINTER(wintypes.FILETIME)]
        set_file_time.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        handle = create_file(
            str(path), 0x0080 | 0x0100, 0x00000007, None, 3, 0x00200000, None)
        handle_value = getattr(handle, "value", handle)
        invalid_handle = ctypes.c_void_p(-1).value
        if handle_value in (None, invalid_handle):
            raise ctypes.WinError(ctypes.get_last_error())
        descriptor = None
        transferred = False
        try:
            descriptor = msvcrt.open_osfhandle(
                handle_value, os.O_RDONLY | getattr(os, "O_BINARY", 0))
            transferred = True
            info = os.fstat(descriptor)
            observed = {
                "st_dev": int(info.st_dev), "st_ino": int(info.st_ino),
                "size": int(info.st_size), "mtime_ns": int(info.st_mtime_ns),
            }
            if (not stat_module.S_ISREG(info.st_mode)
                    or getattr(info, "st_nlink", 1) != 1
                    or not self._same_identity(observed, expected_identity)
                    or self._is_reparse(path)):
                raise OSError("staged restore handle identity is unsafe")
            windows_epoch_ns = 11_644_473_600 * 1_000_000_000
            filetime_value = (timestamp + windows_epoch_ns) // 100
            if (not 0 < filetime_value < 0xFFFFFFFFFFFFFFFF):
                raise OSError("staged restore timestamp is outside Windows range")
            file_time = wintypes.FILETIME(
                filetime_value & 0xFFFFFFFF, (filetime_value >> 32) & 0xFFFFFFFF)
            if not set_file_time(
                    handle, None, None, ctypes.byref(file_time)):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            if descriptor is not None:
                os.close(descriptor)
            elif not transferred:
                close_handle(handle)

    def _preserve_staged_mtime(self, path, source_identity, expected_identity,
                               cancel=None, *, parent=None, parent_identity=None):
        """Carry the source timestamp onto a newly staged owned media copy."""

        if (not isinstance(source_identity, dict)
                or type(source_identity.get("mtime_ns")) is not int
                or type(source_identity.get("size")) is not int):
            raise OSError("restore source timestamp evidence is invalid")
        self._check_cancel(cancel)
        if parent is None:
            parent = Path(path).parent
        before = self._exclusive_identity(path)
        if (not isinstance(expected_identity, dict)
                or (before["st_dev"], before["st_ino"], before["size"], before["mtime_ns"])
                != (expected_identity.get("st_dev"), expected_identity.get("st_ino"),
                    expected_identity.get("size"), expected_identity.get("mtime_ns"))):
            raise OSError("staged restore identity changed before timestamp update")
        self._assert_stable_parent(parent, parent_identity)
        if before["size"] != source_identity["size"]:
            raise OSError("staged restore size differs from source evidence")
        timestamp = source_identity["mtime_ns"]
        self._set_mtime_nofollow(path, timestamp, before)
        self._check_cancel(cancel)
        self._assert_stable_parent(parent, parent_identity)
        after = self._exclusive_identity(path)
        if (after["st_dev"], after["st_ino"], after["size"]) != (
                before["st_dev"], before["st_ino"], before["size"]):
            raise OSError("staged restore identity changed while preserving timestamp")
        return after

    def _restore_records(self, verified, restore_uuid, destination_root, stage_root, cancel,
                         *, copy_objects=True, package_identity=None):
        manifest = verified.manifest
        clip_map = {entry["clip_id"]: str(uuid5(UUID(restore_uuid), "clip:" + entry["clip_id"]))
                    for entry in manifest["entries"]}
        file_map = {row["file_id"]: str(uuid5(UUID(restore_uuid), "file:" + row["file_id"]))
                    for row in manifest["files"]}
        restore_index = self._restore_manifest_index(manifest, restore_uuid)
        trash_map = restore_index["trash_map"]
        trash_by_clip = restore_index["trash_by_clip"]
        album_map = {row["album_id"]: str(uuid5(UUID(restore_uuid), "album:" + row["album_id"]))
                     for row in manifest["albums"]}
        search_map = {row["search_id"]: str(uuid5(UUID(restore_uuid), "search:" + row["search_id"]))
                      for row in manifest["searches"]}
        files = []
        for row in manifest["files"]:
            self._check_cancel(cancel)
            clip_id = row["clip_id"]
            trash_row = trash_by_clip.get(clip_id)
            relative = row["relative_path"]
            if trash_row is not None:
                old_trash = trash_row["trash_id"]
                if relative.startswith(f".fthr-trash/{old_trash}/"):
                    relative = relative.replace(
                        f".fthr-trash/{old_trash}/", f".fthr-trash/{trash_map[old_trash]}/", 1)
                else:
                    relative = relative.replace(f"media/{clip_id}/",
                                                f".fthr-trash/{trash_map[old_trash]}/", 1)
            else:
                relative = relative.replace(
                    f"media/{clip_id}/", f"media/{clip_map[clip_id]}/", 1)
            target = self._restore_target(destination_root, relative)
            staged = self._restore_target(stage_root, relative)
            if copy_objects:
                staged_identity, staged_parent_identity = self._stage_object(
                    Path(verified.package_path), row["object_sha256"],
                    staged, stage_root, cancel, package_identity=package_identity)
                observed = self._preserve_staged_mtime(
                    staged, row["source_identity"], staged_identity, cancel,
                    parent=staged.parent, parent_identity=staged_parent_identity)
            else:
                if not target.is_file() or self._is_reparse(target):
                    raise OSError("published restore file is missing")
                observed = self._identity(target)
            files.append({"file_id": file_map[row.get("file_id", "")],
                          "clip_id": clip_map[clip_id], "role": row["role"],
                          "path": str(target), "path_key": self._key(target),
                          "binding_state": BindingState.TRASHED.value if trash_row else BindingState.CONTENT_VERIFIED.value,
                          "identity": observed, "content_sha256": row["object_sha256"],
                          "provenance": row.get("provenance")})
        primary_target = {}
        primary_original_target = {}
        fresh_files_by_new_clip = {}
        primary_identity_by_old_clip = {}
        for row, fresh in zip(manifest["files"], files, strict=True):
            fresh_files_by_new_clip.setdefault(fresh["clip_id"], []).append(fresh)
            if row["role"] == FileRole.PRIMARY.value:
                primary_target[row["clip_id"]] = fresh["path"]
                primary_identity_by_old_clip[row["clip_id"]] = fresh["identity"]
                primary_original_target[row["clip_id"]] = str(
                    self._restore_target(destination_root,
                                         f"media/{clip_map[row['clip_id']]}/{Path(row['relative_path']).name}"))
        for row in manifest["trash"]:
            if copy_objects and row["status"] == "TRASHED":
                clip_id = row["clip_id"]
                original = self._restore_target(
                    stage_root, f"media/{clip_map[clip_id]}/{Path(primary_target[clip_id]).name}")
                self._safe_parents(original.parent, stage_root, allow_missing=True)
                original.parent.mkdir(parents=True, exist_ok=True)
        entries = []
        for row in manifest["entries"]:
            clip_id = row["clip_id"]
            trash_row = trash_by_clip.get(clip_id)
            primary_identity = primary_identity_by_old_clip.get(clip_id)
            value = self._rebase_metadata(row.get("value", {}), clip_map, file_map,
                                          primary_identity)
            entries.append({"clip_id": clip_map[clip_id],
                            "path": None if trash_row else primary_target.get(clip_id),
                            "value": value})
        trash = []
        for row in manifest["trash"]:
            new_id = trash_map[row["trash_id"]]
            stored = primary_target[row["clip_id"]]
            member_rows = [dict(item) for item in fresh_files_by_new_clip.get(
                clip_map[row["clip_id"]], ())]
            canonical_members = [{key: member[key] for key in (
                "file_id", "role", "path", "path_key", "binding_state", "identity",
                "content_sha256", "provenance")} for member in sorted(
                    member_rows, key=lambda item: item["file_id"])]
            trash.append({"trash_id": new_id, "clip_id": clip_map[row["clip_id"]],
                          "status": row["status"], "created_utc": row["created_utc"],
                          "original_path": primary_original_target[row["clip_id"]],
                          "stored_path": str(stored), "members": canonical_members,
                          "error": row.get("error")})
        albums = []
        for row in manifest["albums"]:
            albums.append({"album_id": album_map[row["album_id"]], "name": row["name"],
                           "created_utc": row["created_utc"]})
        memberships = [{"album_id": album_map[row["album_id"]],
                        "clip_id": clip_map[row["clip_id"]]}
                       for row in manifest["memberships"] if row["album_id"] in album_map
                       and row["clip_id"] in clip_map]
        searches = []
        for row in manifest["searches"]:
            criteria = dict(row.get("criteria") or {})
            old_album_id = criteria.get("album_id")
            if old_album_id in album_map:
                criteria["album_id"] = album_map[old_album_id]
            elif old_album_id is not None:
                criteria["album_id"] = str(
                    uuid5(UUID(restore_uuid), "missing-album:" + str(old_album_id)))
            searches.append({"search_id": search_map[row["search_id"]], "name": row["name"],
                             "created_utc": row["created_utc"], "criteria": criteria})
        return {"entries": entries, "files": files, "trash": trash, "albums": albums,
                "memberships": memberships, "searches": searches}, clip_map

    def _publish_stage(self, stage_payload, destination_root, *,
                       expected_payload_identity=None, expected_parent_identity=None,
                       expected_destination_parent_identity=None):
        stage_payload = Path(stage_payload)
        destination_root = Path(destination_root)
        if destination_root.exists() or destination_root.is_symlink():
            raise FileExistsError("restore destination already exists")
        self._safe_parents(destination_root.parent, self.owned_root, allow_missing=False)
        if self._is_reparse(stage_payload) or not stage_payload.is_dir():
            raise OSError("restore staging payload is not a private directory")
        if expected_payload_identity is None:
            expected_payload_identity = self._stable_directory_identity(stage_payload)
        if expected_parent_identity is None:
            expected_parent_identity = self._stable_directory_identity(stage_payload.parent)
        self._assert_stable_parent(stage_payload.parent, expected_parent_identity)
        if not self._same_stable_identity(
                self._stable_directory_identity(stage_payload), expected_payload_identity):
            raise OSError("restore staging payload changed before publication")
        if expected_destination_parent_identity is None:
            expected_destination_parent_identity = self._stable_directory_identity(
                destination_root.parent)
        self._assert_stable_parent(
            destination_root.parent, expected_destination_parent_identity)
        self._assert_stable_parent(stage_payload.parent, expected_parent_identity)
        if not self._same_stable_identity(
                self._stable_directory_identity(stage_payload), expected_payload_identity):
            raise OSError("restore staging payload changed before publication")
        self._assert_stable_parent(
            destination_root.parent, expected_destination_parent_identity)
        rename_no_replace(stage_payload, destination_root)

    def _refresh_restore_records(self, records, *, cancel=None):
        files_by_clip = {}
        for row in records["files"]:
            self._check_cancel(cancel)
            digest, identity, _size = self._hash_file(
                row["path"], expected=row["identity"], cancel=cancel)
            if row.get("content_sha256") is not None and digest != row["content_sha256"]:
                raise OSError("published restore content changed before metadata commit")
            row["identity"] = identity
            files_by_clip.setdefault(row["clip_id"], []).append(row)
        for row in records["trash"]:
            self._check_cancel(cancel)
            members = [{key: member[key] for key in (
                "file_id", "role", "path", "path_key", "binding_state", "identity",
                "content_sha256", "provenance")} for member in sorted(
                    files_by_clip[row["clip_id"]], key=lambda item: item["file_id"])]
            row["members"] = members
        return records

    def _restore_covers(self, verified, clip_map, cancel, *, package_identity=None):
        if not verified.manifest["covers"]:
            return
        cover_root = Path(self.manager.backup_cover_root)
        database_guess = cover_root.with_suffix("")
        store = CoverAssetStore(database_guess)
        for row in verified.manifest["covers"]:
            self._check_cancel(cancel)
            self._assert_package_identity(verified.package_path, package_identity)
            old_clip = row["clip_id"]
            new_clip = clip_map.get(old_clip)
            if new_clip is None:
                raise OSError("cover references a clip omitted from the restore")
            reference, error = parse_cover_reference(row.get("reference"))
            if error is not None or reference is None:
                raise ValueError("backup contains an invalid cover reference")
            source = Path(verified.package_path) / "objects" / row["sha256"]
            payload = self._read_bounded(source, 32 * 1024 * 1024, cancel=cancel)
            if hashlib.sha256(payload).hexdigest() != row["sha256"]:
                raise OSError("backup cover object digest changed")
            checked = cover_reference_from_png(payload, revision=reference.revision)
            if checked.to_dict() != reference.to_dict():
                raise OSError("backup cover digest or dimensions do not match metadata")
            final = store.asset_path(new_clip, reference.revision)
            if final.exists():
                if self._hash_file(final, cancel=cancel)[0] != row["sha256"]:
                    raise FileExistsError("restored cover collides with another asset")
                continue
            store.ensure_staging()
            staged = store.staging / ("backup-" + str(uuid4()) + ".tmp")
            staged_parent_identity = self._assert_stable_parent(staged.parent)
            with staged.open("xb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
                info = os.fstat(output.fileno())
                staged_open_identity = {
                    "st_dev": int(info.st_dev), "st_ino": int(info.st_ino),
                    "size": int(info.st_size), "mtime_ns": int(info.st_mtime_ns),
                }
            self._assert_owned_temp(staged, staged_open_identity,
                                    staged.parent, staged_parent_identity)
            self._safe_parents(final.parent, Path(final.anchor), allow_missing=True)
            final.parent.mkdir(parents=True, exist_ok=True)
            final_parent_identity = self._assert_stable_parent(final.parent)
            try:
                self._assert_stable_parent(staged.parent, staged_parent_identity)
                self._assert_owned_temp(staged, staged_open_identity,
                                        staged.parent, staged_parent_identity)
                self._assert_stable_parent(final.parent, final_parent_identity)
                store.publish_exclusive(staged, new_clip, reference)
            except Exception:
                try:
                    if staged.exists():
                        self._remove_owned_temp(staged, staged_open_identity,
                                                staged.parent, staged_parent_identity)
                except OSError:
                    # A changed cover staging path remains for forensic resume.
                    pass
                raise

    @staticmethod
    def _rebase_metadata(value, clip_map, file_map, primary_identity):
        if not isinstance(value, dict):
            return value
        result = json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))
        draft = result.get("editor_draft")
        if (isinstance(draft, dict) and type(draft.get("version")) is int
                and draft.get("version") == 1
                and isinstance(draft.get("file"), dict)
                and isinstance(primary_identity, dict)):
            draft["file"] = dict(primary_identity)
        for key in ("editor_project", "project"):
            project = result.get(key)
            if (isinstance(project, dict) and type(project.get("version")) is int
                    and project.get("version") == 2
                    and isinstance(project.get("sources"), list)):
                project["sources"] = [file_map.get(source, source) for source in project["sources"]]
        return result

    def _restore_checkpoint(self, path, value):
        self._write_json(path, value)

    def _commit_restore_records(self, restore_uuid, manifest_sha256,
                                destination_root, records, cancel):
        """Commit metadata once and reconcile an exception through its receipt."""

        self._check_cancel(cancel)
        self._assert_current_root()
        expected_root = self._stable_directory_identity(self.owned_root)
        try:
            committed = self.manager.commit_backup_restore(
                restore_uuid, manifest_sha256, str(destination_root), records,
                owned_root=self.owned_root, imported_roots=self.imported_roots,
                expected_root=expected_root)
            return committed, None
        except Exception as commit_error:
            try:
                receipt = self.manager.get_restore_receipt(restore_uuid)
            except Exception as receipt_error:
                # A failed receipt query leaves the commit direction unknown;
                # preserve the original commit failure and leave all files.
                raise commit_error from receipt_error
            matches = (isinstance(receipt, dict)
                       and receipt.get("manifest_sha256") == manifest_sha256
                       and isinstance(receipt.get("destination_root"), str)
                       and self._key(receipt["destination_root"]) == self._key(destination_root))
            if not matches:
                # No matching receipt proves durability; never compensate media
                # or metadata after an indeterminate writer exception.
                raise commit_error from None
            reload_note = "restore metadata committed; manager cache refreshed"
            try:
                self.manager.reload()
            except Exception as reload_error:
                # Receipt durability wins over a transient worker-side reload failure.
                reload_note = "restore metadata committed; manager reload pending: " + str(reload_error)
            committed = dict(receipt)
            committed["already_imported"] = True
            return committed, reload_note

    def _restore_checkpoint_authority(self, data, restore_uuid, destination,
                                       stage_root, stage_payload, *, phase,
                                       check_identities=True):
        """Validate derived restore paths and stable directory ownership."""

        self._assert_current_root()
        self._require_format_version(data.get("format_version"),
                                     CHECKPOINT_FORMAT_VERSION,
                                     "restore checkpoint")
        expected_checkpoint = self._restore_checkpoint_path(self.owned_root, restore_uuid)
        if (not isinstance(data.get("checkpoint_path"), str)
                or self._key(data["checkpoint_path"]) != self._key(expected_checkpoint)):
            raise OSError("restore checkpoint path is not owned by this restore")
        expected_stage = Path(self.owned_root) / f".fthr-restore-{restore_uuid}"
        expected_payload = expected_stage / "payload"
        if (not isinstance(data.get("stage_root"), str)
                or self._key(data["stage_root"]) != self._key(expected_stage)
                or self._key(stage_root) != self._key(expected_stage)
                or self._key(stage_payload) != self._key(expected_payload)):
            raise OSError("restore staging paths are not derived from the restore UUID")
        if phase not in {"copying", "files_published", "committed"}:
            raise OSError("restore checkpoint has an unknown phase")
        if not check_identities:
            return
        if not self._same_stable_identity(
                self._stable_directory_identity(self.owned_root),
                data.get("owned_root_identity")):
            raise OSError("owned root identity changed during restore")
        if not self._same_stable_identity(
                self._stable_directory_identity(destination.parent),
                data.get("destination_parent_identity")):
            raise OSError("restore destination parent changed")
        if not self._same_stable_identity(
                self._stable_directory_identity(stage_root),
                data.get("stage_root_identity")):
            raise OSError("restore stage identity changed")
        if phase == "copying":
            if not self._same_stable_identity(
                    self._stable_directory_identity(stage_payload),
                    data.get("stage_payload_identity")):
                raise OSError("restore payload identity changed")
        elif phase == "files_published":
            if not destination.is_dir() or not self._same_stable_identity(
                    self._stable_directory_identity(destination),
                    data.get("published_payload_identity")):
                raise OSError("published restore destination identity changed")

    def restore_backup(self, package, destination, *, cancel=None, progress=None,
                       expected_manifest_sha256=None):
        self._check_cancel(cancel)
        self._assert_current_root()
        if expected_manifest_sha256 is not None:
            self._digest(expected_manifest_sha256, "expected manifest sha256")
        verified = self.verify_backup(package, cancel=cancel, progress=progress)
        if (expected_manifest_sha256 is not None
                and verified.manifest_sha256 != expected_manifest_sha256):
            raise OSError("backup manifest changed after restore confirmation")
        self._assert_roots()
        final_root = Path(self._absolute(destination, "restore destination"))
        if (not self._under(final_root, self.owned_root)
                or self._key(final_root) == self._key(self.owned_root)):
            raise OSError("restore destination must be a new owned subfolder")
        if any(self._under(final_root, root) or self._under(root, final_root)
               for root in self.imported_roots):
            raise OSError("restore destination overlaps an imported root")
        self._safe_parents(final_root.parent, self.owned_root, allow_missing=False)
        if final_root.exists() or final_root.is_symlink():
            raise FileExistsError("restore destination must be new")
        restore_uuid = str(uuid4())
        stage_root = Path(self.owned_root) / f".fthr-restore-{restore_uuid}"
        checkpoint_path = self._restore_checkpoint_path(self.owned_root, restore_uuid)
        if stage_root.exists() or checkpoint_path.exists():
            raise FileExistsError("restore UUID collision")
        stage_root.mkdir()
        stage_payload = stage_root / "payload"
        stage_payload.mkdir()
        checkpoint_identity = {
            "owned_root_identity": self._stable_directory_identity(self.owned_root),
            "destination_parent_identity": self._stable_directory_identity(final_root.parent),
            "stage_root_identity": self._stable_directory_identity(stage_root),
            "stage_payload_identity": self._stable_directory_identity(stage_payload),
        }
        checkpoint = {"format_version": CHECKPOINT_FORMAT_VERSION, "kind": "restore",
                      "package_path": str(Path(verified.package_path)),
                      "restore_uuid": restore_uuid, "manifest_sha256": verified.manifest_sha256,
                      "destination_root": str(final_root), "stage_root": str(stage_root),
                      "checkpoint_path": str(checkpoint_path), "phase": "copying",
                      "clips": len(verified.manifest["entries"]),
                      "files": len(verified.manifest["files"]),
                      "excluded": list(verified.excluded),
                      "published_payload_identity": None, **checkpoint_identity}
        self._restore_checkpoint_authority(
            checkpoint, restore_uuid, final_root, stage_root, stage_payload, phase="copying")
        self._restore_checkpoint(checkpoint_path, checkpoint)
        self._last_restore_checkpoint = str(checkpoint_path)
        claims = []
        reserved = False
        try:
            receipt = self.manager.get_restore_receipt(restore_uuid)
            if receipt is not None:
                if (receipt["manifest_sha256"] != verified.manifest_sha256
                        or self._key(receipt["destination_root"]) != self._key(final_root)):
                    raise OSError("restore receipt conflicts with this package")
                note = self._cleanup_restore_marker(
                    final_root, restore_uuid, verified.manifest_sha256)
                excluded = list(verified.excluded)
                if note:
                    excluded.append(note)
                return BackupResult(str(verified.package_path), True, len(verified.manifest["entries"]),
                                    len(verified.manifest["files"]), tuple(excluded),
                                    restore_uuid, receipt["destination_root"], True,
                                    str(checkpoint_path))
            claims = list(self._restore_claims(verified, restore_uuid, final_root))
            if not self._reserve(tuple(claims)):
                raise OSError("backup package is busy; restore was not started")
            reserved = True
            package_identity = self._package_identity(verified.package_path)
            records, clip_map = self._restore_records(
                verified, restore_uuid, final_root, stage_payload, cancel,
                package_identity=package_identity)
            self._check_cancel(cancel)
            self._assert_current_root()
            self._write_restore_marker(stage_payload, restore_uuid, verified.manifest_sha256)
            checkpoint["published_payload_identity"] = self._stable_directory_identity(stage_payload)
            self._restore_checkpoint_authority(
                checkpoint, restore_uuid, final_root, stage_root, stage_payload, phase="copying")
            self._verify_published_tree(stage_payload, verified, restore_uuid, cancel=cancel)
            self._restore_checkpoint(checkpoint_path, checkpoint)
            self._check_cancel(cancel)
            self._assert_current_root()
            self._publish_stage(
                stage_payload, final_root,
                expected_payload_identity=checkpoint["published_payload_identity"],
                expected_parent_identity=checkpoint["stage_root_identity"],
                expected_destination_parent_identity=checkpoint[
                    "destination_parent_identity"])
            if not self._same_stable_identity(
                    self._stable_directory_identity(final_root),
                    checkpoint["published_payload_identity"]):
                raise OSError("published restore identity changed during directory rename")
            self._verify_published_tree(final_root, verified, restore_uuid, cancel=cancel)
            records = self._refresh_restore_records(records, cancel=cancel)
            self._restore_covers(verified, clip_map, cancel,
                                 package_identity=package_identity)
            checkpoint["phase"] = "files_published"
            checkpoint["clip_ids"] = sorted(clip_map.values())
            self._restore_checkpoint(checkpoint_path, checkpoint)
            self._check_cancel(cancel)
            self._assert_current_root()
            self._restore_checkpoint_authority(
                checkpoint, restore_uuid, final_root, stage_root, stage_payload,
                phase="files_published")
            self._require_restore_marker(final_root, restore_uuid, verified.manifest_sha256)
            committed, commit_note = self._commit_restore_records(
                restore_uuid, verified.manifest_sha256, final_root, records, cancel)
            checkpoint["phase"] = "committed"
            checkpoint_error = None
            try:
                self._restore_checkpoint(checkpoint_path, checkpoint)
            except OSError as error:
                checkpoint_error = str(error)
            note = self._cleanup_restore_marker(
                final_root, restore_uuid, verified.manifest_sha256)
            excluded = list(verified.excluded)
            if commit_note:
                excluded.append(commit_note)
            if note:
                excluded.append(note)
            checkpoint["marker_cleanup"] = "pending" if note else "removed"
            try:
                self._restore_checkpoint(checkpoint_path, checkpoint)
            except OSError as error:
                checkpoint_error = str(error)
            if checkpoint_error:
                excluded.append("restore checkpoint pending: " + checkpoint_error)
            return BackupResult(str(verified.package_path), True, len(records["entries"]),
                                len(records["files"]), tuple(excluded), restore_uuid,
                                str(final_root), bool(committed.get("already_imported")),
                                str(checkpoint_path))
        finally:
            if reserved:
                self._release(tuple(claims))

    def resume_restore(self, checkpoint, *, cancel=None, progress=None):
        checkpoint_path = Path(self._absolute(checkpoint, "restore checkpoint"))
        data = self._strict_json(self._read_bounded(checkpoint_path, MAX_MANIFEST_BYTES))
        try:
            self._require_format_version(data.get("format_version"),
                                         CHECKPOINT_FORMAT_VERSION,
                                         "restore checkpoint")
        except ValueError:
            raise OSError("restore checkpoint is unsupported") from None
        if data.get("kind") != "restore":
            raise OSError("restore checkpoint is unsupported")
        restore_uuid = self._uuid(data.get("restore_uuid"), "restore_uuid")
        self._check_cancel(cancel)
        self._assert_current_root()
        self._assert_roots()
        expected_checkpoint = self._restore_checkpoint_path(self.owned_root, restore_uuid)
        if self._key(checkpoint_path) != self._key(expected_checkpoint):
            raise OSError("restore checkpoint is not the owned UUID checkpoint")
        self._last_restore_checkpoint = str(expected_checkpoint)
        destination = Path(data.get("destination_root", ""))
        if not self._under(destination, self.owned_root) or self._key(destination) == self._key(self.owned_root):
            raise OSError("restore checkpoint destination is outside the owned library")
        if any(self._under(destination, root) or self._under(root, destination)
               for root in self.imported_roots):
            raise OSError("restore checkpoint destination overlaps an imported root")
        stage_root = Path(self.owned_root) / f".fthr-restore-{restore_uuid}"
        stage_payload = stage_root / "payload"
        phase = data.get("phase")
        if phase not in {"copying", "files_published", "committed"}:
            raise OSError("restore checkpoint has an unknown phase")
        # The ownership-only phase checks avoid touching package or destination
        # files before the receipt lookup.  A receipt is authoritative even if
        # the user later removed the package or destination tree.
        self._restore_checkpoint_authority(
            data, restore_uuid, destination, stage_root, stage_payload,
            phase="committed", check_identities=False)
        receipt = self.manager.get_restore_receipt(restore_uuid)
        if receipt is not None:
            if (receipt["manifest_sha256"] != data.get("manifest_sha256")
                    or self._key(receipt["destination_root"]) != self._key(destination)):
                raise OSError("restore receipt conflicts with checkpoint")
            clips = data.get("clips")
            files = data.get("files")
            if (type(clips) is not int or clips < 0
                    or type(files) is not int or files < 0):
                raise OSError("restore checkpoint counts are invalid")
            excluded = list(data.get("excluded", ()))
            if destination.is_dir():
                note = self._cleanup_restore_marker(
                    destination, restore_uuid, data["manifest_sha256"])
                if note:
                    excluded.append(note)
            return BackupResult(str(data.get("package_path", "")), True, clips, files,
                                tuple(excluded), restore_uuid, receipt["destination_root"], True,
                                str(expected_checkpoint))
        if phase == "committed":
            raise OSError("committed restore receipt is missing")
        self._check_cancel(cancel)
        self._assert_current_root()
        package = self._package_path(data.get("package_path"))
        package_identity = self._package_identity(package)
        verified = self.verify_backup(package, cancel=cancel, progress=progress)
        if verified.manifest_sha256 != data.get("manifest_sha256"):
            raise OSError("restore package changed since checkpoint")
        if not stage_root.is_dir() or stage_root.is_symlink():
            raise OSError("restore checkpoint staging area is missing")
        if phase == "copying" and stage_payload.is_dir():
            self._restore_checkpoint_authority(
                data, restore_uuid, destination, stage_root, stage_payload, phase="copying")
        elif phase == "files_published":
            self._restore_checkpoint_authority(
                data, restore_uuid, destination, stage_root, stage_payload,
                phase="files_published")
        if phase == "copying" and not stage_payload.exists():
            published_identity = data.get("published_payload_identity")
            if (not destination.is_dir()
                    or not self._same_stable_identity(
                        self._stable_directory_identity(destination), published_identity)):
                raise OSError("restore crash gap has no verified published payload")
            phase = "files_published"
            data["phase"] = phase
            self._restore_checkpoint_authority(
                data, restore_uuid, destination, stage_root, stage_payload,
                phase="files_published")
        if phase == "copying" and not stage_payload.is_dir():
            raise OSError("restore checkpoint payload is missing")
        if phase == "files_published":
            self._restore_checkpoint_authority(
                data, restore_uuid, destination, stage_root, stage_payload,
                phase="files_published")
        claims = list(self._restore_claims(verified, restore_uuid, destination))
        reserved = False
        if not self._reserve(tuple(claims)):
            raise OSError("backup package is busy; restore resume was not started")
        reserved = True
        try:
            if phase == "copying":
                self._check_cancel(cancel)
                self._assert_current_root()
                records, clip_map = self._restore_records(
                    verified, restore_uuid, str(destination), str(stage_payload), cancel,
                    package_identity=package_identity)
                self._write_restore_marker(stage_payload, restore_uuid, verified.manifest_sha256)
                data["published_payload_identity"] = self._stable_directory_identity(stage_payload)
                self._restore_checkpoint_authority(
                    data, restore_uuid, destination, stage_root, stage_payload, phase="copying")
                self._verify_published_tree(stage_payload, verified, restore_uuid, cancel=cancel)
                self._restore_checkpoint(checkpoint_path, data)
                self._check_cancel(cancel)
                self._assert_current_root()
                self._publish_stage(
                    stage_payload, destination,
                    expected_payload_identity=data["published_payload_identity"],
                    expected_parent_identity=data["stage_root_identity"],
                    expected_destination_parent_identity=data[
                        "destination_parent_identity"])
                if not self._same_stable_identity(
                        self._stable_directory_identity(destination),
                        data["published_payload_identity"]):
                    raise OSError("published restore identity changed during directory rename")
                self._verify_published_tree(destination, verified, restore_uuid, cancel=cancel)
            elif phase == "files_published":
                self._check_cancel(cancel)
                self._assert_current_root()
                self._verify_published_tree(destination, verified, restore_uuid, cancel=cancel)
                records, clip_map = self._restore_records(
                    verified, restore_uuid, str(destination), str(stage_payload), cancel,
                    copy_objects=False, package_identity=package_identity)
            else:
                raise OSError("restore checkpoint has an unknown phase")
            records = self._refresh_restore_records(records, cancel=cancel)
            self._restore_covers(verified, clip_map, cancel,
                                 package_identity=package_identity)
            data["phase"] = "files_published"
            data["clip_ids"] = sorted(clip_map.values())
            self._check_cancel(cancel)
            self._assert_current_root()
            self._restore_checkpoint_authority(
                data, restore_uuid, destination, stage_root, stage_payload,
                phase="files_published")
            self._restore_checkpoint(checkpoint_path, data)
            self._check_cancel(cancel)
            self._assert_current_root()
            self._require_restore_marker(destination, restore_uuid, verified.manifest_sha256)
            committed, commit_note = self._commit_restore_records(
                restore_uuid, verified.manifest_sha256, destination, records, cancel)
            data["phase"] = "committed"
            checkpoint_error = None
            try:
                self._restore_checkpoint(checkpoint_path, data)
            except OSError as error:
                checkpoint_error = str(error)
            note = self._cleanup_restore_marker(
                destination, restore_uuid, verified.manifest_sha256)
            excluded = list(verified.excluded)
            if commit_note:
                excluded.append(commit_note)
            if note:
                excluded.append(note)
            data["marker_cleanup"] = "pending" if note else "removed"
            try:
                self._restore_checkpoint(checkpoint_path, data)
            except OSError as error:
                checkpoint_error = str(error)
            if checkpoint_error:
                excluded.append("restore checkpoint pending: " + checkpoint_error)
            return BackupResult(str(package), True, len(records["entries"]), len(records["files"]),
                                tuple(excluded), restore_uuid, str(destination),
                                bool(committed.get("already_imported")), str(checkpoint_path))
        finally:
            if reserved:
                self._release(tuple(claims))
