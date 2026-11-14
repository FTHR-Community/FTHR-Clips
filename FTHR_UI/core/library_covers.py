"""Managed, immutable library-cover assets and reference validation.

The metadata manager owns the transaction and identity checks.  This module
only owns the bounded PNG format checks, the private asset namespace, and the
safe stage/publish/retire primitives used by that manager.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import struct
import tempfile
import stat as stat_module
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from core.clip_mutations import rename_no_replace


COVER_REFERENCE_VERSION = 1
MAX_COVER_INPUT_BYTES = 32 * 1024 * 1024
MAX_COVER_PNG_BYTES = 8 * 1024 * 1024
MAX_COVER_PIXELS = 32 * 1024 * 1024
MAX_NORMALIZED_COVER_WIDTH = 1280
MAX_NORMALIZED_COVER_HEIGHT = 720


class CoverCommittedError(RuntimeError):
    """The cover transaction committed but RAM reconciliation needs retrying."""

    def __init__(self, message, *, reference=None, cleared=False):
        super().__init__(message)
        self.committed = True
        self.committed_reference = reference
        self.cleared = bool(cleared)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_REFERENCE_KEYS = frozenset(("version", "revision", "sha256", "width", "height"))


@dataclass(frozen=True)
class CoverReference:
    """The immutable reference stored in one clip's metadata entry."""

    revision: str
    sha256: str
    width: int
    height: int
    version: int = COVER_REFERENCE_VERSION

    def __post_init__(self):
        if type(self.version) is not int or self.version != COVER_REFERENCE_VERSION:
            raise ValueError("unsupported library cover reference version")
        _validate_uuid(self.revision, "cover revision")
        if (not isinstance(self.sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", self.sha256)):
            raise ValueError("invalid library cover SHA-256")
        _validate_dimensions(self.width, self.height)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "revision": self.revision,
            "sha256": self.sha256,
            "width": self.width,
            "height": self.height,
        }

    def __getitem__(self, key):
        return self.to_dict()[key]


def _validate_uuid(value, label):
    if not isinstance(value, str) or not _UUID_RE.fullmatch(value):
        raise ValueError(f"invalid {label}")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as error:
        raise ValueError(f"invalid {label}") from error
    if str(parsed) != value:
        raise ValueError(f"invalid {label}")


def _validate_dimensions(width, height):
    if (type(width) is not int or type(height) is not int
            or width <= 0 or height <= 0 or width * height > MAX_COVER_PIXELS):
        raise ValueError("invalid library cover dimensions")


def parse_cover_reference(value) -> tuple[CoverReference | None, str | None]:
    """Parse a stored reference without turning malformed metadata into no-cover."""
    if value is None:
        return None, None
    if not isinstance(value, dict):
        return None, "library cover reference is not an object"
    version = value.get("version")
    if type(version) is not int:
        return None, "library cover reference version is invalid"
    if version != COVER_REFERENCE_VERSION:
        return None, f"unsupported library cover reference version {version}"
    if set(value) != _REFERENCE_KEYS:
        return None, "library cover reference fields are invalid"
    try:
        return CoverReference(
            revision=value["revision"], sha256=value["sha256"],
            width=value["width"], height=value["height"], version=version,
        ), None
    except (TypeError, ValueError) as error:
        return None, str(error)


def cover_reference_from_png(png_bytes: bytes, *, revision: str | None = None) -> CoverReference:
    """Validate bounded PNG bytes and derive an immutable reference."""
    width, height = inspect_png(png_bytes, require_normalized=True)
    return CoverReference(
        revision=revision or str(uuid4()),
        sha256=hashlib.sha256(png_bytes).hexdigest(),
        width=width,
        height=height,
    )


def inspect_png(png_bytes: bytes, *, require_normalized: bool = False) -> tuple[int, int]:
    """Return PNG dimensions after checking a complete, static PNG stream."""
    if not isinstance(png_bytes, (bytes, bytearray, memoryview)):
        raise TypeError("library cover must be PNG bytes")
    payload = bytes(png_bytes)
    if len(payload) > MAX_COVER_INPUT_BYTES:
        raise ValueError("library cover exceeds the 32 MiB input limit")
    if require_normalized and len(payload) > MAX_COVER_PNG_BYTES:
        raise ValueError("normalized library cover exceeds the 8 MiB PNG limit")
    if len(payload) < len(_PNG_SIGNATURE) + 12 or not payload.startswith(_PNG_SIGNATURE):
        raise ValueError("library cover is not a PNG")

    offset = len(_PNG_SIGNATURE)
    dimensions = None
    saw_idat = False
    saw_iend = False
    while offset < len(payload):
        if len(payload) - offset < 12:
            raise ValueError("truncated library cover PNG")
        length = struct.unpack_from(">I", payload, offset)[0]
        end = offset + 12 + length
        if end > len(payload):
            raise ValueError("truncated library cover PNG chunk")
        chunk_type = payload[offset + 4:offset + 8]
        chunk = payload[offset + 8:offset + 8 + length]
        expected_crc = struct.unpack_from(">I", payload, offset + 8 + length)[0]
        import zlib
        if zlib.crc32(chunk_type + chunk) & 0xFFFFFFFF != expected_crc:
            raise ValueError("invalid library cover PNG checksum")
        if chunk_type == b"IHDR":
            if dimensions is not None or length != 13 or offset != len(_PNG_SIGNATURE):
                raise ValueError("invalid library cover PNG header")
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", chunk)
            _validate_dimensions(width, height)
            if bit_depth not in (1, 2, 4, 8, 16) or color_type not in (0, 2, 3, 4, 6):
                raise ValueError("unsupported library cover PNG format")
            if compression != 0 or filtering != 0 or interlace not in (0, 1):
                raise ValueError("unsupported library cover PNG encoding")
            if require_normalized and (
                    width > MAX_NORMALIZED_COVER_WIDTH
                    or height > MAX_NORMALIZED_COVER_HEIGHT):
                raise ValueError("library cover exceeds the normalized dimensions")
            dimensions = (width, height)
        elif chunk_type == b"acTL":
            raise ValueError("animated PNG covers are not supported")
        elif chunk_type == b"IDAT":
            if dimensions is None or not chunk:
                raise ValueError("invalid library cover PNG image data")
            saw_idat = True
        elif chunk_type == b"IEND":
            if length != 0 or dimensions is None or not saw_idat:
                raise ValueError("invalid library cover PNG end")
            saw_iend = True
            offset = end
            break
        offset = end
    if not saw_iend or offset != len(payload) or dimensions is None:
        raise ValueError("library cover PNG is incomplete")
    return dimensions


def _validate_component(value, label):
    _validate_uuid(value, label)


class CoverAssetStore:
    """Private filesystem namespace for immutable cover payloads."""

    def __init__(self, database_path):
        database = Path(os.fspath(database_path)).absolute()
        self.root = database.with_name(database.name + ".covers")
        self.staging = self.root / ".staging"

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            # A not-yet-created namespace is safe to create on first publish.
            return False
        attributes = getattr(info, "st_file_attributes", 0)
        reparse = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return path.is_symlink() or bool(attributes & reparse)

    def _ensure_existing_safe(self, path: Path):
        if self._is_reparse_point(path):
            raise OSError("library cover namespace cannot contain reparse points")

    def _ensure_directory(self, path: Path):
        self._ensure_existing_safe(path)
        if path.exists() and not path.is_dir():
            raise OSError("library cover namespace is not a private directory")
        path.mkdir(parents=True, exist_ok=True)
        self._ensure_existing_safe(path)
        if not path.is_dir():
            raise OSError("library cover namespace is not a private directory")

    def ensure_staging(self):
        self._ensure_directory(self.root)
        self._ensure_directory(self.staging)

    def asset_path(self, clip_id: str, revision: str) -> Path:
        _validate_component(clip_id, "clip id")
        _validate_component(revision, "cover revision")
        self._ensure_existing_safe(self.root)
        self._ensure_existing_safe(self.root / clip_id)
        return self.root / clip_id / f"{revision}.png"

    def asset_path_for_read(self, clip_id: str, revision: str) -> Path:
        """Build a cover path for a bounded worker read without filesystem I/O."""
        _validate_component(clip_id, "clip id")
        _validate_component(revision, "cover revision")
        return self.root / clip_id / f"{revision}.png"

    def stage_png(self, png_bytes: bytes) -> tuple[Path, CoverReference]:
        reference = cover_reference_from_png(png_bytes)
        self.ensure_staging()
        descriptor, name = tempfile.mkstemp(prefix="cover-", suffix=".tmp", dir=self.staging)
        path = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(bytes(png_bytes))
                output.flush()
                os.fsync(output.fileno())
        except (OSError, ValueError):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # Staging cleanup is best effort; preserve the original error.
                pass
            raise
        return path, reference

    def publish(self, staged: Path, clip_id: str, reference: CoverReference) -> Path:
        if not isinstance(staged, Path) or staged.parent != self.staging:
            raise ValueError("invalid staged library cover")
        final = self.asset_path(clip_id, reference.revision)
        self._ensure_directory(final.parent)
        self._ensure_existing_safe(final)
        if final.exists():
            raise FileExistsError("library cover revision already exists")
        os.replace(staged, final)
        return final

    def publish_exclusive(self, staged: Path, clip_id: str,
                          reference: CoverReference) -> Path:
        """Publish a staged cover without ever replacing an existing asset."""
        if not isinstance(staged, Path) or staged.parent != self.staging:
            raise ValueError("invalid staged library cover")
        final = self.asset_path(clip_id, reference.revision)
        self._ensure_directory(final.parent)
        self._ensure_existing_safe(final)
        if final.exists():
            raise FileExistsError("library cover revision already exists")
        rename_no_replace(staged, final)
        return final

    @staticmethod
    def remove_owned(path: Path) -> bool:
        if not isinstance(path, Path) or path.is_symlink() or not path.is_file():
            return False
        path.unlink()
        return True


def cleanup_retired_asset(database_path, clip_id: str, reference: CoverReference) -> bool:
    """Delete one old asset only when a fresh DB transaction finds no reference."""
    store = CoverAssetStore(database_path)
    try:
        path = store.asset_path(clip_id, reference.revision)
        database = Path(os.fspath(database_path)).absolute()
        if not database.is_file():
            return False
        with closing(sqlite3.connect(
                database.as_uri() + '?mode=rw', uri=True)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (5, 6):
                connection.rollback()
                return False
            rows = connection.execute("SELECT value FROM entries").fetchall()
            for (raw_value,) in rows:
                try:
                    metadata = json.loads(raw_value)
                except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
                    # Malformed or excessively deep metadata conservatively
                    # retains the asset during optional garbage collection.
                    return False
                if not isinstance(metadata, dict):
                    return False
                stored, error = parse_cover_reference(metadata.get("library_cover"))
                if error is not None:
                    return False
                if stored is not None and stored.revision == reference.revision:
                    return False
            store._ensure_existing_safe(path)
            try:
                before = os.stat(path, follow_symlinks=False)
            except OSError:
                # A missing or inaccessible asset cannot be proven owned.
                return False
            if (not stat_module.S_ISREG(before.st_mode)
                    or before.st_size <= 0
                    or before.st_size > MAX_COVER_PNG_BYTES
                    or store._is_reparse_point(path)):
                return False
            try:
                with path.open('rb') as source:
                    payload = source.read(MAX_COVER_PNG_BYTES + 1)
                after = os.stat(path, follow_symlinks=False)
            except OSError:
                # Read/stat races retain the asset rather than risking deletion.
                return False
            identity_fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns')
            if any(getattr(before, field) != getattr(after, field)
                   for field in identity_fields):
                return False
            if len(payload) != before.st_size:
                return False
            try:
                width, height = inspect_png(payload, require_normalized=True)
            except (TypeError, ValueError, RecursionError):
                # Invalid PNG bytes are not safe to classify as our asset.
                return False
            if (width != reference.width or height != reference.height
                    or hashlib.sha256(payload).hexdigest() != reference.sha256):
                return False
            # Revalidate the complete private namespace and the asset identity
            # immediately before unlink; a replacement during decode must win.
            for candidate in (store.root, path.parent, path):
                store._ensure_existing_safe(candidate)
            try:
                final_stat = os.stat(path, follow_symlinks=False)
            except OSError:
                # An unverifiable or replaced candidate is retained
                # conservatively; optional cleanup must never guess ownership.
                return False
            if (not stat_module.S_ISREG(final_stat.st_mode)
                    or any(getattr(final_stat, field) != getattr(after, field)
                           for field in identity_fields)):
                return False
            path.unlink()
            connection.commit()
            return True
    except (OSError, sqlite3.Error, ValueError, RecursionError):
        # Optional cleanup must never turn a successful metadata commit into
        # a failure or create a missing database as a side effect.
        return False
